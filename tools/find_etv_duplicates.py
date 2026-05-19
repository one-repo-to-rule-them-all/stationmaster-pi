#!/usr/bin/env python3
"""
find_etv_duplicates.py — Find duplicate media entries in ErsatzTV DB
====================================================================
Looks for MediaItems with identical file paths, or movies/shows with
the same title appearing more than once. Outputs a report and optionally
removes the duplicates, keeping the oldest record (lowest Id).

Usage:
    python3 tools/find_etv_duplicates.py
    python3 tools/find_etv_duplicates.py --fix    # remove duplicates (keeps oldest)
    python3 tools/find_etv_duplicates.py --movies-only
    python3 tools/find_etv_duplicates.py --shows-only
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


def find_path_dups(cur: sqlite3.Cursor) -> list:
    """MediaFiles sharing an identical path."""
    return cur.execute("""
        SELECT Path, COUNT(*) AS cnt, GROUP_CONCAT(MediaItemId) AS ids
        FROM MediaFile
        GROUP BY Path
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
    """).fetchall()


def find_movie_title_dups(cur: sqlite3.Cursor) -> list:
    """Movies sharing the same title+year."""
    return cur.execute("""
        SELECT mm.Title, mm.Year, COUNT(*) AS cnt,
               GROUP_CONCAT(mm.MovieId) AS movie_ids
        FROM MovieMetadata mm
        GROUP BY mm.Title, mm.Year
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
    """).fetchall()


def find_show_title_dups(cur: sqlite3.Cursor) -> list:
    """Shows sharing the same title."""
    return cur.execute("""
        SELECT sm.Title, sm.Year, COUNT(*) AS cnt,
               GROUP_CONCAT(sm.ShowId) AS show_ids
        FROM ShowMetadata sm
        GROUP BY sm.Title
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
    """).fetchall()


def remove_path_dups(con: sqlite3.Connection, dups: list) -> int:
    """Keep lowest-Id MediaItem per path, delete the rest."""
    cur = con.cursor()
    total = 0
    for dup in dups:
        ids = [int(i) for i in dup["ids"].split(",")]
        keep = min(ids)
        remove = [i for i in ids if i != keep]
        cur.execute(
            f"DELETE FROM MediaFile WHERE MediaItemId IN ({','.join('?'*len(remove))})",
            remove,
        )
        cur.execute(
            f"DELETE FROM MediaItem WHERE Id IN ({','.join('?'*len(remove))})",
            remove,
        )
        cur.execute(
            f"DELETE FROM CollectionItem WHERE MediaItemId IN ({','.join('?'*len(remove))})",
            remove,
        )
        total += len(remove)
    con.commit()
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Find (and optionally fix) ErsatzTV duplicates")
    parser.add_argument("--fix", action="store_true",
                        help="Remove duplicate entries (keeps lowest Id)")
    parser.add_argument("--movies-only", action="store_true")
    parser.add_argument("--shows-only",  action="store_true")
    args = parser.parse_args()

    con = open_db()
    cur = con.cursor()

    total_issues = 0

    # ── Path duplicates (most reliable signal) ────────────────────────────────
    if not args.movies_only and not args.shows_only:
        path_dups = find_path_dups(cur)
        if path_dups:
            print(f"\n{BOLD}Duplicate file paths ({len(path_dups)} groups):{RESET}")
            for d in path_dups:
                print(f"  {YELLOW}×{d['cnt']}{RESET}  {Path(d['Path']).name}")
                print(f"       MediaItemIds: {d['ids']}")
            total_issues += len(path_dups)

            if args.fix:
                removed = remove_path_dups(con, path_dups)
                print(f"  {GREEN}Removed {removed} duplicate MediaItem(s){RESET}")
        else:
            print(f"\n{GREEN}No duplicate file paths found{RESET}")

    # ── Movie title duplicates ────────────────────────────────────────────────
    if not args.shows_only:
        movie_dups = find_movie_title_dups(cur)
        if movie_dups:
            print(f"\n{BOLD}Duplicate movie titles ({len(movie_dups)} groups):{RESET}")
            for d in movie_dups:
                year = f" ({d['Year']})" if d["Year"] else ""
                print(f"  {YELLOW}×{d['cnt']}{RESET}  {d['Title']}{year}  IDs={d['movie_ids']}")
            total_issues += len(movie_dups)
            if args.fix:
                print(f"  {YELLOW}Movie title dedup is not auto-fixed — use full_cleanup.py --stage 2{RESET}")
        else:
            print(f"\n{GREEN}No duplicate movie titles found{RESET}")

    # ── Show title duplicates ─────────────────────────────────────────────────
    if not args.movies_only:
        show_dups = find_show_title_dups(cur)
        if show_dups:
            print(f"\n{BOLD}Duplicate show titles ({len(show_dups)} groups):{RESET}")
            for d in show_dups:
                year = f" ({d['Year']})" if d["Year"] else ""
                print(f"  {YELLOW}×{d['cnt']}{RESET}  {d['Title']}{year}  IDs={d['show_ids']}")
            total_issues += len(show_dups)
        else:
            print(f"\n{GREEN}No duplicate show titles found{RESET}")

    con.close()

    print()
    if total_issues:
        action = "Fixed" if args.fix else "Found"
        print(f"  {YELLOW}{action} {total_issues} duplicate group(s).{RESET}")
        if not args.fix:
            print(f"  Run with --fix to remove path duplicates automatically.")
    else:
        print(f"  {GREEN}No duplicates found — DB looks clean.{RESET}")
    print()

    return 0 if total_issues == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
