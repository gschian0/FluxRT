#!/usr/bin/env bash
set -euo pipefail

# Separate operation: starts an ffmpeg relay for NBC HLS without touching FluxRT app process.
# Output is a local UDP MPEG-TS endpoint that other tools can ingest.

SRC_URL="${1:-https://xumo-xumoent-vc-105-z0vpm.fast.nbcuni.com/live/master.m3u8}"
OUT_URL="${2:-udp://127.0.0.1:5000?pkt_size=1316}"
LOG_FILE="${3:-/tmp/fluxrt-nbc-relay.log}"
PID_FILE="${4:-/tmp/fluxrt-nbc-relay.pid}"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Relay already running with PID $(cat "$PID_FILE")."
  echo "Stop it first with scripts/streaming/stop_nbc_relay.sh"
  exit 0
fi

# Start relay detached. This does not modify the running FluxRT app.
nohup ffmpeg -hide_banner -loglevel info \
  -fflags nobuffer -flags low_delay -strict experimental \
  -i "$SRC_URL" \
  -an -c:v libx264 -preset veryfast -tune zerolatency \
  -f mpegts "$OUT_URL" \
  > "$LOG_FILE" 2>&1 &

RELAY_PID=$!
echo "$RELAY_PID" > "$PID_FILE"

sleep 2
if kill -0 "$RELAY_PID" 2>/dev/null; then
  echo "Relay started."
  echo "PID: $RELAY_PID"
  echo "Input:  $SRC_URL"
  echo "Output: $OUT_URL"
  echo "Log:    $LOG_FILE"
  exit 0
fi

echo "Relay failed to start. Check log: $LOG_FILE"
exit 1
