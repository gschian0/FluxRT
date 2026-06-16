import argparse
import array
import os
import re
import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False

import riva.client

try:
    import winsound
except ImportError:
    winsound = None

def _load_env_file_manually(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False

    loaded_any = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded_any = True
    return loaded_any


def _load_runtime_env() -> None:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    candidates = [
        repo_root / ".env",
        repo_root / "MagpieTTS" / ".env",
        repo_root.parent / "MagpieTTS" / ".env",
        Path("/home/gschi/MagpieTTS/.env"),
    ]

    # First attempt dotenv when available, then fall back to manual parsing.
    for env_path in candidates:
        if not env_path.is_file():
            continue
        loaded = False
        try:
            loaded = bool(load_dotenv(dotenv_path=env_path, override=False))
        except TypeError:
            loaded = bool(load_dotenv())
        except Exception:
            loaded = False
        if not loaded:
            _load_env_file_manually(env_path)


_load_runtime_env()

SERVER = os.environ.get("MAGPIE_RIVA_SERVER", "grpc.nvcf.nvidia.com:443")
FUNCTION_ID = os.environ.get("MAGPIE_FUNCTION_ID", "877104f7-e885-42b9-8de8-f6e4c6303969")
DEFAULT_VOICE = os.environ.get("MAGPIE_VOICE", "Magpie-Multilingual.EN-US.Aria")
DEFAULT_LANGUAGE = os.environ.get("MAGPIE_LANGUAGE", "en-US")
DEFAULT_SAMPLE_RATE_HZ = int(os.environ.get("MAGPIE_SAMPLE_RATE_HZ", "44100"))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("MAGPIE_OUTPUT_DIR", "voices"))
NCHANNELS = 1
SAMPWIDTH = 2
LEADING_SILENCE_SECONDS = float(os.environ.get("MAGPIE_LEADING_SILENCE_SECONDS", "0.75"))


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def build_auth() -> riva.client.Auth:
    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY is not set")

    return riva.client.Auth(
        use_ssl=True,
        uri=SERVER,
        metadata_args=[
            ["function-id", FUNCTION_ID],
            ["authorization", f"Bearer {api_key}"],
        ],
    )


def synthesize_to_wav(
    text: str,
    output_path: Path,
    voice_name: str,
    language_code: str,
    sample_rate_hz: int,
    leading_silence_seconds: float,
) -> Path:
    auth = build_auth()
    service = riva.client.SpeechSynthesisService(auth)
    response = service.synthesize(
        text=text,
        voice_name=voice_name,
        language_code=language_code,
        sample_rate_hz=sample_rate_hz,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    leading_silence = b"\x00" * int(sample_rate_hz * leading_silence_seconds) * SAMPWIDTH
    with wave.open(str(output_path), "wb") as out_f:
        out_f.setnchannels(NCHANNELS)
        out_f.setsampwidth(SAMPWIDTH)
        out_f.setframerate(sample_rate_hz)
        out_f.writeframes(leading_silence + response.audio)

    return output_path


def apply_reverb(wav_path: Path, mix: float, decay: float, delay_ms: int) -> None:
    mix = clamp(mix, 0.0, 1.0)
    decay = clamp(decay, 0.0, 0.95)
    delay_ms = int(clamp(float(delay_ms), 5.0, 500.0))

    with wave.open(str(wav_path), "rb") as in_f:
        n_channels = in_f.getnchannels()
        sampwidth = in_f.getsampwidth()
        frame_rate = in_f.getframerate()
        n_frames = in_f.getnframes()
        audio_bytes = in_f.readframes(n_frames)

    if n_channels != 1 or sampwidth != 2:
        return

    samples = array.array("h")
    samples.frombytes(audio_bytes)
    if not samples:
        return

    delay_samples = max(1, int(frame_rate * delay_ms / 1000))
    taps = 4

    wet = [0.0] * len(samples)
    for tap in range(1, taps + 1):
        gain = decay**tap
        offset = delay_samples * tap
        for idx in range(offset, len(samples)):
            wet[idx] += samples[idx - offset] * gain

    out = array.array("h")
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
    mix = clamp(mix, 0.0, 1.0)
    decay = clamp(decay, 0.0, 0.95)
    delay_ms = int(clamp(float(delay_ms), 5.0, 500.0))

    with wave.open(str(wav_path), "rb") as in_f:
        n_channels = in_f.getnchannels()
        sampwidth = in_f.getsampwidth()
        frame_rate = in_f.getframerate()
        n_frames = in_f.getnframes()
        audio_bytes = in_f.readframes(n_frames)

    if n_channels != 1 or sampwidth != 2:
        return

    samples = array.array("h")
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

    start_sample = int(clamp(start_ratio, 0.0, 0.98) * len(samples))
    delay_samples = max(1, int(frame_rate * delay_ms / 1000))
    taps = 5

    wet = [0.0] * len(samples)
    for tap in range(1, taps + 1):
        gain = decay**tap
        offset = delay_samples * tap
        for idx in range(start_sample + offset, len(samples)):
            src = idx - offset
            if src < start_sample:
                continue
            wet[idx] += samples[src] * gain

    out = array.array("h")
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


def extract_last_word(text: str) -> str:
    words = re.findall(r"[a-zA-Z']+", text)
    return words[-1] if words else ""


def build_default_output_path(output_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return output_dir / f"magpie_tts-{ts}.wav"


def play_wav(path: Path) -> None:
    resolved = path.resolve()

    if winsound is not None and sys.platform.startswith("win"):
        winsound.PlaySound(str(resolved), winsound.SND_FILENAME)
        return

    # Linux/macOS fallback via ffplay when available.
    ffplay = shutil_which("ffplay")
    if ffplay:
        subprocess.run([ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(resolved)], check=False)
        return

    aplay = shutil_which("aplay")
    if aplay:
        subprocess.run([aplay, str(resolved)], check=False)
        return

    print(f"Playback tool not found. WAV written to: {resolved}")


def shutil_which(binary: str) -> str | None:
    from shutil import which

    return which(binary)


def run_tts(
    text: str,
    output_path: Path | None,
    voice: str,
    lang: str,
    rate: int,
    silence: float,
    reverb: bool,
    reverb_mix: float,
    reverb_decay: float,
    reverb_delay_ms: int,
    last_word_echo: bool,
    echo_target_word: str,
    echo_mix: float,
    echo_decay: float,
    echo_delay_ms: int,
    play: bool,
) -> Path:
    if not text.strip():
        raise ValueError("No text provided")

    out = output_path or build_default_output_path(DEFAULT_OUTPUT_DIR)
    out = synthesize_to_wav(text, out, voice, lang, rate, silence)

    if reverb:
        apply_reverb(out, mix=reverb_mix, decay=reverb_decay, delay_ms=reverb_delay_ms)

    if last_word_echo:
        target = echo_target_word.strip() or extract_last_word(text)
        if target:
            apply_last_word_echo(
                out,
                source_text=text,
                target_word=target,
                mix=echo_mix,
                decay=echo_decay,
                delay_ms=echo_delay_ms,
            )

    if play:
        play_wav(out)

    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Magpie-style TTS with FX")
    parser.add_argument("--text", required=True, help="Text to synthesize")
    parser.add_argument("-o", "--output", help="Output WAV file path")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Voice name")
    parser.add_argument("--lang", default=DEFAULT_LANGUAGE, help="Language code")
    parser.add_argument("--rate", type=int, default=DEFAULT_SAMPLE_RATE_HZ, help="Sample rate")
    parser.add_argument("--silence", type=float, default=LEADING_SILENCE_SECONDS, help="Leading silence")
    parser.add_argument("--reverb", action="store_true", help="Enable reverb")
    parser.add_argument("--reverb-mix", type=float, default=0.32)
    parser.add_argument("--reverb-decay", type=float, default=0.55)
    parser.add_argument("--reverb-delay-ms", type=int, default=70)
    parser.add_argument("--last-word-echo", action="store_true", help="Enable last-word echo")
    parser.add_argument("--echo-target-word", default="", help="Echo anchor word")
    parser.add_argument("--echo-mix", type=float, default=0.52)
    parser.add_argument("--echo-decay", type=float, default=0.78)
    parser.add_argument("--echo-delay-ms", type=int, default=170)
    parser.add_argument("--play", action="store_true", help="Play WAV after render")
    args = parser.parse_args()

    output = run_tts(
        text=args.text,
        output_path=Path(args.output) if args.output else None,
        voice=args.voice,
        lang=args.lang,
        rate=args.rate,
        silence=args.silence,
        reverb=args.reverb,
        reverb_mix=args.reverb_mix,
        reverb_decay=args.reverb_decay,
        reverb_delay_ms=args.reverb_delay_ms,
        last_word_echo=args.last_word_echo,
        echo_target_word=args.echo_target_word,
        echo_mix=args.echo_mix,
        echo_decay=args.echo_decay,
        echo_delay_ms=args.echo_delay_ms,
        play=args.play,
    )
    print(f"Wrote synthesized speech to {output}")


if __name__ == "__main__":
    main()
