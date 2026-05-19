#!/usr/bin/env python3
"""
fix_playout_crash.py — Recover a stuck/crashed ErsatzTV playout
================================================================
When ErsatzTV crashes mid-playout, the PlayoutAnchor can be left pointing
at a time in the past or future, causing the channel to show a black screen
or loop incorrectly. This script resets the anchor to "now" so ErsatzTV
picks up a fresh position on next startup.

If the playout is missing entirely, this script can re-trigger full_setup.py
to rebuild it.

Usage:
    python3 tools/fix_playout_crash.py --channel 2
    python3 tools/fix_playout_crash.py --all         # reset all playout anchors
    python3 tools/fix_playout_crash.py --channel 2 --rebuild   # re-run full_setup for ch 2
"""

import argparse
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ETV_DB_PATH = Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"
REPO_ROOT   = Path(__file__).resolve().parent.parent
FULL_SETUP  = REPO_ROOT / "full_setup.py"

GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def open_db() -> sqlite3.Connection:
    if not ETV_DB_PATH.exists():
        print(f"DB not found at {ETV_DB_PATH}", file=sys.stderr)
        sys.exit(1)
    con = sqlite3.connect(str(ETV_DB_PATH), timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    return con


def stop_etv() -> None:
    r = subprocess.run(["systemctl", "is-active", "--quiet", "ersatztv"], capture_output=True)
    if r.returncode == 0:
        subprocess.run(["sudo", "systemctl", "stop", "ersatztv"])
        print("  Stopped ersatztv.service")
    else:
        subprocess.run(["pkill", "-f", "ErsatzTV"], capture_output=True)
        time.sleep(2)
        print("  Stopped ErsatzTV via pkill")


def start_etv() -> None:
    r = subprocess.run(
        ["systemctl", "list-unit-files", "ersatztv.service"],
        capture_output=True, text=True,
    )
    if "ersatztv.service" in r.stdout:
        subprocess.run(["sudo", "systemctl", "start", "ersatztv"])
        print("  Started ersatztv.service")
    else:
        print("  Start ErsatzTV manually")


def reset_anchor(con: sqlite3.Connection, playout_id: int) -> bool:
    """Reset PlayoutAnchor.NextStart to UTC now."""
    cur = con.cursor()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    existing = cur.execute(
        "SELECT Id, NextStart FROM PlayoutAnchor WHERE PlayoutId = ?",
        (playout_id,),
    ).fetchone()

    if existing:
        cur.execute(
            "UPDATE PlayoutAnchor SET NextStart = ? WHERE PlayoutId = ?",
            (now_iso, playout_id),
        )
        print(f"    {GREEN}Reset anchor from {existing['NextStart']} → {now_iso}{RESET}")
    else:
        cur.execute(
            "INSERT INTO PlayoutAnchor (PlayoutId, NextStart) VALUES (?, ?)",
            (playout_id, now_iso),
        )
        print(f"    {GREEN}Created missing anchor → {now_iso}{RESET}")

    con.commit()
    return True


def fix_channel(con: sqlite3.Connection, number: int) -> bool:
    cur = con.cursor()
    ch = cur.execute(
        "SELECT Id, Name FROM Channel WHERE Number = ?", (number,)
    ).fetchone()
    if not ch:
        print(f"  {YELLOW}Channel {number} not found{RESET}")
        return False

    playout = cur.execute(
        "SELECT Id FROM Playout WHERE ChannelId = ?", (ch["Id"],)
    ).fetchone()
    if not playout:
        print(f"  {RED}Ch {number} ({ch['Name']}): no playout — run full_setup.py{RESET}")
        return False

    print(f"  Ch {number} ({ch['Name']})  playout={playout['Id']}")
    return reset_anchor(con, playout["Id"])


def fix_all(con: sqlite3.Connection) -> int:
    cur = con.cursor()
    channels = cur.execute(
        "SELECT Id, Number, Name FROM Channel ORDER BY Number"
    ).fetchall()

    fixed = 0
    for ch in channels:
        playout = cur.execute(
            "SELECT Id FROM Playout WHERE ChannelId = ?", (ch["Id"],)
        ).fetchone()
        if not playout:
            print(f"  {YELLOW}Ch {ch['Number']} ({ch['Name']}): no playout — skipping{RESET}")
            continue
        print(f"  Ch {ch['Number']} ({ch['Name']})")
        if reset_anchor(con, playout["Id"]):
            fixed += 1

    return fixed


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset crashed ErsatzTV playout anchors")
    parser.add_argument("--channel", "-c", type=int, help="Channel number to fix")
    parser.add_argument("--all", "-a", action="store_true", help="Fix all channels")
    parser.add_argument("--rebuild", action="store_true",
                        help="Re-run full_setup.py after fixing (use with --channel)")
    args = parser.parse_args()

    if not args.channel and not args.all:
        parser.print_help()
        return 0

    print()
    stop_etv()
    print("  Waiting 3s for ErsatzTV to fully stop...")
    time.sleep(3)

    con = open_db()

    if args.all:
        fixed = fix_all(con)
        con.close()
        start_etv()
        print(f"\n  {GREEN}Reset {fixed} playout anchor(s){RESET}")
    else:
        ok = fix_channel(con, args.channel)
        con.close()

        if args.rebuild and FULL_SETUP.exists():
            print(f"\n  Rebuilding channel {args.channel} via full_setup.py...")
            subprocess.run([sys.executable, str(FULL_SETUP)], cwd=str(REPO_ROOT))
        else:
            start_etv()

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
