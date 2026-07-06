#!/usr/bin/env bash
PID_FILE="/tmp/fluxrt-bars-audio-test.pid"
if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE")"
  kill "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
fi
pkill -f "ffmpeg.*smptebars=size=.*fluxrt-bars-audio-test" 2>/dev/null || true
pkill -f "ffmpeg.*smptebars=size=288x160" 2>/dev/null || true
echo "Stopped bars+audio test stream."
