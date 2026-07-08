# FluxRT — 24/7 AI Streaming Pipeline Runbook (L40 RunPod)

> **Branch:** `streaming-l40-self-healing`  
> **Hardware:** Dual NVIDIA L40 GPUs (46GB each), 2TB RAM, 64 CPUs  
> **Target:** Twitch 24/7 AI stream (video + AI music + TTS)  
> **Last verified working:** 2026-07-08

---

## Architecture Overview

```
┌─────────────┐   UDP 5000   ┌──────────┐
│   Gradio    │─────────────▶│          │
│  (FLUX.2)   │   (video)    │          │
│  GPU 0      │              │  Ingest  │
└─────────────┘              │  ffmpeg  │     ┌──────────┐     ┌────────┐
┌─────────────┐   UDP 5002   │  (amix   │     │          │     │        │
│  MusicGen   │─────────────▶│   3-in)  │────▶│ MediaMTX │────▶│ Egress │──▶ Twitch
│  (small)    │   (music)    │          │ RTMP│  :1935   │ RTMP│ ffmpeg │
│  GPU 1      │              │          │     │  fluxrt  │     │        │
└─────────────┘              │          │     └──────────┘     └────────┘
┌─────────────┐   UDP 5004   │          │
│  Edge TTS   │─────────────▶│          │
│  (quotes)   │   (tts)      └──────────┘
└─────────────┘
```

### Data Flow

1. **Gradio** (GPU 0, cores 0-15) — FLUX.2-Klein-4B generates video frames at 8fps, 288x160, int8 quantized. Streams H.264 via UDP to port 5000.
2. **MusicGen** (GPU 1, cores 16-63) — `facebook/musicgen-small` generates 8-second music clips in fp16 at 32kHz. Crossfaded and streamed via ffmpeg pipe to UDP port 5002.
3. **Edge TTS** — Reads quotes from `data/quotes/diffusiongemma_quotes.json` with random voices, reverb, and echo effects. Streams to UDP port 5004.
4. **Ingest ffmpeg** — Mixes 3 audio inputs (silence base + music + TTS) with `amix`, copies video from port 5000, outputs FLV to MediaMTX.
5. **MediaMTX** — RTMP relay on port 1935, path `fluxrt`.
6. **Egress ffmpeg** — Copies stream from MediaMTX to Twitch RTMPS endpoint.
7. **Watchdog** — Monitors all components every 30s, auto-restarts dead ones, detects audio silence.

---

## Component Launch Commands

### 1. MediaMTX (RTMP Relay)

```bash
cat > /tmp/fluxrt-mediamtx.yml << 'EOF'
logLevel: info
rtsp: false
hls: false
webrtc: false
srt: false
api: false
metrics: false
pprof: false
rtmpAddress: :1935
rtmpEncryption: "no"
paths:
  fluxrt:
    source: publisher
EOF

nohup setsid /workspace/FluxRT/tools/mediamtx/mediamtx /tmp/fluxrt-mediamtx.yml \
    > /tmp/fluxrt-mediamtx.log 2>&1 & disown
```

### 2. Gradio (Video Generation — GPU 0)

```bash
cd /workspace/FluxRT
taskset -c 0-15 nohup setsid /root/fluxrt-venv/bin/python -u \
    scripts/run_gradio_stream_demo.py \
    --int8 \
    --server-name 0.0.0.0 \
    --server-port 7862 \
    --config-path configs/stream_demo_config.json \
    --local-video /workspace/test_input.mp4 \
    > /tmp/fluxrt-gradio.log 2>&1 & disown
```

**Config:** `configs/stream_demo_config.json`  
**Resolution:** 288x160 @ 8fps  
**Model:** FLUX.2-Klein-4B int8  
**Share URL:** Check `/tmp/fluxrt-gradio.log` for Gradio share link

### 3. MusicGen (AI Music — GPU 1)

```bash
cd /workspace/FluxRT
taskset -c 16-63 nohup setsid .venv/bin/python3 -u \
    scripts/run_musicgen_radio_plus_musicGEN.py \
    --radio-url \
    --output-dir /dev/shm/musicgen \
    --model facebook/musicgen-small \
    --gen-seconds 8 \
    --top-k 250 \
    --top-p 0.95 \
    --temperature 1.0 \
    --guidance-scale 3.0 \
    --no-drunk-walk \
    --parallel-clips 2 \
    --seed -1 \
    --bootstrap-clips 54 \
    --pre-generate 10 \
    --pause-seconds 0 \
    --base-prompt "" \
    --conditioning-mode continuation \
    --conditioning-seconds 8 \
    --stream-delay-seconds 120 \
    --audio-udp-url "udp://127.0.0.1:5002?pkt_size=1316" \
    --crossfade-seconds 2.0 \
    > /dev/shm/musicgen/musicgen.log 2>&1 & disown
```

**Boot time:** ~120 seconds (model loading + bootstrap generation)  
**RTF:** 0.55-0.58 (generates faster than realtime)  
**Self-healing:** Automatically restarts ffmpeg pipe on `BrokenPipeError`

### 4. Edge TTS (Voice Quotes)

```bash
cd /workspace/FluxRT
nohup setsid python3 -u \
    scripts/run_edge_tts_quotes.py \
    --quotes data/quotes/diffusiongemma_quotes.json \
    --loop --shuffle \
    --interval 15 \
    --random-voices \
    --reverb --reverb-mix 0.35 --reverb-decay 0.50 --reverb-delay-ms 60 \
    --echo --echo-mix 0.45 --echo-decay 0.65 --echo-delay-ms 150 \
    --udp-url "udp://127.0.0.1:5004?pkt_size=1316" \
    --repeat-on-empty \
    --cache-dir voices/quote_cache_edge --cache-size 4 \
    > /tmp/fluxrt-tts.log 2>&1 & disown
```

### 5. Ingest ffmpeg (Audio Mixer + Video Passthrough)

```bash
nohup setsid ffmpeg -hide_banner -loglevel warning \
    -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000" \
    -f mpegts -i "udp://127.0.0.1:5002?fifo_size=500000&overrun_nonfatal=1" \
    -f mpegts -i "udp://127.0.0.1:5004?fifo_size=500000&overrun_nonfatal=1" \
    -f mpegts -i "udp://127.0.0.1:5000?fifo_size=1000000&overrun_nonfatal=1" \
    -filter_complex "[0:a]volume=1.0[a0];[1:a]volume=1.0[a1];[2:a]volume=0.85[a2];[a0][a1][a2]amix=inputs=3:duration=longest:dropout_transition=0[aout]" \
    -map "3:v" -map "[aout]" \
    -c:v copy -c:a aac -b:a 128k -ar 48000 -ac 2 \
    -f flv "rtmp://127.0.0.1:1935/fluxrt" \
    > /tmp/fluxrt-mediamtx-ingest.log 2>&1 & disown
```

**Audio mix:** silence base (vol 1.0) + music (vol 1.0) + TTS (vol 0.85)  
**Video:** Copied from UDP 5000 (no re-encode)

### 6. Egress ffmpeg (MediaMTX → Twitch)

> **Important:** Start this 5-10 seconds AFTER ingest, so MediaMTX has an active publisher.

```bash
nohup setsid ffmpeg -hide_banner -loglevel info \
    -i "rtmp://127.0.0.1:1935/fluxrt" \
    -c copy \
    -f flv "rtmps://live.twitch.tv:443/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw" \
    > /tmp/fluxrt-egress-twitch.log 2>&1 & disown
```

**Twitch stream key:** In `scripts/streaming/rtmp_targets.env`

### 7. Watchdog (Auto-Recovery)

> **Important:** If MusicGen is already running, set `MUSICGEN_START_TIME` to ~200s in the past to skip the grace period.

```bash
# If MusicGen is already running (past boot):
nohup setsid bash -c '
MUSICGEN_START_TIME=$(($(date +%s) - 200))
export MUSICGEN_START_TIME
bash /workspace/FluxRT/scripts/streaming/watchdog.sh
' > /tmp/fluxrt-watchdog.log 2>&1 & disown

# If starting fresh (MusicGen not yet running):
nohup setsid bash /workspace/FluxRT/scripts/streaming/watchdog.sh \
    > /tmp/fluxrt-watchdog.log 2>&1 & disown
```

**Check interval:** 30 seconds  
**MusicGen grace period:** 180 seconds (allows 120s boot)  
**Silence detection:** Restarts MusicGen + ingest if audio < -60 dB  
**Monitors:** MediaMTX, Ingest, Egress, MusicGen, TTS, Gradio

---

## Full Cold Start Sequence

Follow this exact order for a clean start from nothing:

```bash
# 1. MediaMTX
nohup setsid /workspace/FluxRT/tools/mediamtx/mediamtx /tmp/fluxrt-mediamtx.yml \
    > /tmp/fluxrt-mediamtx.log 2>&1 & disown
sleep 2

# 2. Gradio (GPU 0)
cd /workspace/FluxRT
taskset -c 0-15 nohup setsid /root/fluxrt-venv/bin/python -u \
    scripts/run_gradio_stream_demo.py --int8 \
    --server-name 0.0.0.0 --server-port 7862 \
    --config-path configs/stream_demo_config.json \
    --local-video /workspace/test_input.mp4 \
    > /tmp/fluxrt-gradio.log 2>&1 & disown

# 3. TTS
nohup setsid python3 -u scripts/run_edge_tts_quotes.py \
    --quotes data/quotes/diffusiongemma_quotes.json \
    --loop --shuffle --interval 15 --random-voices \
    --reverb --reverb-mix 0.35 --reverb-decay 0.50 --reverb-delay-ms 60 \
    --echo --echo-mix 0.45 --echo-decay 0.65 --echo-delay-ms 150 \
    --udp-url "udp://127.0.0.1:5004?pkt_size=1316" \
    --repeat-on-empty --cache-dir voices/quote_cache_edge --cache-size 4 \
    > /tmp/fluxrt-tts.log 2>&1 & disown

# 4. MusicGen (GPU 1) — takes ~120s to boot
taskset -c 16-63 nohup setsid .venv/bin/python3 -u \
    scripts/run_musicgen_radio_plus_musicGEN.py \
    --radio-url --output-dir /dev/shm/musicgen \
    --model facebook/musicgen-small --gen-seconds 8 \
    --top-k 250 --top-p 0.95 --temperature 1.0 --guidance-scale 3.0 \
    --no-drunk-walk --parallel-clips 2 --seed -1 \
    --bootstrap-clips 54 --pre-generate 10 --pause-seconds 0 \
    --base-prompt "" --conditioning-mode continuation --conditioning-seconds 8 \
    --stream-delay-seconds 120 \
    --audio-udp-url "udp://127.0.0.1:5002?pkt_size=1316" \
    --crossfade-seconds 2.0 \
    > /dev/shm/musicgen/musicgen.log 2>&1 & disown

# 5. Wait for MusicGen to boot (120s + buffer)
echo "Waiting 150s for MusicGen to boot..."
sleep 150

# 6. Ingest
nohup setsid ffmpeg -hide_banner -loglevel warning \
    -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000" \
    -f mpegts -i "udp://127.0.0.1:5002?fifo_size=500000&overrun_nonfatal=1" \
    -f mpegts -i "udp://127.0.0.1:5004?fifo_size=500000&overrun_nonfatal=1" \
    -f mpegts -i "udp://127.0.0.1:5000?fifo_size=1000000&overrun_nonfatal=1" \
    -filter_complex "[0:a]volume=1.0[a0];[1:a]volume=1.0[a1];[2:a]volume=0.85[a2];[a0][a1][a2]amix=inputs=3:duration=longest:dropout_transition=0[aout]" \
    -map "3:v" -map "[aout]" \
    -c:v copy -c:a aac -b:a 128k -ar 48000 -ac 2 \
    -f flv "rtmp://127.0.0.1:1935/fluxrt" \
    > /tmp/fluxrt-mediamtx-ingest.log 2>&1 & disown
sleep 5

# 7. Egress (to Twitch)
nohup setsid ffmpeg -hide_banner -loglevel info \
    -i "rtmp://127.0.0.1:1935/fluxrt" -c copy \
    -f flv "rtmps://live.twitch.tv:443/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw" \
    > /tmp/fluxrt-egress-twitch.log 2>&1 & disown
sleep 10

# 8. Watchdog (MusicGen already running, so skip grace period)
nohup setsid bash -c '
MUSICGEN_START_TIME=$(($(date +%s) - 200))
export MUSICGEN_START_TIME
bash /workspace/FluxRT/scripts/streaming/watchdog.sh
' > /tmp/fluxrt-watchdog.log 2>&1 & disown

# 9. Verify
echo "=== Pipeline Status ==="
echo "MediaMTX: $(pgrep -f mediamtx > /dev/null && echo UP || echo DOWN)"
echo "Ingest: $(pgrep -f 'amix=inputs=3' > /dev/null && echo UP || echo DOWN)"
echo "Egress: $(pgrep -f 'twitch.tv' > /dev/null && echo UP || echo DOWN)"
echo "MusicGen: $(pgrep -f 'run_musicgen_radio' > /dev/null && echo UP || echo DOWN)"
echo "MusicGen ffmpeg: $(pgrep -f 'f32le.*udp' > /dev/null && echo UP || echo DOWN)"
echo "Gradio: $(pgrep -f 'run_gradio_stream' > /dev/null && echo UP || echo DOWN)"
echo "TTS: $(pgrep -f 'run_edge_tts' > /dev/null && echo UP || echo DOWN)"
echo "Watchdog: $(pgrep -f 'watchdog.sh' > /dev/null && echo UP || echo DOWN)"
```

---

## Health Check Commands

### Quick status check
```bash
echo "MediaMTX: $(pgrep -f mediamtx > /dev/null && echo UP || echo DOWN)"
echo "Ingest: $(pgrep -f 'amix=inputs=3' > /dev/null && echo UP || echo DOWN)"
echo "Egress: $(pgrep -f 'twitch.tv' > /dev/null && echo UP || echo DOWN)"
echo "MusicGen: $(pgrep -f 'run_musicgen_radio' > /dev/null && echo UP || echo DOWN)"
echo "MusicGen ffmpeg: $(pgrep -f 'f32le.*udp' > /dev/null && echo UP || echo DOWN)"
echo "Gradio: $(pgrep -f 'run_gradio_stream' > /dev/null && echo UP || echo DOWN)"
echo "TTS: $(pgrep -f 'run_edge_tts' > /dev/null && echo UP || echo DOWN)"
echo "Watchdog: $(pgrep -f 'watchdog.sh' > /dev/null && echo UP || echo DOWN)"
```

### Audio level check
```bash
timeout 20 ffmpeg -hide_banner -loglevel info \
    -i "rtmp://127.0.0.1:1935/fluxrt" \
    -t 8 -af "volumedetect" \
    -f null - 2>&1 | grep -E "mean_volume|max_volume"
```
**Expected:** mean_volume between -15 and -40 dB. If -60 or lower, audio is silent.

### Watchdog log
```bash
tail -30 /tmp/fluxrt-watchdog.log
```

### MusicGen log
```bash
tail -30 /dev/shm/musicgen/musicgen.log
```

---

## Troubleshooting

### Egress dies immediately
**Cause:** Egress tried to connect to MediaMTX before ingest published a stream.  
**Fix:** Wait 5-10 seconds after starting ingest before starting egress. Verify ingest is publishing:
```bash
# Should show video+audio streams
ffmpeg -i "rtmp://127.0.0.1:1935/fluxrt" -t 2 -f null - 2>&1 | grep "Stream #"
```

### MusicGen audio goes silent after hours
**Cause:** ffmpeg pipe breaks between Python and ffmpeg encoder.  
**Fix:** Already handled by self-healing code in `_stream_array()`. Watch for these log lines:
```
[musicgen] PIPE ERROR writing to ffmpeg
[musicgen] ffmpeg encoder restarted
```
If it still goes silent, the watchdog's silence detection will restart MusicGen + ingest.

### Watchdog kills MusicGen during boot
**Cause:** Watchdog checks for `now_playing_path.txt` which doesn't exist during 120s boot.  
**Fix:** 180-second grace period is built into watchdog. If starting watchdog when MusicGen is already running, set `MUSICGEN_START_TIME` to 200s in the past.

### Ingest "Broken pipe" error
**Cause:** MediaMTX dropped the connection (egress disconnected or RTMP hiccup).  
**Fix:** Restart ingest, wait 5s, then restart egress. The watchdog handles this automatically.

### UDP port "Address already in use"
**Cause:** Old process hasn't fully died.  
**Fix:** Wait 3-5 seconds after killing a process before restarting on the same port.

---

## Key Files

| File | Purpose |
|------|---------|
| `scripts/run_gradio_stream_demo.py` | Video generation (FLUX.2, GPU 0) |
| `scripts/run_musicgen_radio_plus_musicGEN.py` | Music generation (MusicGen, GPU 1) |
| `scripts/run_edge_tts_quotes.py` | TTS voice quotes |
| `scripts/streaming/watchdog.sh` | Auto-recovery watchdog |
| `scripts/streaming/rotate_flux_prompt.py` | Prompt rotation for Gradio |
| `scripts/streaming/rtmp_targets.env` | Twitch stream key |
| `configs/stream_demo_config.json` | Gradio/FLUX config |
| `tools/mediamtx/mediamtx` | MediaMTX binary (v1.8.4) |
| `data/quotes/diffusiongemma_quotes.json` | TTS quote source |

## CPU Isolation

| Component | Cores | GPU |
|-----------|-------|-----|
| Gradio (FLUX.2) | 0-15 | GPU 0 |
| MusicGen | 16-63 | GPU 1 |
| MediaMTX / Ingest / Egress / TTS / Watchdog | any | none |

## Port Map

| Port | Protocol | Source → Destination |
|------|----------|---------------------|
| 5000 | UDP (MPEG-TS) | Gradio → Ingest |
| 5002 | UDP (MPEG-TS) | MusicGen ffmpeg → Ingest |
| 5004 | UDP (MPEG-TS) | TTS → Ingest |
| 1935 | RTMP | Ingest → MediaMTX → Egress |
| 7862 | HTTP | Gradio web UI |

---

## Git State

- **Working branch:** `streaming-l40-self-healing`
- **Backup branch:** `backup/20260708-145056-self-healing-pipeline`
- **Commit:** `63278e4` — self-healing MusicGen pipe + watchdog grace period & silence detection
- **Remote:** `https://github.com/gschian0/FluxRT.git`

### To restore this state from scratch:
```bash
git clone https://github.com/gschian0/FluxRT.git
cd FluxRT
git checkout streaming-l40-self-healing
# Then follow "Full Cold Start Sequence" above
```
