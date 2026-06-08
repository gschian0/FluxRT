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

pkill -f 'scripts/run_gradio_demo.py' || true

export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

APP_PORT="${APP_PORT:-7862}"
APP_HOST="${APP_HOST:-0.0.0.0}"

nohup python scripts/run_gradio_demo.py --int8 --server-port "$APP_PORT" --server-name "$APP_HOST" > /tmp/fluxrt-gradio.log 2>&1 &
echo "STARTED_PID=$!"

for _ in $(seq 1 60); do
  if curl -sSf -o /dev/null "http://127.0.0.1:${APP_PORT}"; then
    echo "Gradio is up: http://127.0.0.1:${APP_PORT}"
    exit 0
  fi
  sleep 1
done

echo "Gradio did not become healthy within 60s."
tail -n 120 /tmp/fluxrt-gradio.log || true
exit 2
