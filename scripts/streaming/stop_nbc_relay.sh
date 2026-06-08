#!/usr/bin/env bash
set -euo pipefail

# Separate operation: stops only the ffmpeg relay process.
PID_FILE="${1:-/tmp/fluxrt-nbc-relay.pid}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No PID file found: $PID_FILE"
  exit 0
fi

PID="$(cat "$PID_FILE")"
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  kill -TERM "$PID" || true
  sleep 1
  if kill -0 "$PID" 2>/dev/null; then
    kill -KILL "$PID" || true
  fi
  echo "Stopped relay PID $PID"
else
  echo "Relay process not running (PID was $PID)."
fi

rm -f "$PID_FILE"
