#!/usr/bin/env bash
set -euo pipefail

# Stop RTMP fanout started by start_rtmp_fanout.sh

PID_FILE="${1:-/tmp/fluxrt-rtmp-fanout.pid}"

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
  echo "Stopped RTMP fanout PID $PID"
else
  echo "RTMP fanout process not running (PID was $PID)."
fi

rm -f "$PID_FILE"
