#!/usr/bin/env bash
set -euo pipefail

# Capture frames from the live UDP video stream into a timestamped directory.
# Usage:
#   CAPTURE_EVERY_SECONDS=5 CAPTURE_DURATION_SECONDS=300 \
#   scripts/backup/capture_stream_frames.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

INPUT_URL="${INPUT_URL:-udp://127.0.0.1:5000?pkt_size=1316}"
CAPTURE_EVERY_SECONDS="${CAPTURE_EVERY_SECONDS:-5}"
CAPTURE_DURATION_SECONDS="${CAPTURE_DURATION_SECONDS:-120}"
SESSION_TAG="${SESSION_TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/backups/frames/$SESSION_TAG}"

mkdir -p "$OUT_DIR"

echo "Capturing frames to: $OUT_DIR"
echo "Input: $INPUT_URL"
echo "Every: ${CAPTURE_EVERY_SECONDS}s"
echo "Duration: ${CAPTURE_DURATION_SECONDS}s"

ffmpeg -hide_banner -loglevel warning \
  -fflags +genpts+discardcorrupt \
  -analyzeduration 2M -probesize 2M \
  -i "$INPUT_URL" \
  -vf "fps=1/${CAPTURE_EVERY_SECONDS}" \
  -t "$CAPTURE_DURATION_SECONDS" \
  -q:v 2 \
  "$OUT_DIR/frame_%06d.jpg"

echo "Frame capture complete."
ls -1 "$OUT_DIR"/*.jpg 2>/dev/null | wc -l | awk '{print "Captured frames:", $1}'
