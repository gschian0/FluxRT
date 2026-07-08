# FluxRT — Complete Settings Snapshot (2026-07-08)

> **Branch:** `streaming-l40-self-healing`  
> **Captured:** 2026-07-08, all 8 components UP, audio at -20.4 dB  
> **Purpose:** Exact reproduction reference for the current working state

---

## 1. Gradio UI Settings (Live Console)

These are the current values visible in the Gradio web UI at `http://localhost:7862`.

### Source Tab

| Setting | Value |
|---------|-------|
| Mode | `local` |
| Source status | `initializing` |
| Choose local video | `/workspace/test_input.mp4` |

### Stream Tab (IPTV source — not actively used)

| Setting | Value |
|---------|-------|
| Catalog Group | `us_30a` |
| Channel Choice | `30A Darcizzle Offshore (720p) [1]` |
| Stream URL | `https://streamer1.connectto.com/AABC_WEB_1201/index.m3u8` |
| M3U Catalog | *(empty)* |

### Visual Tab

| Setting | Value |
|---------|-------|
| Manual Text Prompt | `8k ultra high resolution claymation character video frame, expressive handmade alien hosts, visible fingerprints in clay, miniature broadcast studio, saturated practical lights, crisp macro lens detail, cinematic depth of field` |
| Auto Prompt | `False` (unchecked) |
| AI Filter | `True` (checked) |
| Image Prompt Status | `image prompt: manual text prompt mode` |
| Reference Image | *(none)* |

### Music Tab

| Setting | Value |
|---------|-------|
| Music Station | `00-dub-groove-stations \| Reggae King Radio` |
| Radio URL | `http://stream.zeno.fm/0a4yq1u0f0hvv` |
| Music Prompt | `cool groove, electronic chill downtempo, bassline and chords lead the track, fat reggae dub sub bassline, cool jazz chord progression, lush synth pads, airy ethereal melodies throughout, warm chord stabs, light understated drums, steady instrumental club lounge mix` |
| M3U Catalog | `#EXTM3U\n#EXTINF:-1,Reggae King Radio\nhttp://stream.zeno.fm/0a4yq1u0f0hvv\n#EXTINF:-1,Roots Legacy Radio\nhttp://rootslegacy.ddns.net:8000/stream` |
| Sample Seconds | `8` |
| Gen Seconds | `8` |
| Top-k | `110` |
| Top-p | `0.82` |
| Temperature | `0.78` |
| Guidance Scale | `3.4` |
| Stream Delay Seconds | `45` |
| Crossfade Seconds | `1.2` |
| MusicGen Status | `idle` |

> **Note:** The Gradio UI music settings differ from the actual MusicGen CLI args because MusicGen is running as a separate process (not launched from the Gradio UI). The CLI args in Section 3 below are what's actually in effect.

### Quotes Tab

| Setting | Value |
|---------|-------|
| Quote JSON Path | `data/quotes/diffusiongemma_quotes.json` |
| Voice | `Magpie-Multilingual.EN-US.Aria` |
| Quote Interval Seconds | `15` |
| Generate Quote Count | `30` |
| Speak Author | `False` (unchecked) |
| Shuffle | `True` (checked) |
| Loop | `False` (unchecked) |
| Reverb | `True` (checked) |
| Last-Word Echo | `True` (checked) |
| Quote Voice Status | `idle` |

> **Note:** TTS is also running as a separate process with its own CLI args (Section 4). The Gradio UI settings show `Loop=False` but the CLI uses `--loop`.

### SFX Tab

| Setting | Value |
|---------|-------|
| AudioGen Prompts | `subtle analog tape whoosh, short broadcast transition, clean and quiet\nsoft futuristic interface chirps, tiny electric sparkles, short and tasteful\ndistant synthetic thunder swell, low cinematic rumble, restrained\ngentle glass shimmer and airy reverse cymbal, short transition sound` |
| SFX Seconds | `3` |
| Interval Seconds | `35` |
| SFX Pre-Bus Volume | `0.35` |
| Seed | `4242` |
| CPU Mode | `False` (unchecked) |
| AudioGen SFX Status | `idle` |

> **Note:** SFX is not currently running as a separate process. It's disabled in the Broadcast tab.

### Broadcast Tab

| Setting | Value |
|---------|-------|
| Twitch | `True` (checked) |
| YouTube | `False` (unchecked) |
| Facebook | `False` (unchecked) |
| Quote Voice | `True` (checked) |
| SFX | `False` (unchecked) |
| Audio Bus | `True` (checked) |
| Local Monitor | `True` (checked) |
| Bus Music Volume | `0.85` |
| Bus Quote Volume | `1.55` |
| Bus SFX Volume | `0.25` |
| Audio Bitrate | `128k` |
| Fanout Status | `idle` |

> **Note:** The Broadcast tab settings reflect intent but the actual fanout is handled by the separate ingest/egress ffmpeg processes (Sections 5-6). The bus volumes in the UI (0.85/1.55/0.25) differ from the actual ingest ffmpeg volumes (1.0/1.0/0.85) because the separate processes were launched manually.

---

## 2. Gradio / FLUX.2 Configuration

### CLI Launch Command
```bash
taskset -c 0-15 /root/fluxrt-venv/bin/python -u \
    scripts/run_gradio_stream_demo.py \
    --int8 \
    --server-name 0.0.0.0 \
    --server-port 7862 \
    --config-path configs/stream_demo_config.json \
    --local-video /workspace/test_input.mp4
```

### Config File (`configs/stream_demo_config.json`)
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

### Key Parameters
| Parameter | Value |
|-----------|-------|
| Model | FLUX.2-Klein-4B |
| Quantization | int8 |
| Resolution | 288×160 |
| Steps | 1 |
| Seed | 52 |
| Spatial Cache | enabled |
| LoRA | disabled |
| Reference Image | disabled |
| Lip Transfer | disabled |
| Compile Models | false |
| Target FPS | null (defaults to 8) |
| CPU Cores | 0-15 (taskset) |
| GPU | 0 |
| Server | 0.0.0.0:7862 |
| Local Video | /workspace/test_input.mp4 |
| Gradio Share URL | https://e7c3f0b587579b7c34.gradio.live |
| Gradio Version | 6.19.0 |
| API Prefix | /gradio_api |

---

## 3. MusicGen Configuration (Actual Running Process)

### CLI Launch Command
```bash
taskset -c 16-63 .venv/bin/python3 -u \
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
    --crossfade-seconds 2.0
```

### Key Parameters
| Parameter | Value |
|-----------|-------|
| Model | facebook/musicgen-small |
| Precision | fp16 on CUDA |
| GPU | 1 |
| CPU Cores | 16-63 (taskset) |
| Sample Rate | 32000 Hz |
| Gen Seconds | 8 |
| Parallel Clips | 2 |
| Bootstrap Clips | 54 |
| Pre-generate | 10 |
| Stream Delay Seconds | 120 |
| Crossfade Seconds | 2.0 |
| Conditioning Mode | continuation |
| Conditioning Seconds | 8 |
| Top-k | 250 |
| Top-p | 0.95 |
| Temperature | 1.0 |
| Guidance Scale | 3.0 |
| Seed | -1 (random) |
| Drunk Walk | disabled |
| Pause Seconds | 0 |
| Base Prompt | *(empty)* |
| Output Dir | /dev/shm/musicgen |
| Audio UDP URL | udp://127.0.0.1:5002?pkt_size=1316 |
| RTF (realtime factor) | 0.55-0.58 |
| Boot Time | ~120 seconds |

### MusicGen ffmpeg Encoder (subprocess)
```bash
ffmpeg -hide_banner -loglevel error -y \
    -f f32le -ac 1 -ar 32000 -i - \
    -af highpass=f=35,lowpass=f=12000,alimiter=limit=0.85 \
    -c:a aac -b:a 128k -ar 48000 -ac 2 \
    -f mpegts udp://127.0.0.1:5002?pkt_size=1316
```

| Parameter | Value |
|-----------|-------|
| Input format | f32le, mono, 32000 Hz |
| Audio filters | highpass=35Hz, lowpass=12000Hz, alimiter=0.85 |
| Output codec | AAC 128k, 48000 Hz, stereo |
| Output format | MPEG-TS over UDP |
| Output URL | udp://127.0.0.1:5002?pkt_size=1316 |

---

## 4. Edge TTS Configuration

### CLI Launch Command
```bash
python3 -u scripts/run_edge_tts_quotes.py \
    --quotes data/quotes/diffusiongemma_quotes.json \
    --loop \
    --shuffle \
    --interval 15 \
    --random-voices \
    --reverb \
    --reverb-mix 0.35 \
    --reverb-decay 0.50 \
    --reverb-delay-ms 60 \
    --echo \
    --echo-mix 0.45 \
    --echo-decay 0.65 \
    --echo-delay-ms 150 \
    --udp-url "udp://127.0.0.1:5004?pkt_size=1316" \
    --repeat-on-empty \
    --cache-dir voices/quote_cache_edge \
    --cache-size 4
```

### Key Parameters
| Parameter | Value |
|-----------|-------|
| Quotes file | data/quotes/diffusiongemma_quotes.json |
| Loop | enabled |
| Shuffle | enabled |
| Interval | 15 seconds |
| Random Voices | enabled |
| Reverb | enabled |
| Reverb Mix | 0.35 |
| Reverb Decay | 0.50 |
| Reverb Delay | 60 ms |
| Echo | enabled |
| Echo Mix | 0.45 |
| Echo Decay | 0.65 |
| Echo Delay | 150 ms |
| UDP URL | udp://127.0.0.1:5004?pkt_size=1316 |
| Repeat on Empty | enabled |
| Cache Dir | voices/quote_cache_edge |
| Cache Size | 4 |

---

## 5. Ingest ffmpeg Configuration

### Exact Command
```bash
ffmpeg -hide_banner -loglevel warning \
    -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000" \
    -f mpegts -i "udp://127.0.0.1:5002?fifo_size=500000&overrun_nonfatal=1" \
    -f mpegts -i "udp://127.0.0.1:5004?fifo_size=500000&overrun_nonfatal=1" \
    -f mpegts -i "udp://127.0.0.1:5000?fifo_size=1000000&overrun_nonfatal=1" \
    -filter_complex "[0:a]volume=1.0[a0];[1:a]volume=1.0[a1];[2:a]volume=0.85[a2];[a0][a1][a2]amix=inputs=3:duration=longest:dropout_transition=0[aout]" \
    -map "3:v" -map "[aout]" \
    -c:v copy -c:a aac -b:a 128k -ar 48000 -ac 2 \
    -f flv "rtmp://127.0.0.1:1935/fluxrt"
```

### Input Mapping
| Input # | Source | Content | FIFO Size |
|---------|--------|---------|-----------|
| 0 | anullsrc (silence) | Base audio, stereo 48kHz | N/A (lavfi) |
| 1 | udp://127.0.0.1:5002 | Music (from MusicGen) | 500000 |
| 2 | udp://127.0.0.1:5004 | TTS (from Edge TTS) | 500000 |
| 3 | udp://127.0.0.1:5000 | Video (from Gradio) | 1000000 |

### Audio Mix
| Stream | Volume | Description |
|--------|--------|-------------|
| [0:a] silence | 1.0 | Base bed (prevents silence gaps) |
| [1:a] music | 1.0 | MusicGen audio |
| [2:a] tts | 0.85 | TTS quotes (slightly lower) |
| amix | dropout_transition=0 | 3-input mix, longest duration |

### Output
| Parameter | Value |
|-----------|-------|
| Video | Copied from input 3 (no re-encode) |
| Audio codec | AAC 128k, 48000 Hz, stereo |
| Output format | FLV |
| Output URL | rtmp://127.0.0.1:1935/fluxrt |

---

## 6. Egress ffmpeg Configuration

### Exact Command
```bash
ffmpeg -hide_banner -loglevel info \
    -i "rtmp://127.0.0.1:1935/fluxrt" \
    -c copy \
    -f flv "rtmps://live.twitch.tv:443/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw"
```

### Parameters
| Parameter | Value |
|-----------|-------|
| Input | rtmp://127.0.0.1:1935/fluxrt (MediaMTX) |
| Video | Copy (no re-encode) |
| Audio | Copy (no re-encode) |
| Output format | FLV |
| Output URL | rtmps://live.twitch.tv:443/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw |
| Protocol | RTMPS (TLS) |

### Twitch Stream Key
Located in `scripts/streaming/rtmp_targets.env`:
```
TWITCH_RTMP_URL="rtmps://live.twitch.tv:443/app/live_726973151_Z0KsONWcyz53vf5VQXMcecurXLsYHw"
ENABLE_YOUTUBE="0"
ENABLE_TWITCH="1"
ENABLE_FACEBOOK="0"
```

---

## 7. MediaMTX Configuration

### Config File (`/tmp/fluxrt-mediamtx.yml`)
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

### Launch Command
```bash
/workspace/FluxRT/tools/mediamtx/mediamtx /tmp/fluxrt-mediamtx.yml
```

### Parameters
| Parameter | Value |
|-----------|-------|
| Version | 1.8.4 |
| RTMP Address | :1935 |
| RTMP Encryption | none |
| Path | fluxrt |
| Source mode | publisher |
| RTSP | disabled |
| HLS | disabled |
| WebRTC | disabled |
| SRT | disabled |
| API | disabled |
| Metrics | disabled |

---

## 8. Watchdog Configuration

### Launch Command
```bash
# When MusicGen is already running (skip grace period):
MUSICGEN_START_TIME=$(($(date +%s) - 200)) bash /workspace/FluxRT/scripts/streaming/watchdog.sh

# When starting fresh:
bash /workspace/FluxRT/scripts/streaming/watchdog.sh
```

### Parameters
| Parameter | Value |
|-----------|-------|
| Check interval | 30 seconds |
| MusicGen grace period | 180 seconds |
| Silence threshold | -60 dB (mean_volume) |
| Silence check duration | 5 seconds |
| Monitors | MediaMTX, Ingest, Egress, MusicGen, TTS, Gradio |
| Log file | /tmp/fluxrt-watchdog.log |

### Auto-Recovery Actions
| Condition | Action |
|-----------|--------|
| MediaMTX down | Restart MediaMTX |
| Ingest down | Restart ingest ffmpeg |
| Egress down | Restart egress ffmpeg |
| MusicGen down (after grace) | Restart MusicGen |
| MusicGen ffmpeg down (after grace) | Restart MusicGen |
| TTS down | Restart TTS |
| Gradio down | Restart Gradio |
| Audio silence (< -60 dB) | Restart MusicGen + Ingest |

---

## 9. Process PIDs (at time of snapshot)

| Component | PID | PPID |
|-----------|-----|------|
| MediaMTX | 1135518 | 1 |
| Gradio | 1160972 | 1 |
| TTS | 1135750 | 1 |
| MusicGen Python | 1315949 | 1 |
| MusicGen ffmpeg | 1316403 | 1315949 |
| Ingest ffmpeg | 1316851 | 1 |
| Egress ffmpeg | 1317098 | 1 |
| Watchdog | 1317379 | 1 |

---

## 10. Log File Locations

| Component | Log File |
|-----------|----------|
| MediaMTX | /tmp/fluxrt-mediamtx.log |
| Gradio | /tmp/fluxrt-gradio.log |
| MusicGen | /dev/shm/musicgen/musicgen.log |
| TTS | /tmp/fluxrt-tts.log |
| Ingest | /tmp/fluxrt-mediamtx-ingest.log |
| Egress | /tmp/fluxrt-egress-twitch.log |
| Watchdog | /tmp/fluxrt-watchdog.log |

---

## 11. Environment

| Variable | Value |
|----------|-------|
| RUNPOD_POD_ID | v89vkr1b3dhgua |
| Gradio Share URL | https://e7c3f0b587579b7c34.gradio.live |
| RunPod Proxy | https://v89vkr1b3dhgua-7862.proxy.runpod.net |
| Python (Gradio) | /root/fluxrt-venv/bin/python |
| Python (MusicGen) | /workspace/FluxRT/.venv/bin/python3 |
| Python (TTS) | python3 (system) |
| Working directory | /workspace/FluxRT |

---

## 12. Hardware & Resource Allocation

| Resource | Gradio | MusicGen | Other |
|----------|--------|----------|-------|
| GPU | 0 (L40, 46GB) | 1 (L40, 46GB) | none |
| CPU cores | 0-15 (16 cores) | 16-63 (48 cores) | any |
| VRAM usage | ~8GB (int8) | ~4GB (fp16 small) | N/A |

### System Specs
- GPUs: 2× NVIDIA L40 (46GB each)
- RAM: 2TB
- CPUs: 64 cores
- Platform: RunPod pod
