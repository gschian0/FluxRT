#!/usr/bin/env bash
# Smoke test: SMPTE bars + music + quotes → Twitch for 2 minutes.
set -euo pipefail

REPO_ROOT="/workspace/FluxRT"
cd "$REPO_ROOT"

DURATION_SECS="${DURATION_SECS:-120}"
LOG_FILE="${LOG_FILE:-/tmp/fluxrt-smoke-bars.log}"

echo "[smoke-bars] $(date -Is) starting ${DURATION_SECS}s bars regression" | tee -a "$LOG_FILE"

bash scripts/streaming/fluxrt-bars-audio-test.sh >> "$LOG_FILE" 2>&1

sleep 10
if ! pgrep -f "ffmpeg.*smptebars=size=" >/dev/null; then
  echo "[smoke-bars] FAIL: bars ffmpeg not running after 10s" | tee -a "$LOG_FILE"
  tail -30 "$LOG_FILE"
  exit 1
fi

echo "[smoke-bars] $(date -Is) running soak for ${DURATION_SECS}s" | tee -a "$LOG_FILE"
end=$((SECONDS + DURATION_SECS))
stalls=0
last_frame=""

while [[ $SECONDS -lt $end ]]; do
  if ! pgrep -f "ffmpeg.*smptebars=size=" >/dev/null; then
    echo "[smoke-bars] FAIL: bars ffmpeg died during soak" | tee -a "$LOG_FILE"
    tail -30 /tmp/fluxrt-bars-audio-test.log 2>/dev/null || true
    exit 1
  fi
  sleep 15
done

bash scripts/streaming/fluxrt-stop-bars-audio-test.sh >> "$LOG_FILE" 2>&1 || true
echo "[smoke-bars] PASS: completed ${DURATION_SECS}s bars+audio soak" | tee -a "$LOG_FILE"
