#!/usr/bin/env bash
set -euo pipefail

URL_FILE="/home/gschi/.cache/fluxrt/cloudflared_url.txt"
mkdir -p "$(dirname "$URL_FILE")"

rm -f "$URL_FILE"

# Stream cloudflared logs to stdout for journald and capture the active URL.
/usr/local/bin/cloudflared tunnel --url http://127.0.0.1:7862 --no-autoupdate 2>&1 |
while IFS= read -r line; do
  echo "$line"
  if [[ "$line" =~ https://[a-zA-Z0-9.-]+\.trycloudflare\.com ]]; then
    printf '%s\n' "${BASH_REMATCH[0]}" > "$URL_FILE"
  fi
done
