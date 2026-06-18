import argparse
import json
import random
import subprocess
import sys
import time
import wave
from pathlib import Path

from magpie_tts_logic import extract_last_word, run_tts


def load_quotes(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Quote JSON must be a list of objects")
    items: list[dict] = []
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


class UdpAudioWriter:
    def __init__(self, udp_url: str, input_rate_hz: int):
        self.udp_url = udp_url
        self.input_rate_hz = int(input_rate_hz)
        self.channels = 1
        self.sample_width = 2
        self.chunk_frames = 4096
        self.proc = self._start_ffmpeg()

    def _start_ffmpeg(self) -> subprocess.Popen:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(self.input_rate_hz),
            "-ac",
            str(self.channels),
            "-i",
            "-",
            "-c:a",
            "aac",
            "-b:a",
            "192k",  # Increased from 128k for better quality
            "-ar",
            "48000",
            "-ac",
            "2",
            "-fflags",
            "+genpts",  # Generate pts for smoother streaming
            "-f",
            "mpegts",
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
                self.proc.stdin.flush()  # Force flush to avoid buffering stalls
            except (BrokenPipeError, OSError):
                # UDP ffmpeg crashed, restart it
                self.proc = self._start_ffmpeg()
                self.proc.stdin.write(chunk)
                self.proc.stdin.flush()
            frames = len(chunk) / bytes_per_frame
            next_deadline += frames / float(self.input_rate_hz)
            delay = next_deadline - time.monotonic()
            if delay > 0.001:  # Only sleep if meaningful delay
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


def refresh_quotes_with_diffusiongemma(quote_path: Path, count: int) -> list[dict]:
    script_path = Path(__file__).resolve().parent / "generate_quote_data_diffusiongemma.py"
    if not script_path.exists():
        raise RuntimeError(f"Missing generator script: {script_path}")

    cmd = [
        sys.executable,
        str(script_path),
        "--count",
        str(max(1, int(count))),
        "--output",
        str(quote_path),
    ]
    subprocess.run(cmd, check=True)
    return load_quotes(quote_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Speak quote JSON entries using Magpie TTS")
    parser.add_argument(
        "--quotes",
        default="data/quotes/diffusiongemma_quotes.json",
        help="Path to quote JSON generated by generate_quote_data_diffusiongemma.py",
    )
    parser.add_argument("--loop", action="store_true", help="Loop forever through quotes")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle quote order")
    parser.add_argument("--interval", type=float, default=30.0, help="Seconds between spoken quotes")
    parser.add_argument("--max", type=int, default=0, help="Maximum quotes to speak (0 means all / infinite with --loop)")
    parser.add_argument("--no-author", action="store_true", help="Do not append philosopher name to spoken line")

    parser.add_argument("--voice", default="Magpie-Multilingual.EN-US.Aria")
    parser.add_argument("--lang", default="en-US")
    parser.add_argument("--rate", type=int, default=44100)
    parser.add_argument("--silence", type=float, default=0.75)

    parser.add_argument("--reverb", action="store_true")
    parser.add_argument("--reverb-mix", type=float, default=0.32)
    parser.add_argument("--reverb-decay", type=float, default=0.55)
    parser.add_argument("--reverb-delay-ms", type=int, default=70)

    parser.add_argument("--last-word-echo", action="store_true")
    parser.add_argument("--echo-target-word", default="")
    parser.add_argument("--echo-mix", type=float, default=0.52)
    parser.add_argument("--echo-decay", type=float, default=0.78)
    parser.add_argument("--echo-delay-ms", type=int, default=170)

    parser.add_argument("--play", action="store_true", help="Play WAV after generation")
    parser.add_argument(
        "--udp-url",
        default="",
        help="If set, stream each generated TTS wav to this UDP URL (for live ffmpeg mix)",
    )
    parser.add_argument("--auto-refresh", action="store_true", help="Auto-regenerate quotes when queue is low")
    parser.add_argument("--refresh-threshold", type=int, default=3, help="Refresh when remaining quotes <= this")
    parser.add_argument("--refresh-count", type=int, default=30, help="How many quotes to generate on refresh")
    parser.add_argument("--repeat-on-empty", action="store_true", help="Repeat existing quotes when out and no refresh")
    args = parser.parse_args()

    quote_path = Path(args.quotes)
    if not quote_path.exists():
        raise SystemExit(f"Quote JSON not found: {quote_path}")

    quotes = load_quotes(quote_path)
    print(f"Loaded {len(quotes)} quotes from {quote_path}")

    spoken_count = 0
    index = 0
    udp_writer = UdpAudioWriter(args.udp_url.strip(), args.rate) if args.udp_url.strip() else None

    if args.shuffle:
        random.shuffle(quotes)

    try:
        while True:
            remaining = len(quotes) - index
            if remaining <= 0:
                if args.auto_refresh:
                    try:
                        print("[quotes] queue exhausted; auto-refreshing...")
                        quotes = refresh_quotes_with_diffusiongemma(quote_path, args.refresh_count)
                        index = 0
                        if args.shuffle:
                            random.shuffle(quotes)
                        print(f"[quotes] refresh complete: {len(quotes)} new quotes")
                        remaining = len(quotes)
                    except Exception as exc:
                        print(f"[quotes] refresh failed: {type(exc).__name__}: {exc}")
                        if args.repeat_on_empty:
                            print("[quotes] repeating existing quote set")
                            index = 0
                            remaining = len(quotes)
                        else:
                            return
                elif args.repeat_on_empty:
                    print("[quotes] out of quotes; repeating existing quote set")
                    index = 0
                    remaining = len(quotes)
                else:
                    return

            if remaining <= max(0, int(args.refresh_threshold)):
                print(f"[quotes] warning: low queue ({remaining} remaining)")
                if args.auto_refresh:
                    try:
                        print("[quotes] pre-emptive refresh starting...")
                        quotes = refresh_quotes_with_diffusiongemma(quote_path, args.refresh_count)
                        index = 0
                        if args.shuffle:
                            random.shuffle(quotes)
                        remaining = len(quotes)
                        print(f"[quotes] pre-emptive refresh complete: {remaining} quotes")
                    except Exception as exc:
                        print(f"[quotes] pre-emptive refresh failed: {type(exc).__name__}: {exc}")

            item = quotes[index]
            index += 1
            text = build_spoken_text(item, include_author=not args.no_author)
            target_word = args.echo_target_word.strip() or extract_last_word(str(item.get("quote", text)))

            out_path = run_tts(
                text=text,
                output_path=None,
                voice=args.voice,
                lang=args.lang,
                rate=args.rate,
                silence=args.silence,
                reverb=args.reverb,
                reverb_mix=args.reverb_mix,
                reverb_decay=args.reverb_decay,
                reverb_delay_ms=args.reverb_delay_ms,
                last_word_echo=args.last_word_echo,
                echo_target_word=target_word,
                echo_mix=args.echo_mix,
                echo_decay=args.echo_decay,
                echo_delay_ms=args.echo_delay_ms,
                play=args.play,
            )
            spoken_count += 1
            print(f"[{spoken_count}] {item.get('quote', '')} — {item.get('philosopher', '')}")
            print(f"Audio: {out_path}")

            if udp_writer is not None:
                try:
                    udp_writer.write_wav(out_path)
                    print(f"Streamed to: {args.udp_url.strip()}")
                except Exception as exc:
                    print(f"UDP stream failed: {type(exc).__name__}: {exc}")

            if args.max > 0 and spoken_count >= args.max:
                return

            if args.interval > 0:
                # Sleep between quotes. UDP subprocess maintains continuous stream in background.
                time.sleep(args.interval)

            if not args.loop and index >= len(quotes) and not args.repeat_on_empty and not args.auto_refresh:
                return
    finally:
        if udp_writer is not None:
            udp_writer.close()


if __name__ == "__main__":
    main()
