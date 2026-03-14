#!/usr/bin/env bash
# deploy.sh — Sync source naar VPS, bouw en start Bottie daar
#
# Gebruik:
#   ./deploy.sh             # live
#   ./deploy.sh --dry-run   # veilig testen

set -euo pipefail

SSH_TARGET="root@45.76.38.183"
REMOTE_DIR="/opt/bottie"
DRY_RUN_FLAG=""

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN_FLAG="--dry-run"
    echo "⚠️  Dry-run mode: bot plaatst geen echte trades"
fi

echo "==> Syncing source naar ${SSH_TARGET}:${REMOTE_DIR}..."
rsync -az --progress --delete \
    --exclude target/ \
    --exclude .git/ \
    --exclude data/ \
    --exclude ".env" \
    --exclude "research/venv/" \
    . "${SSH_TARGET}:${REMOTE_DIR}/"

echo "==> Syncing .env (secrets)..."
rsync -az .env "${SSH_TARGET}:${REMOTE_DIR}/.env"
ssh "${SSH_TARGET}" "chmod 600 ${REMOTE_DIR}/.env"

echo "==> Bouwen op VPS..."
ssh "${SSH_TARGET}" bash << 'ENDSSH'
set -e

# Installeer Rust als het er nog niet is
if ! command -v cargo &>/dev/null; then
    echo "Rust niet gevonden, installeren..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
    source "$HOME/.cargo/env"
fi

source "$HOME/.cargo/env"
cd /opt/bottie

echo "Cargo build --release..."
cargo build --release --locked
echo "Build geslaagd."
ENDSSH

echo "==> Installeren systemd service..."
ssh "${SSH_TARGET}" bash << ENDSSH
set -e

cp /opt/bottie/target/release/bottie /opt/bottie/bottie-bin
mkdir -p /opt/bottie/data

# Pas ExecStart in service file aan op basis van dry-run
if [[ -n "${DRY_RUN_FLAG}" ]]; then
    sed 's|ExecStart=.*|ExecStart=/opt/bottie/bottie-bin --config /opt/bottie/config.yaml --dry-run|' \
        /opt/bottie/bottie.service > /etc/systemd/system/bottie.service
else
    sed 's|ExecStart=.*|ExecStart=/opt/bottie/bottie-bin --config /opt/bottie/config.yaml|' \
        /opt/bottie/bottie.service > /etc/systemd/system/bottie.service
fi

# Vervang User/Group door root (we draaien als root op de VPS)
sed -i 's|^User=.*||; s|^Group=.*||' /etc/systemd/system/bottie.service

# Verwijder ProtectSystem want dat conflicteert soms met root builds
sed -i 's|^ProtectSystem=.*||; s|^ReadWritePaths=.*|ReadWritePaths=/opt/bottie/data|' \
    /etc/systemd/system/bottie.service

systemctl daemon-reload
systemctl enable bottie
systemctl restart bottie
sleep 2
systemctl status bottie --no-pager -l
ENDSSH

echo ""
echo "✓ Bottie draait op ${SSH_TARGET}"
echo ""
echo "Handige commando's:"
echo "  Logs live:  ssh ${SSH_TARGET} 'journalctl -u bottie -f'"
echo "  Status:     ssh ${SSH_TARGET} 'systemctl status bottie'"
echo "  Stoppen:    ssh ${SSH_TARGET} 'systemctl stop bottie'"
