#!/usr/bin/env bash
set -euo pipefail

MIX_PID="${MIX_PID:-/tmp/fluxrt-audio-mix.pid}"

if [[ -f "$MIX_PID" ]]; then
  pid="$(cat "$MIX_PID" 2>/dev/null || true)"
  if [[ -n "${pid}" ]]; then
    kill "$pid" 2>/dev/null || true
    sleep 0.5
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$MIX_PID"
fi

pkill -f '/tmp/fluxrt-audio-mix-loop.sh' 2>/dev/null || true
pkill -f 'liquidsoap.*fluxrt-audio-mix.liq' 2>/dev/null || true
# Only kill mix-bus encoders (anullsrc + mpegts output). Do NOT match fanout (-i udp://...5006).
pkill -f 'ffmpeg.*anullsrc.*-f mpegts udp://127.0.0.1:5006' 2>/dev/null || true
sleep 0.5
pkill -9 -f 'ffmpeg.*anullsrc.*-f mpegts udp://127.0.0.1:5006' 2>/dev/null || true

echo "Stopped audio mix bus (if running)."