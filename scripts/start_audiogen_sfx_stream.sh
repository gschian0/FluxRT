#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "${AUDIOGEN_SFX_PYTHON_CMD:-}" ]]; then
  read -r -a PYTHON_CMD <<< "$AUDIOGEN_SFX_PYTHON_CMD"
elif [[ -x "$REPO_ROOT/.venv-audiogen/bin/python" ]]; then
  PYTHON_CMD=("$REPO_ROOT/.venv-audiogen/bin/python")
else
  if [[ ! -f .venv/bin/activate ]]; then
    echo "Missing $REPO_ROOT/.venv. Run scripts/bootstrap_uv_env.sh first."
    exit 1
  fi
  source .venv/bin/activate
  if command -v uv >/dev/null 2>&1; then
    PYTHON_CMD=(uv run python)
  else
    PYTHON_CMD=("$REPO_ROOT/.venv/bin/python")
  fi
fi

LOG_FILE="${AUDIOGEN_SFX_LOG_FILE:-/tmp/fluxrt-audiogen-sfx.log}"
PID_FILE="${AUDIOGEN_SFX_PID_FILE:-/tmp/fluxrt-audiogen-sfx.pid}"
OUTPUT_DIR="${AUDIOGEN_SFX_OUTPUT_DIR:-$REPO_ROOT/audiogen_sfx_output}"
MODEL_ID="${AUDIOGEN_SFX_MODEL:-facebook/audiogen-medium}"
DURATION="${AUDIOGEN_SFX_DURATION:-3}"
INTERVAL="${AUDIOGEN_SFX_INTERVAL:-35}"
VOLUME="${AUDIOGEN_SFX_VOLUME:-0.35}"
SEED="${AUDIOGEN_SFX_SEED:-4242}"
TOP_K="${AUDIOGEN_SFX_TOP_K:-250}"
TOP_P="${AUDIOGEN_SFX_TOP_P:-0.0}"
TEMPERATURE="${AUDIOGEN_SFX_TEMPERATURE:-1.0}"
GUIDANCE_SCALE="${AUDIOGEN_SFX_GUIDANCE_SCALE:-3.0}"
AUDIO_UDP_URL="${AUDIOGEN_SFX_AUDIO_UDP_URL:-udp://127.0.0.1:5008?pkt_size=1316}"
PROMPTS="${AUDIOGEN_SFX_PROMPTS:-subtle analog tape whoosh, short broadcast transition, clean and quiet|soft futuristic interface chirps, tiny electric sparkles, short and tasteful|distant synthetic thunder swell, low cinematic rumble, restrained}"
AUDIOGEN_SFX_CPU="${AUDIOGEN_SFX_CPU:-1}"

CPU_ARGS=()
case "${AUDIOGEN_SFX_CPU,,}" in
  1|true|yes|on)
    CPU_ARGS=(--cpu)
    ;;
esac

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    pkill -TERM -P "$old_pid" 2>/dev/null || true
    kill -TERM "$old_pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

pkill -f 'scripts/[r]un_audiogen_sfx_stream[.]py' 2>/dev/null || true
pkill -f '[f]fmpeg .*udp://127[.]0[.]0[.]1:5008' 2>/dev/null || true

mkdir -p "$OUTPUT_DIR"
nohup "${PYTHON_CMD[@]}" -u scripts/run_audiogen_sfx_stream.py \
  --prompts "$PROMPTS" \
  --output-dir "$OUTPUT_DIR" \
  --model "$MODEL_ID" \
  --duration "$DURATION" \
  --interval "$INTERVAL" \
  --volume "$VOLUME" \
  --seed "$SEED" \
  --top-k "$TOP_K" \
  --top-p "$TOP_P" \
  --temperature "$TEMPERATURE" \
  --guidance-scale "$GUIDANCE_SCALE" \
  --audio-udp-url "$AUDIO_UDP_URL" \
  "${CPU_ARGS[@]}" \
  > "$LOG_FILE" 2>&1 &
echo "$!" > "$PID_FILE"

for _ in {1..16}; do
  current_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ ! "$current_pid" =~ ^[0-9]+$ ]] || ! kill -0 "$current_pid" 2>/dev/null; then
    break
  fi
  if grep -q '^\[audiogen\] loading ' "$LOG_FILE" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

current_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ ! "$current_pid" =~ ^[0-9]+$ ]] || ! kill -0 "$current_pid" 2>/dev/null; then
  echo "AudioGen SFX failed during startup."
  echo "Log: $LOG_FILE"
  tail -n 40 "$LOG_FILE" 2>/dev/null || true
  rm -f "$PID_FILE"
  exit 1
fi

echo "AUDIOGEN_SFX_PID=$!"
echo "PID file: $PID_FILE"
echo "Log: $LOG_FILE"
echo "Output: $OUTPUT_DIR"
echo "UDP: $AUDIO_UDP_URL"
if [[ ${#CPU_ARGS[@]} -gt 0 ]]; then
  echo "Device: CPU"
else
  echo "Device: GPU"
fi