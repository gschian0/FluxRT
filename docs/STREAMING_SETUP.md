# FluxRT Streaming Pipeline — Complete Setup Guide

## Current State (as of 2026-07-08 20:35 UTC)

### Pipeline Status: ✅ ALL GREEN
| Component | PID | Status |
|-----------|-----|--------|
| MediaMTX | 1135518 | ✅ UP |
| Gradio (video) | 1512018 | ✅ UP (GPU 0, 16.6 GB) |
| MusicGen (music) | 1528320 | ✅ UP (GPU 1, 3.4 GB, RTF=0.56, 16s clips) |
| Edge TTS (quotes) | 1530056 | ✅ UP |
| Amix ffmpeg | 1530075 | ✅ UP |
| Egress ffmpeg | 1530421 | ✅ UP → Twitch |
| Watchdog | 1530901 | ✅ UP |

### Gradio Web UI
- **Share URL**: `https://ea2790c148262eed6d.gradio.live`
- **Local URL**: `http://127.0.0.1:7862`
- **Note**: Share URL is temporary (lasts up to 1 week, best effort). If it dies, restart Gradio to get a new one.

### Changes Made This Session
1. **Ticker revert** (`run_gradio_stream_demo.py`): Reverted complex temp-strip clipping back to simple `cv2.putText` with `scroll_range = max(1, text_w + w)`. The complex clipping was causing rendering issues.
2. **BPM 120 in MusicGen prompts** (`run_musicgen_radio_plus_musicGEN.py`): Added `--bpm 120` CLI arg. All prompts now include "120 BPM" so generation length aligns with tempo. New constants `_DEFAULT_BPM = 120` and `_SEED_ROTATION_INTERVAL = 4`.
3. **Faster music evolution** (`run_musicgen_radio_plus_musicGEN.py`): Seed prompts now rotate every 4 clips (`cycle_index // _SEED_ROTATION_INTERVAL`) instead of keeping one seed forever. Continuation conditioning still keeps transitions smooth.
4. **Watchdog fixes** (`watchdog.sh`):
   - Grace period increased from 180s → 360s (model load ~30s + pre-gen 10 clips ~200s + buffer)
   - Music volume increased from 1.0 → 1.3 in amix restart command
   - `--bpm 120` added to MusicGen restart command
   - Radio URL set to `https://ice64.securenetsystems.net/LFTM` in restart
   - **Bug fix**: Added pre-loop check for already-running MusicGen — sets `MUSICGEN_START_TIME=$(date +%s)` to prevent immediate kill (was defaulting to 0, making uptime = epoch time ~1.78 billion seconds, bypassing grace period)
5. **Music volume boost**: Amix ffmpeg music channel volume increased from 1.0 → 1.3 (both in live process and watchdog restart command)
6. **16s clip profile** (audio skip fix): Changed from 8s clips to 16s clips — halves the transition frequency without breaking continuation conditioning. 8s clips with 2s crossfade = 25% transition zone, 7.5 transitions/min. 16s clips with 2s crossfade = 12.5% transition zone, 3.75 transitions/min. RTF=0.56 (1.78x realtime throughput). The 5090 branch used 30s clips but the L40 GPU is too slow for that (RTF=1.18, slower than realtime + CUDA crashes). 16s is the sweet spot for L40.

---

## Current Hardware (RunPod L40 Pod)
- **CPU**: AMD EPYC 7773X 64-Core (128 cores / 256 threads, ~2.2 GHz base)
- **RAM**: 2 TB (only ~160 GB used)
- **GPU 0**: NVIDIA L40 46GB — Gradio/FLUX video generation (16.6 GB used, 57% util)
- **GPU 1**: NVIDIA L40 46GB — MusicGen audio generation (3.2 GB used, 24% util)
- **Disk**: 150 GB overlay (31 GB used), 233 GB /dev/shm

## Bottleneck Analysis
- **CPU is the bottleneck**: Gradio's video generation child process uses ~500% CPU (5 cores)
- The EPYC has high core count but low per-core clock speed (~2.2 GHz)
- A faster CPU with higher single-thread performance would help:
  - AMD Ryzen 9 7950X (5.7 GHz boost, 16 cores)
  - Intel Core i9-14900K (6.0 GHz boost, 24 cores)
  - AMD Threadripper PRO 7995WX (5.1 GHz boost, 96 cores) — best of both worlds
- RAM and GPUs are not constraints at all
- **Known issue**: Slight stutters/skips in the video feed, likely caused by CPU-bound video generation not maintaining consistent 8fps frame timing

---

## Pipeline Architecture

```
Gradio (FLUX.2 video gen)     MusicGen (AI music)      Edge TTS (quotes)
     ↓ raw video                    ↓ audio                   ↓ audio
  ffmpeg → UDP 5000          ffmpeg → UDP 5002        ffmpeg → UDP 5004
     \                              |                          /
      \                             |                         /
       →→→ amix ffmpeg (3 inputs + anullsrc) ←←←/
            ↓ video copy + audio mix (vol: base=1.0, music=1.3, tts=0.85)
          MediaMTX (RTMP :1935, path "fluxrt")
            ↓
          egress ffmpeg → Twitch RTMP
```

---

## Exact Commands

### 1. MediaMTX
```bash
/workspace/FluxRT/tools/mediamtx/mediamtx /tmp/fluxrt-mediamtx.yml
```

**Config** (`/tmp/fluxrt-mediamtx.yml`):
```yaml
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
```

### 2. Gradio (Video Generation) — GPU 0
```bash
cd /workspace/FluxRT
GRADIO_SHARE=1 taskset -c 0-15 \
  /root/fluxrt-venv/bin/python -u scripts/run_gradio_stream_demo.py \
    --int8 --server-name 0.0.0.0 --server-port 7862 \
    --config-path configs/stream_demo_config.json \
    --local-video /workspace/test_input.mp4
```

**Config** (`configs/stream_demo_config.json`):
```json
{
    "default_prompt": "claymation, shiny texture, cartoon, computer render",
    "default_steps": 1,
    "default_seed": 52,
    "models_path": "FLUX.2-klein-4B",
    "int8_models_path": "FLUX.2-klein-4B-int8",
    "resolution": { "height": 160, "width": 288 },
    "compile_models": false,
    "enable_spatial_cache": true,
    "use_lora": false,
    "lora_weights_path": "lora/lora.safetensors",
    "enable_int8_quantization": true,
    "target_fps": null,
    "interpolation_exp": 1,
    "use_reference_image": false,
    "reference_image_path": "local_samples/reference.png",
    "reference_image_resolution": { "height": 256, "width": 256 },
    "lip_transfer": { "enable": false, "models_dir": "LivePortrait/liveportrait" },
    "logging": false
}
```

- **Python**: 3.12.13 (`/root/fluxrt-venv/bin/python`)
- **CPU affinity**: cores 0-15 (taskset)
- **GPU**: 0 (default, no CUDA_VISIBLE_DEVICES set)
- **Output**: raw BGR24 288x160 @ 8fps → ffmpeg → UDP 5000 (mpegts)

### 3. MusicGen (AI Music) — GPU 1
```bash
cd /workspace/FluxRT
CUDA_VISIBLE_DEVICES=1 taskset -c 16-63 \
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
    --crossfade-seconds 2.0
```

- **Python**: 3.12.13 (`/workspace/FluxRT/.venv/bin/python3`)
- **CPU affinity**: cores 16-63 (taskset)
- **GPU**: 1 (`CUDA_VISIBLE_DEVICES=1`)
- **Pre-gen**: 10 clips (~200s) before streaming starts
- **Bootstrap**: 24 pre-existing clips from /dev/shm/musicgen
- **Output**: f32le audio → ffmpeg → UDP 5002 (mpegts, AAC 128k)
- **RTF**: ~0.56 (realtime factor — generates 1.78x faster than realtime)

### 4. Edge TTS (Quotes) — CPU only
```bash
cd /workspace/FluxRT
/usr/bin/python3 -u scripts/run_edge_tts_quotes.py \
  --quotes data/quotes/diffusiongemma_quotes.json \
  --loop --shuffle --interval 15 --random-voices \
  --reverb --reverb-mix 0.35 --reverb-decay 0.50 --reverb-delay-ms 60 \
  --echo --echo-mix 0.45 --echo-decay 0.65 --echo-delay-ms 150 \
  --udp-url "udp://127.0.0.1:5004?pkt_size=1316" \
  --repeat-on-empty --cache-dir voices/quote_cache_edge --cache-size 4
```

- **Python**: 3.12.3 (`/usr/bin/python3`)
- **Output**: s16le audio → ffmpeg → UDP 5004 (mpegts, AAC 192k)

### 5. Amix FFmpeg (Audio Mixer + Video Passthrough)
```bash
ffmpeg -hide_banner -loglevel error -fflags +genpts+discardcorrupt+igndts \
  -thread_queue_size 16384 -i "udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
  -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000" \
  -thread_queue_size 16384 -i "udp://127.0.0.1:5002?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
  -thread_queue_size 16384 -i "udp://127.0.0.1:5004?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
  -map 0:v:0 -map "[aout]" \
  -filter_complex "[1:a]volume=1.0[base];[2:a]volume=1.3[music];[3:a]volume=0.85[tts];[base][music][tts]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0,aresample=async=1:min_hard_comp=0.100:first_pts=0[aout]" \
  -c:v copy -c:a aac -b:a 96k -ar 48000 -ac 2 \
  -max_muxing_queue_size 4096 -muxdelay 0 -muxpreload 0 \
  -f flv rtmp://127.0.0.1:1935/fluxrt
```

- **Input 0**: Video from UDP 5000 (Gradio)
- **Input 1**: anullsrc (silence base for audio mix)
- **Input 2**: Music from UDP 5002 (MusicGen) — volume 1.3
- **Input 3**: TTS from UDP 5004 (Edge TTS) — volume 0.85
- **Output**: RTMP to MediaMTX path "fluxrt"

### 6. Egress FFmpeg (Twitch)
```bash
ffmpeg -hide_banner -loglevel warning -fflags +genpts+discardcorrupt+igndts \
  -i rtmp://127.0.0.1:1935/fluxrt \
  -c copy -f flv "rtmps://live.twitch.tv:443/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw"
```

### 7. Watchdog
```bash
cd /workspace/FluxRT
nohup setsid bash scripts/streaming/watchdog.sh > /tmp/fluxrt-watchdog.log 2>&1 & disown
```

- **Check interval**: 30 seconds
- **MusicGen grace period**: 360 seconds (model load ~30s + pre-gen ~200s + buffer)
- **Auto-restarts**: MediaMTX, Ingest, Egress, MusicGen, TTS, Gradio
- **Silence detection**: Checks mean_volume on RTMP stream, restarts MusicGen if < -60 dB

---

## Startup Order

1. MediaMTX
2. Gradio (wait ~30s for model load + video encoder spawn)
3. MusicGen (wait ~230s for model load + 10 pre-gen clips)
4. TTS
5. Amix ffmpeg (wait for publisher confirmation in MediaMTX log)
6. Egress ffmpeg (wait for amix to be publishing first)
7. Watchdog (after everything is stable)

## Key Files
- `/workspace/FluxRT/scripts/streaming/watchdog.sh` — auto-recovery watchdog
- `/workspace/FluxRT/scripts/run_musicgen_radio_plus_musicGEN.py` — MusicGen
- `/workspace/FluxRT/scripts/run_edge_tts_quotes.py` — Edge TTS
- `/workspace/FluxRT/scripts/run_gradio_stream_demo.py` — Gradio video gen
- `/workspace/FluxRT/configs/stream_demo_config.json` — Gradio config
- `/tmp/fluxrt-mediamtx.yml` — MediaMTX config
- `/dev/shm/musicgen/` — MusicGen clip cache (tmpfs)
- `/tmp/fluxrt-watchdog.log` — Watchdog log
- `/dev/shm/musicgen/musicgen.log` — MusicGen log

## Known Issues
- **Overlay broken**: Gradio share URL times out, overlay not rendering. Stream works without it.
- **Stutter**: Slight stutters/skips in the feed — likely CPU-bound video generation.
- **MusicGen RTF ~1.2**: Generates slightly slower than realtime, relies on bootstrap buffer.
