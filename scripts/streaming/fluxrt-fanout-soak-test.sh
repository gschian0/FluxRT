#!/usr/bin/env bash
# Synthetic UDP soak test for RTMP fanout stability (no Twitch, local HLS only).
# Pass: fanout runs 5+ minutes with zero "ffmpeg exited" lines in log.
set -euo pipefail

REPO_ROOT="/workspace/FluxRT"
cd "$REPO_ROOT"

SOAK_SECONDS="${SOAK_SECONDS:-310}"
MONITOR_DIR="${MONITOR_DIR:-/tmp/fluxrt-soak-test}"

bash scripts/streaming/stop_rtmp_fanout.sh 2>/dev/null || true
pkill -f 'ffmpeg.*udp://127.0.0.1:5000' 2>/dev/null || true
pkill -f 'ffmpeg.*udp://127.0.0.1:5006' 2>/dev/null || true
rm -f /tmp/fluxrt-rtmp-fanout.log /tmp/fluxrt-fanout-progress.txt
mkdir -p "$MONITOR_DIR"
sleep 1

ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=288x160:rate=8 \
  -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p \
  -g 16 -keyint_min 16 -sc_threshold 0 -mpegts_flags +resend_headers \
  -f mpegts "udp://127.0.0.1:5000?pkt_size=1316" &
VID_PID=$!

ffmpeg -hide_banner -loglevel error -re -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -c:a aac -b:a 128k -ar 48000 -ac 2 \
  -f mpegts "udp://127.0.0.1:5006?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" &
AUD_PID=$!

cleanup() {
  kill "$VID_PID" "$AUD_PID" 2>/dev/null || true
  bash scripts/streaming/stop_rtmp_fanout.sh 2>/dev/null || true
}
trap cleanup EXIT

sleep 5

ENABLE_TWITCH=0 ENABLE_YOUTUBE=0 ENABLE_FACEBOOK=0 ENABLE_LOCAL_MONITOR=1 \
ENABLE_RECONNECT_BARS=0 VIDEO_TRANSCODE_MODE=copy WAIT_FOR_VIDEO_READY=1 \
AUDIO_SOURCE_MODE=url ENABLE_TTS_OVERLAY=0 \
INPUT_URL='udp://127.0.0.1:5000?pkt_size=1316' \
AUDIO_INPUT_URL='udp://127.0.0.1:5006?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1' \
OUTPUT_WIDTH=288 OUTPUT_HEIGHT=160 FPS=8 \
MONITOR_HLS_DIR="$MONITOR_DIR" \
bash scripts/streaming/start_rtmp_fanout.sh scripts/streaming/rtmp_targets.env

echo "Soaking fanout for ${SOAK_SECONDS}s..."
sleep "$SOAK_SECONDS"

exits="$(grep -c "ffmpeg exited" /tmp/fluxrt-rtmp-fanout.log 2>/dev/null || echo 0)"
bars="$(grep -c "reconnect bars" /tmp/fluxrt-rtmp-fanout.log 2>/dev/null || echo 0)"
last_out="$(grep '^out_time=' /tmp/fluxrt-fanout-progress.txt 2>/dev/null | tail -1 || true)"

echo "ffmpeg exits: $exits"
echo "reconnect bars: $bars"
echo "last progress: $last_out"

if [[ "$exits" -gt 0 ]] || [[ "$bars" -gt 0 ]]; then
  echo "FAIL: fanout unstable during soak"
  exit 1
fi

echo "PASS: fanout stable for ${SOAK_SECONDS}s"
