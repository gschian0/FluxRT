#!/usr/bin/env bash
# FluxRT Pipeline Watchdog
# Monitors all pipeline components and restarts them if they die.
# Also detects audio silence on UDP 5002 and restarts MusicGen's ffmpeg if needed.
#
# Usage: nohup setsid bash scripts/streaming/watchdog.sh > /tmp/fluxrt-watchdog.log 2>&1 & disown

set -u

LOG="/tmp/fluxrt-watchdog.log"
RESTART_COUNT=0
MUSICGEN_START_TIME=0
MUSICGEN_GRACE_PERIOD=360  # 6 minutes: model load (~30s) + pre-gen 10 clips (~200s) + buffer

log() {
    echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"
}

check_process() {
    local name="$1"
    local pattern="$2"
    local pid
    pid=$(pgrep -f "$pattern" 2>/dev/null | head -1)
    if [ -z "$pid" ]; then
        log "❌ $name is DOWN (no process matching: $pattern)"
        return 1
    else
        log "✅ $name is UP (PID $pid)"
        return 0
    fi
}

restart_mediamtx() {
    log "Restarting MediaMTX..."
    pkill -9 -f "mediamtx /tmp/fluxrt-mediamtx.yml" 2>/dev/null
    sleep 2
    cd /workspace/FluxRT
    nohup setsid /workspace/FluxRT/tools/mediamtx/mediamtx /tmp/fluxrt-mediamtx.yml \
        > /tmp/fluxrt-mediamtx.log 2>&1 & disown
    sleep 3
    log "MediaMTX restarted (PID $!)"
}

restart_ingest() {
    log "Restarting ingest ffmpeg..."
    pkill -9 -f "amix=inputs=3" 2>/dev/null
    sleep 2
    nohup setsid ffmpeg -hide_banner -loglevel error \
        -fflags +genpts+discardcorrupt+igndts \
        -thread_queue_size 16384 -i "udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
        -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \
        -thread_queue_size 16384 -i "udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
        -thread_queue_size 16384 -i "udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
        -map 0:v:0 -map "[aout]" \
        -filter_complex "[1:a]volume=1.0[base];[2:a]volume=1.3[music];[3:a]volume=0.85[tts];[base][music][tts]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0,aresample=async=1:min_hard_comp=0.100:first_pts=0[aout]" \
        -c:v copy -c:a aac -b:a 96k -ar 48000 -ac 2 \
        -max_muxing_queue_size 4096 -muxdelay 0 -muxpreload 0 \
        -f flv "rtmp://127.0.0.1:1935/fluxrt" \
        > /tmp/fluxrt-mediamtx-ingest.log 2>&1 & disown
    sleep 3
    log "Ingest ffmpeg restarted (PID $!)"
}

restart_egress() {
    log "Restarting egress ffmpeg to Twitch..."
    pkill -9 -f "twitch.tv" 2>/dev/null
    sleep 2
    # Wait for ingest to be publishing before connecting egress
    sleep 5
    nohup setsid ffmpeg -hide_banner -loglevel error \
        -fflags +genpts+discardcorrupt+igndts \
        -i "rtmp://127.0.0.1:1935/fluxrt" \
        -c copy -f flv "rtmps://live.twitch.tv:443/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw" \
        > /tmp/fluxrt-mediamtx-egress.log 2>&1 & disown
    sleep 3
    log "Egress ffmpeg restarted (PID $!)"
}

restart_musicgen() {
    log "Restarting MusicGen..."
    pkill -9 -f "run_musicgen_radio" 2>/dev/null
    sleep 3
    cd /workspace/FluxRT
    CUDA_VISIBLE_DEVICES=1 nohup setsid taskset -c 16-63 \
        /workspace/FluxRT/.venv/bin/python3 -u scripts/run_musicgen_radio_plus_musicGEN.py \
        --radio-url "https://ice64.securenetsystems.net/LFTM" \
        --output-dir /dev/shm/musicgen \
        --model facebook/musicgen-small \
        --gen-seconds 16 \
        --top-k 250 --top-p 0.95 --temperature 1.0 --guidance-scale 3.0 \
        --no-drunk-walk --parallel-clips 2 --seed -1 \
        --bootstrap-clips 24 --pre-generate 10 --bpm 120 \
        --pause-seconds 0 --base-prompt "" \
        --conditioning-mode continuation --conditioning-seconds 8 \
        --stream-delay-seconds 120 \
        --audio-udp-url "udp://127.0.0.1:5002?pkt_size=1316" \
        --crossfade-seconds 2.0 \
        > /dev/shm/musicgen/musicgen.log 2>&1 & disown
    MUSICGEN_START_TIME=$(date +%s)
    sleep 5
    log "MusicGen restarted (PID $!) — grace period ${MUSICGEN_GRACE_PERIOD}s"
}

restart_tts() {
    log "Restarting TTS..."
    pkill -9 -f "run_edge_tts_quotes" 2>/dev/null
    sleep 2
    cd /workspace/FluxRT
    nohup setsid /workspace/FluxRT/.venv/bin/python3 -u scripts/run_edge_tts_quotes.py \
        --quotes data/quotes/diffusiongemma_quotes.json \
        --loop --shuffle --interval 15 --random-voices \
        --reverb --reverb-mix 0.35 --reverb-decay 0.50 --reverb-delay-ms 60 \
        --echo --echo-mix 0.45 --echo-decay 0.65 --echo-delay-ms 150 \
        --udp-url "udp://127.0.0.1:5004?pkt_size=1316" \
        --repeat-on-empty --cache-dir voices/quote_cache_edge --cache-size 4 \
        > /tmp/fluxrt-tts.log 2>&1 & disown
    sleep 3
    log "TTS restarted (PID $!)"
}

restart_gradio() {
    log "Restarting Gradio..."
    pkill -9 -f "gradio_stream_demo" 2>/dev/null
    pkill -9 -f "multiprocessing.spawn" 2>/dev/null
    sleep 5
    cd /workspace/FluxRT
    GRADIO_SHARE=1 nohup setsid taskset -c 0-15 \
        /root/fluxrt-venv/bin/python -u scripts/run_gradio_stream_demo.py \
        --int8 --server-name 0.0.0.0 --server-port 7862 \
        --config-path configs/stream_demo_config.json \
        --local-video /workspace/test_input.mp4 \
        > /tmp/fluxrt-gradio.log 2>&1 & disown
    sleep 10
    log "Gradio restarted (PID $!)"
}

# Check if MusicGen is actually producing audio (not just running)
# by checking if the now_playing file is being updated
check_musicgen_alive() {
    local now_playing="/dev/shm/musicgen/now_playing_path.txt"
    local musicgen_log="/dev/shm/musicgen/musicgen.log"
    local now=$(date +%s)
    local uptime=$((now - MUSICGEN_START_TIME))

    # Grace period: MusicGen takes ~120s to load model + pre-gen + bootstrap.
    # Don't check now_playing during startup — it won't exist yet.
    if [ $uptime -lt $MUSICGEN_GRACE_PERIOD ]; then
        log "   MusicGen booting (${uptime}/${MUSICGEN_GRACE_PERIOD}s grace period)"
        return 0
    fi

    if [ ! -f "$now_playing" ]; then
        log "⚠️  MusicGen: now_playing file missing (after ${uptime}s)"
        return 1
    fi

    # Check if now_playing file was modified in the last 120 seconds
    local mtime=$(stat -c %Y "$now_playing" 2>/dev/null || echo 0)
    local age=$((now - mtime))

    if [ $age -gt 120 ]; then
        log "⚠️  MusicGen: now_playing file is ${age}s old — playback may be stuck!"
        return 1
    fi

    # Check for underruns in the last 50 lines of log
    local recent_underruns
    recent_underruns=$(tail -50 "$musicgen_log" 2>/dev/null | grep -c "underrun" 2>/dev/null || true)
    recent_underruns=${recent_underruns:-0}
    if [ "${recent_underruns:-0}" -gt 5 ]; then
        log "⚠️  MusicGen: $recent_underruns underruns in recent log — buffer running low"
    fi

    # Check RTF trend
    local last_rtf
    last_rtf=$(grep "realtime_factor" "$musicgen_log" 2>/dev/null | tail -1 | grep -oP 'realtime_factor=\K[0-9.]+')
    if [ -n "$last_rtf" ]; then
        log "   MusicGen RTF=$last_rtf, now_playing age=${age}s"
    fi

    return 0
}

# Check if the MusicGen ffmpeg (audio pipe to UDP 5002) is alive
check_musicgen_ffmpeg() {
    local now=$(date +%s)
    local uptime=$((now - MUSICGEN_START_TIME))

    # Grace period: ffmpeg doesn't start until after model load + pre-gen
    if [ $uptime -lt $MUSICGEN_GRACE_PERIOD ]; then
        return 0
    fi

    local pid
    pid=$(pgrep -f "f32le.*udp://127.0.0.1:5002" 2>/dev/null | head -1)
    if [ -z "$pid" ]; then
        log "⚠️  MusicGen ffmpeg (UDP 5002) is DOWN — no audio reaching ingest!"
        return 1
    fi
    return 0
}

# Check if Gradio's video encoder is feeding UDP 5000
check_video_source() {
    local pid
    pid=$(pgrep -f "udp://127.0.0.1:5000" 2>/dev/null | head -1)
    if [ -z "$pid" ]; then
        log "⚠️  Video source (UDP 5000) is DOWN — no video reaching ingest!"
        return 1
    fi
    return 0
}

# If MusicGen is already running when watchdog starts, set grace timer to now
# so we don't immediately kill it (MUSICGEN_START_TIME defaults to 0 which makes
# uptime = epoch time, bypassing the grace period check).
if pgrep -f "run_musicgen_radio" >/dev/null 2>&1; then
    MUSICGEN_START_TIME=$(date +%s)
    log "MusicGen already running — grace timer set to now (${MUSICGEN_START_TIME})"
fi

log "=== FluxRT Pipeline Watchdog Started ==="
log "Monitoring: MediaMTX, Ingest, Egress, MusicGen, TTS, Gradio"
log "Check interval: 30 seconds"

while true; do
    log ""
    log "--- Health Check #$((++RESTART_COUNT)) ---"

    # Check each component
    mediamtx_ok=true
    ingest_ok=true
    egress_ok=true
    musicgen_ok=true
    musicgen_ffmpeg_ok=true
    tts_ok=true
    gradio_ok=true
    video_ok=true

    check_process "MediaMTX" "mediamtx /tmp/fluxrt-mediamtx.yml" || mediamtx_ok=false
    check_process "Ingest" "amix=inputs=3" || ingest_ok=false
    check_process "Egress" "twitch.tv" || egress_ok=false
    check_process "MusicGen" "run_musicgen_radio" || musicgen_ok=false
    check_process "TTS" "run_edge_tts_quotes" || tts_ok=false
    check_process "Gradio" "gradio_stream_demo" || gradio_ok=false

    # Deeper checks
    check_musicgen_alive || musicgen_ok=false
    check_musicgen_ffmpeg || musicgen_ffmpeg_ok=false
    check_video_source || video_ok=false

    # Auto-restart failed components (order matters!)
    if [ "$mediamtx_ok" = false ]; then
        restart_mediamtx
    fi

    if [ "$musicgen_ok" = false ]; then
        restart_musicgen
    fi

    if [ "$tts_ok" = false ]; then
        restart_tts
    fi

    if [ "$gradio_ok" = false ]; then
        restart_gradio
    fi

    if [ "$ingest_ok" = false ]; then
        restart_ingest
    fi

    if [ "$egress_ok" = false ]; then
        restart_egress
    fi

    # If MusicGen is running but its ffmpeg pipe died, we can't easily restart
    # just the ffmpeg — the playback worker should handle it. But if it doesn't,
    # restart the whole MusicGen process.
    if [ "$musicgen_ok" = true ] && [ "$musicgen_ffmpeg_ok" = false ]; then
        log "MusicGen is running but its audio ffmpeg died — restarting MusicGen"
        restart_musicgen
    fi

    # Check for broken audio pipe: MusicGen + ffmpeg both running but
    # stream audio is silent (pipe broken between Python and ffmpeg).
    # Only check after grace period to avoid false positives during boot.
    if [ "$musicgen_ok" = true ] && [ "$musicgen_ffmpeg_ok" = true ]; then
        _now=$(date +%s)
        _uptime=$((_now - MUSICGEN_START_TIME))
        if [ $_uptime -gt $MUSICGEN_GRACE_PERIOD ]; then
            _mean_vol=$(timeout 12 ffmpeg -hide_banner -loglevel error \
                -i "rtmp://127.0.0.1:1935/fluxrt" \
                -t 5 -af "volumedetect" -f null - 2>&1 \
                | grep -oP 'mean_volume: \K[0-9.-]+')
            if [ -n "$_mean_vol" ]; then
                # Compare as integers (multiply by 10 and truncate)
                _vol_int=$(echo "$_mean_vol" | awk '{printf "%d", $1 * 10}')
                # -60 dB = -600, -80 dB = -800. Below -600 (i.e. more negative) = silence
                if [ "$_vol_int" -lt -600 ] 2>/dev/null; then
                    log "⚠️  Audio silence detected (mean_volume=${_mean_vol}dB) — MusicGen pipe broken, restarting"
                    restart_musicgen
                    # Also restart ingest since it may have a stale connection
                    restart_ingest
                fi
            fi
        fi
    fi

    # If video source died but Gradio is running, Gradio may need a kick
    if [ "$gradio_ok" = true ] && [ "$video_ok" = false ]; then
        log "Gradio is running but not feeding video — will check again next cycle"
    fi

    log "--- Health check complete ---"

    sleep 30
done
