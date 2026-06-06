#!/usr/bin/env bash
# nas-resolve.sh — find the NAS on the LAN by its MAC and publish its current
# IP into /etc/hosts as a stable name, so the CIFS mount survives DHCP drift.
#
# Pairs with:
#   /etc/fstab line:            //nas-stationmaster/Public  /mnt/nas  cifs  ...
#   nas-resolve.service:        runs this before mnt-nas.mount on boot
#
# Belt-and-suspenders with a router DHCP reservation. If the reservation holds,
# this just re-confirms the same IP each boot. If the NAS drifts, this finds it.
set -uo pipefail

NAS_MAC="00:14:ee:0a:be:6e"     # WD My Cloud
NAS_NAME="nas-stationmaster"    # name referenced in /etc/fstab
SUBNET_BASE="192.168.68"        # /24 to sweep on fallback
HOSTS="/etc/hosts"

mac_lc="${NAS_MAC,,}"
ip=""

# Method 1: arp-scan (fast, purpose-built) if installed
if command -v arp-scan >/dev/null 2>&1; then
    ip="$(arp-scan --localnet --quiet --plain 2>/dev/null \
          | awk -v m="$mac_lc" 'tolower($2)==m {print $1; exit}')"
fi

# Method 2: fallback — sweep the /24 to populate ARP, then read neighbor table
if [ -z "$ip" ]; then
    for i in $(seq 1 254); do ping -c1 -W1 "${SUBNET_BASE}.${i}" >/dev/null 2>&1 & done
    wait
    ip="$(ip neigh | awk -v m="$mac_lc" 'tolower($5)==m {print $1; exit}')"
fi

if [ -z "$ip" ]; then
    echo "nas-resolve: NAS (MAC $NAS_MAC) not found on ${SUBNET_BASE}.0/24" >&2
    exit 1
fi

# Atomically rewrite the /etc/hosts entry for NAS_NAME
tmp="$(mktemp)"
grep -vw "$NAS_NAME" "$HOSTS" > "$tmp" 2>/dev/null || true
printf '%s\t%s\n' "$ip" "$NAS_NAME" >> "$tmp"
install -m 0644 "$tmp" "$HOSTS"
rm -f "$tmp"
echo "nas-resolve: $NAS_NAME -> $ip"
