# FluxRT Instance Provisioning and Stream Ingest Plan

This document captures a repeatable Google Cloud setup and the next steps to get stream ingest working again, including the NBC HLS URL tested earlier.

## 1) Goals

- Recreate a working FluxRT GPU host quickly after shutdown.
- Keep costs down by stopping the VM when idle.
- Start FluxRT consistently from a container.
- Add stream ingest in a controlled way so local mode remains stable.

## 2) What Is Working Now

- FluxRT local Gradio app can be started from the project environment.
- Port 7860 is reachable when firewall rule is present.
- Local video mode is the stable baseline.

## 3) Baseline Architecture

- Compute Engine GPU VM.
- Repo on persistent disk at `/home/gschi/FluxRT`.
- NVIDIA container runtime + Docker.
- Runtime launch via:
  - `scripts/bootstrap_uv_env.sh` (first time)
  - `scripts/start_gradio.sh` (every run)

## 4) One-Time Google Cloud Provisioning

## 4.1 VM Requirements

- GPU-enabled machine type (match your successful instance).
- NVIDIA driver + Docker + NVIDIA container toolkit installed.
- External IP attached.
- Network tag: `fluxrt`.

## 4.2 Firewall

Allow external HTTP traffic to Gradio:

```bash
gcloud compute firewall-rules create allow-fluxrt-7860 \
  --network=default \
  --direction=INGRESS \
  --priority=1000 \
  --action=ALLOW \
  --rules=tcp:7860 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=fluxrt
```

## 4.3 GPU Runtime Sanity Check

```bash
sudo docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

## 5) Repeatable Runtime Start

From host:

```bash
sudo docker run --rm -it --gpus all \
  -p 7860:7860 \
  -v /home/gschi/FluxRT:/workspace \
  -w /workspace \
  nvcr.io/nvidia/pytorch:24.10-py3 \
  bash
```

Inside container (first run):

```bash
chmod +x scripts/bootstrap_uv_env.sh scripts/start_gradio.sh
scripts/bootstrap_uv_env.sh
scripts/start_gradio.sh
```

Inside container (later runs):

```bash
scripts/start_gradio.sh
```

Health checks:

```bash
curl -I http://127.0.0.1:7860
```

## 6) Optional: Make This a Template

Use a custom image + instance template:

1. Build and validate one golden VM.
2. Create custom image from boot disk.
3. Create instance template with:
   - same GPU config
   - tag `fluxrt`
   - startup script that runs the container and `scripts/start_gradio.sh`
4. Launch future instances from the template.

Notes:

- Keep `/home/gschi/FluxRT` on persistent disk.
- Pin container/image versions for reproducibility.

## 7) Streaming Plan (NBC HLS Input)

Target URL tested:

- `https://xumo-xumoent-vc-105-z0vpm.fast.nbcuni.com/live/master.m3u8`

Current status:

- Stream mode was rolled back to restore local stability.
- Next stream work should be done in a branch and validated incrementally.

## 7.1 Why It Broke Previously

- UI event mismatch in Gradio (`start_stream_url` got empty inputs).
- OpenCV backend handling for HLS URL was inconsistent.
- Stream and local logic were coupled too tightly.

## 7.2 Safer Re-Implementation Steps

1. Add stream support behind a feature flag in UI (do not alter local flow).
2. Use a dedicated stream worker thread and queue.
3. Keep local worker unchanged.
4. Add visible stream status text: `idle`, `connecting`, `live`, `reconnecting`, `error`.
5. Add bounded reconnect loop with backoff.
6. Keep last good frame during reconnect to avoid black screen flicker.

## 7.3 Recommended Ingest Strategy

For HLS reliability, prefer ffmpeg relay over direct OpenCV URL read.

Path A (recommended):

- Use ffmpeg to ingest HLS and republish as low-latency local stream (UDP/RTSP).
- OpenCV reads the local relay endpoint, not the internet URL directly.

Example relay concept (host/container shell):

```bash
ffmpeg -re -i "https://xumo-xumoent-vc-105-z0vpm.fast.nbcuni.com/live/master.m3u8" \
  -an -c:v libx264 -preset veryfast -tune zerolatency -f mpegts udp://127.0.0.1:5000
```

Then OpenCV reads:

- `udp://127.0.0.1:5000`

Path B (fallback):

- Use direct OpenCV `VideoCapture` on the m3u8 URL with backend auto-selection and retry.

## 7.4 Test Matrix

Run in this order and stop on first failure:

1. Local mode with known mp4 (baseline).
2. Stream mode with synthetic local source (ffmpeg testsrc).
3. Stream mode with NBC URL through ffmpeg relay.
4. Stream mode with direct NBC URL.

Success criteria:

- No Gradio event tracebacks.
- No worker crashes.
- Frame updates continue for 10+ minutes.
- Switching between local and stream does not break local playback.

## 8) Operational Checklist (Daily)

1. Start VM.
2. Start container.
3. Run `scripts/start_gradio.sh`.
4. Check `curl -I http://127.0.0.1:7860`.
5. Use app.
6. Stop app/process.
7. Stop VM.

## 9) License and Commercial Use Snapshot

- FLUX.2-klein-4B: Apache-2.0.
- FLUX.2-klein-4B-int8: Apache-2.0.
- LivePortrait code: MIT.
- LivePortrait-code note: InsightFace model weights are non-commercial research only.
- RIFE-safetensors: MIT.

Commercial deployment action:

- Replace/remove InsightFace detection models used by LivePortrait-code to avoid the non-commercial model restriction.

## 10) Next Session Fast Start

Run these in order:

```bash
cd /home/gschi/FluxRT
sudo docker run --rm -it --gpus all -p 7860:7860 -v /home/gschi/FluxRT:/workspace -w /workspace nvcr.io/nvidia/pytorch:24.10-py3 bash
scripts/start_gradio.sh
```

If environment is missing:

```bash
scripts/bootstrap_uv_env.sh
scripts/start_gradio.sh
```
