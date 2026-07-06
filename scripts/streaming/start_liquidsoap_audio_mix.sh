#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

MIX_LOG="${MIX_LOG:-/tmp/fluxrt-liquidsoap-mix.log}"
MIX_PID="${MIX_PID:-/tmp/fluxrt-audio-mix.pid}"
LIQ_SCRIPT="${LIQ_SCRIPT:-$SCRIPT_DIR/fluxrt-audio-mix.liq}"

MIX_OUTPUT_URL="${MIX_OUTPUT_URL:-udp://127.0.0.1:5006?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
MUSIC_INPUT_URL="${MUSIC_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
TTS_INPUT_URL="${TTS_INPUT_URL:-udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
MUSIC_MIX_VOLUME="${MUSIC_MIX_VOLUME:-0.85}"
TTS_MIX_VOLUME="${TTS_MIX_VOLUME:-1.55}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
FORCE_RESTART="${FORCE_RESTART:-0}"

pid_is_running() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

if ! command -v liquidsoap >/dev/null 2>&1; then
  echo "liquidsoap not installed."
  exit 1
fi

if [[ "$FORCE_RESTART" != "1" ]] && [[ -f "$MIX_PID" ]]; then
  existing_pid="$(cat "$MIX_PID" 2>/dev/null || true)"
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && pid_is_running "$existing_pid"; then
    echo "Liquidsoap audio mix already running (PID $existing_pid)."
    exit 0
  fi
fi

scripts/streaming/stop_audio_mix_bus.sh >/dev/null 2>&1 || true

RUN_LIQ="/tmp/fluxrt-audio-mix-run.liq"
sed \
  -e "s|@MUSIC_VOL@|${MUSIC_MIX_VOLUME}|g" \
  -e "s|@TTS_VOL@|${TTS_MIX_VOLUME}|g" \
  -e "s|@AUDIO_BITRATE@|${AUDIO_BITRATE}|g" \
  -e "s|@MIX_OUTPUT_URL@|${MIX_OUTPUT_URL}|g" \
  -e "s|@MUSIC_INPUT_URL@|${MUSIC_INPUT_URL}|g" \
  -e "s|@TTS_INPUT_URL@|${TTS_INPUT_URL}|g" \
  "$LIQ_SCRIPT" > "$RUN_LIQ"

if ! liquidsoap --check "$RUN_LIQ" >/dev/null 2>&1; then
  echo "Liquidsoap script check failed for $RUN_LIQ"
  liquidsoap --check "$RUN_LIQ" || true
  exit 1
fi

export MUSIC_MIX_VOLUME TTS_MIX_VOLUME AUDIO_BITRATE MIX_OUTPUT_URL MUSIC_INPUT_URL TTS_INPUT_URL

nohup liquidsoap "$RUN_LIQ" > "$MIX_LOG" 2>&1 &
echo "$!" > "$MIX_PID"

sleep 1
if pid_is_running "$(cat "$MIX_PID")"; then
  echo "Liquidsoap audio mix started."
  echo "PID: $(cat "$MIX_PID")"
  echo "Output: $MIX_OUTPUT_URL"
  echo "Log: $MIX_LOG"
  exit 0
fi

echo "Liquidsoap audio mix failed to start. Check log: $MIX_LOG"
exit 1
