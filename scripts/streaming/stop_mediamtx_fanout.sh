#!/usr/bin/env bash
# =============================================================================
# stop_mediamtx_fanout.sh — Stop the MediaMTX fanout stack.
# =============================================================================
# Kills MediaMTX and the ingest/egress ffmpeg loops, removes PID files, and
# cleans up generated loop scripts.
# =============================================================================
set -euo pipefail

for pid_file in /tmp/fluxrt-mediamtx-egress.pid /tmp/fluxrt-mediamtx-ingest.pid /tmp/fluxrt-mediamtx.pid; do
  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file")"
    kill "$pid" 2>/dev/null || true
    rm -f "$pid_file"
  fi
done

pkill -f 'tools/mediamtx/mediamtx' 2>/dev/null || true
pkill -f 'ffmpeg.*rtmp://127.0.0.1:1935/fluxrt' 2>/dev/null || true
pkill -f '/tmp/fluxrt-mediamtx-ingest-loop.sh' 2>/dev/null || true
pkill -f '/tmp/fluxrt-mediamtx-egress-loop.sh' 2>/dev/null || true

rm -f /tmp/fluxrt-mediamtx-ingest-loop.sh /tmp/fluxrt-mediamtx-egress-loop.sh

echo "Stopped MediaMTX fanout stack (if running)."
