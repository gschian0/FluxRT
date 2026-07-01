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

PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

clear_boot_state() {
  echo "[boot-clear] stopping stale stream demo processes"
  pkill -f 'scripts/run_gradio_stream_demo.py' || true

  echo "[boot-clear] stopping orphaned multiprocessing children"
  pkill -f '/home/gschi/FluxRT/.venv/bin/python -c from multiprocessing.spawn import spawn_main' || true

  echo "[boot-clear] stopping stale inference/scheduler workers"
  pkill -f 'model_inference_subprocess' || true
  pkill -f 'output_scheduler_subprocess' || true

  echo "[boot-clear] stopping stale virtual-cam writer on udp:5000"
  # Target only rawvideo writer, not fanout reader.
  pkill -f 'ffmpeg.*-f rawvideo.*udp://127.0.0.1:5000' || true

  sleep 2

  if command -v nvidia-smi >/dev/null 2>&1; then
    echo "[boot-clear] waiting for GPU compute workers to drain"
    for _ in $(seq 1 30); do
      gpu_procs="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | sed '/^\s*$/d' || true)"
      if [[ -z "$gpu_procs" ]]; then
        echo "[boot-clear] GPU is clear"
        break
      fi
      sleep 1
    done
  fi
}

clear_boot_state

export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
APP_PORT="${APP_PORT:-7861}"
APP_HOST="${APP_HOST:-0.0.0.0}"
STREAM_CONFIG_PATH="${STREAM_CONFIG_PATH:-configs/stream_demo_config.json}"

nohup "$PYTHON_BIN" -u scripts/run_gradio_stream_demo.py --int8 --server-port "$APP_PORT" --server-name "$APP_HOST" --config-path "$STREAM_CONFIG_PATH" > /tmp/fluxrt-gradio-stream.log 2>&1 &
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
