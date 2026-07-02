import argparse
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from gradio_client import Client


def start_writer(width: int, height: int, fps: int, udp_url: str) -> subprocess.Popen:
    gop = max(8, fps * 2)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        "-x264-params",
        f"repeat-headers=1:keyint={gop}:min-keyint={gop}:scenecut=0",
        "-mpegts_flags",
        "+resend_headers",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-flush_packets",
        "1",
        "-f",
        "mpegts",
        udp_url,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=width * height * 3 * 2)


def load_bgr(path: str, width: int, height: int) -> np.ndarray | None:
    if not path:
        return None
    image_path = Path(path)
    if not image_path.exists():
        return None
    frame = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if frame is None or frame.size == 0:
        return None
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge Gradio processed frames to UDP MPEG-TS")
    parser.add_argument("--gradio-url", default="http://127.0.0.1:7862")
    parser.add_argument("--udp-url", default="udp://127.0.0.1:5000?pkt_size=1316")
    parser.add_argument("--width", type=int, default=426)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--status-every", type=float, default=10.0)
    args = parser.parse_args()

    client = Client(args.gradio_url)
    writer = start_writer(args.width, args.height, args.fps, args.udp_url)
    frame_interval = 1.0 / max(1, args.fps)
    last_frame: np.ndarray | None = None
    last_frame_lock = threading.Lock()
    stop_event = threading.Event()
    last_status_at = 0.0
    frame_count = 0

    print(f"[gradio-bridge] processed frames -> {args.udp_url} at {args.width}x{args.height}@{args.fps}", flush=True)

    def poll_loop() -> None:
        nonlocal last_frame, last_status_at
        while not stop_event.is_set():
            try:
                input_path, processed_path, status = client.predict(api_name="/poll_video")
                frame = load_bgr(processed_path, args.width, args.height)
                if frame is None:
                    frame = load_bgr(input_path, args.width, args.height)
                if frame is not None:
                    with last_frame_lock:
                        last_frame = frame
                now = time.monotonic()
                if now - last_status_at >= args.status_every:
                    print(f"[gradio-bridge] {status}; frames={frame_count}", flush=True)
                    last_status_at = now
            except Exception as exc:
                print(f"[gradio-bridge] poll failed: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(0.5)

    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    try:
        while True:
            started_at = time.monotonic()
            with last_frame_lock:
                frame_to_write = None if last_frame is None else last_frame.copy()
            if frame_to_write is not None and writer.stdin is not None:
                try:
                    writer.stdin.write(frame_to_write.tobytes())
                    frame_count += 1
                except (BrokenPipeError, OSError):
                    writer = start_writer(args.width, args.height, args.fps, args.udp_url)

            elapsed = time.monotonic() - started_at
            time.sleep(max(0.0, frame_interval - elapsed))
    finally:
        stop_event.set()
        if writer.stdin is not None:
            writer.stdin.close()
        writer.terminate()


if __name__ == "__main__":
    main()