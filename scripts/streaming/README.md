# FluxRT Streaming Scripts

This folder contains the real-time AI video streaming pipeline for FluxRT. It pulls live video from IPTV/HLS sources, restyles it with diffusion models, and forwards the result over UDP to MediaMTX / RTMP fanout / Twitch (or any other target).

## Architecture Overview

```
┌─────────────┐     ┌─────────────────────────────┐     ┌─────────────┐     ┌──────────────┐
│  IPTV/HLS   │────▶│  AI restyle (this folder)   │────▶│  UDP 5000   │────▶│  MediaMTX /  │
│  source     │     │  SD-Turbo / CogVideoX / LTX │     │  MPEG-TS    │     │  RTMP fanout │
└─────────────┘     └─────────────────────────────┘     └─────────────┘     └──────────────┘
                                                                                  │
                                                                                  ▼
                                                                           ┌──────────────┐
                                                                           │  Twitch /    │
                                                                           │  YouTube /   │
                                                                           │  Facebook    │
                                                                           └──────────────┘
```

Audio is handled separately: MusicGen / radio / TTS streams are mixed and sent to UDP 5002/5004/5006, then combined with video by the fanout scripts.

## Quick Start

1. **Configure stream targets**:
   ```bash
   cp scripts/streaming/rtmp_targets.env.example scripts/streaming/rtmp_targets.env
   # Edit scripts/streaming/rtmp_targets.env with your Twitch/YouTube/Facebook keys
   ```

2. **Start the fanout stack** (this receives UDP video + audio and sends to platforms):
   ```bash
   bash scripts/streaming/start_rtmp_fanout_preset_bars.sh
   ```

3. **Start an AI video generator** in another terminal, for example:
   ```bash
   CUDA_VISIBLE_DEVICES=0 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
   python scripts/streaming/run_sdturbo_iptv.py \
       --iptv-url "https://example.com/stream.m3u8" \
       --udp-url "udp://127.0.0.1:5000?pkt_size=1316&fifo_size=50000000&overrun_nonfatal=1" \
       --fps 20 --width 512 --height 512 --control-port 8889
   ```

4. **Monitor locally** before it goes live:
   ```bash
   bash scripts/streaming/start_stream_monitor_http.sh
   # Open http://127.0.0.1:8090/
   ```

## Script Reference

### AI Video Generators

| Script | Model | Description | Latency |
|--------|-------|-------------|---------|
| `run_sdturbo_iptv.py` | SD-Turbo | Frame-by-frame img2img restyling. Fastest, lowest latency, no clip buffering. | ~50–80 ms/frame on L40 |
| `run_sdturbo_iptv_dual.py` | SD-Turbo | Same as above but uses two GPUs in parallel (round-robin frames). | Nearly 2× throughput |
| `run_cogvideo_iptv.py` | CogVideoX-5B | Video-to-video restyling of short clips. Preserves real motion, but has clip buffering delay. | Seconds per clip |
| `run_ltx_iptv.py` | LTX-Video | Image-to-video generation conditioned on live IPTV seed frames. | Seconds per clip |

All generators expose an HTTP control server (default port `8889`) with endpoints such as:
- `GET /status` — current state, FPS, prompt index, etc.
- `GET /prompts` — list loaded prompts
- `POST /prompt` — set an override prompt
- `POST /seed` — set a fixed seed
- `POST /steps` — set inference steps
- `POST /strength` — set img2img / V2V strength
- `POST /next` — jump to next prompt
- `POST /rotate` — resume auto rotation
- `POST /interval` — change prompt rotation interval
- `POST /channel` — switch IPTV source URL

### Optimization / Export

| Script | Purpose |
|--------|---------|
| `export_unet_tensorrt.py` | Export the SD-Turbo UNet to ONNX and build a TensorRT engine. |
| `benchmark_trt_unet.py` | Benchmark TensorRT UNet vs PyTorch UNet inside the full img2img pipeline. |
| `bridge_gradio_processed_to_udp.py` | Poll a Gradio `/poll_video` endpoint and forward processed frames to UDP MPEG-TS. |
| `rotate_flux_prompt.py` | Long-running prompt rotator that talks to the Gradio REST API via `curl`. |

### Fanout & Streaming Infrastructure

| Script | Purpose |
|--------|---------|
| `start_rtmp_fanout.sh` | Main fanout: takes UDP video + audio, re-encodes, and sends to one or more RTMP targets via `ffmpeg` tee. |
| `start_rtmp_fanout_preset_bars.sh` | Convenience wrapper around `start_rtmp_fanout.sh` with startup color bars and TTS overlay enabled. |
| `start_rtmp_fanout_watchdog.sh` | Restarts the RTMP fanout if it dies. |
| `stop_rtmp_fanout.sh` | Stops the fanout and cleans up stale ffmpeg processes. |
| `stop_rtmp_fanout_watchdog.sh` | Stops the fanout watchdog. |
| `start_mediamtx_fanout.sh` | Alternative fanout using MediaMTX as an RTMP relay. |
| `stop_mediamtx_fanout.sh` | Stops the MediaMTX fanout stack. |
| `install_mediamtx_local.sh` | Downloads and installs MediaMTX locally. |
| `start_nbc_relay.sh` | Simple ffmpeg relay for an NBC HLS stream to local UDP. |
| `stop_nbc_relay.sh` | Stops the NBC relay. |

### Audio

| Script | Purpose |
|--------|---------|
| `start_continuous_audio_rail.sh` | Never-stopping audio rail: MusicGen → radio fallback → silence. Always outputs to UDP 5002. |
| `start_audio_mix_bus.sh` | Mixes music (UDP 5002), TTS (UDP 5004), and optional SFX (UDP 5008) into a single output (UDP 5006). Includes sidechain ducking so TTS cuts through music. |
| `stop_audio_mix_bus.sh` | Stops the audio mix bus. |

### Monitoring

| Script | Purpose |
|--------|---------|
| `start_stream_monitor_http.sh` | Starts a tiny HTTP server + HLS player page so you can preview the stream before it goes live. |
| `stop_stream_monitor_http.sh` | Stops the monitor HTTP server. |

### Legacy / Supervisory

| Script | Purpose |
|--------|---------|
| `watchdog.sh` | Legacy supervisor that monitors MediaMTX, ingest, egress, MusicGen, TTS, and Gradio processes and restarts them if they die. |

## Environment Variables

Most shell scripts can be configured via environment variables. Common ones:

| Variable | Default | Description |
|----------|---------|-------------|
| `TWITCH_RTMP_URL` | (from `rtmp_targets.env`) | Twitch ingest URL + stream key |
| `YOUTUBE_RTMP_URL` | (from `rtmp_targets.env`) | YouTube ingest URL + stream key |
| `FACEBOOK_RTMP_URL` | (from `rtmp_targets.env`) | Facebook ingest URL + stream key |
| `ENABLE_TWITCH` / `ENABLE_YOUTUBE` / `ENABLE_FACEBOOK` | `1` | Per-platform switches |
| `VIDEO_INPUT_URL` | `udp://127.0.0.1:5000?...` | AI video input |
| `AUDIO_INPUT_URL` | `udp://127.0.0.1:5002?...` | Music / audio input |
| `TTS_INPUT_URL` | `udp://127.0.0.1:5004?...` | TTS overlay input |
| `MUSIC_MIX_VOLUME` | `0.55`–`0.65` | Music volume in mix |
| `TTS_MIX_VOLUME` | `0.95`–`1.80` | TTS volume in mix |
| `OUTPUT_WIDTH` / `OUTPUT_HEIGHT` | `426×240` | Output resolution |
| `FPS` | `8`–`24` | Target output FPS |
| `VIDEO_BITRATE` | `900k`–`1200k` | Video bitrate |
| `AUDIO_BITRATE` | `96k`–`128k` | Audio bitrate |

## Typical UDP Ports

| Port | Content |
|------|---------|
| `5000` | AI-generated video (MPEG-TS over UDP) |
| `5002` | Music / continuous audio rail |
| `5004` | TTS overlay |
| `5006` | Mixed audio output |
| `5008` | Optional SFX input |
| `1935` | MediaMTX / internal RTMP relay |
| `8090` | Local HLS monitor HTTP server |
| `8889` | AI generator HTTP control API |

## Tips

- **Low latency**: Use `run_sdturbo_iptv.py` with `--use-tensorrt` and `--use-taesd` for the fastest path.
- **Stable motion**: Use `run_cogvideo_iptv.py` or `run_ltx_iptv.py`; they process clips so motion is preserved across frames, but add buffering delay.
- **Dual GPU**: Use `run_sdturbo_iptv_dual.py` when both L40 GPUs are available.
- **Never drop audio**: Always start `start_continuous_audio_rail.sh` and `start_audio_mix_bus.sh` before the fanout so the stream never goes silent.
- **Preview before going live**: Start `start_stream_monitor_http.sh` and open `http://127.0.0.1:8090/`.
