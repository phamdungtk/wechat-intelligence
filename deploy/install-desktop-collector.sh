#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Chạy: sudo bash deploy/install-desktop-collector.sh" >&2
  exit 1
fi

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "Chưa có môi trường .venv trong $APP_DIR" >&2
  exit 1
fi

cat > /etc/systemd/system/wechat-intelligence-collector.service <<EOF
[Unit]
Description=Automatically sync WeChat Desktop articles
Requires=docker.service
After=docker.service wechat-intelligence.service

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python -m app.wechat_desktop watch --interval 5
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now wechat-intelligence-collector.service
systemctl --no-pager --full status wechat-intelligence-collector.service
