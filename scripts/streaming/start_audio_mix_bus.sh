#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

USE_LIQUIDSOAP_MIX="${USE_LIQUIDSOAP_MIX:-1}"
if [[ "$USE_LIQUIDSOAP_MIX" == "1" ]] && command -v liquidsoap >/dev/null 2>&1; then
  exec bash "$SCRIPT_DIR/start_liquidsoap_audio_mix.sh" "$@"
fi

MIX_LOG="${MIX_LOG:-/tmp/fluxrt-audio-mix.log}"
MIX_PID="${MIX_PID:-/tmp/fluxrt-audio-mix.pid}"
MIX_LOOP="/tmp/fluxrt-audio-mix-loop.sh"

MIX_OUTPUT_URL="${MIX_OUTPUT_URL:-udp://127.0.0.1:5006?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
MUSIC_INPUT_URL="${MUSIC_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
TTS_INPUT_URL="${TTS_INPUT_URL:-udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
SFX_INPUT_URL="${SFX_INPUT_URL:-udp://127.0.0.1:5008?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
ENABLE_SFX_INPUT="${ENABLE_SFX_INPUT:-0}"
FORCE_RESTART="${FORCE_RESTART:-0}"

MUSIC_MIX_VOLUME="${MUSIC_MIX_VOLUME:-0.85}"
TTS_MIX_VOLUME="${TTS_MIX_VOLUME:-1.55}"
SFX_MIX_VOLUME="${SFX_MIX_VOLUME:-0.25}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"

pid_is_running() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

if [[ "$FORCE_RESTART" != "1" ]] && [[ -f "$MIX_PID" ]]; then
  existing_pid="$(cat "$MIX_PID" 2>/dev/null || true)"
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && pid_is_running "$existing_pid"; then
    echo "Audio mix bus already running."
    echo "PID: $existing_pid"
    echo "Output: $MIX_OUTPUT_URL"
    echo "Log: $MIX_LOG"
    exit 0
  fi
fi

scripts/streaming/stop_audio_mix_bus.sh >/dev/null 2>&1 || true

if [[ "$ENABLE_SFX_INPUT" == "1" ]]; then
cat > "$MIX_LOOP" <<EOF
#!/usr/bin/env bash
set -u
while true; do
  ffmpeg -hide_banner -loglevel info \
    -fflags +genpts+discardcorrupt+igndts \
    -err_detect ignore_err \
    -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \
    -thread_queue_size 16384 -use_wallclock_as_timestamps 1 -i "${MUSIC_INPUT_URL}" \
    -thread_queue_size 16384 -use_wallclock_as_timestamps 1 -i "${TTS_INPUT_URL}" \
    -thread_queue_size 16384 -use_wallclock_as_timestamps 1 -i "${SFX_INPUT_URL}" \
    -filter_complex "[0:a]aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=N/SR/TB,volume=1.0[base];[1:a]aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=N/SR/TB,volume=${MUSIC_MIX_VOLUME}[music];[2:a]aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=N/SR/TB,volume=${TTS_MIX_VOLUME}[tts];[3:a]aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=N/SR/TB,volume=${SFX_MIX_VOLUME}[sfx];[base][music][tts][sfx]amix=inputs=4:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.95,aresample=async=1:min_hard_comp=0.100:first_pts=0[aout]" \
    -map "[aout]" \
    -c:a aac -b:a "${AUDIO_BITRATE}" -ar 48000 -ac 2 \
    -f mpegts "${MIX_OUTPUT_URL}" || true
  sleep 1
done
EOF
else
cat > "$MIX_LOOP" <<EOF
#!/usr/bin/env bash
set -u
while true; do
  ffmpeg -hide_banner -loglevel info \
    -fflags +genpts+discardcorrupt+igndts \
    -err_detect ignore_err \
    -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \
    -thread_queue_size 16384 -use_wallclock_as_timestamps 1 -i "${MUSIC_INPUT_URL}" \
    -thread_queue_size 16384 -use_wallclock_as_timestamps 1 -i "${TTS_INPUT_URL}" \
    -filter_complex "[0:a]aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=N/SR/TB,volume=1.0[base];[1:a]aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=N/SR/TB,volume=${MUSIC_MIX_VOLUME}[music];[2:a]aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=N/SR/TB,volume=${TTS_MIX_VOLUME}[tts];[base][music][tts]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.95,aresample=async=1:min_hard_comp=0.100:first_pts=0[aout]" \
    -map "[aout]" \
    -c:a aac -b:a "${AUDIO_BITRATE}" -ar 48000 -ac 2 \
    -f mpegts "${MIX_OUTPUT_URL}" || true
  sleep 1
done
EOF
fi

chmod +x "$MIX_LOOP"
nohup "$MIX_LOOP" > "$MIX_LOG" 2>&1 &
echo "$!" > "$MIX_PID"

echo "Audio mix bus started."
echo "PID: $(cat "$MIX_PID")"
echo "Output: $MIX_OUTPUT_URL"
if [[ "$ENABLE_SFX_INPUT" == "1" ]]; then
  echo "SFX input: $SFX_INPUT_URL"
fi
echo "Log: $MIX_LOG"