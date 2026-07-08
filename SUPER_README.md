# FluxRT — Super README: 24/7 AI Streaming Pipeline

> **The complete guide to the self-healing AI streaming pipeline on dual NVIDIA L40 GPUs.**  
> **Branch:** `streaming-l40-self-healing`  
> **Last updated:** 2026-07-08

---

## Table of Contents

1. [What Is This?](#1-what-is-this)
2. [How It Works — Architecture](#2-how-it-works--architecture)
3. [The Fixes — What Broke and How We Fixed It](#3-the-fixes--what-broke-and-how-we-fixed-it)
4. [Different Ways to Start the Pipeline](#4-different-ways-to-start-the-pipeline)
5. [How to Run It — Step by Step](#5-how-to-run-it--step-by-step)
6. [Health Checks & Monitoring](#6-health-checks--monitoring)
7. [Troubleshooting](#7-troubleshooting)
8. [Plan: Build All CLI Commands Into the Gradio UI](#8-plan-build-all-cli-commands-into-the-gradio-ui)
9. [File Reference](#9-file-reference)

---

## 1. What Is This?

FluxRT is a **24/7 AI streaming pipeline** that generates and streams live content to Twitch — no human intervention required.

It combines three AI generators running simultaneously on two GPUs:

| Generator | What It Does | GPU | Output |
|-----------|-------------|-----|--------|
| **FLUX.2-Klein-4B** | Generates AI video frames (288×160 @ 8fps) | GPU 0 | UDP 5000 (video) |
| **MusicGen-small** | Generates continuous crossfaded AI music | GPU 1 | UDP 5002 (music) |
| **Edge TTS** | Reads philosopher quotes with reverb + echo | CPU | UDP 5004 (voice) |

These three streams are mixed by an **ingest ffmpeg** process, relayed through **MediaMTX** (an RTMP server), and pushed to **Twitch** by an **egress ffmpeg** process. A **watchdog** monitors everything and auto-restarts dead components.

### What makes this version special?

This branch (`streaming-l40-self-healing`) includes **self-healing fixes** that allow the pipeline to run for days without manual intervention:

1. **MusicGen pipe auto-recovery** — If the ffmpeg pipe between Python and the audio encoder breaks, MusicGen automatically restarts the encoder and resumes streaming
2. **Watchdog grace period** — The watchdog waits 180 seconds for MusicGen to boot before checking its health (it was killing MusicGen during its 120-second startup)
3. **Audio silence detection** — The watchdog measures actual stream audio levels and restarts MusicGen + ingest if the stream goes silent

---

## 2. How It Works — Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          RUNPOD POD (Dual L40)                           │
│                                                                          │
│  ┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐    │
│  │    GRADIO       │     │    MUSICGEN     │     │    EDGE TTS     │    │
│  │  (FLUX.2-Klein) │     │  (musicgen-     │     │  (philosopher   │    │
│  │   int8, 288×160 │     │   small, fp16)  │     │   quotes)       │    │
│  │   GPU 0         │     │   GPU 1         │     │   CPU           │    │
│  │   cores 0-15    │     │   cores 16-63   │     │                 │    │
│  └────────┬────────┘     └────────┬────────┘     └────────┬────────┘    │
│           │                       │                        │             │
│     UDP 5000               UDP 5002                 UDP 5004             │
│     (H.264 video)          (AAC music)              (AAC voice)          │
│           │                       │                        │             │
│           ▼                       ▼                        ▼             │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    INGEST FFMPEG                                │    │
│  │  • Input 0: silence base (anullsrc, 48kHz stereo)              │    │
│  │  • Input 1: music from UDP 5002 (vol 1.0)                      │    │
│  │  • Input 2: TTS from UDP 5004 (vol 0.85)                       │    │
│  │  • Input 3: video from UDP 5000 (copied, no re-encode)         │    │
│  │  • amix 3 audio inputs → AAC 128k 48kHz stereo                 │    │
│  │  • Output: FLV → rtmp://127.0.0.1:1935/fluxrt                 │    │
│  └────────────────────────────┬────────────────────────────────────┘    │
│                               │                                          │
│                        ┌──────▼──────┐                                   │
│                        │  MEDIAMTX   │                                   │
│                        │  RTMP relay │                                   │
│                        │  :1935      │                                   │
│                        │  path:fluxrt│                                   │
│                        └──────┬──────┘                                   │
│                               │                                          │
│  ┌────────────────────────────▼────────────────────────────────────┐    │
│  │                    EGRESS FFMPEG                                │    │
│  │  • Input: rtmp://127.0.0.1:1935/fluxrt                         │    │
│  │  • Copy video + audio (no re-encode)                           │    │
│  │  • Output: rtmps://live.twitch.tv:443/app/<stream-key>        │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    WATCHDOG (every 30s)                         │    │
│  │  • Checks all 7 processes are alive                             │    │
│  │  • 180s grace period for MusicGen startup                       │    │
│  │  • Audio silence detection (< -60 dB → restart)                │    │
│  │  • Auto-restarts any dead component                             │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘

                    ┌──────────────────┐
                    │     TWITCH       │
                    │  Live Stream     │
                    │  (24/7 AI TV)    │
                    └──────────────────┘
```

### Data Flow Summary

```
Gradio (GPU 0) ──UDP 5000──┐
                            ├──▶ Ingest ffmpeg ──▶ MediaMTX :1935 ──▶ Egress ──▶ Twitch
MusicGen (GPU 1) ─UDP 5002──┤    (amix 3-input)     (RTMP relay)    (RTMPS copy)
                            │
Edge TTS ──────UDP 5004─────┘

Watchdog ── monitors all components every 30s, auto-restarts on failure
```

### Port Map

| Port | Protocol | Source → Destination | Content |
|------|----------|---------------------|---------|
| 5000 | UDP (MPEG-TS) | Gradio → Ingest | H.264 video (288×160 @ 8fps) |
| 5002 | UDP (MPEG-TS) | MusicGen ffmpeg → Ingest | AAC music (48kHz stereo) |
| 5004 | UDP (MPEG-TS) | TTS → Ingest | AAC voice (48kHz stereo) |
| 1935 | RTMP | Ingest → MediaMTX → Egress | FLV stream (video + mixed audio) |
| 7862 | HTTP | Gradio web UI | Browser interface |

### CPU & GPU Allocation

| Component | GPU | CPU Cores | VRAM |
|-----------|-----|-----------|------|
| Gradio (FLUX.2) | 0 (L40) | 0-15 (16 cores) | ~8GB (int8) |
| MusicGen | 1 (L40) | 16-63 (48 cores) | ~4GB (fp16) |
| MediaMTX | none | any | N/A |
| Ingest ffmpeg | none | any | N/A |
| Egress ffmpeg | none | any | N/A |
| TTS | none | any | N/A |
| Watchdog | none | any | N/A |

---

## 3. The Fixes — What Broke and How We Fixed It

### Fix 1: MusicGen Self-Healing Pipe Restart

**The problem:** After running for several hours, the pipe between the MusicGen Python process and its child ffmpeg encoder would break. The `_stream_array()` function in `run_musicgen_radio_plus_musicGEN.py` called `udp_proc.stdin.write()` with no error handling. When the pipe broke, the write silently failed — MusicGen kept "playing" clips but no audio reached the encoder. The stream went silent with no error message.

**Root cause:** Long-running subprocess pipes can break due to:
- ffmpeg crashing or being killed by OOM
- Buffer overflow in the pipe
- System resource exhaustion

**The fix:** Added try/except around `stdin.write()` in `_stream_array()`:

```python
# BEFORE (broken):
udp_proc.stdin.write(chunk.astype(np.float32, copy=False).tobytes())

# AFTER (self-healing):
try:
    udp_proc.stdin.write(chunk.astype(np.float32, copy=False).tobytes())
except (BrokenPipeError, OSError, ValueError) as exc:
    print(f"[musicgen] PIPE ERROR writing to ffmpeg: {type(exc).__name__}: {exc} — restarting ffmpeg")
    try:
        udp_proc.stdin.close()
    except Exception:
        pass
    try:
        udp_proc.kill()
    except Exception:
        pass
    # Restart the ffmpeg encoder
    udp_proc = _start_audio_udp_encoder(sample_rate, _udp_url)
    print("[musicgen] ffmpeg encoder restarted — resuming audio stream")
    # Try writing the chunk again
    try:
        udp_proc.stdin.write(chunk.astype(np.float32, copy=False).tobytes())
    except Exception:
        pass
```

**Also required:**
- Added `audio_udp_url: str = ""` parameter to `_playback_worker()` so the URL is available for restart
- Added `nonlocal udp_proc` to `_stream_array()` so the restarted process is visible to the outer scope
- Updated the call site to pass `args.audio_udp_url.strip()` as the 9th argument

**Files changed:** `scripts/run_musicgen_radio_plus_musicGEN.py` (+27 lines)

---

### Fix 2: Watchdog Grace Period for MusicGen Startup

**The problem:** The watchdog checked for `/dev/shm/musicgen/now_playing_path.txt` every 30 seconds. But MusicGen takes ~120 seconds to boot (load model → pre-generate clips → bootstrap playback). The `now_playing_path.txt` file doesn't exist until boot is complete. The watchdog saw the missing file, killed MusicGen, and restarted it — creating an infinite restart loop where MusicGen could never finish booting.

**The fix:** Added a 180-second grace period at the top of the watchdog:

```bash
MUSICGEN_START_TIME=0
MUSICGEN_GRACE_PERIOD=180  # 3 minutes: model load + pre-gen + bootstrap
```

In `check_musicgen_alive()` and `check_musicgen_ffmpeg()`, skip checks during the grace period:

```bash
check_musicgen_alive() {
    local now=$(date +%s)
    local uptime=$((now - MUSICGEN_START_TIME))

    # Grace period: MusicGen takes ~120s to load model + pre-gen + bootstrap.
    # Don't check now_playing during startup — it won't exist yet.
    if [ $uptime -lt $MUSICGEN_GRACE_PERIOD ]; then
        log "   MusicGen booting (${uptime}/${MUSICGEN_GRACE_PERIOD}s grace period)"
        return 0  # Skip check, assume OK
    fi
    # ... rest of check only runs after grace period
}
```

`restart_musicgen()` sets `MUSICGEN_START_TIME=$(date +%s)` after restart so the grace period applies to restarts too.

**Important:** When starting the watchdog while MusicGen is already running (past boot), set `MUSICGEN_START_TIME` to 200 seconds in the past to skip the grace period:

```bash
MUSICGEN_START_TIME=$(($(date +%s) - 200)) bash scripts/streaming/watchdog.sh
```

**Files changed:** `scripts/streaming/watchdog.sh` (new file, 310 lines)

---

### Fix 3: Watchdog Audio Silence Detection

**The problem:** Even with the pipe self-healing fix, there are edge cases where MusicGen's process is alive and its ffmpeg is alive, but no audio is actually reaching the stream (e.g., the pipe is in a zombie state, or the UDP buffer is full). The watchdog's process checks would pass, but the stream would be silent.

**The fix:** Added audio silence detection to the watchdog's main loop. After the grace period, it samples 5 seconds of audio from the MediaMTX stream and measures the mean volume:

```bash
# Only check after grace period
if [ $_uptime -gt $MUSICGEN_GRACE_PERIOD ]; then
    _mean_vol=$(timeout 12 ffmpeg -hide_banner -loglevel error \
        -i "rtmp://127.0.0.1:1935/fluxrt" \
        -t 5 -af "volumedetect" -f null - 2>&1 \
        | grep -oP 'mean_volume: \K[0-9.-]+')
    if [ -n "$_mean_vol" ]; then
        _vol_int=$(echo "$_mean_vol" | awk '{printf "%d", $1 * 10}')
        # -60 dB = -600. Below -600 (more negative) = silence
        if [ "$_vol_int" -lt -600 ] 2>/dev/null; then
            log "⚠️  Audio silence detected (mean_volume=${_mean_vol}dB) — restarting"
            restart_musicgen
            restart_ingest
        fi
    fi
fi
```

**Threshold:** -60 dB mean volume. Normal music is -15 to -40 dB. Silence is -80 to -infinity.

**Files changed:** `scripts/streaming/watchdog.sh`

---

### Fix 4: Watchdog Bash `local` Keyword Bug

**The problem:** The silence detection code used `local` variables inside the main `while true` loop, which is not inside a function. In bash, `local` can only be used inside functions. This caused a syntax error.

**The fix:** Changed `local` variables to regular variables with underscore-prefixed names to avoid collisions:

```bash
# BEFORE (broken):
local _now=$(date +%s)
local _uptime=$((_now - MUSICGEN_START_TIME))
local _mean_vol=$(...)
local _vol_int=$(...)

# AFTER (fixed):
_now=$(date +%s)
_uptime=$((_now - MUSICGEN_START_TIME))
_mean_vol=$(...)
_vol_int=$(...)
```

---

### Fix 5: Prompt Rotator Enhanced Error Handling

**The problem:** The prompt rotator (`rotate_flux_prompt.py`) would die with SIGTERM (exit 143) when the Gradio API was temporarily unavailable, and had no retry logic.

**The fix:** Added enhanced error handling and retry logic with exponential backoff. The script now uses curl subprocess calls instead of the `gradio_client` library (which was unstable in long-running background processes).

**Files changed:** `scripts/streaming/rotate_flux_prompt.py` (+63 lines)

---

## 4. Different Ways to Start the Pipeline

There are **four ways** to start the pipeline, from simplest to most automated:

### Method A: Manual One-by-One (Full Control)

Start each component manually in the correct order. Best for first-time setup or debugging.

```bash
# 1. MediaMTX
nohup setsid /workspace/FluxRT/tools/mediamtx/mediamtx /tmp/fluxrt-mediamtx.yml \
    > /tmp/fluxrt-mediamtx.log 2>&1 & disown

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

# 5. Wait for MusicGen to boot
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

# 7. Egress (wait 5s after ingest)
sleep 5
nohup setsid ffmpeg -hide_banner -loglevel info \
    -i "rtmp://127.0.0.1:1935/fluxrt" -c copy \
    -f flv "rtmps://live.twitch.tv:443/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw" \
    > /tmp/fluxrt-egress-twitch.log 2>&1 & disown

# 8. Watchdog (MusicGen already running, skip grace period)
sleep 10
nohup setsid bash -c '
MUSICGEN_START_TIME=$(($(date +%s) - 200))
export MUSICGEN_START_TIME
bash /workspace/FluxRT/scripts/streaming/watchdog.sh
' > /tmp/fluxrt-watchdog.log 2>&1 & disown
```

### Method B: Using the Start Scripts (Semi-Automated)

The repo includes shell scripts that handle MediaMTX + ingest + egress in one command:

```bash
# Start MediaMTX + ingest + egress in one shot
bash scripts/streaming/start_mediamtx_fanout.sh

# Stop everything
bash scripts/streaming/stop_mediamtx_fanout.sh
```

This script reads `scripts/streaming/rtmp_targets.env` for the Twitch stream key and handles the RTMP relay. You still need to start Gradio, MusicGen, and TTS separately.

**Other available scripts:**

| Script | Purpose |
|--------|---------|
| `start_mediamtx_fanout.sh` | MediaMTX + ingest + egress (current method) |
| `start_rtmp_fanout.sh` | Direct RTMP fanout without MediaMTX (older method) |
| `start_audio_mix_bus.sh` | Separate audio mix bus (UDP 5006 output) |
| `start_continuous_audio_rail.sh` | Never-stopping audio rail with radio fallback |
| `start_stream_monitor_http.sh` | HTTP monitoring endpoint |
| `start_rtmp_fanout_watchdog.sh` | Watchdog for the old RTMP fanout method |

### Method C: From the Gradio UI (Interactive)

The Gradio web UI has buttons to start/stop Music, Quotes, SFX, and Fanout:

1. Open the Gradio URL (from `/tmp/fluxrt-gradio.log`)
2. **Visual tab** — Set your prompt, toggle AI Filter
3. **Music tab** — Choose a station, set sliders, click "Start Music"
4. **Quotes tab** — Configure voice settings, click "Start Quote Voice"
5. **Broadcast tab** — Enable Twitch, click "Start Fanout"

> **Note:** The Gradio UI launches components with different parameters than the CLI commands in Method A. The UI is good for experimentation but the CLI commands are the production-stable configuration.

### Method D: Full Automated Cold Start (Copy-Paste Script)

A single copy-paste block that starts everything from nothing:

```bash
#!/bin/bash
# FluxRT Full Cold Start
cd /workspace/FluxRT

echo "1/8: Starting MediaMTX..."
nohup setsid /workspace/FluxRT/tools/mediamtx/mediamtx /tmp/fluxrt-mediamtx.yml \
    > /tmp/fluxrt-mediamtx.log 2>&1 & disown
sleep 2

echo "2/8: Starting Gradio (GPU 0)..."
taskset -c 0-15 nohup setsid /root/fluxrt-venv/bin/python -u \
    scripts/run_gradio_stream_demo.py --int8 \
    --server-name 0.0.0.0 --server-port 7862 \
    --config-path configs/stream_demo_config.json \
    --local-video /workspace/test_input.mp4 \
    > /tmp/fluxrt-gradio.log 2>&1 & disown

echo "3/8: Starting TTS..."
nohup setsid python3 -u scripts/run_edge_tts_quotes.py \
    --quotes data/quotes/diffusiongemma_quotes.json \
    --loop --shuffle --interval 15 --random-voices \
    --reverb --reverb-mix 0.35 --reverb-decay 0.50 --reverb-delay-ms 60 \
    --echo --echo-mix 0.45 --echo-decay 0.65 --echo-delay-ms 150 \
    --udp-url "udp://127.0.0.1:5004?pkt_size=1316" \
    --repeat-on-empty --cache-dir voices/quote_cache_edge --cache-size 4 \
    > /tmp/fluxrt-tts.log 2>&1 & disown

echo "4/8: Starting MusicGen (GPU 1) — boot takes ~120s..."
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

echo "5/8: Waiting 150s for MusicGen to boot..."
sleep 150

echo "6/8: Starting Ingest ffmpeg..."
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

echo "7/8: Starting Egress to Twitch..."
sleep 5
nohup setsid ffmpeg -hide_banner -loglevel info \
    -i "rtmp://127.0.0.1:1935/fluxrt" -c copy \
    -f flv "rtmps://live.twitch.tv:443/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw" \
    > /tmp/fluxrt-egress-twitch.log 2>&1 & disown

echo "8/8: Starting Watchdog..."
sleep 10
nohup setsid bash -c '
MUSICGEN_START_TIME=$(($(date +%s) - 200))
export MUSICGEN_START_TIME
bash /workspace/FluxRT/scripts/streaming/watchdog.sh
' > /tmp/fluxrt-watchdog.log 2>&1 & disown

echo ""
echo "=== Pipeline Status ==="
sleep 5
echo "MediaMTX:    $(pgrep -f mediamtx > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "Ingest:      $(pgrep -f 'amix=inputs=3' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "Egress:      $(pgrep -f 'twitch.tv' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "MusicGen:    $(pgrep -f 'run_musicgen_radio' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "MusicGen ff: $(pgrep -f 'f32le.*udp' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "Gradio:      $(pgrep -f 'run_gradio_stream' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "TTS:         $(pgrep -f 'run_edge_tts' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "Watchdog:    $(pgrep -f 'watchdog.sh' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
```

---

## 5. How to Run It — Step by Step

### Prerequisites

- RunPod pod with 2× NVIDIA L40 GPUs
- FluxRT repo cloned and models downloaded
- Python venvs set up (`/root/fluxrt-venv` for Gradio, `.venv` for MusicGen)
- Twitch stream key in `scripts/streaming/rtmp_targets.env`

### First-Time Setup

```bash
# Clone the repo
git clone https://github.com/gschian0/FluxRT.git
cd FluxRT
git checkout streaming-l40-self-healing

# Create MediaMTX config
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

# Set your Twitch stream key
cp scripts/streaming/rtmp_targets.env.example scripts/streaming/rtmp_targets.env
# Edit rtmp_targets.env with your stream key
```

### Starting the Pipeline

Use **Method D** (Full Automated Cold Start) from Section 4 above. Copy the entire block and paste it into a terminal.

### Stopping the Pipeline

```bash
# Stop everything
pkill -f "watchdog.sh"
pkill -f "twitch.tv"
pkill -f "amix=inputs=3"
pkill -f "run_musicgen_radio"
pkill -f "run_edge_tts"
pkill -f "run_gradio_stream"
pkill -f "mediamtx"
```

### Restarting a Single Component

```bash
# Restart just egress (most common need)
pkill -f "twitch.tv"
sleep 3
nohup setsid ffmpeg -hide_banner -loglevel info \
    -i "rtmp://127.0.0.1:1935/fluxrt" -c copy \
    -f flv "rtmps://live.twitch.tv:443/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw" \
    > /tmp/fluxrt-egress-twitch.log 2>&1 & disown

# Restart just ingest
pkill -f "amix=inputs=3"
sleep 5
# ... (use the ingest command from Method A step 6)
```

---

## 6. Health Checks & Monitoring

### Quick Status Check

```bash
echo "MediaMTX:    $(pgrep -f mediamtx > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "Ingest:      $(pgrep -f 'amix=inputs=3' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "Egress:      $(pgrep -f 'twitch.tv' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "MusicGen:    $(pgrep -f 'run_musicgen_radio' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "MusicGen ff: $(pgrep -f 'f32le.*udp' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "Gradio:      $(pgrep -f 'run_gradio_stream' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "TTS:         $(pgrep -f 'run_edge_tts' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
echo "Watchdog:    $(pgrep -f 'watchdog.sh' > /dev/null && echo '✅ UP' || echo '❌ DOWN')"
```

### Audio Level Check

```bash
timeout 20 ffmpeg -hide_banner -loglevel info \
    -i "rtmp://127.0.0.1:1935/fluxrt" \
    -t 8 -af "volumedetect" \
    -f null - 2>&1 | grep -E "mean_volume|max_volume"
```

**Expected values:**
- ✅ Good: mean_volume between -15 and -40 dB
- ⚠️ Warning: mean_volume between -40 and -60 dB
- ❌ Silent: mean_volume below -60 dB (watchdog will auto-restart)

### Log Files

| Component | Log File |
|-----------|----------|
| MediaMTX | `/tmp/fluxrt-mediamtx.log` |
| Gradio | `/tmp/fluxrt-gradio.log` |
| MusicGen | `/dev/shm/musicgen/musicgen.log` |
| TTS | `/tmp/fluxrt-tts.log` |
| Ingest | `/tmp/fluxrt-mediamtx-ingest.log` |
| Egress | `/tmp/fluxrt-egress-twitch.log` |
| Watchdog | `/tmp/fluxrt-watchdog.log` |

### Watch Live Logs

```bash
# Watch watchdog
tail -f /tmp/fluxrt-watchdog.log

# Watch MusicGen
tail -f /dev/shm/musicgen/musicgen.log

# Watch egress (Twitch connection)
tail -f /tmp/fluxrt-egress-twitch.log
```

### Check for Self-Healing Events

```bash
# Check if MusicGen's pipe auto-recovered
grep -E "PIPE ERROR|encoder restarted" /dev/shm/musicgen/musicgen.log

# Check watchdog restarts
grep -E "Restarting|silence detected" /tmp/fluxrt-watchdog.log
```

---

## 7. Troubleshooting

### Egress dies immediately after starting

**Cause:** Egress tried to connect to MediaMTX before ingest published a stream.

**Fix:** Wait 5-10 seconds after starting ingest before starting egress. Verify ingest is publishing:
```bash
ffmpeg -i "rtmp://127.0.0.1:1935/fluxrt" -t 2 -f null - 2>&1 | grep "Stream #"
```
If you see video + audio streams, egress can connect.

### MusicGen audio goes silent after hours

**Cause:** ffmpeg pipe broke between Python and the encoder.

**Fix:** The self-healing code should handle this automatically. Check the log:
```bash
grep -E "PIPE ERROR|encoder restarted" /dev/shm/musicgen/musicgen.log
```
If the self-healing didn't work, the watchdog's silence detection will restart MusicGen within 30-60 seconds.

### Watchdog kills MusicGen during boot

**Cause:** Watchdog checked for `now_playing_path.txt` before MusicGen finished booting.

**Fix:** Already fixed with the 180-second grace period. If you start the watchdog when MusicGen is already running, set `MUSICGEN_START_TIME` to 200s in the past:
```bash
MUSICGEN_START_TIME=$(($(date +%s) - 200)) bash scripts/streaming/watchdog.sh
```

### Ingest "Broken pipe" error

**Cause:** MediaMTX dropped the connection (egress disconnected or RTMP hiccup).

**Fix:** Restart ingest, wait 5s, then restart egress. The watchdog handles this automatically.

### UDP port "Address already in use"

**Cause:** Old process hasn't fully died.

**Fix:** Wait 3-5 seconds after killing a process before restarting on the same port:
```bash
pkill -f "amix=inputs=3"
sleep 5  # Wait for port to be released
# Then restart ingest
```

### Gradio share URL not accessible

**Cause:** Gradio share links are temporary and can expire, or the network may block them.

**Fix:** Use the RunPod proxy URL instead: `https://<POD_ID>-7862.proxy.runpod.net/`  
Or access locally: `http://localhost:7862`

---

## 8. Plan: Build All CLI Commands Into the Gradio UI

### Current State

The Gradio UI already has tabs for Music, Quotes, SFX, and Broadcast with start/stop buttons. However, these buttons launch components with **different parameters** than the production CLI commands. The production pipeline is launched from the terminal with specific arguments that aren't reflected in the UI.

### Goal

Make the Gradio UI the **single control panel** for the entire pipeline — every parameter that's currently a CLI argument should be adjustable from the UI, and every component should be startable/stoppable from the UI with the exact same configuration as the CLI commands.

### Phase 1: Add Missing Controls to Existing Tabs

#### Music Tab — Add Missing Parameters

The Music tab already has sliders for gen-seconds, top-k, top-p, temperature, guidance, stream-delay, and crossfade. Missing:

| Parameter | Current CLI | UI Control Needed | Current UI Value | Production Value |
|-----------|-------------|-------------------|------------------|-----------------|
| `--model` | `facebook/musicgen-small` | Dropdown | *(missing)* | `facebook/musicgen-small` |
| `--parallel-clips` | `2` | Slider 1-4 | *(missing)* | `2` |
| `--bootstrap-clips` | `54` | Slider 0-100 | *(missing)* | `54` |
| `--pre-generate` | `10` | Slider 0-20 | *(missing)* | `10` |
| `--conditioning-mode` | `continuation` | Dropdown | *(missing)* | `continuation` |
| `--conditioning-seconds` | `8` | Slider 1-30 | *(missing)* | `8` |
| `--no-drunk-walk` | flag | Checkbox | *(missing)* | `True` (checked) |
| `--seed` | `-1` | Number | *(missing)* | `-1` (random) |
| `--pause-seconds` | `0` | Slider 0-10 | *(missing)* | `0` |
| `--output-dir` | `/dev/shm/musicgen` | Textbox | *(missing)* | `/dev/shm/musicgen` |
| `--audio-udp-url` | `udp://127.0.0.1:5002...` | Textbox | *(missing)* | `udp://127.0.0.1:5002?pkt_size=1316` |

**Implementation:**
```python
# In the Music tab section, add:
with gr.Row():
    musicgen_model = gr.Dropdown(
        choices=["facebook/musicgen-small", "facebook/musicgen-medium", "facebook/musicgen-large"],
        value="facebook/musicgen-small", label="Model"
    )
    musicgen_parallel_clips = gr.Slider(1, 4, value=2, step=1, label="Parallel Clips")
    musicgen_bootstrap_clips = gr.Slider(0, 100, value=54, step=1, label="Bootstrap Clips")
    musicgen_pre_generate = gr.Slider(0, 20, value=10, step=1, label="Pre-generate")

with gr.Row():
    musicgen_conditioning_mode = gr.Dropdown(
        choices=["continuation", "none"], value="continuation", label="Conditioning Mode"
    )
    musicgen_conditioning_seconds = gr.Slider(1, 30, value=8, step=1, label="Conditioning Seconds")
    musicgen_seed = gr.Number(value=-1, label="Seed (-1 = random)")
    musicgen_drunk_walk = gr.Checkbox(value=False, label="Drunk Walk")

with gr.Row():
    musicgen_output_dir = gr.Textbox(value="/dev/shm/musicgen", label="Output Directory")
    musicgen_audio_udp = gr.Textbox(
        value="udp://127.0.0.1:5002?pkt_size=1316", label="Audio UDP URL"
    )
```

Then update the `start_musicgen()` handler to build the CLI command from all UI values:
```python
def start_musicgen(
    model, gen_seconds, top_k, top_p, temperature, guidance_scale,
    stream_delay, crossfade, parallel_clips, bootstrap_clips, pre_generate,
    conditioning_mode, conditioning_seconds, seed, drunk_walk,
    output_dir, audio_udp_url, radio_url, base_prompt, ...
):
    cmd = [
        sys.executable, "-u", "scripts/run_musicgen_radio_plus_musicGEN.py",
        "--radio-url", radio_url or "",
        "--output-dir", output_dir,
        "--model", model,
        "--gen-seconds", str(int(gen_seconds)),
        "--top-k", str(int(top_k)),
        "--top-p", str(top_p),
        "--temperature", str(temperature),
        "--guidance-scale", str(guidance_scale),
        "--parallel-clips", str(int(parallel_clips)),
        "--seed", str(int(seed)),
        "--bootstrap-clips", str(int(bootstrap_clips)),
        "--pre-generate", str(int(pre_generate)),
        "--pause-seconds", "0",
        "--base-prompt", base_prompt,
        "--conditioning-mode", conditioning_mode,
        "--conditioning-seconds", str(int(conditioning_seconds)),
        "--stream-delay-seconds", str(int(stream_delay)),
        "--audio-udp-url", audio_udp_url,
        "--crossfade-seconds", str(crossfade),
    ]
    if not drunk_walk:
        cmd.append("--no-drunk-walk")
    
    env = {"CUDA_VISIBLE_DEVICES": "1", **os.environ}
    proc = subprocess.Popen(cmd, env=env, ...)
    # Store PID for stop button
```

#### Quotes Tab — Add Missing Parameters

The Quotes tab is missing several TTS parameters:

| Parameter | Current CLI | UI Control Needed | Current UI Value | Production Value |
|-----------|-------------|-------------------|------------------|-----------------|
| `--random-voices` | flag | Checkbox | *(missing)* | `True` |
| `--reverb-mix` | `0.35` | Slider 0-1 | *(missing)* | `0.35` |
| `--reverb-decay` | `0.50` | Slider 0-1 | *(missing)* | `0.50` |
| `--reverb-delay-ms` | `60` | Slider 0-500 | *(missing)* | `60` |
| `--echo-mix` | `0.45` | Slider 0-1 | *(missing)* | `0.45` |
| `--echo-decay` | `0.65` | Slider 0-1 | *(missing)* | `0.65` |
| `--echo-delay-ms` | `150` | Slider 0-1000 | *(missing)* | `150` |
| `--udp-url` | `udp://127.0.0.1:5004...` | Textbox | *(missing)* | `udp://127.0.0.1:5004?pkt_size=1316` |
| `--repeat-on-empty` | flag | Checkbox | *(missing)* | `True` |
| `--cache-dir` | `voices/quote_cache_edge` | Textbox | *(missing)* | `voices/quote_cache_edge` |
| `--cache-size` | `4` | Slider 1-20 | *(missing)* | `4` |

**Implementation:** Add a "Advanced TTS Settings" accordion section with these controls.

### Phase 2: Add New "Pipeline" Tab

Create a new tab that controls the infrastructure components (MediaMTX, Ingest, Egress, Watchdog) — things that currently only exist as CLI commands:

```python
with gr.Tab("Pipeline"):
    gr.Markdown("## Infrastructure Control")
    
    with gr.Row():
        # MediaMTX
        mediamtx_status = gr.Textbox(label="MediaMTX Status", interactive=False)
        mediamtx_start_btn = gr.Button("Start MediaMTX")
        mediamtx_stop_btn = gr.Button("Stop MediaMTX")
    
    with gr.Row():
        # Ingest
        ingest_music_vol = gr.Slider(0, 2, value=1.0, step=0.05, label="Music Volume")
        ingest_tts_vol = gr.Slider(0, 2, value=0.85, step=0.05, label="TTS Volume")
        ingest_audio_bitrate = gr.Dropdown(
            choices=["96k", "128k", "160k", "192k"], value="128k", label="Audio Bitrate"
        )
        ingest_start_btn = gr.Button("Start Ingest", variant="primary")
        ingest_stop_btn = gr.Button("Stop Ingest")
        ingest_status = gr.Textbox(label="Ingest Status", interactive=False)
    
    with gr.Row():
        # Egress
        twitch_stream_key = gr.Textbox(
            label="Twitch Stream Key", type="password",
            value="live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw"
        )
        egress_start_btn = gr.Button("Start Egress to Twitch", variant="primary")
        egress_stop_btn = gr.Button("Stop Egress")
        egress_status = gr.Textbox(label="Egress Status", interactive=False)
    
    with gr.Row():
        # Watchdog
        watchdog_grace_period = gr.Slider(60, 300, value=180, step=10, label="Grace Period (s)")
        watchdog_silence_threshold = gr.Slider(-80, -30, value=-60, step=5, label="Silence Threshold (dB)")
        watchdog_start_btn = gr.Button("Start Watchdog", variant="primary")
        watchdog_stop_btn = gr.Button("Stop Watchdog")
        watchdog_status = gr.Textbox(label="Watchdog Status", interactive=False)
    
    with gr.Row():
        # Master controls
        start_all_btn = gr.Button("🚀 Start Entire Pipeline", variant="primary")
        stop_all_btn = gr.Button("🛑 Stop Entire Pipeline", variant="stop")
        pipeline_status = gr.Textbox(label="Full Pipeline Status", lines=8, interactive=False)
```

**Handler functions** would use `subprocess.Popen` to launch each component with the exact CLI commands, and `pgrep` to check status:

```python
def start_ingest(music_vol, tts_vol, bitrate):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-f", "mpegts", "-i", "udp://127.0.0.1:5002?fifo_size=500000&overrun_nonfatal=1",
        "-f", "mpegts", "-i", "udp://127.0.0.1:5004?fifo_size=500000&overrun_nonfatal=1",
        "-f", "mpegts", "-i", "udp://127.0.0.1:5000?fifo_size=1000000&overrun_nonfatal=1",
        "-filter_complex", f"[0:a]volume=1.0[a0];[1:a]volume={music_vol}[a1];[2:a]volume={tts_vol}[a2];[a0][a1][a2]amix=inputs=3:duration=longest:dropout_transition=0[aout]",
        "-map", "3:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", bitrate, "-ar", "48000", "-ac", "2",
        "-f", "flv", "rtmp://127.0.0.1:1935/fluxrt"
    ]
    proc = subprocess.Popen(cmd, stdout=open("/tmp/fluxrt-mediamtx-ingest.log", "w"), stderr=subprocess.STDOUT)
    return f"Ingest started (PID {proc.pid})"

def check_pipeline_status():
    checks = {
        "MediaMTX": "mediamtx",
        "Ingest": "amix=inputs=3",
        "Egress": "twitch.tv",
        "MusicGen": "run_musicgen_radio",
        "MusicGen ffmpeg": "f32le.*udp",
        "Gradio": "run_gradio_stream",
        "TTS": "run_edge_tts",
        "Watchdog": "watchdog.sh",
    }
    lines = []
    for name, pattern in checks.items():
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
        status = f"✅ UP (PID {result.stdout.decode().strip().split(chr(10))[0]})" if result.returncode == 0 else "❌ DOWN"
        lines.append(f"{name}: {status}")
    return "\n".join(lines)
```

### Phase 3: Add "Cold Start" Button

A single button that runs the entire Method D cold start sequence with proper timing:

```python
def cold_start_pipeline():
    """Start all 8 components in the correct order with proper delays."""
    steps = []
    
    # 1. MediaMTX
    subprocess.Popen(["/workspace/FluxRT/tools/mediamtx/mediamtx", "/tmp/fluxrt-mediamtx.yml"],
                     stdout=open("/tmp/fluxrt-mediamtx.log", "w"), stderr=subprocess.STDOUT,
                     start_new_session=True)
    steps.append("1/8: MediaMTX started")
    time.sleep(2)
    
    # 2. Gradio (already running if UI is up — skip)
    steps.append("2/8: Gradio already running (you're using it)")
    
    # 3. TTS
    # ... launch TTS with subprocess.Popen
    steps.append("3/8: TTS started")
    
    # 4. MusicGen
    # ... launch MusicGen with subprocess.Popen, env={"CUDA_VISIBLE_DEVICES": "1"}
    steps.append("4/8: MusicGen started (booting ~120s)")
    
    # 5. Wait for boot
    steps.append("5/8: Waiting 150s for MusicGen boot...")
    # Note: Can't sleep 150s in a Gradio handler — need to use a background thread
    # or use gr.Timer / periodic check
    
    # 6-8. Ingest, Egress, Watchdog
    # ...
    
    return "\n".join(steps)
```

> **Challenge:** The 150-second wait for MusicGen boot can't be done synchronously in a Gradio handler (it would block the UI). Options:
> - Use a background thread with `threading.Thread`
> - Use Gradio's `gr.Timer` to poll status
> - Start MusicGen first, then use a periodic callback to start ingest/egress once `now_playing_path.txt` exists

### Phase 4: Add Prompt Rotator Control

Add controls for the prompt rotator to the Visual tab:

```python
with gr.Accordion("Prompt Rotator", open=False):
    prompt_rotator_interval = gr.Slider(30, 600, value=120, step=10, label="Rotation Interval (s)")
    prompt_rotator_prompts = gr.Textbox(
        value="\n".join(PROMPTS[:5]),  # Pre-fill with first 5 prompts
        label="Prompts (one per line)", lines=10
    )
    prompt_rotator_start_btn = gr.Button("Start Prompt Rotation")
    prompt_rotator_stop_btn = gr.Button("Stop Prompt Rotation")
    prompt_rotator_status = gr.Textbox(label="Rotator Status", interactive=False)
```

### Phase 5: Persist Settings

Save all UI settings to a JSON file so they survive restarts:

```python
def save_pipeline_config(state):
    """Save all UI values to configs/pipeline_state.json"""
    config = {
        "musicgen": {
            "model": state["model"],
            "gen_seconds": state["gen_seconds"],
            # ... all musicgen params
        },
        "tts": {
            # ... all tts params
        },
        "ingest": {
            "music_volume": state["music_vol"],
            "tts_volume": state["tts_vol"],
            # ...
        },
        "watchdog": {
            "grace_period": state["grace_period"],
            "silence_threshold": state["silence_threshold"],
        }
    }
    with open("configs/pipeline_state.json", "w") as f:
        json.dump(config, f, indent=2)

def load_pipeline_config():
    """Load saved settings on UI startup"""
    try:
        with open("configs/pipeline_state.json") as f:
            return json.load(f)
    except FileNotFoundError:
        return None  # Use defaults
```

### Implementation Priority

| Phase | Effort | Impact | Priority |
|-------|--------|--------|----------|
| Phase 1: Missing Music/Quotes controls | Medium | High | 🔴 Do first |
| Phase 2: Pipeline tab | Medium | High | 🔴 Do first |
| Phase 3: Cold Start button | Hard | Very High | 🟡 Do second |
| Phase 4: Prompt rotator | Low | Medium | 🟢 Do third |
| Phase 5: Settings persistence | Low | High | 🟢 Do third |

### Key Challenges

1. **Process management from Gradio** — Gradio handlers run in the request thread. Long-running operations (like waiting 150s for MusicGen boot) need background threads or async handling.
2. **Process lifecycle** — Need to track PIDs, handle process death, and clean up zombie processes.
3. **GPU isolation** — MusicGen needs `CUDA_VISIBLE_DEVICES=1` and `taskset -c 16-63`. Gradio needs `CUDA_VISIBLE_DEVICES=0` and `taskset -c 0-15`. These must be set correctly when launching from the UI.
4. **Port conflicts** — Need to check if ports 5000/5002/5004/1935 are already in use before starting components.
5. **Gradio restart** — If Gradio itself restarts, it loses track of launched subprocesses. Need PID files or process discovery via `pgrep`.

---

## 9. File Reference

### Core Scripts

| File | Lines | Purpose |
|------|-------|---------|
| `scripts/run_gradio_stream_demo.py` | ~2700 | Video generation (FLUX.2) + Gradio web UI |
| `scripts/run_musicgen_radio_plus_musicGEN.py` | ~1010 | Music generation (MusicGen) with self-healing pipe |
| `scripts/run_edge_tts_quotes.py` | ~600 | TTS voice quotes with reverb/echo |
| `scripts/streaming/watchdog.sh` | 310 | Auto-recovery watchdog with grace period + silence detection |
| `scripts/streaming/rotate_flux_prompt.py` | 103 | Prompt rotation via Gradio API |

### Infrastructure Scripts

| File | Purpose |
|------|---------|
| `scripts/streaming/start_mediamtx_fanout.sh` | Start MediaMTX + ingest + egress |
| `scripts/streaming/stop_mediamtx_fanout.sh` | Stop MediaMTX + ingest + egress |
| `scripts/streaming/start_rtmp_fanout.sh` | Direct RTMP fanout (no MediaMTX) |
| `scripts/streaming/start_audio_mix_bus.sh` | Separate audio mix bus |
| `scripts/streaming/start_continuous_audio_rail.sh` | Never-stopping audio rail with radio fallback |
| `scripts/streaming/start_stream_monitor_http.sh` | HTTP monitoring endpoint |

### Config Files

| File | Purpose |
|------|---------|
| `configs/stream_demo_config.json` | Gradio/FLUX.2 settings (resolution, model, seed, etc.) |
| `scripts/streaming/rtmp_targets.env` | Twitch stream key and platform toggles |
| `/tmp/fluxrt-mediamtx.yml` | MediaMTX RTMP relay config |

### Documentation

| File | Purpose |
|------|---------|
| `SUPER_README.md` | This file — complete guide |
| `RUNBOOK_L40_STREAMING.md` | Quick-reference runbook with exact commands |
| `SETTINGS_SNAPSHOT_2026-07-08.md` | Complete settings snapshot for reproduction |
| `README.md` | Project overview and historical context |

### Git History

| Commit | Description |
|--------|-------------|
| `33cbb6b` | Settings snapshot documentation |
| `7b092ef` | README update for streaming-l40-self-healing branch |
| `e5cae6c` | L40 streaming runbook |
| `63278e4` | Self-healing MusicGen pipe + watchdog grace period & silence detection |

### Restore From Scratch

```bash
git clone https://github.com/gschian0/FluxRT.git
cd FluxRT
git checkout streaming-l40-self-healing
# Follow Section 5: How to Run It — Step by Step
```
