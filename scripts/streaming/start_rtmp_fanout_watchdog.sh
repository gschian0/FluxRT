#!/usr/bin/env bash
set -euo pipefail

# Auto-restart fanout if process exits unexpectedly.
# Usage: scripts/streaming/start_rtmp_fanout_watchdog.sh [env-file]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="${1:-scripts/streaming/rtmp_targets.env}"
WATCHDOG_PID_FILE="${WATCHDOG_PID_FILE:-/tmp/fluxrt-rtmp-fanout-watchdog.pid}"
WATCHDOG_LOG_FILE="${WATCHDOG_LOG_FILE:-/tmp/fluxrt-rtmp-fanout-watchdog.log}"
CHECK_INTERVAL="${CHECK_INTERVAL:-5}"

if [[ -f "$WATCHDOG_PID_FILE" ]] && kill -0 "$(cat "$WATCHDOG_PID_FILE")" 2>/dev/null; then
  echo "Fanout watchdog already running with PID $(cat "$WATCHDOG_PID_FILE")"
  exit 0
fi

nohup bash -lc '
  set -euo pipefail
  cd "'$REPO_ROOT'"
  while true; do
    if [[ ! -f /tmp/fluxrt-rtmp-fanout.pid ]] || ! kill -0 "$(cat /tmp/fluxrt-rtmp-fanout.pid 2>/dev/null)" 2>/dev/null; then
      echo "[$(date -Is)] fanout down -> restarting" >> "'$WATCHDOG_LOG_FILE'"
      ./scripts/streaming/start_rtmp_fanout.sh "'$ENV_FILE'" >> "'$WATCHDOG_LOG_FILE'" 2>&1 || true
    fi
    sleep "'$CHECK_INTERVAL'"
  done
' >> "$WATCHDOG_LOG_FILE" 2>&1 &

echo "$!" > "$WATCHDOG_PID_FILE"
echo "RTMP fanout watchdog started. PID: $!"
echo "Log: $WATCHDOG_LOG_FILE"
