#!/usr/bin/env bash
# Temporary test: SMPTE color bars + music/TTS audio mix → Twitch RTMP
# Bypasses Gradio UDP 5000 video. Does NOT touch Gradio inference.
set -euo pipefail

REPO_ROOT="/workspace/FluxRT"
cd "$REPO_ROOT"

ENV_FILE="${ENV_FILE:-scripts/streaming/rtmp_targets.env}"
LOG_FILE="${LOG_FILE:-/tmp/fluxrt-bars-audio-test.log}"
PID_FILE="${PID_FILE:-/tmp/fluxrt-bars-audio-test.pid}"
LOOP_SCRIPT="/tmp/fluxrt-bars-audio-test-loop.sh"

OUTPUT_WIDTH="${OUTPUT_WIDTH:-288}"
OUTPUT_HEIGHT="${OUTPUT_HEIGHT:-160}"
FPS="${FPS:-8}"
GOP="${GOP:-$((FPS * 2))}"
VIDEO_BITRATE="${VIDEO_BITRATE:-550k}"
VIDEO_MAXRATE="${VIDEO_MAXRATE:-550k}"
VIDEO_BUFSIZE="${VIDEO_BUFSIZE:-1100k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
X264_PRESET="${X264_PRESET:-ultrafast}"

AUDIO_INPUT_URL="${AUDIO_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
TTS_INPUT_URL="${TTS_INPUT_URL:-udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
MUSIC_MIX_VOLUME="${MUSIC_MIX_VOLUME:-0.85}"
TTS_MIX_VOLUME="${TTS_MIX_VOLUME:-1.80}"
MUSIC_WAIT_TIMEOUT="${MUSIC_WAIT_TIMEOUT:-40}"
TTS_WAIT_TIMEOUT="${TTS_WAIT_TIMEOUT:-40}"

udp_audio_is_ready() {
  local url="$1"
  timeout 2 ffprobe -v error -analyzeduration 1M -probesize 1M \
    -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 \
    "$url" >/dev/null 2>&1
}

wait_udp_audio_ready() {
  local url="$1"
  local wait_secs="$2"
  local label="$3"
  local i=0
  while [[ "$i" -lt "$wait_secs" ]]; do
    if udp_audio_is_ready "$url"; then
      echo "[bars-test] $(date -Is) ${label} ready on ${url}" >> "$LOG_FILE"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "[bars-test] $(date -Is) WARNING: ${label} not ready after ${wait_secs}s (${url})" >> "$LOG_FILE"
  return 1
}

# shellcheck disable=SC1090
source "$ENV_FILE"

TARGET_URL="${TWITCH_RTMP_URL:-}"
if [[ -z "$TARGET_URL" ]]; then
  echo "TWITCH_RTMP_URL not set in $ENV_FILE"
  exit 1
fi

# Stop conflicting fanout stacks
scripts/streaming/stop_mediamtx_fanout.sh >/dev/null 2>&1 || true
scripts/streaming/stop_rtmp_fanout.sh >/dev/null 2>&1 || true
pkill -f "ffmpeg.*smptebars=size=288x160" 2>/dev/null || true
if [[ -f "$PID_FILE" ]]; then
  LOOP_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${LOOP_PID:-}" ]] && kill -0 "$LOOP_PID" 2>/dev/null; then
    kill "$LOOP_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi
sleep 1

wait_udp_audio_ready "$AUDIO_INPUT_URL" "$MUSIC_WAIT_TIMEOUT" "music" || true
wait_udp_audio_ready "$TTS_INPUT_URL" "$TTS_WAIT_TIMEOUT" "quote TTS" || true

cat > "$LOOP_SCRIPT" <<EOF
#!/usr/bin/env bash
set -u
PID_FILE="$PID_FILE"
LOG_FILE="$LOG_FILE"
TARGET_URL="$TARGET_URL"
udp_audio_is_ready() {
  local url="\$1"
  timeout 2 ffprobe -v error -analyzeduration 1M -probesize 1M \
    -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 \
    "\$url" >/dev/null 2>&1
}

wait_udp_audio_ready() {
  local url="\$1"
  local wait_secs="\$2"
  local label="\$3"
  local i=0
  while [[ "\$i" -lt "\$wait_secs" ]]; do
    if udp_audio_is_ready "\$url"; then
      echo "[bars-test] \$(date -Is) \${label} ready" >> "\$LOG_FILE"
      return 0
    fi
    sleep 1
    i=\$((i + 1))
  done
  echo "[bars-test] \$(date -Is) WARNING: \${label} not ready after \${wait_secs}s" >> "\$LOG_FILE"
  return 1
}

while [[ -f "\$PID_FILE" ]]; do
  echo "[bars-test] \$(date -Is) ffmpeg starting" >> "\$LOG_FILE"
  ffmpeg -hide_banner -loglevel info \
    -fflags +genpts+discardcorrupt+igndts \
    -analyzeduration 2M -probesize 2M \
    -err_detect ignore_err \
    -f lavfi -i "smptebars=size=${OUTPUT_WIDTH}x${OUTPUT_HEIGHT}:rate=${FPS}" \
    -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \
    -timeout 5000000 -thread_queue_size 16384 -i "${AUDIO_INPUT_URL}" \
    -thread_queue_size 16384 -i "${TTS_INPUT_URL}" \
    -map 0:v:0 -map "[aout]" \
    -filter_complex "[1:a]volume=1.0[base];[2:a]volume=${MUSIC_MIX_VOLUME}[music];[3:a]volume=${TTS_MIX_VOLUME}[tts];[base][music][tts]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0,aresample=async=1:min_hard_comp=0.100:first_pts=0[aout]" \
    -r ${FPS} -fps_mode cfr \
    -c:v libx264 -preset ${X264_PRESET} -tune zerolatency -pix_fmt yuv420p \
    -force_key_frames "expr:gte(t,n_forced*2)" \
    -g ${GOP} -keyint_min ${GOP} -sc_threshold 0 \
    -x264-params "nal-hrd=cbr:force-cfr=1" \
    -b:v ${VIDEO_BITRATE} -minrate ${VIDEO_BITRATE} -maxrate ${VIDEO_MAXRATE} -bufsize ${VIDEO_BUFSIZE} \
    -c:a aac -b:a ${AUDIO_BITRATE} -ar 48000 -ac 2 \
    -max_muxing_queue_size 4096 -muxdelay 0 -muxpreload 0 \
    -flvflags no_duration_filesize \
    -f flv "\$TARGET_URL" >> "\$LOG_FILE" 2>&1 || true
  if [[ ! -f "\$PID_FILE" ]]; then break; fi
  echo "[bars-test] \$(date -Is) ffmpeg exited, reconnecting in 2s" >> "\$LOG_FILE"
  sleep 2
done
echo "[bars-test] \$(date -Is) stopped" >> "\$LOG_FILE"
EOF
chmod +x "$LOOP_SCRIPT"

echo "[bars-test] $(date -Is) starting bars+audio → Twitch (${OUTPUT_WIDTH}x${OUTPUT_HEIGHT} @ ${FPS}fps)" >> "$LOG_FILE"

nohup "$LOOP_SCRIPT" >> "$LOG_FILE" 2>&1 &
echo "$!" > "$PID_FILE"
sleep 4

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null && pgrep -f "ffmpeg.*smptebars=size=${OUTPUT_WIDTH}x${OUTPUT_HEIGHT}" >/dev/null; then
  echo "Bars+audio test stream started."
  echo "PID: $(cat "$PID_FILE")"
  echo "Log: $LOG_FILE"
  echo "Video: SMPTE color bars ${OUTPUT_WIDTH}x${OUTPUT_HEIGHT} @ ${FPS}fps"
  echo "Audio: music (5002) + TTS (5004) mix"
  echo "Target: Twitch"
else
  echo "Failed to start. Check $LOG_FILE"
  tail -20 "$LOG_FILE"
  exit 1
fi
