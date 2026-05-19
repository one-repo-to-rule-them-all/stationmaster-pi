#!/usr/bin/env python3
"""
diagnose_show.py — Deep-inspect a specific show in ErsatzTV
===========================================================
Shows how many seasons/episodes ErsatzTV has indexed, whether they
appear in any channel collections, and what order they'll play in.

Usage:
    python3 tools/diagnose_show.py "Futurama"
    python3 tools/diagnose_show.py "futurama" --fuzzy   # case-insensitive partial match
"""

import argparse
import sqlite3
import sys
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
    con = sqlite3.connect(str(ETV_DB_PATH), timeout=5)
    con.row_factory = sqlite3.Row
    return con


def find_shows(cur: sqlite3.Cursor, query: str, fuzzy: bool) -> list:
    if fuzzy:
        return cur.execute(
            "SELECT Id, Title, Year FROM ShowMetadata WHERE Title LIKE ? ORDER BY Title",
            (f"%{query}%",),
        ).fetchall()
    else:
        return cur.execute(
            "SELECT Id, Title, Year FROM ShowMetadata WHERE LOWER(Title) = LOWER(?)",
            (query,),
        ).fetchall()


def inspect_show(cur: sqlite3.Cursor, show: sqlite3.Row) -> None:
    show_id   = show["Id"]
    show_name = show["Title"]
    year      = f" ({show['Year']})" if show["Year"] else ""
    print(f"\n{BOLD}Show: {show_name}{year}{RESET}  (ShowId={show_id})")
    print("─" * 60)

    # Seasons
    seasons = cur.execute("""
        SELECT ss.Id, ss.SeasonNumber,
               COUNT(em.Id) AS episode_count
        FROM Season ss
        LEFT JOIN Episode ep ON ep.SeasonId = ss.Id
        LEFT JOIN EpisodeMetadata em ON em.EpisodeId = ep.Id
        WHERE ss.ShowId = ?
        GROUP BY ss.Id
        ORDER BY ss.SeasonNumber
    """, (show_id,)).fetchall()

    total_eps = sum(s["episode_count"] for s in seasons)
    print(f"  Seasons: {len(seasons)}   Total episodes: {total_eps}")

    for s in seasons:
        eps_label = f"{s['episode_count']} ep(s)"
        print(f"    S{s['SeasonNumber']:02d}  {eps_label}")

    if not seasons:
        print(f"  {YELLOW}No seasons found — show may not have been fully scanned{RESET}")
        return

    # Sample episode order (first 20)
    episodes = cur.execute("""
        SELECT em.Title AS ep_title, em.EpisodeNumber, ss.SeasonNumber,
               mf.Path
        FROM Episode ep
        JOIN EpisodeMetadata em ON em.EpisodeId = ep.Id
        JOIN Season ss ON ss.Id = ep.SeasonId
        LEFT JOIN MediaFile mf ON mf.MediaItemId = ep.Id
        WHERE ss.ShowId = ?
        ORDER BY ss.SeasonNumber, em.EpisodeNumber
        LIMIT 20
    """, (show_id,)).fetchall()

    print(f"\n  Play order (first {len(episodes)} episodes, sequential):")
    for ep in episodes:
        fname = Path(ep["Path"]).name if ep["Path"] else "?"
        print(
            f"    S{ep['SeasonNumber']:02d}E{ep['EpisodeNumber']:02d}  "
            f"{ep['ep_title'] or '(no title)'}  — {fname}"
        )

    # Check which channels include this show
    channels = cur.execute("""
        SELECT DISTINCT c.Number, c.Name
        FROM Channel c
        JOIN Playout p ON p.ChannelId = c.Id
        JOIN ProgramSchedule ps ON ps.Id = p.ProgramScheduleId
        JOIN ProgramScheduleItem psi ON psi.ProgramScheduleId = ps.Id
        JOIN CollectionItem ci ON ci.CollectionId = psi.CollectionId
        JOIN Episode ep ON ep.Id = ci.MediaItemId
        JOIN Season ss ON ss.Id = ep.SeasonId
        WHERE ss.ShowId = ?
        ORDER BY c.Number
    """, (show_id,)).fetchall()

    if channels:
        names = ", ".join(f"Ch {c['Number']} ({c['Name']})" for c in channels)
        print(f"\n  {GREEN}Present in: {names}{RESET}")
    else:
        print(f"\n  {YELLOW}Not assigned to any channel collection{RESET}")
        print("  → Check CHANNEL_DEFS in full_setup.py for matching patterns")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose a show in ErsatzTV")
    parser.add_argument("show", nargs="?", help="Show title to inspect")
    parser.add_argument("--fuzzy", action="store_true",
                        help="Partial/case-insensitive match")
    args = parser.parse_args()

    if not args.show:
        parser.print_help()
        return 0

    con = open_db()
    cur = con.cursor()

    shows = find_shows(cur, args.show, args.fuzzy)

    if not shows:
        print(f"\n  {YELLOW}No show matching '{args.show}' found in ErsatzTV DB{RESET}")
        print("  Try --fuzzy for a partial match, or check ErsatzTV → Library for scan status")
        con.close()
        return 1

    if len(shows) > 1:
        print(f"\n  Found {len(shows)} matching shows:")
        for s in shows:
            print(f"    {s['Title']} ({s['Year']})  Id={s['Id']}")
        print("  Inspecting all matches...\n")

    for show in shows:
        inspect_show(cur, show)

    con.close()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
