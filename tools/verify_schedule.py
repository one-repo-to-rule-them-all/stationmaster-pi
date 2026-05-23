#!/usr/bin/env python3
"""
verify_schedule.py — Sanity-check ErsatzTV schedule/playout state
==================================================================
Prints a table of every channel with its schedule kind, item count,
playout anchor time, and whether the playout is stale.

Usage:
    python3 tools/verify_schedule.py
    python3 tools/verify_schedule.py --channel 2   # check a specific channel
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ETV_DB_PATH = Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"

PLAYBACK_ORDER = {0: "Standard", 1: "Shuffle", 2: "ShufInOrder", 3: "Chrono"}
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify ErsatzTV schedule health")
    parser.add_argument("--channel", type=int, default=None,
                        help="Only check this channel number")
    args = parser.parse_args()

    if not ETV_DB_PATH.exists():
        print(f"DB not found at {ETV_DB_PATH}", file=sys.stderr)
        return 1

    con = sqlite3.connect(str(ETV_DB_PATH), timeout=5)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    where = "WHERE c.Number = ?" if args.channel else ""
    params = (args.channel,) if args.channel else ()

    channels = cur.execute(f"""
        SELECT c.Id, c.Number, c.Name,
               MAX(psi.PlaybackOrder)  AS PlaybackOrder,
               COUNT(DISTINCT psi.Id)  AS sched_items,
               p.Id                    AS playout_id,
               COUNT(DISTINCT pi.Id)   AS playout_items,
               pa.NextStart            AS anchor_time
        FROM Channel c
        LEFT JOIN Playout p             ON p.ChannelId          = c.Id
        LEFT JOIN ProgramSchedule ps    ON ps.Id                = p.ProgramScheduleId
        LEFT JOIN ProgramScheduleItem psi ON psi.ProgramScheduleId = ps.Id
        LEFT JOIN PlayoutAnchor pa      ON pa.PlayoutId         = p.Id
        LEFT JOIN PlayoutItem pi        ON pi.PlayoutId         = p.Id
        {where}
        GROUP BY c.Id
        ORDER BY c.Number
    """, params).fetchall()

    con.close()

    if not channels:
        msg = f"Channel {args.channel} not found" if args.channel else "No channels in DB"
        print(f"  {YELLOW}{msg}{RESET}")
        return 0

    now = datetime.now(timezone.utc)

    print()
    print(f"{BOLD}{'Ch':>4}  {'Name':<30}  {'Kind':<8}  {'Sched':>5}  {'Items':>5}  {'Anchor':<22}  Status{RESET}")
    print("─" * 90)

    issues = 0
    for ch in channels:
        kind      = PLAYBACK_ORDER.get(ch["PlaybackOrder"] or 0, "?")
        pi_count  = ch["playout_items"] or 0
        anchor    = ch["anchor_time"] or ""
        sched_cnt = ch["sched_items"] or 0

        # Status
        flags = []
        if not ch["playout_id"]:
            flags.append("NO PLAYOUT")
        if pi_count == 0:
            flags.append("0 ITEMS")
        if sched_cnt == 0:
            flags.append("NO SCHEDULE")
        if ch["PlaybackOrder"] not in (0, 1, 2, 3):
            flags.append(f"BAD ORDER ({ch['PlaybackOrder']})")

        if anchor:
            try:
                # ErsatzTV stores UTC ISO strings
                dt = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
                stale_hours = (now - dt).total_seconds() / 3600
                if stale_hours > 48:
                    flags.append(f"ANCHOR STALE {stale_hours:.0f}h")
                anchor = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        if flags:
            status = f"{RED}{', '.join(flags)}{RESET}"
            issues += 1
        else:
            status = f"{GREEN}OK{RESET}"

        print(
            f"  {ch['Number']:>3}  {ch['Name']:<30}  {kind:<8}  "
            f"{sched_cnt:>5}  {pi_count:>5}  {anchor:<22}  {status}"
        )

    print()
    if issues:
        print(f"  {YELLOW}{issues} channel(s) with issues — run full_setup.py to rebuild{RESET}")
    else:
        print(f"  {GREEN}All {len(channels)} channel(s) look healthy{RESET}")
    print()

    return 0 if issues == 0 else 1



if __name__ == "__main__":
    sys.exit(main())
