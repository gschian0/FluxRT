# FluxRT Stable Audio + Video Stream (2026-07-07)

## Status: ✅ WORKING — Video + Music streaming to Twitch

This branch (`stable-audio-video`) is a known-good checkpoint of the
MediaMTX fanout streaming pipeline with **video + music audio** working
end-to-end on RunPod (L40 GPU, Ubuntu 24.04 minimal).

TTS overlay is **disabled** in this checkpoint and will be re-enabled next.

---

## Architecture

```
Gradio (UDP 5000, H264/MPEG-TS)  ──┐
                                    ├──► [ingest ffmpeg] ──► MediaMTX :1935/fluxrt ──► [egress ffmpeg] ──► Twitch RTMPS
MusicGen (UDP 5002, AAC/MPEG-TS) ──┘    3-input amix            (local RTMP)           -c copy -f flv      live.twitch.tv:443
   │                                                                                                          /app/{stream_key}
   └── base silence (anullsrc) always present
   └── TTS silence (anullsrc) — TTS disabled, volume=0.0
```

### Data flow

1. **Gradio** (`scripts/run_gradio_stream_demo.py`) generates AI video frames,
   encodes them as H264/MPEG-TS, and writes to `udp://127.0.0.1:5000`
2. **MusicGen** (`scripts/run_musicgen_radio_plus_musicGEN.py`) generates music,
   encodes as AAC/MPEG-TS, and writes to `udp://127.0.0.1:5002`
3. **Ingest ffmpeg** (loop script) reads all three inputs (video + base silence
   + music), mixes audio with `amix`, copies video (`-c:v copy`), encodes audio
   to AAC, and publishes to `rtmp://127.0.0.1:1935/fluxrt`
4. **MediaMTX** (`tools/mediamtx/mediamtx`) accepts the RTMP publish on path
   `fluxrt`
5. **Egress ffmpeg** (loop script) reads from `rtmp://127.0.0.1:1935/fluxrt`,
   copies both streams (`-c copy`), and pushes to Twitch via RTMPS

---

## Key Files

| File | Purpose |
|------|---------|
| `scripts/streaming/start_mediamtx_fanout.sh` | Main launcher — starts MediaMTX, ingest loop, egress loop |
| `scripts/streaming/stop_mediamtx_fanout.sh` | Clean shutdown — kills all fanout processes |
| `scripts/streaming/rtmp_targets.env` | Twitch stream key and target URL (NOT committed — gitignored) |
| `configs/stream_demo_config.json` | Gradio config (prompt, model path, resolution) |
| `scripts/run_musicgen_radio_plus_musicGEN.py` | MusicGen radio generator + UDP streamer |
| `scripts/run_gradio_stream_demo.py` | Gradio video generator + UDP streamer |
| `tools/mediamtx/mediamtx` | MediaMTX v1.8.4 binary (local RTMP server) |

---

## Environment Variables

### Fanout script defaults (overridable via env)

| Variable | Default | Description |
|----------|---------|-------------|
| `VIDEO_INPUT_URL` | `udp://127.0.0.1:5000?pkt_size=1316&...` | Gradio video source |
| `AUDIO_INPUT_URL` | `udp://127.0.0.1:5002?pkt_size=1316&...` | MusicGen audio source |
| `TTS_INPUT_URL` | `udp://127.0.0.1:5004?pkt_size=1316&...` | TTS audio source (unused in this checkpoint) |
| `ENABLE_TTS_OVERLAY` | `1` | Set to `0` to disable TTS (this checkpoint uses `0`) |
| `MUSIC_MIX_VOLUME` | `0.65` | Music volume in mix (this checkpoint uses `0.85`) |
| `TTS_MIX_VOLUME` | `1.80` | TTS volume (irrelevant when TTS disabled) |
| `OUTPUT_WIDTH` | `426` | Output video width |
| `OUTPUT_HEIGHT` | `240` | Output video height |
| `FPS` | `12` | Output framerate |
| `VIDEO_BITRATE` | `1200k` | Video bitrate (transcode mode only) |
| `VIDEO_TRANSCODE_MODE` | `copy` | `copy` = passthrough H264, `transcode` = re-encode |
| `MUSIC_WAIT_TIMEOUT` | `120` | Seconds to wait for MusicGen before silence fallback |
| `VIDEO_WAIT_TIMEOUT` | `30` | Seconds to wait for video before fallback |
| `VIDEO_FALLBACK_ON_MISS` | `1` | Fall back to synthetic black video if no input |

### Stack profile (`/workspace/.stack-profile.env`)

Key settings used on RunPod:
```
MUSICGEN_DELAY=0
TTS_MIX_VOLUME=0
USE_MEDIAMTX_FANOUT=1
MUSIC_MIX_VOLUME=0.85
BROADCAST_FPS=8
FANOUT_FPS=8
ENABLE_TTS_OVERLAY=0
AUTO_START_STREAM=1
GRADIO_PORT=7862
```

---

## How to Start (Manual)

### 1. Start Gradio (video source)

```bash
cd /workspace/FluxRT
python -u scripts/run_gradio_stream_demo.py \
  --int8 --server-name 0.0.0.0 --server-port 7862 \
  --config-path configs/stream_demo_config.json \
  --local-video /workspace/test_input.mp4
```

Gradio writes H264/MPEG-TS to `udp://127.0.0.1:5000`.

### 2. Start MusicGen (audio source)

```bash
cd /workspace/FluxRT
bash scripts/start_musicgen_radio_plus_musicGEN.sh
```

Or directly:
```bash
uv run python -u scripts/run_musicgen_radio_plus_musicGEN.py \
  --radio-url http://stream.zeno.fm/0a4yq1u0f0hvv \
  --output-dir /workspace/FluxRT/musicgen_output_bass_chords_pads \
  --model facebook/musicgen-small \
  --sample-seconds 12 --gen-seconds 30 \
  --top-k 100 --top-p 0.80 --temperature 0.74 \
  --guidance-scale 3.2 --no-drunk-walk \
  --parallel-clips 1 --seed 424242 \
  --bootstrap-clips 12 --pause-seconds 0 \
  --base-prompt "complete 90 second instrumental diddy, electronic chill downtempo, fat reggae dub sub bassline, cool jazz chord progression, lush synth pads, airy melodies, warm chord stabs, light understated drums, steady club lounge groove, cohesive songform same key throughout, clear intro and steady develop, no vocals, no abrupt style change" \
  --conditioning-mode text --conditioning-seconds 12 \
  --stream-delay-seconds 0 \
  --audio-udp-url "udp://127.0.0.1:5002?pkt_size=1316" \
  --crossfade-seconds 2.5
```

MusicGen writes AAC/MPEG-TS to `udp://127.0.0.1:5002`.

### 3. Start the fanout (MediaMTX + ingest + egress)

```bash
cd /workspace/FluxRT
ENABLE_TTS_OVERLAY=0 MUSIC_MIX_VOLUME=0.85 bash scripts/streaming/start_mediamtx_fanout.sh
```

### 4. Stop everything

```bash
cd /workspace/FluxRT
bash scripts/streaming/stop_mediamtx_fanout.sh
```

---

## Fixes Applied in This Checkpoint

### 1. `ss` command not found on RunPod

RunPod's minimal Ubuntu image doesn't have `iproute2` (the `ss` command).
Replaced the MediaMTX port-1935 readiness check with bash's built-in
`/dev/tcp` which requires no external packages.

**File:** `scripts/streaming/start_mediamtx_fanout.sh` (lines ~98-106)

```bash
# Before (broken on RunPod):
if ss -ltn | grep -q ':1935 '; then

# After (works everywhere):
if (echo > /dev/tcp/127.0.0.1/1935) 2>/dev/null; then
```

### 2. Increased wait timeouts

- `MUSIC_WAIT_TIMEOUT`: 30s → 120s (MusicGen can take 20-30s to generate
  the first clip before it starts streaming)
- `VIDEO_WAIT_TIMEOUT`: 6s → 30s (Gradio may take a few seconds to start
  writing to UDP 5000)

### 3. Video no longer falls back to synthetic

The original script would fall back to `color=c=black` (synthetic video) if
UDP 5000 wasn't ready within the timeout. This caused a crash because
`wrapped_avframe` from the `color` source is incompatible with `-c:v copy`
to FLV. Now the script binds the UDP URL directly and waits for video to
arrive, rather than falling back to synthetic.

When synthetic video IS used (explicit `VIDEO_SOURCE_MODE=synthetic`),
`VIDEO_TRANSCODE_MODE` is automatically set to `transcode` to avoid the
codec incompatibility.

### 4. Removed global probe settings

Removed the global `-analyzeduration 2M -probesize 2M` from the ingest
ffmpeg command. This was causing ffmpeg to block for extended periods
trying to probe UDP inputs that didn't have data yet. Per-input probe
settings are handled by the input URL parameters.

### 5. Stop script: suppress stderr

Added `2>/dev/null` to `pkill` commands in `stop_mediamtx_fanout.sh` to
avoid noisy error messages when no matching processes exist.

---

## Known Issues / Next Steps

### TTS Overlay (TODO)

TTS is disabled in this checkpoint (`ENABLE_TTS_OVERLAY=0`). To re-enable:

1. Start the TTS generation service writing to `udp://127.0.0.1:5004`
2. Set `ENABLE_TTS_OVERLAY=1` when launching the fanout
3. Set `TTS_MIX_VOLUME` to desired level (default 1.80)

The 3-input amix filter is already in place — it just needs the TTS
audio source on UDP 5004 and the flag enabled.

### MusicGen playback thread crash

If the UDP consumer on port 5002 is killed while MusicGen is streaming,
the playback thread crashes with `BrokenPipeError` and MusicGen stops
streaming (though it continues generating). Restart MusicGen if this
happens:

```bash
pkill -f 'run_musicgen_radio_plus_musicGEN'
sleep 2
bash scripts/start_musicgen_radio_plus_musicGEN.sh
```

### Old architecture processes

If you previously ran the old (non-fanout) streaming architecture, make
sure to kill all old processes before starting the fanout:

```bash
pkill -9 -f '/tmp/fluxrt-audio-rail-loop.sh'
pkill -9 -f '/tmp/fluxrt-audio-mix-loop.sh'
pkill -9 -f '/tmp/fluxrt-hls-loop.sh'
pkill -9 -f '/tmp/fluxrt-twitch-loop.sh'
fuser -k 5000/udp 5002/udp 5004/udp 5006/udp 2>/dev/null
```

---

## Verified Working

- **Date:** 2026-07-07
- **Platform:** RunPod pod v89vkr1b3dhgua, dual L40 GPUs
- **Twitch stream:** Live with video (288x160, 8fps, H264) + music (AAC, 48kHz stereo)
- **Bitrate:** ~400 kbits/s
- **Latency:** ~3-5 seconds end-to-end
