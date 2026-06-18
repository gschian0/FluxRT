#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

APP_PORT="${APP_PORT:-7861}"
APP_HOST="${APP_HOST:-0.0.0.0}"
STREAM_CONFIG_PATH="${STREAM_CONFIG_PATH:-configs/stream_demo_config.json}"

# Publisher mode:
# - ffmpeg (default): no GUI, works today with installed tools
# - obs: headless OBS via xvfb-run (requires OBS preinstalled + preconfigured profile/scene)
PUBLISH_MODE="${PUBLISH_MODE:-ffmpeg}"

TWITCH_RTMP_URL="${TWITCH_RTMP_URL:-}"
if [[ -z "$TWITCH_RTMP_URL" ]]; then
  echo "Missing TWITCH_RTMP_URL environment variable."
  echo "Example: export TWITCH_RTMP_URL='rtmp://live.twitch.tv/app/<stream_key>'"
  exit 1
fi

VIDEO_INPUT_URL="${VIDEO_INPUT_URL:-udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
AUDIO_INPUT_URL="${AUDIO_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"

OUT_WIDTH="${OUT_WIDTH:-640}"
OUT_HEIGHT="${OUT_HEIGHT:-360}"
OUT_FPS="${OUT_FPS:-12}"
VIDEO_BITRATE="${VIDEO_BITRATE:-1200k}"
VIDEO_MAXRATE="${VIDEO_MAXRATE:-1200k}"
VIDEO_BUFSIZE="${VIDEO_BUFSIZE:-2400k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-96k}"

OBS_PROFILE="${OBS_PROFILE:-FluxRTHeadless}"
OBS_SCENE_COLLECTION="${OBS_SCENE_COLLECTION:-FluxRTHeadless}"

GRADIO_LOG="${GRADIO_LOG:-/tmp/fluxrt-gradio-stream.log}"
PUBLISH_LOG="${PUBLISH_LOG:-/tmp/fluxrt-publisher.log}"

clear_stale_processes() {
  echo "[cleanup] stopping stale FluxRT processes"
  pkill -f 'scripts/run_gradio_stream_demo.py' || true
  pkill -f 'run_musicgen_radio_plus_musicGEN.py' || true
  pkill -f 'run_quote_tts_from_json.py' || true

  echo "[cleanup] stopping stale ffmpeg publishers/writers"
  pkill -f 'ffmpeg.*udp://127.0.0.1:5000' || true
  pkill -f 'ffmpeg.*udp://127.0.0.1:5002' || true
  pkill -f 'ffmpeg.*udp://127.0.0.1:5004' || true
  pkill -f 'ffmpeg.*udp://127.0.0.1:5010' || true
  pkill -f 'ffmpeg.*live.twitch.tv/app' || true

  echo "[cleanup] stopping stale multiprocessing workers"
  pkill -f 'model_inference_subprocess' || true
  pkill -f 'output_scheduler_subprocess' || true
  pkill -f 'from multiprocessing.spawn import spawn_main' || true
  pkill -f 'multiprocessing.resource_tracker' || true

  echo "[cleanup] stopping stale fanout loop"
  scripts/streaming/stop_rtmp_fanout.sh >/dev/null 2>&1 || true

  sleep 2
}

wait_for_gpu_clear() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    return
  fi

  echo "[cleanup] waiting for GPU compute processes to drain"
  for _ in $(seq 1 45); do
    gpu_pids="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | sed '/^\s*$/d' || true)"
    if [[ -z "$gpu_pids" ]]; then
      echo "[cleanup] GPU is clear"
      return
    fi
    sleep 1
  done

  echo "[cleanup] warning: GPU still has compute processes"
  nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv,noheader || true
}

start_gradio() {
  export PYTORCH_CUDA_ALLOC_CONF='expandable_segments:True,max_split_size_mb:128'

  if [[ -f .venv/bin/python ]]; then
    nohup .venv/bin/python -u scripts/run_gradio_stream_demo.py --int8 --server-port "$APP_PORT" --server-name "$APP_HOST" --config-path "$STREAM_CONFIG_PATH" > "$GRADIO_LOG" 2>&1 &
  else
    nohup uv run scripts/run_gradio_stream_demo.py --int8 --server-port "$APP_PORT" --server-name "$APP_HOST" --config-path "$STREAM_CONFIG_PATH" > "$GRADIO_LOG" 2>&1 &
  fi

  GRADIO_PID=$!
  echo "$GRADIO_PID" > /tmp/fluxrt-gradio-stream.pid
  echo "[start] gradio pid=$GRADIO_PID log=$GRADIO_LOG"

  for _ in $(seq 1 180); do
    if curl -sSf -o /dev/null "http://127.0.0.1:${APP_PORT}"; then
      echo "[start] gradio ready on http://127.0.0.1:${APP_PORT}"
      return
    fi
    sleep 1
  done

  echo "[error] gradio did not become healthy in time"
  tail -n 80 "$GRADIO_LOG" || true
  exit 2
}

start_ffmpeg_publisher() {
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[error] ffmpeg is required for PUBLISH_MODE=ffmpeg"
    exit 1
  fi

  nohup ffmpeg -hide_banner -loglevel info \
    -fflags +genpts+discardcorrupt+igndts \
    -analyzeduration 2M -probesize 2M \
    -thread_queue_size 16384 -i "$VIDEO_INPUT_URL" \
    -thread_queue_size 16384 -i "$AUDIO_INPUT_URL" \
    -map 0:v:0 -map 1:a:0 \
    -vf "scale=${OUT_WIDTH}:${OUT_HEIGHT}:force_original_aspect_ratio=increase,crop=${OUT_WIDTH}:${OUT_HEIGHT}" \
    -r "$OUT_FPS" -fps_mode cfr \
    -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p \
    -g "$((OUT_FPS * 2))" -keyint_min "$((OUT_FPS * 2))" -sc_threshold 0 \
    -x264-params "nal-hrd=cbr:force-cfr=1" \
    -b:v "$VIDEO_BITRATE" -minrate "$VIDEO_BITRATE" -maxrate "$VIDEO_MAXRATE" -bufsize "$VIDEO_BUFSIZE" \
    -af "aresample=async=1:min_hard_comp=0.100:first_pts=0" \
    -c:a aac -b:a "$AUDIO_BITRATE" -ar 48000 -ac 2 \
    -max_muxing_queue_size 4096 -muxdelay 0 -muxpreload 0 \
    -f flv "$TWITCH_RTMP_URL" > "$PUBLISH_LOG" 2>&1 &

  PUBLISH_PID=$!
  echo "$PUBLISH_PID" > /tmp/fluxrt-publisher.pid
  echo "[start] ffmpeg publisher pid=$PUBLISH_PID log=$PUBLISH_LOG"
}

start_obs_headless_publisher() {
  if ! command -v obs >/dev/null 2>&1; then
    echo "[error] obs is not installed. Install OBS or use PUBLISH_MODE=ffmpeg."
    exit 1
  fi
  if ! command -v xvfb-run >/dev/null 2>&1; then
    echo "[error] xvfb-run is required for headless OBS mode."
    exit 1
  fi

  echo "[start] launching headless OBS"
  echo "[note] OBS mode expects a preconfigured profile/scene collection with stream settings."

  nohup xvfb-run -a obs \
    --disable-updater \
    --minimize-to-tray \
    --profile "$OBS_PROFILE" \
    --collection "$OBS_SCENE_COLLECTION" \
    --startstreaming > "$PUBLISH_LOG" 2>&1 &

  PUBLISH_PID=$!
  echo "$PUBLISH_PID" > /tmp/fluxrt-publisher.pid
  echo "[start] obs headless pid=$PUBLISH_PID log=$PUBLISH_LOG"
}

clear_stale_processes
wait_for_gpu_clear
start_gradio

case "$PUBLISH_MODE" in
  ffmpeg)
    start_ffmpeg_publisher
    ;;
  obs)
    start_obs_headless_publisher
    ;;
  *)
    echo "[error] Unknown PUBLISH_MODE=$PUBLISH_MODE (expected: ffmpeg or obs)"
    exit 1
    ;;
esac

echo "[ok] run_gradio_obs_demo is live"
echo "[info] gradio: http://127.0.0.1:${APP_PORT}"
echo "[info] gradio log: $GRADIO_LOG"
echo "[info] publisher log: $PUBLISH_LOG"
