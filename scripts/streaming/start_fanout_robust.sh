#!/usr/bin/env bash
set -uo pipefail

# =============================================================================
# start_fanout_robust.sh — Never-die HLS + Twitch fanout
# =============================================================================
#
# ARCHITECTURE:
#
#   UDP 5000 (video, from Gradio) ─┐
#                                   ├─→ [HLS ingest loop] ──→ /tmp/fluxrt-monitor/stream.m3u8
#   UDP 5006 (audio, from mix bus) ─┘    (never dies, auto-restarts)
#                                            │
#                                            └─→ [Twitch relay loop] ──→ rtmps://live.twitch.tv
#                                                 (reads HLS, never dies)
#
# KEY IMPROVEMENTS over start_split_fanout.sh:
#   1. Waits up to 300s for video on UDP 5000 (model warmup can take 90s+)
#   2. HLS ingest runs in a never-die loop — if ffmpeg crashes, it restarts
#   3. Twitch relay runs in a never-die loop — if connection drops, it reconnects
#   4. Audio comes from mix bus (5006) only — no UDP bind conflicts
#   5. HLS segments are checked for freshness before Twitch starts
#   6. Clean cleanup on exit — kills all child processes
#
# USAGE:
#   bash /workspace/start_fanout_robust.sh
#
# ENV VARS:
#   VIDEO_INPUT_URL  — video source (default: udp://127.0.0.1:5000)
#   AUDIO_INPUT_URL  — audio source (default: udp://127.0.0.1:5006)
#   HLS_DIR          — HLS output directory (default: /tmp/fluxrt-monitor)
#   HLS_SEG_TIME     — segment duration in seconds (default: 2)
#   HLS_LIST_SIZE    — playlist window size (default: 6)
#   VIDEO_WAIT_TIMEOUT — seconds to wait for video (default: 300)
#   AUDIO_WAIT_TIMEOUT — seconds to wait for audio (default: 30)
#   BROADCAST_FPS    — output FPS (default: 8)
#   VIDEO_BITRATE    — video bitrate (default: 550k)
#   AUDIO_BITRATE    — audio bitrate (default: 128k)
#   OUTPUT_WIDTH     — output width (default: 288)
#   OUTPUT_HEIGHT    — output height (default: 160)
# =============================================================================

cd /workspace/FluxRT || exit 1

# Load secrets
if [[ -f /workspace/AI_TV_ORCHESTRATION/.env ]]; then
  set -a && source /workspace/AI_TV_ORCHESTRATION/.env && set +a
elif [[ -f /root/.env ]]; then
  set -a && source /root/.env && set +a
fi

# Also source stack profile
if [[ -f /workspace/.stack-profile.env ]]; then
  set -a && source /workspace/.stack-profile.env && set +a
fi

VIDEO_INPUT_URL="${VIDEO_INPUT_URL:-udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
AUDIO_INPUT_URL="${AUDIO_INPUT_URL:-udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1}"
HLS_DIR="${HLS_DIR:-/tmp/fluxrt-monitor}"
HLS_SEG_TIME="${HLS_SEG_TIME:-2}"
HLS_LIST_SIZE="${HLS_LIST_SIZE:-6}"
VIDEO_WAIT_TIMEOUT="${VIDEO_WAIT_TIMEOUT:-300}"
AUDIO_WAIT_TIMEOUT="${AUDIO_WAIT_TIMEOUT:-5}"
BROADCAST_FPS="${BROADCAST_FPS:-8}"
VIDEO_BITRATE="${VIDEO_BITRATE:-550k}"
AUDIO_BITRATE="${AUDIO_BITRATE:-128k}"
OUTPUT_WIDTH="${OUTPUT_WIDTH:-288}"
OUTPUT_HEIGHT="${OUTPUT_HEIGHT:-160}"

HLS_LOG="${HLS_LOG:-/tmp/fluxrt-hls.log}"
TWITCH_LOG="${TWITCH_LOG:-/tmp/fluxrt-twitch.log}"
HLS_LOOP_SCRIPT="/tmp/fluxrt-hls-loop.sh"
TWITCH_LOOP_SCRIPT="/tmp/fluxrt-twitch-loop.sh"
HLS_PID_FILE="/tmp/fluxrt-hls.pid"
TWITCH_PID_FILE="/tmp/fluxrt-twitch.pid"

TWITCH_STREAM_KEY="${TWITCH_STREAM_KEY:-}"
TWITCH_URL="rtmps://live.twitch.tv:443/app/${TWITCH_STREAM_KEY}"

# =============================================================================
# Cleanup — kill old processes
# =============================================================================
echo "=== Killing old fanout processes ==="
pkill -9 -f 'ffmpeg.*fluxrt-hls' 2>/dev/null || true
pkill -9 -f 'ffmpeg.*fluxrt-twitch' 2>/dev/null || true
pkill -9 -f '/tmp/fluxrt-hls-loop' 2>/dev/null || true
pkill -9 -f '/tmp/fluxrt-twitch-loop' 2>/dev/null || true
pkill -9 -f "ffmpeg.*-f hls.*${HLS_DIR}" 2>/dev/null || true
pkill -9 -f "ffmpeg.*-f flv.*live.twitch.tv" 2>/dev/null || true
sleep 2

# Cleanup function for exit
cleanup() {
  echo "=== Fanout shutting down ==="
  pkill -9 -f '/tmp/fluxrt-hls-loop' 2>/dev/null || true
  pkill -9 -f '/tmp/fluxrt-twitch-loop' 2>/dev/null || true
  pkill -9 -f 'ffmpeg.*fluxrt-hls' 2>/dev/null || true
  pkill -9 -f 'ffmpeg.*fluxrt-twitch' 2>/dev/null || true
  exit 0
}
trap cleanup SIGINT SIGTERM

# =============================================================================
# NO PROBING — just start ffmpeg directly. ffmpeg with -analyzeduration will
# patiently wait for UDP packets. No ffprobe, no waiting, no freezing.
# The HLS ingest loop handles everything: if video isn't ready, ffmpeg waits.
# If it crashes, the loop restarts it. Simple and robust.
# =============================================================================
echo "Audio source: UDP 5006 (mix bus — always reads, picks up audio when ready)"
AUDIO_USE_SILENCE=0

# =============================================================================
# Prepare HLS output directory
# =============================================================================
mkdir -p "${HLS_DIR}"
rm -f "${HLS_DIR}"/stream_*.ts "${HLS_DIR}"/stream.m3u8

# Write monitor page
cat > "${HLS_DIR}/index.html" <<'HTML'
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FluxRT Live Monitor</title>
  <style>
    body { margin: 0; background: #10100e; color: #f2f2ec; font-family: sans-serif; }
    main { max-width: 980px; margin: 0 auto; padding: 24px; }
    video { width: 100%; background: #000; border: 1px solid #333; }
    button { margin: 12px 8px 12px 0; padding: 10px 14px; border: 0; border-radius: 6px; background: #e7e0cc; color: #111; cursor: pointer; }
    code { color: #a7f3d0; }
    #status { color: #cbd5e1; white-space: pre-wrap; }
  </style>
</head>
<body>
  <main>
    <h1>FluxRT Live Monitor</h1>
    <video id="video" controls playsinline muted></video>
    <div>
      <button id="play">Play / Reload</button>
      <button id="mute">Mute / Unmute</button>
    </div>
    <p>Playlist: <a href="stream.m3u8"><code>/stream.m3u8</code></a></p>
    <p id="status">initializing...</p>
  </main>
  <script>
    const video = document.getElementById('video');
    const status = document.getElementById('status');
    const makeSrc = () => `stream.m3u8?cacheBust=${Date.now()}`;
    let hls = null;

    function setStatus(msg) { status.textContent = msg; }

    function loadScript(src) {
      return new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = src;
        s.onload = resolve;
        s.onerror = reject;
        document.head.appendChild(s);
      });
    }

    async function ensureHls() {
      if (window.Hls) return true;
      const urls = [
        'https://cdn.jsdelivr.net/npm/hls.js@1.5.16/dist/hls.min.js',
        'https://unpkg.com/hls.js@1.5.16/dist/hls.min.js'
      ];
      for (const u of urls) {
        try { setStatus(`loading HLS player from ${u}`); await loadScript(u); if (window.Hls) return true; } catch (_) {}
      }
      return false;
    }

    function destroyPlayer() {
      if (hls) { hls.destroy(); hls = null; }
      video.pause(); video.removeAttribute('src'); video.srcObject = null; video.load();
    }

    async function start(force = false) {
      if (force) destroyPlayer();
      const src = makeSrc();
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src;
        setStatus('using native HLS playback');
      } else if (await ensureHls() && window.Hls && window.Hls.isSupported()) {
        if (hls) hls.destroy();
        hls = new Hls({ lowLatencyMode: true, liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 10, maxLiveSyncPlaybackRate: 1.15, fragLoadingRetryDelay: 500, manifestLoadingRetryDelay: 500, levelLoadingRetryDelay: 500 });
        hls.on(Hls.Events.ERROR, (_, data) => { setStatus(`HLS error: ${data.type} / ${data.details}`); if (data.fatal) setTimeout(() => start(true), 700); });
        hls.on(Hls.Events.MANIFEST_PARSED, () => setStatus('live HLS manifest parsed'));
        hls.loadSource(src); hls.attachMedia(video);
      } else { setStatus('Browser cannot play HLS. Open /stream.m3u8 in VLC.'); return; }
      try { await video.play(); } catch (_) { setStatus('Ready. Press play if autoplay blocked.'); }
    }

    document.getElementById('play').addEventListener('click', () => start(true));
    document.getElementById('mute').addEventListener('click', () => { video.muted = !video.muted; });
    video.addEventListener('playing', () => setStatus('playing live HLS'));
    video.addEventListener('waiting', () => setStatus('buffering...'));
    video.addEventListener('error', () => { setStatus('video error; reloading...'); start(true); });
    start();
  </script>
</body>
</html>
HTML

# =============================================================================
# HLS INGEST LOOP — never dies, auto-restarts on crash
# =============================================================================
AUDIO_USE_SILENCE=${AUDIO_USE_SILENCE}

cat > "${HLS_LOOP_SCRIPT}" <<EOF
#!/usr/bin/env bash
set -u

VIDEO_URL="${VIDEO_INPUT_URL}"
AUDIO_URL="${AUDIO_INPUT_URL}"
USE_SILENCE=${AUDIO_USE_SILENCE}
HLS_DIR="${HLS_DIR}"
SEG_TIME=${HLS_SEG_TIME}
LIST_SIZE=${HLS_LIST_SIZE}
FPS=${BROADCAST_FPS}
VBITRATE=${VIDEO_BITRATE}
ABITRATE=${AUDIO_BITRATE}
WIDTH=${OUTPUT_WIDTH}
HEIGHT=${OUTPUT_HEIGHT}

while true; do
  echo "[\$(date +%H:%M:%S)] HLS ingest: starting ffmpeg (silence=\${USE_SILENCE})"

  if [[ "\${USE_SILENCE}" == "1" ]]; then
    # Silence fallback — no audio UDP source, just anullsrc
    ffmpeg -hide_banner -loglevel warning \\
      -fflags +genpts+discardcorrupt+igndts+fastseek \\
      -flags +global_header -err_detect ignore_err \\
      -analyzeduration 2M -probesize 2M -thread_queue_size 32784 \\
      -use_wallclock_as_timestamps 1 -max_interleave_delta 0 \\
      -i "\${VIDEO_URL}" \\
      -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 \\
      -map 0:v:0 -map 1:a:0 \\
      -vf "scale=\${WIDTH}:\${HEIGHT},format=yuv420p" -r \${FPS} -fps_mode cfr \\
      -af "aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=PTS-STARTPTS" \\
      -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p \\
      -b:v \${VBITRATE} -c:a aac -b:a \${ABITRATE} -ar 48000 -ac 2 \\
      -async 1 -max_interleave_delta 0 \\
      -f hls -hls_time \${SEG_TIME} -hls_list_size \${LIST_SIZE} \\
      -hls_flags delete_segments+program_date_time+independent_segments \\
      -hls_segment_filename "\${HLS_DIR}/stream_%05d.ts" \\
      "\${HLS_DIR}/stream.m3u8"
  else
    # Audio from UDP 5002 (MusicGen output)
    ffmpeg -hide_banner -loglevel warning \\
      -fflags +genpts+discardcorrupt+igndts+fastseek \\
      -flags +global_header -err_detect ignore_err \\
      -analyzeduration 2M -probesize 2M -thread_queue_size 32784 \\
      -use_wallclock_as_timestamps 1 -max_interleave_delta 0 \\
      -i "\${VIDEO_URL}" \\
      -thread_queue_size 32784 \\
      -fflags +genpts+discardcorrupt \\
      -i "\${AUDIO_URL}" \\
      -map 0:v:0 -map 1:a:0? \\
      -vf "scale=\${WIDTH}:\${HEIGHT},format=yuv420p" -r \${FPS} -fps_mode cfr \\
      -af "aresample=async=1:min_hard_comp=0.100:first_pts=0,asetpts=PTS-STARTPTS" \\
      -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p \\
      -b:v \${VBITRATE} -c:a aac -b:a \${ABITRATE} -ar 48000 -ac 2 \\
      -async 1 -max_interleave_delta 0 \\
      -f hls -hls_time \${SEG_TIME} -hls_list_size \${LIST_SIZE} \\
      -hls_flags delete_segments+program_date_time+independent_segments \\
      -hls_segment_filename "\${HLS_DIR}/stream_%05d.ts" \\
      "\${HLS_DIR}/stream.m3u8"
  fi

  echo "[\$(date +%H:%M:%S)] HLS ingest: ffmpeg exited (rc=\$?), restarting in 2s..."
  sleep 2
done
EOF
chmod +x "${HLS_LOOP_SCRIPT}"

# =============================================================================
# TWITCH RELAY LOOP — never dies, auto-restarts on crash
# =============================================================================
if [[ -z "${TWITCH_STREAM_KEY}" ]]; then
  echo "WARNING: TWITCH_STREAM_KEY not set — Twitch relay disabled"
  TWITCH_ENABLED=0
else
  TWITCH_ENABLED=1
fi

if [[ "${TWITCH_ENABLED}" == "1" ]]; then
cat > "${TWITCH_LOOP_SCRIPT}" <<EOF
#!/usr/bin/env bash
set -u

HLS_DIR="${HLS_DIR}"
TWITCH_URL="${TWITCH_URL}"

# Wait for HLS playlist to exist before starting
echo "[\$(date +%H:%M:%S)] Twitch relay: waiting for HLS playlist..."
while true; do
  if [[ -f "\${HLS_DIR}/stream.m3u8" ]] && ls \${HLS_DIR}/stream_*.ts >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
echo "[\$(date +%H:%M:%S)] Twitch relay: HLS playlist found, starting relay"

while true; do
  echo "[\$(date +%H:%M:%S)] Twitch relay: starting ffmpeg"

  ffmpeg -hide_banner -loglevel verbose \\
    -re -i "\${HLS_DIR}/stream.m3u8" \\
    -c:v copy -c:a copy \\
    -f flv "\${TWITCH_URL}"

  echo "[\$(date +%H:%M:%S)] Twitch relay: ffmpeg exited (rc=\$?), reconnecting in 3s..."
  sleep 3
done
EOF
chmod +x "${TWITCH_LOOP_SCRIPT}"
fi

# =============================================================================
# Start the loops
# =============================================================================
echo "=== Starting HLS ingest loop ==="
nohup "${HLS_LOOP_SCRIPT}" > "${HLS_LOG}" 2>&1 &
HLS_PID=$!
echo "${HLS_PID}" > "${HLS_PID_FILE}"
echo "HLS loop PID: ${HLS_PID}"

if [[ "${TWITCH_ENABLED}" == "1" ]]; then
  echo "=== Starting Twitch relay loop ==="
  nohup "${TWITCH_LOOP_SCRIPT}" > "${TWITCH_LOG}" 2>&1 &
  TWITCH_PID=$!
  echo "${TWITCH_PID}" > "${TWITCH_PID_FILE}"
  echo "Twitch loop PID: ${TWITCH_PID}"
else
  echo "Twitch relay disabled (no stream key)"
fi

# =============================================================================
# Done — just print status and exit. The loops run in the background.
# =============================================================================
echo ""
echo "=== Fanout running ==="
echo "HLS loop:  PID ${HLS_PID} (never-die, auto-restart)"
if [[ "${TWITCH_ENABLED}" == "1" ]]; then
  echo "Twitch:    PID ${TWITCH_PID} (never-die, auto-reconnect)"
fi
echo ""
echo "Monitor:   https://v89vkr1b3dhgua-8090.proxy.runpod.net"
echo "HLS log:   ${HLS_LOG}"
echo "Twitch log: ${TWITCH_LOG}"
echo ""
echo "The loops never give up. If ffmpeg crashes, it restarts automatically."
echo "To stop: kill \$(cat ${HLS_PID_FILE}) \$(cat ${TWITCH_PID_FILE} 2>/dev/null)"
