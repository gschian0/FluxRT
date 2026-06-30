#!/usr/bin/env bash
set -euo pipefail

URL_FILE="/home/gschi/.cache/fluxrt/cloudflared_url.txt"

echo "=== systemd services ==="
systemctl --no-pager --full status fluxrt-gradio.service fluxrt-cloudflared.service | sed -n '1,30p'

echo
echo "=== app health ==="
if curl -fsS -m 5 http://127.0.0.1:7861 >/dev/null; then
  echo "gradio: up on 7861"
else
  echo "gradio: not reachable on 7861"
fi

echo
echo "=== cloudflared url ==="
if [[ -f "$URL_FILE" ]]; then
  cat "$URL_FILE"
else
  echo "(not available yet; check: journalctl -u fluxrt-cloudflared -n 80 --no-pager)"
fi
