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

export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

nohup python scripts/run_gradio_stream_demo.py --int8 --server-port 7861 > /tmp/fluxrt-gradio-stream.log 2>&1 &
echo "STREAM_DEMO_PID=$!"

for _ in $(seq 1 60); do
  if curl -sSf -o /dev/null http://127.0.0.1:7861; then
    echo "Stream demo is up: http://127.0.0.1:7861"
    exit 0
  fi
  sleep 1
done

echo "Stream demo did not become healthy within 60s."
tail -n 120 /tmp/fluxrt-gradio-stream.log || true
exit 2
