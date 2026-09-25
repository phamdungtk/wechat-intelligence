#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=wechat-intelligence
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
APP_USER="${SUDO_USER:-$(id -un)}"
APP_GROUP="$(id -gn "$APP_USER")"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Chạy: sudo bash deploy/install-service.sh" >&2
  exit 1
fi
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "Chưa có .venv. Hãy tạo môi trường ảo và cài requirements.txt trước." >&2
  exit 1
fi
if [[ "$APP_DIR" =~ [[:space:]] ]]; then
  echo "Đường dẫn dự án không được chứa khoảng trắng: $APP_DIR" >&2
  exit 1
fi

install -d -o "$APP_USER" -g "$APP_GROUP" "$APP_DIR/storage" "$APP_DIR/logs"

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=WeChat Intelligence API
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 3435
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME"
echo "Đã cài service $SERVICE_NAME trên cổng 3435."
