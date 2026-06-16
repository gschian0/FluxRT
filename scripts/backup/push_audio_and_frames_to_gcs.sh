#!/usr/bin/env bash
set -euo pipefail

# Upload local research audio (WAV) and captured frame images to GCS.
# Usage:
#   PROJECT_ID=schianosound BUCKET=aitvbucket scripts/backup/push_audio_and_frames_to_gcs.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PROJECT_ID="${PROJECT_ID:-schianosound}"
BUCKET="${BUCKET:-aitvbucket}"
SESSION_TAG="${SESSION_TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
AUDIO_DIR="${AUDIO_DIR:-$REPO_ROOT/musicgen_output_plus_musicGEN}"
FRAMES_ROOT="${FRAMES_ROOT:-$REPO_ROOT/backups/frames}"

AUDIO_DEST="gs://${BUCKET}/research/${SESSION_TAG}/audio/"
FRAMES_DEST="gs://${BUCKET}/research/${SESSION_TAG}/frames/"

if ! command -v gsutil >/dev/null 2>&1; then
  echo "gsutil not found. Install Google Cloud SDK first."
  exit 1
fi

gcloud config set project "$PROJECT_ID" >/dev/null

echo "Using bucket: gs://${BUCKET}"
echo "Session tag: ${SESSION_TAG}"

gsutil ls "gs://${BUCKET}" >/dev/null

shopt -s nullglob
wav_files=("$AUDIO_DIR"/*.wav)

if [[ ${#wav_files[@]} -eq 0 ]]; then
  echo "No WAV files found in $AUDIO_DIR"
else
  echo "Uploading ${#wav_files[@]} WAV files to $AUDIO_DEST"
  gsutil -m cp -n "${wav_files[@]}" "$AUDIO_DEST"
fi

latest_frames_dir=""
if [[ -d "$FRAMES_ROOT" ]]; then
  latest_frames_dir="$(find "$FRAMES_ROOT" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1 || true)"
fi

if [[ -n "$latest_frames_dir" ]]; then
  frame_files=("$latest_frames_dir"/*.jpg "$latest_frames_dir"/*.png)
  existing_frame_files=()
  for f in "${frame_files[@]}"; do
    [[ -e "$f" ]] && existing_frame_files+=("$f")
  done

  if [[ ${#existing_frame_files[@]} -gt 0 ]]; then
    echo "Uploading ${#existing_frame_files[@]} frame files from $latest_frames_dir to $FRAMES_DEST"
    gsutil -m cp -n "${existing_frame_files[@]}" "$FRAMES_DEST"
  else
    echo "No frame images found in $latest_frames_dir"
  fi
else
  echo "No frame capture directory found under $FRAMES_ROOT"
fi

echo "Remote counts:"
echo -n "audio wavs: "
gsutil ls "${AUDIO_DEST}*.wav" 2>/dev/null | wc -l || true

echo -n "frame images: "
gsutil ls "${FRAMES_DEST}*.jpg" "${FRAMES_DEST}*.png" 2>/dev/null | wc -l || true

echo "Done."
