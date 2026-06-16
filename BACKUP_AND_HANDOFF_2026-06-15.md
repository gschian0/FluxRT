# FluxRT Backup and Handoff (2026-06-15)

This document captures the current state of the streaming + MusicGen integration work and how to preserve it.

## What Is Implemented

### 1) RTMP fanout hardening (dropout-resistant)
- File: `scripts/streaming/start_rtmp_fanout.sh`
- Added:
  - UDP buffer hardening with `fifo_size` and `overrun_nonfatal`
  - Larger thread queues for video/audio inputs
  - `aresample=async=1` to maintain audio continuity on jitter
  - safer ffmpeg mux settings (`max_muxing_queue_size`, low muxdelay)
- Current stability defaults:
  - `VIDEO_BITRATE=450k`
  - `FPS=6`
  - `OUTPUT_WIDTH=256`
  - `OUTPUT_HEIGHT=144`
  - `VIDEO_BUFSIZE=900k`

### 2) Fanout watchdog auto-restart
- New files:
  - `scripts/streaming/start_rtmp_fanout_watchdog.sh`
  - `scripts/streaming/stop_rtmp_fanout_watchdog.sh`
- Purpose:
  - keeps fanout alive by restarting it if the ffmpeg process exits.

### 3) MusicGen controls based on Audiocraft demo-style generation params
- File: `scripts/run_musicgen_radio_plus_musicGEN.py`
- Added generation controls:
  - `--top-k` (default `250`)
  - `--temperature` (default `1.0`)
- These values are now applied in `model.generate(...)`.
- Metadata JSON now includes `top_k`, `temperature`, and `guidance_scale`.

### 4) Launcher wiring for new generation controls
- File: `scripts/start_musicgen_radio_plus_musicGEN.sh`
- Added env passthrough:
  - `MUSICGEN_TOP_K` -> `--top-k`
  - `MUSICGEN_TEMPERATURE` -> `--temperature`

### 5) Gradio path wiring (explicit defaults)
- File: `scripts/run_gradio_stream_demo.py`
- Added defaults when starting MusicGen from UI:
  - `MUSICGEN_TOP_K=250`
  - `MUSICGEN_TEMPERATURE=1.0`

## Operational Commands (Current)

### Start fanout (Twitch-only example)
```bash
cd /home/gschi/FluxRT
AUDIO_SOURCE_MODE=url \
AUDIO_INPUT_URL='udp://127.0.0.1:5002?pkt_size=1316' \
ENABLE_YOUTUBE=0 ENABLE_TWITCH=1 ENABLE_FACEBOOK=0 \
scripts/streaming/start_rtmp_fanout.sh
```

### Start/stop fanout watchdog
```bash
cd /home/gschi/FluxRT
scripts/streaming/start_rtmp_fanout_watchdog.sh
scripts/streaming/stop_rtmp_fanout_watchdog.sh
```

### Start MusicGen with generation controls
```bash
cd /home/gschi/FluxRT
RADIO_URL='http://uk2.internet-radio.com:8024/' \
MUSICGEN_TOP_K=250 \
MUSICGEN_TEMPERATURE=1.0 \
MUSICGEN_STREAM_DELAY_SECONDS=8 \
scripts/start_musicgen_radio_plus_musicGEN.sh
```

## Logs and PIDs
- Fanout log: `/tmp/fluxrt-rtmp-fanout.log`
- Fanout pid: `/tmp/fluxrt-rtmp-fanout.pid`
- Watchdog log: `/tmp/fluxrt-rtmp-fanout-watchdog.log`
- Watchdog pid: `/tmp/fluxrt-rtmp-fanout-watchdog.pid`
- MusicGen log: `/tmp/fluxrt-musicgen-radio.log`

## Backup Strategy

Use `scripts/backup/create_fluxrt_snapshot.sh` to generate:
1. A timestamped project archive in `backups/`
2. A git patch for tracked changes
3. A file list + git status snapshot

This keeps a quick restore path even if the VM is interrupted.
