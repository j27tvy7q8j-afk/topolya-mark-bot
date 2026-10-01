#!/bin/bash
# Один раз на сервере: таймер автообновления раз в 30 минут.
set -e
cd "$(dirname "$(readlink -f "$0")")"
chmod +x update.sh
cat > /etc/systemd/system/mark-update.service <<UNIT
[Unit]
Description=Автообновление бота Марк
[Service]
Type=oneshot
ExecStart=$(pwd)/update.sh
UNIT
cat > /etc/systemd/system/mark-update.timer <<UNIT
[Unit]
Description=Проверка обновлений бота Марк каждые 30 минут
[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now mark-update.timer
systemctl list-timers mark-update.timer --no-pager
