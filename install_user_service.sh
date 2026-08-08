#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${HOME}/bn-stra-high-risk-1"
SERVICE_NAME="bn-stra-high-risk-1.service"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"

mkdir -p "${USER_SYSTEMD_DIR}"
cp "${APP_DIR}/${SERVICE_NAME}" "${USER_SYSTEMD_DIR}/${SERVICE_NAME}"

if [ ! -f "${APP_DIR}/.env" ]; then
  cat > "${APP_DIR}/.env" <<'EOF'
# Required only when config.json has "dry_run": false
BINANCE_API_KEY=
BINANCE_API_SECRET=
EOF
  chmod 600 "${APP_DIR}/.env"
fi

systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}"

cat <<EOF
Installed ${SERVICE_NAME}.

Commands:
  systemctl --user start ${SERVICE_NAME}
  systemctl --user stop ${SERVICE_NAME}
  systemctl --user restart ${SERVICE_NAME}
  systemctl --user status ${SERVICE_NAME}
  journalctl --user -u ${SERVICE_NAME} -f

For start-on-boot without an active SSH session, root must run:
  loginctl enable-linger ${USER}
EOF
