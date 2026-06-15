#!/usr/bin/env python3
"""Generate installation music from internet radio context using MusicGen.

This script samples short chunks from an internet radio stream, extracts simple
energy/brightness/tempo descriptors, maps them into a safe artistic prompt, and
renders original music clips with MusicGen.
"""

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np


def _check_ffmpeg() -> None:
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True)
    except Exception as exc:
        raise RuntimeError("ffmpeg is required and not available in PATH") from exc


def _sample_radio_to_wav(radio_url: str, out_wav: str, seconds: int, sample_rate: int) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        radio_url,
        "-t",
        str(seconds),
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-vn",
        out_wav,
    ]
    subprocess.run(cmd, check=True)


def _extract_descriptors(wav_path: str) -> dict:
    import librosa

    y, sr = librosa.load(wav_path, sr=None, mono=True)
    if y.size == 0:
        return {"tempo": 100.0, "rms": 0.01, "centroid": 1200.0}

    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    rms = float(np.sqrt(np.mean(np.square(y))))
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))

    if np.isnan(tempo):
        tempo = 100.0

    return {"tempo": float(tempo), "rms": rms, "centroid": centroid}


def _build_prompt(descriptors: dict, base_prompt: str) -> str:
    tempo = int(max(60, min(160, descriptors["tempo"])))
    intensity = "calm" if descriptors["rms"] < 0.03 else "energetic"
    tone = "warm" if descriptors["centroid"] < 1800 else "bright"

    return (
        f"{base_prompt}, {intensity} dynamics, {tone} timbre, "
        f"around {tempo} bpm, evolving instrumental texture, no vocals"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="MusicGen radio-driven generator")
    parser.add_argument("--radio-url", required=True, help="Internet radio stream URL")
    parser.add_argument("--output-dir", default="musicgen_output_plus_musicGEN", help="Output directory")
    parser.add_argument("--model", default="facebook/musicgen-small", help="HuggingFace model ID")
    parser.add_argument("--sample-seconds", type=int, default=12, help="Seconds to sample from radio")
    parser.add_argument("--gen-seconds", type=int, default=12, help="Seconds to generate per clip")
    parser.add_argument("--sample-rate", type=int, default=32000, help="Audio sample rate")
    parser.add_argument("--pause-seconds", type=float, default=1.0, help="Pause between loops")
    parser.add_argument(
        "--base-prompt",
        default="experimental electronic sound art for an internet installation",
        help="Prompt seed for generation",
    )
    args = parser.parse_args()

    _check_ffmpeg()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Lazy import so users can inspect script without installed ML deps.
    import soundfile as sf
    import torch
    from audiocraft.models import MusicGen

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = MusicGen.get_pretrained(args.model, device=device)
    model.set_generation_params(duration=args.gen_seconds, top_k=250, temperature=1.0)

    print(f"[musicgen] device={device} model={args.model} out={out_dir}")
    index = 0

    while True:
        with tempfile.TemporaryDirectory(prefix="musicgen_radio_") as tmpdir:
            radio_wav = os.path.join(tmpdir, "radio.wav")
            _sample_radio_to_wav(args.radio_url, radio_wav, args.sample_seconds, args.sample_rate)
            descriptors = _extract_descriptors(radio_wav)
            prompt = _build_prompt(descriptors, args.base_prompt)

            generated = model.generate([prompt], progress=False)
            audio = generated[0].cpu().numpy().T

            clip_name = f"musicgen_clip_{index:06d}.wav"
            meta_name = f"musicgen_clip_{index:06d}.json"
            clip_path = out_dir / clip_name
            meta_path = out_dir / meta_name

            sf.write(str(clip_path), audio, model.sample_rate)
            meta = {
                "index": index,
                "prompt": prompt,
                "descriptors": descriptors,
                "model": args.model,
                "sample_seconds": args.sample_seconds,
                "gen_seconds": args.gen_seconds,
            }
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

            print(f"[musicgen] wrote {clip_path}")

        index += 1
        time.sleep(max(0.0, args.pause_seconds))


if __name__ == "__main__":
    main()
