#!/usr/bin/env bash
set -euo pipefail

# Create a timestamped backup snapshot for current FluxRT work.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${1:-$REPO_ROOT/backups/$STAMP}"
mkdir -p "$OUT_DIR"

ARCHIVE="$OUT_DIR/fluxrt-work-$STAMP.tgz"
PATCH_FILE="$OUT_DIR/working-tree-$STAMP.patch"
STATUS_FILE="$OUT_DIR/git-status-$STAMP.txt"
FILES_FILE="$OUT_DIR/file-list-$STAMP.txt"
NOTES_FILE="$OUT_DIR/restore-notes-$STAMP.txt"

# Save code + scripts + docs + generated music outputs.
# Exclude heavy model dirs and venv to keep backups practical.
tar \
  --exclude='./.git' \
  --exclude='./.venv' \
  --exclude='./FLUX.2-klein-4B' \
  --exclude='./FLUX.2-klein-4B-int8' \
  --exclude='./LivePortrait' \
  --exclude='./LivePortrait-code/pretrained_weights' \
  --exclude='./RIFE-safetensors' \
  --exclude='./snap' \
  -czf "$ARCHIVE" \
  .

git status --short > "$STATUS_FILE" || true
git diff > "$PATCH_FILE" || true
find . -maxdepth 4 -type f | sort > "$FILES_FILE" || true

cat > "$NOTES_FILE" <<EOF
Backup created: $STAMP
Archive: $ARCHIVE
Patch: $PATCH_FILE
Status: $STATUS_FILE

Restore quick steps:
1) Extract archive into target folder.
2) If needed, apply patch with: git apply "$PATCH_FILE"
3) Reinstall env/deps and start services from RUNBOOK.md
EOF

echo "Snapshot created in: $OUT_DIR"
echo "Archive: $ARCHIVE"
echo "Patch:   $PATCH_FILE"
echo "Status:  $STATUS_FILE"
