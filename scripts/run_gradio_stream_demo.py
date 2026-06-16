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

default_prompt = "claymation"
default_stream_url = "https://30a-tv.com/feeds/masters/30atv.m3u8"
default_music_radio_url = "http://uk2.internet-radio.com:8024/"
default_music_station_name = "internet-radio.com default station"

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
music_station_map = {}
music_station_map_lock = threading.Lock()
repo_catalog_paths = {}

music_station_map[default_music_station_name] = default_music_radio_url

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
                '-preset', 'ultrafast',
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


def load_music_station_catalog(m3u_text: str):
    stations = parse_m3u_channels(m3u_text)
    with music_station_map_lock:
        music_station_map.clear()
        for name, url in stations:
            music_station_map[name] = url

    if not stations:
        set_status("music station catalog: no stations parsed")
        return gr.update(choices=[], value=None), gr.update()

    first_name, first_url = stations[0]
    set_status(f"music station catalog loaded: {len(stations)} stations")
    return gr.update(choices=[name for name, _ in stations], value=first_name), gr.update(value=first_url)


def apply_music_station_choice(station_name: str):
    with music_station_map_lock:
        url = music_station_map.get(station_name, "")
    if not url:
        return gr.update()
    set_status(f"music station selected: {station_name}")
    return gr.update(value=url)


def _repo_root() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(script_dir)


def _radio_stations_dir() -> str:
    return os.path.join(_repo_root(), "radio-stations")


def load_music_stations_from_repo_files() -> list[str]:
    base = _radio_stations_dir()
    parsed: list[tuple[str, str]] = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if not name.endswith(".m3u"):
                continue
            path = os.path.join(base, name)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    entries = parse_m3u_channels(f.read())
            except Exception:
                continue
            for station_name, station_url in entries:
                label = f"{os.path.splitext(name)[0]} | {station_name}"
                parsed.append((label, station_url))

    with music_station_map_lock:
        music_station_map.clear()
        for label, url in parsed:
            music_station_map[label] = url
        if not music_station_map:
            music_station_map[default_music_station_name] = default_music_radio_url
        return list(music_station_map.keys())


def start_musicgen_stream(
    radio_url: str,
    base_prompt: str,
    sample_seconds: int,
    gen_seconds: int,
    stream_delay_seconds: float,
    crossfade_seconds: float,
):
    if not radio_url or not radio_url.strip():
        return "musicgen: radio URL required"

    env = os.environ.copy()
    env["RADIO_URL"] = radio_url.strip()
    env["MUSICGEN_BASE_PROMPT"] = (base_prompt or "experimental electronic sound art").strip()
    env["MUSICGEN_SAMPLE_SECONDS"] = str(int(sample_seconds))
    env["MUSICGEN_GEN_SECONDS"] = str(int(gen_seconds))
    env["MUSICGEN_TOP_K"] = "250"
    env["MUSICGEN_TEMPERATURE"] = "1.0"
    env["MUSICGEN_PARALLEL_CLIPS"] = "2"
    env["MUSICGEN_SEED"] = "-1"
    env["MUSICGEN_STREAM_DELAY_SECONDS"] = str(float(stream_delay_seconds))
    env["MUSICGEN_CROSSFADE_SECONDS"] = str(float(crossfade_seconds))
    env["MUSICGEN_PAUSE_SECONDS"] = "0"
    env["MUSICGEN_BOOTSTRAP_CLIPS"] = "24"
    env["MUSICGEN_AUDIO_UDP_URL"] = "udp://127.0.0.1:5002?pkt_size=1316"

    cmd = ["bash", "-lc", "cd /home/gschi/FluxRT && scripts/start_musicgen_radio_plus_musicGEN.sh"]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "unknown error").strip()
        set_status(f"musicgen start failed: {msg}")
        return f"musicgen start failed: {msg}"

    set_status(f"broadcast waiting on backing track: buffering {float(stream_delay_seconds):.0f}s")
    return (
        (result.stdout or "musicgen stream started").strip()
        + f"\nBroadcast waiting on backing track buffer ({float(stream_delay_seconds):.0f}s)."
    )


def stop_musicgen_stream():
    cmd = ["bash", "-lc", "cd /home/gschi/FluxRT && scripts/stop_musicgen_radio_plus_musicGEN.sh"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    msg = (result.stdout or result.stderr or "musicgen stopped").strip()
    set_status("musicgen stream stopped")
    return msg


def start_fanout_with_music(enable_youtube: bool, enable_twitch: bool, enable_facebook: bool):
    if not (enable_youtube or enable_twitch or enable_facebook):
        set_status("fanout start skipped: no platform enabled")
        return "fanout start skipped: enable at least one platform"

    enable_youtube_str = "1" if enable_youtube else "0"
    enable_twitch_str = "1" if enable_twitch else "0"
    enable_facebook_str = "1" if enable_facebook else "0"

    launch_cmd = (
        "cd /home/gschi/FluxRT && "
        "scripts/streaming/stop_rtmp_fanout.sh || true; "
        f"ENABLE_YOUTUBE={enable_youtube_str} ENABLE_TWITCH={enable_twitch_str} ENABLE_FACEBOOK={enable_facebook_str} AUDIO_SOURCE_MODE=url "
        "STARTUP_BARS_SECONDS=0 WAIT_FOR_VIDEO_READY=0 "
        "AUDIO_INPUT_URL='udp://127.0.0.1:5002?pkt_size=1316' "
        "scripts/streaming/start_rtmp_fanout.sh >> /tmp/fluxrt-rtmp-fanout-launch.log 2>&1"
    )
    try:
        subprocess.Popen(["bash", "-lc", launch_cmd])
    except Exception as exc:
        msg = str(exc)
        set_status(f"fanout launch failed: {msg}")
        return f"fanout launch failed: {msg}"

    set_status("fanout launching in background (bars -> live handoff)")
    return "fanout launch started in background; check /tmp/fluxrt-rtmp-fanout.log"


def _latest_musicgen_clip() -> str | None:
    output_dir = os.path.join(_repo_root(), "musicgen_output_plus_musicGEN")
    if not os.path.isdir(output_dir):
        return None

    candidates = [
        os.path.join(output_dir, name)
        for name in os.listdir(output_dir)
        if name.endswith(".wav") and name.startswith("musicgen_clip_")
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _read_marker(marker_name: str) -> str | None:
    marker_path = os.path.join(_repo_root(), "musicgen_output_plus_musicGEN", marker_name)
    if not os.path.isfile(marker_path):
        return None
    try:
        path = open(marker_path, "r", encoding="utf-8").read().strip()
    except Exception:
        return None
    if not path:
        return None
    return path if os.path.isfile(path) else None


def _clip_index_from_path(path: str | None) -> int | None:
    if not path:
        return None
    name = os.path.basename(path)
    m = re.search(r"musicgen_clip_(\d+)\.wav$", name)
    if not m:
        return None
    return int(m.group(1))


def poll_musicgen_preview():
    now_clip = _read_marker("now_playing_path.txt")
    latest_clip = _latest_musicgen_clip()

    output_dir = os.path.join(_repo_root(), "musicgen_output_plus_musicGEN")
    all_clips = []
    if os.path.isdir(output_dir):
        for name in os.listdir(output_dir):
            if name.startswith("musicgen_clip_") and name.endswith(".wav"):
                all_clips.append(os.path.join(output_dir, name))

    all_clips.sort(key=lambda p: _clip_index_from_path(p) or -1)

    now_value = now_clip or latest_clip
    now_idx = _clip_index_from_path(now_value)

    edit_value = None
    if now_idx is not None:
        for clip in all_clips:
            clip_idx = _clip_index_from_path(clip)
            if clip_idx is not None and clip_idx > now_idx:
                edit_value = clip
                break
    if edit_value is None:
        marker_edit = _read_marker("editing_path.txt")
        marker_idx = _clip_index_from_path(marker_edit)
        if marker_edit and marker_idx is not None and (now_idx is None or marker_idx > now_idx):
            edit_value = marker_edit

    if now_value is None and edit_value is None:
        return (
            gr.update(value=None),
            gr.update(value=None),
            "now feed: no clip",
            "edit feed: no clip",
        )

    now_text = f"now feed: {os.path.basename(now_value)}" if now_value else "now feed: waiting"
    edit_text = f"edit feed: {os.path.basename(edit_value)}" if edit_value else "edit feed: waiting for next clip"
    return gr.update(value=now_value), gr.update(value=edit_value), now_text, edit_text


def render_frame(frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if frame_bgr is None:
        return frame_bgr, frame_bgr

    frame_with_overlay = apply_overlay(frame_bgr) if is_overlay_enabled() else frame_bgr
    if is_filter_enabled():
        try:
            _, processed = process_frame(frame_with_overlay)
            return frame_with_overlay, processed
        except Exception as exc:
            set_status(f"filter degraded: {exc}")
            return frame_with_overlay, frame_with_overlay
    return frame_with_overlay, frame_with_overlay

def to_rgb(frame):
    if frame is None:
        return None
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


def _placeholder_rgb(message: str, width: int = 640, height: int = 360) -> np.ndarray:
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    cv2.rectangle(canvas, (0, 0), (width, height), (12, 12, 12), -1)
    cv2.putText(
        canvas,
        "FluxRT Loading",
        (24, 56),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (210, 210, 210),
        2,
    )

    msg = re.sub(r"\s+", " ", (message or "waiting for frames").strip())
    if len(msg) > 64:
        msg = msg[:61] + "..."
    cv2.putText(
        canvas,
        msg,
        (24, 104),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (160, 220, 255),
        2,
    )
    return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)


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
        input_frame = current_input_frame
        processed_frame = current_processed_frame

    if input_frame is None:
        input_frame = _placeholder_rgb(f"Input: {status}")
    if processed_frame is None:
        if current_input_frame is not None:
            processed_frame = input_frame
        else:
            processed_frame = _placeholder_rgb(f"Processed: {status}")
    elif isinstance(processed_frame, np.ndarray) and processed_frame.size > 0 and int(processed_frame.max()) == 0:
        if current_input_frame is not None:
            processed_frame = input_frame
        else:
            processed_frame = _placeholder_rgb(f"Processed warming up: {status}")

    return input_frame, processed_frame, status


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
    default_catalog = "us_30a" if "us_30a" in repo_catalog_paths else (catalog_choices[0] if catalog_choices else None)
    initial_music_station_choices = load_music_stations_from_repo_files()
    default_music_choice = initial_music_station_choices[0] if initial_music_station_choices else default_music_station_name
    with gr.Blocks() as demo:
        mode = gr.Radio(
            choices=["webcam", "local", "stream"],
            value="stream",
            label="Mode",
        )

        with gr.Column(visible=False) as webcam_output_col:
            webcam_output = gr.Image(streaming=True, label="Processed stream")

        with gr.Column(visible=True) as source_output_col:
            source_output = gr.Image(label="Processed stream", value=_placeholder_rgb("Processed: initializing"))
            source_input = gr.Image(label="Input stream", value=_placeholder_rgb("Input: initializing"))
            source_status = gr.Textbox(label="Source status", value="initializing")

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
                            allow_custom_value=True,
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

            with gr.Row():
                musicgen_radio_url = gr.Textbox(
                    label="MusicGen Radio URL",
                    value=music_station_map.get(default_music_choice, default_music_radio_url),
                    lines=1,
                    scale=3,
                )
                musicgen_base_prompt = gr.Textbox(
                    label="Music Prompt",
                    value="experimental electronic sound art for an internet installation",
                    lines=1,
                    scale=3,
                )
            with gr.Row():
                music_station_choice = gr.Dropdown(
                    label="Music Station",
                    choices=initial_music_station_choices,
                    value=default_music_choice,
                    allow_custom_value=False,
                    filterable=True,
                    interactive=True,
                )
                load_music_catalog_btn = gr.Button("Load Music Stations")
                use_music_station_btn = gr.Button("Use Selected Station")
            with gr.Accordion("Paste Music Station M3U", open=False):
                music_station_m3u = gr.Textbox(
                    label="Music M3U Catalog",
                    lines=8,
                    value="#EXTM3U\n#EXTINF:-1,internet-radio.com station\nhttp://uk2.internet-radio.com:8024/",
                    placeholder="#EXTM3U\n#EXTINF:-1,Station Name\nhttps://example.com/radio",
                )
            with gr.Row():
                musicgen_sample_seconds = gr.Slider(
                    label="Sample Seconds",
                    minimum=6,
                    maximum=30,
                    value=12,
                    step=1,
                )
                musicgen_gen_seconds = gr.Slider(
                    label="Gen Seconds",
                    minimum=6,
                    maximum=30,
                    value=12,
                    step=1,
                )
                musicgen_stream_delay = gr.Slider(
                    label="Stream Delay Seconds",
                    minimum=2,
                    maximum=240,
                    value=180,
                    step=1,
                )
                musicgen_crossfade_seconds = gr.Slider(
                    label="Crossfade Seconds",
                    minimum=0,
                    maximum=4,
                    value=1.5,
                    step=0.1,
                )
            with gr.Row():
                musicgen_start_btn = gr.Button("Start Music Stream")
                musicgen_stop_btn = gr.Button("Stop Music Stream")
                fanout_music_btn = gr.Button("Start Twitch Fanout With Music")

            with gr.Row():
                fanout_enable_youtube = gr.Checkbox(value=False, label="Fanout YouTube")
                fanout_enable_twitch = gr.Checkbox(value=True, label="Fanout Twitch")
                fanout_enable_facebook = gr.Checkbox(value=False, label="Fanout Facebook")

            musicgen_status = gr.Textbox(label="MusicGen Status", value="idle", lines=3)
            with gr.Row():
                musicgen_audio_now = gr.Audio(
                    label="Now Playing Feed",
                    type="filepath",
                    interactive=False,
                )
                musicgen_audio_edit = gr.Audio(
                    label="Editing / Next Feed",
                    type="filepath",
                    interactive=False,
                )
            with gr.Row():
                musicgen_now_status = gr.Textbox(
                    label="Now Feed Status",
                    value="now feed: idle",
                    lines=1,
                )
                musicgen_edit_status = gr.Textbox(
                    label="Edit Feed Status",
                    value="edit feed: idle",
                    lines=1,
                )

            musicgen_audio_preview = gr.Audio(
                label="Latest Clip (Fallback)",
                type="filepath",
                interactive=False,
                visible=False,
            )

            ref_image_input = gr.Image(
                label="Reference Image",
                type="numpy",
                sources=["upload"],
                image_mode="RGB",
            )

        musicgen_timer = gr.Timer(value=2.0, active=True)

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

        musicgen_start_btn.click(
            start_musicgen_stream,
            inputs=[
                musicgen_radio_url,
                musicgen_base_prompt,
                musicgen_sample_seconds,
                musicgen_gen_seconds,
                musicgen_stream_delay,
                musicgen_crossfade_seconds,
            ],
            outputs=[musicgen_status],
        )

        musicgen_stop_btn.click(
            stop_musicgen_stream,
            outputs=[musicgen_status],
        )

        fanout_music_btn.click(
            start_fanout_with_music,
            inputs=[fanout_enable_youtube, fanout_enable_twitch, fanout_enable_facebook],
            outputs=[musicgen_status],
            queue=False,
            show_progress=False,
        )

        load_music_catalog_btn.click(
            load_music_station_catalog,
            inputs=[music_station_m3u],
            outputs=[music_station_choice, musicgen_radio_url],
        )

        music_station_m3u.change(
            load_music_station_catalog,
            inputs=[music_station_m3u],
            outputs=[music_station_choice, musicgen_radio_url],
        )

        use_music_station_btn.click(
            apply_music_station_choice,
            inputs=[music_station_choice],
            outputs=[musicgen_radio_url],
        )

        music_station_choice.change(
            apply_music_station_choice,
            inputs=[music_station_choice],
            outputs=[musicgen_radio_url],
        )

        musicgen_timer.tick(
            poll_musicgen_preview,
            outputs=[musicgen_audio_now, musicgen_audio_edit, musicgen_now_status, musicgen_edit_status],
        )

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
