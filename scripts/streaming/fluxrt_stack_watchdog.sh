#!/usr/bin/env bash
# FluxRT stack watchdog — detects stuck fanout ffmpeg and forces reconnect.
# Runs as a long-lived loop (typically in a tmux window from start_stack.sh).
set -uo pipefail

WATCHDOG_LOG="${WATCHDOG_LOG:-/tmp/fluxrt-watchdog.log}"
PROGRESS_FILE="${PROGRESS_FILE:-/tmp/fluxrt-fanout-progress.txt}"
FANOUT_PID_FILE="${FANOUT_PID_FILE:-/tmp/fluxrt-rtmp-fanout.pid}"
CHECK_INTERVAL="${CHECK_INTERVAL:-10}"
STALL_THRESHOLD="${STALL_THRESHOLD:-25}"
MIN_RESTART_INTERVAL="${MIN_RESTART_INTERVAL:-60}"
STARTUP_GRACE="${STARTUP_GRACE:-45}"
GRADIO_PORT="${GRADIO_PORT:-7862}"
GRADIO_URL="${GRADIO_URL:-http://127.0.0.1:${GRADIO_PORT}/}"
CHECK_UDP_VIDEO="${CHECK_UDP_VIDEO:-1}"

last_restart=0
last_frame=""
last_frame_change=0
seen_frame=0

log() {
  echo "[$(date -Is)] $*" >> "$WATCHDOG_LOG"
}

get_progress_frame() {
  if [[ ! -f "$PROGRESS_FILE" ]]; then
    return
  fi
  grep -E '^frame=' "$PROGRESS_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' '
}

fanout_ffmpeg_running() {
  pgrep -f "ffmpeg.*-i udp://127.0.0.1:5000.*-f tee" >/dev/null 2>&1
}

check_gradio() {
  curl -sf -o /dev/null --max-time 5 "$GRADIO_URL" >/dev/null 2>&1
}

check_udp_video_flow() {
  if [[ "$CHECK_UDP_VIDEO" != "1" ]] || ! command -v ffprobe >/dev/null 2>&1; then
    return 0
  fi
  timeout 2 ffprobe -v error -analyzeduration 500000 -probesize 500000 \
    -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 \
    "udp://127.0.0.1:5000?pkt_size=1316&fifo_size=1000000&overrun_nonfatal=1&timeout=1500000" \
    >/dev/null 2>&1
}

kill_stuck_fanout_ffmpeg() {
  pkill -f "ffmpeg.*-i udp://127.0.0.1:5000.*-f tee" 2>/dev/null || true
  pkill -f "ffmpeg.*smptebars=size=.*-f tee" 2>/dev/null || true
  sleep 1
}

drain_udp_port() {
  local port="$1"
  timeout 0.5 ffmpeg -hide_banner -loglevel error \
    -timeout 200000 \
    -i "udp://127.0.0.1:${port}?pkt_size=1316&fifo_size=1000000&overrun_nonfatal=1" \
    -f null - 2>/dev/null || true
}

force_fanout_recovery() {
  local reason="$1"
  local now
  now=$(date +%s)
  if [[ "$last_restart" -gt 0 ]] && [[ $((now - last_restart)) -lt "$MIN_RESTART_INTERVAL" ]]; then
    log "skip recovery ($reason): min interval ${MIN_RESTART_INTERVAL}s not elapsed"
    return 1
  fi
  if ! fanout_ffmpeg_running; then
    log "skip recovery ($reason): no live fanout ffmpeg to kill"
    return 1
  fi

  log "RECOVERY: $reason — killing stuck fanout ffmpeg"
  kill_stuck_fanout_ffmpeg
  drain_udp_port 5000
  last_restart=$now
  last_frame=""
  last_frame_change=$now
  seen_frame=0
  return 0
}

log "watchdog started (interval=${CHECK_INTERVAL}s stall=${STALL_THRESHOLD}s min_restart=${MIN_RESTART_INTERVAL}s startup_grace=${STARTUP_GRACE}s)"

while true; do
  now=$(date +%s)

  if ! check_gradio; then
    log "WARN: Gradio not responding at $GRADIO_URL"
  fi

  fanout_loop_alive=0
  if [[ -f "$FANOUT_PID_FILE" ]]; then
    fanout_pid="$(cat "$FANOUT_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$fanout_pid" ]] && kill -0 "$fanout_pid" 2>/dev/null; then
      fanout_loop_alive=1
    fi
  fi

  if [[ "$fanout_loop_alive" -eq 0 ]]; then
    log "INFO: fanout reconnect loop not running (no PID file or dead)"
    seen_frame=0
    last_frame=""
    last_frame_change=0
    sleep "$CHECK_INTERVAL"
    continue
  fi

  if ! fanout_ffmpeg_running; then
    # Reconnect loop is between ffmpeg attempts — do not interfere.
    seen_frame=0
    last_frame=""
    last_frame_change=0
    sleep "$CHECK_INTERVAL"
    continue
  fi

  frame="$(get_progress_frame || true)"
  if [[ -n "$frame" && "$frame" =~ ^[0-9]+$ ]]; then
    seen_frame=1
    if [[ "$frame" != "$last_frame" ]]; then
      last_frame="$frame"
      last_frame_change=$now
    elif [[ "$last_frame_change" -gt 0 ]] && [[ $((now - last_frame_change)) -ge "$STALL_THRESHOLD" ]]; then
      force_fanout_recovery "frame stalled at ${frame} for ${STALL_THRESHOLD}s" || true
    fi
  elif [[ "$seen_frame" -eq 1 ]] && [[ "$last_frame_change" -gt 0 ]] && [[ $((now - last_frame_change)) -ge "$STALL_THRESHOLD" ]]; then
    force_fanout_recovery "progress file stale after frame=${last_frame}" || true
  fi

  if [[ "$CHECK_UDP_VIDEO" == "1" ]] && [[ "$seen_frame" -eq 1 ]] && [[ -n "$last_frame" ]]; then
    if [[ $((now - last_frame_change)) -ge 10 ]] && ! check_udp_video_flow; then
      log "WARN: UDP 5000 video probe failed while fanout frame=${last_frame}"
    fi
  fi

  sleep "$CHECK_INTERVAL"
done
