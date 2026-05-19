#!/usr/bin/env python3
"""
diagnose.py — stationmaster-pi health checker
==============================================
Run this first when anything is broken. Produces a quick-scan report of every
layer in the stack: env → NAS → system tools → ErsatzTV → Jellyfin → streaming.

Usage:
    python3 tools/diagnose.py
    python3 tools/diagnose.py --stream    # include a 5-second stream test
    python3 tools/diagnose.py --verbose   # print detail on every check

Exit codes:
    0 = all checks passed
    1 = one or more checks failed
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).resolve().parent
REPO_ROOT    = SCRIPT_DIR.parent
ENV_FILE     = REPO_ROOT / ".env"
ETV_DB_PATH  = Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"
ETV_EXE      = Path("/opt/ersatztv/ErsatzTV")
NAS_DEFAULT  = "/mnt/nas"

# ── Colour / output helpers ───────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
RESET  = "\033[0m"

VERBOSE = False


def ok(tag: str, msg: str = "") -> None:
    detail = f"  {msg}" if (VERBOSE and msg) else ""
    print(f"{GREEN}[+]{RESET} {tag}{detail}")


def fail(tag: str, msg: str = "") -> None:
    detail = f"\n    {RED}{msg}{RESET}" if msg else ""
    print(f"{RED}[-]{RESET} {tag}{detail}")


def warn(tag: str, msg: str = "") -> None:
    detail = f"\n    {YELLOW}{msg}{RESET}" if msg else ""
    print(f"{YELLOW}[~]{RESET} {tag}{detail}")


# ── .env loader ───────────────────────────────────────────────────────────────
def load_env(path: Path) -> dict:
    env: dict = {}
    if not path.exists():
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ── HTTP helper ───────────────────────────────────────────────────────────────
def http_get(url: str, token: str | None = None, timeout: int = 5) -> tuple[int, bytes]:
    headers = {}
    if token:
        headers["X-Emby-Authorization"] = (
            f'MediaBrowser Token="{token}", Client="diagnose", '
            'Device="stationmaster-pi", DeviceId="diagnose_001", Version="1.0"'
        )
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def http_post(url: str, payload: dict, token: str | None = None, timeout: int = 5) -> tuple[int, bytes]:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Emby-Authorization"] = (
            f'MediaBrowser Token="{token}", Client="diagnose", '
            'Device="stationmaster-pi", DeviceId="diagnose_001", Version="1.0"'
        )
    else:
        headers["X-Emby-Authorization"] = (
            'MediaBrowser Client="diagnose", Device="stationmaster-pi", '
            'DeviceId="diagnose_001", Version="1.0"'
        )
    try:
        req = Request(url, data=data, headers=headers, method="POST")
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except HTTPError as e:
        return e.code, e.read()
    except Exception:
        return 0, b""


# ══════════════════════════════════════════════════════════════════════════════
# CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def check_env(env: dict) -> bool:
    """Verify .env exists and required keys are present."""
    required = [
        "NAS_UNC_PATH", "NAS_MOUNT_POINT", "NAS_USER", "NAS_PASS",
        "ETV_HOST", "JELLYFIN_HOST", "JELLYFIN_ADMIN_USER",
    ]
    if not ENV_FILE.exists():
        fail("ENV", f".env not found at {ENV_FILE}")
        return False

    missing = [k for k in required if not env.get(k)]
    if missing:
        fail("ENV", f"Missing required keys: {', '.join(missing)}")
        return False

    ok("ENV", f"Loaded {len(env)} vars from {ENV_FILE}")
    return True


def check_nas(env: dict) -> bool:
    """Confirm NAS is mounted and readable."""
    mount_point = env.get("NAS_MOUNT_POINT", NAS_DEFAULT)
    mp = Path(mount_point)

    if not mp.exists():
        fail("NAS", f"Mount point {mount_point} does not exist")
        return False

    # Check if it's actually mounted (not just the directory)
    result = subprocess.run(
        ["mountpoint", "-q", mount_point],
        capture_output=True,
    )
    if result.returncode != 0:
        fail("NAS", f"{mount_point} exists but is NOT mounted")
        return False

    # Quick readability test
    try:
        entries = list(mp.iterdir())
        ok("NAS", f"{mount_point} mounted, {len(entries)} top-level entries")
        return True
    except PermissionError:
        fail("NAS", f"{mount_point} mounted but not readable (permissions?)")
        return False


def check_ffmpeg() -> bool:
    """Verify ffmpeg/ffprobe are on PATH."""
    ffmpeg  = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if ffmpeg and ffprobe:
        try:
            ver = subprocess.check_output(
                ["ffmpeg", "-version"], stderr=subprocess.STDOUT, text=True
            ).splitlines()[0]
        except Exception:
            ver = "unknown"
        ok("FFMPEG", ver)
        return True
    else:
        missing = []
        if not ffmpeg:
            missing.append("ffmpeg")
        if not ffprobe:
            missing.append("ffprobe")
        fail("FFMPEG", f"Not found: {', '.join(missing)}")
        return False


def check_etv_process(env: dict) -> bool:
    """Check ErsatzTV is running via systemctl or process."""
    # Prefer systemctl if available
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", "ersatztv"],
        capture_output=True,
    )
    if result.returncode == 0:
        ok("ETV", "ersatztv.service is active (systemd)")
        return True

    # Fallback: pgrep
    result2 = subprocess.run(
        ["pgrep", "-f", "ErsatzTV"],
        capture_output=True, text=True,
    )
    if result2.returncode == 0:
        pids = result2.stdout.strip().split()
        ok("ETV", f"ErsatzTV process running (PID {', '.join(pids)}), but not via systemd")
        return True

    fail("ETV", "ErsatzTV is NOT running — start it with: sudo systemctl start ersatztv")
    return False


def check_etv_api(env: dict) -> bool:
    """Verify ErsatzTV API responds."""
    host = env.get("ETV_HOST", "http://localhost:8409")

    # Try health endpoint first, fall back to channels.m3u (some versions lack /health)
    for path in ("/api/health", "/iptv/channels.m3u"):
        url = f"{host}{path}"
        code, body = http_get(url, timeout=5)
        if code in (200, 204):
            ok("ETVAPI", f"GET {path} → HTTP {code}")
            return True

    fail("ETVAPI", f"ErsatzTV API not reachable at {host} — check logs: journalctl -u ersatztv -n 50")
    return False


def check_etv_db() -> bool:
    """Inspect ErsatzTV SQLite DB for sanity."""
    if not ETV_DB_PATH.exists():
        fail("ETVDB", f"DB not found at {ETV_DB_PATH} (ETV may never have started)")
        return False

    try:
        con = sqlite3.connect(str(ETV_DB_PATH), timeout=5)
        cur = con.cursor()

        lib_count = cur.execute("SELECT COUNT(*) FROM Library").fetchone()[0]
        media_count = cur.execute("SELECT COUNT(*) FROM MediaItem").fetchone()[0]
        channel_count = cur.execute("SELECT COUNT(*) FROM Channel").fetchone()[0]
        playout_count = cur.execute("SELECT COUNT(*) FROM Playout").fetchone()[0]
        con.close()

        tag = "ETVDB"
        detail = (
            f"libs={lib_count}  media={media_count}  "
            f"channels={channel_count}  playouts={playout_count}"
        )

        issues = []
        if lib_count == 0:
            issues.append("no libraries (run bootstrap.py phase 9)")
        if media_count == 0:
            issues.append("no media items (libraries not scanned yet)")
        if channel_count == 0:
            issues.append("no channels (run full_setup.py)")
        if playout_count == 0:
            issues.append("no playouts (run full_setup.py)")

        if issues:
            warn(tag, detail + " — " + "; ".join(issues))
            return False
        else:
            ok(tag, detail)
            return True

    except sqlite3.OperationalError as e:
        fail("ETVDB", f"DB read error: {e}")
        return False


def check_jellyfin_api(env: dict) -> tuple[bool, str | None]:
    """Ping Jellyfin and return (success, auth_token)."""
    host = env.get("JELLYFIN_HOST", "http://localhost:8096")
    user = env.get("JELLYFIN_ADMIN_USER", "")
    pw   = env.get("JELLYFIN_ADMIN_PASS", "")

    # Basic ping
    code, _ = http_get(f"{host}/System/Info/Public", timeout=5)
    if code != 200:
        fail("JFAPI", f"Jellyfin not reachable at {host} (HTTP {code})")
        return False, None

    if not pw:
        warn("JFAPI", "Jellyfin reachable but JELLYFIN_ADMIN_PASS not set — skipping auth checks")
        return True, None

    # Authenticate
    code2, body2 = http_post(
        f"{host}/Users/AuthenticateByName",
        {"Username": user, "Pw": pw},
        timeout=8,
    )
    if code2 != 200:
        warn("JFAPI", f"Jellyfin reachable but auth failed (HTTP {code2}) — check credentials in .env")
        return True, None

    try:
        token = json.loads(body2).get("AccessToken")
    except Exception:
        token = None

    ok("JFAPI", f"{host} → authenticated as {user}")
    return True, token


def check_jellyfin_tuner(env: dict, token: str | None) -> bool:
    """Verify ErsatzTV tuner is registered in Jellyfin."""
    host = env.get("JELLYFIN_HOST", "http://localhost:8096")
    etv_url = env.get("JF_ETV_URL", env.get("ETV_HOST", "http://localhost:8409"))

    if not token:
        warn("JFCFG", "No auth token — skipping tuner check")
        return True  # treat as non-blocking

    code, body = http_get(f"{host}/LiveTv/TunerHosts", token=token, timeout=5)
    if code != 200:
        fail("JFCFG", f"Could not fetch tuner list (HTTP {code})")
        return False

    try:
        tuners = json.loads(body)
    except Exception:
        fail("JFCFG", "Could not parse tuner response")
        return False

    etv_tuners = [
        t for t in tuners
        if "8409" in t.get("Url", "") or "ersatztv" in t.get("Url", "").lower()
    ]

    if not etv_tuners:
        fail("JFCFG", f"No ErsatzTV tuner registered — run full_setup.py (expected URL includes {etv_url})")
        return False

    current_url = etv_tuners[0].get("Url", "")
    expected    = f"{etv_url}/iptv/channels.m3u"

    if current_url != expected:
        warn("JFCFG", f"Tuner URL mismatch:\n    current  = {current_url}\n    expected = {expected}\n    Run full_setup.py to reconcile")
        return False

    ok("JFCFG", f"Tuner registered: {current_url}")
    return True


def check_jellyfin_epg(env: dict, token: str | None) -> bool:
    """Verify XMLTV / EPG guide source is registered in Jellyfin."""
    host = env.get("JELLYFIN_HOST", "http://localhost:8096")

    if not token:
        warn("JFEPG", "No auth token — skipping EPG check")
        return True

    code, body = http_get(f"{host}/LiveTv/ListingProviders", token=token, timeout=5)
    if code != 200:
        fail("JFEPG", f"Could not fetch listing providers (HTTP {code})")
        return False

    try:
        providers = json.loads(body)
    except Exception:
        fail("JFEPG", "Could not parse listing providers response")
        return False

    if not providers:
        fail("JFEPG", "No XMLTV guide registered — add XMLTV source in Jellyfin → Live TV → Guide Data Providers")
        return False

    urls = [p.get("Path", p.get("Url", "?")) for p in providers]
    ok("JFEPG", f"{len(providers)} guide provider(s): {', '.join(urls)}")
    return True


def check_stream(env: dict, duration: int = 5) -> bool:
    """Pull 5 seconds of channel 1 via ffmpeg as a live stream smoke test."""
    host = env.get("ETV_HOST", "http://localhost:8409")
    stream_url = f"{host}/iptv/channel/1.m3u8"

    print(f"    Testing stream: {stream_url} ({duration}s)...")

    # First check if the URL is reachable
    code, body = http_get(stream_url, timeout=5)
    if code != 200:
        fail("STREAM", f"Channel 1 HLS playlist not reachable (HTTP {code}) — is channel 1 built in ErsatzTV?")
        return False

    # Try to pull frames via ffmpeg
    if not shutil.which("ffmpeg"):
        warn("STREAM", "ffmpeg not available — skipping frame pull (URL reachable)")
        return True

    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", stream_url,
            "-t", str(duration),
            "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=30,
    )

    if result.returncode == 0:
        ok("STREAM", f"ffmpeg pulled {duration}s from channel 1 successfully")
        return True
    else:
        err = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        fail("STREAM", f"ffmpeg stream test failed: {err}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    global VERBOSE

    parser = argparse.ArgumentParser(
        description="stationmaster-pi health checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--stream", action="store_true",
                        help="Include a 5-second live stream smoke test (slower)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show detail on passing checks too")
    args = parser.parse_args()

    VERBOSE = args.verbose

    print()
    print("━" * 56)
    print("  stationmaster-pi  ·  System Diagnostics")
    print("━" * 56)
    print()

    env = load_env(ENV_FILE)
    results: dict[str, bool] = {}

    # ── ENV ───────────────────────────────────────────────────────────────────
    results["ENV"] = check_env(env)

    # ── NAS ───────────────────────────────────────────────────────────────────
    results["NAS"] = check_nas(env)

    # ── FFMPEG ────────────────────────────────────────────────────────────────
    results["FFMPEG"] = check_ffmpeg()

    # ── ErsatzTV ─────────────────────────────────────────────────────────────
    results["ETV"]    = check_etv_process(env)
    results["ETVAPI"] = check_etv_api(env)
    results["ETVDB"]  = check_etv_db()

    # ── Jellyfin ──────────────────────────────────────────────────────────────
    jf_ok, token      = check_jellyfin_api(env)
    results["JFAPI"]  = jf_ok
    results["JFCFG"]  = check_jellyfin_tuner(env, token)
    results["JFEPG"]  = check_jellyfin_epg(env, token)

    # ── Stream (optional) ─────────────────────────────────────────────────────
    if args.stream:
        results["STREAM"] = check_stream(env)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("━" * 56)
    passed = sum(1 for v in results.values() if v)
    total  = len(results)
    colour = GREEN if passed == total else RED
    print(f"  {colour}{passed}/{total} checks passed{RESET}")

    failed = [k for k, v in results.items() if not v]
    if failed:
        print(f"  {RED}Failed: {', '.join(failed)}{RESET}")
        print()
        print("  Suggested next steps:")
        if "ENV" in failed:
            print("    • Copy .env.example → .env and fill in your values")
        if "NAS" in failed:
            print("    • Check NAS is on the network: ping WDMYCLOUD")
            print("    • Try manually: sudo mount /mnt/nas")
            print("    • Verify /etc/fstab entry and /etc/stationmaster-nas.creds")
        if "FFMPEG" in failed:
            print("    • sudo apt install ffmpeg")
        if "ETV" in failed:
            print("    • sudo systemctl start ersatztv")
            print("    • journalctl -u ersatztv -n 50")
        if "ETVAPI" in failed:
            print("    • ErsatzTV started but API not up — wait 15s and retry")
            print("    • Check: journalctl -u ersatztv -n 50")
        if "ETVDB" in failed:
            print("    • Run bootstrap.py to initialise libraries")
            print("    • Run full_setup.py to build channels")
        if "JFAPI" in failed:
            print("    • sudo systemctl start jellyfin")
            print("    • journalctl -u jellyfin -n 50")
        if "JFCFG" in failed:
            print("    • Run full_setup.py — it registers the ErsatzTV tuner")
        if "JFEPG" in failed:
            print("    • Add XMLTV guide: Jellyfin → Live TV → Guide Data Providers")
            print(f"      URL: {env.get('ETV_HOST','http://localhost:8409')}/xmltv/channels.xml")
        if "STREAM" in failed:
            print("    • Check ErsatzTV has a playout built for channel 1")
            print("    • In ErsatzTV UI: go to Channels → Channel 1 → Playout")

    print("━" * 56)
    print()

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
