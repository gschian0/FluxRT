#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# tmux windows often have a minimal PATH — ss/netstat live under /usr/sbin
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

ENV_FILE="${1:-scripts/streaming/rtmp_targets.env}"
MEDIAMTX_BIN="${MEDIAMTX_BIN:-$REPO_ROOT/tools/mediamtx/mediamtx}"
MEDIAMTX_CFG="${MEDIAMTX_CFG:-/tmp/fluxrt-mediamtx.yml}"
MEDIAMTX_LOG="${MEDIAMTX_LOG:-/tmp/fluxrt-mediamtx.log}"
MEDIAMTX_PID="${MEDIAMTX_PID:-/tmp/fluxrt-mediamtx.pid}"

INGEST_LOG="${INGEST_LOG:-/tmp/fluxrt-mediamtx-ingest.log}"
INGEST_PID="${INGEST_PID:-/tmp/fluxrt-mediamtx-ingest.pid}"
INGEST_PROGRESS="${INGEST_PROGRESS:-/tmp/fluxrt-mediamtx-ingest.progress}"
EGRESS_LOG="${EGRESS_LOG:-/tmp/fluxrt-mediamtx-egress.log}"
EGRESS_PID="${EGRESS_PID:-/tmp/fluxrt-mediamtx-egress.pid}"
EGRESS_PROGRESS="${EGRESS_PROGRESS:-/tmp/fluxrt-mediamtx-egress.progress}"

VIDEO_INPUT_URL="${VIDEO_INPUT_URL:-udp://127.0.0.1:5000?pkt_size=1316}"
AUDIO_INPUT_URL="${AUDIO_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
TTS_INPUT_URL="${TTS_INPUT_URL:-udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"

UDP_RW_TIMEOUT_US="${UDP_RW_TIMEOUT_US:-15000000}"
INGEST_RECONNECT_SLEEP="${INGEST_RECONNECT_SLEEP:-3}"
DRAIN_ON_RECONNECT="${DRAIN_ON_RECONNECT:-0}"

ENABLE_TTS_OVERLAY="${ENABLE_TTS_OVERLAY:-1}"
OUTPUT_WIDTH="${OUTPUT_WIDTH:-288}"
OUTPUT_HEIGHT="${OUTPUT_HEIGHT:-160}"
FPS="${FPS:-8}"
VIDEO_BITRATE="${VIDEO_BITRATE:-550k}"
VIDEO_MAXRATE="${VIDEO_MAXRATE:-550k}"
VIDEO_BUFSIZE="${VIDEO_BUFSIZE:-1100k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
VIDEO_TRANSCODE_MODE="${VIDEO_TRANSCODE_MODE:-copy}"
MUSIC_MIX_VOLUME="${MUSIC_MIX_VOLUME:-0.85}"
TTS_MIX_VOLUME="${TTS_MIX_VOLUME:-1.55}"
MUSIC_WAIT_TIMEOUT="${MUSIC_WAIT_TIMEOUT:-30}"
TTS_WAIT_TIMEOUT="${TTS_WAIT_TIMEOUT:-40}"
VIDEO_SOURCE_MODE="${VIDEO_SOURCE_MODE:-wait}"
VIDEO_WAIT_TIMEOUT="${VIDEO_WAIT_TIMEOUT:-120}"
VIDEO_FALLBACK_ON_MISS="${VIDEO_FALLBACK_ON_MISS:-1}"
RTMP_PUBLISHER_WAIT_TIMEOUT="${RTMP_PUBLISHER_WAIT_TIMEOUT:-60}"

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
sleep 2
if pgrep -f '/tmp/fluxrt-mediamtx-ingest-loop.sh' >/dev/null 2>&1 \
   || pgrep -f '/tmp/fluxrt-mediamtx-egress-loop.sh' >/dev/null 2>&1; then
  echo "ERROR: duplicate fanout loops still running after stop — aborting start"
  pgrep -af '/tmp/fluxrt-mediamtx-.*-loop.sh' || true
  exit 1
fi
rm -f "$INGEST_PROGRESS" "$EGRESS_PROGRESS"

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

rtmp_port_is_listening() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -qE '(:1935 |:1935$)'
    return $?
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -q ':1935'
    return $?
  fi
  (echo > /dev/tcp/127.0.0.1/1935) >/dev/null 2>&1
}

nohup "$MEDIAMTX_BIN" "$MEDIAMTX_CFG" > "$MEDIAMTX_LOG" 2>&1 &
echo "$!" > "$MEDIAMTX_PID"

for _ in $(seq 1 50); do
  if rtmp_port_is_listening; then
    break
  fi
  sleep 0.1
done

if ! rtmp_port_is_listening; then
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

ensure_udp_reader_params() {
  local url="$1"
  if [[ "$url" != udp://* ]]; then
    echo "$url"
    return
  fi
  # Reader must not add fifo_size — it can make ffmpeg bind UDP 5000 and collide with Gradio.
  local base="${url%%\?*}"
  echo "${base}?pkt_size=1316"
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

rtmp_publisher_is_ready() {
  timeout 3 ffprobe -v error -analyzeduration 1M -probesize 1M \
    -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 \
    "rtmp://127.0.0.1:1935/fluxrt" >/dev/null 2>&1
}

wait_rtmp_publisher_ready() {
  local wait_secs="$1"
  local i=0
  while [[ "$i" -lt "$wait_secs" ]]; do
    if rtmp_publisher_is_ready; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

INGEST_LOOP="/tmp/fluxrt-mediamtx-ingest-loop.sh"
EGRESS_LOOP="/tmp/fluxrt-mediamtx-egress-loop.sh"

VIDEO_READER_URL="$(ensure_udp_reader_params "$VIDEO_INPUT_URL")"
AUDIO_INPUT_URL="$(ensure_udp_buffer_params "$AUDIO_INPUT_URL")"
TTS_INPUT_URL="$(ensure_udp_buffer_params "$TTS_INPUT_URL")"

AUDIO_SOURCE_MODE="${AUDIO_SOURCE_MODE:-url}"
TTS_SOURCE_MODE="${TTS_SOURCE_MODE:-url}"

if [[ "$AUDIO_SOURCE_MODE" == "url" ]]; then
  if wait_udp_audio_ready "$AUDIO_INPUT_URL" "$MUSIC_WAIT_TIMEOUT"; then
    AUDIO1_INPUT="-thread_queue_size 16384 -i ${AUDIO_INPUT_URL@Q}"
  else
    echo "[fanout] music input unavailable after ${MUSIC_WAIT_TIMEOUT}s, using silence fallback"
    AUDIO1_INPUT="-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000"
  fi
else
  AUDIO1_INPUT="-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000"
fi

if [[ "$ENABLE_TTS_OVERLAY" == "1" && "$TTS_SOURCE_MODE" == "url" ]]; then
  if wait_udp_audio_ready "$TTS_INPUT_URL" "$TTS_WAIT_TIMEOUT"; then
    AUDIO2_INPUT="-thread_queue_size 16384 -i ${TTS_INPUT_URL@Q}"
  else
    echo "[fanout] tts input unavailable after ${TTS_WAIT_TIMEOUT}s, using silence fallback"
    AUDIO2_INPUT="-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000"
  fi
  TTS_VOL_EFFECTIVE="$TTS_MIX_VOLUME"
else
  AUDIO2_INPUT="-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000"
  TTS_VOL_EFFECTIVE="0.0"
fi

BASE_AUDIO_INPUT="-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000"
FILTER_COMPLEX="[1:a]volume=1.0[base];[2:a]volume=${MUSIC_MIX_VOLUME}[music];[3:a]volume=${TTS_VOL_EFFECTIVE}[tts];[base][music][tts]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0,aresample=async=1:min_hard_comp=0.100:first_pts=0[aout]"

if [[ "$VIDEO_TRANSCODE_MODE" == "copy" ]]; then
  VIDEO_ENCODE_ARGS='-c:v copy -bsf:v h264_mp4toannexb'
else
  VIDEO_ENCODE_ARGS="-vf scale=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,crop=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT} -r ${FPS} -fps_mode cfr -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g $((FPS * 2)) -keyint_min $((FPS * 2)) -sc_threshold 0 -x264-params nal-hrd=cbr:force-cfr=1 -b:v ${VIDEO_BITRATE} -minrate ${VIDEO_BITRATE} -maxrate ${VIDEO_MAXRATE} -bufsize ${VIDEO_BUFSIZE}"
fi

SYNTHETIC_VIDEO_INPUT="-f lavfi -i color=c=black:s=${OUTPUT_WIDTH}x${OUTPUT_HEIGHT}:r=${FPS}"

shell_single_quote() {
  local s="$1"
  printf "'%s'" "${s//\'/\'\\\'\'}"
}

if [[ "$AUDIO1_INPUT" == *"anullsrc"* ]]; then
  AUDIO1_ARRAY_LITERAL='(-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000)'
else
  AUDIO1_ARRAY_LITERAL="(-thread_queue_size 16384 -i $(shell_single_quote "$AUDIO_INPUT_URL"))"
fi

if [[ "$AUDIO2_INPUT" == *"anullsrc"* ]]; then
  AUDIO2_ARRAY_LITERAL='(-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000)'
else
  AUDIO2_ARRAY_LITERAL="(-thread_queue_size 16384 -i $(shell_single_quote "$TTS_INPUT_URL"))"
fi

if [[ "$VIDEO_TRANSCODE_MODE" == "copy" ]]; then
  VIDEO_ENCODE_ARRAY_LITERAL='(-c:v copy -bsf:v h264_mp4toannexb)'
else
  VFILTER="scale=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,crop=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT}"
  VIDEO_ENCODE_ARRAY_LITERAL="(-vf $(shell_single_quote "$VFILTER") -r ${FPS} -fps_mode cfr -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g $((FPS * 2)) -keyint_min $((FPS * 2)) -sc_threshold 0 -x264-params nal-hrd=cbr:force-cfr=1 -b:v ${VIDEO_BITRATE} -minrate ${VIDEO_BITRATE} -maxrate ${VIDEO_MAXRATE} -bufsize ${VIDEO_BUFSIZE})"
fi

cat > "$INGEST_LOOP" <<EOF
#!/usr/bin/env bash
set -u

VIDEO_READER_URL="${VIDEO_READER_URL}"
VIDEO_SOURCE_MODE="${VIDEO_SOURCE_MODE}"
VIDEO_WAIT_TIMEOUT="${VIDEO_WAIT_TIMEOUT}"
VIDEO_FALLBACK_ON_MISS="${VIDEO_FALLBACK_ON_MISS}"
UDP_RW_TIMEOUT_US="${UDP_RW_TIMEOUT_US}"
INGEST_RECONNECT_SLEEP="${INGEST_RECONNECT_SLEEP}"
DRAIN_ON_RECONNECT="${DRAIN_ON_RECONNECT}"
INGEST_PROGRESS="${INGEST_PROGRESS}"
AUDIO_BITRATE="${AUDIO_BITRATE}"
FILTER_COMPLEX="${FILTER_COMPLEX}"
AUDIO1_ARGS=${AUDIO1_ARRAY_LITERAL}
AUDIO2_ARGS=${AUDIO2_ARRAY_LITERAL}
VIDEO_ENCODE_ARGS=${VIDEO_ENCODE_ARRAY_LITERAL}

udp_video_is_ready() {
  local url="\$1"
  timeout 2 ffprobe -v error -analyzeduration 1M -probesize 1M \\
    -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 \\
    "\$url" >/dev/null 2>&1
}

wait_udp_video_ready() {
  local url="\$1"
  local wait_secs="\$2"
  local i=0
  while [[ "\$i" -lt "\$wait_secs" ]]; do
    if udp_video_is_ready "\$url"; then
      return 0
    fi
    sleep 1
    i=\$((i + 1))
  done
  return 1
}

drain_udp_video() {
  # Do not drain before every ffmpeg start — binding UDP 5000 disrupts Gradio publisher.
  if [[ "\${DRAIN_ON_RECONNECT}" != "1" ]]; then
    return 0
  fi
  echo "[ingest] \$(date -Is) draining stale UDP video buffer (DRAIN_ON_RECONNECT=1)"
  timeout 1 ffmpeg -hide_banner -loglevel error \\
    -rw_timeout 500000 \\
    -i "\${VIDEO_READER_URL}" \\
    -c copy -f null - 2>/dev/null || true
}

wait_udp_keyframe() {
  local url="\$1"
  local wait_secs="\${2:-20}"
  echo "[ingest] \$(date -Is) waiting for keyframe on UDP video (up to \${wait_secs}s)"
  timeout "\${wait_secs}" ffmpeg -hide_banner -loglevel error \\
    -fflags +discardcorrupt \\
    -rw_timeout 5000000 \\
    -i "\${url}" \\
    -vf "select='eq(pict_type,I)'" -vsync vfr -frames:v 1 -f null - 2>/dev/null
}

resolve_video_input() {
  if [[ "\${VIDEO_SOURCE_MODE}" == "synthetic" ]]; then
    printf '%s\n' -f lavfi -i "color=c=black:s=${OUTPUT_WIDTH}x${OUTPUT_HEIGHT}:r=${FPS}"
    return
  fi
  if [[ "\${VIDEO_SOURCE_MODE}" == "wait" ]]; then
    if wait_udp_video_ready "\${VIDEO_READER_URL}" "\${VIDEO_WAIT_TIMEOUT}"; then
      printf '%s\n' -rw_timeout "\${UDP_RW_TIMEOUT_US}" -thread_queue_size 16384 -i "\${VIDEO_READER_URL}"
      return
    fi
    if [[ "\${VIDEO_FALLBACK_ON_MISS}" == "1" ]]; then
      echo "[ingest] \$(date -Is) video wait timeout, synthetic fallback" >&2
      printf '%s\n' -f lavfi -i "color=c=black:s=${OUTPUT_WIDTH}x${OUTPUT_HEIGHT}:r=${FPS}"
      return
    fi
    echo "[ingest] \$(date -Is) video wait timeout, attaching anyway" >&2
    printf '%s\n' -rw_timeout "\${UDP_RW_TIMEOUT_US}" -thread_queue_size 16384 -i "\${VIDEO_READER_URL}"
    return
  fi
  if udp_video_is_ready "\${VIDEO_READER_URL}"; then
    printf '%s\n' -rw_timeout "\${UDP_RW_TIMEOUT_US}" -thread_queue_size 16384 -i "\${VIDEO_READER_URL}"
  elif [[ "\${VIDEO_FALLBACK_ON_MISS}" == "1" ]]; then
    echo "[ingest] \$(date -Is) video not ready, synthetic fallback" >&2
    printf '%s\n' -f lavfi -i "color=c=black:s=${OUTPUT_WIDTH}x${OUTPUT_HEIGHT}:r=${FPS}"
  else
    printf '%s\n' -rw_timeout "\${UDP_RW_TIMEOUT_US}" -thread_queue_size 16384 -i "\${VIDEO_READER_URL}"
  fi
}

first_start=1
while true; do
  if [[ "\${first_start}" -eq 0 ]]; then
    drain_udp_video
    echo "[ingest] \$(date -Is) waiting \${INGEST_RECONNECT_SLEEP}s for MediaMTX publisher slot"
    sleep "\${INGEST_RECONNECT_SLEEP}"
    wait_udp_keyframe "\${VIDEO_READER_URL}" 20 || \\
      echo "[ingest] \$(date -Is) keyframe wait timed out, attaching anyway" >&2
  fi
  first_start=0
  if ! wait_udp_video_ready "\${VIDEO_READER_URL}" "\${VIDEO_WAIT_TIMEOUT}"; then
    if [[ "\${VIDEO_FALLBACK_ON_MISS}" == "1" ]]; then
      echo "[ingest] \$(date -Is) video not ready, using synthetic until UDP 5000 returns" >&2
    else
      echo "[ingest] \$(date -Is) video not ready, retrying" >&2
      sleep "\${INGEST_RECONNECT_SLEEP}"
      continue
    fi
  fi
  mapfile -t VIDEO_INPUT_ARGS < <(resolve_video_input)
  rm -f "\${INGEST_PROGRESS}"
  echo "[ingest] \$(date -Is) ffmpeg starting"
  ffmpeg -hide_banner -loglevel info \\
    -fflags +genpts+discardcorrupt+igndts \\
    -analyzeduration 2M -probesize 2M \\
    -err_detect ignore_err \\
    "\${VIDEO_INPUT_ARGS[@]}" \\
    -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \\
    "\${AUDIO1_ARGS[@]}" \\
    "\${AUDIO2_ARGS[@]}" \\
    -filter_complex "\${FILTER_COMPLEX}" \\
    -map 0:v:0 -map "[aout]" \\
    "\${VIDEO_ENCODE_ARGS[@]}" \\
    -c:a aac -b:a "\${AUDIO_BITRATE}" -ar 48000 -ac 2 \\
    -max_muxing_queue_size 4096 -muxdelay 0 -muxpreload 0 \\
    -flvflags no_duration_filesize \\
    -progress "\${INGEST_PROGRESS}" -nostats \\
    -f flv "rtmp://127.0.0.1:1935/fluxrt" || true
  echo "[ingest] \$(date -Is) ffmpeg exited, reconnecting"
done
EOF
chmod +x "$INGEST_LOOP"

cat > "$EGRESS_LOOP" <<EOF
#!/usr/bin/env bash
set -u

EGRESS_PROGRESS="${EGRESS_PROGRESS}"
RTMP_PUBLISHER_WAIT_TIMEOUT="${RTMP_PUBLISHER_WAIT_TIMEOUT}"
TARGET_URL="${TARGET_URL}"

rtmp_publisher_is_ready() {
  timeout 3 ffprobe -v error -analyzeduration 1M -probesize 1M \\
    -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 \\
    "rtmp://127.0.0.1:1935/fluxrt" >/dev/null 2>&1
}

wait_rtmp_publisher_ready() {
  local wait_secs="\$1"
  local i=0
  while [[ "\$i" -lt "\$wait_secs" ]]; do
    if rtmp_publisher_is_ready; then
      return 0
    fi
    sleep 1
    i=\$((i + 1))
  done
  return 1
}

while true; do
  if ! wait_rtmp_publisher_ready "\${RTMP_PUBLISHER_WAIT_TIMEOUT}"; then
    echo "[egress] \$(date -Is) no RTMP publisher after \${RTMP_PUBLISHER_WAIT_TIMEOUT}s, retrying"
    sleep 2
    continue
  fi
  rm -f "\${EGRESS_PROGRESS}"
  echo "[egress] \$(date -Is) ffmpeg starting"
  ffmpeg -hide_banner -loglevel info \\
    -fflags +genpts+discardcorrupt+igndts \\
    -i "rtmp://127.0.0.1:1935/fluxrt" \\
    -c copy \\
    -progress "\${EGRESS_PROGRESS}" -nostats \\
    -f flv "\${TARGET_URL}" || true
  echo "[egress] \$(date -Is) ffmpeg exited, reconnecting in 2s"
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
echo "Progress:"
echo "  $INGEST_PROGRESS"
echo "  $EGRESS_PROGRESS"
echo "Logs:"
echo "  $MEDIAMTX_LOG"
echo "  $INGEST_LOG"
echo "  $EGRESS_LOG"
