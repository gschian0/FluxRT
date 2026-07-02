#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="/home/gschi/FluxRT"
SERVICE_DIR="/etc/systemd/system"

# Use sudo only when not already root (e.g. RunPod containers run as root)
SUDO=""
if [[ "$EUID" -ne 0 ]]; then
  SUDO="sudo"
fi

if [[ ! -x /snap/bin/uv ]]; then
  echo "Missing /snap/bin/uv"
  exit 1
fi

if [[ ! -x /usr/local/bin/cloudflared ]]; then
  echo "Missing /usr/local/bin/cloudflared"
  exit 1
fi

chmod +x "$REPO_ROOT/scripts/systemd/run_cloudflared_quick_tunnel.sh"
chmod +x "$REPO_ROOT/scripts/systemd/fluxrt_boot_status.sh"

${SUDO} tee "$SERVICE_DIR/fluxrt-gradio.service" >/dev/null <<'EOF'
[Unit]
Description=FluxRT Gradio Stream Demo
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=gschi
WorkingDirectory=/home/gschi/FluxRT
Environment=PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,garbage_collection_threshold:0.8
ExecStart=/snap/bin/uv run scripts/run_gradio_stream_demo.py --int8 --server-name 0.0.0.0 --server-port 7861 --config-path configs/stream_demo_config.json
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=30
StandardOutput=append:/var/log/fluxrt-gradio.log
StandardError=append:/var/log/fluxrt-gradio.log

[Install]
WantedBy=multi-user.target
EOF

${SUDO} tee "$SERVICE_DIR/fluxrt-cloudflared.service" >/dev/null <<'EOF'
[Unit]
Description=FluxRT Cloudflared Quick Tunnel
After=network-online.target fluxrt-gradio.service
Wants=network-online.target
Requires=fluxrt-gradio.service

[Service]
Type=simple
User=gschi
WorkingDirectory=/home/gschi/FluxRT
ExecStart=/home/gschi/FluxRT/scripts/systemd/run_cloudflared_quick_tunnel.sh
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=20
StandardOutput=append:/var/log/fluxrt-cloudflared.log
StandardError=append:/var/log/fluxrt-cloudflared.log

[Install]
WantedBy=multi-user.target
EOF

${SUDO} systemctl daemon-reload
${SUDO} systemctl enable fluxrt-gradio.service fluxrt-cloudflared.service
${SUDO} systemctl restart fluxrt-gradio.service fluxrt-cloudflared.service

echo
echo "Installed and restarted services:"
${SUDO} systemctl --no-pager --full status fluxrt-gradio.service fluxrt-cloudflared.service | sed -n '1,40p'

echo
echo "To check later:"
echo "  $REPO_ROOT/scripts/systemd/fluxrt_boot_status.sh"
