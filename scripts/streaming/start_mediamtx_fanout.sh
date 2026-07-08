#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="${1:-scripts/streaming/rtmp_targets.env}"
MEDIAMTX_BIN="${MEDIAMTX_BIN:-$REPO_ROOT/tools/mediamtx/mediamtx}"
MEDIAMTX_CFG="${MEDIAMTX_CFG:-/tmp/fluxrt-mediamtx.yml}"
MEDIAMTX_LOG="${MEDIAMTX_LOG:-/tmp/fluxrt-mediamtx.log}"
MEDIAMTX_PID="${MEDIAMTX_PID:-/tmp/fluxrt-mediamtx.pid}"

INGEST_LOG="${INGEST_LOG:-/tmp/fluxrt-mediamtx-ingest.log}"
INGEST_PID="${INGEST_PID:-/tmp/fluxrt-mediamtx-ingest.pid}"
EGRESS_LOG="${EGRESS_LOG:-/tmp/fluxrt-mediamtx-egress.log}"
EGRESS_PID="${EGRESS_PID:-/tmp/fluxrt-mediamtx-egress.pid}"

VIDEO_INPUT_URL="${VIDEO_INPUT_URL:-udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
AUDIO_INPUT_URL="${AUDIO_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
TTS_INPUT_URL="${TTS_INPUT_URL:-udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"

ENABLE_TTS_OVERLAY="${ENABLE_TTS_OVERLAY:-1}"
OUTPUT_WIDTH="${OUTPUT_WIDTH:-426}"
OUTPUT_HEIGHT="${OUTPUT_HEIGHT:-240}"
FPS="${FPS:-12}"
VIDEO_BITRATE="${VIDEO_BITRATE:-1200k}"
VIDEO_MAXRATE="${VIDEO_MAXRATE:-1200k}"
VIDEO_BUFSIZE="${VIDEO_BUFSIZE:-2400k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-96k}"
VIDEO_TRANSCODE_MODE="${VIDEO_TRANSCODE_MODE:-copy}"
MUSIC_MIX_VOLUME="${MUSIC_MIX_VOLUME:-1.0}"
TTS_MIX_VOLUME="${TTS_MIX_VOLUME:-0.85}"
MUSIC_WAIT_TIMEOUT="${MUSIC_WAIT_TIMEOUT:-120}"
TTS_WAIT_TIMEOUT="${TTS_WAIT_TIMEOUT:-40}"
VIDEO_SOURCE_MODE="${VIDEO_SOURCE_MODE:-url}"
VIDEO_WAIT_TIMEOUT="${VIDEO_WAIT_TIMEOUT:-30}"
VIDEO_FALLBACK_ON_MISS="${VIDEO_FALLBACK_ON_MISS:-1}"

ENABLE_YOUTUBE="${ENABLE_YOUTUBE:-0}"
ENABLE_TWITCH="${ENABLE_TWITCH:-1}"
ENABLE_FACEBOOK="${ENABLE_FACEBOOK:-0}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing target env file: $ENV_FILE"
  echo "Copy scripts/streaming/rtmp_targets.env.example to scripts/streaming/rtmp_targets.env and fill keys."
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

TARGET_URL=""
TARGET_NAME=""
if [[ "$ENABLE_TWITCH" == "1" && -n "${TWITCH_RTMP_URL:-}" ]]; then
  TARGET_URL="$TWITCH_RTMP_URL"
  TARGET_NAME="Twitch"
elif [[ "$ENABLE_YOUTUBE" == "1" && -n "${YOUTUBE_RTMP_URL:-}" ]]; then
  TARGET_URL="$YOUTUBE_RTMP_URL"
  TARGET_NAME="YouTube"
elif [[ "$ENABLE_FACEBOOK" == "1" && -n "${FACEBOOK_RTMP_URL:-}" ]]; then
  TARGET_URL="$FACEBOOK_RTMP_URL"
  TARGET_NAME="Facebook"
fi

if [[ -z "$TARGET_URL" ]]; then
  echo "No enabled target URL found in $ENV_FILE"
  exit 1
fi

scripts/streaming/stop_mediamtx_fanout.sh >/dev/null 2>&1 || true

if [[ ! -x "$MEDIAMTX_BIN" ]]; then
  echo "MediaMTX binary not found; installing locally..."
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

nohup "$MEDIAMTX_BIN" "$MEDIAMTX_CFG" > "$MEDIAMTX_LOG" 2>&1 &
echo "$!" > "$MEDIAMTX_PID"

for _ in $(seq 1 50); do
  if (echo > /dev/tcp/127.0.0.1/1935) 2>/dev/null; then
    break
  fi
  sleep 0.1
done

if ! (echo > /dev/tcp/127.0.0.1/1935) 2>/dev/null; then
  echo "MediaMTX failed to bind port 1935. See $MEDIAMTX_LOG"
  exit 1
fi

ensure_udp_buffer_params() {
  local url="$1"
  if [[ "$url" != udp://* ]]; then
    echo "$url"
    return
  fi
  if [[ "$url" == *"fifo_size="* ]] && [[ "$url" == *"overrun_nonfatal="* ]]; then
    echo "$url"
    return
  fi
  local sep='?'
  if [[ "$url" == *"?"* ]]; then
    sep='&'
  fi
  echo "${url}${sep}fifo_size=50000000&overrun_nonfatal=1"
}

udp_audio_is_ready() {
  local url="$1"
  timeout 2 ffprobe -v error -analyzeduration 1M -probesize 1M \
    -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 \
    "$url" >/dev/null 2>&1
}

udp_video_is_ready() {
  local url="$1"
  timeout 2 ffprobe -v error -analyzeduration 1M -probesize 1M \
    -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 \
    "$url" >/dev/null 2>&1
}

wait_udp_audio_ready() {
  local url="$1"
  local wait_secs="$2"
  local i=0
  while [[ "$i" -lt "$wait_secs" ]]; do
    if udp_audio_is_ready "$url"; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

wait_udp_video_ready() {
  local url="$1"
  local wait_secs="$2"
  local i=0
  while [[ "$i" -lt "$wait_secs" ]]; do
    if udp_video_is_ready "$url"; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

INGEST_LOOP="/tmp/fluxrt-mediamtx-ingest-loop.sh"
EGRESS_LOOP="/tmp/fluxrt-mediamtx-egress-loop.sh"

AUDIO_INPUT_URL="$(ensure_udp_buffer_params "$AUDIO_INPUT_URL")"
TTS_INPUT_URL="$(ensure_udp_buffer_params "$TTS_INPUT_URL")"

AUDIO_SOURCE_MODE="${AUDIO_SOURCE_MODE:-url}"
TTS_SOURCE_MODE="${TTS_SOURCE_MODE:-url}"

# Keep broadcast audio always open: base silence is always present, while
# music / TTS legs are added when available and otherwise replaced by silence.
BASE_AUDIO_INPUT="-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000"

if [[ "$AUDIO_SOURCE_MODE" == "url" ]]; then
  if wait_udp_audio_ready "$AUDIO_INPUT_URL" "$MUSIC_WAIT_TIMEOUT"; then
    echo "[fanout] music input ready, binding UDP"
    AUDIO1_INPUT="-thread_queue_size 16384 -i \"${AUDIO_INPUT_URL}\""
  else
    echo "[fanout] music input unavailable after ${MUSIC_WAIT_TIMEOUT}s, using silence fallback"
    AUDIO1_INPUT="-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000"
  fi
else
  AUDIO1_INPUT="-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000"
fi

if [[ "$ENABLE_TTS_OVERLAY" == "1" && "$TTS_SOURCE_MODE" == "url" ]]; then
  echo "[fanout] binding TTS UDP directly (will wait for audio to arrive)"
  AUDIO2_INPUT="-thread_queue_size 16384 -i \"${TTS_INPUT_URL}\""
  TTS_VOL_EFFECTIVE="$TTS_MIX_VOLUME"
else
  AUDIO2_INPUT="-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000"
  TTS_VOL_EFFECTIVE="0.0"
fi

FILTER_COMPLEX="[1:a]volume=1.0[base];[2:a]volume=${MUSIC_MIX_VOLUME}[music];[3:a]volume=${TTS_VOL_EFFECTIVE}[tts];[base][music][tts]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0,aresample=async=1:min_hard_comp=0.100:first_pts=0[aout]"

if [[ "$VIDEO_TRANSCODE_MODE" == "copy" ]]; then
  VIDEO_ENCODE_ARGS='-c:v copy'
  VIDEO_FILTER_ARGS=''
  VIDEO_FPS_ARGS=''
else
  VIDEO_ENCODE_ARGS="-vf scale=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,crop=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT} -r ${FPS} -fps_mode cfr -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g $((FPS * 2)) -keyint_min $((FPS * 2)) -sc_threshold 0 -x264-params nal-hrd=cbr:force-cfr=1 -b:v ${VIDEO_BITRATE} -minrate ${VIDEO_BITRATE} -maxrate ${VIDEO_MAXRATE} -bufsize ${VIDEO_BUFSIZE}"
  VIDEO_FILTER_ARGS=''
  VIDEO_FPS_ARGS=''
fi

if [[ "$VIDEO_SOURCE_MODE" == "synthetic" ]]; then
  VIDEO_INPUT_ARG="-f lavfi -re -i color=c=black:s=${OUTPUT_WIDTH}x${OUTPUT_HEIGHT}:r=${FPS}"
  VIDEO_TRANSCODE_MODE="transcode"
else
  echo "[fanout] binding video UDP directly (will wait for video to arrive)"
  VIDEO_INPUT_ARG="-thread_queue_size 16384 -i \"${VIDEO_INPUT_URL}\""
fi

cat > "$INGEST_LOOP" <<EOF
#!/usr/bin/env bash
set -u
while true; do
  ffmpeg -hide_banner -loglevel info \
    -fflags +genpts+discardcorrupt+igndts \
    ${VIDEO_INPUT_ARG} \
    ${BASE_AUDIO_INPUT} \
    ${AUDIO1_INPUT} \
    ${AUDIO2_INPUT} \
    -map 0:v:0 -map "[aout]" \
    -filter_complex "${FILTER_COMPLEX}" \
    ${VIDEO_ENCODE_ARGS} \
    -c:a aac -b:a "${AUDIO_BITRATE}" -ar 48000 -ac 2 \
    -max_muxing_queue_size 4096 -muxdelay 0 -muxpreload 0 \
    -f flv "rtmp://127.0.0.1:1935/fluxrt" || true
  sleep 1
done
EOF
chmod +x "$INGEST_LOOP"

cat > "$EGRESS_LOOP" <<EOF
#!/usr/bin/env bash
set -u
while true; do
  ffmpeg -hide_banner -loglevel info \
    -fflags +genpts+discardcorrupt+igndts \
    -i "rtmp://127.0.0.1:1935/fluxrt" \
    -c copy -f flv "${TARGET_URL}" || true
  sleep 2
done
EOF
chmod +x "$EGRESS_LOOP"

nohup "$INGEST_LOOP" > "$INGEST_LOG" 2>&1 &
echo "$!" > "$INGEST_PID"

nohup "$EGRESS_LOOP" > "$EGRESS_LOG" 2>&1 &
echo "$!" > "$EGRESS_PID"

echo "MediaMTX fanout started."
echo "Target: $TARGET_NAME"
echo "MediaMTX PID: $(cat "$MEDIAMTX_PID")"
echo "Ingest PID: $(cat "$INGEST_PID")"
echo "Egress PID: $(cat "$EGRESS_PID")"
echo "Logs:"
echo "  $MEDIAMTX_LOG"
echo "  $INGEST_LOG"
echo "  $EGRESS_LOG"