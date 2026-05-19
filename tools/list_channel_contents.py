#!/usr/bin/env python3
"""
list_channel_contents.py — Show what's in each ErsatzTV channel collection
===========================================================================
Prints all MediaItems assigned to a channel's collection(s), useful for
debugging why a channel plays unexpected content or misses expected titles.

Usage:
    python3 tools/list_channel_contents.py --channel 2
    python3 tools/list_channel_contents.py --channel 2 --limit 50
    python3 tools/list_channel_contents.py --all          # summary of all channels
"""

import argparse
import sqlite3
import sys
from pathlib import Path

ETV_DB_PATH = Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RESET  = "\033[0m"


def open_db() -> sqlite3.Connection:
    if not ETV_DB_PATH.exists():
        print(f"DB not found at {ETV_DB_PATH}", file=sys.stderr)
        sys.exit(1)
    con = sqlite3.connect(str(ETV_DB_PATH), timeout=5)
    con.row_factory = sqlite3.Row
    return con


def list_channel(cur: sqlite3.Cursor, number: int, limit: int) -> None:
    ch = cur.execute(
        "SELECT Id, Name, Number FROM Channel WHERE Number = ?", (number,)
    ).fetchone()
    if not ch:
        print(f"  Channel {number} not found")
        return

    print(f"\n{BOLD}Channel {ch['Number']}: {ch['Name']}{RESET}")
    print("─" * 60)

    # Find collections attached to this channel's schedule
    playouts = cur.execute(
        "SELECT Id FROM Playout WHERE ChannelId = ?", (ch["Id"],)
    ).fetchall()

    if not playouts:
        print("  No playouts found for this channel")
        return

    playout_id = playouts[0]["Id"]

    # Check whether MediaItemId column exists on ProgramScheduleItem (schema varies by ETV version)
    psi_cols = {row[1] for row in cur.execute("PRAGMA table_info(ProgramScheduleItem)").fetchall()}
    psi_item_col = "psi.MediaItemId" if "MediaItemId" in psi_cols else "NULL AS MediaItemId"

    psfi_cols = {row[1] for row in cur.execute("PRAGMA table_info(ProgramScheduleFloodItem)").fetchall()}
    psfi_item_col = "psfi.MediaItemId" if "MediaItemId" in psfi_cols else "NULL AS MediaItemId"

    # Get schedule items → their collection IDs
    sched_items = cur.execute(f"""
        SELECT psi.CollectionId, psi.CollectionType, {psi_item_col}
        FROM Playout p
        JOIN ProgramSchedule ps ON ps.Id = p.ProgramScheduleId
        JOIN ProgramScheduleItem psi ON psi.ProgramScheduleId = ps.Id
        WHERE p.Id = ?
    """, (playout_id,)).fetchall()

    if not sched_items:
        # Try flood items
        sched_items = cur.execute(f"""
            SELECT psfi.CollectionId, psfi.CollectionType, {psfi_item_col}
            FROM Playout p
            JOIN ProgramSchedule ps ON ps.Id = p.ProgramScheduleId
            JOIN ProgramScheduleFloodItem psfi ON psfi.ProgramScheduleId = ps.Id
            WHERE p.Id = ?
        """, (playout_id,)).fetchall()

    if not sched_items:
        print("  No schedule items found")
        return

    seen = set()
    total = 0

    for si in sched_items:
        col_id   = si["CollectionId"]
        col_type = si["CollectionType"]
        item_id  = si["MediaItemId"]

        key = (col_id, col_type, item_id)
        if key in seen:
            continue
        seen.add(key)

        if col_id and col_type == 0:  # Regular Collection
            items = cur.execute(f"""
                SELECT mi.Id,
                       COALESCE(mm.Title, em.Title, ovm.Title, 'Unknown') AS Title,
                       COALESCE(mm.Year, sm.Year, '') AS Year,
                       mi.MediaItemState
                FROM CollectionItem ci
                JOIN MediaItem mi ON mi.Id = ci.MediaItemId
                LEFT JOIN MovieMetadata mm    ON mm.MovieId    = mi.Id
                LEFT JOIN EpisodeMetadata em  ON em.EpisodeId  = mi.Id
                LEFT JOIN OtherVideoMetadata ovm ON ovm.OtherVideoId = mi.Id
                LEFT JOIN ShowMetadata sm     ON sm.ShowId     = mi.Id
                WHERE ci.CollectionId = ?
                ORDER BY Title
                LIMIT ?
            """, (col_id, limit)).fetchall()

            col_name = cur.execute(
                "SELECT Name FROM Collection WHERE Id = ?", (col_id,)
            ).fetchone()
            col_label = col_name["Name"] if col_name else f"Collection {col_id}"

            print(f"\n  {GREEN}Collection: {col_label}{RESET}  ({len(items)} item(s))")
            for item in items:
                year = f" ({item['Year']})" if item["Year"] else ""
                print(f"    {item['Title']}{year}")
            total += len(items)

        elif item_id:
            # Single MediaItem
            item = cur.execute("""
                SELECT COALESCE(mm.Title, em.Title, ovm.Title, 'Unknown') AS Title,
                       COALESCE(mm.Year, '') AS Year
                FROM MediaItem mi
                LEFT JOIN MovieMetadata mm       ON mm.MovieId   = mi.Id
                LEFT JOIN EpisodeMetadata em     ON em.EpisodeId = mi.Id
                LEFT JOIN OtherVideoMetadata ovm ON ovm.OtherVideoId = mi.Id
                WHERE mi.Id = ?
            """, (item_id,)).fetchone()
            if item:
                year = f" ({item['Year']})" if item["Year"] else ""
                print(f"    (single) {item['Title']}{year}")
                total += 1

    print(f"\n  Total: {total} item(s)")


def all_channels_summary(cur: sqlite3.Cursor) -> None:
    channels = cur.execute(
        "SELECT Id, Number, Name FROM Channel ORDER BY Number"
    ).fetchall()

    if not channels:
        print("  No channels found")
        return

    print(f"\n{BOLD}{'Ch':>4}  {'Name':<35}  {'Items':>6}{RESET}")
    print("─" * 52)

    for ch in channels:
        # Count items across all collections for this channel
        count = cur.execute("""
            SELECT COUNT(DISTINCT ci.MediaItemId)
            FROM Playout p
            JOIN ProgramSchedule ps ON ps.Id = p.ProgramScheduleId
            JOIN ProgramScheduleItem psi ON psi.ProgramScheduleId = ps.Id
            JOIN CollectionItem ci ON ci.CollectionId = psi.CollectionId
            WHERE p.ChannelId = ?
        """, (ch["Id"],)).fetchone()[0]

        colour = YELLOW if count == 0 else ""
        print(f"  {ch['Number']:>3}  {ch['Name']:<35}  {colour}{count:>6}{RESET}")

    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="List ErsatzTV channel contents")
    parser.add_argument("--channel", "-c", type=int, default=None,
                        help="Channel number to inspect")
    parser.add_argument("--all", "-a", action="store_true",
                        help="Print a summary table of all channels")
    pars