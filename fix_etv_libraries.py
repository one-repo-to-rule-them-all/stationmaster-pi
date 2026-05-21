#!/usr/bin/env python3
"""
fix_etv_libraries.py
Fixes ErsatzTV library classification — removes the broad "nas" library,
sets up correctly typed libraries for Movies, TV Shows, Fitness, Stand Up Comedy.

Run on the Pi:
    python3 fix_etv_libraries.py
"""
import os, sys, sqlite3, subprocess, time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

MOUNT  = os.environ.get("NAS_MOUNT_POINT", "/mnt/nas")
DB     = os.environ.get("ETV_DB_PATH", "") or str(Path.home() / ".local/share/ersatztv/ersatztv.sqlite3")

GREEN  = "\033[0;32m"; YELLOW = "\033[1;33m"; CYAN = "\033[0;36m"; RESET = "\033[0m"
def ok(m):   print(f"{GREEN}[+]{RESET} {m}")
def info(m): print(f"{CYAN}[~]{RESET} {m}")
def warn(m): print(f"{YELLOW}[!]{RESET} {m}")
def die(m):  print(f"\033[0;31m[X]{RESET} {m}"); sys.exit(1)

print(f"\n ErsatzTV Library Fix\n{'─'*50}")
info(f"DB   : {DB}")
info(f"Mount: {MOUNT}")

if not Path(DB).exists():
    die(f"ErsatzTV database not found at {DB}")
if not Path(MOUNT).exists():
    die(f"NAS mount not found at {MOUNT}")

# ── Stop ErsatzTV ─────────────────────────────────────────────────────────────
info("Stopping ErsatzTV...")
subprocess.run(["sudo", "systemctl", "stop", "ersatztv"], capture_output=True)
subprocess.run(["pkill", "-TERM", "-f", "ErsatzTV"], capture_output=True)
time.sleep(3)
subprocess.run(["pkill", "-KILL", "-f", "ErsatzTV"], capture_output=True)
time.sleep(1)
ok("ErsatzTV stopped.")

# ── Connect to DB ─────────────────────────────────────────────────────────────
conn = sqlite3.connect(DB)
cur  = conn.cursor()

# ErsatzTV MediaKind enum values
KIND = {"movie": 1, "show": 2, "musicvideo": 3, "othervideo": 4}

# ── Remove broad/incorrect library paths ──────────────────────────────────────
info("Scanning for broad library paths to remove...")
cur.execute("SELECT lp.Id, lp.LibraryId, lp.Path, l.Name FROM LibraryPath lp JOIN Library l ON l.Id=lp.LibraryId")
for pid, lid, path, name in cur.fetchall():
    if not path:
        continue
    lp = Path(path)
    mp = Path(MOUNT)
    # Remove if path IS the mount root or a parent of it (too broad)
    is_broad = (lp == mp) or (mp in lp.parents) or (lp in mp.parents) or (lp == mp.parent)
    if is_broad:
        warn(f"  Removing broad path: [{name}] → {path}")
        cur.execute("DELETE FROM LibraryPath WHERE Id=?", (pid,))
        cur.execute("SELECT COUNT(*) FROM LibraryPath WHERE LibraryId=?", (lid,))
        if cur.fetchone()[0] == 0:
            cur.execute("DELETE FROM Library WHERE Id=?", (lid,))
            warn(f"  Removed empty library: {name} (id={lid})")

conn.commit()

# ── Define correct typed libraries ────────────────────────────────────────────
# Support both flat Movies dir and Kids/Adult split
kids_path  = str(Path(MOUNT) / "Videos/Movies/Kids")
adult_path = str(Path(MOUNT) / "Videos/Movies/Adult")
flat_path  = str(Path(MOUNT) / "Videos/Movies")

movie_paths = []
if Path(kids_path).exists():
    movie_paths.append(kids_path)
if Path(adult_path).exists():
    movie_paths.append(adult_path)
elif Path(flat_path).exists():
    movie_paths.append(flat_path)

LIBRARIES = []
if movie_paths:
    LIBRARIES.append(("Movies", KIND["movie"], movie_paths))

for name, kind, paths in [
    ("TV Shows",        KIND["show"],       [str(Path(MOUNT) / "Videos/TV Shows")]),
    ("Fitness",         KIND["othervideo"], [str(Path(MOUNT) / "Videos/Fitness")]),
    ("Stand Up Comedy", KIND["othervideo"], [str(Path(MOUNT) / "Videos/Stand Up Comedy")]),
]:
    LIBRARIES.append((name, kind, paths))

# ── Create/update libraries ───────────────────────────────────────────────────
print()
info("Configuring libraries...")
for name, kind, paths in LIBRARIES:
    valid = [p for p in paths if Path(p).exists()]
    if not valid:
        warn(f"  SKIP '{name}' — no paths found on disk: {paths}")
        continue

    # Reuse existing library by name if present, otherwise create
    cur.execute("SELECT Id FROM Library WHERE Name=?", (name,))
    row = cur.fetchone()
    if row:
        lib_id = row[0]
        cur.execute("UPDATE Library SET MediaKind=?, LastScan=NULL WHERE Id=?", (kind, lib_id))
        info(f"  Reusing library '{name}' (id={lib_id})")
    else:
        cur.execute("INSERT INTO Library (Name, MediaKind, LastScan) VALUES (?,?,NULL)", (name, kind))
        lib_id = cur.lastrowid
        ok(f"  Created library '{name}' (id={lib_id})")

    for path in valid:
        cur.execute("SELECT Id FROM LibraryPath WHERE Path=?", (path,))
        if not cur.fetchone():
            cur.execute("INSERT INTO LibraryPath (LibraryId, Path, LastScan) VALUES (?,?,NULL)", (lib_id, path))
            ok(f"    Path registered: {path}")
        else:
            # Make sure it's pointing to the right library
            cur.execute("UPDATE LibraryPath SET LibraryId=?, LastScan=NULL WHERE Path=?", (lib_id, path))
            info(f"    Path already exists, reassigned to '{name}': {path}")

conn.commit()

# ── Print final state ─────────────────────────────────────────────────────────
print()
ok("Final library configuration:")
kind_name = {1:"movie", 2:"show", 3:"musicvideo", 4:"othervideo", 5:"song", 6:"image", 7:"remote"}
for row in cur.execute("""
    SELECT l.Name, l.MediaKind, lp.Path
    FROM Library l
    JOIN LibraryPath lp ON lp.LibraryId=l.Id
    ORDER BY l.Id
"""):
    print(f"  [{kind_name.get(row[1], str(row[1]))}] {row[0]} → {row[2]}")

conn.close()

# ── Restart ErsatzTV ──────────────────────────────────────────────────────────
print()
info("Restarting ErsatzTV (will trigger rescan)...")
result = subprocess.run(["sudo", "systemctl", "start", "ersatztv"], capture_output=True)
if result.returncode == 0:
    ok("ErsatzTV started. Rescan will run automatically.")
    ok("Watch progress: journalctl -u ersatztv -f")
else:
    warn("systemctl start failed — trying direct launch...")
    env = os.environ.copy()
    env["HOME"] = str(Path.home())
    subprocess.Popen(["/opt/ersatztv/ErsatzTV"], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok("ErsatzTV launched directly.")

print()
ok("Done. ErsatzTV will rescan all libraries with correct types.")
info("Once scan completes, re-run: python3 full_setup.py")
