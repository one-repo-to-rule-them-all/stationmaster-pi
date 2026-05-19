#!/usr/bin/env python3
"""
audit_movies.py — Audit movie coverage across channels
=======================================================
Compares movies on the NAS against what ErsatzTV has indexed and which
channels they appear on. Helps identify movies that didn't get picked up,
movies not assigned to any channel, and how Kids vs Adult splits look.

Usage:
    python3 tools/audit_movies.py
    python3 tools/audit_movies.py --missing         # only show unindexed NAS files
    python3 tools/audit_movies.py --unassigned      # only show indexed but channelless movies
    python3 tools/audit_movies.py --kids / --adult  # filter by library type
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT  = SCRIPT_DIR.parent
ENV_FILE   = REPO_ROOT / ".env"

ETV_DB_PATH = Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}

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


def open_db() -> sqlite3.Connection:
    if not ETV_DB_PATH.exists():
        print(f"DB not found at {ETV_DB_PATH}", file=sys.stderr)
        sys.exit(1)
    con = sqlite3.connect(str(ETV_DB_PATH), timeout=5)
    con.row_factory = sqlite3.Row
    return con


def scan_nas_movies(env: dict, category: str) -> list[Path]:
    """Return all video files under the specified NAS movies directory."""
    mount  = env.get("NAS_MOUNT_POINT", "/mnt/nas")
    if category == "kids":
        subdir = env.get("NAS_MOVIES_KIDS_DIR", "Videos/Movies/Kids")
    else:
        subdir = env.get("NAS_MOVIES_ADULT_DIR", "Videos/Movies/Adult")

    base = Path(mount) / subdir
    if not base.exists():
        print(f"  {YELLOW}NAS path not found: {base}{RESET}")
        return []

    files = []
    for root, _, filenames in os.walk(base):
        for fn in filenames:
            p = Path(root) / fn
            if p.suffix.lower() in VIDEO_EXTS:
                files.append(p)
    return sorted(files)


def get_etv_movies(cur: sqlite3.Cursor) -> dict[str, sqlite3.Row]:
    """All movies indexed in ETV, keyed by file path."""
    rows = cur.execute("""
        SELECT mf.Path, mm.Title, mm.Year, mi.Id AS MediaItemId
        FROM MediaFile mf
        JOIN MediaItem mi ON mi.Id = mf.MediaItemId
        JOIN MovieMetadata mm ON mm.MovieId = mi.Id
    """).fetchall()
    return {row["Path"]: row for row in rows}


def get_movie_channels(cur: sqlite3.Cursor) -> dict[int, list[str]]:
    """Map MediaItemId → list of channel names."""
    rows = cur.execute("""
        SELECT ci.MediaItemId,
               c.Number, c.Name
        FROM CollectionItem ci
        JOIN Collection col ON col.Id = ci.CollectionId
        JOIN ProgramScheduleItem psi ON psi.CollectionId = col.Id
        JOIN ProgramSchedule ps ON ps.Id = psi.ProgramScheduleId
        JOIN Playout p ON p.ProgramScheduleId = ps.Id
        JOIN Channel c ON c.Id = p.ChannelId
        WHERE ci.MediaItemId IS NOT NULL
    """).fetchall()

    result: dict[int, list[str]] = {}
    for r in rows:
        mid = r["MediaItemId"]
        label = f"Ch {r['Number']} ({r['Name']})"
        result.setdefault(mid, [])
        if label not in result[mid]:
            result[mid].append(label)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit movie coverage")
    parser.add_argument("--missing",    action="store_true", help="Show NAS files not in ETV")
    parser.add_argument("--unassigned", action="store_true", help="Show ETV movies not in any channel")
    parser.add_argument("--kids",       action="store_true", help="Kids movies only")
    parser.add_argument("--adult",      action="store_true", help="Adult movies only")
    args = parser.parse_args()

    env = load_env()
    con = open_db()
    cur = con.cursor()

    etv_movies   = get_etv_movies(cur)
    ch_map       = get_movie_channels(cur)
    etv_paths    = set(etv_movies.keys())

    categories = []
    if args.kids and not args.adult:
        categories = ["kids"]
    elif args.adult and not args.kids:
        categories = ["adult"]
    else:
        categories = ["kids", "adult"]

    total_nas   = 0
    total_miss  = 0
    total_unasg = 0

    for cat in categories:
        nas_files = scan_nas_movies(env, cat)
        total_nas += len(nas_files)

        print(f"\n{BOLD}{'Kids' if cat == 'kids' else 'Adult'} Movies — NAS: {len(nas_files)} files{RESET}")
        print("─" * 60)

        missing    = []
        unassigned = []
        assigned   = []

        for fpath in nas_files:
            # ETV may store paths with different mount roots — match on filename+parent
            match = None
            for ep, row in etv_movies.items():
                if Path(ep).name == fpath.name:
                    match = row
                    break

            if not match:
                missing.append(fpath)
                total_miss += 1
            else:
                channels = ch_map.get(match["MediaItemId"], [])
                if not channels:
                    unassigned.append((fpath, match))
                    total_unasg += 1
                else:
                    assigned.append((fpath, match, channels))

        # ── Missing from ETV ──────────────────────────────────────────────────
        if missing and (args.missing or not args.unassigned):
            print(f"\n  {RED}Not indexed by ErsatzTV ({len(missing)}):{RESET}")
            for f in missing:
                print(f"    {f.name}")

        # ── In ETV but no channel ─────────────────────────────────────────────
        if unassigned and (args.unassigned or not args.missing):
            print(f"\n  {YELLOW}Indexed but not in any channel ({len(unassigned)}):{RESET}")
            for f, row in unassigned:
                print(f"    {row['Title']} ({row['Year']}) — {f.name}")

        # ── Summary ───────────────────────────────────────────────────────────
        if not args.missing and not args.unassigned:
            print(f"\n  {GREEN}Assigned to channels ({len(assigned)}):{RESET}")
            for f, row, channels in assigned[:30]:
                print(f"    {row['Title']:<40}  {', '.join(channels)}")
            if len(assigned) > 30:
                print(f"    ... and {len(assigned) - 30} more")

    con.close()

    print()
    print(f"{BOLD}{'━' * 60}{RESET}")
    print(f"  NAS files scanned : {total_nas}")
    print(f"  Missing from ETV  : {RED}{total_miss}{RESET}")
    print(f"  Not in any channel: {YELLOW}{total_unasg}{RESET}")
    print(f"  Coverage          : {GREEN}{total_nas - total_miss - total_unasg}/{total_nas}{RESET}")
    print(f"{BOLD}{'━' * 60}{RESET}")
    print()

    return 0 if (total_miss == 0 and total_unasg == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
