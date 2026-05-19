#!/usr/bin/env python3
"""
factory_reset.py — stationmaster-pi clean-slate channel rebuild
===============================================================
Wipes ALL ErsatzTV channels, schedules, and playouts from the SQLite DB,
then re-runs full_setup.py to rebuild everything from scratch.

Use this when:
  - Channels are in a broken state that full_setup.py can't fix
  - You want to renumber channels cleanly
  - The playout anchor table is corrupted
  - You changed CHANNEL_DEFS in full_setup.py and need a fresh slate

WHAT IT DELETES (ErsatzTV tables only — media library data is untouched):
  Channel, ProgramSchedule, ProgramScheduleItem, ProgramScheduleFloodItem,
  Playout, PlayoutAnchor, PlayoutItem, PlayoutProgramScheduleAnchor,
  CollectionItem (type=collection only), Collection

WHAT IT DOES NOT TOUCH:
  Library, LibraryPath, MediaItem, MediaFile, MovieMetadata, EpisodeMetadata,
  OtherVideoMetadata, ShowMetadata, SeasonMetadata — all your scanned media
  remains intact.

Usage:
    python3 tools/factory_reset.py
    python3 tools/factory_reset.py --yes    # skip confirmation prompt
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).resolve().parent
REPO_ROOT   = SCRIPT_DIR.parent
FULL_SETUP  = REPO_ROOT / "full_setup.py"
ETV_DB_PATH = Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"
ETV_EXE     = Path("/opt/ersatztv/ErsatzTV")

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def banner(msg: str) -> None:
    print(f"\n{BOLD}── {msg} {'─' * max(0, 52 - len(msg))}{RESET}")


def info(msg: str) -> None:
    print(f"  {msg}")


def success(msg: str) -> None:
    print(f"  {GREEN}✓{RESET}  {msg}")


def error(msg: str) -> None:
    print(f"  {RED}✗{RESET}  {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET}  {msg}")


# ── ErsatzTV process management ───────────────────────────────────────────────
def etv_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "ErsatzTV"], capture_output=True)
    return r.returncode == 0


def stop_etv() -> None:
    """Stop ErsatzTV — try systemctl first, fall back to pkill."""
    banner("Stopping ErsatzTV")
    r = subprocess.run(
        ["systemctl", "is-active", "--quiet", "ersatztv"],
        capture_output=True,
    )
    if r.returncode == 0:
        subprocess.run(["sudo", "systemctl", "stop", "ersatztv"], check=True)
        info("Stopped ersatztv.service via systemctl")
    elif etv_running():
        subprocess.run(["pkill", "-f", "ErsatzTV"], capture_output=True)
        time.sleep(3)
        if etv_running():
            subprocess.run(["pkill", "-9", "-f", "ErsatzTV"], capture_output=True)
            time.sleep(2)
        info("Stopped ErsatzTV process via pkill")
    else:
        info("ErsatzTV was not running")


def start_etv() -> None:
    """Restart ErsatzTV — prefer systemctl."""
    banner("Restarting ErsatzTV")
    r = subprocess.run(
        ["systemctl", "list-unit-files", "ersatztv.service"],
        capture_output=True, text=True,
    )
    if "ersatztv.service" in r.stdout:
        subprocess.run(["sudo", "systemctl", "start", "ersatztv"])
        info("Started ersatztv.service via systemctl")
    elif ETV_EXE.exists():
        subprocess.Popen(
            [str(ETV_EXE)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        info(f"Started {ETV_EXE} in background")
    else:
        warn("Could not start ErsatzTV — start it manually")


# ── DB wipe ───────────────────────────────────────────────────────────────────
# Tables to truncate, in dependency order (children before parents)
WIPE_TABLES = [
    "PlayoutItem",
    "PlayoutProgramScheduleAnchor",
    "PlayoutAnchor",
    "Playout",
    "ProgramScheduleFloodItem",
    "ProgramScheduleItem",
    "ProgramSchedule",
    "CollectionItem",
    "Collection",
    "Channel",
]


def backup_db() -> Path:
    """Create a timestamped backup of the ErsatzTV DB before wiping."""
    ts  = time.strftime("%Y%m%d_%H%M%S")
    bak = ETV_DB_PATH.parent / f"ersatztv_backup_{ts}.sqlite3"
    shutil.copy2(str(ETV_DB_PATH), str(bak))
    return bak


def wipe_db() -> dict[str, int]:
    """
    Delete channel/schedule/playout data.
    Returns a dict of {table: rows_deleted}.
    """
    con = sqlite3.connect(str(ETV_DB_PATH), timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.cursor()
    deleted: dict[str, int] = {}

    for table in WIPE_TABLES:
        try:
            # Check if table exists
            exists = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                deleted[table] = 0
                continue

            if table == "CollectionItem":
                # Only delete items where the parent Collection isn't a system collection
                # (ErsatzTV may have internal "Favorites" etc. we don't want to nuke)
                cur.execute("DELETE FROM CollectionItem")
            else:
                cur.execute(f"DELETE FROM {table}")  # noqa: S608

            deleted[table] = cur.rowcount
        except sqlite3.OperationalError as e:
            warn(f"  Could not delete from {table}: {e}")
            deleted[table] = -1

    con.commit()
    con.close()
    return deleted


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="stationmaster-pi factory reset — wipe channels and rebuild",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()

    print()
    print(f"{BOLD}{'━' * 56}{RESET}")
    print(f"  {BOLD}stationmaster-pi  ·  Factory Reset{RESET}")
    print(f"{BOLD}{'━' * 56}{RESET}")
    print()
    print("  This will:")
    print("    1. Stop ErsatzTV")
    print("    2. Back up the current DB")
    print("    3. Wipe all channels, schedules, and playouts")
    print("    4. Restart ErsatzTV")
    print("    5. Run full_setup.py to rebuild everything")
    print()
    print(f"  {YELLOW}Media library data (scanned files) is NOT touched.{RESET}")
    print()

    if not ETV_DB_PATH.exists():
        error(f"ErsatzTV DB not found at {ETV_DB_PATH}")
        error("Run bootstrap.py first to initialise the stack.")
        return 1

    if not FULL_SETUP.exists():
        error(f"full_setup.py not found at {FULL_SETUP}")
        return 1

    # Confirm
    if not args.yes:
        try:
            answer = input(f"  {BOLD}Proceed? [y/N]{RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            info("Aborted.")
            return 0
        if answer not in ("y", "yes"):
            info("Aborted.")
            return 0

    print()

    # Step 1: stop ErsatzTV
    stop_etv()

    # Step 2: backup
    banner("Backing up DB")
    try:
        bak = backup_db()
        success(f"Backup → {bak}")
    except Exception as e:
        error(f"Backup failed: {e}")
        error("Aborting to protect your data. Check disk space.")
        start_etv()
        return 1

    # Step 3: wipe
    banner("Wiping Channel / Schedule / Playout data")
    try:
        deleted = wipe_db()
        for table, count in deleted.items():
            if count > 0:
                success(f"Deleted {count:>4} row(s) from {table}")
            elif count == 0:
                info(f"  (already empty)  {table}")
    except Exception as e:
        error(f"DB wipe failed: {e}")
        error(f"Restore from backup: cp {bak} {ETV_DB_PATH}")
        start_etv()
        return 1

    # Step 4: restart ErsatzTV
    start_etv()
    info("Waiting 15s for ErsatzTV to initialise...")
    time.sleep(15)

    # Step 5: run full_setup.py
    banner("Rebuilding channels via full_setup.py")
    result = subprocess.run(
        [sys.executable, str(FULL_SETUP)],
        cwd=str(REPO_ROOT),
    )

    print()
    print(f"{BOLD}{'━' * 56}{RESET}")
    if result.returncode == 0:
        print(f"  {GREEN}{BOLD}Factory reset complete.{RESET}")
        print(f"  Backup saved at: {bak}")
    else:
        print(f"  {YELLOW}full_setup.py exited with code {result.returncode}.{RESET}")
        print(f"  Channels may be partially built. Backup: {bak}")
    print(f"{BOLD}{'━' * 56}{RESET}")
    print()

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
