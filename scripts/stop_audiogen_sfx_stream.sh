#!/usr/bin/env bash
set -euo pipefail

PID_FILE="${AUDIOGEN_SFX_PID_FILE:-/tmp/fluxrt-audiogen-sfx.pid}"

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    pkill -TERM -P "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
    sleep 0.5
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

pkill -f 'scripts/[r]un_audiogen_sfx_stream[.]py' 2>/dev/null || true
pkill -f '[f]fmpeg .*udp://127[.]0[.]0[.]1:5008' 2>/dev/null || true

echo "Stopped AudioGen SFX generator (if running)."