#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to create the isolated AudioGen environment."
  exit 1
fi

AUDIOGEN_SFX_PYTHON_VERSION="${AUDIOGEN_SFX_PYTHON_VERSION:-3.10}"

uv python install "$AUDIOGEN_SFX_PYTHON_VERSION"
uv venv --seed --clear --python "$AUDIOGEN_SFX_PYTHON_VERSION" .venv-audiogen
.venv-audiogen/bin/python -m pip install --upgrade pip
uv pip install \
  --python .venv-audiogen/bin/python \
  --no-cache \
  --extra-index-url https://download.pytorch.org/whl/cu128 \
  -r requirements_audiogen_sfx.txt

echo "AudioGen SFX environment ready: $REPO_ROOT/.venv-audiogen"
echo "Python: $(.venv-audiogen/bin/python --version)"