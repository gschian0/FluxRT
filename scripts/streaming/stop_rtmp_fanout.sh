#!/usr/bin/env bash
set -euo pipefail

# Stop RTMP fanout started by start_rtmp_fanout.sh

PID_FILE="${1:-/tmp/fluxrt-rtmp-fanout.pid}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No PID file found: $PID_FILE"
  # Continue cleanup for stale fanout ffmpeg processes that might have no PID file.
fi

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE")"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID" || true
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
      kill -KILL "$PID" || true
    fi
    echo "Stopped RTMP fanout PID $PID"
  else
    echo "RTMP fanout process not running (PID was $PID)."
  fi
fi

# Cleanup stale fanout ffmpeg processes (bars/live) that can keep UDP ports bound.
pkill -f "ffmpeg.*smptebars=size=.*-f tee" || true
pkill -f "ffmpeg.*-i udp://127.0.0.1:5000" || true
pkill -f "ffmpeg.*thread_queue_size.*-i udp://127.0.0.1:5002" || true

rm -f "$PID_FILE"
