#!/usr/bin/env bash
set -uo pipefail

# =============================================================================
# start_continuous_audio_rail.sh — Never-stopping audio rail
# =============================================================================
# Architecture:
#   MusicGen (UDP 5002) ─┐
#                        ├─→ [audio rail relay] ──→ UDP 5002 (continuous)
#   Radio fallback ──────┘                         (never stops)
#
# The rail always outputs to UDP 5002. If MusicGen's stream hiccups or dies,
# the relay seamlessly falls back to a radio stream or generated silence.
# TTS drops in non-blocking on top via the mix bus (UDP 5004 → 5006).
#
# The "skips" the user mentioned happen on the line — the rail keeps rolling
# like a tape recording, glitches are audible but the stream never drops.
#
# Usage:
#   bash start_continuous_audio_rail.sh
#
# Env vars:
#   MUSIC_INPUT_URL   — MusicGen output (default: udp://127.0.0.1:5002?...)
#   RAIL_OUTPUT_URL   — Rail output (default: udp://127.0.0.1:5002?...)
#   RADIO_FALLBACK_URL — Radio stream for when MusicGen is down
#   RAIL_MODE         — "relay" (pass MusicGen through) or "generate" (use radio)
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

RAIL_LOG="${RAIL_LOG:-/tmp/fluxrt-audio-rail.log}"
RAIL_PID_FILE="${RAIL_PID_FILE:-/tmp/fluxrt-audio-rail.pid}"
RAIL_LOOP="/tmp/fluxrt-audio-rail-loop.sh"

MUSIC_INPUT_URL="${MUSIC_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
RAIL_OUTPUT_URL="${RAIL_OUTPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
RADIO_FALLBACK_URL="${RADIO_FALLBACK_URL:-http://stream.zeno.fm/0a4yq1u0f0hvv}"
RAIL_MODE="${RAIL_MODE:-relay}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"

# If MusicGen writes directly to 5002 and we also want to relay to 5002,
# we need a different port for MusicGen output. Let's use 5012 as the
# MusicGen raw output port, and the rail reads from 5012 and writes to 5002.
# But if MusicGen already writes to 5002, we can just pass through.
# In relay mode, we read from MUSIC_INPUT_URL and write to RAIL_OUTPUT_URL.
# If they're the same, we need to change one. Let's handle this:

if [[ "$MUSIC_INPUT_URL" == "$RAIL_OUTPUT_URL" ]]; then
  # MusicGen writes directly to 5002 — no relay needed, just monitor
  echo "MusicGen output URL == rail output URL; MusicGen writes directly to 5002."
  echo "Rail will only start a fallback if MusicGen dies."
  MUSIC_INPUT_URL="udp://127.0.0.1:5012?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1"
  echo "Set MUSIC_INPUT_URL to ${MUSIC_INPUT_URL} (MusicGen should output here)"
  echo "Rail relays 5012 → 5002"
fi

audio_input_ready() {
  local url="$1"
  if ! command -v ffprobe >/dev/null 2>&1; then return 1; fi
  if command -v timeout >/dev/null 2>&1; then
    timeout 2s ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$url" >/dev/null 2>&1
    return $?
  fi
  ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$url" >/dev/null 2>&1
}

# Kill any existing rail
if [[ -f "$RAIL_PID_FILE" ]]; then
  OLD_PID="$(cat "$RAIL_PID_FILE" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill -9 "$OLD_PID" 2>/dev/null || true
  fi
fi
pkill -f '/tmp/fluxrt-audio-rail-loop' 2>/dev/null || true

# =============================================================================
# The rail loop: tries MusicGen first, falls back to radio, then silence.
# Never exits. If ffmpeg crashes, it restarts immediately.
# =============================================================================
cat > "$RAIL_LOOP" <<'RAIL_EOF'
#!/usr/bin/env bash
set -u

MUSIC_URL="MUSIC_URL_PLACEHOLDER"
RAIL_OUT="RAIL_OUT_PLACEHOLDER"
RADIO_URL="RADIO_FALLBACK_PLACEHOLDER"
BITRATE="BITRATE_PLACEHOLDER"

audio_ready() {
  local url="$1"
  if ! command -v ffprobe >/dev/null 2>&1; then return 1; fi
  if command -v timeout >/dev/null 2>&1; then
    timeout 2s ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$url" >/dev/null 2>&1
    return $?
  fi
  ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$url" >/dev/null 2>&1
}

relay_music() {
  # Pass MusicGen audio through, re-encoding to ensure consistent format
  ffmpeg -hide_banner -loglevel warning \
    -fflags +genpts+discardcorrupt+igndts+fastseek \
    -flags +global_header -err_detect ignore_err \
    -max_interleave_delta 0 \
    -thread_queue_size 32768 -use_wallclock_as_timestamps 1 \
    -i "$MUSIC_URL" \
    -map 0:a:0 \
    -af "aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=PTS-STARTPTS" \
    -c:a aac -b:a "$BITRATE" -ar 48000 -ac 2 \
    -async 1 -max_interleave_delta 0 \
    -fflags +genpts+discardcorrupt+igndts \
    -f mpegts "$RAIL_OUT"
}

relay_radio() {
  # Fall back to radio stream
  ffmpeg -hide_banner -loglevel warning \
    -fflags +genpts+discardcorrupt+igndts+fastseek \
    -flags +global_header -err_detect ignore_err \
    -max_interleave_delta 0 \
    -thread_queue_size 32768 -use_wallclock_as_timestamps 1 \
    -i "$RADIO_URL" \
    -map 0:a:0 \
    -af "aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=PTS-STARTPTS" \
    -c:a aac -b:a "$BITRATE" -ar 48000 -ac 2 \
    -async 1 -max_interleave_delta 0 \
    -fflags +genpts+discardcorrupt+igndts \
    -f mpegts "$RAIL_OUT"
}

relay_silence() {
  # Last resort: generate silence so the stream never drops
  ffmpeg -hide_banner -loglevel warning \
    -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000" \
    -af "asetpts=PTS-STARTPTS" \
    -c:a aac -b:a "$BITRATE" -ar 48000 -ac 2 \
    -async 1 -max_interleave_delta 0 \
    -fflags +genpts+discardcorrupt+igndts \
    -f mpegts "$RAIL_OUT"
}

# Main loop: never exit
while true; do
  # Try MusicGen first
  if audio_ready "$MUSIC_URL"; then
    echo "[$(date +%H:%M:%S)] Rail: relaying MusicGen audio"
    relay_music
    echo "[$(date +%H:%M:%S)] Rail: MusicGen stream ended or crashed"
  fi

  # Try radio fallback
  if audio_ready "$RADIO_URL"; then
    echo "[$(date +%H:%M:%S)] Rail: falling back to radio stream"
    relay_radio
    echo "[$(date +%H:%M:%S)] Rail: radio stream ended or crashed"
  fi

  # Last resort: silence (keeps the UDP port alive)
  echo "[$(date +%H:%M:%S)] Rail: generating silence (no audio source available)"
  relay_silence
  echo "[$(date +%H:%M:%S)] Rail: silence generator crashed, restarting..."

  sleep 0.5
done
RAIL_EOF

# Replace placeholders
sed -i \
  -e "s|MUSIC_URL_PLACEHOLDER|${MUSIC_INPUT_URL}|g" \
  -e "s|RAIL_OUT_PLACEHOLDER|${RAIL_OUTPUT_URL}|g" \
  -e "s|RADIO_FALLBACK_PLACEHOLDER|${RADIO_FALLBACK_URL}|g" \
  -e "s|BITRATE_PLACEHOLDER|${AUDIO_BITRATE}|g" \
  "$RAIL_LOOP"

chmod +x "$RAIL_LOOP"

nohup "$RAIL_LOOP" > "$RAIL_LOG" 2>&1 &
RAIL_PID=$!
echo "$RAIL_PID" > "$RAIL_PID_FILE"

echo "=== Continuous audio rail started ==="
echo "PID: $RAIL_PID"
echo "Music input: $MUSIC_INPUT_URL"
echo "Rail output: $RAIL_OUTPUT_URL"
echo "Radio fallback: $RADIO_FALLBACK_URL"
echo "Log: $RAIL_LOG"
echo ""
echo "The rail never stops. MusicGen → Radio → Silence fallback chain."
echo "TTS drops in non-blocking on top via the mix bus (UDP 5004 → 5006)."
