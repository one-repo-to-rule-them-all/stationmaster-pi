#!/usr/bin/env bash
# startup_sync.sh — Self-healing startup script for stationmaster-pi.
#
# Runs on every boot via the stationmaster-sync systemd service.
# Mirrors the behaviour of startup_sync.ps1 from the Windows version:
#
#   1. Ensure NAS is mounted (waits up to 90s for SMB to be reachable)
#   2. Start ErsatzTV if not running, wait for its API
#   3. Reconcile Jellyfin tuner URL if JF_ETV_URL changed in .env
#
# Logs to: /home/cmpe8803/.local/share/stationmaster/startup_sync.log
# (and to the systemd journal — view with: journalctl -u stationmaster-sync)

set -uo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_ROOT/.env"

LOG_DIR="/home/cmpe8803/.local/share/stationmaster"
LOG_FILE="$LOG_DIR/startup_sync.log"
mkdir -p "$LOG_DIR"

# ── Logging ───────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "=== stationmaster startup_sync start ==="

# ── Load .env ─────────────────────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
    # Export only non-comment, non-empty lines
    set -a
    # shellcheck disable=SC1090
    source <(grep -v '^\s*#' "$ENV_FILE" | grep -v '^\s*$')
    set +a
    log "Loaded .env from $ENV_FILE"
else
    log "WARNING: .env not found at $ENV_FILE — using defaults"
fi

NAS_MOUNT_POINT="${NAS_MOUNT_POINT:-/mnt/nas}"
ETV_HOST="${ETV_HOST:-http://localhost:8409}"
ETV_EXE_PATH="${ETV_EXE_PATH:-/opt/ersatztv/ErsatzTV}"
JF_ETV_URL="${JF_ETV_URL:-http://localhost:8409}"
JELLYFIN_HOST="${JELLYFIN_HOST:-http://localhost:8096}"
JELLYFIN_ADMIN_USER="${JELLYFIN_ADMIN_USER:-cmpe8803}"
JELLYFIN_ADMIN_PASS="${JELLYFIN_ADMIN_PASS:-}"

# ── Step 1: Ensure NAS is mounted ─────────────────────────────────────────────
log "Checking NAS mount at $NAS_MOUNT_POINT..."

if mountpoint -q "$NAS_MOUNT_POINT"; then
    log "NAS already mounted."
else
    log "NAS not mounted — attempting mount..."
    MAX_WAIT=90
    ELAPSED=0
    MOUNTED=false

    while [[ $ELAPSED -lt $MAX_WAIT ]]; do
        if sudo mount "$NAS_MOUNT_POINT" 2>/dev/null; then
            MOUNTED=true
            log "NAS mounted successfully after ${ELAPSED}s."
            break
        fi
        sleep 5
        ELAPSED=$((ELAPSED + 5))
        log "  Waiting for NAS... (${ELAPSED}s elapsed)"
    done

    if [[ "$MOUNTED" != "true" ]]; then
        log "WARNING: NAS mount failed after ${MAX_WAIT}s. ErsatzTV may report missing media."
        # Don't exit — let ErsatzTV start anyway; it handles missing paths gracefully
    fi
fi

# ── Step 2: Ensure ErsatzTV is running ────────────────────────────────────────
log "Checking ErsatzTV..."

if systemctl is-active --quiet ersatztv; then
    log "ErsatzTV service is running."
else
    log "ErsatzTV not running — starting via systemctl..."
    if sudo systemctl start ersatztv; then
        log "ErsatzTV started."
    else
        log "ERROR: Failed to start ErsatzTV service."
    fi
fi

# Wait up to 60s for ErsatzTV API to respond
log "Waiting for ErsatzTV API at $ETV_HOST..."
MAX_WAIT=60
ELAPSED=0
ETV_UP=false

while [[ $ELAPSED -lt $MAX_WAIT ]]; do
    if curl -sf "$ETV_HOST/api/health" -o /dev/null 2>/dev/null || \
       curl -sf "$ETV_HOST/iptv/channels.m3u" -o /dev/null 2>/dev/null; then
        ETV_UP=true
        log "ErsatzTV API responded after ${ELAPSED}s."
        break
    fi
    sleep 3
    ELAPSED=$((ELAPSED + 3))
done

if [[ "$ETV_UP" != "true" ]]; then
    log "WARNING: ErsatzTV API not reachable after ${MAX_WAIT}s."
fi

# ── Step 3: Reconcile Jellyfin tuner URL ──────────────────────────────────────
# If JF_ETV_URL changed (e.g. you moved ETV to a different machine), update
# the tuner registration in Jellyfin automatically.
#
# This is a best-effort step — if Jellyfin isn't up yet, skip it silently.
log "Checking Jellyfin tuner reconciliation..."

if [[ -z "$JELLYFIN_ADMIN_PASS" ]]; then
    log "JELLYFIN_ADMIN_PASS not set in .env — skipping tuner reconciliation."
else
    # Get an auth token
    AUTH_RESPONSE=$(curl -sf -X POST \
        -H "Content-Type: application/json" \
        -H "X-Emby-Authorization: MediaBrowser Client=\"stationmaster\", Device=\"startup_sync\", DeviceId=\"startup_sync_001\", Version=\"1.0\"" \
        -d "{\"Username\":\"$JELLYFIN_ADMIN_USER\",\"Pw\":\"$JELLYFIN_ADMIN_PASS\"}" \
        "$JELLYFIN_HOST/Users/AuthenticateByName" 2>/dev/null) || true

    if [[ -z "$AUTH_RESPONSE" ]]; then
        log "Jellyfin not reachable — skipping tuner reconciliation (will retry on next boot)."
    else
        JF_TOKEN=$(echo "$AUTH_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('AccessToken',''))" 2>/dev/null || echo "")

        if [[ -n "$JF_TOKEN" ]]; then
            # Get existing tuners
            TUNERS=$(curl -sf \
                -H "X-Emby-Authorization: MediaBrowser Token=\"$JF_TOKEN\"" \
                "$JELLYFIN_HOST/LiveTv/TunerHosts" 2>/dev/null) || true

            CURRENT_URL=$(echo "$TUNERS" | python3 -c "
import sys, json
try:
    hosts = json.load(sys.stdin)
    etv_hosts = [h for h in hosts if 'ersatztv' in h.get('Url','').lower() or '8409' in h.get('Url','')]
    if etv_hosts:
        print(etv_hosts[0].get('Url',''))
except:
    pass
" 2>/dev/null || echo "")

            EXPECTED_URL="${JF_ETV_URL}/iptv/channels.m3u"

            if [[ -n "$CURRENT_URL" && "$CURRENT_URL" != "$EXPECTED_URL" ]]; then
                log "Tuner URL mismatch: current='$CURRENT_URL' expected='$EXPECTED_URL' — run full_setup.py to reconcile."
            else
                log "Jellyfin tuner URL OK."
            fi
        else
            log "Jellyfin auth failed — skipping tuner reconciliation."
        fi
    fi
fi

log "=== startup_sync complete ==="
