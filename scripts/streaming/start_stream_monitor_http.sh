#!/usr/bin/env bash
set -euo pipefail

MONITOR_DIR="${MONITOR_HLS_DIR:-/tmp/fluxrt-monitor}"
MONITOR_HOST="${MONITOR_HOST:-0.0.0.0}"
MONITOR_PORT="${MONITOR_PORT:-8090}"
MONITOR_PID_FILE="${MONITOR_PID_FILE:-/tmp/fluxrt-monitor-http.pid}"
MONITOR_LOG_FILE="${MONITOR_LOG_FILE:-/tmp/fluxrt-monitor-http.log}"

mkdir -p "$MONITOR_DIR"

cat > "$MONITOR_DIR/index.html" <<'HTML'
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FluxRT Pre-Twitch Monitor</title>
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
    <h1>FluxRT Pre-Twitch Monitor</h1>
    <video id="video" controls playsinline></video>
    <div>
      <button id="play">Play / Reload Live Monitor</button>
      <button id="mute">Mute / Unmute</button>
    </div>
    <p>Playlist: <a href="stream.m3u8"><code>/stream.m3u8</code></a></p>
    <p id="status">initializing...</p>
  </main>
  <script>
    const video = document.getElementById('video');
    const status = document.getElementById('status');
    const playlist = () => `stream.m3u8?cacheBust=${Date.now()}`;
    let hls;

    function setStatus(message) {
      status.textContent = message;
    }

    function loadScript(src) {
      return new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = src;
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
      });
    }

    async function ensureHls() {
      if (window.Hls) return true;
      const urls = [
        'https://cdn.jsdelivr.net/npm/hls.js@latest',
        'https://unpkg.com/hls.js@latest'
      ];
      for (const url of urls) {
        try {
          setStatus(`loading HLS player from ${url}`);
          await loadScript(url);
          if (window.Hls) return true;
        } catch (err) {}
      }
      return false;
    }

    async function start() {
      const src = playlist();
      if (hls) {
        hls.destroy();
        hls = null;
      }
      video.pause();
      video.removeAttribute('src');
      video.load();

      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src;
        setStatus('using native HLS playback');
      } else if (await ensureHls() && window.Hls.isSupported()) {
        hls = new Hls({ liveSyncDurationCount: 3, enableWorker: true });
        hls.on(Hls.Events.ERROR, (_, data) => setStatus(`HLS error: ${data.type} / ${data.details}`));
        hls.loadSource(src);
        hls.attachMedia(video);
        setStatus('HLS player attached; press play if the browser blocks autoplay');
      } else {
        setStatus('This browser cannot play HLS here. Open stream.m3u8 in VLC/ffplay, or expose this page through the same tunnel as Gradio.');
        return;
      }

      try {
        await video.play();
      } catch (err) {
        setStatus('Ready. Press the video play button if autoplay was blocked.');
      }
    }

    document.getElementById('play').addEventListener('click', start);
    document.getElementById('mute').addEventListener('click', () => { video.muted = !video.muted; });
    video.addEventListener('playing', () => setStatus('playing live pre-Twitch HLS'));
    video.addEventListener('waiting', () => setStatus('buffering live HLS...'));
    start();
  </script>
</body>
</html>
HTML

if [[ -f "$MONITOR_PID_FILE" ]]; then
  old_pid="$(cat "$MONITOR_PID_FILE" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Monitor HTTP already running."
    echo "PID: $old_pid"
    echo "URL: http://127.0.0.1:${MONITOR_PORT}/"
    exit 0
  fi
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -x "/home/gschi/FluxRT/.venv/bin/python" ]]; then
  PYTHON_BIN="/home/gschi/FluxRT/.venv/bin/python"
fi

nohup "$PYTHON_BIN" -m http.server "$MONITOR_PORT" --bind "$MONITOR_HOST" --directory "$MONITOR_DIR" > "$MONITOR_LOG_FILE" 2>&1 &
echo "$!" > "$MONITOR_PID_FILE"

echo "Monitor HTTP started."
echo "PID: $(cat "$MONITOR_PID_FILE")"
echo "URL: http://127.0.0.1:${MONITOR_PORT}/"
echo "Playlist: http://127.0.0.1:${MONITOR_PORT}/stream.m3u8"
echo "Directory: $MONITOR_DIR"
echo "Log: $MONITOR_LOG_FILE"