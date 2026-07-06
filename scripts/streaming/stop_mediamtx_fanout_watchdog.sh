#!/usr/bin/env bash
set -euo pipefail

WATCHDOG_PID_FILE="${1:-/tmp/fluxrt-mediamtx-fanout-watchdog.pid}"

if [[ ! -f "$WATCHDOG_PID_FILE" ]]; then
  echo "No watchdog PID file found: $WATCHDOG_PID_FILE"
  exit 0
fi

PID="$(cat "$WATCHDOG_PID_FILE" 2>/dev/null || true)"
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  kill "$PID" 2>/dev/null || true
  echo "Stopped stack watchdog PID $PID"
else
  echo "Stack watchdog process not running (PID was ${PID:-empty})."
fi

rm -f "$WATCHDOG_PID_FILE"
