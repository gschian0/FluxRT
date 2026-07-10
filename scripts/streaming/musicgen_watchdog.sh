#!/usr/bin/env bash
# MusicGen watchdog — restarts MusicGen if it dies or if realtime_factor > 1.0
set -u

REPO_ROOT="/workspace/FluxRT"
LOG="/dev/shm/musicgen/musicgen.log"
WATCHDOG_LOG="/tmp/fluxrt-musicgen-watchdog.log"
PID_FILE="/tmp/fluxrt-musicgen.pid"
MAX_RT_FACTOR=1.05  # restart if consistently slower than realtime

log() { echo "[$(date -Is)] $*" >> "$WATCHDOG_LOG"; }

get_musicgen_pid() {
  pgrep -f 'run_musicgen_radio_plus_musicGEN.py' 2>/dev/null | head -1
}

start_musicgen() {
  log "starting MusicGen..."
  rm -f /dev/shm/musicgen/musicgen_clip_*.wav /dev/shm/musicgen/now_marker.txt 2>/dev/null
  cd "$REPO_ROOT"
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 CUDA_VISIBLE_DEVICES=1 \
    nohup setsid taskset -c 16-63 .venv/bin/python3 -u scripts/run_musicgen_radio_plus_musicGEN.py \
    --radio-url "https://ice64.securenetsystems.net/LFTM" \
    --output-dir /dev/shm/musicgen \
    --model facebook/musicgen-small \
    --gen-seconds 16 \
    --top-k 250 --top-p 0.95 --temperature 1.0 --guidance-scale 3.0 \
    --drunk-walk --drunk-walk-strength 1.0 \
    --parallel-clips 1 --seed -1 \
    --bootstrap-clips 24 --pre-generate 3 --bpm 120 \
    --pause-seconds 0 --base-prompt "" \
    --conditioning-mode continuation --conditioning-seconds 8 \
    --stream-delay-seconds 20 \
    --audio-udp-url "udp://127.0.0.1:5002?pkt_size=1316" \
    --crossfade-seconds 2.0 \
    > "$LOG" 2>&1 & disown
  local pid=$!
  echo "$pid" > "$PID_FILE"
  log "MusicGen started PID=$pid"
  sleep 60  # wait for model to load
}

check_rt_factor() {
  # Check last 5 realtime_factor entries — if all > 1.0, MusicGen can't keep up
  local factors
  factors=$(grep 'realtime_factor' "$LOG" 2>/dev/null | tail -5 | grep -oP 'realtime_factor=\K[0-9.]+')
  if [[ -z "$factors" ]]; then
    return 0  # not enough data yet
  fi
  local slow=0
  while read -r f; do
    if (( $(echo "$f > $MAX_RT_FACTOR" | bc -l 2>/dev/null || echo 0) )); then
      slow=$((slow + 1))
    fi
  done <<< "$factors"
  if [[ "$slow" -ge 4 ]]; then
    log "WARNING: $slow/5 last clips slower than realtime (factor > $MAX_RT_FACTOR)"
    return 1
  fi
  return 0
}

log "watchdog started"

while true; do
  pid=$(get_musicgen_pid)
  if [[ -z "$pid" ]]; then
    log "MusicGen not running — restarting"
    start_musicgen
    continue
  fi

  # Check if process is actually alive (not zombie)
  if ! kill -0 "$pid" 2>/dev/null; then
    log "MusicGen PID $pid not responding — restarting"
    start_musicgen
    continue
  fi

  # Check realtime factor (only after enough clips generated)
  clip_count=$(grep -c 'realtime_factor' "$LOG" 2>/dev/null || echo 0)
  if [[ "$clip_count" -ge 5 ]]; then
    if ! check_rt_factor; then
      log "MusicGen too slow — killing and restarting"
      kill -9 "$pid" 2>/dev/null
      sleep 2
      start_musicgen
      continue
    fi
  fi

  sleep 30
done
