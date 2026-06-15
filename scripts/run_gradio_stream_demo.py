import argparse
import os
import random
import re
import subprocess
import threading
import time

import cv2
import numpy as np
import gradio as gr

from fluxrt import StreamProcessor
from fluxrt.utils import crop_maximal_rectangle

default_prompt = "Turn this image into cyberpunk night, red and blue neon lamps, bokeh"
default_stream_url = "https://xumo-xumoent-vc-105-z0vpm.fast.nbcuni.com/live/master.m3u8"

stream_processor = None
input_tensor = None
output_tensor = None
resolution = None
use_int8 = False

processor_lock = threading.Lock()
current_video_id = 0
current_video_id_lock = threading.Lock()

current_input_frame = None
current_processed_frame = None
frame_lock = threading.Lock()

stream_status = "idle"
stream_status_lock = threading.Lock()
process_frame_counter = 0
stream_config_path = "configs/stream_demo_config.json"
filter_enabled = True
filter_enabled_lock = threading.Lock()
overlay_enabled = True
overlay_enabled_lock = threading.Lock()
stream_channel_map = {}
stream_channel_map_lock = threading.Lock()
repo_catalog_paths = {}

udp_writer = None
udp_writer_lock = threading.Lock()
udp_writer_dims = (0, 0)


def _get_udp_writer(width, height, fps=25):
    global udp_writer, udp_writer_dims
    with udp_writer_lock:
        if udp_writer is not None and udp_writer_dims != (width, height):
            try:
                udp_writer.terminate()
                udp_writer.wait(timeout=2)
            except Exception:
                pass
            udp_writer = None
            # Kill any orphaned ffmpeg writers left over from previous sessions
            import subprocess as _sp
            _sp.run(['pkill', '-f', 'ffmpeg.*udp://127.0.0.1:5000'], capture_output=True)

        if udp_writer is None:
            cmd = [
                'ffmpeg',
                '-hide_banner', '-loglevel', 'error',
                '-y',
                '-f', 'rawvideo',
                '-vcodec', 'rawvideo',
                '-pix_fmt', 'bgr24',
                '-s', f'{width}x{height}',
                '-r', str(fps),
                '-i', '-',
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-tune', 'zerolatency',
                '-g', '50',
                '-keyint_min', '50',
                '-sc_threshold', '0',
                '-x264-params', 'repeat-headers=1:keyint=50:min-keyint=50:scenecut=0',
                '-pix_fmt', 'yuv420p',
                '-f', 'mpegts',
                'udp://127.0.0.1:5000?pkt_size=1316'
            ]
            udp_writer = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            udp_writer_dims = (width, height)
        return udp_writer


def _write_to_udp(frame, fps=25):
    if frame is None:
        return
    h, w = frame.shape[:2]
    writer = _get_udp_writer(w, h, fps)
    if writer and writer.stdin:
        try:
            writer.stdin.write(frame.tobytes())
        except Exception:
            pass


def get_processor():
    global stream_processor, input_tensor, output_tensor, resolution

    if stream_processor is None:
        stream_processor = StreamProcessor(stream_config_path)
        if use_int8:
            stream_processor.enable_quantization()
        stream_processor.start()
        stream_processor.set_prompt(default_prompt)

        input_tensor = stream_processor.get_input_tensor()
        output_tensor = stream_processor.get_output_tensor()
        resolution = stream_processor.get_resolution()

    return stream_processor, input_tensor, output_tensor, resolution


def _workers_alive() -> bool:
    sp = stream_processor
    if sp is None:
        return False
    try:
        mi_proc = sp.model_inference_subprocess.process
        out_proc = sp.output_scheduler_subprocess.process
        return bool(
            mi_proc is not None
            and out_proc is not None
            and mi_proc.is_alive()
            and out_proc.is_alive()
        )
    except Exception:
        return False


def reset_processor(reason: str):
    global stream_processor, input_tensor, output_tensor, resolution
    set_status(f"recovering: {reason}")
    if stream_processor is not None:
        try:
            stream_processor.stop()
        except Exception:
            pass
    stream_processor = None
    input_tensor = None
    output_tensor = None
    resolution = None


def to_bgr(frame):
    if frame is None:
        return None
    return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

SHOW_NAMES = [
    "Quantum Flux TV",
    "Synthwave Sunday",
    "Cybernetic News Network",
    "Midnight AI Broadcast",
    "Neon Glow Live",
    "Neural Network Morning",
    "AI Visionaries",
    "The Matrix Feed",
]

_current_show_str = SHOW_NAMES[0]
_show_last_changed = time.time()

def add_tv_overlay(frame_bgr: np.ndarray) -> np.ndarray:
    global _current_show_str, _show_last_changed
    now = time.time()
    
    if now - _show_last_changed > 15.0:  # Change every 15 seconds
        _current_show_str = random.choice(SHOW_NAMES)
        _show_last_changed = now
        
    out = frame_bgr.copy()
    h, w = out.shape[:2]

    # Lower-left title package.
    overlay = out.copy()
    cv2.rectangle(overlay, (16, h - 78), (w - 16, h - 18), (0, 0, 0), -1)
    cv2.rectangle(overlay, (26, h - 66), (110, h - 28), (0, 0, 220), -1)
    cv2.addWeighted(overlay, 0.6, out, 0.4, 0, out)

    cv2.putText(out, "LIVE", (36, h - 40), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2)
    cv2.putText(out, _current_show_str, (126, h - 38), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)

    # Upper-right numeric clock.
    current_time_str = time.strftime("%H:%M:%S")
    tw, th = cv2.getTextSize(current_time_str, cv2.FONT_HERSHEY_SIMPLEX, 0.72, 2)[0]
    x2 = w - 18
    x1 = max(12, x2 - tw - 24)
    y1 = 16
    y2 = y1 + th + 18
    cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 0), -1)
    cv2.rectangle(out, (x1, y1), (x2, y2), (70, 70, 70), 1)
    cv2.putText(out, current_time_str, (x1 + 12, y2 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (235, 235, 235), 2)
    
    return out


def apply_overlay(frame_bgr: np.ndarray) -> np.ndarray:
    if frame_bgr is None:
        return frame_bgr
    try:
        return add_tv_overlay(frame_bgr)
    except Exception as exc:
        print(f"Overlay error: {exc}")
        return frame_bgr


def set_filter_enabled(enabled: bool):
    global filter_enabled
    with filter_enabled_lock:
        filter_enabled = bool(enabled)
    set_status(f"filter: {'on' if filter_enabled else 'off'}")


def is_filter_enabled() -> bool:
    with filter_enabled_lock:
        return filter_enabled


def set_overlay_enabled(enabled: bool):
    global overlay_enabled
    with overlay_enabled_lock:
        overlay_enabled = bool(enabled)
    set_status(f"overlay: {'on' if overlay_enabled else 'off'}")


def is_overlay_enabled() -> bool:
    with overlay_enabled_lock:
        return overlay_enabled


def parse_m3u_channels(m3u_text: str) -> list[tuple[str, str]]:
    channels: list[tuple[str, str]] = []
    if not m3u_text:
        return channels

    pending_name: str | None = None
    for raw_line in m3u_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name = line.split(",", 1)[-1].strip() if "," in line else ""
            pending_name = re.sub(r"\s+", " ", name) or "Untitled"
            continue
        if line.startswith("#"):
            continue
        if line.startswith("http://") or line.startswith("https://"):
            channel_name = pending_name or f"Channel {len(channels) + 1}"
            channels.append((channel_name, line))
            pending_name = None
    return channels


def _catalog_dir() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    return os.path.join(repo_root, "iptv-streams")


def discover_repo_catalogs() -> dict[str, str]:
    base = _catalog_dir()
    if not os.path.isdir(base):
        return {}

    out: dict[str, str] = {}
    for name in sorted(os.listdir(base)):
        if not name.endswith(".m3u"):
            continue
        key = name[:-4]
        out[key] = os.path.join(base, name)
    return out


def _load_repo_channels(catalog_key: str) -> list[tuple[str, str]]:
    path = repo_catalog_paths.get(catalog_key)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return parse_m3u_channels(f.read())
    except Exception:
        return []


def load_repo_catalog_choice(catalog_key: str):
    channels = _load_repo_channels(catalog_key)
    with stream_channel_map_lock:
        stream_channel_map.clear()
        for idx, (name, url) in enumerate(channels, start=1):
            label = f"{name} [{idx}]"
            stream_channel_map[label] = url

    if not channels:
        set_status(f"catalog {catalog_key}: no channels parsed")
        return gr.update(choices=[], value=None), gr.update()

    first_name = next(iter(stream_channel_map.keys()))
    first_url = stream_channel_map[first_name]
    set_status(f"catalog {catalog_key}: loaded {len(channels)} channels")
    return gr.update(choices=list(stream_channel_map.keys()), value=first_name), gr.update(value=first_url)


def load_m3u_catalog(m3u_text: str):
    channels = parse_m3u_channels(m3u_text)
    with stream_channel_map_lock:
        stream_channel_map.clear()
        for name, url in channels:
            stream_channel_map[name] = url

    if not channels:
        set_status("m3u catalog: no channels parsed")
        return gr.update(choices=[], value=None), gr.update()

    first_name, first_url = channels[0]
    set_status(f"m3u catalog loaded: {len(channels)} channels")
    return (
        gr.update(choices=[name for name, _ in channels], value=first_name),
        gr.update(value=first_url),
    )


def apply_channel_choice(channel_name: str):
    with stream_channel_map_lock:
        url = stream_channel_map.get(channel_name, "")
    if not url:
        set_status("channel select: no URL found")
        return gr.update()
    set_status(f"channel selected: {channel_name}")
    return gr.update(value=url)


def render_frame(frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if frame_bgr is None:
        return frame_bgr, frame_bgr

    frame_with_overlay = apply_overlay(frame_bgr) if is_overlay_enabled() else frame_bgr
    if is_filter_enabled():
        _, processed = process_frame(frame_with_overlay)
        return frame_with_overlay, processed
    return frame_with_overlay, frame_with_overlay

def to_rgb(frame):
    if frame is None:
        return None
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


def process_frame(frame):
    global process_frame_counter
    if stream_processor is not None and not _workers_alive():
        reset_processor("worker down")

    _, local_input_tensor, local_output_tensor, local_resolution = get_processor()
    frame = crop_maximal_rectangle(
        frame, local_resolution["height"], local_resolution["width"]
    )

    with processor_lock:
        local_input_tensor.copy_from(frame)
        processed = local_output_tensor.to_numpy()

    process_frame_counter += 1
    if process_frame_counter % 90 == 0:
        p_min = int(processed.min())
        p_max = int(processed.max())
        p_mean = float(processed.mean())
        if p_max == 0:
            set_status("processed output is all zeros (inference likely not running)")
        print(
            f"[stream-demo] processed stats: min={p_min} max={p_max} mean={p_mean:.2f}"
        )

    return frame, processed


def set_prompt(prompt: str):
    sp, _, _, _ = get_processor()
    sp.set_prompt(prompt)


def set_reference_image_ui(image):
    sp, _, _, _ = get_processor()
    try:
        if image is not None:
            image = np.asarray(image)
            if image.ndim == 2:
                image = np.stack([image] * 3, axis=-1)
            if image.shape[-1] == 4:
                image = image[:, :, :3]
            image = image.astype(np.uint8, copy=False)
        sp.set_reference_image(image)
        set_status("reference image updated")
    except Exception as exc:
        set_status(f"reference image unavailable: {exc}")


def set_status(value: str):
    global stream_status
    with stream_status_lock:
        stream_status = value


def get_status() -> str:
    with stream_status_lock:
        return stream_status


def get_worker_health() -> str:
    sp = stream_processor
    if sp is None:
        return "workers: not initialized"

    try:
        mi_proc = sp.model_inference_subprocess.process
        out_proc = sp.output_scheduler_subprocess.process
        mi_alive = bool(mi_proc is not None and mi_proc.is_alive())
        out_alive = bool(out_proc is not None and out_proc.is_alive())
        return f"workers: inference={mi_alive} scheduler={out_alive}"
    except Exception:
        return "workers: unknown"


def _local_video_loop(video_path: str, video_id: int):
    global current_input_frame, current_processed_frame
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        set_status("local open failed")
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    frame_time = 1.0 / fps
    set_status("local live")

    try:
        while True:
            with current_video_id_lock:
                if current_video_id != video_id:
                    break

            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue

            start = time.time()
            try:
                input_frame, processed = render_frame(frame)
                _write_to_udp(processed, fps=int(fps))
            except Exception as exc:
                set_status(f"local processing error: {exc}")
                time.sleep(0.1)
                continue

            with frame_lock:
                current_input_frame = to_rgb(input_frame)
                current_processed_frame = to_rgb(processed)

            time.sleep(max(0, frame_time - (time.time() - start)))
    finally:
        cap.release()


def _open_stream_capture(stream_url: str):
    cap = cv2.VideoCapture(stream_url)
    if cap.isOpened():
        return cap

    cap.release()
    return cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)


def _stream_loop(stream_url: str, video_id: int):
    global current_input_frame, current_processed_frame
    cap = _open_stream_capture(stream_url)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    frame_time = 1.0 / 25

    try:
        while True:
            with current_video_id_lock:
                if current_video_id != video_id:
                    break

            if not cap.isOpened():
                set_status("stream reconnecting")
                time.sleep(0.5)
                cap.release()
                cap = _open_stream_capture(stream_url)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                continue

            ok, frame = cap.read()
            if not ok:
                set_status("stream reconnecting")
                time.sleep(0.5)
                cap.release()
                cap = _open_stream_capture(stream_url)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                continue

            set_status("stream live")
            start = time.time()
            try:
                input_frame, processed = render_frame(frame)
                _write_to_udp(processed, fps=25)
            except Exception as exc:
                set_status(f"stream processing error: {exc}")
                time.sleep(0.1)
                continue

            with frame_lock:
                current_input_frame = to_rgb(input_frame)
                current_processed_frame = to_rgb(processed)

            time.sleep(max(0, frame_time - (time.time() - start)))
    finally:
        cap.release()


def start_local_video(video_path: str | None):
    global current_video_id, current_input_frame, current_processed_frame

    with current_video_id_lock:
        current_video_id += 1
        my_id = current_video_id

    with frame_lock:
        current_input_frame = None
        current_processed_frame = None

    if not video_path:
        set_status("idle")
        return

    t = threading.Thread(target=_local_video_loop, args=(video_path, my_id), daemon=True)
    t.start()


def start_stream_video(stream_url: str | None):
    global current_video_id

    if stream_url is None:
        set_status("stream url missing")
        return

    stream_url = stream_url.strip()
    if not stream_url:
        set_status("stream url missing")
        return

    with current_video_id_lock:
        current_video_id += 1
        my_id = current_video_id

    set_status("stream connecting")
    t = threading.Thread(target=_stream_loop, args=(stream_url, my_id), daemon=True)
    t.start()


def stop_video_source():
    global current_video_id
    with current_video_id_lock:
        current_video_id += 1
    set_status("idle")


def poll_video():
    with frame_lock:
        status = f"{get_status()} | {get_worker_health()}"
        return current_input_frame, current_processed_frame, status


def switch_mode(mode: str, request: gr.Request | None):
    if mode == "webcam":
        stop_video_source()

    webcam_visible = mode == "webcam"
    local_visible = mode == "local"
    stream_visible = mode == "stream"
    file_visible = mode == "local"

    return (
        gr.update(visible=webcam_visible),
        gr.update(visible=(local_visible or stream_visible)),
        gr.update(visible=webcam_visible),
        gr.update(visible=(local_visible or stream_visible)),
        gr.update(visible=file_visible),
        gr.update(visible=stream_visible),
        gr.update(active=(local_visible or stream_visible)),
    )


def process_webcam(frame):
    if frame is None:
        return None

    _, processed = render_frame(to_bgr(frame))

    _write_to_udp(processed.copy(), fps=25)
    return to_rgb(processed)


def main():
    global use_int8, stream_config_path
    parser = argparse.ArgumentParser(description="Run FluxRT Stream Gradio demo.")
    parser.add_argument("--int8", action="store_true", help="Enable int8 quantization")
    parser.add_argument(
        "--config-path",
        type=str,
        default="configs/stream_demo_config.json",
        help="Stream processor config path",
    )
    parser.add_argument(
        "--server-port", type=int, default=7861, help="Port for stream demo app"
    )
    parser.add_argument(
        "--server-name", type=str, default="0.0.0.0", help="Bind address"
    )
    args, _ = parser.parse_known_args()
    use_int8 = args.int8
    stream_config_path = args.config_path

    global repo_catalog_paths
    get_processor()
    repo_catalog_paths = discover_repo_catalogs()
    catalog_choices = list(repo_catalog_paths.keys())
    default_catalog = catalog_choices[0] if catalog_choices else None
    with gr.Blocks() as demo:
        mode = gr.Radio(
            choices=["webcam", "local", "stream"],
            value="stream",
            label="Mode",
        )

        with gr.Column(visible=False) as webcam_output_col:
            webcam_output = gr.Image(streaming=True, label="Processed stream")

        with gr.Column(visible=True) as source_output_col:
            source_output = gr.Image(label="Processed stream")
            source_input = gr.Image(label="Input stream")
            source_status = gr.Textbox(label="Source status", value="idle")

        source_timer = gr.Timer(value=0.04, active=True)

        with gr.Row():
            with gr.Column(visible=False) as webcam_input_col:
                webcam_input = gr.Image(
                    sources=["webcam"],
                    streaming=True,
                    type="numpy",
                    label="Webcam",
                )

            with gr.Column(visible=True) as source_input_col:
                with gr.Column(visible=False) as local_controls:
                    video_file = gr.File(
                        label="Choose local video",
                        file_count="single",
                        file_types=["video"],
                        type="filepath",
                    )

                with gr.Column(visible=True) as stream_controls:
                    with gr.Row():
                        catalog_choice = gr.Dropdown(
                            label="Catalog Group",
                            choices=catalog_choices,
                            value=default_catalog,
                            allow_custom_value=False,
                            filterable=True,
                            interactive=True,
                        )
                        load_catalog_btn = gr.Button("Reload Catalog")
                    with gr.Row():
                        channel_choice = gr.Dropdown(
                            label="Channel Choice",
                            choices=[],
                            allow_custom_value=False,
                            filterable=True,
                            interactive=True,
                        )
                    stream_url = gr.Textbox(
                        label="Stream URL (custom field)",
                        value=default_stream_url,
                        lines=1,
                    )
                    with gr.Row():
                        apply_channel_btn = gr.Button("Use Selected Channel")
                        stream_start_btn = gr.Button("Start Stream")
                        stream_stop_btn = gr.Button("Stop")

                    with gr.Accordion("Paste Custom M3U (optional)", open=False):
                        m3u_catalog = gr.Textbox(
                            label="Custom M3U Catalog",
                            lines=10,
                            value="",
                            placeholder="#EXTM3U\n#EXTINF:-1,Channel Name\nhttps://example.com/live.m3u8",
                        )

            prompt = gr.Textbox(value=default_prompt, label="Prompt", lines=3)
            filter_toggle = gr.Checkbox(value=True, label="Enable AI Filter")
            with gr.Row():
                overlay_on_btn = gr.Button("Overlay On")
                overlay_off_btn = gr.Button("Overlay Off")

            ref_image_input = gr.Image(
                label="Reference Image",
                type="numpy",
                sources=["upload"],
                image_mode="RGB",
            )

        mode.change(
            switch_mode,
            inputs=mode,
            outputs=[
                webcam_output_col,
                source_output_col,
                webcam_input_col,
                source_input_col,
                local_controls,
                stream_controls,
                source_timer,
            ],
        )

        webcam_input.stream(
            process_webcam,
            inputs=webcam_input,
            outputs=[webcam_output],
            stream_every=0.04,
            concurrency_limit=1,
        )

        video_file.change(start_local_video, inputs=video_file, outputs=None)

        if default_catalog is not None:
            initial_channels_update, initial_url_update = load_repo_catalog_choice(default_catalog)
            channel_choice.value = initial_channels_update["value"]
            if "value" in initial_url_update:
                stream_url.value = initial_url_update["value"]

        catalog_choice.change(
            load_repo_catalog_choice,
            inputs=[catalog_choice],
            outputs=[channel_choice, stream_url],
        )

        load_catalog_btn.click(
            load_repo_catalog_choice,
            inputs=[catalog_choice],
            outputs=[channel_choice, stream_url],
        )

        m3u_catalog.change(
            load_m3u_catalog,
            inputs=[m3u_catalog],
            outputs=[channel_choice, stream_url],
        )

        apply_channel_btn.click(
            apply_channel_choice,
            inputs=[channel_choice],
            outputs=[stream_url],
        )

        channel_choice.change(
            apply_channel_choice,
            inputs=[channel_choice],
            outputs=[stream_url],
        )

        stream_start_btn.click(
            start_stream_video,
            inputs=[stream_url],
            outputs=None,
        )

        stream_stop_btn.click(stop_video_source, outputs=None)

        source_timer.tick(
            poll_video,
            outputs=[source_input, source_output, source_status],
        )

        prompt.change(set_prompt, inputs=prompt, outputs=None)
        filter_toggle.change(set_filter_enabled, inputs=filter_toggle, outputs=None)
        overlay_on_btn.click(lambda: set_overlay_enabled(True), outputs=None)
        overlay_off_btn.click(lambda: set_overlay_enabled(False), outputs=None)

        ref_image_input.change(
            set_reference_image_ui,
            inputs=ref_image_input,
            outputs=None,
        )

    demo.queue(default_concurrency_limit=1).launch(
        server_name=args.server_name,
        server_port=args.server_port,
    )


if __name__ == "__main__":
    main()
