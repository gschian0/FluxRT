#!/usr/bin/env python3
"""
Edge-TTS quote streamer — drop-in replacement for run_quote_tts_from_json.py
that uses Microsoft Edge TTS (free, no API key, no GPU) instead of NVIDIA Riva.

Streams spoken quotes to UDP 5004 as AAC/MPEG-TS for the MediaMTX fanout amix.
"""

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Optional

import edge_tts


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


async def synthesize_edge_tts(
    text: str,
    voice: str,
    output_path: Path,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
) -> bool:
    """Generate a WAV file from text using edge-tts."""
    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            volume=volume,
            pitch=pitch,
        )
        # edge-tts outputs MP3, so we save MP3 then convert to WAV via ffmpeg
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
        return True
    except Exception as exc:
        print(f"[edge-tts] synthesis failed: {type(exc).__name__}: {exc}")
        return False


class QuoteCache:
    """Background-render quote WAVs so the streamer never blocks on TTS."""

    def __init__(
        self,
        quotes: list[dict],
        cache_dir: Path,
        voice: str,
        rate: str,
        volume: str,
        pitch: str,
        include_author: bool,
        target_size: int = 4,
    ):
        self._quotes = quotes
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._voice = voice
        self._rate = rate
        self._volume = volume
        self._pitch = pitch
        self._include_author = include_author
        self._target_size = target_size
        self._queue: list[Path] = []
        self._fallback_paths: list[Path] = []
        self._stop_event = asyncio.Event()
        self._index = 0
        self._cache_index = 0
        self._load_existing_cache()

    def _load_existing_cache(self) -> None:
        existing = sorted(self._cache_dir.glob("quote_*.wav"))
        self._fallback_paths = existing[-max(1, self._target_size * 2):]
        self._queue.extend(self._fallback_paths[-self._target_size:])

    async def _render_one(self, item: dict) -> Optional[Path]:
        text = build_spoken_text(item, include_author=self._include_author)
        cache_path = self._cache_dir / f"quote_{int(time.time())}_{self._cache_index:06d}.wav"
        self._cache_index += 1
        ok = await synthesize_edge_tts(
            text=text,
            voice=self._voice,
            output_path=cache_path,
            rate=self._rate,
            volume=self._volume,
            pitch=self._pitch,
        )
        if not ok:
            return None
        return cache_path

    async def _worker(self) -> None:
        while not self._stop_event.is_set():
            need = max(0, self._target_size - len(self._queue))
            if need == 0:
                await asyncio.sleep(0.5)
                continue
            item = self._quotes[self._index % len(self._quotes)]
            self._index += 1
            path = await self._render_one(item)
            if path is not None:
                self._queue.append(path)
                self._fallback_paths.append(path)
                self._fallback_paths = self._fallback_paths[-max(1, self._target_size * 2):]

    def get(self, timeout: float = 30.0) -> Optional[Path]:
        """Synchronous get — used from the main async loop via run_in_executor."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue:
                return self._queue.pop(0)
            time.sleep(0.1)
        if self._fallback_paths:
            return random.choice(self._fallback_paths)
        return None

    async def get_async(self, timeout: float = 30.0) -> Optional[Path]:
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
        description="Speak quote JSON entries using Edge TTS (Microsoft Neural voices)"
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

    parser.add_argument("--voice", default="en-US-AriaNeural", help="Edge TTS voice name")
    parser.add_argument("--rate", default="+0%", help="Speech rate (e.g. +10%%, -10%%)")
    parser.add_argument("--volume", default="+0%", help="Volume adjustment (e.g. +20%%)")
    parser.add_argument("--pitch", default="+0Hz", help="Pitch adjustment (e.g. +5Hz, -5Hz)")

    parser.add_argument(
        "--udp-url",
        default="",
        help="If set, stream each TTS wav to this UDP URL (for live ffmpeg mix)",
    )
    parser.add_argument("--repeat-on-empty", action="store_true", help="Repeat existing quotes when out")
    parser.add_argument("--cache-dir", default="voices/quote_cache_edge", help="Directory for cached WAVs")
    parser.add_argument("--cache-size", type=int, default=4, help="Pre-rendered quotes to keep ready")
    args = parser.parse_args()

    quote_path = Path(args.quotes)
    if not quote_path.exists():
        raise SystemExit(f"Quote JSON not found: {quote_path}")

    quotes = load_quotes(quote_path)
    print(f"Loaded {len(quotes)} quotes from {quote_path}")
    print(f"Voice: {args.voice}")

    if args.shuffle:
        random.shuffle(quotes)

    udp_writer = UdpAudioWriter(args.udp_url.strip()) if args.udp_url.strip() else None
    cache = QuoteCache(
        quotes=quotes,
        cache_dir=Path(args.cache_dir),
        voice=args.voice,
        rate=args.rate,
        volume=args.volume,
        pitch=args.pitch,
        include_author=not args.no_author,
        target_size=max(1, int(args.cache_size)),
    )

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

            spoken_count += 1
            print(f"[{spoken_count}] streaming: {out_path.name}")

            if udp_writer is not None:
                try:
                    udp_writer.write_wav(out_path)
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
