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
  RADIO_URL="http://stream.zeno.fm/0a4yq1u0f0hvv"
fi

source .venv/bin/activate

if command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run python)
else
  PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
  if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3)"
  fi
  PYTHON_CMD=("$PYTHON_BIN")
fi

LOG_FILE="${MUSICGEN_LOG_FILE:-/tmp/fluxrt-musicgen-radio.log}"
PID_FILE="${MUSICGEN_PID_FILE:-/tmp/fluxrt-musicgen-radio.pid}"
OUTPUT_DIR="${MUSICGEN_OUTPUT_DIR:-$REPO_ROOT/musicgen_output_bass_chords_pads}"
MODEL_ID="${MUSICGEN_MODEL:-facebook/musicgen-small}"
SAMPLE_SECONDS="${MUSICGEN_SAMPLE_SECONDS:-8}"
GEN_SECONDS="${MUSICGEN_GEN_SECONDS:-8}"
TOP_K="${MUSICGEN_TOP_K:-110}"
TEMPERATURE="${MUSICGEN_TEMPERATURE:-0.78}"
TOP_P="${MUSICGEN_TOP_P:-0.82}"
GUIDANCE_SCALE="${MUSICGEN_GUIDANCE_SCALE:-3.4}"
DRUNK_WALK="${MUSICGEN_DRUNK_WALK:-0}"
DRUNK_WALK_STRENGTH="${MUSICGEN_DRUNK_WALK_STRENGTH:-0.0}"
PARALLEL_CLIPS="${MUSICGEN_PARALLEL_CLIPS:-3}"
SEED="${MUSICGEN_SEED:-424242}"
PAUSE_SECONDS="${MUSICGEN_PAUSE_SECONDS:-0}"
BASE_PROMPT="${MUSICGEN_BASE_PROMPT:-cool groove, electronic chill downtempo, bassline and chords lead the track, fat reggae dub sub bassline, cool jazz chord progression, lush synth pads, airy ethereal melodies throughout, warm chord stabs, light understated drums, steady instrumental club lounge mix}"
STREAM_DELAY_SECONDS="${MUSICGEN_STREAM_DELAY_SECONDS:-45}"
AUDIO_UDP_URL="${MUSICGEN_AUDIO_UDP_URL:-udp://127.0.0.1:5002?pkt_size=1316}"
CROSSFADE_SECONDS="${MUSICGEN_CROSSFADE_SECONDS:-1.2}"
BOOTSTRAP_CLIPS="${MUSICGEN_BOOTSTRAP_CLIPS:-64}"
CONDITIONING_MODE="${MUSICGEN_CONDITIONING_MODE:-text}"
CONDITIONING_SECONDS="${MUSICGEN_CONDITIONING_SECONDS:-8}"

DRUNK_WALK_ARGS=(--drunk-walk)
case "${DRUNK_WALK,,}" in
  0|false|no|off)
    DRUNK_WALK_ARGS=(--no-drunk-walk)
    ;;
esac

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    pkill -TERM -P "$OLD_PID" 2>/dev/null || true
    kill -TERM "$OLD_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

pkill -f 'scripts/[r]un_musicgen_radio_plus_musicGEN[.]py' || true
pkill -f '[f]fmpeg .* -f f32le .*udp://127[.]0[.]0[.]1:5002' || true

nohup "${PYTHON_CMD[@]}" -u scripts/run_musicgen_radio_plus_musicGEN.py \
  --radio-url "$RADIO_URL" \
  --output-dir "$OUTPUT_DIR" \
  --model "$MODEL_ID" \
  --sample-seconds "$SAMPLE_SECONDS" \
  --gen-seconds "$GEN_SECONDS" \
  --top-k "$TOP_K" \
  --top-p "$TOP_P" \
  --temperature "$TEMPERATURE" \
  --guidance-scale "$GUIDANCE_SCALE" \
  "${DRUNK_WALK_ARGS[@]}" \
  --drunk-walk-strength "$DRUNK_WALK_STRENGTH" \
  --parallel-clips "$PARALLEL_CLIPS" \
  --seed "$SEED" \
  --bootstrap-clips "$BOOTSTRAP_CLIPS" \
  --pause-seconds "$PAUSE_SECONDS" \
  --base-prompt "$BASE_PROMPT" \
  --conditioning-mode "$CONDITIONING_MODE" \
  --conditioning-seconds "$CONDITIONING_SECONDS" \
  --stream-delay-seconds "$STREAM_DELAY_SECONDS" \
  --audio-udp-url "$AUDIO_UDP_URL" \
  --crossfade-seconds "$CROSSFADE_SECONDS" \
  > "$LOG_FILE" 2>&1 &
echo "$!" > "$PID_FILE"

echo "MUSICGEN_RADIO_PID=$!"
echo "PID file: $PID_FILE"
echo "Log: $LOG_FILE"
echo "Output: $OUTPUT_DIR"
