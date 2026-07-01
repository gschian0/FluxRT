#!/usr/bin/env bash
set -euo pipefail

# Start RTMP fanout to one or more platforms (YouTube/Twitch/Facebook) from a single input stream.
# Targets are loaded from an env file so secrets do not need to be committed.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="${1:-scripts/streaming/rtmp_targets.env}"
INPUT_URL="${INPUT_URL:-udp://127.0.0.1:5000?pkt_size=1316}"
LOG_FILE="${LOG_FILE:-/tmp/fluxrt-rtmp-fanout.log}"
PID_FILE="${PID_FILE:-/tmp/fluxrt-rtmp-fanout.pid}"
VIDEO_BITRATE="${VIDEO_BITRATE:-900k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-96k}"
# 24fps: ffmpeg -fps_mode cfr will pad/duplicate frames to hold this rate
# even when inference only produces 2-4fps — prevents Twitch UNSTABLE warning.
FPS="${FPS:-8}"
# Keep keyframe interval at ~2s by default for ingest compatibility.
GOP="${GOP:-$((FPS * 2))}"
OUTPUT_WIDTH="${OUTPUT_WIDTH:-426}"
OUTPUT_HEIGHT="${OUTPUT_HEIGHT:-240}"
VIDEO_MAXRATE="${VIDEO_MAXRATE:-${VIDEO_BITRATE}}"
VIDEO_BUFSIZE="${VIDEO_BUFSIZE:-1800k}"
X264_PRESET="${X264_PRESET:-ultrafast}"
ENABLE_YOUTUBE="${ENABLE_YOUTUBE:-1}"
ENABLE_TWITCH="${ENABLE_TWITCH:-1}"
ENABLE_FACEBOOK="${ENABLE_FACEBOOK:-1}"
# url mode skips ffprobe probing of UDP ports — prevents silence-fill freeze at startup.
AUDIO_SOURCE_MODE="${AUDIO_SOURCE_MODE:-url}"
AUDIO_INPUT_URL="${AUDIO_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316}"
ENABLE_TTS_OVERLAY="${ENABLE_TTS_OVERLAY:-1}"
TTS_INPUT_URL="${TTS_INPUT_URL:-udp://127.0.0.1:5004?pkt_size=1316}"
TTS_SOURCE_MODE="${TTS_SOURCE_MODE:-url}"
MUSIC_MIX_VOLUME="${MUSIC_MIX_VOLUME:-0.65}"
TTS_MIX_VOLUME="${TTS_MIX_VOLUME:-1.80}"
STARTUP_BARS_SECONDS="${STARTUP_BARS_SECONDS:-0}"
STARTUP_BARS_EXTEND_SECONDS="${STARTUP_BARS_EXTEND_SECONDS:-5}"
MAX_BARS_EXTENSIONS="${MAX_BARS_EXTENSIONS:-2}"
WAIT_FOR_VIDEO_READY="${WAIT_FOR_VIDEO_READY:-0}"
ENABLE_RECONNECT_BARS="${ENABLE_RECONNECT_BARS:-1}"
RECONNECT_BARS_SECONDS="${RECONNECT_BARS_SECONDS:-4}"
RECONNECT_SLEEP_SECONDS="${RECONNECT_SLEEP_SECONDS:-1}"
ENABLE_LOCAL_MONITOR="${ENABLE_LOCAL_MONITOR:-0}"
MONITOR_HLS_DIR="${MONITOR_HLS_DIR:-/tmp/fluxrt-monitor}"
MONITOR_HLS_PLAYLIST="${MONITOR_HLS_PLAYLIST:-$MONITOR_HLS_DIR/stream.m3u8}"

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

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required but not installed."
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing target env file: $ENV_FILE"
  echo "Copy scripts/streaming/rtmp_targets.env.example to scripts/streaming/rtmp_targets.env and fill keys."
  exit 1
fi

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "RTMP fanout already running with PID $(cat "$PID_FILE")."
  echo "Stop first with scripts/streaming/stop_rtmp_fanout.sh"
  exit 0
fi

# Clear stale fanout ffmpeg listeners from previous aborted starts.
pkill -f "ffmpeg.*smptebars=size=.*-f tee" || true
pkill -f "ffmpeg.*-i udp://127.0.0.1:5000" || true
pkill -f "ffmpeg.*thread_queue_size.*-i udp://127.0.0.1:5002" || true

# shellcheck disable=SC1090
source "$ENV_FILE"

TARGETS=()
[[ "$ENABLE_YOUTUBE" == "1" && -n "${YOUTUBE_RTMP_URL:-}" ]] && TARGETS+=("[f=flv:onfail=ignore]${YOUTUBE_RTMP_URL}")
[[ "$ENABLE_TWITCH" == "1" && -n "${TWITCH_RTMP_URL:-}" ]] && TARGETS+=("[f=flv:onfail=ignore]${TWITCH_RTMP_URL}")
[[ "$ENABLE_FACEBOOK" == "1" && -n "${FACEBOOK_RTMP_URL:-}" ]] && TARGETS+=("[f=flv:onfail=ignore]${FACEBOOK_RTMP_URL}")
if [[ "$ENABLE_LOCAL_MONITOR" == "1" ]]; then
  mkdir -p "$MONITOR_HLS_DIR"
  rm -f "$MONITOR_HLS_DIR"/stream.m3u8 "$MONITOR_HLS_DIR"/stream_*.ts
  TARGETS+=("[f=hls:onfail=ignore:hls_time=2:hls_list_size=8:hls_flags=delete_segments+program_date_time+independent_segments:hls_segment_filename=${MONITOR_HLS_DIR}/stream_%05d.ts]${MONITOR_HLS_PLAYLIST}")
fi

if [[ "${#TARGETS[@]}" -eq 0 ]]; then
  echo "No RTMP targets configured in $ENV_FILE"
  echo "Set at least one RTMP URL and keep its ENABLE_* switch set to 1"
  exit 1
fi

TEE_OUTPUT="$(IFS='|'; echo "${TARGETS[*]}")"
# Video input is a UDP MPEG-TS reader; adding fifo_size/overrun_nonfatal here
# makes ffmpeg try to bind the port itself, colliding with the Gradio writer.
# Keep the URL bare so it acts as a pure multicast reader.
INPUT_URL="${INPUT_URL%%\?*}?pkt_size=1316"

AUDIO_INPUT_URL_BUFFERED="$(ensure_udp_buffer_params "$AUDIO_INPUT_URL")"
TTS_INPUT_URL_BUFFERED="$(ensure_udp_buffer_params "$TTS_INPUT_URL")"

if [[ "$AUDIO_SOURCE_MODE" == "url" ]]; then
  PREROLL_AUDIO_INPUT_ARGS=(
    -thread_queue_size 16384
    -i "$AUDIO_INPUT_URL_BUFFERED"
  )
  PREROLL_AUDIO_MAP=(
    -map 0:v:0 -map 1:a:0
  )
elif [[ "$AUDIO_SOURCE_MODE" == "optional_url" ]]; then
  if audio_input_ready "$AUDIO_INPUT_URL_BUFFERED"; then
    PREROLL_AUDIO_INPUT_ARGS=(
      -thread_queue_size 16384
      -i "$AUDIO_INPUT_URL_BUFFERED"
    )
    echo "[fanout] Preroll audio detected: $AUDIO_INPUT_URL_BUFFERED" >> "$LOG_FILE"
  else
    PREROLL_AUDIO_INPUT_ARGS=(
      -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000
    )
    echo "[fanout] Preroll audio not ready; using silence fallback" >> "$LOG_FILE"
  fi
  PREROLL_AUDIO_MAP=(
    -map 0:v:0 -map 1:a:0
  )
else
  PREROLL_AUDIO_INPUT_ARGS=(
    -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000
  )
  PREROLL_AUDIO_MAP=(
    -map 0:v:0 -map 1:a:0
  )
fi

video_input_ready() {
  if ! command -v ffprobe >/dev/null 2>&1; then
    return 0
  fi
  if command -v timeout >/dev/null 2>&1; then
    timeout 3s ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$INPUT_URL" >/dev/null 2>&1
    return $?
  fi
  ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$INPUT_URL" >/dev/null 2>&1
}

audio_input_ready() {
  local url="$1"
  if ! command -v ffprobe >/dev/null 2>&1; then
    return 1
  fi
  if command -v timeout >/dev/null 2>&1; then
    timeout 2s ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$url" >/dev/null 2>&1
    return $?
  fi
  ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$url" >/dev/null 2>&1
}

run_bars_segment() {
  local duration="$1"
  ffmpeg -hide_banner -loglevel info \
    -re \
    -f lavfi -i "smptebars=size=${OUTPUT_WIDTH}x${OUTPUT_HEIGHT}:rate=${FPS}" \
    "${PREROLL_AUDIO_INPUT_ARGS[@]}" \
    "${PREROLL_AUDIO_MAP[@]}" \
    -t "$duration" \
    -r "$FPS" -fps_mode cfr \
    -c:v libx264 -preset "$X264_PRESET" -tune zerolatency -pix_fmt yuv420p \
    -force_key_frames "expr:gte(t,n_forced*2)" \
    -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
    -x264-params "nal-hrd=cbr:force-cfr=1" \
    -b:v "$VIDEO_BITRATE" -minrate "$VIDEO_BITRATE" -maxrate "$VIDEO_MAXRATE" -bufsize "$VIDEO_BUFSIZE" \
    -c:a aac -b:a "$AUDIO_BITRATE" -ar 48000 -ac 2 \
    -max_muxing_queue_size 4096 -muxdelay 0 -muxpreload 0 \
    -flvflags no_duration_filesize \
    -f tee "$TEE_OUTPUT" \
    >> "$LOG_FILE" 2>&1 || true
}

if [[ "$STARTUP_BARS_SECONDS" -gt 0 ]]; then
  echo "[fanout] Startup bars enabled (${STARTUP_BARS_SECONDS}s)." >> "$LOG_FILE"
  run_bars_segment "$STARTUP_BARS_SECONDS"
else
  echo "[fanout] Startup bars disabled; starting live feed immediately." >> "$LOG_FILE"
fi

if [[ "$AUDIO_SOURCE_MODE" == "url" ]]; then
  AUDIO_INPUT_URL="$AUDIO_INPUT_URL_BUFFERED"
  AUDIO_INPUT_ARGS=(
    -thread_queue_size 16384
    -i "$AUDIO_INPUT_URL"
  )
elif [[ "$AUDIO_SOURCE_MODE" == "optional_url" ]]; then
  AUDIO_INPUT_URL="$AUDIO_INPUT_URL_BUFFERED"
  if audio_input_ready "$AUDIO_INPUT_URL"; then
    AUDIO_INPUT_ARGS=(
      -thread_queue_size 16384
      -i "$AUDIO_INPUT_URL"
    )
    echo "[fanout] Live audio detected: $AUDIO_INPUT_URL" >> "$LOG_FILE"
  else
    AUDIO_INPUT_ARGS=(
      -f lavfi
      -i anullsrc=channel_layout=stereo:sample_rate=48000
    )
    echo "[fanout] Live audio not ready; using silence fallback" >> "$LOG_FILE"
  fi
else
  AUDIO_INPUT_ARGS=(
    -f lavfi
    -i anullsrc=channel_layout=stereo:sample_rate=48000
  )
fi

if [[ "$ENABLE_TTS_OVERLAY" == "1" ]]; then
  if [[ "$TTS_SOURCE_MODE" == "url" ]]; then
    TTS_INPUT_ARGS=(
      -thread_queue_size 16384
      -i "$TTS_INPUT_URL_BUFFERED"
    )
  elif [[ "$TTS_SOURCE_MODE" == "optional_url" ]]; then
    if audio_input_ready "$TTS_INPUT_URL_BUFFERED"; then
      TTS_INPUT_ARGS=(
        -thread_queue_size 16384
        -i "$TTS_INPUT_URL_BUFFERED"
      )
      echo "[fanout] TTS input detected: $TTS_INPUT_URL_BUFFERED" >> "$LOG_FILE"
    else
      TTS_INPUT_ARGS=(
        -f lavfi
        -i anullsrc=channel_layout=stereo:sample_rate=48000
      )
      echo "[fanout] TTS input not ready; using silence fallback" >> "$LOG_FILE"
    fi
  else
    TTS_INPUT_ARGS=(
      -f lavfi
      -i anullsrc=channel_layout=stereo:sample_rate=48000
    )
    echo "[fanout] TTS source mode '$TTS_SOURCE_MODE' -> silence" >> "$LOG_FILE"
  fi
else
  TTS_INPUT_ARGS=()
fi

_run_fanout_loop() {
  while true; do
    # Exit loop if stop was requested (PID file removed).
    if [[ ! -f "$PID_FILE" ]]; then
      echo "[fanout] PID file gone — stopping loop." >> "$LOG_FILE"
      break
    fi

    echo "[fanout] $(date -Is) starting ffmpeg..." >> "$LOG_FILE"
    if [[ "$ENABLE_TTS_OVERLAY" == "1" ]]; then
      ffmpeg -hide_banner -loglevel info \
        -fflags +genpts+discardcorrupt+igndts \
        -err_detect ignore_err \
        -analyzeduration 2M -probesize 2M \
        -thread_queue_size 16384 \
        -i "$INPUT_URL" \
        "${AUDIO_INPUT_ARGS[@]}" \
        "${TTS_INPUT_ARGS[@]}" \
        -map 0:v:0 -map "[aout]" \
        -vf "scale=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,crop=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT}" \
        -filter_complex "[1:a]volume=${MUSIC_MIX_VOLUME}[music];[2:a]volume=${TTS_MIX_VOLUME}[tts];[music][tts]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,aresample=async=1:min_hard_comp=0.100:first_pts=0[aout]" \
        -r "$FPS" -fps_mode cfr \
        -c:v libx264 -preset "$X264_PRESET" -tune zerolatency -pix_fmt yuv420p \
        -force_key_frames "expr:gte(t,n_forced*2)" \
        -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
        -x264-params "nal-hrd=cbr:force-cfr=1" \
        -b:v "$VIDEO_BITRATE" -minrate "$VIDEO_BITRATE" -maxrate "$VIDEO_MAXRATE" -bufsize "$VIDEO_BUFSIZE" \
        -c:a aac -b:a "$AUDIO_BITRATE" -ar 48000 -ac 2 \
        -max_muxing_queue_size 4096 -muxdelay 0 -muxpreload 0 \
        -flvflags no_duration_filesize \
        -f tee "$TEE_OUTPUT" \
        >> "$LOG_FILE" 2>&1
    else
      ffmpeg -hide_banner -loglevel info \
        -fflags +genpts+discardcorrupt+igndts \
        -err_detect ignore_err \
        -analyzeduration 2M -probesize 2M \
        -thread_queue_size 16384 \
        -i "$INPUT_URL" \
        "${AUDIO_INPUT_ARGS[@]}" \
        -map 0:v:0 -map 1:a:0 \
        -vf "scale=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,crop=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT}" \
        -r "$FPS" -fps_mode cfr \
        -c:v libx264 -preset "$X264_PRESET" -tune zerolatency -pix_fmt yuv420p \
        -force_key_frames "expr:gte(t,n_forced*2)" \
        -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
        -x264-params "nal-hrd=cbr:force-cfr=1" \
        -b:v "$VIDEO_BITRATE" -minrate "$VIDEO_BITRATE" -maxrate "$VIDEO_MAXRATE" -bufsize "$VIDEO_BUFSIZE" \
        -af "aresample=async=1:min_hard_comp=0.100:first_pts=0" \
        -c:a aac -b:a "$AUDIO_BITRATE" -ar 48000 -ac 2 \
        -max_muxing_queue_size 4096 -muxdelay 0 -muxpreload 0 \
        -flvflags no_duration_filesize \
        -f tee "$TEE_OUTPUT" \
        >> "$LOG_FILE" 2>&1
    fi

    EXIT_CODE=$?
    if [[ ! -f "$PID_FILE" ]]; then
      echo "[fanout] $(date -Is) stopped cleanly." >> "$LOG_FILE"
      break
    fi
    echo "[fanout] $(date -Is) ffmpeg exited (code $EXIT_CODE)." >> "$LOG_FILE"
    if [[ "$ENABLE_RECONNECT_BARS" == "1" ]] && [[ "$RECONNECT_BARS_SECONDS" -gt 0 ]]; then
      echo "[fanout] $(date -Is) sending reconnect bars for ${RECONNECT_BARS_SECONDS}s." >> "$LOG_FILE"
      run_bars_segment "$RECONNECT_BARS_SECONDS"
    fi
    echo "[fanout] $(date -Is) reconnecting in ${RECONNECT_SLEEP_SECONDS}s..." >> "$LOG_FILE"
    sleep "$RECONNECT_SLEEP_SECONDS"
  done
}

nohup bash -c "$(declare -f run_bars_segment); $(declare -f _run_fanout_loop); \
  INPUT_URL='$INPUT_URL'; \
  AUDIO_INPUT_ARGS=(${AUDIO_INPUT_ARGS[*]@Q}); \
  PREROLL_AUDIO_INPUT_ARGS=(${PREROLL_AUDIO_INPUT_ARGS[*]@Q}); \
  PREROLL_AUDIO_MAP=(${PREROLL_AUDIO_MAP[*]@Q}); \
  TTS_INPUT_ARGS=(${TTS_INPUT_ARGS[*]@Q}); \
  ENABLE_TTS_OVERLAY='$ENABLE_TTS_OVERLAY'; \
  MUSIC_MIX_VOLUME='$MUSIC_MIX_VOLUME'; \
  TTS_MIX_VOLUME='$TTS_MIX_VOLUME'; \
  OUTPUT_WIDTH='$OUTPUT_WIDTH'; OUTPUT_HEIGHT='$OUTPUT_HEIGHT'; \
  FPS='$FPS'; GOP='$GOP'; X264_PRESET='$X264_PRESET'; \
  VIDEO_BITRATE='$VIDEO_BITRATE'; VIDEO_MAXRATE='$VIDEO_MAXRATE'; VIDEO_BUFSIZE='$VIDEO_BUFSIZE'; \
  AUDIO_BITRATE='$AUDIO_BITRATE'; \
  ENABLE_RECONNECT_BARS='$ENABLE_RECONNECT_BARS'; \
  RECONNECT_BARS_SECONDS='$RECONNECT_BARS_SECONDS'; \
  RECONNECT_SLEEP_SECONDS='$RECONNECT_SLEEP_SECONDS'; \
  TEE_OUTPUT='$TEE_OUTPUT'; \
  PID_FILE='$PID_FILE'; LOG_FILE='$LOG_FILE'; \
  _run_fanout_loop" >> "$LOG_FILE" 2>&1 &

FANOUT_PID=$!
echo "$FANOUT_PID" > "$PID_FILE"

sleep 2
if kill -0 "$FANOUT_PID" 2>/dev/null; then
  echo "RTMP fanout started (auto-reconnect loop)."
  echo "PID: $FANOUT_PID"
  echo "Input: $INPUT_URL"
  echo "Targets configured: ${#TARGETS[@]}"
  if [[ "$ENABLE_LOCAL_MONITOR" == "1" ]]; then
    echo "Local monitor playlist: $MONITOR_HLS_PLAYLIST"
  fi
  echo "Log: $LOG_FILE"
  exit 0
fi

echo "RTMP fanout failed to start. Check log: $LOG_FILE"
exit 1
