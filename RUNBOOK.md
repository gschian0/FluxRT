# FluxRT Ops Runbook

This runbook is for a repeatable setup so you can shut the VM down when idle and resume quickly.

## One-Time Setup

1. Keep this repo on persistent disk (already true for your VM home directory).
2. Ensure Docker + NVIDIA runtime works:
   - `sudo docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`
3. Ensure port 7860 is open in GCP firewall (already configured in your session).

## Start Workflow (After VM Boot)

Run a GPU container with the repo mounted at `/workspace`:

```bash
sudo docker run --rm -it --gpus all \
  -p 7862:7862 \
  -p 7861:7861 \
  -v /home/gschi/FluxRT:/workspace \
  -w /workspace \
  nvcr.io/nvidia/pytorch:24.10-py3 \
  bash
```

Inside the container:

```bash
chmod +x scripts/bootstrap_uv_env.sh scripts/start_gradio.sh
scripts/bootstrap_uv_env.sh
scripts/start_gradio.sh
```

Open:

- Baseline app (default): `http://127.0.0.1:7862`
- Stream demo (default): `http://127.0.0.1:7861`

## Run My Tweak (Stream Demo on 7861)

Use this flow to run the working stream demo tweak on port 7861.

From host (recommended):

```bash
cd /home/gschi/FluxRT
pkill -f 'scripts/run_gradio_stream_demo.py' || true
APP_HOST=0.0.0.0 APP_PORT=7861 ./scripts/start_gradio_stream_demo.sh
```

Quick health checks:

```bash
ss -ltnp | grep ':7861'
curl -I --max-time 8 http://127.0.0.1:7861
pgrep -af 'scripts/run_gradio_stream_demo.py'
```

Public URL (replace with your current external IP):

- `http://YOUR_EXTERNAL_IP:7861`

Get current external IP:

```bash
curl -s ifconfig.me || curl -s https://api.ipify.org
```

If public URL does not open, add GCP firewall for 7861:

```bash
gcloud compute firewall-rules create allow-fluxrt-7861 \
  --direction=INGRESS \
  --priority=1000 \
  --network=default \
  --action=ALLOW \
  --rules=tcp:7861 \
  --source-ranges=0.0.0.0/0
```

## Daily Fast Resume

If `.venv` already exists in `/workspace`, skip bootstrap and just run:

```bash
scripts/start_gradio.sh
```

Optional override for baseline app port:

```bash
APP_PORT=7870 scripts/start_gradio.sh
```

## Stop Workflow

Inside container:

```bash
pkill -f scripts/run_gradio_demo.py || true
```

Then exit the container and stop the VM.

## Logs and Health

- App log: `/tmp/fluxrt-gradio.log`
- Health check (baseline default): `curl -I http://127.0.0.1:7862`

## What Is Persisted

- Code and local edits under `/home/gschi/FluxRT`
- Python env in `/home/gschi/FluxRT/.venv` (if created with these scripts)

## Backup for Recreate

Capture these three artifacts before shutting down long-term:

1. Git history and local commits:

```bash
cd /home/gschi/FluxRT
git bundle create /home/gschi/FluxRT-backup-$(date +%F).bundle --all
```

2. Environment/VM manifest:

```bash
cd /home/gschi/FluxRT
chmod +x scripts/export_env_manifest.sh
scripts/export_env_manifest.sh
```

3. Infrastructure settings snapshot (from a machine with `gcloud` configured):

```bash
gcloud compute instances describe YOUR_VM_NAME --zone YOUR_ZONE > /home/gschi/FluxRT/backups/instance-describe.txt
gcloud compute firewall-rules list > /home/gschi/FluxRT/backups/firewall-rules.txt
```

Recreate checklist:

- Same GPU type/count and machine family.
- Docker + NVIDIA runtime available.
- Port 7860 ingress rule present.
- Repo restored (`git clone` or bundle restore) and runbook scripts available.

## Commercial Use Note

Current stack highlights:

- FLUX.2-klein-4B / int8 quantized: Apache-2.0
- LivePortrait code: MIT
- LivePortrait-code includes note: InsightFace detection models are non-commercial research only.

For commercial deployment, replace/remove InsightFace detection models as noted by LivePortrait-code license text.

## Separate Streaming Operation

To avoid breaking the known-good local app, use the separate staged plan:

- See `STREAMING_STAGED_PLAN.md` for checkpoint-based execution and rollback at every stage.
- Relay helper scripts:
  - `scripts/streaming/start_nbc_relay.sh`
  - `scripts/streaming/stop_nbc_relay.sh`

## RTMP Fanout Upgrade (FluxRT First)

Use this flow to publish one stream to multiple platforms from FluxRT.

1. Keep stream demo running on 7861 as usual.
2. Start local source relay (NBC example) to feed ffmpeg fanout input:

```bash
cd /home/gschi/FluxRT
chmod +x scripts/streaming/start_nbc_relay.sh scripts/streaming/stop_nbc_relay.sh
scripts/streaming/start_nbc_relay.sh
```

3. Configure RTMP targets:

```bash
cd /home/gschi/FluxRT
cp scripts/streaming/rtmp_targets.env.example scripts/streaming/rtmp_targets.env
```

Edit `scripts/streaming/rtmp_targets.env` and set one or more:

- `YOUTUBE_RTMP_URL`
- `TWITCH_RTMP_URL`
- `FACEBOOK_RTMP_URL`

Optional per-platform switches in the same env file:

- `ENABLE_YOUTUBE=0` to disable YouTube output
- `ENABLE_TWITCH=1` to keep Twitch output on
- `ENABLE_FACEBOOK=0` if Facebook is unused

4. Start RTMP fanout:

```bash
cd /home/gschi/FluxRT
chmod +x scripts/streaming/start_rtmp_fanout.sh scripts/streaming/stop_rtmp_fanout.sh
scripts/streaming/start_rtmp_fanout.sh
```

Current locked defaults (stable profile):

- `FPS=12`
- `OUTPUT_WIDTH=426`
- `OUTPUT_HEIGHT=240`
- `VIDEO_BITRATE=2200k`
- `VIDEO_MAXRATE=2200k`
- `VIDEO_BUFSIZE=1800k`
- `AUDIO_BITRATE=128k`
- `X264_PRESET=ultrafast`
- `GOP=FPS*2` (2-second keyframes)

Override any setting at launch (examples):

```bash
cd /home/gschi/FluxRT
FPS=15 VIDEO_BITRATE=1200k VIDEO_MAXRATE=1200k VIDEO_BUFSIZE=2400k \
OUTPUT_WIDTH=640 OUTPUT_HEIGHT=360 X264_PRESET=veryfast \
scripts/streaming/start_rtmp_fanout.sh
```

You can also change only one variable without touching others:

```bash
cd /home/gschi/FluxRT
VIDEO_BITRATE=1500k scripts/streaming/start_rtmp_fanout.sh
```

Temporary YouTube-off launch (without editing env file):

```bash
cd /home/gschi/FluxRT
ENABLE_YOUTUBE=0 scripts/streaming/start_rtmp_fanout.sh
```

If YouTube shows "Preparing stream" for too long, keep `GOP` at `FPS*2` and avoid disabling forced keyframes.

5. Check fanout health:

```bash
cat /tmp/fluxrt-rtmp-fanout.pid
ps -fp "$(cat /tmp/fluxrt-rtmp-fanout.pid)"
tail -n 120 /tmp/fluxrt-rtmp-fanout.log
```

6. Stop fanout:

```bash
cd /home/gschi/FluxRT
scripts/streaming/stop_rtmp_fanout.sh
```

Notes:

- Default fanout input is `udp://127.0.0.1:5000?pkt_size=1316`.
- Override input with `INPUT_URL=... scripts/streaming/start_rtmp_fanout.sh`.
- Keep real stream keys in `scripts/streaming/rtmp_targets.env` (local only, not committed).

## Stopping Everything

If you need to stop all streams, the inference engine, and the application cleanly, run these commands:

```bash
cd /home/gschi/FluxRT

# 1. Stop the RTMP Fanout (Twitch/YouTube)
scripts/streaming/stop_rtmp_fanout.sh || true

# 2. Stop the local NBC relay (if you were using it)
scripts/streaming/stop_nbc_relay.sh || true

# 3. Stop the Gradio application and the rendering engine
pkill -f 'scripts/run_gradio_stream_demo.py' || true
```

## MusicGen Radio (musicgenRADIO branch)

This branch includes a radio-conditioned MusicGen loop for installation/research use.

Files:

- `scripts/run_musicgen_radio_plus_musicGEN.py`
- `scripts/start_musicgen_radio_plus_musicGEN.sh`
- `scripts/stop_musicgen_radio_plus_musicGEN.sh`
- `requirements_musicgen_plus_musicGEN.txt`

Install deps in your active `.venv`:

```bash
cd /home/gschi/FluxRT
source .venv/bin/activate
pip install -r requirements_musicgen_plus_musicGEN.txt
```

Start generator:

```bash
cd /home/gschi/FluxRT
RADIO_URL="https://your-radio-stream-url" scripts/start_musicgen_radio_plus_musicGEN.sh
```

Stop generator:

```bash
cd /home/gschi/FluxRT
scripts/stop_musicgen_radio_plus_musicGEN.sh
```

Runtime output:

- Log: `/tmp/fluxrt-musicgen-radio.log`
- Generated clips: `musicgen_output_plus_musicGEN/`
