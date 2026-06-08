#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OUT_DIR="${1:-$REPO_ROOT/backups}"
mkdir -p "$OUT_DIR"

TS="$(date +%Y%m%d-%H%M%S)"
OUT_FILE="$OUT_DIR/env-manifest-$TS.txt"

{
  echo "# FluxRT Environment Manifest"
  echo "generated_at=$(date -Iseconds)"
  echo

  echo "## host"
  echo "hostname=$(hostname)"
  echo "kernel=$(uname -srmo)"
  echo

  echo "## cpu"
  lscpu || true
  echo

  echo "## memory"
  free -h || true
  echo

  echo "## gpu"
  nvidia-smi -L || true
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader || true
  echo

  echo "## docker"
  docker --version || true
  sudo docker info --format 'server={{.ServerVersion}} runtimes={{json .Runtimes}}' 2>/dev/null || true
  echo

  echo "## python_uv"
  command -v python3 || true
  python3 --version || true
  command -v uv || true
  uv --version || true
  echo

  echo "## repo"
  cd "$REPO_ROOT"
  git rev-parse --short HEAD || true
  git branch --show-current || true
  git remote -v || true
  echo

  echo "## files"
  ls -la /home/gschi/FluxRT || true
  echo

  echo "## requirements"
  sed -n '1,200p' /home/gschi/FluxRT/requirements.txt || true
  echo
  sed -n '1,200p' /home/gschi/FluxRT/requirements_lipsync.txt || true
  echo
  sed -n '1,240p' /home/gschi/FluxRT/RUNBOOK.md || true
} > "$OUT_FILE"

echo "Manifest written: $OUT_FILE"
