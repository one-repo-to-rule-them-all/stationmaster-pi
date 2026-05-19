#!/usr/bin/env python3
"""
delete_channels.py — Remove specific channels from ErsatzTV DB
==============================================================
Deletes Channel rows and all associated Playout, PlayoutItem,
ProgramSchedule, and Collection data for the specified channels.
Media library data is untouched.

Use this when you want to remove a specific channel without doing a
full factory reset. full_setup.py can re-create them afterward.

Usage:
    python3 tools/delete_channels.py --channel 79 80
    python3 tools/delete_channels.py --channel 79 80 --yes
    python3 tools/delete_channels.py --list     # show all channels, then exit
"""

import argparse
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ETV_DB_PATH = Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"

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


def list_channels(cur: sqlite3.Cursor) -> None:
    channels = cur.execute(
        "SELECT Number, Name, Id FROM Channel ORDER BY Number"
    ).fetchall()
    if not channels:
        print("  No channels in DB")
        return
    print(f"\n{BOLD}{'Ch':>4}  {'Name':<40}  DB Id{RESET}")
    print("─" * 55)
    for ch in channels:
        print(f"  {ch['Number']:>3}  {ch['Name']:<40}  {ch['Id']}")
    print()


def stop_etv() -> None:
    r = subprocess.run(["systemctl", "is-active", "--quiet", "ersatztv"], capture_output=True)
    if r.returncode == 0:
        subprocess.run(["sudo", "systemctl", "stop", "ersatztv"])
        print("  Stopped ersatztv.service")
    else:
        subprocess.run(["pkill", "-f", "ErsatzTV"], capture_output=True)
        time.sleep(2)


def start_etv() -> None:
    r = subprocess.run(
        ["systemctl", "list-unit-files", "ersatztv.service"],
        capture_output=True, text=True,
    )
    if "ersatztv.service" in r.stdout:
        subprocess.run(["sudo", "systemctl", "start", "ersatztv"])
        print("  Started ersatztv.service")
    else:
        print("  Start ErsatzTV manually — systemd unit not installed")


def delete_channel(con: sqlite3.Connection, number: int) -> bool:
    cur = con.cursor()
    ch = cur.execute(
        "SELECT Id, Name FROM Channel WHERE Number = ?", (number,)
    ).fetchone()
    if not ch:
        print(f"  {YELLOW}Channel {number} not found in DB — skipping{RESET}")
        return False

    ch_id   = ch["Id"]
    ch_name = ch["Name"]

    # 1. Playouts
    playouts = cur.execute(
        "SELECT Id FROM Playout WHERE ChannelId = ?", (ch_id,)
    ).fetchall()
    for p in playouts:
        pid = p["Id"]
        cur.execute("DELETE FROM PlayoutItem WHERE PlayoutId = ?", (pid,))
        cur.execute("DELETE FROM PlayoutProgramScheduleAnchor WHERE PlayoutId = ?", (pid,))
        cur.execute("DELETE FROM PlayoutAnchor WHERE PlayoutId = ?", (pid,))
        cur.execute("DELETE FROM Playout WHERE Id = ?", (pid,))

    # 2. ProgramSchedule (linked via Channel name convention in full_setup.py)
    sched = cur.execute(
        "SELECT Id FROM ProgramSchedule WHERE Name LIKE ?", (f"%Ch {number}%",)
    ).fetchall()
    for s in sched:
        sid = s["Id"]
        cur.execute("DELETE FROM ProgramScheduleFloodItem WHERE ProgramScheduleId = ?", (sid,))
        cur.execute("DELETE FROM ProgramScheduleItem WHERE ProgramScheduleId = ?", (sid,))
        cur.execute("DELETE FROM ProgramSchedule WHERE Id = ?", (sid,))

    # 3. Collections (linked via Channel name)
    colls = cur.execute(
        "SELECT Id FROM Collection WHERE Name LIKE ?", (f"%Ch {number}%",)
    ).fetchall()
    for c in colls:
        cur.execute("DELETE FROM CollectionItem WHERE CollectionId = ?", (c["Id"],))
        cur.execute("DELETE FROM Collection WHERE Id = ?", (c["Id"],))

    # 4. Channel itself
    cur.execute("DELETE FROM Channel WHERE Id = ?", (ch_id,))

    con.commit()
    print(f"  {GREEN}Deleted Ch {number}: {ch_name}{RESET}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete specific ErsatzTV channels")
    parser.add_argument("--channel", "-c", type=int, nargs="+",
                        help="Channel numbers to delete")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List all channels and exit")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation")
    args = parser.parse_args()

    con = open_db()
    cur = con.cursor()

    if args.list or not args.channel:
        list_channels(cur)
        con.close()
        return 0

    print()
    print(f"  Will delete channels: {args.channel}")
    print(f"  {YELLOW}Media library data is untouched.{RESET}")

    if not args.yes:
        try:
            answer = input(f"  {BOLD}Proceed? [y/N]{RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            con.close()
            return 0
        if answer not in ("y", "yes"):
            con.close()
            return 0

    print()
    stop_etv()

    deleted = 0
    for num in args.channel:
        if delete_channel(con, num):
            deleted += 1

    con.close()
    start_etv()

    print()
    print(f"  {GREEN}Done. Deleted {deleted}/{len(args.channel)} channel(s).{RESET}")
    print("  Run full_setup.py to rebuild any deleted channels.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
