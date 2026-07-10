#!/usr/bin/env python3
"""
Simple audio streamer: reads WAV files from a directory in order and
streams them as real-time audio to a UDP URL via ffmpeg.

This is a robust replacement for the complex playback worker in
run_musicgen_radio_plus_musicGEN.py. MusicGen generates WAV files,
this script plays them back in real-time.

Usage:
    python scripts/streaming/stream_audio_udp.py \
        --dir musicgen_output_plus_musicGEN \
        --udp-url "udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
        --sample-rate 32000
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf


def start_encoder(sample_rate: int, udp_url: str) -> subprocess.Popen:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-f", "f32le",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-i", "-",
        "-af", "highpass=f=35,lowpass=f=12000,alimiter=limit=0.85",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-f", "mpegts",
        udp_url,
    ]
    print(f"[audio-stream] starting ffmpeg encoder -> {udp_url}")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=sample_rate * 4)


def main():
    parser = argparse.ArgumentParser(description="Stream WAV files to UDP in real-time")
    parser.add_argument("--dir", required=True, help="Directory with WAV files")
    parser.add_argument("--udp-url", default="udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1")
    parser.add_argument("--sample-rate", type=int, default=32000)
    parser.add_argument("--chunk-size", type=int, default=2048)
    parser.add_argument("--loop", action="store_true", help="Loop back to start when done")
    args = parser.parse_args()

    out_dir = Path(args.dir)
    sample_rate = args.sample_rate
    chunk_size = args.chunk_size

    # Start ffmpeg encoder
    encoder = start_encoder(sample_rate, args.udp_url)

    played = set()
    next_write_at = time.monotonic()
    total_streamed = 0.0

    def write_array(arr: np.ndarray):
        nonlocal next_write_at, encoder
        if arr is None or arr.size == 0:
            return
        arr = np.nan_to_num(arr, copy=False)
        arr = np.clip(arr, -0.98, 0.98).astype(np.float32)
        idx = 0
        while idx < arr.shape[0]:
            chunk = arr[idx:idx + chunk_size]
            try:
                encoder.stdin.write(chunk.tobytes())
                encoder.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                print(f"[audio-stream] pipe error: {exc} — restarting encoder")
                try:
                    encoder.stdin.close()
                except Exception:
                    pass
                try:
                    encoder.kill()
                except Exception:
                    pass
                time.sleep(0.5)
                encoder = start_encoder(sample_rate, args.udp_url)
                try:
                    encoder.stdin.write(chunk.tobytes())
                except Exception:
                    pass
            idx += chunk_size
            next_write_at += chunk.shape[0] / sample_rate
            now = time.monotonic()
            if next_write_at - now > 0:
                time.sleep(next_write_at - now)
            elif now - next_write_at > 0.5:
                next_write_at = now

    print(f"[audio-stream] watching {out_dir} for WAV files...", flush=True)

    while True:
        # Find all WAV files, sorted by index
        wavs = sorted(out_dir.glob("musicgen_clip_*.wav"))
        if not wavs:
            print("[audio-stream] no WAV files yet, waiting...", flush=True)
            time.sleep(2)
            continue

        # Find files we haven't played yet
        new_files = [w for w in wavs if str(w) not in played]

        if not new_files:
            if args.loop:
                # Reset and play from the beginning
                played.clear()
                new_files = wavs
                print(f"[audio-stream] looping: replaying {len(new_files)} files", flush=True)
            else:
                # No new files, wait for more
                time.sleep(1)
                continue

        for wav_path in new_files:
            try:
                arr, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
            except Exception as exc:
                print(f"[audio-stream] error reading {wav_path}: {exc}", flush=True)
                continue

            if int(sr) != int(sample_rate):
                print(f"[audio-stream] skipping {wav_path}: sample rate {sr} != {sample_rate}", flush=True)
                played.add(str(wav_path))
                continue

            if arr.ndim == 2:
                arr = arr.mean(axis=1)
            arr = np.asarray(arr, dtype=np.float32).reshape(-1)

            clip_seconds = arr.shape[0] / sample_rate
            print(f"[audio-stream] playing {wav_path.name} ({clip_seconds:.1f}s)", flush=True)
            write_array(arr)
            played.add(str(wav_path))
            total_streamed += clip_seconds
            print(f"[audio-stream] total streamed: {total_streamed:.1f}s")

    # Cleanup
    try:
        encoder.stdin.close()
        encoder.terminate()
        encoder.wait(timeout=3)
    except Exception:
        pass


if __name__ == "__main__":
    main()
