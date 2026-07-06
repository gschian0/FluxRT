#!/usr/bin/env bash
# FluxRT stack watchdog — detects stuck fanout ffmpeg and forces reconnect.
# Supports legacy RTMP tee fanout and MediaMTX ingest/egress paths.
set -uo pipefail

WATCHDOG_LOG="${WATCHDOG_LOG:-/tmp/fluxrt-watchdog.log}"
CHECK_INTERVAL="${CHECK_INTERVAL:-10}"
STALL_THRESHOLD="${STALL_THRESHOLD:-25}"
MIN_RESTART_INTERVAL="${MIN_RESTART_INTERVAL:-60}"
STARTUP_GRACE="${STARTUP_GRACE:-45}"
GRADIO_PORT="${GRADIO_PORT:-7862}"
GRADIO_URL="${GRADIO_URL:-http://127.0.0.1:${GRADIO_PORT}/}"
CHECK_UDP_VIDEO="${CHECK_UDP_VIDEO:-1}"
FANOUT_MODE="${FANOUT_MODE:-auto}"

# Legacy RTMP fanout
RTMP_PROGRESS_FILE="${RTMP_PROGRESS_FILE:-/tmp/fluxrt-fanout-progress.txt}"
RTMP_FANOUT_PID_FILE="${RTMP_FANOUT_PID_FILE:-/tmp/fluxrt-rtmp-fanout.pid}"

# MediaMTX fanout
INGEST_PROGRESS="${INGEST_PROGRESS:-/tmp/fluxrt-mediamtx-ingest.progress}"
EGRESS_PROGRESS="${EGRESS_PROGRESS:-/tmp/fluxrt-mediamtx-egress.progress}"
INGEST_PID_FILE="${INGEST_PID_FILE:-/tmp/fluxrt-mediamtx-ingest.pid}"
EGRESS_PID_FILE="${EGRESS_PID_FILE:-/tmp/fluxrt-mediamtx-egress.pid}"
MEDIAMTX_PID_FILE="${MEDIAMTX_PID_FILE:-/tmp/fluxrt-mediamtx.pid}"
MEDIAMTX_ENV_FILE="${MEDIAMTX_ENV_FILE:-scripts/streaming/rtmp_targets.env}"
REPO_ROOT="${REPO_ROOT:-/workspace/FluxRT}"

last_restart=0
ingest_last_frame=""
ingest_last_change=0
ingest_seen_frame=0
egress_last_frame=""
egress_last_change=0
egress_seen_frame=0
rtmp_last_frame=""
rtmp_last_change=0
rtmp_seen_frame=0

log() {
  echo "[$(date -Is)] $*" >> "$WATCHDOG_LOG"
}

pid_is_alive() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

get_progress_frame() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    return
  fi
  local frame
  frame="$(grep -E '^frame=' "$file" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ')"
  if [[ -n "$frame" && "$frame" =~ ^[0-9]+$ ]]; then
    echo "$frame"
    return
  fi
  local out_ms
  out_ms="$(grep -E '^out_time_ms=' "$file" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ')"
  if [[ -n "$out_ms" && "$out_ms" =~ ^[0-9]+$ ]]; then
    echo "$out_ms"
  fi
}

detect_fanout_mode() {
  if [[ "$FANOUT_MODE" != "auto" ]]; then
    echo "$FANOUT_MODE"
    return
  fi
  if pid_is_alive "$INGEST_PID_FILE" || pid_is_alive "$MEDIAMTX_PID_FILE"; then
    echo "mediamtx"
    return
  fi
  if pid_is_alive "$RTMP_FANOUT_PID_FILE"; then
    echo "rtmp"
    return
  fi
  if pgrep -f "ffmpeg.*rtmp://127.0.0.1:1935/fluxrt" >/dev/null 2>&1; then
    echo "mediamtx"
    return
  fi
  if pgrep -f "ffmpeg.*-i udp://127.0.0.1:5000.*-f tee" >/dev/null 2>&1; then
    echo "rtmp"
    return
  fi
  echo "none"
}

rtmp_port_is_listening() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn | grep -qE '(:1935 |:1935$)'
    return $?
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -q ':1935'
    return $?
  fi
  (echo > /dev/tcp/127.0.0.1/1935) >/dev/null 2>&1
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

drain_udp_port() {
  local port="$1"
  timeout 0.5 ffmpeg -hide_banner -loglevel error \
    -rw_timeout 200000 \
    -i "udp://127.0.0.1:${port}?pkt_size=1316&fifo_size=1000000&overrun_nonfatal=1" \
    -f null - 2>/dev/null || true
}

kill_stuck_rtmp_fanout_ffmpeg() {
  pkill -f "ffmpeg.*-i udp://127.0.0.1:5000.*-f tee" 2>/dev/null || true
  pkill -f "ffmpeg.*smptebars=size=.*-f tee" 2>/dev/null || true
  sleep 1
}

rtmp_fanout_ffmpeg_running() {
  pgrep -f "ffmpeg.*-i udp://127.0.0.1:5000.*-f tee" >/dev/null 2>&1
}

mediamtx_ingest_ffmpeg_running() {
  pgrep -f "ffmpeg.*rtmp://127.0.0.1:1935/fluxrt" | head -1 >/dev/null 2>&1
}

mediamtx_egress_ffmpeg_running() {
  pgrep -f "ffmpeg.*-i rtmp://127.0.0.1:1935/fluxrt" >/dev/null 2>&1
}

can_restart_now() {
  local now
  now=$(date +%s)
  if [[ "$last_restart" -gt 0 ]] && [[ $((now - last_restart)) -lt "$MIN_RESTART_INTERVAL" ]]; then
    return 1
  fi
  return 0
}

force_mediamtx_recovery() {
  local reason="$1"
  if ! can_restart_now; then
    log "skip mediamtx recovery ($reason): min interval ${MIN_RESTART_INTERVAL}s not elapsed"
    return 1
  fi
  log "RECOVERY: $reason — restarting MediaMTX fanout stack"
  (
    cd "$REPO_ROOT" || exit 1
    scripts/streaming/stop_mediamtx_fanout.sh >/dev/null 2>&1 || true
    drain_udp_port 5000
    sleep 2
    # shellcheck disable=SC1090
    [[ -f /workspace/.stack-profile.env ]] && set -a && source /workspace/.stack-profile.env && set +a
    VIDEO_SOURCE_MODE="${VIDEO_SOURCE_MODE:-wait}" \
    VIDEO_WAIT_TIMEOUT="${VIDEO_WAIT_TIMEOUT:-120}" \
    OUTPUT_WIDTH="${OUTPUT_WIDTH:-288}" OUTPUT_HEIGHT="${OUTPUT_HEIGHT:-160}" \
    FPS="${FANOUT_FPS:-${BROADCAST_FPS:-8}}" \
    ENABLE_TWITCH="${ENABLE_TWITCH:-1}" ENABLE_YOUTUBE=0 ENABLE_FACEBOOK=0 \
    ENABLE_TTS_OVERLAY="${ENABLE_TTS_OVERLAY:-1}" AUDIO_SOURCE_MODE=url TTS_SOURCE_MODE=url \
    AUDIO_INPUT_URL='udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1' \
    TTS_INPUT_URL='udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1' \
    MUSIC_MIX_VOLUME="${MUSIC_MIX_VOLUME:-0.85}" TTS_MIX_VOLUME="${TTS_MIX_VOLUME:-1.55}" \
    VIDEO_TRANSCODE_MODE="${VIDEO_TRANSCODE_MODE:-copy}" VIDEO_BITRATE=550k VIDEO_MAXRATE=550k VIDEO_BUFSIZE=1100k \
    AUDIO_BITRATE=128k MUSIC_WAIT_TIMEOUT=30 TTS_WAIT_TIMEOUT=40 \
    bash scripts/streaming/start_mediamtx_fanout.sh "$MEDIAMTX_ENV_FILE" >> "$WATCHDOG_LOG" 2>&1 || true
  )
  last_restart=$(date +%s)
  ingest_last_frame=""
  ingest_last_change=$last_restart
  ingest_seen_frame=0
  egress_last_frame=""
  egress_last_change=$last_restart
  egress_seen_frame=0
  return 0
}

force_rtmp_recovery() {
  local reason="$1"
  if ! can_restart_now; then
    log "skip rtmp recovery ($reason): min interval ${MIN_RESTART_INTERVAL}s not elapsed"
    return 1
  fi
  if ! rtmp_fanout_ffmpeg_running; then
    log "skip rtmp recovery ($reason): no live fanout ffmpeg to kill"
    return 1
  fi
  log "RECOVERY: $reason — killing stuck RTMP fanout ffmpeg"
  kill_stuck_rtmp_fanout_ffmpeg
  drain_udp_port 5000
  last_restart=$(date +%s)
  rtmp_last_frame=""
  rtmp_last_change=$last_restart
  rtmp_seen_frame=0
  return 0
}

check_progress_stall() {
  local label="$1"
  local progress_file="$2"
  local -n last_frame_ref="$3"
  local -n last_change_ref="$4"
  local -n seen_frame_ref="$5"
  local recovery_fn="$6"
  local now
  now=$(date +%s)

  local frame
  frame="$(get_progress_frame "$progress_file" || true)"
  if [[ -n "$frame" && "$frame" =~ ^[0-9]+$ ]]; then
    seen_frame_ref=1
    if [[ "$frame" != "$last_frame_ref" ]]; then
      last_frame_ref="$frame"
      last_change_ref=$now
    elif [[ "$last_change_ref" -gt 0 ]] && [[ $((now - last_change_ref)) -ge "$STALL_THRESHOLD" ]]; then
      "$recovery_fn" "${label} frame stalled at ${frame} for ${STALL_THRESHOLD}s" || true
    fi
  elif [[ "$seen_frame_ref" -eq 1 ]] && [[ "$last_change_ref" -gt 0 ]] && [[ $((now - last_change_ref)) -ge "$STALL_THRESHOLD" ]]; then
    "$recovery_fn" "${label} progress file stale after frame=${last_frame_ref}" || true
  fi
}

log "watchdog started (mode=${FANOUT_MODE} interval=${CHECK_INTERVAL}s stall=${STALL_THRESHOLD}s min_restart=${MIN_RESTART_INTERVAL}s startup_grace=${STARTUP_GRACE}s)"

while true; do
  now=$(date +%s)
  mode="$(detect_fanout_mode)"

  if ! check_gradio; then
    log "WARN: Gradio not responding at $GRADIO_URL"
  fi

  if [[ "$mode" == "mediamtx" ]]; then
    if ! rtmp_port_is_listening; then
      log "WARN: MediaMTX port 1935 not listening"
      if pid_is_alive "$INGEST_PID_FILE" || pid_is_alive "$EGRESS_PID_FILE"; then
        force_mediamtx_recovery "MediaMTX port down" || true
      fi
      sleep "$CHECK_INTERVAL"
      continue
    fi

    if pid_is_alive "$INGEST_PID_FILE"; then
      if mediamtx_ingest_ffmpeg_running; then
        check_progress_stall "ingest" "$INGEST_PROGRESS" ingest_last_frame ingest_last_change ingest_seen_frame force_mediamtx_recovery
      fi
    else
      ingest_seen_frame=0
      ingest_last_frame=""
      ingest_last_change=0
    fi

    if pid_is_alive "$EGRESS_PID_FILE"; then
      if mediamtx_egress_ffmpeg_running; then
        check_progress_stall "egress" "$EGRESS_PROGRESS" egress_last_frame egress_last_change egress_seen_frame force_mediamtx_recovery
      fi
    else
      egress_seen_frame=0
      egress_last_frame=""
      egress_last_change=0
    fi

    if [[ "$CHECK_UDP_VIDEO" == "1" ]] && [[ "$ingest_seen_frame" -eq 1 ]] && [[ -n "$ingest_last_frame" ]]; then
      if [[ $((now - ingest_last_change)) -ge 10 ]] && ! check_udp_video_flow; then
        log "WARN: UDP 5000 video probe failed while ingest frame=${ingest_last_frame}"
      fi
    fi

  elif [[ "$mode" == "rtmp" ]]; then
    fanout_loop_alive=0
    if pid_is_alive "$RTMP_FANOUT_PID_FILE"; then
      fanout_loop_alive=1
    fi

    if [[ "$fanout_loop_alive" -eq 0 ]]; then
      log "INFO: RTMP fanout reconnect loop not running"
      rtmp_seen_frame=0
      rtmp_last_frame=""
      rtmp_last_change=0
      sleep "$CHECK_INTERVAL"
      continue
    fi

    if ! rtmp_fanout_ffmpeg_running; then
      rtmp_seen_frame=0
      rtmp_last_frame=""
      rtmp_last_change=0
      sleep "$CHECK_INTERVAL"
      continue
    fi

    check_progress_stall "rtmp" "$RTMP_PROGRESS_FILE" rtmp_last_frame rtmp_last_change rtmp_seen_frame force_rtmp_recovery

    if [[ "$CHECK_UDP_VIDEO" == "1" ]] && [[ "$rtmp_seen_frame" -eq 1 ]] && [[ -n "$rtmp_last_frame" ]]; then
      if [[ $((now - rtmp_last_change)) -ge 10 ]] && ! check_udp_video_flow; then
        log "WARN: UDP 5000 video probe failed while rtmp frame=${rtmp_last_frame}"
      fi
    fi
  else
    log "INFO: no active fanout detected (mode=none)"
  fi

  sleep "$CHECK_INTERVAL"
done
