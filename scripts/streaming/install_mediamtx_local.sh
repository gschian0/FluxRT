#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TOOLS_DIR="$REPO_ROOT/tools/mediamtx"

MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-1.8.4}"
ARCH="$(uname -m)"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"

case "$ARCH" in
  x86_64|amd64)
    ARCH_TAG="amd64"
    ;;
  aarch64|arm64)
    ARCH_TAG="arm64"
    ;;
  *)
    echo "Unsupported architecture: $ARCH"
    exit 1
    ;;
esac

if [[ "$OS" != "linux" ]]; then
  echo "Unsupported OS: $OS"
  exit 1
fi

mkdir -p "$TOOLS_DIR"
TARBALL="$TOOLS_DIR/mediamtx_v${MEDIAMTX_VERSION}_${OS}_${ARCH_TAG}.tar.gz"
URL="https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_${OS}_${ARCH_TAG}.tar.gz"

echo "Downloading MediaMTX v${MEDIAMTX_VERSION} (${OS}_${ARCH_TAG})..."
curl -fL "$URL" -o "$TARBALL"

tmp_extract="$(mktemp -d)"
tar -xzf "$TARBALL" -C "$tmp_extract"

install -m 755 "$tmp_extract/mediamtx" "$TOOLS_DIR/mediamtx"
if [[ -f "$tmp_extract/mediamtx.yml" ]]; then
  install -m 644 "$tmp_extract/mediamtx.yml" "$TOOLS_DIR/mediamtx.default.yml"
fi

rm -rf "$tmp_extract"

echo "Installed: $TOOLS_DIR/mediamtx"
"$TOOLS_DIR/mediamtx" --version || true
