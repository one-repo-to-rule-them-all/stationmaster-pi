#!/usr/bin/env python3
"""
create_media_user.py — Create a restricted Jellyfin user for TV viewing
=======================================================================
Creates a Jellyfin user with Live TV access but no admin rights,
suitable for family members or devices that should only watch TV channels.

Optionally enables parental controls to hide the Adult Movies library.

Usage:
    python3 tools/create_media_user.py --username "livingroom" --password "tv1234"
    python3 tools/create_media_user.py --username "kids" --password "abc123" --kids-only
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT  = SCRIPT_DIR.parent
ENV_FILE   = REPO_ROOT / ".env"

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


def _headers(token: str | None = None, auth_only: bool = False) -> dict:
    base = (
        'MediaBrowser Client="create_media_user", Device="stationmaster-pi", '
        'DeviceId="cmu_001", Version="1.0"'
    )
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Emby-Authorization"] = f'{base}, Token="{token}"'
    elif auth_only:
        h["X-Emby-Authorization"] = base
    return h


def http(method: str, url: str, payload: dict | None = None,
         token: str | None = None, auth_only: bool = False) -> tuple[int, dict | list | None]:
    data  = json.dumps(payload).encode() if payload is not None else None
    heads = _headers(token, auth_only)
    try:
        req = Request(url, data=data, headers=heads, method=method)
        with urlopen(req, timeout=10) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except HTTPError as e:
        body = e.read()
        return e.code, (json.loads(body) if body else None)
    except Exception as e:
        print(f"  {RED}Request failed: {e}{RESET}", file=sys.stderr)
        return 0, None


def get_admin_token(host: str, user: str, pw: str) -> str | None:
    code, body = http(
        "POST", f"{host}/Users/AuthenticateByName",
        {"Username": user, "Pw": pw}, auth_only=True,
    )
    if code == 200 and body:
        return body.get("AccessToken")  # type: ignore[union-attr]
    return None


def get_libraries(host: str, token: str) -> list[dict]:
    code, body = http("GET", f"{host}/Library/MediaFolders?IsHidden=false", token=token)
    if code == 200 and isinstance(body, dict):
        return body.get("Items", [])
    return []


def create_user(host: str, token: str, username: str, password: str) -> str | None:
    code, body = http(
        "POST", f"{host}/Users/New",
        {"Name": username, "Password": password},
        token=token,
    )
    if code == 200 and isinstance(body, dict):
        return body.get("Id")
    print(f"  {RED}Failed to create user (HTTP {code}): {body}{RESET}")
    return None


def configure_user_policy(host: str, token: str, user_id: str,
                           is_kids: bool, library_ids: list[str]) -> bool:
    policy = {
        "IsAdministrator": False,
        "IsHidden": False,
        "IsDisabled": False,
        "EnableLiveTvAccess": True,
        "EnableLiveTvManagement": False,
        "EnableMediaPlayback": True,
        "EnableAudioPlaybackTranscoding": True,
        "EnableVideoPlaybackTranscoding": True,
        "EnablePlaybackRemuxing": True,
        "EnableContentDownloading": False,
        "EnableSubtitleDownloading": False,
        "EnableSubtitleManagement": False,
        "EnableSyncTranscoding": True,
        "EnableMediaConversion": False,
        "EnableAllDevices": True,
        "EnableAllChannels": True,
        "EnableAllFolders": not is_kids,   # kids mode: restrict to allowed folders only
        "EnabledFolders": library_ids if is_kids else [],
        "EnablePublicSharing": False,
        "AccessSchedules": [],
        "BlockedTags": ["Adult"] if is_kids else [],
        "MaxParentalRating": 10 if is_kids else None,    # G/PG range
    }
    code, _ = http(
        "POST", f"{host}/Users/{user_id}/Policy",
        policy, token=token,
    )
    return code in (200, 204)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Jellyfin media viewer user")
    parser.add_argument("--username", required=True, help="New Jellyfin username")
    parser.add_argument("--password", required=True, help="New user's password")
    parser.add_argument("--kids-only", action="store_true",
                        help="Restrict to Kids Movies + TV Shows libraries only (hides Adult Movies)")
    args = parser.parse_args()

    env  = load_env()
    host = env.get("JELLYFIN_HOST", "http://localhost:8096")
    user = env.get("JELLYFIN_ADMIN_USER", "")
    pw   = env.get("JELLYFIN_ADMIN_PASS", "")

    if not pw:
        print(f"{RED}JELLYFIN_ADMIN_PASS not set in .env{RESET}")
        return 1

    print()
    print(f"{BOLD}Creating Jellyfin user: {args.username}{RESET}")
    if args.kids_only:
        print(f"  {YELLOW}Kids-only mode: Adult Movies will be hidden{RESET}")
    print()

    # Authenticate as admin
    print("  Authenticating as admin...")
    token = get_admin_token(host, user, pw)
    if not token:
        print(f"  {RED}Admin auth failed — check JELLYFIN_ADMIN_USER / JELLYFIN_ADMIN_PASS{RESET}")
        return 1
    print(f"  {GREEN}Admin token acquired{RESET}")

    # Get library list (needed for kids-mode folder restriction)
    libraries = get_libraries(host, token)
    if args.kids_only:
        allowed_names = {"Kids Movies", "TV Shows", "Stand Up Comedy", "Fitness"}
        allowed_ids   = [
            lib["Id"] for lib in libraries
            if lib.get("Name") in allowed_names
        ]
        print(f"  Allowed libraries for kids: {[l['Name'] for l in libraries if l['Id'] in allowed_ids]}")
    else:
        allowed_ids = []

    # Create user
    print(f"  Creating user '{args.username}'...")
    new_uid = create_user(host, token, args.username, args.password)
    if not new_uid:
        return 1
    print(f"  {GREEN}User created (Id: {new_uid}){RESET}")

    # Configure policy
    print("  Applying permissions policy...")
    ok = configure_user_policy(host, token, new_uid, args.kids_only, allowed_ids)
    if ok:
        print(f"  {GREEN}Policy applied{RESET}")
    else:
        print(f"  {YELLOW}Policy update returned unexpected status — check Jellyfin dashboard{RESET}")

    print()
    print(f"  {GREEN}Done!{RESET}  User '{args.username}' can now log into Jellyfin.")
    print(f"  URL: {host}")
    if args.kids_only:
        print(f"  {YELLOW}Note: Adult Movies library is hidden for this user.{RESET}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
