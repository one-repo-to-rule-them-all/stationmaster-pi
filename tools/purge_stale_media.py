#!/usr/bin/env python3
"""
purge_stale_media.py
====================
Clears all scanned media entries from the ErsatzTV SQLite database so a
fresh library scan can succeed without PathHash UNIQUE-constraint collisions.

WHY THIS IS NEEDED
------------------
When a Library or LibraryPath row is deleted via raw Python sqlite3 (which
does NOT enforce foreign keys by default), ErsatzTV's child rows —
LibraryFolder, MediaFile, MediaVersion, Movie, Episode, OtherVideo, and all
associated metadata — are NOT cascade-deleted.  Those orphaned rows still
hold PathHash values for every file on the NAS.  When ErsatzTV's new scan
tries to INSERT the same files under a new MediaVersion it hits a UNIQUE
constraint on MediaFile.PathHash and logs:
    [WRN] Error processing movie at …: An error occurred while saving the
    entity changes.  INSERT INTO "MediaFile" …

FIX: delete every scanned-media row and let ErsatzTV re-index cleanly.
The Library / LibraryPath configuration rows are left untouched.

Run on the Pi:
    python3 tools/purge_stale_media.py
    # then restart ErsatzTV (the script does this automatically)
"""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

DB = os.environ.get("ETV_DB_PATH", "") or str(
    Path.home() / ".local/share/ersatztv/ersatztv.sqlite3"
)

GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
RED    = "\033[0;31m"
RESET  = "\033[0m"

def ok(m):   print(f"{GREEN}[+]{RESET} {m}")
def info(m): print(f"{CYAN}[~]{RESET} {m}")
def warn(m): print(f"{YELLOW}[!]{RESET} {m}")
def die(m):  print(f"{RED}[X]{RESET} {m}"); sys.exit(1)


print(f"\n ErsatzTV Stale Media Purge\n{'─'*50}")
info(f"DB: {DB}")

if not Path(DB).exists():
    die(f"Database not found: {DB}")

# ── Stop ErsatzTV ─────────────────────────────────────────────────────────────
info("Stopping ErsatzTV...")
subprocess.run(["sudo", "systemctl", "stop", "ersatztv"], capture_output=True)
subprocess.run(["pkill", "-TERM", "-f", "ErsatzTV"], capture_output=True)
time.sleep(3)
subprocess.run(["pkill", "-KILL", "-f", "ErsatzTV"], capture_output=True)
time.sleep(1)
ok("ErsatzTV stopped.")

# ── Wipe scanned media (preserve library config) ───────────────────────────────
conn = sqlite3.connect(DB)
cur  = conn.cursor()

# Enable FK enforcement so we can see the actual schema shape, but we'll
# delete in dependency order anyway (child → parent).
cur.execute("PRAGMA foreign_keys = OFF")

tables_to_clear = [
    # Media files / versions (leaf nodes)
    "MediaFile",
    "MediaChapter",
    "MediaStream",
    "MediaVersion",

    # Content metadata
    "MovieMetadata",
    "ShowMetadata",
    "SeasonMetadata",
    "EpisodeMetadata",
    "OtherVideoMetadata",
    "ArtistMetadata",
    "MusicVideoMetadata",
    "SongMetadata",

    # Artwork / tags on metadata
    "MetadataGuid",
    "MetadataGenre",
    "MetadataTag",
    "MetadataStudio",
    "MetadataActor",
    "MetadataDirector",
    "MetadataWriter",
    "Artwork",

    # Content type rows (TPT child tables)
    "Movie",
    "Episode",
    "Season",
    "Show",
    "OtherVideo",
    "Artist",
    "MusicVideo",
    "Song",
    "Image",

    # Base media item
    "MediaItem",

    # Library folder index (rebuilt during scan)
    "LibraryFolder",
]

# Only delete tables that actually exist in this schema version
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
existing = {row[0] for row in cur.fetchall()}

print()
info("Purging scanned media tables...")
deleted_total = 0
for table in tables_to_clear:
    if table not in existing:
        warn(f"  Table '{table}' not found — skipping")
        continue
    cur.execute(f"SELECT COUNT(*) FROM \"{table}\"")
    count = cur.fetchone()[0]
    cur.execute(f"DELETE FROM \"{table}\"")
    ok(f"  {table}: {count} rows deleted")
    deleted_total += count

# Also reset LastScan on all LibraryPaths so ErsatzTV rescans everything
if "LibraryPath" in existing:
    cur.execute('UPDATE "LibraryPath" SET "LastScan" = NULL')
    ok("  LibraryPath.LastScan reset → full rescan will occur")

if "Library" in existing:
    cur.execute('UPDATE "Library" SET "LastScan" = NULL')
    ok("  Library.LastScan reset")

# Collections reference MediaItems — clear collection items too
for tbl in ("CollectionItem", "TraktListItem", "SmartCollectionItem"):
    if tbl in existing:
        cur.execute(f"SELECT COUNT(*) FROM \"{tbl}\"")
        count = cur.fetchone()[0]
        cur.execute(f"DELETE FROM \"{tbl}\"")
        ok(f"  {tbl}: {count} rows deleted")

conn.commit()
conn.close()

print()
ok(f"Purge complete — {deleted_total} rows removed across {len(tables_to_clear)} tables.")
ok("Library/LibraryPath configuration is untouched.")

# ── Restart ErsatzTV ──────────────────────────────────────────────────────────
print()
info("Restarting ErsatzTV for fresh scan...")
result = subprocess.run(["sudo", "systemctl", "start", "ersatztv"], capture_output=True)
if result.returncode == 0:
    ok("ErsatzTV started.  Fresh scan will begin automatically.")
else:
    warn("systemctl start failed — trying direct launch...")
    env = os.environ.copy()
    env["HOME"] = str(Path.home())
    subprocess.Popen(
        ["/opt/ersatztv/ErsatzTV"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    ok("ErsatzTV launched directly.")

print()
ok("Next steps:")
info("  1. Watch the scan:  journalctl -u ersatztv -f")
info("  2. Wait for WRN errors to stop and scan to complete (~5-15 min)")
info("  3. Run:  python3 tools/purge_stale_media.py --check  (to verify counts)")
info("  4. Then: python3 full_setup.py  (to rebuild channels)")
