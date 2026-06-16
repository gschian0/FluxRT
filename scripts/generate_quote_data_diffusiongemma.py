import argparse
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

INVOKE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_MODEL = "google/diffusiongemma-26b-a4b-it"

PHILOSOPHERS = [
    "Socrates",
    "Plato",
    "Aristotle",
    "Epictetus",
    "Marcus Aurelius",
    "Seneca",
    "Confucius",
    "Laozi",
    "Nietzsche",
    "Kierkegaard",
    "Emerson",
]

TOPICS = [
    "patience",
    "time",
    "discipline",
    "attention",
    "change",
    "hope",
    "silence",
    "memory",
    "fear",
    "craft",
]

FORMS = [
    "paradox",
    "gentle warning",
    "hard-earned lesson",
    "koan-like reflection",
    "concrete metaphor",
    "calm imperative",
]

MOODS = [
    "stoic",
    "lucid",
    "tender",
    "austere",
    "curious",
    "serene",
]


def load_local_env_files() -> None:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    candidates = [
        repo_root / ".env",
        repo_root / "MagpieTTS" / ".env",
        repo_root.parent / "MagpieTTS" / ".env",
    ]

    def _load_env_path(path: Path) -> None:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return

        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

    for env_path in candidates:
        if env_path.is_file():
            _load_env_path(env_path)


def build_prompt(philosopher: str, topic: str, form: str, mood: str) -> str:
    return (
        "Write one short original quote only. "
        "No intro, no numbering, no quotation marks. "
        f"Tone influence: {philosopher}. "
        f"Topic: {topic}. Form: {form}. Mood: {mood}. "
        "Keep it under 24 words and make it sound like a real aphorism."
    )


def extract_quote(raw_text: str) -> str:
    text = " ".join(raw_text.strip().split())
    text = text.strip('"\'')
    text = re.sub(r"^[-*\d\.)\s]+", "", text)
    return text


def generate_one_quote(api_key: str, model: str, timeout: int) -> dict:
    philosopher = random.choice(PHILOSOPHERS)
    topic = random.choice(TOPICS)
    form = random.choice(FORMS)
    mood = random.choice(MOODS)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": build_prompt(philosopher, topic, form, mood)}],
        "max_tokens": 128,
        "temperature": 1.0,
        "top_p": 0.95,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    response = requests.post(INVOKE_URL, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    raw = data["choices"][0]["message"]["content"]
    quote = extract_quote(raw)

    return {
        "quote": quote,
        "philosopher": philosopher,
        "topic": topic,
        "form": form,
        "mood": mood,
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate quote dataset with Diffusion Gemma")
    parser.add_argument("--count", type=int, default=20, help="Number of quotes to generate")
    parser.add_argument(
        "--output",
        default="data/quotes/diffusiongemma_quotes.json",
        help="Output JSON path",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="NVIDIA model name")
    parser.add_argument("--timeout", type=int, default=60, help="Request timeout seconds")
    args = parser.parse_args()

    load_local_env_files()

    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("NVIDIA_API_KEY is not set")

    count = max(1, args.count)
    results = []
    for idx in range(count):
        item = generate_one_quote(api_key=api_key, model=args.model, timeout=args.timeout)
        results.append(item)
        print(f"[{idx + 1}/{count}] {item['quote']} — {item['philosopher']}")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote {len(results)} quotes to {output_path}")


if __name__ == "__main__":
    main()
