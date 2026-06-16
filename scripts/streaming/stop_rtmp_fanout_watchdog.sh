#!/usr/bin/env bash
set -euo pipefail

WATCHDOG_PID_FILE="${1:-/tmp/fluxrt-rtmp-fanout-watchdog.pid}"

if [[ ! -f "$WATCHDOG_PID_FILE" ]]; then
  echo "No watchdog PID file found: $WATCHDOG_PID_FILE"
  exit 0
fi

PID="$(cat "$WATCHDOG_PID_FILE")"
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  kill -TERM "$PID" || true
  echo "Stopped fanout watchdog PID $PID"
else
  echo "Fanout watchdog process not running (PID was $PID)."
fi

rm -f "$WATCHDOG_PID_FILE"
