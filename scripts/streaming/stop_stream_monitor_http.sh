#!/usr/bin/env bash
set -euo pipefail

MONITOR_PID_FILE="${MONITOR_PID_FILE:-/tmp/fluxrt-monitor-http.pid}"

if [[ -f "$MONITOR_PID_FILE" ]]; then
  pid="$(cat "$MONITOR_PID_FILE" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 0.5
    kill -9 "$pid" 2>/dev/null || true
    echo "Stopped monitor HTTP PID $pid"
  else
    echo "Monitor HTTP process not running."
  fi
  rm -f "$MONITOR_PID_FILE"
else
  echo "No monitor HTTP PID file found."
fi