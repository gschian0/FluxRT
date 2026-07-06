#!/usr/bin/env bash
set -euo pipefail

# Stall-aware watchdog for MediaMTX ingest/egress and legacy RTMP fanout.
# Usage: scripts/streaming/start_mediamtx_fanout_watchdog.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

WATCHDOG_PID_FILE="${WATCHDOG_PID_FILE:-/tmp/fluxrt-mediamtx-fanout-watchdog.pid}"
WATCHDOG_LOG="${WATCHDOG_LOG:-/tmp/fluxrt-watchdog.log}"
CHECK_INTERVAL="${CHECK_INTERVAL:-10}"
STALL_THRESHOLD="${STALL_THRESHOLD:-25}"
MIN_RESTART_INTERVAL="${MIN_RESTART_INTERVAL:-60}"
FANOUT_MODE="${FANOUT_MODE:-auto}"

if [[ -f "$WATCHDOG_PID_FILE" ]]; then
  existing_pid="$(cat "$WATCHDOG_PID_FILE" 2>/dev/null || true)"
  if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "Stack watchdog already running with PID $existing_pid"
    exit 0
  fi
fi

export REPO_ROOT CHECK_INTERVAL STALL_THRESHOLD MIN_RESTART_INTERVAL FANOUT_MODE WATCHDOG_LOG
nohup bash "$REPO_ROOT/scripts/streaming/fluxrt_stack_watchdog.sh" >> "$WATCHDOG_LOG" 2>&1 &
echo "$!" > "$WATCHDOG_PID_FILE"

echo "MediaMTX stack watchdog started. PID: $(cat "$WATCHDOG_PID_FILE")"
echo "Log: $WATCHDOG_LOG"
