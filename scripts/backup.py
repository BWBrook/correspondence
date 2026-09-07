#!/usr/bin/env python3
"""Consistent SQLite snapshot; never copy an active WAL database with ordinary cp."""
import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("destination", nargs="?", type=Path)
args = parser.parse_args()
directory = Path(os.environ.get("CORRESPONDENCE_DATA_DIR", "~/Library/Application Support/Correspondence")).expanduser().resolve()
source_path = directory / "board.sqlite3"
if not source_path.is_file():
    raise SystemExit(f"No existing board database at {source_path}; nothing was backed up.")
destination = args.destination or directory / "backups" / (datetime.now(timezone.utc).strftime("board-%Y%m%dT%H%M%S%fZ") + ".sqlite3")
destination = destination.expanduser().resolve()
destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
# Refuse overwrites, including accidentally backing up onto the live database.
with destination.open("xb"):
    pass
try:
    destination.chmod(0o600)
    with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(destination) as target:
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
except BaseException:
    destination.unlink(missing_ok=True)
    raise
print(destination)
