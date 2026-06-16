#!/usr/bin/env python3
"""Generate installation music from internet radio context using MusicGen.

This script samples short chunks from an internet radio stream, extracts simple
energy/brightness/tempo descriptors, maps them into a safe artistic prompt, and
renders original music clips with MusicGen.
"""

import argparse
import json
import os
import queue
import random
import subprocess
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlparse
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
        "-fflags",
        "+discardcorrupt",
        "-err_detect",
        "ignore_err",
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


def _resolve_playlist_stream_url(radio_url: str) -> str:
    lowered = radio_url.lower()
    parsed = urlparse(radio_url)
    path_lower = (parsed.path or "").lower()
    looks_like_playlist = (
        "playlistgenerator" in lowered
        or lowered.endswith(".m3u")
        or lowered.endswith(".pls")
        or path_lower.endswith(".m3u")
        or path_lower.endswith(".pls")
        or ".pls?" in lowered
        or ".m3u?" in lowered
        or "t=.m3u" in lowered
    )
    if not looks_like_playlist:
        return radio_url

    try:
        with urllib.request.urlopen(radio_url, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return radio_url

    # Parse PLS first (File1=...)
    for line in content.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.lower().startswith("file") and "=" in s:
            candidate = s.split("=", 1)[1].strip()
            if candidate.startswith("http://") or candidate.startswith("https://"):
                return candidate

    # Parse M3U next (first non-comment URL)
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("http://") or s.startswith("https://"):
            return s

    return radio_url


def _extract_descriptors(wav_path: str) -> dict:
    import librosa

    y, sr = librosa.load(wav_path, sr=None, mono=True)
    if y.size == 0:
        return {"tempo": 100.0, "rms": 0.01, "centroid": 1200.0}

    tempo_raw, _ = librosa.beat.beat_track(y=y, sr=sr)
    tempo = float(np.asarray(tempo_raw).reshape(-1)[0])
    rms = float(np.sqrt(np.mean(np.square(y))))
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))

    if np.isnan(tempo):
        tempo = 100.0

    return {"tempo": tempo, "rms": rms, "centroid": centroid}


def _build_prompt(descriptors: dict, base_prompt: str) -> str:
    tempo = int(max(60, min(160, descriptors["tempo"])))
    intensity = "calm" if descriptors["rms"] < 0.03 else "energetic"
    tone = "warm" if descriptors["centroid"] < 1800 else "bright"

    return (
        f"{base_prompt}, {intensity} dynamics, {tone} timbre, "
        f"around {tempo} bpm, evolving instrumental texture, no vocals"
    )


def _start_audio_udp_encoder(sample_rate: int, audio_udp_url: str):
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
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-f",
        "mpegts",
        audio_udp_url,
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def _write_marker(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def _load_bootstrap_clips(out_dir: Path, max_clips: int, sample_rate: int, sf_module) -> list[np.ndarray]:
    if max_clips <= 0:
        return []

    candidates = sorted(out_dir.glob("musicgen_clip_*.wav"))[-max_clips:]
    clips: list[np.ndarray] = []
    for wav_path in candidates:
        try:
            arr, sr = sf_module.read(str(wav_path), dtype="float32", always_2d=False)
        except Exception:
            continue
        if int(sr) != int(sample_rate):
            continue
        if isinstance(arr, np.ndarray) and arr.ndim == 2:
            arr = arr.mean(axis=1)
        arr = np.asarray(arr, dtype=np.float32).reshape(-1)
        if arr.size < int(0.5 * sample_rate):
            continue
        clips.append(np.clip(arr, -0.98, 0.98))
    return clips


def _playback_worker(
    audio_queue: "queue.Queue[tuple[int, np.ndarray] | None]",
    sample_rate: int,
    delay_seconds: float,
    crossfade_seconds: float,
    udp_proc,
    now_marker_path: Path,
    out_dir: Path,
    bootstrap_clips: list[np.ndarray] | None = None,
):
    started = False
    buffered_seconds = 0.0
    buffer: list[tuple[int, np.ndarray]] = []
    chunk_size = 2048
    fade_samples = max(0, int(crossfade_seconds * sample_rate))
    pending_tail: np.ndarray | None = None
    idle_silence_chunk = np.zeros((int(sample_rate * 0.25),), dtype=np.float32)
    loop_source = np.zeros((0,), dtype=np.float32)
    loop_cursor = 0
    underrun_announced = False

    if bootstrap_clips:
        for boot_clip in bootstrap_clips:
            buffer.append((-1, boot_clip))
            buffered_seconds += boot_clip.shape[0] / sample_rate
        print(
            f"[musicgen] loaded {len(bootstrap_clips)} bootstrap clips ({buffered_seconds:.1f}s)"
        )

    def _stream_array(arr: np.ndarray):
        if arr is None or arr.size == 0:
            return
        if udp_proc is None or udp_proc.stdin is None:
            return
        arr = np.nan_to_num(arr, copy=False)
        arr = np.clip(arr, -0.98, 0.98)
        total = arr.shape[0]
        idx = 0
        while idx < total:
            chunk = arr[idx : idx + chunk_size]
            udp_proc.stdin.write(chunk.astype(np.float32, copy=False).tobytes())
            idx += chunk_size
            time.sleep(chunk.shape[0] / sample_rate)

    def _stream_silence(seconds: float):
        if seconds <= 0:
            return
        count = max(1, int(seconds * sample_rate))
        silence = np.zeros((count,), dtype=np.float32)
        _stream_array(silence)

    def _remember_for_loop(arr: np.ndarray):
        nonlocal loop_source, loop_cursor
        if arr is None or arr.size == 0:
            return
        arr = np.asarray(arr, dtype=np.float32).reshape(-1)
        # Keep a rolling 3-minute memory window for seamless fallback looping.
        if loop_source.size == 0:
            loop_source = arr.copy()
        else:
            loop_source = np.concatenate([loop_source, arr])
        max_len = int(sample_rate * 180)
        if loop_source.size > max_len:
            loop_source = loop_source[-max_len:]
        if loop_cursor >= loop_source.size:
            loop_cursor = 0

    def _next_loop_chunk(n: int) -> np.ndarray:
        nonlocal loop_cursor
        if loop_source.size == 0 or n <= 0:
            return np.zeros((n,), dtype=np.float32)
        out = np.empty((n,), dtype=np.float32)
        remaining = n
        pos = 0
        while remaining > 0:
            tail = loop_source[loop_cursor:]
            take = min(remaining, tail.size)
            out[pos : pos + take] = tail[:take]
            pos += take
            remaining -= take
            loop_cursor = (loop_cursor + take) % loop_source.size
        return out

    def _stream_clip(index: int, clip: np.ndarray):
        nonlocal pending_tail
        if index >= 0:
            _write_marker(now_marker_path, str(out_dir / f"musicgen_clip_{index:06d}.wav"))
        _remember_for_loop(clip)

        if fade_samples <= 0 or clip.shape[0] <= fade_samples * 2:
            if pending_tail is not None:
                _stream_array(pending_tail)
                pending_tail = None
            _stream_array(clip)
            return

        head = clip[:fade_samples]
        middle = clip[fade_samples:-fade_samples]
        tail = clip[-fade_samples:]

        if pending_tail is None:
            _stream_array(clip[:-fade_samples])
            pending_tail = tail
            return

        ramp = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
        mixed = pending_tail * (1.0 - ramp) + head * ramp
        _stream_array(mixed)
        _stream_array(middle)
        pending_tail = tail

    # Pre-seed fallback loop memory from bootstrap content so startup buffering
    # can still output continuous non-silent program audio.
    if bootstrap_clips:
        for boot_clip in bootstrap_clips:
            _remember_for_loop(boot_clip)

    if buffered_seconds >= delay_seconds and len(buffer) > 0:
        print(f"[musicgen] bootstrap buffer ready: {buffered_seconds:.1f}s")
        for q_index, queued_clip in buffer:
            _stream_clip(q_index, queued_clip)
        buffer = []
        started = True

    while True:
        try:
            item = audio_queue.get(timeout=0.25)
        except queue.Empty:
            # Keep encoder timing continuous to avoid audible crackle on underrun.
            if started and pending_tail is not None and pending_tail.size > 0:
                _stream_array(pending_tail)
                _remember_for_loop(pending_tail)
                pending_tail = None
            if started and loop_source.size > 0:
                if not underrun_announced:
                    print("[musicgen] underrun: looping recent buffer until next clip arrives")
                    underrun_announced = True
                _stream_array(_next_loop_chunk(idle_silence_chunk.size))
            else:
                _stream_array(idle_silence_chunk)
            continue

        underrun_announced = False

        if item is None:
            break
        index, clip = item
        clip_seconds = clip.shape[0] / sample_rate

        if not started:
            buffer.append((index, clip))
            buffered_seconds += clip_seconds
            # Keep a valid track alive while building buffer: prefer recent looped
            # program audio over silence when available.
            if loop_source.size > 0:
                _stream_array(_next_loop_chunk(int(clip_seconds * sample_rate)))
            else:
                _stream_silence(clip_seconds)
            print(
                f"[musicgen] buffering backing track {buffered_seconds:.1f}/{delay_seconds:.1f}s"
            )
            if buffered_seconds < delay_seconds:
                continue
            print(f"[musicgen] backing track buffer ready: {buffered_seconds:.1f}s")
            for q_index, queued_clip in buffer:
                _stream_clip(q_index, queued_clip)
            buffer = []
            started = True
            continue

        _stream_clip(index, clip)

    if pending_tail is not None:
        _stream_array(pending_tail)

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


def main() -> None:
    parser = argparse.ArgumentParser(description="MusicGen radio-driven generator")
    parser.add_argument("--radio-url", required=True, help="Internet radio stream URL")
    parser.add_argument("--output-dir", default="musicgen_output_plus_musicGEN", help="Output directory")
    parser.add_argument("--model", default="facebook/musicgen-small", help="HuggingFace model ID")
    parser.add_argument("--sample-seconds", type=int, default=8, help="Seconds to sample from radio")
    parser.add_argument("--gen-seconds", type=int, default=8, help="Seconds to generate per clip")
    parser.add_argument(
        "--parallel-clips",
        type=int,
        default=2,
        help="Number of clips to generate in parallel from one sampled radio context",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=-1,
        help="Base random seed (negative value uses time-based seed)",
    )
    parser.add_argument(
        "--bootstrap-clips",
        type=int,
        default=10,
        help="Number of previous generated clips to preload as intro while new clips buffer",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=250,
        help="Top-k sampling cutoff (Audiocraft demo-style control)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="Sampling temperature (Audiocraft demo-style control)",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=0.95,
        help="Top-p (nucleus) sampling cutoff (Audiocraft demo-style control)",
    )
    parser.add_argument(
        "--guidance-scale",
        type=float,
        default=3.0,
        help="Classifier-free guidance scale for MusicGen generation",
    )
    parser.add_argument("--sample-rate", type=int, default=32000, help="Audio sample rate")
    parser.add_argument("--pause-seconds", type=float, default=0.0, help="Pause between loops")
    parser.add_argument(
        "--stream-delay-seconds",
        type=float,
        default=8.0,
        help="Buffered delay before streaming generated audio",
    )
    parser.add_argument(
        "--audio-udp-url",
        default="",
        help="If set, stream generated audio to this UDP URL as MPEG-TS AAC",
    )
    parser.add_argument(
        "--crossfade-seconds",
        type=float,
        default=1.0,
        help="Crossfade seconds between generated clips for seamless splicing",
    )
    parser.add_argument(
        "--base-prompt",
        default="experimental electronic sound art for an internet installation",
        help="Prompt seed for generation",
    )
    args = parser.parse_args()

    _check_ffmpeg()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    now_marker_path = out_dir / "now_playing_path.txt"
    edit_marker_path = out_dir / "editing_path.txt"

    # Lazy import so users can inspect script without installed ML deps.
    import soundfile as sf
    import torch
    from transformers import AutoProcessor, MusicgenForConditionalGeneration

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    processor = AutoProcessor.from_pretrained(args.model)
    model = MusicgenForConditionalGeneration.from_pretrained(args.model, torch_dtype=dtype)
    model = model.to(device)
    model.eval()

    # MusicGen uses approximately 50 audio tokens per second.
    max_new_tokens = max(64, int(args.gen_seconds * 50))
    top_k = max(0, int(args.top_k))
    temperature = max(0.1, float(args.temperature))
    top_p = max(0.05, min(1.0, float(args.top_p)))
    guidance_scale = max(1.0, float(args.guidance_scale))
    parallel_clips = max(1, int(args.parallel_clips))
    seed_base = int(args.seed)
    if seed_base < 0:
        seed_base = int(time.time() * 1000) % (2**31 - 1)
    random.seed(seed_base)

    sample_rate = int(model.config.audio_encoder.sampling_rate)
    audio_queue: "queue.Queue[tuple[int, np.ndarray] | None]" | None = None
    playback_thread: threading.Thread | None = None
    udp_proc = None

    if args.audio_udp_url.strip():
        audio_queue = queue.Queue(maxsize=32)
        udp_proc = _start_audio_udp_encoder(sample_rate, args.audio_udp_url.strip())
        bootstrap_clips = _load_bootstrap_clips(
            out_dir=out_dir,
            max_clips=max(0, int(args.bootstrap_clips)),
            sample_rate=sample_rate,
            sf_module=sf,
        )
        playback_thread = threading.Thread(
            target=_playback_worker,
            args=(
                audio_queue,
                sample_rate,
                max(0.0, args.stream_delay_seconds),
                max(0.0, args.crossfade_seconds),
                udp_proc,
                now_marker_path,
                out_dir,
                bootstrap_clips,
            ),
            daemon=True,
        )
        playback_thread.start()
        print(
            f"[musicgen] audio stream -> {args.audio_udp_url} "
            f"(delay={args.stream_delay_seconds}s, bootstrap_clips={len(bootstrap_clips)})"
        )

    print(
        f"[musicgen] device={device} model={args.model} out={out_dir} "
        f"gen(top_k={top_k}, top_p={top_p}, temperature={temperature}, guidance_scale={guidance_scale}, "
        f"parallel_clips={parallel_clips}, seed={seed_base})"
    )
    resolved_radio_url = _resolve_playlist_stream_url(args.radio_url)
    if resolved_radio_url != args.radio_url:
        print(f"[musicgen] resolved playlist URL -> {resolved_radio_url}")
    index = 0

    while True:
        with tempfile.TemporaryDirectory(prefix="musicgen_radio_") as tmpdir:
            radio_wav = os.path.join(tmpdir, "radio.wav")
            _sample_radio_to_wav(resolved_radio_url, radio_wav, args.sample_seconds, args.sample_rate)
            descriptors = _extract_descriptors(radio_wav)
            prompt = _build_prompt(descriptors, args.base_prompt)

            batch_prompts = [prompt for _ in range(parallel_clips)]
            inputs = processor(text=batch_prompts, padding=True, return_tensors="pt")
            inputs = {k: v.to(device) for k, v in inputs.items()}
            loop_seed = (seed_base + index) % (2**31 - 1)
            with torch.no_grad():
                if device == "cuda":
                    with torch.autocast(device_type="cuda", dtype=torch.float16):
                        generated = model.generate(
                            **inputs,
                            do_sample=True,
                            top_k=top_k,
                            top_p=top_p,
                            temperature=temperature,
                            guidance_scale=guidance_scale,
                            max_new_tokens=max_new_tokens,
                            
                        )
                else:
                    generated = model.generate(
                        **inputs,
                        do_sample=True,
                        top_k=top_k,
                        top_p=top_p,
                        temperature=temperature,
                        guidance_scale=guidance_scale,
                        max_new_tokens=max_new_tokens,
                        
                    )

            batch_size = int(generated.shape[0]) if hasattr(generated, "shape") else parallel_clips
            for batch_pos in range(batch_size):
                audio = generated[batch_pos].detach().cpu().numpy()
                # transformers MusicGen returns (channels, samples); soundfile expects (samples, channels).
                if audio.ndim == 2 and audio.shape[0] <= 8:
                    audio = audio.T
                if audio.ndim == 1:
                    audio = np.expand_dims(audio, axis=1)
                audio = audio.astype(np.float32, copy=False)

                clip_name = f"musicgen_clip_{index:06d}.wav"
                meta_name = f"musicgen_clip_{index:06d}.json"
                clip_path = out_dir / clip_name
                meta_path = out_dir / meta_name

                sf.write(str(clip_path), audio, sample_rate)
                _write_marker(edit_marker_path, str(clip_path))
                if index == 0:
                    _write_marker(now_marker_path, str(clip_path))

                mono_audio = audio.mean(axis=1) if audio.ndim == 2 else audio.squeeze()
                if mono_audio.ndim == 0:
                    mono_audio = np.array([float(mono_audio)], dtype=np.float32)
                mono_audio = mono_audio.astype(np.float32, copy=False)
                if audio_queue is not None:
                    audio_queue.put((index, np.clip(mono_audio, -0.98, 0.98)))
                meta = {
                    "index": index,
                    "batch_pos": batch_pos,
                    "parallel_clips": parallel_clips,
                    "seed": loop_seed,
                    "prompt": prompt,
                    "descriptors": descriptors,
                    "model": args.model,
                    "sample_seconds": args.sample_seconds,
                    "gen_seconds": args.gen_seconds,
                    "top_k": top_k,
                    "top_p": top_p,
                    "temperature": temperature,
                    "guidance_scale": guidance_scale,
                }
                meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

                print(f"[musicgen] wrote {clip_path}")
                index += 1

        time.sleep(max(0.0, args.pause_seconds))

    if audio_queue is not None:
        audio_queue.put(None)
    if playback_thread is not None:
        playback_thread.join(timeout=2)


if __name__ == "__main__":
    main()
