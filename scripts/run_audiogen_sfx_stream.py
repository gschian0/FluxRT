#!/usr/bin/env python3
"""Generate short AudioGen sound effects and stream them as UDP AAC."""

import argparse
import json
import queue
import random
import subprocess
import threading
import time
from pathlib import Path

import numpy as np


DEFAULT_PROMPTS = [
    "subtle analog tape whoosh, short broadcast transition, clean and quiet",
    "soft futuristic interface chirps, tiny electric sparkles, short and tasteful",
    "distant synthetic thunder swell, low cinematic rumble, restrained",
    "gentle glass shimmer and airy reverse cymbal, short transition sound",
    "soft room tone in a strange TV studio, faint machinery hum, not musical",
]


def _check_ffmpeg() -> None:
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True)
    except Exception as exc:
        raise RuntimeError("ffmpeg is required and not available in PATH") from exc


def _parse_prompts(args: argparse.Namespace) -> list[str]:
    prompts: list[str] = []
    for item in args.prompt or []:
        item = item.strip()
        if item:
            prompts.append(item)

    raw_prompts = (args.prompts or "").replace("|", "\n")
    for line in raw_prompts.splitlines():
        line = line.strip()
        if line:
            prompts.append(line)

    if args.prompt_file:
        path = Path(args.prompt_file)
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    prompts.append(line)

    return prompts or list(DEFAULT_PROMPTS)


def _start_audio_udp_encoder(sample_rate: int, audio_udp_url: str, volume: float):
    volume = max(0.0, min(2.0, float(volume)))
    audio_filter = f"volume={volume:.3f},highpass=f=35,lowpass=f=14000,alimiter=limit=0.90"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "f32le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-i",
        "-",
        "-af",
        audio_filter,
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-f",
        "mpegts",
        audio_udp_url,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def _playback_worker(
    audio_queue: "queue.Queue[tuple[str, np.ndarray] | None]",
    sample_rate: int,
    udp_proc,
) -> None:
    chunk_size = 2048
    silence_chunk = np.zeros((max(1, int(sample_rate * 0.25)),), dtype=np.float32)
    next_write_at = time.monotonic()

    def _stream_array(arr: np.ndarray) -> None:
        nonlocal next_write_at
        if udp_proc is None or udp_proc.stdin is None:
            return
        arr = np.asarray(arr, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return
        arr = np.nan_to_num(arr, copy=False)
        arr = np.clip(arr, -0.98, 0.98)
        idx = 0
        total = arr.shape[0]
        while idx < total:
            chunk = arr[idx : idx + chunk_size]
            udp_proc.stdin.write(chunk.astype(np.float32, copy=False).tobytes())
            idx += chunk_size
            next_write_at += chunk.shape[0] / sample_rate
            now = time.monotonic()
            if next_write_at - now > 0:
                time.sleep(next_write_at - now)
            elif now - next_write_at > 0.5:
                next_write_at = now

    while True:
        try:
            item = audio_queue.get_nowait()
        except queue.Empty:
            _stream_array(silence_chunk)
            continue

        if item is None:
            break

        prompt, clip = item
        print(f"[audiogen] playing: {prompt} ({clip.shape[0] / sample_rate:.1f}s)", flush=True)
        _stream_array(clip)

    if udp_proc is not None and udp_proc.stdin is not None:
        try:
            udp_proc.stdin.close()
        except Exception:
            pass
    if udp_proc is not None:
        try:
            udp_proc.terminate()
        except Exception:
            pass


def _apply_fade(arr: np.ndarray, sample_rate: int, fade_seconds: float) -> np.ndarray:
    fade_samples = int(max(0.0, fade_seconds) * sample_rate)
    if fade_samples <= 0 or arr.size <= fade_samples * 2:
        return arr
    out = arr.copy()
    fade_in = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
    fade_out = np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
    out[:fade_samples] *= fade_in
    out[-fade_samples:] *= fade_out
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="AudioGen streaming sound effects lane")
    parser.add_argument("--prompt", action="append", default=[], help="Sound effect prompt; repeatable")
    parser.add_argument("--prompts", default="", help="Newline or pipe separated sound effect prompts")
    parser.add_argument("--prompt-file", default="", help="Optional text file with one prompt per line")
    parser.add_argument("--output-dir", default="audiogen_sfx_output", help="Directory for generated WAV/metadata")
    parser.add_argument("--model", default="facebook/audiogen-medium", help="AudioCraft AudioGen model ID")
    parser.add_argument("--duration", type=float, default=3.0, help="Seconds per generated sound effect")
    parser.add_argument("--interval", type=float, default=35.0, help="Seconds between generation starts")
    parser.add_argument("--audio-udp-url", default="udp://127.0.0.1:5008?pkt_size=1316", help="UDP MPEG-TS output URL")
    parser.add_argument("--volume", type=float, default=0.35, help="Pre-bus SFX lane volume")
    parser.add_argument("--seed", type=int, default=4242, help="Random seed; negative uses time")
    parser.add_argument("--top-k", type=int, default=250, help="Sampling top-k")
    parser.add_argument("--top-p", type=float, default=0.0, help="Sampling top-p; 0 disables nucleus sampling")
    parser.add_argument("--temperature", type=float, default=1.0, help="Sampling temperature")
    parser.add_argument("--guidance-scale", type=float, default=3.0, help="Classifier-free guidance coefficient")
    parser.add_argument("--fade-seconds", type=float, default=0.08, help="Short fade applied to generated clips")
    parser.add_argument("--shuffle", action=argparse.BooleanOptionalAction, default=True, help="Shuffle prompt choice")
    parser.add_argument("--cpu", action="store_true", help="Force CPU inference")
    args = parser.parse_args()

    _check_ffmpeg()
    prompts = _parse_prompts(args)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        import soundfile as sf
        import torch
        from audiocraft.models import AudioGen
    except Exception as exc:
        print(
            "[audiogen] AudioCraft is not installed in this environment. "
            "Install AudioCraft in a compatible GPU env, then rerun this script. "
            f"Original import error: {exc}",
            flush=True,
        )
        raise SystemExit(2) from exc

    seed = int(args.seed)
    if seed < 0:
        seed = int(time.time() * 1000) % (2**31 - 1)
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    device = "cpu" if args.cpu else ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[audiogen] loading {args.model} on {device}", flush=True)
    model = AudioGen.get_pretrained(args.model, device=device)
    generation_kwargs = {
        "duration": max(0.5, float(args.duration)),
        "use_sampling": True,
        "top_k": max(0, int(args.top_k)),
        "temperature": max(0.1, float(args.temperature)),
        "cfg_coef": max(0.1, float(args.guidance_scale)),
    }
    if float(args.top_p) > 0:
        generation_kwargs["top_p"] = max(0.0, min(1.0, float(args.top_p)))
    try:
        model.set_generation_params(**generation_kwargs)
    except TypeError:
        generation_kwargs.pop("top_p", None)
        model.set_generation_params(**generation_kwargs)

    sample_rate = int(model.sample_rate)
    audio_queue: "queue.Queue[tuple[str, np.ndarray] | None]" = queue.Queue(maxsize=8)
    udp_proc = _start_audio_udp_encoder(sample_rate, args.audio_udp_url.strip(), args.volume)
    playback_thread = threading.Thread(
        target=_playback_worker,
        args=(audio_queue, sample_rate, udp_proc),
        daemon=True,
    )
    playback_thread.start()
    print(
        f"[audiogen] stream -> {args.audio_udp_url} prompts={len(prompts)} "
        f"duration={float(args.duration):.1f}s interval={float(args.interval):.1f}s",
        flush=True,
    )

    index = 0
    try:
        while True:
            prompt = random.choice(prompts) if args.shuffle else prompts[index % len(prompts)]
            print(f"[audiogen] generating: {prompt}", flush=True)
            started_at = time.monotonic()
            with torch.no_grad():
                wav = model.generate([prompt], progress=True)
            one_wav = wav[0].detach().float().cpu()
            if one_wav.ndim == 2:
                one_wav = one_wav.mean(dim=0)
            arr = np.asarray(one_wav.numpy(), dtype=np.float32).reshape(-1)
            arr = _apply_fade(np.clip(np.nan_to_num(arr), -0.98, 0.98), sample_rate, args.fade_seconds)

            wav_path = out_dir / f"audiogen_sfx_{index:06d}.wav"
            json_path = out_dir / f"audiogen_sfx_{index:06d}.json"
            sf.write(str(wav_path), arr, sample_rate)
            json_path.write_text(
                json.dumps(
                    {
                        "prompt": prompt,
                        "model": args.model,
                        "sample_rate": sample_rate,
                        "duration_seconds": arr.shape[0] / sample_rate,
                        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "generation_seconds": time.monotonic() - started_at,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (out_dir / "now_playing_path.txt").write_text(str(wav_path), encoding="utf-8")
            audio_queue.put((prompt, arr), timeout=2)
            index += 1

            remaining = max(0.0, float(args.interval) - (time.monotonic() - started_at))
            end_wait = time.monotonic() + remaining
            while time.monotonic() < end_wait:
                time.sleep(min(0.5, end_wait - time.monotonic()))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            audio_queue.put(None, timeout=1)
        except Exception:
            pass


if __name__ == "__main__":
    main()