#!/usr/bin/env bash
set -euo pipefail

# Refresh local IPTV stream catalog from iptv-org/iptv (streams folder only).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${REPO_ROOT}/iptv-streams"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

git clone --depth 1 --filter=blob:none --sparse https://github.com/iptv-org/iptv.git "${TMP_DIR}/iptv"
(
  cd "${TMP_DIR}/iptv"
  git sparse-checkout set streams
)

mkdir -p "${DEST_DIR}"
rsync -a --delete "${TMP_DIR}/iptv/streams/" "${DEST_DIR}/"

echo "IPTV stream catalog updated at: ${DEST_DIR}"
find "${DEST_DIR}" -type f | wc -l | awk '{print "Files:", $1}'
du -sh "${DEST_DIR}" | awk '{print "Size:", $1}'
