#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  python3 -m pip install --user -U uv
  export PATH="$HOME/.local/bin:$PATH"
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"

uv venv --python "$PYTHON_BIN" --clear
source .venv/bin/activate

export UV_LINK_MODE=copy

uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
uv pip install -r requirements.txt
uv pip install -r requirements_lipsync.txt
uv pip install -e .

python - <<'PY'
import cv2, gradio, scipy, torch
print("cv2", cv2.__version__)
print("gradio", gradio.__version__)
print("scipy", scipy.__version__)
print("torch", torch.__version__)
PY

echo "Bootstrap complete."
