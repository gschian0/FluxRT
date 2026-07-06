#!/usr/bin/env bash
set -euo pipefail

OWNCAST_PID="${OWNCAST_PID:-/tmp/fluxrt-owncast.pid}"

if [[ -f "$OWNCAST_PID" ]]; then
  pid="$(cat "$OWNCAST_PID" 2>/dev/null || true)"
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
    sleep 0.5
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$OWNCAST_PID"
fi

pkill -f '/workspace/owncast/owncast' 2>/dev/null || true
echo "Stopped Owncast sidecar (if running)."
