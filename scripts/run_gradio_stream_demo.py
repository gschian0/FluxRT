import argparse
import threading
import time

import cv2
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
    sp.set_reference_image(image)


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
                input_frame, processed = process_frame(frame)
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
                input_frame, processed = process_frame(frame)
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

    _, processed = process_frame(to_bgr(frame))
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

    get_processor()
    use_reference_image = stream_processor.config.get("use_reference_image", False)

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
                    stream_url = gr.Textbox(
                        label="Stream URL",
                        value=default_stream_url,
                        lines=1,
                    )
                    with gr.Row():
                        stream_start_btn = gr.Button("Start Stream")
                        stream_stop_btn = gr.Button("Stop")

                source_input = gr.Image(label="Input stream")
                source_status = gr.Textbox(label="Source status", value="idle")

            prompt = gr.Textbox(value=default_prompt, label="Prompt", lines=3)

            if use_reference_image:
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

        if use_reference_image:
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
