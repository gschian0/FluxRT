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
VIDEO_BITRATE="${VIDEO_BITRATE:-2200k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
FPS="${FPS:-12}"
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

# shellcheck disable=SC1090
source "$ENV_FILE"

TARGETS=()
[[ "$ENABLE_YOUTUBE" == "1" && -n "${YOUTUBE_RTMP_URL:-}" ]] && TARGETS+=("[f=flv:onfail=ignore]${YOUTUBE_RTMP_URL}")
[[ "$ENABLE_TWITCH" == "1" && -n "${TWITCH_RTMP_URL:-}" ]] && TARGETS+=("[f=flv:onfail=ignore]${TWITCH_RTMP_URL}")
[[ "$ENABLE_FACEBOOK" == "1" && -n "${FACEBOOK_RTMP_URL:-}" ]] && TARGETS+=("[f=flv:onfail=ignore]${FACEBOOK_RTMP_URL}")

if [[ "${#TARGETS[@]}" -eq 0 ]]; then
  echo "No RTMP targets configured in $ENV_FILE"
  echo "Set at least one RTMP URL and keep its ENABLE_* switch set to 1"
  exit 1
fi

TEE_OUTPUT="$(IFS='|'; echo "${TARGETS[*]}")"

nohup ffmpeg -hide_banner -loglevel info \
  -fflags +genpts+discardcorrupt \
  -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \
  -thread_queue_size 4096 \
  -i "$INPUT_URL" \
  -map 1:v:0 -map 0:a:0 \
  -vf "scale=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,crop=${OUTPUT_WIDTH}:${OUTPUT_HEIGHT}" \
  -r "$FPS" -fps_mode cfr \
  -c:v libx264 -preset "$X264_PRESET" -tune zerolatency -pix_fmt yuv420p \
  -force_key_frames "expr:gte(t,n_forced*2)" \
  -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
  -x264-params "nal-hrd=cbr:force-cfr=1" \
  -b:v "$VIDEO_BITRATE" -minrate "$VIDEO_BITRATE" -maxrate "$VIDEO_MAXRATE" -bufsize "$VIDEO_BUFSIZE" \
  -c:a aac -b:a "$AUDIO_BITRATE" -ar 48000 -ac 2 \
  -f tee "$TEE_OUTPUT" \
  > "$LOG_FILE" 2>&1 &

FANOUT_PID=$!
echo "$FANOUT_PID" > "$PID_FILE"

sleep 2
if kill -0 "$FANOUT_PID" 2>/dev/null; then
  echo "RTMP fanout started."
  echo "PID: $FANOUT_PID"
  echo "Input: $INPUT_URL"
  echo "Targets configured: ${#TARGETS[@]}"
  echo "Log: $LOG_FILE"
  exit 0
fi

echo "RTMP fanout failed to start. Check log: $LOG_FILE"
exit 1
