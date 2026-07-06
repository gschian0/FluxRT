#!/usr/bin/env bash
# Smoke test: verify MediaMTX ingest/egress progress while stack is running.
set -euo pipefail

REPO_ROOT="/workspace/FluxRT"
cd "$REPO_ROOT"

DURATION_SECS="${DURATION_SECS:-300}"
STALL_THRESHOLD="${STALL_THRESHOLD:-30}"
LOG_FILE="${LOG_FILE:-/tmp/fluxrt-smoke-mediamtx.log}"

INGEST_PROGRESS="/tmp/fluxrt-mediamtx-ingest.progress"
EGRESS_PROGRESS="/tmp/fluxrt-mediamtx-egress.progress"

log() {
  echo "[smoke-mediamtx] $(date -Is) $*" | tee -a "$LOG_FILE"
}

progress_frame() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  grep -E '^frame=' "$file" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' '
}

wait_for_progress() {
  local label="$1"
  local file="$2"
  local timeout_secs="$3"
  local i=0
  while [[ "$i" -lt "$timeout_secs" ]]; do
    local frame
    frame="$(progress_frame "$file" || true)"
    if [[ -n "$frame" && "$frame" =~ ^[0-9]+$ ]]; then
      log "${label} progress frame=${frame}"
      return 0
    fi
    sleep 5
    i=$((i + 5))
  done
  log "FAIL: ${label} progress not seen within ${timeout_secs}s (${file})"
  return 1
}

log "starting MediaMTX smoke (${DURATION_SECS}s soak, stall=${STALL_THRESHOLD}s)"

if ! pgrep -f "tools/mediamtx/mediamtx" >/dev/null; then
  log "WARN: MediaMTX not running — start stack first or run start_mediamtx_fanout.sh"
fi

wait_for_progress "ingest" "$INGEST_PROGRESS" 180
wait_for_progress "egress" "$EGRESS_PROGRESS" 180

ingest_last=""
egress_last=""
ingest_change=$SECONDS
egress_change=$SECONDS
end=$((SECONDS + DURATION_SECS))

while [[ $SECONDS -lt $end ]]; do
  ingest_frame="$(progress_frame "$INGEST_PROGRESS" || true)"
  egress_frame="$(progress_frame "$EGRESS_PROGRESS" || true)"

  if [[ -n "$ingest_frame" && "$ingest_frame" =~ ^[0-9]+$ ]]; then
    if [[ "$ingest_frame" != "$ingest_last" ]]; then
      ingest_last="$ingest_frame"
      ingest_change=$SECONDS
    elif [[ $((SECONDS - ingest_change)) -ge "$STALL_THRESHOLD" ]]; then
      log "FAIL: ingest stalled at frame=${ingest_frame} for ${STALL_THRESHOLD}s"
      exit 1
    fi
  fi

  if [[ -n "$egress_frame" && "$egress_frame" =~ ^[0-9]+$ ]]; then
    if [[ "$egress_frame" != "$egress_last" ]]; then
      egress_last="$egress_frame"
      egress_change=$SECONDS
    elif [[ $((SECONDS - egress_change)) -ge "$STALL_THRESHOLD" ]]; then
      log "FAIL: egress stalled at frame=${egress_frame} for ${STALL_THRESHOLD}s"
      exit 1
    fi
  fi

  if ! pgrep -f "/tmp/fluxrt-mediamtx-ingest-loop.sh" >/dev/null; then
    log "FAIL: ingest loop not running"
    exit 1
  fi

  sleep 15
done

log "PASS: ingest frame=${ingest_last} egress frame=${egress_last} after ${DURATION_SECS}s"
