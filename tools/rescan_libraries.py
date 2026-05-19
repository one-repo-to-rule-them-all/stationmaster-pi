#!/usr/bin/env python3
"""
rescan_libraries.py — Force a library re-scan in both Jellyfin and ErsatzTV
============================================================================
Use this after adding new media to the NAS. Triggers scans in both systems
and optionally waits for them to complete before returning.

Usage:
    python3 tools/rescan_libraries.py
    python3 tools/rescan_libraries.py --jellyfin-only
    python3 tools/rescan_libraries.py --etv-only
    python3 tools/rescan_libraries.py --wait      # poll until scan complete (up to 10 min)
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

SCRIPT_DIR  = Path(__file__).resolve().parent
REPO_ROOT   = SCRIPT_DIR.parent
ENV_FILE    = REPO_ROOT / ".env"
ETV_DB_PATH = Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"

GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def load_env() -> dict:
    env: dict = {}
    if not ENV_FILE.exists():
        return env
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def http_get(url: str, token: str | None = None, timeout: int = 8) -> tuple[int, bytes]:
    headers = {}
    if token:
        headers["X-Emby-Authorization"] = (
            f'MediaBrowser Token="{token}", Client="rescan", '
            'Device="stationmaster-pi", DeviceId="rescan_001", Version="1.0"'
        )
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except Exception:
        return 0, b""


def http_post(url: str, payload: dict | None = None, token: str | None = None,
              timeout: int = 8) -> tuple[int, bytes]:
    data = json.dumps(payload or {}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Emby-Authorization"] = (
            f'MediaBrowser Token="{token}", Client="rescan", '
            'Device="stationmaster-pi", DeviceId="rescan_001", Version="1.0"'
        )
    else:
        headers["X-Emby-Authorization"] = (
            'MediaBrowser Client="rescan", Device="stationmaster-pi", '
            'DeviceId="rescan_001", Version="1.0"'
        )
    try:
        req = Request(url, data=data, headers=headers, method="POST")
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except Exception:
        return 0, b""


def jf_auth(env: dict) -> str | None:
    host = env.get("JELLYFIN_HOST", "http://localhost:8096")
    user = env.get("JELLYFIN_ADMIN_USER", "")
    pw   = env.get("JELLYFIN_ADMIN_PASS", "")
    if not pw:
        print(f"  {YELLOW}JELLYFIN_ADMIN_PASS not set in .env — skipping Jellyfin rescan{RESET}")
        return None
    code, body = http_post(
        f"{host}/Users/AuthenticateByName",
        {"Username": user, "Pw": pw},
    )
    if code == 200:
        try:
            return json.loads(body).get("AccessToken")
        except Exception:
            return None
    print(f"  {RED}Jellyfin auth failed (HTTP {code}){RESET}")
    return None


def rescan_jellyfin(env: dict, wait: bool) -> bool:
    host  = env.get("JELLYFIN_HOST", "http://localhost:8096")
    token = jf_auth(env)
    if not token:
        return False

    code, _ = http_post(f"{host}/Library/Refresh", token=token)
    if code in (200, 204):
        print(f"  {GREEN}Jellyfin library refresh triggered{RESET}")
    else:
        print(f"  {YELLOW}Jellyfin refresh returned HTTP {code}{RESET}")
        return False

    if wait:
        print("  Waiting for Jellyfin scan to complete (checking every 15s)...")
        for attempt in range(40):  # up to 10 min
            time.sleep(15)
            code2, body2 = http_get(f"{host}/System/ActivityLog/Entries?startIndex=0&limit=5",
                                    token=token)
            if code2 == 200:
                try:
                    entries = json.loads(body2).get("Items", [])
                    for e in entries:
                        if "scan" in e.get("Name", "").lower() and "complete" in e.get("Name", "").lower():
                            print(f"  {GREEN}Jellyfin scan complete (detected in activity log){RESET}")
                            return True
                except Exception:
                    pass
            print(f"  Still scanning... ({(attempt+1)*15}s elapsed)")

        print(f"  {YELLOW}Scan still in progress after 10 min — check Jellyfin dashboard{RESET}")

    return True


def rescan_etv(env: dict, wait: bool) -> bool:
    host = env.get("ETV_HOST", "http://localhost:8409")

    if not ETV_DB_PATH.exists():
        print(f"  {RED}ETV DB not found — was bootstrap.py run?{RESET}")
        return False

    con = sqlite3.connect(str(ETV_DB_PATH), timeout=5)
    con.row_factory = sqlite3.Row
    libs = con.execute("SELECT Id, Name FROM Library").fetchall()
    baseline_count = con.execute("SELECT COUNT(*) FROM MediaItem").fetchone()[0]
    con.close()

    if not libs:
        print(f"  {YELLOW}No libraries in ETV DB — run bootstrap.py{RESET}")
        return False

    print(f"  Triggering ErsatzTV scan for {len(libs)} libraries...")
    scanned = 0
    for lib in libs:
        url = f"{host}/api/libraries/{lib['Id']}/scan"
        code, _ = http_post(url)
        if code in (200, 202, 204):
            print(f"    {GREEN}Queued: {lib['Name']}{RESET}")
            scanned += 1
        else:
            print(f"    {YELLOW}HTTP {code} for {lib['Name']} — trigger scan manually in ETV UI{RESET}")

    if wait and scanned:
        print("  Waiting for ErsatzTV scan (checking every 20s)...")
        for attempt in range(30):  # up to 10 min
            time.sleep(20)
            con2 = sqlite3.connect(str(ETV_DB_PATH), timeout=5)
            new_count = con2.execute("SELECT COUNT(*) FROM MediaItem").fetchone()[0]
            con2.close()
            if new_count != baseline_count:
                print(f"  {GREEN}ETV scan progress: {baseline_count} → {new_count} media items{RESET}")
                baseline_count = new_count
            else:
                print(f"  MediaItem count stable at {new_count} ({(attempt+1)*20}s elapsed)")
                if attempt > 3:
                    print(f"  {GREEN}Scan appears complete{RESET}")
                    break

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-scan Jellyfin and ErsatzTV libraries")
    parser.add_argument("--jellyfin-only", action="store_true")
    parser.add_argument("--etv-only",      action="store_true")
    parser.add_argument("--wait",          action="store_true",
                        help="Poll until scans complete before returning")
    args = parser.parse_args()

    print()
    print(f"{BOLD}{'━' * 50}{RESET}")
    print(f"  {BOLD}Library Rescan{RESET}")
    print(f"{BOLD}{'━' * 50}{RESET}")
    print()

    env = load_env()
    ok  = True

    if not args.etv_only:
        print(f"{BOLD}Jellyfin:{RESET}")
        ok = rescan_jellyfin(env, args.wait) and ok
        print()

    if not args.jellyfin_only:
        print(f"{BOLD}ErsatzTV:{RESET}")
        ok = rescan_etv(env, args.wait) and ok
        print()

    print(f"{BOLD}{'━' * 50}{RESET}")
    status = f"{GREEN}Done{RESET}" if ok else f"{YELLOW}Done with warnings{RESET}"
    print(f"  {status}")
    print(f"{BOLD}{'━' * 50}{RESET}")
    print()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
