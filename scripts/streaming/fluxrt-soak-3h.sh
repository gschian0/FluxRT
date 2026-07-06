#!/usr/bin/env bash
# Long soak test with periodic health snapshots (default 3 hours).
set -euo pipefail

REPO_ROOT="/workspace/FluxRT"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-/tmp/soak}"
DURATION_SECS="${DURATION_SECS:-10800}"
INTERVAL_SECS="${INTERVAL_SECS:-300}"
LOG_FILE="${LOG_FILE:-/tmp/fluxrt-soak-3h.log}"

mkdir -p "$SNAPSHOT_DIR"

log() {
  echo "[soak] $(date -Is) $*" | tee -a "$LOG_FILE"
}

snapshot() {
  local tag="$1"
  local out="${SNAPSHOT_DIR}/snapshot-${tag}.txt"
  {
    echo "=== snapshot ${tag} $(date -Is) ==="
    bash /workspace/check_stack.sh 2>/dev/null || true
    echo ""
    echo "=== ingest progress ==="
    tail -5 /tmp/fluxrt-mediamtx-ingest.progress 2>/dev/null || echo "(none)"
    echo ""
    echo "=== egress progress ==="
    tail -5 /tmp/fluxrt-mediamtx-egress.progress 2>/dev/null || echo "(none)"
    echo ""
    echo "=== ingest log tail ==="
    tail -10 /tmp/fluxrt-mediamtx-ingest.log 2>/dev/null || echo "(none)"
    echo ""
    echo "=== watchdog log tail ==="
    tail -10 /tmp/fluxrt-watchdog.log 2>/dev/null || echo "(none)"
  } > "$out"
  log "wrote $out"
}

log "starting ${DURATION_SECS}s soak (snapshots every ${INTERVAL_SECS}s) -> $SNAPSHOT_DIR"
end=$((SECONDS + DURATION_SECS))
idx=0

while [[ $SECONDS -lt $end ]]; do
  snapshot "$(printf '%04d' "$idx")"
  idx=$((idx + 1))
  sleep "$INTERVAL_SECS"
done

snapshot "final"
log "PASS: soak completed (${DURATION_SECS}s)"
