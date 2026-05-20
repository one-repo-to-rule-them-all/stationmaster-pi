#!/usr/bin/env bash
# install_services.sh — Install and enable systemd services for stationmaster-pi.
#
# Run once after bootstrap.py completes, or any time you need to reinstall
# the services (e.g. after editing the unit file).
#
# Usage:
#   sudo bash tools/install_services.sh
#
# What this does:
#   1. Copies systemd/ersatztv.service to /etc/systemd/system/
#   2. Reloads systemd daemon
#   3. Enables + starts ErsatzTV
#   4. Installs the startup_sync service (self-healer on boot)
#   5. Prints status for both services

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
SERVICE_USER="cmpe8803"

# ── Colour helpers ─────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[X]${NC} $*"; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && fail "Run this script with sudo."
id "$SERVICE_USER" &>/dev/null || fail "User '$SERVICE_USER' not found. Create it first."
[[ -f "$REPO_ROOT/systemd/ersatztv.service" ]] || fail "systemd/ersatztv.service not found. Run from repo root."

# ── ErsatzTV service ──────────────────────────────────────────────────────────
ok "Installing ErsatzTV systemd unit..."
cp "$REPO_ROOT/systemd/ersatztv.service" /etc/systemd/system/ersatztv.service
chmod 644 /etc/systemd/system/ersatztv.service

systemctl daemon-reload
systemctl enable ersatztv

# Kill any ErsatzTV process started by bootstrap (Popen) before systemd takes over.
# If a stale instance is running, systemd's start will fail with "Another instance already running."
if pgrep -f ErsatzTV &>/dev/null; then
    warn "Stopping ErsatzTV process started by bootstrap (handing off to systemd)..."
    pkill -TERM -f ErsatzTV 2>/dev/null || true
    sleep 3
    # Force-kill if still alive
    pkill -KILL -f ErsatzTV 2>/dev/null || true
    sleep 1
fi

systemctl start ersatztv || warn "ErsatzTV failed to start immediately — check: journalctl -u ersatztv -n 50"

# ── startup_sync service (self-healer) ────────────────────────────────────────
ok "Installing startup_sync service..."

cat > /etc/systemd/system/stationmaster-sync.service << EOF
[Unit]
Description=Stationmaster startup self-healer
After=network-online.target mnt-nas.mount ersatztv.service
Wants=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
ExecStart=/bin/bash $REPO_ROOT/tools/startup_sync.sh
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal
SyslogIdentifier=stationmaster-sync

[Install]
WantedBy=multi-user.target
EOF

chmod 644 /etc/systemd/system/stationmaster-sync.service
systemctl daemon-reload
systemctl enable stationmaster-sync

ok "Services installed."
echo ""
echo "── Status ────────────────────────────────────────────────────────"
systemctl status ersatztv --no-pager -l | head -20 || true
echo ""
echo "── Useful commands ───────────────────────────────────────────────"
echo "  journalctl -u ersatztv -f          # live ErsatzTV log"
echo "  journalctl -u stationmaster-sync   # startup sync log"
echo "  systemctl restart ersatztv         # manual restart"
echo "  systemctl status ersatztv          # check status"
