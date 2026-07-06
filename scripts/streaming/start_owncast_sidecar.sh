#!/usr/bin/env bash
set -euo pipefail

# Optional Owncast sidecar for free HLS preview + self-hosted mirror.
# Fanout tees RTMP here when ENABLE_OWNCAST=1 (see start_rtmp_fanout.sh).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWNCAST_DIR="${OWNCAST_DIR:-/workspace/owncast}"
OWNCAST_VERSION="${OWNCAST_VERSION:-0.2.3}"
OWNCAST_HTTP_PORT="${OWNCAST_HTTP_PORT:-8080}"
OWNCAST_RTMP_PORT="${OWNCAST_RTMP_PORT:-1935}"
OWNCAST_LOG="${OWNCAST_LOG:-/tmp/fluxrt-owncast.log}"
OWNCAST_PID="${OWNCAST_PID:-/tmp/fluxrt-owncast.pid}"
OWNCAST_DATA="${OWNCAST_DATA:-$OWNCAST_DIR/data}"

if [[ -f "$OWNCAST_PID" ]] && kill -0 "$(cat "$OWNCAST_PID")" 2>/dev/null; then
  echo "Owncast already running (PID $(cat "$OWNCAST_PID"))."
  echo "Web UI: http://127.0.0.1:${OWNCAST_HTTP_PORT}"
  echo "RTMP ingest: rtmp://127.0.0.1:${OWNCAST_RTMP_PORT}/live/owncast"
  exit 0
fi

mkdir -p "$OWNCAST_DIR" "$OWNCAST_DATA"

if [[ ! -x "$OWNCAST_DIR/owncast" ]]; then
  echo "Downloading Owncast ${OWNCAST_VERSION}..."
  arch="$(uname -m)"
  case "$arch" in
    x86_64) owncast_arch="linux-64bit" ;;
    aarch64|arm64) owncast_arch="linux-arm64" ;;
    *)
      echo "Unsupported arch for Owncast auto-install: $arch"
      echo "Install manually from https://owncast.online and set OWNCAST_DIR."
      exit 1
      ;;
  esac
  tarball="owncast-${OWNCAST_VERSION}-${owncast_arch}.tar.gz"
  url="https://github.com/owncast/owncast/releases/download/v${OWNCAST_VERSION}/${tarball}"
  curl -fsSL "$url" -o "/tmp/${tarball}"
  tar -xzf "/tmp/${tarball}" -C "$OWNCAST_DIR"
  rm -f "/tmp/${tarball}"
fi

if [[ ! -x "$OWNCAST_DIR/owncast" ]]; then
  echo "Owncast binary not found at $OWNCAST_DIR/owncast"
  exit 1
fi

# Minimal config: RTMP on 1935, web on 8080. Admin sets stream key in UI on first boot.
export OWNCAST_HTTP_PORT OWNCAST_RTMP_PORT

cd "$OWNCAST_DIR"
nohup ./owncast -streamkey owncast >> "$OWNCAST_LOG" 2>&1 &
echo "$!" > "$OWNCAST_PID"

sleep 3
if kill -0 "$(cat "$OWNCAST_PID")" 2>/dev/null; then
  echo "Owncast started."
  echo "PID: $(cat "$OWNCAST_PID")"
  echo "Web UI: http://127.0.0.1:${OWNCAST_HTTP_PORT}"
  echo "RTMP ingest: rtmp://127.0.0.1:${OWNCAST_RTMP_PORT}/live/owncast"
  echo "Set ENABLE_OWNCAST=1 and OWNCAST_RTMP_URL in fanout env."
  echo "Log: $OWNCAST_LOG"
  exit 0
fi

echo "Owncast failed to start. Check log: $OWNCAST_LOG"
exit 1
