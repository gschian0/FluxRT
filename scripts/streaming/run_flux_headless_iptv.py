#!/usr/bin/env python3
"""Headless FluxRT IPTV -> UDP streamer.

Reads IPTV frames, runs FluxRT StreamProcessor, and publishes processed
frames to UDP MPEG-TS for downstream fanout (Twitch/MediaMTX).
"""

import argparse
import contextlib
import json
import os
import signal
import subprocess
import tempfile
import time

import cv2
import numpy as np
from fluxrt import StreamProcessor


DEFAULT_PROMPT = (
    "glitch art digital distortion, datamoshing, RGB channel splitting, "
    "pixel sorting, corrupted video, cinematic high contrast"
)


def start_udp_writer(width: int, height: int, fps: int, udp_url: str) -> subprocess.Popen:
    gop = max(8, int(fps * 2))
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
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        "-x264-params",
        f"repeat-headers=1:keyint={gop}:min-keyint={gop}:scenecut=0",
        "-pix_fmt",
        "yuv420p",
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
    print(f"[flux-headless] UDP writer: {width}x{height}@{fps} -> {udp_url}", flush=True)
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=width * height * 3 * 2)


def resize_letterbox(frame, width: int, height: int):
    src_h, src_w = frame.shape[:2]
    if src_h <= 0 or src_w <= 0:
        return None
    scale = min(width / src_w, height / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = cv2.copyMakeBorder(
        resized,
        (height - new_h) // 2,
        height - new_h - (height - new_h) // 2,
        (width - new_w) // 2,
        width - new_w - (width - new_w) // 2,
        cv2.BORDER_CONSTANT,
        value=(0, 0, 0),
    )
    return canvas


def open_capture(url: str):
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        raise RuntimeError(f"failed to open IPTV stream: {url}")
    return cap


def placeholder_frame(width: int, height: int) -> np.ndarray:
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    cv2.rectangle(canvas, (0, 0), (width - 1, height - 1), (24, 24, 24), 1)
    cv2.putText(
        canvas,
        "FLUX LOADING",
        (12, max(28, height // 2)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (200, 200, 200),
        2,
        cv2.LINE_AA,
    )
    return canvas


def is_zero_frame(frame: np.ndarray | None) -> bool:
    if frame is None or not isinstance(frame, np.ndarray):
        return True
    if frame.size == 0:
        return True
    return int(frame.max()) == 0


def looks_like_passthrough(processed: np.ndarray | None, source: np.ndarray | None) -> bool:
    if processed is None or source is None:
        return True
    if not isinstance(processed, np.ndarray) or not isinstance(source, np.ndarray):
        return True
    if processed.shape != source.shape:
        return False
    # Flux output should differ materially from the prepared source frame.
    diff = cv2.absdiff(processed, source)
    mean_diff = float(diff.mean())
    max_diff = int(diff.max())
    return mean_diff < 8.0 and max_diff < 32


def main():
    parser = argparse.ArgumentParser(description="Headless FluxRT IPTV -> UDP streamer")
    parser.add_argument("--iptv-url", required=True)
    parser.add_argument(
        "--udp-url",
        default="udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1",
    )
    parser.add_argument("--config-path", default="configs/stream_demo_config.json")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--steps", type=int, default=2)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=180)
    parser.add_argument("--int8", action="store_true")
    parser.add_argument("--disable-spatial-cache", action="store_true", default=True)
    args = parser.parse_args()

    running = True

    def handle_signal(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    with open(args.config_path, "r", encoding="utf-8") as handle:
        effective_config = json.load(handle)

    if args.int8:
        effective_config["enable_int8_quantization"] = True
    effective_config["default_steps"] = int(args.steps)
    effective_config["default_prompt"] = args.prompt
    effective_config["logging"] = True
    if args.disable_spatial_cache:
        effective_config["enable_spatial_cache"] = False

    temp_config = tempfile.NamedTemporaryFile(
        mode="w", suffix="_flux_headless_config.json", delete=False, encoding="utf-8"
    )
    json.dump(effective_config, temp_config)
    temp_config.flush()
    temp_config.close()

    print(f"[flux-headless] config: {temp_config.name}", flush=True)
    print(f"[flux-headless] seed=auto, steps=runtime, fps={args.fps}", flush=True)
    print("[flux-headless] starting StreamProcessor (loads Flux models)...", flush=True)

    stream_processor = StreamProcessor(temp_config.name)

    resolution = stream_processor.get_resolution()
    width = int(resolution["width"])
    height = int(resolution["height"])
    print(f"[flux-headless] processor resolution: {width}x{height}", flush=True)

    input_tensor = stream_processor.get_input_tensor()
    output_tensor = stream_processor.get_output_tensor()

    stream_processor.start()
    stream_processor.set_prompt(args.prompt)
    stream_processor.set_steps(args.steps)

    writer = start_udp_writer(width, height, args.fps, args.udp_url)
    cap = open_capture(args.iptv_url)

    total_written = 0
    last_stats = time.time()
    start_time = time.time()
    debug_frames = 0
    last_good_processed = None
    warmup_frame = placeholder_frame(width, height)
    ready_processed_streak = 0
    first_ready_at = None

    try:
        while running:
            if debug_frames < 3:
                print(f"[flux-headless] reading frame {debug_frames + 1}", flush=True)
            ok, frame = cap.read()
            if not ok:
                print("[flux-headless] frame read failed; reconnecting IPTV in 1s", flush=True)
                cap.release()
                time.sleep(1)
                cap = open_capture(args.iptv_url)
                continue

            prepared = resize_letterbox(frame, width, height)
            if prepared is None:
                continue

            if debug_frames < 3:
                print(f"[flux-headless] frame {debug_frames + 1}: copy input", flush=True)
            input_tensor.copy_from(prepared)

            if debug_frames < 3:
                print(f"[flux-headless] frame {debug_frames + 1}: read processed output", flush=True)
            processed = output_tensor.to_numpy()
            candidate_is_ready = not is_zero_frame(processed) and not looks_like_passthrough(processed, prepared)
            if candidate_is_ready:
                ready_processed_streak += 1
            else:
                ready_processed_streak = 0

            warmup_elapsed = time.time() - start_time
            minimum_streak = 12
            minimum_warmup_seconds = 20.0

            if candidate_is_ready and first_ready_at is None:
                first_ready_at = now = time.time()
                print("[flux-headless] first processed frame candidate accepted", flush=True)

            if ready_processed_streak < minimum_streak or warmup_elapsed < minimum_warmup_seconds:
                processed = last_good_processed if last_good_processed is not None else warmup_frame
            else:
                last_good_processed = processed

            if last_good_processed is None and warmup_elapsed >= 45.0:
                raise RuntimeError(
                    "Flux never produced a validated processed frame; stopping instead of broadcasting fallback"
                )

            try:
                if debug_frames < 3:
                    print(f"[flux-headless] frame {debug_frames + 1}: write UDP", flush=True)
                writer.stdin.write(processed.tobytes())
                writer.stdin.flush()
                total_written += 1
                debug_frames += 1
            except (BrokenPipeError, OSError):
                print("[flux-headless] UDP writer broke; restarting", flush=True)
                try:
                    writer.terminate()
                    writer.wait(timeout=2)
                except Exception:
                    pass
                writer = start_udp_writer(args.width, args.height, args.fps, args.udp_url)

            now = time.time()
            if now - last_stats >= 10:
                print(
                    f"[flux-headless] written={total_written} ready_streak={ready_processed_streak} "
                    f"has_good={last_good_processed is not None}",
                    flush=True,
                )
                last_stats = now

    finally:
        print("[flux-headless] shutting down", flush=True)
        try:
            cap.release()
        except Exception:
            pass
        try:
            writer.stdin.close()
            writer.terminate()
            writer.wait(timeout=3)
        except Exception:
            pass
        try:
            stream_processor.stop()
        except Exception:
            pass
        with contextlib.suppress(Exception):
            os.unlink(temp_config.name)


if __name__ == "__main__":
    main()
