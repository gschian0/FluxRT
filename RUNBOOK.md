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
