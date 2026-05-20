#!/usr/bin/env python3
"""
fix_jf_libraries.py
Removes duplicate Jellyfin libraries and adds any missing ones.
Run directly: python3 fix_jf_libraries.py
"""
import sys, time
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests",
                           "--break-system-packages", "-q"])
    import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import os

JF_HOST  = os.environ.get("JELLYFIN_HOST",        "http://localhost:8096")
JF_USER  = os.environ.get("JELLYFIN_ADMIN_USER",   "cmpe8803")
JF_PASS  = os.environ.get("JELLYFIN_ADMIN_PASS",   "")
MOUNT    = os.environ.get("NAS_MOUNT_POINT",        "/mnt/nas")

GREEN  = "\033[0;32m"; YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"; RESET  = "\033[0m"
def ok(m):   print(f"{GREEN}[+]{RESET} {m}")
def info(m): print(f"{CYAN}[~]{RESET} {m}")
def warn(m): print(f"{YELLOW}[!]{RESET} {m}")

# ── Auth ──────────────────────────────────────────────────────────────────────
print(f"\n Fixing Jellyfin libraries on {JF_HOST}\n{'─'*50}")
info(f"Authenticating as {JF_USER}...")
for attempt in range(1, 7):
    try:
        r = requests.post(f"{JF_HOST}/Users/AuthenticateByName",
            json={"Username": JF_USER, "Pw": JF_PASS},
            headers={"X-Emby-Authorization": 'MediaBrowser Client="fix-libs", Device="pi", DeviceId="fix001", Version="1.0"'},
            timeout=30)
        if r.status_code == 200:
            token = r.json()["AccessToken"]
            ok(f"Authenticated (token={token[:12]}...)")
            break
        else:
            print(f"  Auth failed: HTTP {r.status_code} — {r.text[:100]}")
            sys.exit(1)
    except requests.exceptions.Timeout:
        warn(f"Jellyfin busy (attempt {attempt}/6), retrying in 15s...")
        time.sleep(15)
else:
    print("Could not reach Jellyfin after 6 attempts. Is it running?")
    sys.exit(1)

HDR = {
    "X-Emby-Authorization": (f'MediaBrowser Client="fix-libs", Device="pi", DeviceId="fix001", Version="1.0", Token="{token}"'),
    "Content-Type": "application/json",
}

# ── List current libraries ────────────────────────────────────────────────────
def get_libraries():
    for attempt in range(1, 7):
        try:
            r = requests.get(f"{JF_HOST}/Library/VirtualFolders", headers=HDR, timeout=60)
            if r.status_code == 200:
                return r.json()
        except requests.exceptions.Timeout:
            warn(f"  List timeout (attempt {attempt}/6), retrying in 15s...")
            time.sleep(15)
    return []

info("Fetching current libraries...")
libs = get_libraries()
print(f"  Found {len(libs)} libraries:")
for lib in libs:
    paths = [p.get("Path", "?") for p in lib.get("Locations", [])]
    print(f"    [{lib['CollectionType']}] {lib['Name']} → {paths}")

# ── Remove duplicates ─────────────────────────────────────────────────────────
from collections import defaultdict
by_name = defaultdict(list)
for lib in libs:
    by_name[lib["Name"]].append(lib)

print()
info("Checking for duplicates...")
for name, copies in by_name.items():
    if len(copies) > 1:
        warn(f"  '{name}' has {len(copies)} copies — removing extras...")
        for dup in copies[1:]:
            lid = dup.get("ItemId") or dup.get("Id", "")
            r = requests.delete(f"{JF_HOST}/Library/VirtualFolders",
                params={"id": lid}, headers=HDR, timeout=60)
            if r.status_code in (200, 204):
                ok(f"    Deleted duplicate '{name}' (id={lid})")
            else:
                warn(f"    Delete returned {r.status_code}: {r.text[:100]}")

# ── Add missing libraries ─────────────────────────────────────────────────────
existing = {lib["Name"] for lib in get_libraries()}

LIBRARIES = []

kids_path  = str(Path(MOUNT) / "Videos/Movies/Kids")
adult_path = str(Path(MOUNT) / "Videos/Movies/Adult")
flat_path  = str(Path(MOUNT) / "Videos/Movies")

if Path(kids_path).exists():
    LIBRARIES.append(("Kids Movies", "movies", [kids_path]))
if Path(adult_path).exists():
    LIBRARIES.append(("Movies", "movies", [adult_path]))
elif Path(flat_path).exists():
    info(f"No Movies/Adult subdir — using flat Movies folder")
    LIBRARIES.append(("Movies", "movies", [flat_path]))

LIBRARIES += [
    ("TV Shows",        "tvshows",    [str(Path(MOUNT) / "Videos/TV Shows")]),
    ("Fitness",         "homevideos", [str(Path(MOUNT) / "Videos/Fitness")]),
    ("Stand Up Comedy", "homevideos", [str(Path(MOUNT) / "Videos/Stand Up Comedy")]),
]

print()
info("Adding missing libraries...")
for name, ctype, paths in LIBRARIES:
    if name in existing:
        info(f"  '{name}' already exists — skipping.")
        continue
    valid = [p for p in paths if Path(p).exists()]
    if not valid:
        warn(f"  '{name}' — path not found on disk: {paths}  Skipping.")
        continue
    payload = {"LibraryOptions": {"EnableRealtimeMonitor": True,
               "EnableChapterImageExtraction": False,
               "PathInfos": [{"Path": p} for p in valid]}}
    for attempt in range(1, 6):
        try:
            r = requests.post(f"{JF_HOST}/Library/VirtualFolders",
                params={"name": name, "collectionType": ctype, "refreshLibrary": "false"},
                json=payload, headers=HDR, timeout=120)
            if r.status_code in (200, 204):
                ok(f"  Added '{name}' → {valid}")
                existing.add(name)
                break
            elif r.status_code == 409:
                info(f"  '{name}' already exists.")
                existing.add(name)
                break
            else:
                warn(f"  '{name}' failed: HTTP {r.status_code} {r.text[:150]}")
                break
        except requests.exceptions.Timeout:
            wait = attempt * 20
            warn(f"  Timeout on '{name}' (attempt {attempt}/5), retrying in {wait}s...")
            time.sleep(wait)

print()
ok("Done. Final library list:")
for lib in get_libraries():
    paths = [p.get("Path", "?") for p in lib.get("Locations", [])]
    print(f"  [{lib['CollectionType']}] {lib['Name']} → {paths}")
