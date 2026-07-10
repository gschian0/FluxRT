#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

MODE="${1:-start}"

GRADIO_PY="/root/fluxrt-venv/bin/python"
PROJECT_PY="$REPO_ROOT/.venv/bin/python3"
MEDIAMTX_BIN="$REPO_ROOT/tools/mediamtx/mediamtx"
MEDIAMTX_CFG="/tmp/fluxrt-mediamtx.yml"

GRADIO_LOG="/tmp/fluxrt-gradio.log"
TTS_LOG="/tmp/fluxrt-tts.log"
MUSICGEN_LOG="/dev/shm/musicgen/musicgen.log"
WATCHDOG_LOG="/tmp/fluxrt-watchdog.log"

start_media_mtx() {
  if pgrep -f "mediamtx /tmp/fluxrt-mediamtx.yml" >/dev/null 2>&1; then
    return 0
  fi
  if [[ ! -x "$MEDIAMTX_BIN" ]]; then
    scripts/streaming/install_mediamtx_local.sh
  fi
  cat > "$MEDIAMTX_CFG" <<'YAML'
logLevel: info
rtsp: false
hls: false
webrtc: false
srt: false
api: false
metrics: false
pprof: false
rtmpAddress: :1935
rtmpEncryption: "no"
paths:
  fluxrt:
    source: publisher
YAML
  nohup setsid "$MEDIAMTX_BIN" "$MEDIAMTX_CFG" > /tmp/fluxrt-mediamtx.log 2>&1 & disown
  sleep 2
}

start_gradio() {
  if pgrep -f "run_gradio_stream_demo.py --int8" >/dev/null 2>&1; then
    return 0
  fi
  GRADIO_SHARE=1 nohup setsid "$GRADIO_PY" -u scripts/run_gradio_stream_demo.py \
    --int8 \
    --server-name 0.0.0.0 --server-port 7862 \
    --config-path configs/stream_demo_config.json \
    --local-video /workspace/test_input.mp4 \
    > "$GRADIO_LOG" 2>&1 & disown
}

start_tts() {
  if pgrep -f "run_edge_tts_quotes.py" >/dev/null 2>&1; then
    return 0
  fi
  nohup setsid "$PROJECT_PY" -u scripts/run_edge_tts_quotes.py \
    --quotes data/quotes/diffusiongemma_quotes.json \
    --loop --shuffle --interval 15 --random-voices \
    --reverb --reverb-mix 0.35 --reverb-decay 0.50 --reverb-delay-ms 60 \
    --echo --echo-mix 0.45 --echo-decay 0.65 --echo-delay-ms 150 \
    --udp-url "udp://127.0.0.1:5004?pkt_size=1316" \
    --repeat-on-empty --cache-dir voices/quote_cache_edge --cache-size 4 \
    > "$TTS_LOG" 2>&1 & disown
}

start_musicgen() {
  if pgrep -f "run_musicgen_radio_plus_musicGEN.py" >/dev/null 2>&1; then
    return 0
  fi
  mkdir -p /dev/shm/musicgen
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  CUDA_VISIBLE_DEVICES=1 nohup setsid taskset -c 16-63 \
    "$PROJECT_PY" -u scripts/run_musicgen_radio_plus_musicGEN.py \
    --radio-url "https://ice64.securenetsystems.net/LFTM" \
    --output-dir /dev/shm/musicgen \
    --model facebook/musicgen-small \
    --gen-seconds 16 \
    --top-k 250 --top-p 0.95 --temperature 1.0 --guidance-scale 3.0 \
    --drunk-walk --drunk-walk-strength 1.0 --parallel-clips 2 --seed -1 \
    --bootstrap-clips 24 --pre-generate 2 --bpm 120 \
    --pause-seconds 0 --base-prompt "" \
    --conditioning-mode continuation --conditioning-seconds 8 \
    --stream-delay-seconds 30 \
    --audio-udp-url "udp://127.0.0.1:5002?pkt_size=1316" \
    --crossfade-seconds 2.0 \
    > "$MUSICGEN_LOG" 2>&1 & disown
}

start_watchdog() {
  if pgrep -f "scripts/streaming/watchdog.sh" >/dev/null 2>&1; then
    return 0
  fi
  MUSICGEN_START_TIME="$(( $(date +%s) - 200 ))" nohup setsid bash -c 'export MUSICGEN_START_TIME; bash scripts/streaming/watchdog.sh' \
    > "$WATCHDOG_LOG" 2>&1 & disown
}

start_fanout() {
  if pgrep -f "fluxrt-mediamtx-ingest-loop" >/dev/null 2>&1; then
    return 0
  fi
  VIDEO_SOURCE_MODE=synthetic AUDIO_SOURCE_MODE=url TTS_SOURCE_MODE=url \
  ENABLE_TTS_OVERLAY=1 \
  nohup setsid bash scripts/streaming/start_mediamtx_fanout.sh \
    > /tmp/fluxrt-fanout-start.log 2>&1 & disown
}

stop_all() {
  pkill -9 -f "run_gradio_stream_demo.py --int8" 2>/dev/null || true
  pkill -9 -f "run_edge_tts_quotes.py" 2>/dev/null || true
  pkill -9 -f "run_musicgen_radio_plus_musicGEN.py" 2>/dev/null || true
  pkill -9 -f "scripts/streaming/watchdog.sh" 2>/dev/null || true
  pkill -9 -f "fluxrt-mediamtx-ingest-loop" 2>/dev/null || true
  pkill -9 -f "fluxrt-mediamtx-egress-loop" 2>/dev/null || true
  pkill -9 -f "mediamtx /tmp/fluxrt-mediamtx.yml" 2>/dev/null || true
  pkill -9 -f "rtmp://127.0.0.1:1935/fluxrt" 2>/dev/null || true
  pkill -9 -f "rtmps://live.twitch.tv:443/app/live_726973151" 2>/dev/null || true
}

status_all() {
  echo "=== Gradio ==="
  pgrep -af "run_gradio_stream_demo.py --int8" || true
  echo "=== MusicGen ==="
  pgrep -af "run_musicgen_radio_plus_musicGEN.py" || true
  echo "=== TTS ==="
  pgrep -af "run_edge_tts_quotes.py" || true
  echo "=== Watchdog ==="
  pgrep -af "scripts/streaming/watchdog.sh" || true
  echo "=== MediaMTX ==="
  pgrep -af "mediamtx /tmp/fluxrt-mediamtx.yml" || true
  echo "=== UDP ==="
  cat /proc/net/udp | grep -E "1388|138a|138c" || true
}

case "$MODE" in
  start)
    start_media_mtx
    start_gradio
    start_tts
    start_musicgen
    sleep 10
    start_fanout
    start_watchdog
    sleep 5
    status_all
    ;;
  stop)
    stop_all
    ;;
  status)
    status_all
    ;;
  restart)
    stop_all
    sleep 2
    start_media_mtx
    start_gradio
    start_tts
    start_musicgen
    sleep 10
    start_fanout
    start_watchdog
    sleep 5
    status_all
    ;;
  *)
    echo "Usage: $0 {start|stop|status|restart}"
    exit 1
    ;;
esac