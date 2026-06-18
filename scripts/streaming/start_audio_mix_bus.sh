#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

MIX_LOG="${MIX_LOG:-/tmp/fluxrt-audio-mix.log}"
MIX_PID="${MIX_PID:-/tmp/fluxrt-audio-mix.pid}"
MIX_LOOP="/tmp/fluxrt-audio-mix-loop.sh"

MIX_OUTPUT_URL="${MIX_OUTPUT_URL:-udp://127.0.0.1:5006?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
MUSIC_INPUT_URL="${MUSIC_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
TTS_INPUT_URL="${TTS_INPUT_URL:-udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"

MUSIC_MIX_VOLUME="${MUSIC_MIX_VOLUME:-0.65}"
TTS_MIX_VOLUME="${TTS_MIX_VOLUME:-2.8}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"

scripts/streaming/stop_audio_mix_bus.sh >/dev/null 2>&1 || true

cat > "$MIX_LOOP" <<EOF
#!/usr/bin/env bash
set -u
while true; do
  ffmpeg -hide_banner -loglevel info \
    -fflags +genpts+discardcorrupt+igndts \
    -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \
    -thread_queue_size 16384 -i "${MUSIC_INPUT_URL}" \
    -thread_queue_size 16384 -i "${TTS_INPUT_URL}" \
    -filter_complex "[0:a]volume=1.0[base];[1:a]volume=${MUSIC_MIX_VOLUME}[music];[2:a]volume=${TTS_MIX_VOLUME}[tts];[base][music][tts]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0,aresample=async=1:min_hard_comp=0.100:first_pts=0[aout]" \
    -map "[aout]" \
    -c:a aac -b:a "${AUDIO_BITRATE}" -ar 48000 -ac 2 \
    -f mpegts "${MIX_OUTPUT_URL}" || true
  sleep 1
done
EOF

chmod +x "$MIX_LOOP"
nohup "$MIX_LOOP" > "$MIX_LOG" 2>&1 &
echo "$!" > "$MIX_PID"

echo "Audio mix bus started."
echo "PID: $(cat "$MIX_PID")"
echo "Output: $MIX_OUTPUT_URL"
echo "Log: $MIX_LOG"