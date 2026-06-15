#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f .venv/bin/activate ]]; then
  echo "Missing $REPO_ROOT/.venv. Run scripts/bootstrap_uv_env.sh first."
  exit 1
fi

if [[ -z "${RADIO_URL:-}" ]]; then
  echo "RADIO_URL is required."
  echo "Example: RADIO_URL='https://example.com/live.m3u8' scripts/start_musicgen_radio_plus_musicGEN.sh"
  exit 1
fi

source .venv/bin/activate

LOG_FILE="${MUSICGEN_LOG_FILE:-/tmp/fluxrt-musicgen-radio.log}"
OUTPUT_DIR="${MUSICGEN_OUTPUT_DIR:-$REPO_ROOT/musicgen_output_plus_musicGEN}"
MODEL_ID="${MUSICGEN_MODEL:-facebook/musicgen-small}"
SAMPLE_SECONDS="${MUSICGEN_SAMPLE_SECONDS:-6}"
GEN_SECONDS="${MUSICGEN_GEN_SECONDS:-6}"
PAUSE_SECONDS="${MUSICGEN_PAUSE_SECONDS:-0}"
BASE_PROMPT="${MUSICGEN_BASE_PROMPT:-experimental electronic sound art for an internet installation}"
STREAM_DELAY_SECONDS="${MUSICGEN_STREAM_DELAY_SECONDS:-16}"
AUDIO_UDP_URL="${MUSICGEN_AUDIO_UDP_URL:-udp://127.0.0.1:5002?pkt_size=1316}"
CROSSFADE_SECONDS="${MUSICGEN_CROSSFADE_SECONDS:-1.5}"

pkill -f 'scripts/run_musicgen_radio_plus_musicGEN.py' || true

nohup python -u scripts/run_musicgen_radio_plus_musicGEN.py \
  --radio-url "$RADIO_URL" \
  --output-dir "$OUTPUT_DIR" \
  --model "$MODEL_ID" \
  --sample-seconds "$SAMPLE_SECONDS" \
  --gen-seconds "$GEN_SECONDS" \
  --pause-seconds "$PAUSE_SECONDS" \
  --base-prompt "$BASE_PROMPT" \
  --stream-delay-seconds "$STREAM_DELAY_SECONDS" \
  --audio-udp-url "$AUDIO_UDP_URL" \
  --crossfade-seconds "$CROSSFADE_SECONDS" \
  > "$LOG_FILE" 2>&1 &

echo "MUSICGEN_RADIO_PID=$!"
echo "Log: $LOG_FILE"
echo "Output: $OUTPUT_DIR"
