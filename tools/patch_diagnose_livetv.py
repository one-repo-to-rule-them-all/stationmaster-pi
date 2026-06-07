#!/usr/bin/env python3
"""patch_diagnose_livetv.py — one-shot, idempotent fix for diagnose.py.

The JFCFG/JFEPG checks GET /LiveTv/TunerHosts and /LiveTv/ListingProviders,
which are POST/DELETE-only on current Jellyfin and return HTTP 405. The
configured tuners and listing providers actually live in the Live TV config
object at GET /System/Configuration/livetv. This rewrites both checks to read
from there. Safe to run more than once.

Usage:  python3 tools/patch_diagnose_livetv.py
"""
import ast
import shutil
import sys
import time
from pathlib import Path

P = Path(__file__).resolve().parent / "diagnose.py"
src = P.read_text()
orig = src

repls = [
    ('http_get(f"{host}/LiveTv/TunerHosts", token=token, timeout=5)',
     'http_get(f"{host}/System/Configuration/livetv", token=token, timeout=5)'),
    ('        tuners = json.loads(body)\n',
     '        tuners = json.loads(body).get("TunerHosts", [])\n'),
    ('http_get(f"{host}/LiveTv/ListingProviders", token=token, timeout=5)',
     'http_get(f"{host}/System/Configuration/livetv", token=token, timeout=5)'),
    ('        providers = json.loads(body)\n',
     '        providers = json.loads(body).get("ListingProviders", [])\n'),
]

# Key off whether the OLD pattern is still present. (Both http_get lines share
# the same replacement target, so a "is the new string present?" guard would
# wrongly skip the second one once the first is applied.)
applied, skipped = [], []
for old, new in repls:
    if old in src:
        src = src.replace(old, new, 1)
        applied.append(old[:45])
    else:
        skipped.append(old[:45])

# Safety net: no stale Live TV GET endpoints may remain.
if 'http_get(f"{host}/LiveTv/' in src:
    print("!! stale Live TV endpoint still present — diagnose.py differs from expected")
    sys.exit(2)

if src == orig:
    print("Already patched — no changes needed.")
    sys.exit(0)

ast.parse(src)  # fail before writing if anything broke
bak = P.with_name(f"diagnose.py.bak-{int(time.time())}")
shutil.copy2(P, bak)
P.write_text(src)
print(f"Patched: {P}")
print(f"Backup:  {bak}")
print("Applied:", applied)
if skipped:
    print("Skipped (already done):", skipped)
