#!/usr/bin/env bash
set -euo pipefail

cd /workspace

if [[ ! -f .venv/bin/activate ]]; then
  echo "Missing /workspace/.venv. Run scripts/bootstrap_uv_env.sh first."
  exit 1
fi

source .venv/bin/activate

pkill -f 'scripts/run_gradio_demo.py' || true

export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

nohup python scripts/run_gradio_demo.py --int8 > /tmp/fluxrt-gradio.log 2>&1 &
echo "STARTED_PID=$!"

for _ in $(seq 1 60); do
  if curl -sSf -o /dev/null http://127.0.0.1:7860; then
    echo "Gradio is up: http://127.0.0.1:7860"
    exit 0
  fi
  sleep 1
done

echo "Gradio did not become healthy within 60s."
tail -n 120 /tmp/fluxrt-gradio.log || true
exit 2
