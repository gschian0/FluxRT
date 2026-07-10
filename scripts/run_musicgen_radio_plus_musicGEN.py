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
    """Extract tempo, RMS energy, and spectral centroid from a WAV file using librosa."""
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


# ---------------------------------------------------------------------------
# Creative prompt system — random starting points with smooth continuation.
# Each session picks a random creative seed prompt, then all subsequent clips
# use continuation conditioning (feeding previous audio back in) so the music
# evolves smoothly and crossfades cleanly without abrupt style jumps.
# ---------------------------------------------------------------------------

# Pool of creative starting prompts — randomly selected per session.
# These give the music a distinctive, memorable character from clip #0.
_CREATIVE_SEED_PROMPTS = [
    "glitch jungle reggae, dub bass wobbles with chopped breakbeats, "
    "warm sub frequencies, trippy echo delays, smooth instrumental groove",

    "breakbeat nat king cole alien groove, vintage jazz piano chords "
    "meets futuristic drum breaks, silky smooth bassline, spacey synth pads",

    "deep sea dub techno, echoing chord stabs submerged in reverb, "
    "steady four-on-the-floor kick, warm analog bass, hypnotic underwater vibe",

    "cosmic country surf rock, twangy guitar licks over reggae bassline, "
    "spacey spring reverb, laid-back drum shuffle, instrumental storytelling",

    "afro-futurist jazz fusion, talking bass guitar, polyrhythmic drums, "
    "floating Rhodes piano, warm brass stabs, spiritual cosmic groove",

    "lo-fi hip hop bossa nova, crackly vinyl piano samples, nylon guitar, "
    "soft boom-bap drums, warm bass, lazy afternoon cafe atmosphere",

    "psychedelic dub funk, wah-wah guitar riffs, fat bassline grooves, "
    "trippy tape echo delays, organ stabs, steady head-nodding rhythm",

    "ambient space reggae, floating dub chords, deep bass pulses, "
    "sparse percussion, cosmic synth textures, weightless groove",

    "neo-soul broken beat, lush jazz chords, syncopated drum patterns, "
    "warm bass guitar, Rhodes piano, smooth instrumental flow",

    "krautrock dub voyage, motorik drum pulse, hypnotic bassline, "
    "swirling analog synths, guitar textures, steady cosmic journey",
]

# Consistency tail — appended to every prompt to keep clips coherent.
_CONSISTENCY_TAIL = (
    "cohesive songform, same key and tempo throughout, "
    "smooth instrumental mix, no vocals, no abrupt style change, "
    "seamless transitions, consistent mood and instrumentation"
)

# Default BPM for generation — evenly divisible, easy to work with.
_DEFAULT_BPM = 120

# How many clips before rotating to a new seed prompt (higher = more consistent).
_SEED_ROTATION_INTERVAL = 8


def _pick_random_seed_prompt(base_prompt: str, bpm: int = _DEFAULT_BPM) -> str:
    """Pick a random creative seed prompt, or use the provided base_prompt.

    If base_prompt is non-empty and doesn't look like the default, use it.
    Otherwise pick a random creative prompt from the pool.
    BPM is always included so generation length aligns with tempo.
    """
    bpm_str = f"{bpm} BPM"
    if base_prompt.strip() and base_prompt.strip() != "experimental electronic sound art for an internet installation":
        return f"{base_prompt.strip()}, {bpm_str}, {_CONSISTENCY_TAIL}"
    import random as _r
    chosen = _r.choice(_CREATIVE_SEED_PROMPTS)
    return f"{chosen}, {bpm_str}, {_CONSISTENCY_TAIL}"


def _build_layered_prompt(base_prompt: str, cycle_index: int, bpm: int = _DEFAULT_BPM) -> str:
    """Build prompt for a clip.

    Rotates to a new seed prompt every _SEED_ROTATION_INTERVAL clips so the
    music evolves faster while continuation conditioning keeps transitions smooth.
    BPM is always included so generation length aligns with tempo.
    """
    # Every N clips, pick a fresh seed prompt for faster evolution
    rotation = cycle_index // _SEED_ROTATION_INTERVAL
    if rotation > 0:
        import random as _r
        # Use deterministic seed based on rotation index for reproducibility
        _r.seed(rotation * 1000)
        chosen = _r.choice(_CREATIVE_SEED_PROMPTS)
        bpm_str = f"{bpm} BPM"
        return f"{chosen}, {bpm_str}, {_CONSISTENCY_TAIL}"
    return base_prompt


def _build_prompt(descriptors: dict, base_prompt: str) -> str:
    """Legacy compatibility — delegates to layered prompt builder."""
    return _build_layered_prompt(base_prompt, 0)


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
        "-af",
        "highpass=f=35,lowpass=f=12000,alimiter=limit=0.85",
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


def _load_conditioning_audio(wav_path: str, sample_rate: int, seconds: float, sf_module) -> np.ndarray | None:
    try:
        arr, sr = sf_module.read(wav_path, dtype="float32", always_2d=False)
    except Exception:
        return None
    if int(sr) != int(sample_rate):
        return None
    if isinstance(arr, np.ndarray) and arr.ndim == 2:
        arr = arr.mean(axis=1)
    arr = np.asarray(arr, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return None
    max_samples = max(1, int(seconds * sample_rate))
    return np.clip(arr[-max_samples:], -0.98, 0.98)


def _playback_worker(
    audio_queue: "queue.Queue[tuple[int, np.ndarray] | None]",
    sample_rate: int,
    delay_seconds: float,
    crossfade_seconds: float,
    udp_proc,
    now_marker_path: Path,
    out_dir: Path,
    bootstrap_clips: list[np.ndarray] | None = None,
    audio_udp_url: str = "",
):
    # Store the UDP URL for ffmpeg restart on pipe break
    _udp_url = audio_udp_url
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
    next_write_at = time.monotonic()

    if bootstrap_clips:
        for boot_clip in bootstrap_clips:
            buffer.append((-1, boot_clip))
            buffered_seconds += boot_clip.shape[0] / sample_rate
        print(
            f"[musicgen] loaded {len(bootstrap_clips)} bootstrap clips ({buffered_seconds:.1f}s)"
        )

    def _stream_array(arr: np.ndarray):
        nonlocal next_write_at, udp_proc
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
            try:
                udp_proc.stdin.write(chunk.astype(np.float32, copy=False).tobytes())
            except (BrokenPipeError, OSError, ValueError) as exc:
                print(f"[musicgen] PIPE ERROR writing to ffmpeg: {type(exc).__name__}: {exc} — restarting ffmpeg")
                try:
                    udp_proc.stdin.close()
                except Exception:
                    pass
                try:
                    udp_proc.kill()
                except Exception:
                    pass
                # Restart the ffmpeg encoder
                udp_proc = _start_audio_udp_encoder(sample_rate, _udp_url)
                print("[musicgen] ffmpeg encoder restarted — resuming audio stream")
                # Try writing the chunk again
                try:
                    udp_proc.stdin.write(chunk.astype(np.float32, copy=False).tobytes())
                except Exception:
                    pass
            idx += chunk_size
            next_write_at += chunk.shape[0] / sample_rate
            now = time.monotonic()
            if next_write_at - now > 0:
                time.sleep(next_write_at - now)
            elif now - next_write_at > 0.5:
                next_write_at = now

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
        # If we have enough audio, apply a short crossfade at the loop boundary
        # to avoid clicks/pops when wrapping around.
        out = np.empty((n,), dtype=np.float32)
        remaining = n
        pos = 0
        fade_len = min(int(sample_rate * 0.05), loop_source.size // 4)  # 50ms crossfade
        while remaining > 0:
            tail = loop_source[loop_cursor:]
            take = min(remaining, tail.size)
            out[pos : pos + take] = tail[:take]
            pos += take
            remaining -= take
            old_cursor = loop_cursor
            loop_cursor = (loop_cursor + take) % loop_source.size
            # Apply crossfade when wrapping around the loop boundary
            if loop_cursor < old_cursor and fade_len > 0 and remaining == 0:
                # We wrapped — crossfade the end of out with the start of loop_source
                fade_region = min(fade_len, out.shape[0])
                fade_in = loop_source[:fade_region]
                phase = np.linspace(0.0, 1.0, fade_region, dtype=np.float32)
                out[-fade_region:] = out[-fade_region:] * (1.0 - phase) + fade_in * phase
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

        phase = np.linspace(0.0, np.pi / 2, fade_samples, endpoint=False, dtype=np.float32)
        mixed = pending_tail * np.cos(phase) + head * np.sin(phase)
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
        streamed_bootstrap_seconds = 0.0
        for q_index, queued_clip in buffer:
            if streamed_bootstrap_seconds >= delay_seconds:
                break
            _stream_clip(q_index, queued_clip)
            streamed_bootstrap_seconds += queued_clip.shape[0] / sample_rate
        print(f"[musicgen] streamed {streamed_bootstrap_seconds:.1f}s bootstrap intro before live queue")
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
    parser.add_argument("--radio-url", default="", help="Internet radio stream URL (optional, skipped if empty)")
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
        default=20,
        help="Number of previous generated clips to preload as intro while new clips buffer",
    )
    parser.add_argument(
        "--pre-generate",
        type=int,
        default=0,
        help="Pre-generate N clips before starting playback to build a large buffer (prevents underruns)",
    )
    parser.add_argument(
        "--compile",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Use torch.compile() on the model for faster generation (adds startup overhead)",
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
    parser.add_argument(
        "--drunk-walk",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Slightly random-walk generation parameters between batches",
    )
    parser.add_argument(
        "--drunk-walk-strength",
        type=float,
        default=0.0,
        help="Scale for drunk-walk parameter drift; 1.0 is subtle",
    )
    parser.add_argument("--sample-rate", type=int, default=32000, help="Audio sample rate")
    parser.add_argument("--pause-seconds", type=float, default=0.0, help="Pause between loops")
    parser.add_argument(
        "--stream-delay-seconds",
        type=float,
        default=30.0,
        help="Buffered delay before streaming generated audio (should be >= 2x gen_seconds)",
    )
    parser.add_argument(
        "--audio-udp-url",
        default="",
        help="If set, stream generated audio to this UDP URL as MPEG-TS AAC",
    )
    parser.add_argument(
        "--crossfade-seconds",
        type=float,
        default=2.0,
        help="Crossfade seconds between generated clips for seamless splicing",
    )
    parser.add_argument(
        "--base-prompt",
        default="experimental electronic sound art for an internet installation",
        help="Prompt seed for generation",
    )
    parser.add_argument(
        "--bpm",
        type=int,
        default=_DEFAULT_BPM,
        help="BPM to include in prompts so generation length aligns with tempo (default 120)",
    )
    parser.add_argument(
        "--conditioning-mode",
        choices=("text", "chroma", "continuation", "hybrid"),
        default="text",
        help="Use text only, radio chroma, previous-output continuation, or hybrid conditioning",
    )
    parser.add_argument(
        "--conditioning-seconds",
        type=float,
        default=8.0,
        help="Seconds of audio to use for chroma/continuation conditioning",
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

    # Only chroma/hybrid modes need the melody model. Continuation works
    # with the standard MusicGen model by feeding raw audio as the prompt.
    use_melody_conditioning = args.conditioning_mode in ("chroma", "hybrid")
    model_id = args.model
    if use_melody_conditioning and "melody" not in model_id.lower():
        model_id = "facebook/musicgen-melody"
        print(f"[musicgen] conditioning mode {args.conditioning_mode} requires melody model; using {model_id}")
    if use_melody_conditioning:
        from transformers import MusicgenMelodyForConditionalGeneration

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    processor = AutoProcessor.from_pretrained(model_id)
    if use_melody_conditioning:
        model = MusicgenMelodyForConditionalGeneration.from_pretrained(model_id, torch_dtype=dtype)
    else:
        model = MusicgenForConditionalGeneration.from_pretrained(model_id, torch_dtype=dtype)
    model = model.to(device)
    model.eval()

    # Performance optimizations for faster generation:
    # 1. Use SDPA (scaled dot product attention) — much faster than eager attention
    # 2. Enable CUDA graph-friendly generation with static shapes
    try:
        model.config._attn_implementation = "sdpa"
        # Re-apply with sdpa by reloading attention implementation
        for module in model.modules():
            if hasattr(module, 'config') and hasattr(module.config, '_attn_implementation'):
                module.config._attn_implementation = "sdpa"
        print("[musicgen] attention implementation: sdpa", flush=True)
    except Exception:
        print("[musicgen] attention implementation: default (sdpa setup failed)", flush=True)

    # 2. Enable TF32 for faster matmul on Ampere+ (L40 supports it)
    if device == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        print("[musicgen] TF32 matmul enabled", flush=True)

    # 3. torch.compile() — can give 1.5-3x speedup for autoregressive generation
    if args.compile and device == "cuda":
        try:
            print("[musicgen] compiling model with torch.compile() (may take 1-2 min)...", flush=True)
            model = torch.compile(model, mode="reduce-overhead")
            print("[musicgen] torch.compile() ready", flush=True)
        except Exception as compile_exc:
            print(f"[musicgen] torch.compile() failed ({compile_exc}); using eager mode", flush=True)

    # Pick a random creative seed prompt for this session.
    session_prompt = _pick_random_seed_prompt(args.base_prompt, args.bpm)
    print(f"[musicgen] session prompt: {session_prompt}", flush=True)

    # MusicGen uses approximately 50 audio tokens per second.
    max_new_tokens = max(64, int(args.gen_seconds * 50))
    top_k = max(0, int(args.top_k))
    temperature = max(0.1, float(args.temperature))
    top_p = max(0.05, min(1.0, float(args.top_p)))
    guidance_scale = max(1.0, float(args.guidance_scale))
    parallel_clips = max(1, int(args.parallel_clips))
    drunk_walk_strength = max(0.0, float(args.drunk_walk_strength))
    seed_base = int(args.seed)
    if seed_base < 0:
        seed_base = int(time.time() * 1000) % (2**31 - 1)
    random.seed(seed_base)

    sample_rate = int(model.config.audio_encoder.sampling_rate)
    audio_queue: "queue.Queue[tuple[int, np.ndarray] | None]" | None = None
    playback_thread: threading.Thread | None = None
    udp_proc = None

    if args.audio_udp_url.strip():
        audio_queue = queue.Queue(maxsize=64)
        udp_proc = _start_audio_udp_encoder(sample_rate, args.audio_udp_url.strip())
        # Load ALL existing clips as bootstrap — this gives us a huge buffer
        # from previous runs so playback can run for minutes before first underrun.
        bootstrap_clips = _load_bootstrap_clips(
            out_dir=out_dir,
            max_clips=max(0, int(args.bootstrap_clips)),
            sample_rate=sample_rate,
            sf_module=sf,
        )
        bootstrap_seconds = sum(c.shape[0] / sample_rate for c in bootstrap_clips)
        print(
            f"[musicgen] bootstrap: {len(bootstrap_clips)} clips ({bootstrap_seconds:.1f}s) loaded from {out_dir}",
            flush=True,
        )

    print(
        f"[musicgen] device={device} model={args.model} out={out_dir} "
        f"gen(top_k={top_k}, top_p={top_p}, temperature={temperature}, guidance_scale={guidance_scale}, "
        f"parallel_clips={parallel_clips}, seed={seed_base})"
    )
    resolved_radio_url = ""
    if args.radio_url.strip():
        resolved_radio_url = _resolve_playlist_stream_url(args.radio_url)
        if resolved_radio_url != args.radio_url:
            print(f"[musicgen] resolved playlist URL -> {resolved_radio_url}")
    else:
        print("[musicgen] no radio URL provided — using layered prompt mode (no radio sampling)")

    # Start playback as soon as we have bootstrap clips so the stream can begin
    # immediately, even while new clips are still being generated.
    if args.audio_udp_url.strip():
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
                args.audio_udp_url.strip(),
            ),
            daemon=True,
        )
        playback_thread.start()
        print(
            f"[musicgen] audio stream -> {args.audio_udp_url} "
            f"(delay={args.stream_delay_seconds}s, bootstrap={len(bootstrap_clips)} clips / {bootstrap_seconds:.1f}s)",
            flush=True,
        )

    # --- Pre-generation phase: build a large buffer while playback is already live ---
    # We still generate an initial batch so fresh clips are ready to take over,
    # but the listener hears the bootstrap clips immediately.
    pre_gen_clips: list[np.ndarray] = []
    pre_gen_previous_audio: np.ndarray | None = None

    if args.pre_generate > 0 and audio_queue is not None:
        print(f"[musicgen] pre-generating {args.pre_generate} clips before starting playback...", flush=True)
        for pg_idx in range(args.pre_generate):
            pg_prompt = _build_layered_prompt(session_prompt, pg_idx, args.bpm)
            pg_batch_prompts = [pg_prompt for _ in range(parallel_clips)]
            pg_seed = seed_base + pg_idx
            torch.manual_seed(pg_seed)
            if device == "cuda":
                torch.cuda.manual_seed_all(pg_seed)

            if pre_gen_previous_audio is not None and args.conditioning_mode in ("continuation", "hybrid"):
                pg_cond_batch = [pre_gen_previous_audio for _ in range(parallel_clips)]
                pg_inputs = processor(
                    text=pg_batch_prompts,
                    audio=pg_cond_batch,
                    sampling_rate=sample_rate,
                    padding=True,
                    return_tensors="pt",
                )
            else:
                pg_inputs = processor(text=pg_batch_prompts, padding=True, return_tensors="pt")
            pg_inputs = {k: v.to(device) for k, v in pg_inputs.items()}

            pg_t0 = time.monotonic()
            with torch.no_grad():
                if device == "cuda":
                    with torch.autocast(device_type="cuda", dtype=torch.float16):
                        pg_generated = model.generate(
                            **pg_inputs,
                            do_sample=True,
                            top_k=top_k,
                            top_p=top_p,
                            temperature=temperature,
                            guidance_scale=guidance_scale,
                            max_new_tokens=max_new_tokens,
                        )
                else:
                    pg_generated = model.generate(
                        **pg_inputs,
                        do_sample=True,
                        top_k=top_k,
                        top_p=top_p,
                        temperature=temperature,
                        guidance_scale=guidance_scale,
                        max_new_tokens=max_new_tokens,
                    )
            pg_wall = time.monotonic() - pg_t0
            pg_batch_size = int(pg_generated.shape[0]) if hasattr(pg_generated, "shape") else parallel_clips
            print(
                f"[musicgen] pre-gen clip {pg_idx+1}/{args.pre_generate} "
                f"({pg_batch_size * args.gen_seconds:.1f}s in {pg_wall:.1f}s, RTF={pg_wall / (pg_batch_size * args.gen_seconds):.2f})",
                flush=True,
            )
            for bp in range(pg_batch_size):
                pg_audio = pg_generated[bp].detach().cpu().numpy()
                if pg_audio.ndim == 2 and pg_audio.shape[0] <= 8:
                    pg_audio = pg_audio.T
                if pg_audio.ndim == 1:
                    pg_audio = np.expand_dims(pg_audio, axis=1)
                pg_audio = pg_audio.astype(np.float32, copy=False)
                pg_mono = pg_audio.mean(axis=1) if pg_audio.ndim == 2 else pg_audio.squeeze()
                if pg_mono.ndim == 0:
                    pg_mono = np.array([float(pg_mono)], dtype=np.float32)
                pg_mono = pg_mono.astype(np.float32, copy=False)
                pre_gen_previous_audio = pg_mono[-int(max(1.0, float(args.conditioning_seconds)) * sample_rate) :].copy()
                pg_clip = np.clip(pg_mono, -0.98, 0.98)
                pre_gen_clips.append(pg_clip)
                if audio_queue is not None:
                    audio_queue.put((pg_idx * max(1, pg_batch_size) + bp, pg_clip))

        # Write pre-gen clips to disk so they become bootstrap for future runs
        for pg_idx, pg_clip in enumerate(pre_gen_clips):
            clip_path = out_dir / f"musicgen_clip_{pg_idx:06d}.wav"
            sf.write(str(clip_path), pg_clip.reshape(-1, 1), sample_rate)

        pre_gen_seconds = sum(c.shape[0] / sample_rate for c in pre_gen_clips)
        print(f"[musicgen] pre-generation complete: {len(pre_gen_clips)} clips ({pre_gen_seconds:.1f}s)", flush=True)

        # Add pre-gen clips to bootstrap so playback worker uses them
        bootstrap_clips = (bootstrap_clips or []) + pre_gen_clips
        bootstrap_seconds = sum(c.shape[0] / sample_rate for c in bootstrap_clips)
        print(f"[musicgen] total bootstrap buffer: {len(bootstrap_clips)} clips ({bootstrap_seconds:.1f}s)", flush=True)

    def _generation_loop() -> None:
        current_top_k = top_k
        current_top_p = top_p
        current_temperature = temperature
        current_guidance_scale = guidance_scale
        # Use pre-gen audio as continuation seed so the live stream picks up
        # smoothly from where pre-generation left off.
        previous_generated_audio = pre_gen_previous_audio if pre_gen_previous_audio is not None else None

        def _walk_params() -> tuple[int, float, float, float]:
            nonlocal current_top_k, current_top_p, current_temperature, current_guidance_scale
            if args.drunk_walk and drunk_walk_strength > 0:
                current_top_k += int(round(random.gauss(0.0, 12.0) * drunk_walk_strength))
                current_top_p += random.gauss(0.0, 0.012) * drunk_walk_strength
                current_temperature += random.gauss(0.0, 0.025) * drunk_walk_strength
                current_guidance_scale += random.gauss(0.0, 0.08) * drunk_walk_strength

            current_top_k = max(40, min(280, current_top_k))
            current_top_p = max(0.78, min(0.96, current_top_p))
            current_temperature = max(0.72, min(1.08, current_temperature))
            current_guidance_scale = max(2.8, min(4.2, current_guidance_scale))
            return (
                int(current_top_k),
                round(float(current_top_p), 3),
                round(float(current_temperature), 3),
                round(float(current_guidance_scale), 3),
            )

        index = len(pre_gen_clips) if pre_gen_clips else 0
        while True:
            try:
                with tempfile.TemporaryDirectory(prefix="musicgen_radio_") as tmpdir:
                    radio_wav = os.path.join(tmpdir, "radio.wav")
                    radio_sample_ok = False

                    # Skip radio sampling entirely if no URL provided — saves
                    # ~5-10s per cycle of ffmpeg + librosa overhead.
                    if resolved_radio_url:
                        try:
                            _sample_radio_to_wav(resolved_radio_url, radio_wav, args.sample_seconds, args.sample_rate)
                            descriptors = _extract_descriptors(radio_wav)
                            radio_sample_ok = True
                        except Exception as sample_exc:
                            descriptors = {"tempo": 92.0, "rms": 0.04, "centroid": 1400.0}
                            print(
                                f"[musicgen] radio sample unavailable "
                                f"({type(sample_exc).__name__}: {sample_exc}); using prompt-only generation",
                                flush=True,
                            )
                    else:
                        descriptors = {"tempo": 92.0, "rms": 0.04, "centroid": 1400.0}

                    # Use the session prompt for all clips — continuation
                    # conditioning handles evolution, prompt stays consistent.
                    prompt = _build_layered_prompt(session_prompt, index, args.bpm)
                    batch_top_k, batch_top_p, batch_temperature, batch_guidance_scale = _walk_params()
                    print(
                        f"[musicgen] clip #{index} params top_k={batch_top_k} top_p={batch_top_p:.3f} "
                        f"temperature={batch_temperature:.3f} guidance_scale={batch_guidance_scale:.3f}",
                        flush=True,
                    )

                    batch_prompts = [prompt for _ in range(parallel_clips)]
                    conditioning_source = "text"
                    conditioning_audio = None
                    if args.conditioning_mode in ("continuation", "hybrid") and previous_generated_audio is not None:
                        conditioning_audio = previous_generated_audio
                        conditioning_source = "previous_generated"
                    elif radio_sample_ok and args.conditioning_mode in ("chroma", "hybrid"):
                        conditioning_audio = _load_conditioning_audio(
                            radio_wav,
                            sample_rate=args.sample_rate,
                            seconds=max(1.0, float(args.conditioning_seconds)),
                            sf_module=sf,
                        )
                        conditioning_source = "radio_chroma" if conditioning_audio is not None else "text"

                    # Build inputs — continuation mode feeds raw audio to the
                    # standard MusicGen model via the processor's audio param.
                    if conditioning_audio is not None and not use_melody_conditioning:
                        # Standard MusicGen continuation: pass audio as raw waveform
                        conditioning_batch = [conditioning_audio for _ in range(parallel_clips)]
                        inputs = processor(
                            text=batch_prompts,
                            audio=conditioning_batch,
                            sampling_rate=sample_rate,
                            padding=True,
                            return_tensors="pt",
                        )
                    elif use_melody_conditioning and conditioning_audio is not None:
                        conditioning_batch = [conditioning_audio for _ in range(parallel_clips)]
                        inputs = processor(
                            text=batch_prompts,
                            audio=conditioning_batch,
                            sampling_rate=args.sample_rate,
                            padding=True,
                            return_tensors="pt",
                        )
                    else:
                        inputs = processor(text=batch_prompts, padding=True, return_tensors="pt")
                    inputs = {k: v.to(device) for k, v in inputs.items()}
                    loop_seed = seed_base + index
                    torch.manual_seed(loop_seed)
                    if device == "cuda":
                        torch.cuda.manual_seed_all(loop_seed)
                    generation_started = time.monotonic()
                    with torch.no_grad():
                        if device == "cuda":
                            with torch.autocast(device_type="cuda", dtype=torch.float16):
                                generated = model.generate(
                                    **inputs,
                                    do_sample=True,
                                    top_k=batch_top_k,
                                    top_p=batch_top_p,
                                    temperature=batch_temperature,
                                    guidance_scale=batch_guidance_scale,
                                    max_new_tokens=max_new_tokens,
                                )
                        else:
                            generated = model.generate(
                                **inputs,
                                do_sample=True,
                                top_k=batch_top_k,
                                top_p=batch_top_p,
                                temperature=batch_temperature,
                                guidance_scale=batch_guidance_scale,
                                max_new_tokens=max_new_tokens,
                            )

                    batch_size = int(generated.shape[0]) if hasattr(generated, "shape") else parallel_clips
                    generation_wall_seconds = max(0.001, time.monotonic() - generation_started)
                    generated_audio_seconds = max(0.001, float(batch_size * args.gen_seconds))
                    print(
                        f"[musicgen] generated {generated_audio_seconds:.1f}s audio "
                        f"in {generation_wall_seconds:.1f}s wall "
                        f"(realtime_factor={generation_wall_seconds / generated_audio_seconds:.2f})",
                        flush=True,
                    )
                    for batch_pos in range(batch_size):
                        audio = generated[batch_pos].detach().cpu().numpy()
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
                        previous_generated_audio = mono_audio[-int(max(1.0, float(args.conditioning_seconds)) * sample_rate) :].copy()
                        if audio_queue is not None:
                            audio_queue.put((index, np.clip(mono_audio, -0.98, 0.98)))
                        meta = {
                            "index": index,
                            "batch_pos": batch_pos,
                            "parallel_clips": parallel_clips,
                            "seed": loop_seed,
                            "prompt": prompt,
                            "descriptors": descriptors,
                            "model": model_id,
                            "sample_seconds": args.sample_seconds,
                            "gen_seconds": args.gen_seconds,
                            "conditioning_mode": args.conditioning_mode,
                            "conditioning_source": conditioning_source,
                            "conditioning_seconds": args.conditioning_seconds,
                            "top_k": batch_top_k,
                            "top_p": batch_top_p,
                            "temperature": batch_temperature,
                            "guidance_scale": batch_guidance_scale,
                            "drunk_walk": bool(args.drunk_walk),
                            "drunk_walk_strength": drunk_walk_strength,
                        }
                        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

                        print(f"[musicgen] wrote {clip_path}")
                        index += 1
            except Exception as exc:
                print(f"[musicgen] generation loop error: {type(exc).__name__}: {exc}")
                time.sleep(2)
                continue

            time.sleep(max(0.0, args.pause_seconds))

    # Always start the generation thread — even without audio_queue we still
    # write clips to disk for the separate stream_audio_udp.py to pick up.
    generation_thread = threading.Thread(target=_generation_loop, daemon=True)
    generation_thread.start()
    print("[musicgen] generation thread started", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[musicgen] shutting down...")
    finally:
        if audio_queue is not None:
            audio_queue.put(None)
        if playback_thread is not None:
            playback_thread.join(timeout=5)
        if udp_proc is not None:
            try:
                if udp_proc.stdin is not None:
                    udp_proc.stdin.close()
                udp_proc.wait(timeout=5)
            except Exception:
                try:
                    udp_proc.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
