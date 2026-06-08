#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -f .venv/bin/activate ]]; then
  echo "Missing $REPO_ROOT/.venv. Run scripts/bootstrap_uv_env.sh first."
  exit 1
fi

source .venv/bin/activate

pkill -f 'scripts/run_gradio_stream_demo.py' || true
# Clear orphaned multiprocessing children from previous crashed runs.
pkill -f '/home/gschi/FluxRT/.venv/bin/python -c from multiprocessing.spawn import spawn_main' || true

export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
APP_PORT="${APP_PORT:-7861}"
APP_HOST="${APP_HOST:-0.0.0.0}"
STREAM_CONFIG_PATH="${STREAM_CONFIG_PATH:-configs/stream_demo_config.json}"

nohup python -u scripts/run_gradio_stream_demo.py --int8 --server-port "$APP_PORT" --server-name "$APP_HOST" --config-path "$STREAM_CONFIG_PATH" > /tmp/fluxrt-gradio-stream.log 2>&1 &
echo "STREAM_DEMO_PID=$!"

for _ in $(seq 1 180); do
  if curl -sSf -o /dev/null "http://127.0.0.1:${APP_PORT}"; then
    echo "Stream demo is up: http://127.0.0.1:${APP_PORT}"
    exit 0
  fi
  sleep 1
done

echo "Stream demo did not become healthy within 180s."
tail -n 120 /tmp/fluxrt-gradio-stream.log || true
exit 2
