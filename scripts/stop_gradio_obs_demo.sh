#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "[stop] stopping publisher and gradio"

if [[ -f /tmp/fluxrt-publisher.pid ]]; then
  kill "$(cat /tmp/fluxrt-publisher.pid)" 2>/dev/null || true
  rm -f /tmp/fluxrt-publisher.pid
fi
if [[ -f /tmp/fluxrt-gradio-stream.pid ]]; then
  kill "$(cat /tmp/fluxrt-gradio-stream.pid)" 2>/dev/null || true
  rm -f /tmp/fluxrt-gradio-stream.pid
fi

pkill -f 'scripts/run_gradio_stream_demo.py' || true
pkill -f 'run_musicgen_radio_plus_musicGEN.py' || true
pkill -f 'run_quote_tts_from_json.py' || true
pkill -f 'ffmpeg.*udp://127.0.0.1:5000' || true
pkill -f 'ffmpeg.*udp://127.0.0.1:5002' || true
pkill -f 'ffmpeg.*udp://127.0.0.1:5004' || true
pkill -f 'ffmpeg.*udp://127.0.0.1:5010' || true
pkill -f 'ffmpeg.*live.twitch.tv/app' || true
pkill -f 'obs --disable-updater' || true
pkill -f 'xvfb-run -a obs' || true
pkill -f 'from multiprocessing.spawn import spawn_main' || true
pkill -f 'multiprocessing.resource_tracker' || true

scripts/streaming/stop_rtmp_fanout.sh >/dev/null 2>&1 || true

echo "[stop] done"
