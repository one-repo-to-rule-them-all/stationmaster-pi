#!/usr/bin/env python3
"""
bootstrap.py — stationmaster-pi one-command installer.

Raspberry Pi 5 (ARM64) · Raspberry Pi OS 64-bit · Python 3.10+

Installs and configures the full stationmaster stack on a fresh Pi:
  - Jellyfin (via official apt repo)
  - ErsatzTV Legacy (linux-arm64 binary from GitHub releases)
  - NAS CIFS mount via /etc/fstab
  - systemd services for both components
  - Wires up Live TV in Jellyfin (M3U tuner + XMLTV guide)

Usage:
    python3 bootstrap.py

Skip flags (for re-runs or partial installs):
    python3 bootstrap.py --skip-apt          # system packages already installed
    python3 bootstrap.py --skip-nas          # NAS already mounted
    python3 bootstrap.py --skip-jellyfin     # Jellyfin already installed
    python3 bootstrap.py --skip-wizard       # Jellyfin already initialized
    python3 bootstrap.py --skip-libs         # Jellyfin libraries already configured
    python3 bootstrap.py --skip-etv          # ErsatzTV already installed
    python3 bootstrap.py --skip-channels     # channels already built
    python3 bootstrap.py --skip-services     # systemd services already installed
    python3 bootstrap.py --resume            # skip apt+nas+jellyfin+wizard, keep libs

Run as the regular user (cmpe8803). The script will sudo only where needed.
Do NOT run as root — Jellyfin and ErsatzTV data dirs must be owned by your user.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

# ── Bootstrap: ensure python-dotenv is available ──────────────────────────────
try:
    from dotenv import load_dotenv, set_key
except ImportError:
    print("[bootstrap] Installing python-dotenv...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-dotenv", "--break-system-packages", "-q"])
    from dotenv import load_dotenv, set_key

try:
    import requests
except ImportError:
    print("[bootstrap] Installing requests...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--break-system-packages", "-q"])
    import requests

# ── Paths ──────────────────────────────────────────────────────────────────────
REPO_ROOT   = Path(__file__).parent.resolve()
ENV_FILE    = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# ── Colour output ─────────────────────────────────────────────────────────────
class C:
    GREEN  = "\033[0;32m"
    YELLOW = "\033[1;33m"
    RED    = "\033[0;31m"
    CYAN   = "\033[0;36m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

def ok(msg):   print(f"{C.GREEN}[+]{C.RESET} {msg}")
def info(msg): print(f"{C.CYAN}[~]{C.RESET} {msg}")
def warn(msg): print(f"{C.YELLOW}[!]{C.RESET} {msg}")
def err(msg):  print(f"{C.RED}[X]{C.RESET} {msg}")
def hdr(msg):  print(f"\n{C.BOLD}{C.CYAN}{'─'*60}{C.RESET}\n{C.BOLD} {msg}{C.RESET}\n{'─'*60}")

def die(msg):
    err(msg)
    sys.exit(1)

def run(cmd, check=True, capture=False, sudo=False):
    """Run a shell command, optionally with sudo."""
    if sudo and os.geteuid() != 0:
        cmd = ["sudo"] + (cmd if isinstance(cmd, list) else cmd.split())
    if isinstance(cmd, str):
        cmd = cmd.split()
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if check and result.returncode != 0:
        die(f"Command failed: {' '.join(str(c) for c in cmd)}\n{result.stderr or ''}")
    return result

def run_shell(cmd, check=True, capture=False):
    """Run a command through the shell (for pipes, globs, etc.)."""
    result = subprocess.run(cmd, shell=True, capture_output=capture, text=True)
    if check and result.returncode != 0:
        die(f"Shell command failed: {cmd}\n{result.stderr or ''}")
    return result

# ── Password generation ────────────────────────────────────────────────────────
def gen_password(length=20):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pwd) and
            any(c.isupper() for c in pwd) and
            any(c.isdigit() for c in pwd)):
            return pwd

# ── .env helpers ──────────────────────────────────────────────────────────────
def load_env():
    if not ENV_FILE.exists():
        if ENV_EXAMPLE.exists():
            shutil.copy(ENV_EXAMPLE, ENV_FILE)
            info(f"Created .env from .env.example — please review it at:\n    {ENV_FILE}")
        else:
            die(".env and .env.example both missing. Re-clone the repo.")
    load_dotenv(ENV_FILE, override=True)

def env(key, default=""):
    return os.environ.get(key, default).strip()

def save_env(key, value):
    """Write a key=value back to .env."""
    set_key(str(ENV_FILE), key, value)
    os.environ[key] = value

# ── Network helpers ────────────────────────────────────────────────────────────
def get_lan_ip():
    """Return the primary LAN IPv4 address (not loopback, not 169.254.x.x)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"

def wait_for_http(url, timeout=120, interval=3, label=None):
    """Poll url until it returns 2xx or timeout expires. Returns True on success."""
    label = label or url
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code < 400:
                return True
        except Exception:
            pass
        elapsed = int(time.time() - (deadline - timeout))
        print(f"  Waiting for {label}... ({elapsed}s)", end="\r", flush=True)
        time.sleep(interval)
    print()
    return False

# ── Auto-detection + fallback config ─────────────────────────────────────────
_EXAMPLE_NAS_UNC = "//WDMYCLOUD/Public"

# Common WD MyCloud / NAS hostnames to probe, in order
_WD_HOSTNAMES = ["wdmycloud", "WDMYCLOUD", "wdmycloud.local", "wdcloud", "mybooklive"]
# Common share names to try after finding the host
_WD_SHARES    = ["Public", "public", "Shared", "share"]

def _prompt(label, current, secret=False, hint=None):
    """Last-resort interactive prompt — only called when auto-detection fails."""
    display = ("(hidden)" if secret and current else current) or ""
    suffix  = f" [{display}]" if display else ""
    if hint:
        print(f"  {C.CYAN}hint:{C.RESET} {hint}")
    try:
        if secret:
            import getpass
            val = getpass.getpass(f"  {label}{suffix}: ")
        else:
            val = input(f"  {label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        die("Setup cancelled.")
    return val if val else current

def _detect_system_timezone():
    """Read the system timezone — timedatectl, /etc/timezone, or symlink."""
    # 1. timedatectl (most reliable on systemd)
    try:
        r = subprocess.run(
            ["timedatectl", "show", "--property=Timezone", "--value"],
            capture_output=True, text=True, timeout=5
        )
        tz = r.stdout.strip()
        if tz and "/" in tz:
            return tz
    except Exception:
        pass
    # 2. /etc/timezone (Debian/Ubuntu)
    try:
        tz = Path("/etc/timezone").read_text().strip()
        if tz and "/" in tz:
            return tz
    except Exception:
        pass
    # 3. /etc/localtime symlink → zoneinfo
    try:
        link = Path("/etc/localtime").resolve()
        parts = link.parts
        zi = next((i for i, p in enumerate(parts) if p == "zoneinfo"), None)
        if zi and zi + 2 <= len(parts):
            return "/".join(parts[zi + 1:])
    except Exception:
        pass
    return None

def _scan_smb_host(ip, timeout=1):
    """Return True if port 445 (SMB) is open on ip."""
    try:
        with socket.create_connection((ip, 445), timeout=timeout):
            return True
    except Exception:
        return False

def _get_subnet_prefix():
    """Return the /24 prefix of the Pi's primary LAN IP (e.g. '192.168.1')."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        parts = ip.split(".")
        return ".".join(parts[:3])
    except Exception:
        return None

def _smb_shares_for_host(host):
    """Return list of share names on host via smbclient (anonymous), or []."""
    try:
        r = subprocess.run(
            ["smbclient", "-L", f"//{host}", "-N", "--no-pass"],
            capture_output=True, text=True, timeout=8
        )
        shares = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("Disk",):
                shares.append(parts[0].strip())
        return shares
    except Exception:
        return []

def _discover_nas():
    """
    Auto-discover a NAS on the LAN. Strategy:
      1. Try well-known WD hostnames (hostname + .local)
      2. nmblookup broadcast for known names
      3. Port-scan the /24 subnet for SMB (445) — fast parallel scan
    Returns a UNC string like //192.168.1.10/Public, or None.
    """
    import concurrent.futures
    import re as _re

    info("Auto-discovering NAS on your network...")

    def probe_host(host):
        """Return (ip, shares) if host has SMB open, else None."""
        ip = None
        try:
            ip = socket.gethostbyname(host)
        except socket.gaierror:
            pass
        if ip and _scan_smb_host(ip):
            shares = _smb_shares_for_host(host)
            return ip, shares
        # Also try the raw IP directly if we have it
        if ip and _scan_smb_host(ip, timeout=1):
            return ip, []
        return None

    found = {}  # ip → shares

    # 1. Known hostnames
    for name in _WD_HOSTNAMES:
        result = probe_host(name)
        if result:
            ip, shares = result
            if ip not in found:
                found[ip] = shares
                ok(f"  Found NAS: {name} ({ip})  shares={shares or ['(unknown)']}")

    # 2. nmblookup broadcast
    if not found:
        try:
            r = subprocess.run(
                ["nmblookup", "-S", "*"],
                capture_output=True, text=True, timeout=8
            )
            for line in r.stdout.splitlines():
                m = _re.match(r"^(\d+\.\d+\.\d+\.\d+)\s+", line)
                if m:
                    ip = m.group(1)
                    if ip not in found and ip != "0.0.0.0" and _scan_smb_host(ip):
                        shares = _smb_shares_for_host(ip)
                        found[ip] = shares
                        ok(f"  Found NAS via NetBIOS: {ip}  shares={shares or ['(unknown)']}")
        except Exception:
            pass

    # 3. Subnet port scan (parallel, fast — 1s timeout per host)
    if not found:
        prefix = _get_subnet_prefix()
        if prefix:
            info(f"  Scanning {prefix}.0/24 for SMB hosts (this takes ~10s)...")
            candidates = [f"{prefix}.{i}" for i in range(1, 255)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
                futures = {ex.submit(_scan_smb_host, ip): ip for ip in candidates}
                for fut in concurrent.futures.as_completed(futures):
                    ip = futures[fut]
                    if fut.result():
                        shares = _smb_shares_for_host(ip)
                        found[ip] = shares
                        ok(f"  Found SMB host: {ip}  shares={shares or ['(unknown)']}")

    if not found:
        return None

    # Pick best candidate — prefer one that has a recognised share name
    for ip, shares in found.items():
        for share in _WD_SHARES:
            if share in shares:
                unc = f"//{ip}/{share}"
                ok(f"  Selected NAS: {unc}")
                return unc
        # No recognised share but host is there — use first share or Public
        share = shares[0] if shares else "Public"
        unc = f"//{ip}/{share}"
        ok(f"  Selected NAS: {unc}")
        return unc

def _test_anonymous_smb(nas_unc):
    """Return True if the NAS accepts anonymous/guest connections."""
    try:
        r = subprocess.run(
            ["smbclient", nas_unc, "-N", "--no-pass", "-c", "ls"],
            capture_output=True, text=True, timeout=10
        )
        return r.returncode == 0
    except Exception:
        return False

def _autodetect_config():
    """
    Silently detect timezone, NAS, and credentials.
    Only falls back to prompting for things it genuinely cannot figure out.
    """
    hdr("Phase 0a — Auto-detecting configuration")
    changed = False

    # ── Timezone ──────────────────────────────────────────────────────────────
    tz = env("TZ", "")
    if not tz or tz == "America/Chicago":
        detected = _detect_system_timezone()
        if detected:
            ok(f"Timezone detected from system: {detected}")
            save_env("TZ", detected)
            changed = True
        else:
            warn("Could not detect system timezone.")
            val = _prompt("Timezone", "America/Chicago",
                          hint="e.g. America/New_York, Europe/London")
            save_env("TZ", val)
            changed = True

    # ── NAS discovery ─────────────────────────────────────────────────────────
    nas_unc = env("NAS_UNC_PATH", "")
    if not nas_unc or nas_unc == _EXAMPLE_NAS_UNC:
        discovered = _discover_nas()
        if discovered:
            save_env("NAS_UNC_PATH", discovered)
            changed = True
        else:
            warn("Could not auto-discover a NAS on this network.")
            val = _prompt("NAS UNC path", _EXAMPLE_NAS_UNC,
                          hint="e.g. //WDMYCLOUD/Public  or  //192.168.1.10/Public")
            save_env("NAS_UNC_PATH", val)
            changed = True

    # ── NAS credentials ───────────────────────────────────────────────────────
    nas_user = env("NAS_USER", "")
    nas_pass = env("NAS_PASS", "")
    current_unc = env("NAS_UNC_PATH", "")
    if not nas_user and not nas_pass and current_unc:
        if _test_anonymous_smb(current_unc):
            ok("NAS accepts anonymous/guest access — no credentials needed.")
            save_env("NAS_USER", "")
            save_env("NAS_PASS", "")
        else:
            warn("NAS requires credentials.")
            u = _prompt("NAS username", "media")
            p = _prompt("NAS password", "", secret=True)
            save_env("NAS_USER", u)
            save_env("NAS_PASS", p)
            changed = True

    if changed:
        load_dotenv(ENV_FILE, override=True)
        ok("Config written to .env")
    else:
        info("Config already set — skipping auto-detection.")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 0 — Preflight
# ─────────────────────────────────────────────────────────────────────────────
def phase_preflight():
    hdr("Phase 0 — Preflight checks")

    # Must NOT be running as root
    if os.geteuid() == 0:
        die("Do not run bootstrap.py as root. Run as cmpe8803 and the script will sudo where needed.")

    # Must be on a 64-bit ARM Linux system
    machine = os.uname().machine
    if machine not in ("aarch64", "arm64"):
        warn(f"Detected architecture: {machine}. This script targets linux-arm64 (aarch64). Proceed with caution.")

    # Confirm .env exists and is loaded — creates from .env.example if missing
    load_env()

    # Auto-detect timezone, NAS, and credentials.
    # Only prompts for things it genuinely cannot figure out.
    nas_unc = env("NAS_UNC_PATH")
    if not nas_unc or nas_unc == _EXAMPLE_NAS_UNC:
        _autodetect_config()

    # Final guard — wizard should have handled this, but be explicit
    nas_unc = env("NAS_UNC_PATH")
    if not nas_unc:
        die("NAS_UNC_PATH is still not set. Check your .env file and re-run.")

    ok("Preflight passed.")
    info(f"NAS path : {nas_unc}")
    info(f"Mount at : {env('NAS_MOUNT_POINT', '/mnt/nas')}")
    info(f"Pi user  : {os.environ.get('USER', 'cmpe8803')}")
    info(f"LAN IP   : {get_lan_ip()}")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 — System packages
# ─────────────────────────────────────────────────────────────────────────────
def phase_apt():
    hdr("Phase 1 — System packages")

    packages = [
        "curl", "wget", "git",
        "python3", "python3-pip",
        "cifs-utils",       # SMB/CIFS NAS mounting
        "ffmpeg",           # ErsatzTV transcoding
        "sqlite3",          # DB inspection
        "ufw",              # Firewall
    ]

    info("Updating apt package index...")
    run(["apt-get", "update", "-qq"], sudo=True)

    info(f"Installing: {', '.join(packages)}")
    run(["apt-get", "install", "-y", "-qq"] + packages, sudo=True)

    ok("System packages installed.")

    # Firewall rules
    info("Configuring firewall (ufw)...")
    run(["ufw", "allow", "ssh"], sudo=True)
    run(["ufw", "allow", "8096/tcp", "comment", "Jellyfin HTTP"], sudo=True)
    run(["ufw", "allow", "7359/udp", "comment", "Jellyfin autodiscovery"], sudo=True)
    run(["ufw", "allow", "8409/tcp", "comment", "ErsatzTV HTTP"], sudo=True)
    run_shell("echo 'y' | sudo ufw enable")
    ok("Firewall configured.")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 — NAS mount
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_nas_host(nas_unc: str) -> str:
    """
    Try to resolve the NAS hostname from NAS_UNC_PATH.
    Attempt order: bare hostname → hostname.local (mDNS) → nmblookup (NetBIOS).
    If a working variant is found, updates NAS_UNC_PATH in .env and returns
    the corrected UNC path. Dies with a clear message if nothing resolves.
    """
    import re as _re
    m = _re.match(r"//([^/]+)(/.+)", nas_unc)
    if not m:
        return nas_unc  # unusual format — pass through unchanged

    hostname, share = m.group(1), m.group(2)

    # Already an IP address — nothing to resolve
    if _re.match(r"^\d+\.\d+\.\d+\.\d+$", hostname):
        ok(f"NAS: using IP address directly ({hostname})")
        return nas_unc

    def try_hostname(h):
        try:
            ip = socket.gethostbyname(h)
            return ip
        except socket.gaierror:
            return None

    def try_nmblookup(h):
        try:
            result = subprocess.run(
                ["nmblookup", h], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and _re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]):
                    if parts[0] not in ("0.0.0.0",):
                        return parts[0]
        except Exception:
            pass
        return None

    # 1. Try bare hostname
    ip = try_hostname(hostname)
    if ip:
        ok(f"NAS hostname '{hostname}' resolved → {ip}")
        return nas_unc

    warn(f"NAS hostname '{hostname}' not resolvable directly — trying alternatives...")

    # 2. Try hostname.local (mDNS / Avahi)
    mdns = hostname + ".local"
    ip = try_hostname(mdns)
    if ip:
        new_unc = f"//{mdns}{share}"
        ok(f"'{mdns}' resolved → {ip}  (updating NAS_UNC_PATH in .env)")
        save_env("NAS_UNC_PATH", new_unc)
        return new_unc

    # 3. Try nmblookup (NetBIOS — requires samba-common or cifs-utils)
    ip = try_nmblookup(hostname)
    if ip:
        new_unc = f"//{ip}{share}"
        ok(f"nmblookup found NAS at {ip}  (updating NAS_UNC_PATH in .env)")
        save_env("NAS_UNC_PATH", new_unc)
        # Also add to /etc/hosts so fstab can resolve it at boot
        hosts_line = f"{ip}  {hostname}\n"
        existing = Path("/etc/hosts").read_text()
        if hostname not in existing:
            run_shell(f"echo '{ip}  {hostname}' | sudo tee -a /etc/hosts > /dev/null")
            ok(f"Added {hostname} → {ip} to /etc/hosts for boot-time fstab resolution")
        return new_unc

    die(
        f"Cannot reach NAS at '{hostname}'.\n"
        f"  1. Make sure the WD MyCloud is powered on and on the same network.\n"
        f"  2. Find its IP address (check your router's device list).\n"
        f"  3. Edit .env and set: NAS_UNC_PATH=//<ip-address>/Public\n"
        f"  4. Re-run bootstrap.py"
    )


def phase_nas():
    hdr("Phase 2 — NAS mount")

    mount_point = env("NAS_MOUNT_POINT", "/mnt/nas")
    nas_unc     = _resolve_nas_host(env("NAS_UNC_PATH", "//WDMYCLOUD/Public"))
    nas_user    = env("NAS_USER", "")
    nas_pass    = env("NAS_PASS", "")
    creds_file  = "/etc/stationmaster-nas.creds"

    # Create mount point
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    run(["chown", f"{os.environ.get('USER','cmpe8803')}:", mount_point], sudo=True)
    ok(f"Mount point: {mount_point}")

    # Write credentials file
    creds_content = f"username={nas_user}\npassword={nas_pass}\n"
    run_shell(f"echo '{creds_content}' | sudo tee {creds_file} > /dev/null")
    run(["chmod", "600", creds_file], sudo=True)
    ok(f"Credentials file: {creds_file} (chmod 600)")

    # Get current user's UID/GID for mount options
    uid = os.getuid()
    gid = os.getgid()

    # Build fstab entry
    # _netdev: wait for network before mounting
    # nofail: don't block boot if NAS is offline
    fstab_entry = (
        f"{nas_unc}  {mount_point}  cifs  "
        f"credentials={creds_file},_netdev,nofail,"
        f"uid={uid},gid={gid},iocharset=utf8,vers=3.0  0  0"
    )

    # Check if entry already exists
    fstab = Path("/etc/fstab").read_text()
    if mount_point in fstab:
        warn(f"fstab already contains an entry for {mount_point} — skipping fstab write.")
        warn("If you need to update it, edit /etc/fstab manually.")
    else:
        run_shell(f"echo '{fstab_entry}' | sudo tee -a /etc/fstab > /dev/null")
        ok("fstab entry added.")

    # Mount now
    info("Mounting NAS...")
    result = run(["mount", mount_point], sudo=True, check=False)
    if result.returncode == 0:
        ok(f"NAS mounted at {mount_point}")
    else:
        # Try refreshing mount
        run(["mount", "-a"], sudo=True, check=False)
        if not Path(mount_point).is_mount():
            die(
                f"Could not mount {nas_unc} at {mount_point}.\n"
                f"Check:\n"
                f"  1. NAS is powered on and reachable: ping wdmycloud\n"
                f"  2. Credentials are correct in {creds_file}\n"
                f"  3. Share path is correct: {nas_unc}"
            )
        ok(f"NAS mounted at {mount_point}")

    # Verify media dirs are reachable
    for key, label in [
        ("NAS_MOVIES_KIDS_DIR", "Kids Movies"),
        ("NAS_MOVIES_ADULT_DIR", "Adult Movies"),
        ("NAS_SHOWS_DIR", "TV Shows"),
        ("NAS_FITNESS_DIR", "Fitness"),
        ("NAS_STANDUP_DIR", "Stand Up Comedy"),
    ]:
        subdir = env(key)
        if subdir:
            full_path = Path(mount_point) / subdir
            if full_path.exists():
                ok(f"  {label}: {full_path}")
            else:
                warn(f"  {label}: {full_path} — directory not found on NAS. Check NAS_*_DIR values in .env.")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3 — Install Jellyfin
# ─────────────────────────────────────────────────────────────────────────────
def phase_install_jellyfin():
    hdr("Phase 3 — Install Jellyfin")

    # Check if already installed
    result = run_shell("dpkg -l jellyfin 2>/dev/null | grep -q '^ii'", check=False)
    if result.returncode == 0:
        ok("Jellyfin already installed.")
        return

    info("Adding Jellyfin apt repository...")
    # Official Jellyfin install script handles repo setup and GPG key
    run_shell("curl -fsSL https://repo.jellyfin.org/install-debuntu.sh | sudo bash")

    info("Installing Jellyfin...")
    run(["apt-get", "install", "-y", "jellyfin"], sudo=True)

    run(["systemctl", "enable", "jellyfin"], sudo=True)
    run(["systemctl", "start", "jellyfin"], sudo=True)

    ok("Jellyfin installed and started.")
    info("Waiting for Jellyfin to become ready...")

    jf_host = env("JELLYFIN_HOST", "http://localhost:8096")
    if not wait_for_http(f"{jf_host}/health", timeout=120, label="Jellyfin"):
        die("Jellyfin didn't respond within 2 minutes. Check: journalctl -u jellyfin -n 50")

    ok("Jellyfin is up.")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4 — Jellyfin first-run wizard
# ─────────────────────────────────────────────────────────────────────────────
def phase_jellyfin_wizard():
    hdr("Phase 4 — Jellyfin first-run wizard")

    jf_host  = env("JELLYFIN_HOST", "http://localhost:8096")
    jf_user  = env("JELLYFIN_ADMIN_USER", "cmpe8803")
    jf_pass  = env("JELLYFIN_ADMIN_PASS", "")

    if not jf_pass:
        jf_pass = gen_password()
        save_env("JELLYFIN_ADMIN_PASS", jf_pass)
        ok(f"Generated admin password and saved to .env")

    # Wait for Jellyfin's internal state machine to be fully ready
    # (health endpoint responding is not enough — wizard API needs extra time)
    info("Waiting for Jellyfin wizard API to be ready...")
    wizard_ready = False
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            r = requests.get(f"{jf_host}/Startup/Configuration", timeout=5)
            if r.status_code == 404:
                ok("Jellyfin wizard already completed.")
                return
            if r.status_code == 200:
                wizard_ready = True
                break
        except Exception:
            pass
        time.sleep(3)

    if not wizard_ready:
        die("Jellyfin wizard API didn't become ready within 60s. Try re-running with --skip-jellyfin.")

    info("Running Jellyfin startup wizard...")

    # Step 1: Initial configuration
    requests.post(f"{jf_host}/Startup/Configuration",
        json={"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"},
        timeout=15)
    time.sleep(2)

    # Step 2: Create admin user — retry up to 5 times in case Jellyfin needs a moment
    last_err = None
    for attempt in range(5):
        r = requests.post(f"{jf_host}/Startup/User",
            json={"Name": jf_user, "Password": jf_pass},
            timeout=15)
        if r.status_code in (200, 204):
            break
        last_err = f"{r.status_code} {r.text[:200]}"
        warn(f"  Wizard user creation attempt {attempt+1} returned {r.status_code} — retrying in 3s...")
        time.sleep(3)
    else:
        die(f"Jellyfin wizard user creation failed after 5 attempts: {last_err}")

    # Step 3: Complete wizard
    requests.post(f"{jf_host}/Startup/Complete", timeout=15)

    ok(f"Jellyfin wizard complete. Admin: {jf_user} / (see .env for password)")

def jf_auth(jf_host, jf_user, jf_pass):
    """Authenticate with Jellyfin and return an access token."""
    r = requests.post(
        f"{jf_host}/Users/AuthenticateByName",
        json={"Username": jf_user, "Pw": jf_pass},
        headers={
            "X-Emby-Authorization": (
                'MediaBrowser Client="stationmaster", Device="bootstrap", '
                'DeviceId="bootstrap_001", Version="1.0"'
            )
        },
        timeout=15,
    )
    if r.status_code != 200:
        die(f"Jellyfin auth failed: {r.status_code} {r.text[:200]}")
    return r.json()["AccessToken"]

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 5 — Jellyfin libraries
# ─────────────────────────────────────────────────────────────────────────────
def phase_jellyfin_libraries():
    hdr("Phase 5 — Jellyfin media libraries")

    jf_host  = env("JELLYFIN_HOST", "http://localhost:8096")
    jf_user  = env("JELLYFIN_ADMIN_USER", "cmpe8803")
    jf_pass  = env("JELLYFIN_ADMIN_PASS")
    mount    = env("NAS_MOUNT_POINT", "/mnt/nas")

    token = jf_auth(jf_host, jf_user, jf_pass)
    headers = {
        "X-Emby-Authorization": (
            f'MediaBrowser Client="stationmaster", Device="bootstrap", '
            f'DeviceId="bootstrap_001", Version="1.0", Token="{token}"'
        ),
        "Content-Type": "application/json",
    }

    def add_library(name, media_type, paths):
        payload = {
            "LibraryOptions": {
                "EnableRealtimeMonitor": True,
                "EnableChapterImageExtraction": False,
                "PathInfos": [{"Path": p} for p in paths],
            }
        }
        r = requests.post(
            f"{jf_host}/Library/VirtualFolders",
            params={"name": name, "collectionType": media_type, "refreshLibrary": "false"},
            json=payload,
            headers=headers,
            timeout=30,
        )
        if r.status_code in (200, 204):
            ok(f"  Library added: {name} → {paths}")
        else:
            warn(f"  Library '{name}' may already exist or failed: {r.status_code}")

    # Kids Movies (separate library for future parental controls)
    kids_path = str(Path(mount) / env("NAS_MOVIES_KIDS_DIR", "Videos/Movies/Kids"))
    add_library("Kids Movies", "movies", [kids_path])

    # Adult Movies
    adult_path = str(Path(mount) / env("NAS_MOVIES_ADULT_DIR", "Videos/Movies/Adult"))
    add_library("Movies", "movies", [adult_path])

    # TV Shows
    shows_path = str(Path(mount) / env("NAS_SHOWS_DIR", "Videos/TV Shows"))
    add_library("TV Shows", "tvshows", [shows_path])

    # Fitness
    fitness_path = str(Path(mount) / env("NAS_FITNESS_DIR", "Videos/Fitness"))
    add_library("Fitness", "homevideos", [fitness_path])

    # Stand Up Comedy
    standup_path = str(Path(mount) / env("NAS_STANDUP_DIR", "Videos/Stand Up Comedy"))
    add_library("Stand Up Comedy", "homevideos", [standup_path])

    ok("Jellyfin libraries configured.")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 6 — Create Jellyfin media user
# ─────────────────────────────────────────────────────────────────────────────
def phase_jellyfin_media_user():
    hdr("Phase 6 — Jellyfin media user")

    jf_host       = env("JELLYFIN_HOST", "http://localhost:8096")
    jf_user       = env("JELLYFIN_ADMIN_USER", "cmpe8803")
    jf_pass       = env("JELLYFIN_ADMIN_PASS")
    media_user    = env("JELLYFIN_MEDIA_USER", "")
    media_pass    = env("JELLYFIN_MEDIA_PASS", "media123")

    if not media_user:
        info("JELLYFIN_MEDIA_USER not set — skipping media user creation.")
        return

    token = jf_auth(jf_host, jf_user, jf_pass)
    headers = {
        "X-Emby-Authorization": (
            f'MediaBrowser Client="stationmaster", Device="bootstrap", '
            f'DeviceId="bootstrap_001", Version="1.0", Token="{token}"'
        ),
        "Content-Type": "application/json",
    }

    # Create user
    r = requests.post(
        f"{jf_host}/Users/New",
        json={"Name": media_user, "Password": media_pass},
        headers=headers,
        timeout=15,
    )
    if r.status_code in (200, 204):
        ok(f"Media user created: {media_user}")
        user_id = r.json().get("Id", "")
        # Set policy: view-only, can watch Live TV, cannot manage server
        policy = {
            "IsAdministrator": False,
            "EnableContentDownloading": False,
            "EnableMediaPlayback": True,
            "EnableLiveTvAccess": True,
            "EnableLiveTvManagement": False,
            "EnableUserPreferenceAccess": True,
        }
        requests.post(
            f"{jf_host}/Users/{user_id}/Policy",
            json=policy,
            headers=headers,
            timeout=15,
        )
        ok(f"Media user policy set (view-only, Live TV access).")
    else:
        warn(f"Media user '{media_user}' may already exist: {r.status_code}")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 7 — Install ErsatzTV
# ─────────────────────────────────────────────────────────────────────────────
def phase_install_etv():
    hdr("Phase 7 — Install ErsatzTV (linux-arm64)")

    etv_exe = Path(env("ETV_EXE_PATH", "/opt/ersatztv/ErsatzTV"))

    if etv_exe.exists():
        ok(f"ErsatzTV already installed at {etv_exe}")
        return

    repo = env("ETV_GITHUB_REPO", "ErsatzTV/legacy")
    info(f"Fetching latest release from {repo}...")

    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    r = requests.get(api_url, timeout=30)
    if r.status_code != 200:
        die(f"GitHub API request failed: {r.status_code}")

    release = r.json()
    tag = release["tag_name"]
    info(f"Latest release: {tag}")

    # Find linux-arm64 asset
    asset_url = None
    asset_name = None
    for asset in release.get("assets", []):
        name = asset["name"]
        if "linux-arm64" in name and name.endswith(".tar.gz"):
            asset_url = asset["browser_download_url"]
            asset_name = name
            break

    if not asset_url:
        die(
            f"No linux-arm64 .tar.gz asset found in release {tag}.\n"
            f"Available assets: {[a['name'] for a in release.get('assets', [])]}"
        )

    info(f"Downloading {asset_name} ({release['assets'][0].get('size', '?')} bytes)...")
    tmp_tar = Path(f"/tmp/{asset_name}")

    # Stream download with progress
    with requests.get(asset_url, stream=True, timeout=300) as dl:
        total = int(dl.headers.get("content-length", 0))
        downloaded = 0
        with open(tmp_tar, "wb") as f:
            for chunk in dl.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded * 100 / total)
                    print(f"  Downloading... {pct}%", end="\r", flush=True)
    print()
    ok(f"Downloaded to {tmp_tar}")

    # Extract
    install_dir = etv_exe.parent
    run(["mkdir", "-p", str(install_dir)], sudo=True)

    info(f"Extracting to {install_dir}...")
    with tarfile.open(tmp_tar, "r:gz") as tf:
        tf.extractall(path="/tmp/etv_extract")

    # Find the ErsatzTV binary in the extracted tree
    extracted_bin = next(
        (p for p in Path("/tmp/etv_extract").rglob("ErsatzTV") if p.is_file()),
        None
    )
    if not extracted_bin:
        die("Could not locate ErsatzTV binary in extracted archive.")

    # Move everything to install_dir
    extract_root = extracted_bin.parent
    run_shell(f"sudo cp -r {extract_root}/. {install_dir}/")
    run(["chmod", "+x", str(etv_exe)], sudo=True)
    run(["chown", "-R", f"{os.environ.get('USER','cmpe8803')}:", str(install_dir)], sudo=True)

    ok(f"ErsatzTV installed at {etv_exe}")
    tmp_tar.unlink(missing_ok=True)
    shutil.rmtree("/tmp/etv_extract", ignore_errors=True)

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 8 — ErsatzTV first run + DB init
# ─────────────────────────────────────────────────────────────────────────────
def phase_etv_first_run():
    hdr("Phase 8 — ErsatzTV first run")

    etv_exe  = Path(env("ETV_EXE_PATH", "/opt/ersatztv/ErsatzTV"))
    etv_host = env("ETV_HOST", "http://localhost:8409")
    db_path  = Path(env("ETV_DB_PATH", "")).expanduser() if env("ETV_DB_PATH") else \
               Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"

    # Ensure data dir exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Start ErsatzTV in the background if not already running
    result = run_shell("pgrep -f ErsatzTV", check=False, capture=True)
    if result.returncode == 0:
        ok("ErsatzTV process already running.")
    else:
        info("Starting ErsatzTV (first run — initialising DB)...")
        env_vars = os.environ.copy()
        env_vars["HOME"] = str(Path.home())
        subprocess.Popen(
            [str(etv_exe)],
            env=env_vars,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # Wait for SQLite DB to appear
    info("Waiting for ErsatzTV to create its database...")
    deadline = time.time() + 120
    while time.time() < deadline:
        if db_path.exists() and db_path.stat().st_size > 0:
            ok(f"ErsatzTV database created: {db_path}")
            break
        elapsed = int(time.time() - (deadline - 120))
        print(f"  Waiting for DB... ({elapsed}s)", end="\r", flush=True)
        time.sleep(3)
    else:
        die(f"ErsatzTV DB not found at {db_path} after 2 minutes.\n"
            f"Check ErsatzTV logs in ~/.local/share/ersatztv/logs/")
    print()

    # Wait for API to respond
    info("Waiting for ErsatzTV API...")
    if not wait_for_http(f"{etv_host}/iptv/channels.m3u", timeout=120, label="ErsatzTV API"):
        die("ErsatzTV API not responding. Check ~/.local/share/ersatztv/logs/")
    ok("ErsatzTV API is up.")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 9 — Inject ErsatzTV libraries
# ─────────────────────────────────────────────────────────────────────────────
def phase_etv_libraries():
    hdr("Phase 9 — Configure ErsatzTV libraries")

    import sqlite3

    db_path  = Path(env("ETV_DB_PATH", "")).expanduser() if env("ETV_DB_PATH") else \
               Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"
    mount    = env("NAS_MOUNT_POINT", "/mnt/nas")

    # Stop ErsatzTV before touching the DB
    info("Stopping ErsatzTV to configure libraries...")
    run_shell("sudo systemctl stop ersatztv 2>/dev/null || pkill -f ErsatzTV || true", check=False)
    time.sleep(3)

    # Define libraries: (name, kind, subdirs)
    #   kind: "Local" for local filesystem paths
    LIBRARIES = [
        ("Kids Movies",     "LocalMovie",     [env("NAS_MOVIES_KIDS_DIR",  "Videos/Movies/Kids")]),
        ("Movies",          "LocalMovie",     [env("NAS_MOVIES_ADULT_DIR", "Videos/Movies/Adult")]),
        ("TV Shows",        "LocalShow",      [env("NAS_SHOWS_DIR",        "Videos/TV Shows")]),
        ("Fitness",         "LocalOtherVideo",[env("NAS_FITNESS_DIR",      "Videos/Fitness")]),
        ("Stand Up Comedy", "LocalOtherVideo",[env("NAS_STANDUP_DIR",      "Videos/Stand Up Comedy")]),
    ]

    conn = sqlite3.connect(str(db_path))
    cur  = conn.cursor()

    for lib_name, lib_kind, subdirs in LIBRARIES:
        # Check if library already exists
        cur.execute("SELECT Id FROM Library WHERE Name = ?", (lib_name,))
        row = cur.fetchone()

        if row:
            lib_id = row[0]
            info(f"  Library '{lib_name}' already exists (id={lib_id})")
        else:
            # Insert into Library
            cur.execute(
                "INSERT INTO Library (Name, MediaKind, LastScan) VALUES (?, ?, NULL)",
                (lib_name, lib_kind)
            )
            lib_id = cur.lastrowid
            ok(f"  Created library: {lib_name} (id={lib_id})")

        # Insert LibraryPath rows for each subdir
        for subdir in subdirs:
            full_path = str(Path(mount) / subdir)
            cur.execute("SELECT Id FROM LibraryPath WHERE Path = ?", (full_path,))
            if cur.fetchone():
                info(f"    Path already registered: {full_path}")
                continue

            cur.execute(
                "INSERT INTO LibraryPath (LibraryId, Path, LastScan) VALUES (?, ?, NULL)",
                (lib_id, full_path)
            )
            ok(f"    Registered path: {full_path}")

    conn.commit()
    conn.close()

    ok("ErsatzTV libraries configured.")

    # Restart ErsatzTV
    info("Restarting ErsatzTV to trigger library scan...")
    env_vars = os.environ.copy()
    env_vars["HOME"] = str(Path.home())
    subprocess.Popen(
        [str(Path(env("ETV_EXE_PATH", "/opt/ersatztv/ErsatzTV")))],
        env=env_vars,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 10 — Wait for ErsatzTV scan
# ─────────────────────────────────────────────────────────────────────────────
def phase_etv_scan():
    hdr("Phase 10 — Wait for ErsatzTV media scan")

    import sqlite3

    db_path  = Path(env("ETV_DB_PATH", "")).expanduser() if env("ETV_DB_PATH") else \
               Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"
    etv_host = env("ETV_HOST", "http://localhost:8409")

    # Wait for API first
    if not wait_for_http(f"{etv_host}/iptv/channels.m3u", timeout=120, label="ErsatzTV API"):
        die("ErsatzTV API not responding after restart.")

    info("Waiting for media scan to populate MediaItem table...")
    info("(This can take 5–30 minutes depending on library size. Go grab a coffee.)")

    STABLE_FOR = 90   # seconds of no new items = scan complete
    CHECK_INTERVAL = 10

    last_count = -1
    stable_since = None
    deadline = time.time() + 3600  # max 1 hour

    while time.time() < deadline:
        try:
            conn = sqlite3.connect(str(db_path))
            cur  = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM MediaItem")
            count = cur.fetchone()[0]
            conn.close()
        except Exception:
            count = 0

        now = time.time()
        elapsed = int(now - (deadline - 3600))

        if count != last_count:
            print(f"\r  MediaItems: {count} ({elapsed}s elapsed)    ", flush=True)
            last_count = count
            stable_since = now
        elif stable_since and (now - stable_since) >= STABLE_FOR:
            print()
            ok(f"Scan stable — {count} media items found.")
            break
        else:
            remaining = int(STABLE_FOR - (now - (stable_since or now)))
            print(f"\r  MediaItems: {count} — stable for {int(now - (stable_since or now))}s / {STABLE_FOR}s needed   ",
                  end="", flush=True)

        time.sleep(CHECK_INTERVAL)
    else:
        print()
        warn("Scan timeout reached. Proceeding with whatever's been scanned.")
        warn("You can re-run channel building later with: python3 full_setup.py")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 11 — Build channels
# ─────────────────────────────────────────────────────────────────────────────
def phase_build_channels():
    hdr("Phase 11 — Build ErsatzTV channels")

    full_setup = REPO_ROOT / "full_setup.py"
    if not full_setup.exists():
        die(f"full_setup.py not found at {full_setup}")

    info("Running full_setup.py to build channels...")
    result = subprocess.run(
        [sys.executable, str(full_setup)],
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        warn("full_setup.py exited with errors. Check output above.")
        warn("You can re-run it manually: python3 full_setup.py")
    else:
        ok("Channels built successfully.")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 12 — Wire Jellyfin tuner
# ─────────────────────────────────────────────────────────────────────────────
def phase_jellyfin_tuner():
    hdr("Phase 12 — Wire Jellyfin Live TV tuner")

    jf_host    = env("JELLYFIN_HOST", "http://localhost:8096")
    jf_user    = env("JELLYFIN_ADMIN_USER", "cmpe8803")
    jf_pass    = env("JELLYFIN_ADMIN_PASS")
    etv_url    = env("JF_ETV_URL", "http://localhost:8409")
    tuner_count = int(env("JF_TUNER_COUNT", "4"))

    token = jf_auth(jf_host, jf_user, jf_pass)
    headers = {
        "X-Emby-Authorization": (
            f'MediaBrowser Client="stationmaster", Device="bootstrap", '
            f'DeviceId="bootstrap_001", Version="1.0", Token="{token}"'
        ),
        "Content-Type": "application/json",
    }

    m3u_url  = f"{etv_url}/iptv/channels.m3u"
    xmltv_url = f"{etv_url}/iptv/xmltv.xml"

    # Remove duplicate tuners first
    info("Checking for existing ErsatzTV tuners...")
    r = requests.get(f"{jf_host}/LiveTv/TunerHosts", headers=headers, timeout=15)
    if r.status_code == 200:
        existing = [h for h in r.json() if "8409" in h.get("Url", "")]
        for h in existing:
            requests.delete(f"{jf_host}/LiveTv/TunerHosts", params={"id": h["Id"]},
                            headers=headers, timeout=10)
            info(f"  Removed stale tuner: {h.get('Url')}")

    # Register M3U tuner
    info("Registering M3U tuner...")
    r = requests.post(
        f"{jf_host}/LiveTv/TunerHosts",
        json={
            "Type": "m3u",
            "Url": m3u_url,
            "TunerCount": tuner_count,
            "AllowHWTranscoding": False,
        },
        headers=headers,
        timeout=15,
    )
    if r.status_code in (200, 204):
        ok(f"M3U tuner registered: {m3u_url} (TunerCount={tuner_count})")
    else:
        warn(f"M3U tuner registration returned {r.status_code}: {r.text[:200]}")

    # Register XMLTV guide
    info("Registering XMLTV guide provider...")
    r = requests.post(
        f"{jf_host}/LiveTv/ListingProviders",
        json={
            "Type": "xmltv",
            "Path": xmltv_url,
            "EnableAllTuners": True,
        },
        headers=headers,
        timeout=15,
    )
    if r.status_code in (200, 204):
        ok(f"XMLTV guide registered: {xmltv_url}")
    else:
        warn(f"XMLTV registration returned {r.status_code} — add manually in Jellyfin UI if needed.")
        warn(f"  Dashboard → Live TV → TV Guide Data Providers → + → XMLTV → {xmltv_url}")

    # Trigger channel + guide refresh
    info("Triggering channel and guide refresh in Jellyfin...")
    requests.post(f"{jf_host}/LiveTv/Channels/Refresh", headers=headers, timeout=15)
    requests.post(f"{jf_host}/LiveTv/Guide/Refresh",    headers=headers, timeout=15)
    ok("Refresh tasks queued.")

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 13 — Install systemd services
# ─────────────────────────────────────────────────────────────────────────────
def phase_install_services():
    hdr("Phase 13 — Install systemd services")

    install_script = REPO_ROOT / "tools" / "install_services.sh"
    if not install_script.exists():
        die(f"tools/install_services.sh not found at {install_script}")

    run_shell(f"sudo bash {install_script}")
    ok("systemd services installed.")

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
def print_summary():
    hdr("Setup complete!")

    jf_host   = env("JELLYFIN_HOST", "http://localhost:8096")
    jf_user   = env("JELLYFIN_ADMIN_USER", "cmpe8803")
    jf_pass   = env("JELLYFIN_ADMIN_PASS")
    etv_host  = env("ETV_HOST", "http://localhost:8409")
    lan_ip    = get_lan_ip()

    print(f"""
  Jellyfin admin  : {jf_user}
  Jellyfin pass   : {jf_pass}   (also in .env)

  ── Local URLs ────────────────────────────────────────────
  Jellyfin UI     : {jf_host}
  ErsatzTV UI     : {etv_host}

  ── From other devices on your LAN ───────────────────────
  Jellyfin        : http://{lan_ip}:8096
  ErsatzTV        : http://{lan_ip}:8409

  ── Next step ─────────────────────────────────────────────
  Open Jellyfin → Live TV → Guide
  If the guide is empty, wait 60s and refresh the page.
  If channels are missing, run: python3 tools/diagnose.py

  ── Day-2 operations ──────────────────────────────────────
  Health check    : python3 tools/diagnose.py
  Rebuild channels: python3 full_setup.py
  Factory reset   : python3 tools/factory_reset.py --yes
""")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="stationmaster-pi one-command installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--skip-apt",       action="store_true", help="Skip apt package install")
    parser.add_argument("--skip-nas",       action="store_true", help="Skip NAS mount setup")
    parser.add_argument("--skip-jellyfin",  action="store_true", help="Skip Jellyfin install")
    parser.add_argument("--skip-wizard",    action="store_true", help="Skip Jellyfin first-run wizard")
    parser.add_argument("--skip-libs",      action="store_true", help="Skip Jellyfin library setup")
    parser.add_argument("--skip-etv",       action="store_true", help="Skip ErsatzTV install")
    parser.add_argument("--skip-channels",  action="store_true", help="Skip channel building")
    parser.add_argument("--skip-services",  action="store_true", help="Skip systemd service install")
    parser.add_argument("--resume",         action="store_true",
                        help="Skip apt+nas+jellyfin+wizard (pick up at library config)")
    args = parser.parse_args()

    if args.resume:
        args.skip_apt      = True
        args.skip_nas      = True
        args.skip_jellyfin = True
        args.skip_wizard   = True

    print(f"\n{C.BOLD}{C.CYAN}stationmaster-pi bootstrap{C.RESET}")
    print(f"{'─'*60}")

    phase_preflight()

    if not args.skip_apt:
        phase_apt()
    else:
        info("Skipping apt packages.")

    if not args.skip_nas:
        phase_nas()
    else:
        load_env()
        info("Skipping NAS setup — loading .env.")

    if not args.skip_jellyfin:
        phase_install_jellyfin()
    else:
        info("Skipping Jellyfin install.")

    if not args.skip_wizard:
        phase_jellyfin_wizard()
    else:
        info("Skipping Jellyfin wizard.")

    if not args.skip_libs:
        phase_jellyfin_libraries()
        phase_jellyfin_media_user()
    else:
        info("Skipping Jellyfin library setup.")

    if not args.skip_etv:
        phase_install_etv()
        phase_etv_first_run()
        phase_etv_libraries()
        phase_etv_scan()
    else:
        info("Skipping ErsatzTV install.")

    if not args.skip_channels:
        phase_build_channels()
        phase_jellyfin_tuner()
    else:
        info("Skipping channel building.")

    if not args.skip_services:
        phase_install_services()
    else:
        info("Skipping systemd service install.")

    print_summary()


if __name__ == "__main__":
    main()
