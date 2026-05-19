#!/usr/bin/env python3
"""
full_cleanup.py — stationmaster-pi 7-stage maintenance pipeline
===============================================================
A best-effort house-keeping run that keeps the ErsatzTV DB and Jellyfin
metadata clean without wiping everything.

Stages:
  1. Purge stale MediaItems whose files no longer exist on the NAS
  2. Deduplicate MediaItems that share the same file path
  3. Remove orphaned PlayoutItems (playout deleted but items remain)
  4. Compact the SQLite DB (VACUUM)
  5. Trigger Jellyfin library refresh
  6. Trigger ErsatzTV library re-scan
  7. Verify playout health (warn on any channel with 0 items)

Usage:
    python3 tools/full_cleanup.py
    python3 tools/full_cleanup.py --stage 1,3,4   # run specific stages only
    python3 tools/full_cleanup.py --dry-run        # report without modifying DB
    python3 tools/full_cleanup.py --yes            # skip confirmation
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).resolve().parent
REPO_ROOT   = SCRIPT_DIR.parent
ENV_FILE    = REPO_ROOT / ".env"
ETV_DB_PATH = Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

DRY_RUN = False


def banner(stage: int, msg: str) -> None:
    tag = f"Stage {stage}"
    dry = "  [DRY-RUN]" if DRY_RUN else ""
    print(f"\n{BOLD}── {tag}: {msg} {'─' * max(0, 44 - len(msg))}{RESET}{dry}")


def info(msg: str) -> None:
    print(f"  {msg}")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET}  {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET}  {msg}")


def err(msg: str) -> None:
    print(f"  {RED}✗{RESET}  {msg}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────
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


def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(str(ETV_DB_PATH), timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    return con


def http_get(url: str, token: str | None = None, timeout: int = 8) -> tuple[int, bytes]:
    headers = {}
    if token:
        headers["X-Emby-Authorization"] = (
            f'MediaBrowser Token="{token}", Client="cleanup", '
            'Device="stationmaster-pi", DeviceId="cleanup_001", Version="1.0"'
        )
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except Exception:
        return 0, b""


def http_post(url: str, payload: dict, token: str | None = None, timeout: int = 8) -> tuple[int, bytes]:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Emby-Authorization"] = (
            f'MediaBrowser Token="{token}", Client="cleanup", '
            'Device="stationmaster-pi", DeviceId="cleanup_001", Version="1.0"'
        )
    else:
        headers["X-Emby-Authorization"] = (
            'MediaBrowser Client="cleanup", Device="stationmaster-pi", '
            'DeviceId="cleanup_001", Version="1.0"'
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
    return None


# ══════════════════════════════════════════════════════════════════════════════
# STAGES
# ══════════════════════════════════════════════════════════════════════════════

def stage1_purge_stale_media(env: dict) -> int:
    """Remove MediaItems whose backing files no longer exist."""
    banner(1, "Purge stale MediaItems (missing files)")
    mount_point = env.get("NAS_MOUNT_POINT", "/mnt/nas")

    con = open_db()
    cur = con.cursor()

    # Fetch all MediaFile paths
    rows = cur.execute(
        "SELECT mf.Id, mf.Path, mi.Id AS MediaItemId "
        "FROM MediaFile mf "
        "JOIN MediaItem mi ON mi.Id = mf.MediaItemId"
    ).fetchall()

    info(f"Checking {len(rows)} media file records...")
    stale_item_ids = set()

    for row in rows:
        path = row["Path"]
        # Convert ETV absolute path to something we can check on the Pi
        p = Path(path)
        if not p.exists():
            stale_item_ids.add(row["MediaItemId"])

    if not stale_item_ids:
        ok("No stale media items found")
        con.close()
        return 0

    warn(f"Found {len(stale_item_ids)} stale MediaItem(s)")

    if not DRY_RUN:
        # Delete MediaFiles first (FK constraint)
        cur.execute(
            f"DELETE FROM MediaFile WHERE MediaItemId IN ({','.join('?' * len(stale_item_ids))})",
            list(stale_item_ids),
        )
        mf_del = cur.rowcount

        # Delete MediaItems
        cur.execute(
            f"DELETE FROM MediaItem WHERE Id IN ({','.join('?' * len(stale_item_ids))})",
            list(stale_item_ids),
        )
        mi_del = cur.rowcount

        # Also clean up CollectionItems pointing to now-deleted MediaItems
        cur.execute(
            f"DELETE FROM CollectionItem WHERE MediaItemId IN ({','.join('?' * len(stale_item_ids))})",
            list(stale_item_ids),
        )

        con.commit()
        ok(f"Deleted {mi_del} MediaItem(s) and {mf_del} MediaFile record(s)")
    else:
        info(f"  [DRY-RUN] Would delete {len(stale_item_ids)} MediaItem(s)")

    con.close()
    return len(stale_item_ids)


def stage2_dedup_media() -> int:
    """Remove duplicate MediaItems sharing the same file path."""
    banner(2, "Deduplicate MediaItems (same file path)")

    con = open_db()
    cur = con.cursor()

    # Find paths with more than one MediaFile record
    dups = cur.execute("""
        SELECT mf.Path, COUNT(*) as cnt, MIN(mf.MediaItemId) as keep_id
        FROM MediaFile mf
        GROUP BY mf.Path
        HAVING COUNT(*) > 1
    """).fetchall()

    if not dups:
        ok("No duplicate file paths found")
        con.close()
        return 0

    info(f"Found {len(dups)} duplicate path(s)")
    total_removed = 0

    for dup in dups:
        path     = dup["Path"]
        keep_id  = dup["keep_id"]

        dupe_ids = [
            r["MediaItemId"]
            for r in cur.execute(
                "SELECT MediaItemId FROM MediaFile WHERE Path=? AND MediaItemId != ?",
                (path, keep_id),
            ).fetchall()
        ]

        if not dupe_ids:
            continue

        info(f"  Path: {Path(path).name}  keeping={keep_id}  removing={dupe_ids}")

        if not DRY_RUN:
            cur.execute(
                f"DELETE FROM MediaFile WHERE MediaItemId IN ({','.join('?' * len(dupe_ids))})",
                dupe_ids,
            )
            cur.execute(
                f"DELETE FROM MediaItem WHERE Id IN ({','.join('?' * len(dupe_ids))})",
                dupe_ids,
            )
            cur.execute(
                f"DELETE FROM CollectionItem WHERE MediaItemId IN ({','.join('?' * len(dupe_ids))})",
                dupe_ids,
            )
            total_removed += len(dupe_ids)

    if not DRY_RUN:
        con.commit()
        ok(f"Removed {total_removed} duplicate MediaItem(s)")
    else:
        info(f"  [DRY-RUN] Would remove {sum(len([r for r in cur.execute('SELECT MediaItemId FROM MediaFile WHERE Path=? AND MediaItemId != ?', (dup['Path'], dup['keep_id'])).fetchall()]) for dup in dups)} duplicate(s)")

    con.close()
    return total_removed


def stage3_orphan_playout_items() -> int:
    """Remove PlayoutItems whose parent Playout no longer exists."""
    banner(3, "Remove orphaned PlayoutItems")

    con = open_db()
    cur = con.cursor()

    # PlayoutItems with no matching Playout
    orphans = cur.execute("""
        SELECT pi.Id
        FROM PlayoutItem pi
        LEFT JOIN Playout p ON p.Id = pi.PlayoutId
        WHERE p.Id IS NULL
    """).fetchall()

    if not orphans:
        ok("No orphaned PlayoutItems found")
        con.close()
        return 0

    orphan_ids = [r["Id"] for r in orphans]
    info(f"Found {len(orphan_ids)} orphaned PlayoutItem(s)")

    if not DRY_RUN:
        cur.execute(
            f"DELETE FROM PlayoutItem WHERE Id IN ({','.join('?' * len(orphan_ids))})",
            orphan_ids,
        )
        con.commit()
        ok(f"Deleted {len(orphan_ids)} orphaned PlayoutItem(s)")

    con.close()
    return len(orphan_ids)


def stage4_vacuum_db() -> None:
    """VACUUM the SQLite DB to reclaim space and defragment pages."""
    banner(4, "Compact DB (VACUUM)")

    if DRY_RUN:
        info("[DRY-RUN] Would run VACUUM")
        return

    size_before = ETV_DB_PATH.stat().st_size
    # VACUUM must run outside of a WAL transaction
    con = sqlite3.connect(str(ETV_DB_PATH), timeout=10)
    con.execute("VACUUM")
    con.close()
    size_after = ETV_DB_PATH.stat().st_size

    saved = size_before - size_after
    ok(
        f"VACUUM complete  "
        f"before={size_before // 1024}KB  after={size_after // 1024}KB  "
        f"saved={max(0, saved) // 1024}KB"
    )


def stage5_jellyfin_refresh(env: dict) -> None:
    """Trigger a Jellyfin library scan."""
    banner(5, "Trigger Jellyfin library refresh")

    host  = env.get("JELLYFIN_HOST", "http://localhost:8096")
    token = jf_auth(env)

    if not token:
        warn("No Jellyfin auth token — skipping (set JELLYFIN_ADMIN_PASS in .env)")
        return

    if DRY_RUN:
        info("[DRY-RUN] Would POST /Library/Refresh")
        return

    code, _ = http_post(f"{host}/Library/Refresh", {}, token=token)
    if code in (200, 204):
        ok(f"Jellyfin library refresh triggered → HTTP {code}")
    else:
        warn(f"Jellyfin refresh returned HTTP {code} — it may already be scanning")


def stage6_etv_rescan(env: dict) -> None:
    """Tell ErsatzTV to re-scan all libraries."""
    banner(6, "Trigger ErsatzTV library re-scan")

    host = env.get("ETV_HOST", "http://localhost:8409")

    if DRY_RUN:
        info("[DRY-RUN] Would POST ErsatzTV scan endpoint")
        return

    # ErsatzTV doesn't have a single "scan all" REST endpoint in older versions.
    # The best approach without the UI is to fetch library IDs from DB and call
    # the per-library scan endpoint if available, or just let the scheduled scan run.
    con = open_db()
    libs = con.execute("SELECT Id, Name FROM Library").fetchall()
    con.close()

    if not libs:
        warn("No libraries found in ErsatzTV DB — was bootstrap.py run?")
        return

    scanned = 0
    for lib in libs:
        # ErsatzTV API v2 endpoint for manual scan
        url = f"{host}/api/libraries/{lib['Id']}/scan"
        code, _ = http_post(url, {})
        if code in (200, 202, 204):
            scanned += 1
            info(f"  Scan queued: {lib['Name']} (lib {lib['Id']})")
        else:
            # Older ErsatzTV — endpoint may not exist; that's fine
            info(f"  Scan endpoint not available for {lib['Name']} (HTTP {code}) — manual scan in UI")

    if scanned:
        ok(f"Queued {scanned}/{len(libs)} library scan(s)")
    else:
        info("ErsatzTV scan endpoint not available in this version — use the UI to trigger scans")


def stage7_verify_playouts() -> bool:
    """Check each channel has an active playout with items."""
    banner(7, "Verify playout health")

    con = open_db()
    cur = con.cursor()

    channels = cur.execute(
        "SELECT Id, Name, Number FROM Channel ORDER BY Number"
    ).fetchall()

    if not channels:
        warn("No channels found — run full_setup.py")
        con.close()
        return False

    issues: list[str] = []
    for ch in channels:
        playout = cur.execute(
            "SELECT Id FROM Playout WHERE ChannelId = ?", (ch["Id"],)
        ).fetchone()

        if not playout:
            issues.append(f"  Ch {ch['Number']:>3}  {ch['Name']}  → NO PLAYOUT")
            continue

        item_count = cur.execute(
            "SELECT COUNT(*) FROM PlayoutItem WHERE PlayoutId = ?",
            (playout["Id"],),
        ).fetchone()[0]

        if item_count == 0:
            issues.append(f"  Ch {ch['Number']:>3}  {ch['Name']}  → playout exists but 0 items")

    con.close()

    if issues:
        warn(f"{len(issues)} channel(s) have playout issues:")
        for issue in issues:
            print(f"  {YELLOW}{issue}{RESET}")
        info("Run full_setup.py to rebuild affected channels")
        return False
    else:
        ok(f"All {len(channels)} channel(s) have active playouts")
        return True


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

ALL_STAGES = [1, 2, 3, 4, 5, 6, 7]


def main() -> int:
    global DRY_RUN

    parser = argparse.ArgumentParser(
        description="stationmaster-pi maintenance pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--stage", type=str, default="",
        help="Comma-separated list of stages to run (default: all). E.g. --stage 1,3,4",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be changed without modifying the DB")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()

    DRY_RUN = args.dry_run

    # Parse stages
    if args.stage:
        try:
            stages = [int(s.strip()) for s in args.stage.split(",")]
        except ValueError:
            print("Invalid --stage value. Use comma-separated integers e.g. --stage 1,3,4")
            return 1
    else:
        stages = ALL_STAGES

    print()
    print(f"{BOLD}{'━' * 56}{RESET}")
    print(f"  {BOLD}stationmaster-pi  ·  Full Cleanup{RESET}")
    if DRY_RUN:
        print(f"  {YELLOW}DRY-RUN mode — no changes will be made{RESET}")
    print(f"{BOLD}{'━' * 56}{RESET}")
    print()
    print(f"  Running stages: {stages}")
    print()

    if not ETV_DB_PATH.exists():
        err(f"ErsatzTV DB not found at {ETV_DB_PATH}")
        err("Run bootstrap.py first.")
        return 1

    if not args.yes and not DRY_RUN:
        try:
            answer = input(f"  {BOLD}Proceed? [y/N]{RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            info("Aborted.")
            return 0
        if answer not in ("y", "yes"):
            info("Aborted.")
            return 0

    env = load_env()
    start = time.time()
    all_ok = True

    if 1 in stages:
        stage1_purge_stale_media(env)

    if 2 in stages:
        stage2_dedup_media()

    if 3 in stages:
        stage3_orphan_playout_items()

    if 4 in stages:
        stage4_vacuum_db()

    if 5 in stages:
        stage5_jellyfin_refresh(env)

    if 6 in stages:
        stage6_etv_rescan(env)

    if 7 in stages:
        all_ok = stage7_verify_playouts() and all_ok

    elapsed = time.time() - start
    print()
    print(f"{BOLD}{'━' * 56}{RESET}")
    status  = f"{GREEN}Complete{RESET}" if all_ok else f"{YELLOW}Complete with warnings{RESET}"
    print(f"  {BOLD}{status}{RESET}  ({elapsed:.1f}s)")
    print(f"{BOLD}{'━' * 56}{RESET}")
    print()

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
