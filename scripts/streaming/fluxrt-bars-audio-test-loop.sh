#!/usr/bin/env bash
set -u
PID_FILE="/tmp/fluxrt-bars-audio-test.pid"
LOG_FILE="/tmp/fluxrt-bars-audio-test.log"
TARGET_URL="rtmp://live.twitch.tv/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw"
udp_audio_is_ready() {
  local url="$1"
  timeout 2 ffprobe -v error -analyzeduration 1M -probesize 1M     -select_streams a:0 -show_entries stream=codec_name -of csv=p=0     "$url" >/dev/null 2>&1
}

wait_udp_audio_ready() {
  local url="$1"
  local wait_secs="$2"
  local label="$3"
  local i=0
  while [[ "$i" -lt "$wait_secs" ]]; do
    if udp_audio_is_ready "$url"; then
      echo "[bars-test] $(date -Is) ${label} ready" >> "$LOG_FILE"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "[bars-test] $(date -Is) WARNING: ${label} not ready after ${wait_secs}s" >> "$LOG_FILE"
  return 1
}

while [[ -f "$PID_FILE" ]]; do
  echo "[bars-test] $(date -Is) ffmpeg starting" >> "$LOG_FILE"
  ffmpeg -hide_banner -loglevel info     -fflags +genpts+discardcorrupt+igndts     -analyzeduration 2M -probesize 2M     -err_detect ignore_err     -f lavfi -i "smptebars=size=288x160:rate=8"     -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000     -timeout 5000000 -thread_queue_size 16384 -i "udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1"     -thread_queue_size 16384 -i "udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1"     -map 0:v:0 -map "[aout]"     -filter_complex "[1:a]volume=1.0[base];[2:a]volume=0.85[music];[3:a]volume=1.80[tts];[base][music][tts]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0,aresample=async=1:min_hard_comp=0.100:first_pts=0[aout]"     -r 8 -fps_mode cfr     -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p     -force_key_frames "expr:gte(t,n_forced*2)"     -g 16 -keyint_min 16 -sc_threshold 0     -x264-params "nal-hrd=cbr:force-cfr=1"     -b:v 550k -minrate 550k -maxrate 550k -bufsize 1100k     -c:a aac -b:a 128k -ar 48000 -ac 2     -max_muxing_queue_size 4096 -muxdelay 0 -muxpreload 0     -flvflags no_duration_filesize     -f flv "$TARGET_URL" >> "$LOG_FILE" 2>&1 || true
  if [[ ! -f "$PID_FILE" ]]; then break; fi
  echo "[bars-test] $(date -Is) ffmpeg exited, reconnecting in 2s" >> "$LOG_FILE"
  sleep 2
done
echo "[bars-test] $(date -Is) stopped" >> "$LOG_FILE"
