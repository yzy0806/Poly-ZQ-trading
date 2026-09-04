#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root" >&2
    exit 1
fi

ROOT_DIR=/opt/zq-arb
CONFIG_DIR=/etc/zq-arb
DATA_DIR=/var/lib/zq-arb
BACKUP_DIR=/var/backups/zq-arb

install -d -m 0755 "$ROOT_DIR"
install -d -m 0700 "$CONFIG_DIR"
install -d -o 10001 -g 10001 -m 0750 "$DATA_DIR"
install -d -m 0700 "$BACKUP_DIR"

if [ ! -f "$CONFIG_DIR/zq-arb.env" ]; then
    python3 "$ROOT_DIR/deploy/bootstrap_env.py" \
        "$ROOT_DIR/deploy/zq-arb.env.example" \
        "$CONFIG_DIR/zq-arb.env"
fi

if [ ! -f "$CONFIG_DIR/deployment.env" ]; then
    install -m 0644 /dev/null "$CONFIG_DIR/deployment.env"
fi

install -m 0644 "$ROOT_DIR/deploy/systemd/zq-arb-backup.service" \
    /etc/systemd/system/zq-arb-backup.service
install -m 0644 "$ROOT_DIR/deploy/systemd/zq-arb-backup.timer" \
    /etc/systemd/system/zq-arb-backup.timer

docker network inspect ib-gateway_default >/dev/null
systemctl daemon-reload
systemctl enable --now zq-arb-backup.timer

echo "VPS directories and fail-closed configuration are ready."
echo "Set an immutable ZQ_ARB_IMAGE digest in $CONFIG_DIR/deployment.env before starting."
