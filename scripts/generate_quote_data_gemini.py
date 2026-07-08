#!/usr/bin/env python3
"""Generate philosopher-style quotes using Google Gemini 3.5 Flash REST API.

Uses the Gemini REST endpoint directly (no SDK needed — just `requests`).
Generates ALL quotes in a SINGLE batch request — ask Gemini for N quotes
at once with structured JSON output, then parse the response.

Usage:
    python scripts/generate_quote_data_gemini.py --count 50
    python scripts/generate_quote_data_gemini.py --count 100 --output data/quotes/gemini_quotes.json
"""
import argparse
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.5-flash"

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
        repo_root.parent / "AI_TV_ORCHESTRATION" / ".env",
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


def build_batch_prompt(count: int) -> str:
    """Build a single prompt asking for N quotes as a JSON array."""
    # Pre-assign random philosopher/topic/form/mood combos so we can tag them
    assignments = []
    for i in range(count):
        philosopher = random.choice(PHILOSOPHERS)
        topic = random.choice(TOPICS)
        form = random.choice(FORMS)
        mood = random.choice(MOODS)
        assignments.append({
            "philosopher": philosopher,
            "topic": topic,
            "form": form,
            "mood": mood,
        })

    # Build the instruction with the assignments embedded
    lines = []
    lines.append(
        f"Generate exactly {count} short original philosophical quotes. "
        "Each quote should be under 24 words and sound like a real aphorism — "
        "no intro, no numbering, no quotation marks."
    )
    lines.append("")
    lines.append("For each quote, use the following tone/topic/form/mood assignments:")
    lines.append("")
    for i, a in enumerate(assignments):
        lines.append(
            f"Quote {i + 1}: philosopher={a['philosopher']}, "
            f"topic={a['topic']}, form={a['form']}, mood={a['mood']}"
        )
    lines.append("")
    lines.append(
        "Respond with ONLY a valid JSON array. No markdown, no code fences, no explanation. "
        "Each element must be an object with exactly these fields:\n"
        '  {"quote": "the quote text", "philosopher": "name", "topic": "topic", '
        '"form": "form", "mood": "mood"}'
    )
    lines.append("")
    lines.append(f"Return exactly {count} objects in the array.")

    return "\n".join(lines), assignments


def extract_json_from_response(text: str) -> list:
    """Extract a JSON array from the model response, handling markdown fences."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        # Remove first line (```json or ```)
        lines = text.split("\n")
        lines = lines[1:]  # skip opening fence
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]  # skip closing fence
        text = "\n".join(lines).strip()
    # Find the JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"Could not find JSON array in response: {text[:200]}...")
    json_str = text[start:end + 1]
    return json.loads(json_str)


def generate_batch_quotes(
    api_key: str,
    model: str,
    count: int,
    timeout: int,
    max_retries: int = 3,
) -> list:
    """Generate N quotes in a single API call using structured output."""
    prompt, assignments = build_batch_prompt(count)

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

    url = GEMINI_URL.format(model=model)
    params = {"key": api_key}

    last_exc = None
    for attempt in range(max_retries):
        try:
            response = requests.post(url, params=params, json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            items = extract_json_from_response(raw)

            results = []
            for i, item in enumerate(items):
                quote_text = extract_quote(item.get("quote", ""))
                if not quote_text:
                    continue
                # Use the assignment data for metadata (more reliable than model's tags)
                a = assignments[i] if i < len(assignments) else {}
                results.append({
                    "quote": quote_text,
                    "philosopher": item.get("philosopher", a.get("philosopher", "Unknown")),
                    "topic": item.get("topic", a.get("topic", "unknown")),
                    "form": item.get("form", a.get("form", "unknown")),
                    "mood": item.get("mood", a.get("mood", "unknown")),
                    "model": model,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                })
            return results
        except Exception as exc:
            last_exc = exc
            wait = 2 ** attempt
            print(f"  [retry {attempt + 1}/{max_retries}] {type(exc).__name__}: {exc} — waiting {wait}s", flush=True)
            time.sleep(wait)

    raise RuntimeError(f"Failed after {max_retries} retries: {last_exc}")


def extract_quote(raw_text: str) -> str:
    text = " ".join(raw_text.strip().split())
    text = text.strip('"\'')
    text = re.sub(r"^[-*\d\.)\s]+", "", text)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate quote dataset with Gemini 3.5 Flash (batch mode)")
    parser.add_argument("--count", type=int, default=20, help="Number of quotes to generate")
    parser.add_argument(
        "--output",
        default="data/quotes/gemini_quotes.json",
        help="Output JSON path",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model name")
    parser.add_argument("--timeout", type=int, default=30, help="Request timeout seconds")
    parser.add_argument("--append", action="store_true", help="Append to existing file instead of overwriting")
    args = parser.parse_args()

    load_local_env_files()

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is not set. Add it to .env or export it.")

    count = max(1, args.count)
    results = []

    # Load existing quotes if appending
    output_path = Path(args.output)
    if args.append and output_path.is_file():
        try:
            results = json.loads(output_path.read_text(encoding="utf-8"))
            print(f"Loaded {len(results)} existing quotes from {output_path}")
        except Exception:
            print(f"Warning: could not parse existing file, starting fresh")

    # Generate in small batches (10 at a time — fast and reliable)
    BATCH_SIZE = 10
    remaining = count
    batch_num = 0
    while remaining > 0:
        batch_count = min(remaining, BATCH_SIZE)
        batch_num += 1
        print(f"\n=== Batch {batch_num}: requesting {batch_count} quotes in a single API call ===", flush=True)
        batch_results = generate_batch_quotes(
            api_key=api_key,
            model=args.model,
            count=batch_count,
            timeout=args.timeout,
        )
        print(f"=== Batch {batch_num}: got {len(batch_results)} quotes ===\n", flush=True)
        for i, item in enumerate(batch_results):
            print(f"  [{len(results) + 1}] {item['quote']} — {item['philosopher']}", flush=True)
        results.extend(batch_results)
        remaining -= batch_count
        if remaining > 0:
            time.sleep(1)  # Brief pause between batches

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {len(results)} quotes to {output_path}")


if __name__ == "__main__":
    main()
