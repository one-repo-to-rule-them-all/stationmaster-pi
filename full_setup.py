#!/usr/bin/env python3
"""
full_setup.py — ErsatzTV channel/collection/schedule/playout builder (Pi edition).

Assumes:
  - ErsatzTV is installed and running (or stopped — this script stops/starts it)
  - ErsatzTV has scanned its configured libraries and MediaItem rows exist
  - .env is populated (NAS_MOUNT_POINT, ETV_DB_PATH, etc.)

Lineup design philosophy:
  - ALL channels use ORDER_IN_ORDER (sequential playback, never shuffle).
  - Episode-based channels play S01E01 → S01E02 → ... then loop.
  - Movie channels play in discovery order (typically alphabetical by title),
    then loop. For franchise channels this means release order if titles are
    named conventionally.
  - Category channels (Sitcoms, Action, etc.) play through their matched
    content sequentially — feels like a marathon block, not random TV noise.

Usage:
    python3 full_setup.py                          # full channel-building run
    python3 full_setup.py --discover               # print media groups, no changes
    python3 full_setup.py --dry-run                # show what would happen
    python3 full_setup.py --no-restart             # skip ErsatzTV restart
    python3 full_setup.py --auto-shows-min 10      # add per-show channels
    python3 full_setup.py --auto-movies-min 2      # add per-franchise channels
    python3 full_setup.py --jellyfin-only \\
        --jellyfin-user U --jellyfin-password P    # only wire Jellyfin Live TV
"""

import argparse
import os
import re
import random
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

try:
    import requests
    from dotenv import load_dotenv
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "requests", "python-dotenv", "--break-system-packages", "-q"])
    import requests
    from dotenv import load_dotenv

# ── Load .env ─────────────────────────────────────────────────────────────────
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE, override=True)
else:
    load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────
ETV_HOST    = os.environ.get("ETV_HOST", "http://localhost:8409")
JF_HOST     = os.environ.get("JELLYFIN_HOST", "http://localhost:8096")

_JF_ETV_URL = os.environ.get("JF_ETV_URL", f"http://localhost:8409")
M3U_URL     = f"{_JF_ETV_URL}/iptv/channels.m3u"
XMLTV_URL   = f"{_JF_ETV_URL}/iptv/xmltv.xml"

FFMPEG_PROFILE_ID = int(os.environ.get("FFMPEG_PROFILE_ID", "1"))

# ErsatzTV PlayoutScheduleKind: 1 = Classic (ProgramSchedule-based). Must be 1.
SCHEDULE_KIND   = 1
COLLECTION_TYPE = 0   # Collection
FILL_GROUP_MODE = 0
GUIDE_MODE      = 0
MARATHON_GROUP_BY = 0

# PlaybackOrder: 0 = InOrder (sequential), 1 = Shuffle
# All channels use InOrder — user requirement: sequential lineups, no random.
ORDER_IN_ORDER = 0
ORDER_SHUFFLE  = 1   # kept for reference; not used


def _default_etv_db() -> str:
    return str(Path.home() / ".local" / "share" / "ersatztv" / "ersatztv.sqlite3")


def _default_etv_exe() -> str:
    return str(Path(os.environ.get("ETV_EXE_PATH", "/opt/ersatztv/ErsatzTV")))


def _detect_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ── Channel definitions ───────────────────────────────────────────────────────
# Numbering scheme:
#   1–9    : core / catch-all channels
#   10–19  : dedicated single-show channels (explicit)
#   12–79  : genre / theme channels
#   79–89  : fitness sub-channels
#   20–99  : auto-generated per-show channels (--auto-shows-min)
#   100+   : auto-generated per-franchise movie channels (--auto-movies-min)
#
# ALL channels: ORDER_IN_ORDER — sequential playback, never shuffle.
# Category channels (Sitcoms, Action, etc.) play all matched content
# from start to finish then loop. This creates a "marathon block" feel
# rather than a random grab-bag.

CHANNEL_DEFS = [
    # ── Core / catch-all ──────────────────────────────────────────────────
    {"number": 1, "name": "All My Media",
        "patterns": [],
        "order": ORDER_IN_ORDER},

    # ── Comedy & Animation ────────────────────────────────────────────────
    {"number": 2, "name": "Sitcoms",
        "patterns": [
            "big bang theory", "everybody loves raymond", "king of queens",
            "family guy", "that 70's show", "that 70s show",
            "parks and recreation", "it always sunny", "always sunny",
            "silicon valley", "kenan and kel", "wonder years",
            "rick and morty", "south park", "i love lucy",
        ],
        "order": ORDER_IN_ORDER},

    {"number": 3, "name": "Cartoons",
        "patterns": [
            "animaniacs", "beakman", "rocko", "tiny toon",
            "are you afraid of the dark", "rugrats", "hey arnold",
            "powerpuff", "recess", "dexter's lab",
            "boondocks", "the boondocks",
        ],
        "order": ORDER_IN_ORDER},

    {"number": 5, "name": "Anime",
        "patterns": [
            "sailor moon", "boku no hero", "dragon ball",
            "seven deadly sins", "demon slayer", "sword art online",
        ],
        "order": ORDER_IN_ORDER},

    # ── Franchise channels ────────────────────────────────────────────────
    {"number": 6, "name": "Lord of the Rings",
        "patterns": [
            "lord of the ring", "the.lord.of.the.rings",
            "the.hobbit", "hobbit", "return of the king",
        ],
        "order": ORDER_IN_ORDER},

    # ── Activity channels ─────────────────────────────────────────────────
    {"number": 8, "name": "Fitness",
        "patterns": ["fitness"],
        "order": ORDER_IN_ORDER},

    {"number": 9, "name": "Stand Up",
        "patterns": ["stand up", "standup"],
        "order": ORDER_IN_ORDER},

    # ── Dedicated single-show channels ────────────────────────────────────
    {"number": 10, "name": "I Love Lucy",
        "patterns": ["i love lucy"],
        "order": ORDER_IN_ORDER},

    {"number": 11, "name": "The Boondocks",
        "patterns": ["boondocks", "the boondocks"],
        "order": ORDER_IN_ORDER},

    # ── Genre channels ────────────────────────────────────────────────────
    {"number": 12, "name": "Action",
        "patterns": [
            "avengers", "iron man", "captain america", "thor",
            "black panther", "black widow", "captain marvel",
            "doctor strange", "guardians of the galaxy",
            "spider man", "spider-man", "deadpool",
            "the dark knight", "batman", "man of steel",
            "justice league", "the suicide squad",
            "john wick", "mission impossible",
            "die hard", "rambo", "expendables", "transporter",
            "fast and furious", "fast.and.furious",
            "bad boys", "rush hour",
            "kingsman", "the kings man",
            "x men", "x-men", "logan", "wolverine",
            "mad max", "indiana jones",
            "kill bill", "the equalizer",
            "shaft", "the gray man", "wrath of man",
            "lara croft", "tomb raider",
            "samaritan", "mortal kombat", "kingdom of heaven",
            "blade", "blade ii", "blade trinity",
            "black hawk down", "we were soldiers",
            "act of valor", "american sniper",
            "the man from toronto", "red notice",
            "plane", "the last duel",
        ],
        "order": ORDER_IN_ORDER},

    {"number": 13, "name": "Horror",
        "patterns": [
            "friday the 13th", "jason goes to hell", "jason lives",
            "jason takes",
            "halloween",
            "the conjuring", "annabelle", "the nun",
            "the curse of la llorona",
            "poltergeist", "hellraiser",
            "the texas chainsaw", "texas chainsaw massacre",
            "the shining", "doctor sleep",
            "it 2017", "it chapter 2",
            "terrifier", "smile",
            "the popes exorcist", "the devil conspiracy",
            "the people under the stairs", "the wicker man",
            "the black phone", "antlers",
            "trick r treat", "trench 11",
            "brightburn", "the invitation",
            "the dead dont die", "the mist",
            "alien", "alien vs predator", "predator",
            "the munsters",
            "scream", "saw", "the grudge", "devils due",
            "the curse of the were-rabbit",
            "3 from hell",
        ],
        "order": ORDER_IN_ORDER},

    {"number": 14, "name": "Drama",
        "patterns": [
            "the godfather",
            "the green mile", "forrest gump",
            "the wolf of wall street", "the big short",
            "no country for old men", "three billboards",
            "fences", "malcolm x", "american history x",
            "12 years a slave", "django unchained",
            "blood diamond", "we were soldiers",
            "the basketball diaries", "stand and deliver",
            "good will hunting", "a time to kill",
            "the founder",
            "saving mr banks", "king richard",
            "the banker", "emancipation",
            "all quiet on the western front",
            "babylon", "the social network",
            "manchester by the sea", "moonlight",
            "spotlight",
            "se7en", "memento", "insomnia",
            "erin brockovich", "i am sam",
            "the choice", "five feet apart",
            "revolutionary road",
            "hell or high water", "margin call",
            "patch adams", "life of a king",
            "my sisters keeper", "honey boy",
            "ozark",
        ],
        "order": ORDER_IN_ORDER},

    {"number": 15, "name": "Sci-Fi",
        "patterns": [
            "the matrix", "blade runner",
            "star trek", "star wars",
            "terminator", "the terminator",
            "interstellar", "inception",
            "ready player one", "in time", "elysium",
            "i am legend", "world war z",
            "ex machina", "arrival",
            "tron", "tron legacy", "tron ares",
            "dune",
            "2001 a space odyssey",
            "edge of tomorrow",
            "minority report", "total recall",
            "looper", "real steel",
            "transformers", "transformers one",
            "passengers", "spiderhead",
            "kingdom of the planet of the apes",
            "avatar", "the way of water",
        ],
        "order": ORDER_IN_ORDER},

    {"number": 16, "name": "Comedy",
        "patterns": [
            "happy gilmore", "big daddy", "click",
            "billy madison", "mr deeds", "anger management",
            "blended", "bedtime stories", "grown ups",
            "i now pronounce you chuck and larry",
            "you don't mess with the zohan",
            "eight crazy nights", "going overboard",
            "punch drunk love", "the wedding singer",
            "joe dirt", "joe dirt 2",
            "the longest yard", "white chicks",
            "talladega nights", "tropic thunder",
            "step brothers", "the other guys",
            "the hangover",
            "scary movie",
            "21 jump street", "22 jump street",
            "pineapple express", "this is the end",
            "we're the millers", "ferris buellers day off",
            "10 things i hate about you",
            "nacho libre", "a million ways to die in the west",
            "european vacation", "national lampoons",
            "norbit", "the mask",
            "half baked", "friday", "next friday",
            "friday after next",
            "anchor man",
            "dumb and dumber",
        ],
        "order": ORDER_IN_ORDER},

    # ── Kids Movies channel ───────────────────────────────────────────────
    # Pulls from the Kids Movies library specifically via path matching.
    # The discover_media function buckets movies from both library paths;
    # this channel uses patterns tuned to kids titles to keep it family-friendly.
    {"number": 17, "name": "Kids Movies",
        "patterns": [
            "toy story", "shrek", "cars", "ice age",
            "despicable me", "minions", "lego",
            "kung fu panda", "madagascar",
            "how to train your dragon",
            "the incredibles", "finding nemo", "finding dory",
            "monsters inc", "monsters university",
            "ratatouille", "wall-e", "wall.e",
            "up ", "brave", "coco", "soul", "encanto",
            "moana", "frozen", "tangled",
            "the lion king",
            "beauty and the beast",
            "aladdin", "mulan", "hercules",
            "tarzan", "lilo", "stitch",
            "home alone", "home.alone",
            "elf", "the santa clause", "jingle all the way",
            "bedknobs and broomsticks",
            "the little mermaid", "cinderella",
            "sleeping beauty", "snow white",
            "peter pan", "alice in wonderland",
            "dumbo", "bambi", "pinocchio",
            "spirited away", "my neighbor totoro",
            "kiki's delivery service",
            "castle in the sky",
            "the emoji movie", "smallfoot",
            "abominable",
            "puss in boots", "the bad guys",
            "turning red",
        ],
        "order": ORDER_IN_ORDER},

    # ── Fitness sub-channels ──────────────────────────────────────────────
    {"number": 79, "name": "Asylum V1",
        "patterns": [
            "insanity asylum v1", "asylum v1",
            "asylum vol 1", "insanity the asylum vol 1",
        ],
        "order": ORDER_IN_ORDER},

    {"number": 80, "name": "Asylum V2",
        "patterns": [
            "insanity asylum v2", "asylum vol 2",
            "asylum volume 2", "asylum v2", "asylum 2",
        ],
        "order": ORDER_IN_ORDER},

    {"number": 81, "name": "T25 Alpha",
        "patterns": [
            "t25 cardio alpha", "t25 speed 1.0 alpha",
            "t25 total body circuit alpha",
            "t25 focus ab intervals alpha", "t25 ab intervals alpha",
            "t25 lower focus alpha", "t25 stretch",
            "focus t25 alpha",
        ],
        "order": ORDER_IN_ORDER},

    {"number": 82, "name": "T25 Beta",
        "patterns": [
            "t25 core cardio beta", "t25 speed 2.0 beta",
            "t25 rip't circuit beta", "t25 ript circuit beta",
            "t25 dynamic core beta", "t25 upper focus beta",
            "t25 core speed beta", "focus t25 beta",
        ],
        "order": ORDER_IN_ORDER},
]


# ── Franchise definitions ─────────────────────────────────────────────────────
# Used by --auto-movies-min to create per-franchise channels.
# All franchises play in ORDER_IN_ORDER (release sequence → loop).
FRANCHISE_DEFS = [
    {"name": "Marvel Cinematic Universe", "patterns": [
        "avengers", "iron man", "captain america", "thor",
        "black panther", "black widow", "captain marvel",
        "doctor strange", "doctor.strange", "guardians of the galaxy",
        "ant man", "spider man", "spider-man",
        "the.incredible.hulk", "eternals", "shazam", "morbius",
        "venom", "deadpool", "ultimate.avengers",
    ], "order": ORDER_IN_ORDER},
    {"name": "DC Films", "patterns": [
        "batman", "the dark knight", "joker",
        "the suicide squad", "man.of.steel", "wonder woman",
        "justice league", "zack.snyders.justice.league",
    ], "order": ORDER_IN_ORDER},
    {"name": "Harry Potter", "patterns": [
        "harry.potter", "harry_potter", "harry potter", "fantastic beasts",
    ], "order": ORDER_IN_ORDER},
    {"name": "Mission: Impossible", "patterns": [
        "mission.impossible", "mission impossible",
    ], "order": ORDER_IN_ORDER},
    {"name": "The Matrix", "patterns": ["the.matrix", "the matrix"],
        "order": ORDER_IN_ORDER},
    {"name": "Pirates of the Caribbean", "patterns": [
        "pirates.of.the.caribbean", "pirates of the caribbean",
    ], "order": ORDER_IN_ORDER},
    {"name": "Terminator", "patterns": ["terminator"], "order": ORDER_IN_ORDER},
    {"name": "Indiana Jones", "patterns": [
        "indiana.jones", "indiana jones",
    ], "order": ORDER_IN_ORDER},
    {"name": "Resident Evil Films", "patterns": [
        "resident evil", "resident.evil",
    ], "order": ORDER_IN_ORDER},
    {"name": "Friday the 13th", "patterns": [
        "friday the 13th", "friday.the.13th",
        "jason goes to hell", "jason lives", "jason takes",
    ], "order": ORDER_IN_ORDER},
    {"name": "X-Men", "patterns": [
        "x-men", "x.men", "x2.x-men", "x.first.class",
        "logan", "wolverine",
    ], "order": ORDER_IN_ORDER},
    {"name": "Rocky",            "patterns": ["rocky"],      "order": ORDER_IN_ORDER},
    {"name": "Rush Hour",        "patterns": ["rush.hour", "rush hour"],  "order": ORDER_IN_ORDER},
    {"name": "The Hangover",     "patterns": ["the.hangover", "the hangover"], "order": ORDER_IN_ORDER},
    {"name": "Halloween / Halloweentown", "patterns": ["halloween"], "order": ORDER_IN_ORDER},
    {"name": "The Hunger Games", "patterns": [
        "the.hunger.games", "the hunger games", "catching.fire", "mockingjay",
    ], "order": ORDER_IN_ORDER},
    {"name": "Toy Story",        "patterns": ["toy.story", "toy story"],  "order": ORDER_IN_ORDER},
    {"name": "Shrek",            "patterns": ["shrek"],       "order": ORDER_IN_ORDER},
    {"name": "Cars (Pixar)",     "patterns": ["^cars$", "cars.2", "cars.3"], "order": ORDER_IN_ORDER},
    {"name": "Ice Age",          "patterns": ["ice age"],     "order": ORDER_IN_ORDER},
    {"name": "Despicable Me & Minions", "patterns": ["despicable.me", "despicable me", "minions"], "order": ORDER_IN_ORDER},
    {"name": "Lego Movies",      "patterns": ["the.lego"],    "order": ORDER_IN_ORDER},
    {"name": "Bad Boys",         "patterns": ["bad boys"],    "order": ORDER_IN_ORDER},
    {"name": "Scary Movie",      "patterns": ["scary movie", "scary.movie"], "order": ORDER_IN_ORDER},
    {"name": "Back to the Future","patterns": ["back.to.the.future", "back to the future"], "order": ORDER_IN_ORDER},
    {"name": "Men in Black",     "patterns": ["men.in.black", "men in black"], "order": ORDER_IN_ORDER},
    {"name": "Predator / Alien", "patterns": ["predator", "alien.vs.predator", "aliens"], "order": ORDER_IN_ORDER},
    {"name": "Conjuring Universe","patterns": [
        "the.conjuring", "the conjuring", "annabelle", "the.nun", "the nun",
        "the.curse.of.la.llorona",
    ], "order": ORDER_IN_ORDER},
    {"name": "Jurassic Park / World", "patterns": [
        "jurassic.park", "jurassic park", "jurassic.world", "jurassic world",
    ], "order": ORDER_IN_ORDER},
    {"name": "Home Alone",       "patterns": ["home alone", "home.alone"], "order": ORDER_IN_ORDER},
    {"name": "Tron",             "patterns": ["^tron$", "tron.legacy", "tron.ares"], "order": ORDER_IN_ORDER},
    {"name": "Tremors",          "patterns": ["tremors"],     "order": ORDER_IN_ORDER},
    {"name": "Hannibal Lecter",  "patterns": ["hannibal", "red.dragon"], "order": ORDER_IN_ORDER},
    {"name": "Dragon Ball Movies","patterns": [
        "dragon ball z -", "dragon ball -", "dragon ball super -",
    ], "order": ORDER_IN_ORDER},
    {"name": "Tarzan",           "patterns": ["tarzan"],      "order": ORDER_IN_ORDER},
    {"name": "Star Trek",        "patterns": ["star.trek", "star trek"], "order": ORDER_IN_ORDER},
    {"name": "Lilo & Stitch",    "patterns": ["lilo", "stitch"], "order": ORDER_IN_ORDER},
    {"name": "Madagascar / Kung Fu Panda", "patterns": [
        "madagascar", "kung.fu.panda", "kung fu panda",
    ], "order": ORDER_IN_ORDER},
    {"name": "How To Train Your Dragon", "patterns": [
        "how.to.train.your.dragon", "how to train your dragon",
    ], "order": ORDER_IN_ORDER},
    {"name": "The Godfather",    "patterns": ["the godfather", "the.godfather"], "order": ORDER_IN_ORDER},
    {"name": "Blade",            "patterns": ["^blade$", "blade ii", "blade.trinity"], "order": ORDER_IN_ORDER},
    {"name": "The Incredibles",  "patterns": ["the.incredibles", "the incredibles"], "order": ORDER_IN_ORDER},
    {"name": "Mad Max",          "patterns": ["mad max", "mad.max"], "order": ORDER_IN_ORDER},
    {"name": "Lord of the Rings","patterns": [
        "lord of the rings", "the.lord.of.the.rings", "the.hobbit", "hobbit",
    ], "order": ORDER_IN_ORDER},
]

DRY_RUN = False


# ── Process management (Linux) ────────────────────────────────────────────────

def etv_is_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "ErsatzTV"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def restart_ersatztv(exe_path: str = "") -> bool:
    exe = Path(exe_path or _default_etv_exe())
    print("\n── Restarting ErsatzTV to trigger playout builds ──")

    # Try systemctl first (preferred), fall back to pkill
    result = subprocess.run(
        ["sudo", "systemctl", "restart", "ersatztv"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  ✓ ErsatzTV restarted via systemctl")
    else:
        # Fall back: pkill then relaunch
        subprocess.run(["pkill", "-f", "ErsatzTV"], capture_output=True)
        time.sleep(3)
        if not exe.exists():
            print(f"  ✗ ErsatzTV binary not found at {exe}")
            return False
        env = os.environ.copy()
        env["HOME"] = str(Path.home())
        subprocess.Popen(
            [str(exe)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"  ✓ ErsatzTV launched: {exe}")

    # Wait for API
    print("  Waiting for ErsatzTV API", end="", flush=True)
    for _ in range(45):
        time.sleep(2)
        try:
            r = requests.get(f"{ETV_HOST}/iptv/channels.m3u", timeout=3)
            if r.ok:
                print(" ✓")
                print("  ErsatzTV is online — playouts are being built.")
                return True
        except Exception:
            pass
        print(".", end="", flush=True)

    print(" timed out")
    print("  ⚠ ErsatzTV did not respond in 90s — check: journalctl -u ersatztv -n 50")
    return False


# ── DB helpers ────────────────────────────────────────────────────────────────

def open_db(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        print(f"✗ Database not found: {path}")
        sys.exit(1)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def qone(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def qall(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def get_table_cols(conn, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


# ── Pattern matching ──────────────────────────────────────────────────────────

def _pat_matches(pattern: str, group_key: str) -> bool:
    """Match a pattern against a group key.

    Supports:
      ^...$  — full regex anchor
      plain  — whole-word match (start-of-word anchored), dots/underscores/hyphens
               treated as spaces. Prevents 'rocko' matching 'erin brockovich'.
    """
    if pattern.startswith("^") and pattern.endswith("$"):
        return re.match(pattern, group_key) is not None
    pat_norm = re.sub(r"[._\-]+", " ", pattern.lower()).strip()
    key_norm = re.sub(r"[._\-]+", " ", group_key.lower()).strip()
    return bool(re.search(r"\b" + re.escape(pat_norm), key_norm))


# ── Media discovery ───────────────────────────────────────────────────────────

def discover_media(conn) -> dict:
    """Return { group_key (lowercase): [media_item_id, ...] }

    Group keys:
      - TV show title (lowercased)
      - Movie title (lowercased) — picks up both Kids and Adult libraries
        since both are LocalMovie libraries, both have MovieMetadata rows
      - OtherVideo title + path segments (for fitness program sub-channels)
    """
    groups: dict = {}

    def add(group: str, mid: int):
        groups.setdefault(group, []).append(mid)

    # TV episodes → group by show title, ordered by season + episode
    rows = qall(conn, """
        SELECT DISTINCT mi.Id, sm.Title,
               COALESCE(sea.SeasonNumber, 0) AS SeasonNum,
               COALESCE(e.EpisodeNumber, 0)  AS EpNum
        FROM   MediaItem      mi
        JOIN   Episode        e   ON e.Id      = mi.Id
        JOIN   Season         sea ON sea.Id    = e.SeasonId
        JOIN   Show           s   ON s.Id      = sea.ShowId
        JOIN   ShowMetadata   sm  ON sm.ShowId = s.Id
        ORDER BY sm.Title, SeasonNum, EpNum
    """)
    for r in rows:
        add(r["Title"].lower().strip(), r["Id"])

    # Movies → group by title, ordered alphabetically
    # Both Kids Movies and Movies (Adult) libraries have MovieMetadata rows.
    rows = qall(conn, """
        SELECT DISTINCT mi.Id, mm.Title
        FROM   MediaItem      mi
        JOIN   Movie          m   ON m.Id      = mi.Id
        JOIN   MovieMetadata  mm  ON mm.MovieId = m.Id
        ORDER BY mm.Title
    """)
    for r in rows:
        add(r["Title"].lower().strip(), r["Id"])

    # OtherVideos (Fitness, Stand Up Comedy)
    rows = qall(conn, """
        SELECT DISTINCT mi.Id, lp.Path AS LibPath,
                        mf.Path AS FilePath, ovm.Title
        FROM   MediaItem          mi
        JOIN   OtherVideo         ov  ON ov.Id           = mi.Id
        LEFT  JOIN OtherVideoMetadata ovm ON ovm.OtherVideoId = mi.Id
        JOIN   LibraryPath        lp  ON lp.Id           = mi.LibraryPathId
        LEFT  JOIN MediaVersion   mv  ON mv.OtherVideoId = ov.Id
        LEFT  JOIN MediaFile      mf  ON mf.MediaVersionId = mv.Id
    """)
    for r in rows:
        lib_path  = (r["LibPath"]  or "").replace("\\", "/").lower()
        file_path = (r["FilePath"] or "").replace("\\", "/").lower()
        title     = (r["Title"]    or "untitled").lower().strip()

        if title and title != "untitled":
            add(title, r["Id"])

        # Bucket by each path segment below the library root
        if file_path:
            try:
                tail = file_path
                if lib_path and file_path.startswith(lib_path):
                    tail = file_path[len(lib_path):]
                segments = [s for s in tail.split("/") if s]
                for seg in segments[:-1]:   # skip the filename itself
                    add(seg.strip(), r["Id"])
            except Exception:
                pass

        # Category buckets for the catch-all Fitness / Stand Up channels
        if "fitness" in lib_path or "fitness" in file_path:
            add("fitness", r["Id"])
        elif ("stand up" in lib_path or "standup" in lib_path
              or "comedy" in lib_path or "stand up" in file_path
              or "standup" in file_path):
            add("stand up", r["Id"])

    return groups


# ── Channel assignment ────────────────────────────────────────────────────────

def assign_channels(groups: dict, channel_defs=None) -> list:
    """Match media groups to channel_defs. Returns list of task dicts."""
    defs = channel_defs if channel_defs is not None else CHANNEL_DEFS
    tasks = []
    all_ids: list = []
    for ids in groups.values():
        all_ids.extend(ids)
    seen: set = set()
    unique_all = [i for i in all_ids if not (i in seen or seen.add(i))]

    for ch_def in defs:
        if ch_def.get("media_ids") is not None:
            tasks.append(dict(ch_def))
            continue

        patterns = ch_def["patterns"]
        if not patterns:
            tasks.append({**ch_def, "media_ids": unique_all})
            continue

        matched: list = []
        for group_key, ids in groups.items():
            if any(_pat_matches(p.lower(), group_key) for p in patterns):
                matched.extend(ids)

        seen2: set = set()
        matched = [i for i in matched if not (i in seen2 or seen2.add(i))]
        if matched:
            tasks.append({**ch_def, "media_ids": matched})
        else:
            print(f"  ⚠ No media matched channel {ch_def['number']} "
                  f"'{ch_def['name']}' — skipping")

    return tasks


# ── Auto-channel generation ───────────────────────────────────────────────────

AUTO_SKIP_KEYS = {"fitness", "stand up", "standup"}


def _is_show_group(conn, group_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ShowMetadata WHERE LOWER(TRIM(Title)) = ? LIMIT 1",
        (group_key,)).fetchone()
    return row is not None


def _existing_pattern_set() -> set:
    seen = set()
    for d in CHANNEL_DEFS:
        for p in d.get("patterns", []):
            seen.add(p.lower())
    return seen


def _title_case(key: str) -> str:
    s = key.replace(".", " ").replace("_", " ")
    s = " ".join(w for w in s.split() if w)
    return s.title()


def auto_generate_channel_defs(conn, groups: dict, show_min: int = 0,
                               movie_min: int = 0,
                               start_show_number: int = 20,
                               start_franchise_number: int = 100) -> list:
    """Generate additional channel defs from discovered media.

    show_min  > 0: one channel per TV show with at least N episodes
    movie_min > 0: one channel per franchise with at least N films

    All auto-generated channels use ORDER_IN_ORDER (sequential).
    """
    additions = []
    taken = {int(d["number"]) for d in CHANNEL_DEFS}

    def _alloc(start: int) -> int:
        n = start
        while n in taken:
            n += 1
        taken.add(n)
        return n

    if show_min > 0:
        # Sort by episode count desc, then title asc for stable numbering
        candidates = sorted(
            ((k, v) for k, v in groups.items()
             if k not in AUTO_SKIP_KEYS and len(v) >= show_min),
            key=lambda x: (-len(x[1]), x[0]),
        )
        existing_show_names = {d["name"].lower() for d in CHANNEL_DEFS}
        next_n = start_show_number
        for key, ids in candidates:
            if _title_case(key).lower() in existing_show_names:
                continue
            if not _is_show_group(conn, key):
                continue
            num = _alloc(next_n)
            additions.append({
                "number": num,
                "name": _title_case(key),
                "patterns": [key],
                "order": ORDER_IN_ORDER,   # always sequential
            })
            next_n = num + 1

    if movie_min > 0:
        next_n = start_franchise_number
        for fr in FRANCHISE_DEFS:
            patterns = [p.lower() for p in fr["patterns"]]
            matching = []
            for group_key, ids in groups.items():
                if group_key in AUTO_SKIP_KEYS:
                    continue
                if _is_show_group(conn, group_key):
                    continue
                if any(_pat_matches(p, group_key) for p in patterns):
                    matching.extend(ids)
            seen_ids: set = set()
            matching = [i for i in matching
                        if not (i in seen_ids or seen_ids.add(i))]
            if len(matching) < movie_min:
                continue
            num = _alloc(next_n)
            additions.append({
                "number": num,
                "name": fr["name"],
                "patterns": fr["patterns"],
                "order": ORDER_IN_ORDER,   # always sequential
                "media_ids": matching,
            })
            next_n = num + 1

    return additions


# ── ErsatzTV DB upserts ───────────────────────────────────────────────────────

def upsert_channel(conn, number: int, name: str) -> int:
    existing = qone(conn, "SELECT Id FROM Channel WHERE Number = ?", (str(number),))
    if existing:
        cid = existing["Id"]
        if not DRY_RUN:
            conn.execute("UPDATE Channel SET Name = ? WHERE Id = ?", (name, cid))
        print(f"  ↻ Channel #{number} '{name}' (id={cid}) — reused")
        return cid

    if DRY_RUN:
        print(f"  + Would create channel #{number} '{name}'")
        return -1

    uid = str(uuid.uuid4())
    avail = get_table_cols(conn, "Channel")

    col_names = [
        "Number", "Name", "FFmpegProfileId", '"Group"',
        "IsEnabled", "ShowInEpg",
        "IdleBehavior", "PlayoutMode", "PlayoutSource", "StreamingMode",
        "TranscodeMode", "SubtitleMode", "SongVideoMode",
        "MusicVideoCreditsMode", "StreamSelectorMode", "SortNumber", "UniqueId",
    ]
    col_vals = [
        str(number), name, FFMPEG_PROFILE_ID, "IPTV",
        1, 1,
        0, 0, 0, 0,
        1, 0, 0, 0,
        0, number, uid,
    ]

    for col, val in [("StreamingEngine", 0), ("NextEngineTextSubtitleMode", 0)]:
        if col in avail:
            col_names.append(col)
            col_vals.append(val)

    sql = (f"INSERT INTO Channel ({', '.join(col_names)}) "
           f"VALUES ({', '.join(['?'] * len(col_vals))})")
    cur = conn.execute(sql, col_vals)
    cid = cur.lastrowid
    print(f"  + Channel #{number} '{name}' created (id={cid})")
    return cid


def upsert_collection(conn, name: str, media_ids: list) -> int:
    existing = qone(conn, "SELECT Id FROM Collection WHERE Name = ?", (name,))
    if existing:
        coll_id = existing["Id"]
        print(f"  ↻ Collection '{name}' (id={coll_id}) — reused, "
              f"syncing {len(media_ids)} items")
    else:
        if DRY_RUN:
            print(f"  + Would create collection '{name}' "
                  f"with {len(media_ids)} items")
            return -1
        cur = conn.execute(
            "INSERT INTO Collection (Name, UseCustomPlaybackOrder) VALUES (?,?)",
            (name, 0))
        coll_id = cur.lastrowid
        print(f"  + Collection '{name}' created (id={coll_id}) "
              f"with {len(media_ids)} items")

    if DRY_RUN:
        return coll_id

    conn.execute("DELETE FROM CollectionItem WHERE CollectionId = ?", (coll_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO CollectionItem "
        "(CollectionId, MediaItemId, CustomIndex) VALUES (?,?,?)",
        [(coll_id, mid, 0) for mid in media_ids])
    return coll_id


def upsert_schedule(conn, name: str, coll_id: int, playback_order: int) -> int:
    existing = qone(conn, "SELECT Id FROM ProgramSchedule WHERE Name = ?", (name,))
    if existing:
        sched_id = existing["Id"]
        print(f"  ↻ Schedule '{name}' (id={sched_id}) — reused")
    else:
        if DRY_RUN:
            print(f"  + Would create schedule '{name}'")
            return -1
        cur = conn.execute("""
            INSERT INTO ProgramSchedule
              (Name, KeepMultiPartEpisodesTogether, RandomStartPoint,
               ShuffleScheduleItems, TreatCollectionsAsShows,
               FixedStartTimeBehavior)
            VALUES (?,?,?,?,?,?)
        """, (name, 0, 0, 0, 0, 0))
        sched_id = cur.lastrowid
        print(f"  + Schedule '{name}' created (id={sched_id})")

    if DRY_RUN:
        return sched_id

    existing_item = qone(conn,
        "SELECT Id FROM ProgramScheduleItem WHERE ProgramScheduleId = ? LIMIT 1",
        (sched_id,))
    if not existing_item:
        cur2 = conn.execute("""
            INSERT INTO ProgramScheduleItem
              (ProgramScheduleId, CollectionId, CollectionType, PlaybackOrder,
               "Index", FillWithGroupMode, GuideMode, MarathonGroupBy,
               MarathonShuffleGroups, MarathonShuffleItems, MarathonBatchSize)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (sched_id, coll_id, COLLECTION_TYPE, playback_order,
              0, FILL_GROUP_MODE, GUIDE_MODE, MARATHON_GROUP_BY,
              0, 0, None))
        item_id = cur2.lastrowid
        conn.execute(
            "INSERT INTO ProgramScheduleFloodItem (Id) VALUES (?)", (item_id,))
        print(f"  + Flood item added (item_id={item_id})")
    else:
        conn.execute("""
            UPDATE ProgramScheduleItem
            SET CollectionId = ?, PlaybackOrder = ?
            WHERE Id = ?
        """, (coll_id, playback_order, existing_item["Id"]))

    return sched_id


def upsert_playout(conn, channel_id: int, sched_id: int) -> int:
    existing = qone(conn, "SELECT Id FROM Playout WHERE ChannelId = ?", (channel_id,))
    if existing:
        playout_id = existing["Id"]
        if not DRY_RUN:
            conn.execute(
                "UPDATE Playout SET ProgramScheduleId = ?, ScheduleKind = ? "
                "WHERE Id = ?",
                (sched_id, SCHEDULE_KIND, playout_id))
        print(f"  ↻ Playout (id={playout_id}) — updated ScheduleKind={SCHEDULE_KIND}")
        return playout_id

    if DRY_RUN:
        print(f"  + Would create playout for channel {channel_id}")
        return -1

    seed = random.randint(0, 2**31 - 1)
    cur = conn.execute("""
        INSERT INTO Playout (ChannelId, ProgramScheduleId, ScheduleKind, Seed)
        VALUES (?,?,?,?)
    """, (channel_id, sched_id, SCHEDULE_KIND, seed))
    playout_id = cur.lastrowid

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    existing_anchor = qone(conn,
        "SELECT PlayoutId FROM PlayoutAnchor WHERE PlayoutId = ?", (playout_id,))
    if not existing_anchor:
        conn.execute("""
            INSERT INTO PlayoutAnchor
              (PlayoutId, NextStart, InFlood, InDurationFiller, NextGuideGroup,
               NextInstructionIndex, MultipleRemaining)
            VALUES (?,?,?,?,?,?,?)
        """, (playout_id, now_iso, 1, 0, 0, 0, None))

    print(f"  + Playout created (id={playout_id})")
    return playout_id


# ── Channel renumbering ───────────────────────────────────────────────────────

def renumber_channels(conn, tasks: list) -> None:
    """Ensure SortNumber is sequential 1..N across all channels.
    Gaps in channel numbers cause Jellyfin's internal channel-row index
    to drift. This keeps the EPG guide aligned."""
    if DRY_RUN:
        return
    rows = qall(conn, "SELECT Id, Number FROM Channel ORDER BY CAST(Number AS INTEGER)")
    for idx, row in enumerate(rows, start=1):
        conn.execute("UPDATE Channel SET SortNumber = ? WHERE Id = ?",
                     (idx, row["Id"]))
    print(f"  ✓ SortNumber renumbered 1..{len(rows)}")


# ── Jellyfin Live TV wiring ───────────────────────────────────────────────────

def jf_auth_header(token: str = "") -> dict:
    parts = [
        'MediaBrowser Client="stationmaster-pi"',
        'Device="full_setup"',
        'DeviceId="full_setup_001"',
        'Version="1.0"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return {"X-Emby-Authorization": ", ".join(parts)}


def jf_authenticate(username: str, password: str) -> str:
    r = requests.post(
        f"{JF_HOST}/Users/AuthenticateByName",
        json={"Username": username, "Pw": password},
        headers={**jf_auth_header(), "Content-Type": "application/json"},
        timeout=10,
    )
    if not r.ok:
        print(f"  ✗ Jellyfin auth failed: {r.status_code}")
        return ""
    print("  ✓ Authenticated to Jellyfin")
    return r.json()["AccessToken"]


def _is_etv_tuner(e: dict) -> bool:
    for key in ("Url", "Path"):
        if "/iptv/channels.m3u" in (e.get(key) or "").lower():
            return True
    return False


def _is_etv_guide(e: dict) -> bool:
    for key in ("Url", "Path"):
        if "/iptv/xmltv.xml" in (e.get(key) or "").lower():
            return True
    return False


def _prune_duplicates(items, matcher, kind_label, delete_path, token):
    matches = [e for e in items if matcher(e)]
    if not matches:
        return None
    survivor = matches[0]
    for e in matches[1:]:
        eid = e.get("Id") or e.get("id")
        if not eid:
            continue
        if DRY_RUN:
            print(f"    DRY-RUN would DELETE {kind_label} id={eid}")
            continue
        for url, params in (
            (delete_path, {"id": eid}),
            (f"{delete_path}/{eid}", None),
        ):
            try:
                r = requests.delete(
                    f"{JF_HOST}{url}",
                    headers=jf_auth_header(token),
                    params=params, timeout=15,
                )
                if r.status_code in (200, 204):
                    print(f"    + deleted {kind_label} id={eid}")
                    break
            except Exception:
                break
    return survivor


def configure_jellyfin(username: str, password: str) -> None:
    print("\n── Jellyfin Live TV setup ──")
    token = jf_authenticate(username, password)
    if not token:
        return

    # JF 10.11+: configured tuners live in /System/Configuration/livetv
    try:
        cfg_r = requests.get(
            f"{JF_HOST}/System/Configuration/livetv",
            headers=jf_auth_header(token), timeout=15)
        cfg = cfg_r.json() if cfg_r.ok else {}
    except Exception:
        cfg = {}

    tuners    = cfg.get("TunerHosts",       []) or []
    providers = cfg.get("ListingProviders", []) or []

    # Prune duplicate tuners, then add if missing
    tuner_survivor = _prune_duplicates(
        tuners, _is_etv_tuner, "M3U tuner", "/LiveTv/TunerHosts", token)

    if tuner_survivor:
        print(f"  ✓ M3U tuner already registered: {tuner_survivor.get('Url')}")
        tid = tuner_survivor.get("Id")
        if not DRY_RUN and tid:
            requests.put(
                f"{JF_HOST}/LiveTv/TunerHosts",
                json={**tuner_survivor, "Url": M3U_URL,
                      "TunerCount": int(os.environ.get("JF_TUNER_COUNT", "4"))},
                headers={**jf_auth_header(token), "Content-Type": "application/json"},
                timeout=15)
    else:
        if not DRY_RUN:
            r = requests.post(
                f"{JF_HOST}/LiveTv/TunerHosts",
                json={"Type": "m3u", "Url": M3U_URL,
                      "TunerCount": int(os.environ.get("JF_TUNER_COUNT", "4")),
                      "AllowHWTranscoding": False},
                headers={**jf_auth_header(token), "Content-Type": "application/json"},
                timeout=15)
            if r.ok:
                print(f"  ✓ M3U tuner registered: {M3U_URL}")
            else:
                print(f"  ⚠ M3U tuner registration returned {r.status_code}")

    # XMLTV guide
    guide_survivor = _prune_duplicates(
        providers, _is_etv_guide, "XMLTV guide", "/LiveTv/ListingProviders", token)

    if guide_survivor:
        print(f"  ✓ XMLTV guide already registered: {guide_survivor.get('Url') or guide_survivor.get('Path')}")
    else:
        if not DRY_RUN:
            r = requests.post(
                f"{JF_HOST}/LiveTv/ListingProviders",
                json={"Type": "xmltv", "Path": XMLTV_URL, "EnableAllTuners": True},
                headers={**jf_auth_header(token), "Content-Type": "application/json"},
                timeout=15)
            if r.ok:
                print(f"  ✓ XMLTV guide registered: {XMLTV_URL}")
            else:
                print(f"  ⚠ XMLTV registration returned {r.status_code} — "
                      f"add manually: Dashboard → Live TV → TV Guide Data Providers → + → XMLTV → {XMLTV_URL}")

    # Refresh channels + guide
    if not DRY_RUN:
        for endpoint in ("/LiveTv/Channels/Refresh", "/LiveTv/Guide/Refresh",
                         "/ScheduledTasks/Running/refreshGuide"):
            try:
                requests.post(
                    f"{JF_HOST}{endpoint}",
                    headers=jf_auth_header(token), timeout=15)
            except Exception:
                pass
        print("  ✓ Refresh tasks triggered")


# ── Main channel-building pipeline ───────────────────────────────────────────

def build_channels(db_path: str, auto_shows_min: int = 0,
                   auto_movies_min: int = 0) -> None:
    """Core pipeline: discover → assign → upsert into ErsatzTV DB."""
    print(f"\n── Opening ErsatzTV database: {db_path} ──")

    # Stop ErsatzTV before writing to the DB
    print("  Stopping ErsatzTV...")
    subprocess.run(["sudo", "systemctl", "stop", "ersatztv"],
                   capture_output=True)
    subprocess.run(["pkill", "-f", "ErsatzTV"], capture_output=True)
    time.sleep(2)

    conn = open_db(db_path)

    print("\n── Discovering media ──")
    groups = discover_media(conn)
    total_items = sum(len(v) for v in groups.values())
    print(f"  Found {len(groups)} groups, {total_items} total media references")

    # Build final channel list
    all_defs = list(CHANNEL_DEFS)
    if auto_shows_min > 0 or auto_movies_min > 0:
        extras = auto_generate_channel_defs(
            conn, groups,
            show_min=auto_shows_min,
            movie_min=auto_movies_min,
        )
        all_defs.extend(extras)
        print(f"\n  Auto-generated {len(extras)} additional channels")

    print("\n── Assigning media to channels ──")
    tasks = assign_channels(groups, all_defs)
    print(f"  {len(tasks)} channels will be built")

    print("\n── Building channels ──")
    built = 0
    skipped = 0
    for task in sorted(tasks, key=lambda t: t["number"]):
        number      = task["number"]
        name        = task["name"]
        media_ids   = task.get("media_ids", [])
        order       = task.get("order", ORDER_IN_ORDER)   # always IN_ORDER

        if not media_ids:
            skipped += 1
            continue

        print(f"\n  Ch #{number} '{name}' — {len(media_ids)} items "
              f"[{'IN_ORDER' if order == ORDER_IN_ORDER else 'SHUFFLE'}]")

        if DRY_RUN:
            built += 1
            continue

        coll_name = f"{name} Collection"
        cid       = upsert_channel(conn, number, name)
        coll_id   = upsert_collection(conn, coll_name, media_ids)
        sched_id  = upsert_schedule(conn, f"{name} Schedule", coll_id, order)
        _         = upsert_playout(conn, cid, sched_id)
        built += 1

    if not DRY_RUN:
        renumber_channels(conn, tasks)
        conn.commit()

    conn.close()
    print(f"\n  ✓ {built} channels built, {skipped} skipped (no media)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    global DRY_RUN

    parser = argparse.ArgumentParser(
        description="stationmaster-pi channel builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--discover",          action="store_true",
                        help="Print discovered media groups and exit")
    parser.add_argument("--dry-run",           action="store_true",
                        help="Print what would happen without making changes")
    parser.add_argument("--no-restart",        action="store_true",
                        help="Skip ErsatzTV restart after building")
    parser.add_argument("--auto-shows-min",    type=int, default=0, metavar="N",
                        help="Add per-show channels for shows with >= N episodes")
    parser.add_argument("--auto-movies-min",   type=int, default=0, metavar="N",
                        help="Add per-franchise channels for franchises with >= N films")
    parser.add_argument("--jellyfin-only",     action="store_true",
                        help="Only wire Jellyfin Live TV (skip DB/channel build)")
    parser.add_argument("--jellyfin-user",     default="",
                        help="Jellyfin admin username")
    parser.add_argument("--jellyfin-password", default="",
                        help="Jellyfin admin password")
    args = parser.parse_args()

    DRY_RUN = args.dry_run

    db_path = os.environ.get("ETV_DB_PATH") or _default_etv_db()

    # Auto-picks from env if not supplied on CLI
    jf_user = (args.jellyfin_user
                or os.environ.get("JELLYFIN_ADMIN_USER", ""))
    jf_pass = (args.jellyfin_password
                or os.environ.get("JELLYFIN_ADMIN_PASS", ""))

    print(f"\nstationmaster-pi  full_setup.py")
    print(f"{'─'*50}")
    print(f"  ETV DB   : {db_path}")
    print(f"  ETV host : {ETV_HOST}")
    print(f"  JF host  : {JF_HOST}")
    print(f"  Dry run  : {DRY_RUN}")
    print(f"  Order    : ALL CHANNELS → IN_ORDER (sequential)")

    if args.discover:
        db = open_db(db_path)
        groups = discover_media(db)
        db.close()
        print(f"\nDiscovered {len(groups)} groups:\n")
        for k, v in sorted(groups.items(), key=lambda x: -len(x[1])):
            print(f"  {len(v):4d}  {k}")
        return

    if not args.jellyfin_only:
        auto_shows = int(os.environ.get("AUTO_SHOWS_MIN", args.auto_shows_min))
        auto_movies = int(os.environ.get("AUTO_MOVIES_MIN", args.auto_movies_min))
        build_channels(db_path,
                       auto_shows_min=auto_shows,
                       auto_movies_min=auto_movies)

        if not args.no_restart and not DRY_RUN:
            exe_path = os.environ.get("ETV_EXE_PATH", "") or _default_etv_exe()
            restart_ersatztv(exe_path)

    if jf_user and jf_pass:
        configure_jellyfin(jf_user, jf_pass)
    else:
        print("\n  Skipping Jellyfin wire-up (no --jellyfin-user/--jellyfin-password).")
        print("  To wire Jellyfin manually:")
        print(f"    python3 full_setup.py --jellyfin-only "
              f"--jellyfin-user <user> --jellyfin-password <pass>")

    print("\n✓ full_setup.py complete.")


if __name__ == "__main__":
    main()
