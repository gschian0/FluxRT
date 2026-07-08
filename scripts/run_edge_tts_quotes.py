#!/usr/bin/env python3
"""
Edge-TTS quote streamer — drop-in replacement for run_quote_tts_from_json.py
that uses Microsoft Edge TTS (free, no API key, no GPU) instead of NVIDIA Riva.

Features:
- Randomly switches between all available en-US Neural voices
- Reverb effect (matching Magpie TTS settings: mix=0.18, decay=0.35, delay=45ms)
- Last-word echo effect (matching Magpie TTS: mix=0.28, decay=0.55, delay=120ms)
- Streams spoken quotes to UDP 5004 as AAC/MPEG-TS for the MediaMTX fanout amix
- Continuous silence between quotes so the UDP stream never drops
"""

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
import wave
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import edge_tts

try:
    import requests as _requests
except ImportError:
    _requests = None

# All available en-US Neural voices for random switching
ALL_VOICES = [
    "en-US-AnaNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-AndrewNeural",
    "en-US-AriaNeural",
    "en-US-AvaMultilingualNeural",
    "en-US-AvaNeural",
    "en-US-BrianMultilingualNeural",
    "en-US-BrianNeural",
    "en-US-ChristopherNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-EmmaNeural",
    "en-US-EricNeural",
    "en-US-GuyNeural",
    "en-US-JennyNeural",
    "en-US-MichelleNeural",
    "en-US-RogerNeural",
    "en-US-SteffanNeural",
]


# ─── Live Gemini quote generation ───────────────────────────────────────────
# When --live-gemini is enabled, the streamer generates fresh quotes on-the-fly
# using Google Gemini 2.5 Flash (thinkingBudget=0 for speed).  This replaces the
# old NVIDIA diffusiongemma endpoint and gives an infinite supply of quotes.

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_GEMINI_MODEL = "gemini-2.5-flash"

_GEMINI_PHILOSOPHERS = [
    "Socrates", "Plato", "Aristotle", "Epictetus", "Marcus Aurelius",
    "Seneca", "Confucius", "Laozi", "Nietzsche", "Kierkegaard", "Emerson",
]
_GEMINI_TOPICS = [
    "patience", "time", "discipline", "attention", "change",
    "hope", "silence", "memory", "fear", "craft",
]
_GEMINI_FORMS = [
    "paradox", "gentle warning", "hard-earned lesson",
    "koan-like reflection", "concrete metaphor", "calm imperative",
]
_GEMINI_MOODS = [
    "stoic", "lucid", "tender", "austere", "curious", "serene",
]


def _load_env_for_gemini() -> str:
    """Load GEMINI_API_KEY from .env files if not already in env."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    for env_path in [
        Path(__file__).resolve().parent.parent / ".env",
        Path("/workspace/AI_TV_ORCHESTRATION/.env"),
    ]:
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    os.environ["GEMINI_API_KEY"] = key
                    return key
    return ""


def generate_gemini_quotes(count: int = 10, timeout: int = 30) -> list[dict]:
    """Generate *count* philosophical quotes via Gemini 2.5 Flash (single API call)."""
    if _requests is None:
        raise RuntimeError("requests library not installed — cannot use live Gemini")
    api_key = _load_env_for_gemini()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not found — set it in .env")

    count = max(1, min(count, 20))  # cap at 20 per call for reliability
    assignments = []
    for _ in range(count):
        assignments.append({
            "philosopher": random.choice(_GEMINI_PHILOSOPHERS),
            "topic": random.choice(_GEMINI_TOPICS),
            "form": random.choice(_GEMINI_FORMS),
            "mood": random.choice(_GEMINI_MOODS),
        })

    prompt_lines = [
        f"Generate exactly {count} short original philosophical quotes. "
        "Each quote should be under 24 words and sound like a real aphorism — "
        "no intro, no numbering, no quotation marks.",
        "",
        "For each quote, use the following tone/topic/form/mood assignments:",
        "",
    ]
    for i, a in enumerate(assignments):
        prompt_lines.append(
            f"Quote {i + 1}: philosopher={a['philosopher']}, "
            f"topic={a['topic']}, form={a['form']}, mood={a['mood']}"
        )
    prompt_lines.extend([
        "",
        "Respond with ONLY a valid JSON array. No markdown, no code fences, no explanation. "
        'Each element must be an object with exactly these fields:\n'
        '  {"quote": "the quote text", "philosopher": "name", "topic": "topic", '
        '"form": "form", "mood": "mood"}',
        "",
        f"Return exactly {count} objects in the array.",
    ])
    prompt = "\n".join(prompt_lines)

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 8192,
            "temperature": 1.0,
            "topP": 0.95,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    url = _GEMINI_URL.format(model=_GEMINI_MODEL)
    resp = _requests.post(url, params={"key": api_key}, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"]

    # Parse JSON array from response
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array in Gemini response: {text[:200]}")
    items = json.loads(text[start:end + 1])

    results = []
    for i, item in enumerate(items):
        quote_text = str(item.get("quote", "")).strip().strip('"\'')
        quote_text = re.sub(r"^[-*\d\.)\s]+", "", quote_text)
        if not quote_text:
            continue
        a = assignments[i] if i < len(assignments) else {}
        results.append({
            "quote": quote_text,
            "philosopher": item.get("philosopher", a.get("philosopher", "Unknown")),
            "topic": item.get("topic", a.get("topic", "unknown")),
            "form": item.get("form", a.get("form", "unknown")),
            "mood": item.get("mood", a.get("mood", "unknown")),
            "model": _GEMINI_MODEL,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    return results


def load_quotes(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Quote JSON must be a list of objects")
    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        quote = str(entry.get("quote", "")).strip()
        philosopher = str(entry.get("philosopher", "")).strip()
        if quote:
            items.append({"quote": quote, "philosopher": philosopher, **entry})
    if not items:
        raise ValueError("No valid quote entries found")
    return items


def build_spoken_text(item: dict, include_author: bool) -> str:
    quote = str(item.get("quote", "")).strip()
    philosopher = str(item.get("philosopher", "")).strip()
    if include_author and philosopher:
        return f"{quote} ... {philosopher}."
    return quote


def extract_last_word(text: str) -> str:
    """Extract the last meaningful word from text for echo targeting."""
    words = re.findall(r"[a-zA-Z']+", text)
    return words[-1] if words else ""


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def apply_fade_in(wav_path: Path, fade_ms: int = 50) -> None:
    """Apply a short fade-in to the start of the WAV to prevent UDP stutter.

    The UDP AAC encoder can clip or stutter on the first few milliseconds
    of audio when a new clip starts. A 50ms linear fade-in smooths this out.
    """
    fade_ms = int(_clamp(float(fade_ms), 0.0, 500.0))
    if fade_ms <= 0:
        return

    with wave.open(str(wav_path), "rb") as in_f:
        n_channels = in_f.getnchannels()
        sampwidth = in_f.getsampwidth()
        frame_rate = in_f.getframerate()
        n_frames = in_f.getnframes()
        audio_bytes = in_f.readframes(n_frames)

    if n_channels != 1 or sampwidth != 2:
        return

    samples = array("h")
    samples.frombytes(audio_bytes)
    if not samples:
        return

    fade_samples = min(int(frame_rate * fade_ms / 1000), len(samples))
    if fade_samples <= 1:
        return

    for idx in range(fade_samples):
        ratio = idx / fade_samples  # 0.0 → 1.0
        samples[idx] = int(samples[idx] * ratio)

    with wave.open(str(wav_path), "wb") as out_f:
        out_f.setnchannels(n_channels)
        out_f.setsampwidth(sampwidth)
        out_f.setframerate(frame_rate)
        out_f.writeframes(samples.tobytes())


def apply_reverb(wav_path: Path, mix: float, decay: float, delay_ms: int) -> None:
    """Apply reverb using feedforward delays (matching magpie_tts_logic.py exactly).

    Uses 4 taps with exponentially decaying gain — no feedback loops, so no
    harsh feedback sound. This is the exact same DSP as the original Magpie TTS.
    """
    mix = _clamp(mix, 0.0, 1.0)
    decay = _clamp(decay, 0.0, 0.95)
    delay_ms = int(_clamp(float(delay_ms), 5.0, 500.0))

    with wave.open(str(wav_path), "rb") as in_f:
        n_channels = in_f.getnchannels()
        sampwidth = in_f.getsampwidth()
        frame_rate = in_f.getframerate()
        n_frames = in_f.getnframes()
        audio_bytes = in_f.readframes(n_frames)

    if n_channels != 1 or sampwidth != 2:
        return

    samples = array("h")
    samples.frombytes(audio_bytes)
    if not samples:
        return

    delay_samples = max(1, int(frame_rate * delay_ms / 1000))
    taps = 4

    wet = [0.0] * len(samples)
    for tap in range(1, taps + 1):
        gain = decay ** tap
        offset = delay_samples * tap
        for idx in range(offset, len(samples)):
            wet[idx] += samples[idx - offset] * gain

    out = array("h")
    dry_gain = 1.0 - mix
    wet_gain = mix
    for idx, dry in enumerate(samples):
        mixed = int(dry * dry_gain + wet[idx] * wet_gain)
        if mixed > 32767:
            mixed = 32767
        elif mixed < -32768:
            mixed = -32768
        out.append(mixed)

    with wave.open(str(wav_path), "wb") as out_f:
        out_f.setnchannels(n_channels)
        out_f.setsampwidth(sampwidth)
        out_f.setframerate(frame_rate)
        out_f.writeframes(out.tobytes())


def apply_last_word_echo(
    wav_path: Path,
    source_text: str,
    target_word: str,
    mix: float,
    decay: float,
    delay_ms: int,
) -> None:
    """Apply echo to the last word only (matching magpie_tts_logic.py exactly).

    Uses 5 feedforward taps starting from the last word's position.
    No feedback loops — just delayed copies mixed in.
    """
    mix = _clamp(mix, 0.0, 1.0)
    decay = _clamp(decay, 0.0, 0.95)
    delay_ms = int(_clamp(float(delay_ms), 5.0, 500.0))

    with wave.open(str(wav_path), "rb") as in_f:
        n_channels = in_f.getnchannels()
        sampwidth = in_f.getsampwidth()
        frame_rate = in_f.getframerate()
        n_frames = in_f.getnframes()
        audio_bytes = in_f.readframes(n_frames)

    if n_channels != 1 or sampwidth != 2:
        return

    samples = array("h")
    samples.frombytes(audio_bytes)
    if not samples:
        return

    clean_text = " ".join(source_text.lower().split())
    clean_target = re.sub(r"[^a-z0-9']+", "", target_word.lower())

    start_ratio = 0.78
    if clean_text and clean_target:
        idx = clean_text.rfind(clean_target)
        if idx >= 0:
            start_ratio = idx / max(1, len(clean_text))

    start_sample = int(_clamp(start_ratio, 0.0, 0.98) * len(samples))
    delay_samples = max(1, int(frame_rate * delay_ms / 1000))
    taps = 5

    wet = [0.0] * len(samples)
    for tap in range(1, taps + 1):
        gain = decay ** tap
        offset = delay_samples * tap
        for idx in range(start_sample + offset, len(samples)):
            src = idx - offset
            if src < start_sample:
                continue
            wet[idx] += samples[src] * gain

    out = array("h")
    dry_gain = 1.0 - mix
    wet_gain = mix
    for idx, dry in enumerate(samples):
        if idx < start_sample:
            mixed = dry
        else:
            mixed = int(dry * dry_gain + wet[idx] * wet_gain)
        if mixed > 32767:
            mixed = 32767
        elif mixed < -32768:
            mixed = -32768
        out.append(mixed)

    with wave.open(str(wav_path), "wb") as out_f:
        out_f.setnchannels(n_channels)
        out_f.setsampwidth(sampwidth)
        out_f.setframerate(frame_rate)
        out_f.writeframes(out.tobytes())


async def synthesize_edge_tts(
    text: str,
    voice: str,
    output_path: Path,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
    reverb: bool = False,
    reverb_mix: float = 0.18,
    reverb_decay: float = 0.35,
    reverb_delay_ms: int = 45,
    last_word_echo: bool = False,
    echo_mix: float = 0.28,
    echo_decay: float = 0.55,
    echo_delay_ms: int = 120,
) -> bool:
    """Generate a WAV file from text using edge-tts with reverb + echo effects."""
    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            volume=volume,
            pitch=pitch,
        )
        mp3_path = output_path.with_suffix(".mp3")
        await communicate.save(str(mp3_path))

        # Convert MP3 to WAV (16-bit PCM, 44100 Hz, mono) via ffmpeg
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(mp3_path),
                "-ar", "44100", "-ac", "1", "-sample_fmt", "s16",
                "-y", str(output_path),
            ],
            check=True,
        )
        mp3_path.unlink(missing_ok=True)

        # Apply fade-in first to prevent UDP stutter on clip start
        apply_fade_in(output_path, fade_ms=50)

        # Apply reverb + echo as pure Python DSP (matching magpie_tts_logic.py)
        if reverb:
            apply_reverb(output_path, reverb_mix, reverb_decay, reverb_delay_ms)
        if last_word_echo:
            echo_word = extract_last_word(text)
            if echo_word:
                apply_last_word_echo(
                    output_path, text, echo_word,
                    echo_mix, echo_decay, echo_delay_ms,
                )
        return True
    except Exception as exc:
        print(f"[edge-tts] synthesis failed: {type(exc).__name__}: {exc}")
        mp3_path = output_path.with_suffix(".mp3")
        mp3_path.unlink(missing_ok=True)
        return False


class QuoteCache:
    """Background-render quote WAVs so the streamer never blocks on TTS."""

    def __init__(
        self,
        quotes: list[dict],
        cache_dir: Path,
        voices: list[str],
        rate: str,
        volume: str,
        pitch: str,
        include_author: bool,
        reverb: bool,
        reverb_mix: float,
        reverb_decay: float,
        reverb_delay_ms: int,
        last_word_echo: bool,
        echo_mix: float,
        echo_decay: float,
        echo_delay_ms: int,
        target_size: int = 4,
        live_gemini: bool = False,
        gemini_refresh_threshold: int = 5,
        gemini_refresh_count: int = 10,
        gemini_timeout: int = 30,
    ):
        self._quotes = quotes
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._voices = voices
        self._rate = rate
        self._volume = volume
        self._pitch = pitch
        self._include_author = include_author
        self._reverb = reverb
        self._reverb_mix = reverb_mix
        self._reverb_decay = reverb_decay
        self._reverb_delay_ms = reverb_delay_ms
        self._last_word_echo = last_word_echo
        self._echo_mix = echo_mix
        self._echo_decay = echo_decay
        self._echo_delay_ms = echo_delay_ms
        self._target_size = target_size
        self._queue: list[tuple[Path, str]] = []  # (wav_path, voice_name)
        self._fallback_paths: list[tuple[Path, str]] = []
        self._stop_event = asyncio.Event()
        self._index = 0
        self._cache_index = 0
        self._load_existing_cache()

        # Live Gemini auto-refresh state
        self._live_gemini = live_gemini
        self._gemini_refresh_threshold = gemini_refresh_threshold
        self._gemini_refresh_count = gemini_refresh_count
        self._gemini_timeout = gemini_timeout
        self._gemini_refreshing = False
        self._spoken_indices = 0  # how many quotes have been dispatched from the original pool

    def _load_existing_cache(self) -> None:
        existing = sorted(self._cache_dir.glob("quote_*.wav"))
        for p in existing[-max(1, self._target_size * 2):]:
            self._fallback_paths.append((p, "unknown"))
        for p, v in self._fallback_paths[-self._target_size:]:
            self._queue.append((p, v))

    async def _render_one(self, item: dict) -> Optional[tuple[Path, str]]:
        text = build_spoken_text(item, include_author=self._include_author)
        voice = random.choice(self._voices)
        cache_path = self._cache_dir / f"quote_{int(time.time())}_{self._cache_index:06d}.wav"
        self._cache_index += 1
        ok = await synthesize_edge_tts(
            text=text,
            voice=voice,
            output_path=cache_path,
            rate=self._rate,
            volume=self._volume,
            pitch=self._pitch,
            reverb=self._reverb,
            reverb_mix=self._reverb_mix,
            reverb_decay=self._reverb_decay,
            reverb_delay_ms=self._reverb_delay_ms,
            last_word_echo=self._last_word_echo,
            echo_mix=self._echo_mix,
            echo_decay=self._echo_decay,
            echo_delay_ms=self._echo_delay_ms,
        )
        if not ok:
            return None
        return (cache_path, voice)

    def _maybe_refresh_gemini(self) -> None:
        """Check if we need to generate fresh quotes via Gemini and do it synchronously."""
        if not self._live_gemini or self._gemini_refreshing:
            return
        remaining = len(self._quotes) - self._spoken_indices
        if remaining > self._gemini_refresh_threshold:
            return
        self._gemini_refreshing = True
        try:
            print(f"[gemini] Pool low ({remaining} left) — generating {self._gemini_refresh_count} fresh quotes...", flush=True)
            new_quotes = generate_gemini_quotes(
                count=self._gemini_refresh_count,
                timeout=self._gemini_timeout,
            )
            if new_quotes:
                self._quotes.extend(new_quotes)
                print(f"[gemini] Added {len(new_quotes)} fresh quotes — pool now {len(self._quotes)} total", flush=True)
                for q in new_quotes:
                    print(f"  [gemini] \"{q['quote'][:60]}...\" — {q['philosopher']}", flush=True)
            else:
                print("[gemini] No quotes returned, will retry later", flush=True)
        except Exception as exc:
            print(f"[gemini] Refresh failed: {type(exc).__name__}: {exc}", flush=True)
        finally:
            self._gemini_refreshing = False

    async def _worker(self) -> None:
        while not self._stop_event.is_set():
            need = max(0, self._target_size - len(self._queue))
            if need == 0:
                await asyncio.sleep(0.5)
                continue

            # Live Gemini refresh: generate fresh quotes when pool runs low
            if self._live_gemini:
                self._maybe_refresh_gemini()

            item = self._quotes[self._index % len(self._quotes)]
            self._index += 1
            self._spoken_indices += 1
            result = await self._render_one(item)
            if result is not None:
                self._queue.append(result)
                self._fallback_paths.append(result)
                self._fallback_paths = self._fallback_paths[-max(1, self._target_size * 2):]

    async def get_async(self, timeout: float = 30.0) -> Optional[tuple[Path, str]]:
        """Async get — yields control so the renderer worker can run."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue:
                return self._queue.pop(0)
            await asyncio.sleep(0.1)
        if self._fallback_paths:
            return random.choice(self._fallback_paths)
        return None

    def stop(self) -> None:
        self._stop_event.set()


class UdpAudioWriter:
    """Stream WAV files as AAC/MPEG-TS to a UDP URL via ffmpeg."""

    def __init__(self, udp_url: str, input_rate_hz: int = 44100):
        self.udp_url = udp_url
        self.input_rate_hz = int(input_rate_hz)
        self.channels = 1
        self.sample_width = 2
        self.chunk_frames = 4096
        self.proc = self._start_ffmpeg()

    def _start_ffmpeg(self) -> subprocess.Popen:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le",
            "-ar", str(self.input_rate_hz),
            "-ac", str(self.channels),
            "-i", "-",
            "-c:a", "aac", "-b:a", "192k",
            "-ar", "48000", "-ac", "2",
            "-fflags", "+genpts",
            "-f", "mpegts",
            self.udp_url,
        ]
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=65536)

    def _write_pcm_realtime(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        if self.proc.stdin is None:
            raise RuntimeError("UDP writer stdin is not available")

        bytes_per_frame = self.channels * self.sample_width
        chunk_size = self.chunk_frames * bytes_per_frame
        next_deadline = time.monotonic()
        for offset in range(0, len(pcm_bytes), chunk_size):
            chunk = pcm_bytes[offset: offset + chunk_size]
            try:
                self.proc.stdin.write(chunk)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.proc = self._start_ffmpeg()
                self.proc.stdin.write(chunk)
                self.proc.stdin.flush()
            frames = len(chunk) / bytes_per_frame
            next_deadline += frames / float(self.input_rate_hz)
            delay = next_deadline - time.monotonic()
            if delay > 0.001:
                time.sleep(delay)

    def write_wav(self, wav_path: Path) -> None:
        with wave.open(str(wav_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_rate = wav_file.getframerate()
            pcm_bytes = wav_file.readframes(wav_file.getnframes())

        if channels != self.channels or sample_width != self.sample_width or frame_rate != self.input_rate_hz:
            raise RuntimeError(
                f"Unexpected WAV format: channels={channels}, sample_width={sample_width}, rate={frame_rate}"
            )
        self._write_pcm_realtime(pcm_bytes)

    def write_silence(self, duration_seconds: float) -> None:
        duration_seconds = max(0.0, float(duration_seconds))
        if duration_seconds <= 0:
            return
        total_frames = int(duration_seconds * self.input_rate_hz)
        bytes_per_frame = self.channels * self.sample_width
        silence_chunk = b"\x00" * (self.chunk_frames * bytes_per_frame)
        frames_remaining = total_frames
        while frames_remaining > 0:
            frames_to_write = min(self.chunk_frames, frames_remaining)
            byte_count = frames_to_write * bytes_per_frame
            self._write_pcm_realtime(silence_chunk[:byte_count])
            frames_remaining -= frames_to_write

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


async def main_async() -> None:
    parser = argparse.ArgumentParser(
        description="Speak quote JSON entries using Edge TTS with reverb + echo + random voices"
    )
    parser.add_argument(
        "--quotes",
        default="data/quotes/diffusiongemma_quotes.json",
        help="Path to quote JSON",
    )
    parser.add_argument("--loop", action="store_true", help="Loop forever through quotes")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle quote order")
    parser.add_argument("--interval", type=float, default=15.0, help="Seconds between spoken quotes")
    parser.add_argument("--max", type=int, default=0, help="Maximum quotes to speak (0 = all/infinite)")
    parser.add_argument("--no-author", action="store_true", help="Do not append philosopher name")

    # Voice selection
    parser.add_argument(
        "--voice",
        default="",
        help="Single voice to use (overrides --random-voices). If empty, uses random voices.",
    )
    parser.add_argument(
        "--random-voices",
        action="store_true",
        default=True,
        help="Randomly switch between all available en-US voices (default: True)",
    )
    parser.add_argument(
        "--no-random-voices",
        dest="random_voices",
        action="store_false",
        help="Use a single voice instead of random switching",
    )

    # Speech parameters
    parser.add_argument("--rate", default="+0%", help="Speech rate (e.g. +10%%, -10%%)")
    parser.add_argument("--volume", default="+0%", help="Volume adjustment (e.g. +20%%)")
    parser.add_argument("--pitch", default="+0Hz", help="Pitch adjustment (e.g. +5Hz, -5Hz)")

    # Reverb (matching Magpie TTS defaults)
    parser.add_argument("--reverb", action="store_true", default=True, help="Enable reverb effect")
    parser.add_argument("--no-reverb", dest="reverb", action="store_false", help="Disable reverb")
    parser.add_argument("--reverb-mix", type=float, default=0.35, help="Reverb wet mix (0-1)")
    parser.add_argument("--reverb-decay", type=float, default=0.50, help="Reverb decay (0-1)")
    parser.add_argument("--reverb-delay-ms", type=int, default=60, help="Reverb delay in ms")

    # Last-word echo (matching Magpie TTS defaults)
    parser.add_argument("--echo", action="store_true", default=True, help="Enable last-word echo")
    parser.add_argument("--no-echo", dest="echo", action="store_false", help="Disable echo")
    parser.add_argument("--echo-mix", type=float, default=0.45, help="Echo wet mix (0-1)")
    parser.add_argument("--echo-decay", type=float, default=0.65, help="Echo decay (0-1)")
    parser.add_argument("--echo-delay-ms", type=int, default=150, help="Echo delay in ms")

    # Streaming
    parser.add_argument(
        "--udp-url",
        default="",
        help="If set, stream each TTS wav to this UDP URL (for live ffmpeg mix)",
    )
    parser.add_argument("--repeat-on-empty", action="store_true", help="Repeat existing quotes when out")
    parser.add_argument("--cache-dir", default="voices/quote_cache_edge", help="Directory for cached WAVs")
    parser.add_argument("--cache-size", type=int, default=4, help="Pre-rendered quotes to keep ready")

    # Live Gemini auto-refresh
    parser.add_argument("--live-gemini", action="store_true", help="Auto-generate fresh quotes via Gemini 2.5 Flash when pool runs low")
    parser.add_argument("--gemini-refresh-threshold", type=int, default=5, help="Refresh when remaining unspoken quotes <= this")
    parser.add_argument("--gemini-refresh-count", type=int, default=10, help="How many quotes to generate per Gemini refresh")
    parser.add_argument("--gemini-timeout", type=int, default=30, help="Gemini API timeout in seconds")
    args = parser.parse_args()

    quote_path = Path(args.quotes)
    if not quote_path.exists():
        raise SystemExit(f"Quote JSON not found: {quote_path}")

    quotes = load_quotes(quote_path)
    print(f"Loaded {len(quotes)} quotes from {quote_path}")

    # Determine voice list
    if args.voice:
        voices = [args.voice]
        print(f"Voice: {args.voice} (single voice)")
    elif args.random_voices:
        voices = ALL_VOICES[:]
        print(f"Voices: {len(voices)} en-US Neural voices (random switching)")
    else:
        voices = ["en-US-AriaNeural"]
        print(f"Voice: en-US-AriaNeural (default)")

    if args.shuffle:
        random.shuffle(quotes)

    print(f"Reverb: {'ON' if args.reverb else 'OFF'} (mix={args.reverb_mix}, decay={args.reverb_decay}, delay={args.reverb_delay_ms}ms)")
    print(f"Echo: {'ON' if args.echo else 'OFF'} (mix={args.echo_mix}, decay={args.echo_decay}, delay={args.echo_delay_ms}ms)")

    udp_writer = UdpAudioWriter(args.udp_url.strip()) if args.udp_url.strip() else None
    cache = QuoteCache(
        quotes=quotes,
        cache_dir=Path(args.cache_dir),
        voices=voices,
        rate=args.rate,
        volume=args.volume,
        pitch=args.pitch,
        include_author=not args.no_author,
        reverb=args.reverb,
        reverb_mix=args.reverb_mix,
        reverb_decay=args.reverb_decay,
        reverb_delay_ms=args.reverb_delay_ms,
        last_word_echo=args.echo,
        echo_mix=args.echo_mix,
        echo_decay=args.echo_decay,
        echo_delay_ms=args.echo_delay_ms,
        target_size=max(1, int(args.cache_size)),
        live_gemini=args.live_gemini,
        gemini_refresh_threshold=args.gemini_refresh_threshold,
        gemini_refresh_count=args.gemini_refresh_count,
        gemini_timeout=args.gemini_timeout,
    )

    if args.live_gemini:
        print(f"[gemini] Live mode ON — will auto-generate {args.gemini_refresh_count} fresh quotes when pool <= {args.gemini_refresh_threshold}", flush=True)

    # Start background renderer
    renderer_task = asyncio.create_task(cache._worker())

    spoken_count = 0
    try:
        while True:
            if args.max > 0 and spoken_count >= args.max:
                break

            out_path = await cache.get_async(timeout=30.0)
            if out_path is None:
                print("[quotes] cache empty; waiting for renderer...")
                await asyncio.sleep(1)
                continue

            wav_path, voice_name = out_path
            spoken_count += 1
            print(f"[{spoken_count}] voice={voice_name} streaming: {wav_path.name}")

            if udp_writer is not None:
                try:
                    udp_writer.write_wav(wav_path)
                    print(f"  → streamed to {args.udp_url.strip()}")
                except Exception as exc:
                    print(f"  → UDP stream failed: {type(exc).__name__}: {exc}")

            if args.interval > 0:
                if udp_writer is not None:
                    udp_writer.write_silence(args.interval)
                else:
                    await asyncio.sleep(args.interval)

            if not args.loop and spoken_count >= len(quotes) and not args.repeat_on_empty:
                break
    finally:
        cache.stop()
        renderer_task.cancel()
        if udp_writer is not None:
            udp_writer.close()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
