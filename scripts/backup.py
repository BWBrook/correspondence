#!/usr/bin/env python3
"""Create a checked SQLite snapshot; retain 30 days of automatic backups."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import sqlite3
import stat


STAMP = "%Y%m%dT%H%M%S%fZ"
BACKUP_NAME = re.compile(r"board-(\d{8}T\d{12}Z)\.sqlite3")


def snapshot(source_path: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Backup integrity check failed")


def prune_backups(directory: Path, source_path: Path, cutoff: datetime) -> None:
    for candidate in directory.iterdir():
        match = BACKUP_NAME.fullmatch(candidate.name)
        if not match:
            continue
        try:
            created = datetime.strptime(match[1], STAMP).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        info = candidate.lstat()
        if (created >= cutoff or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_nlink != 1
                or candidate.samefile(source_path)):
            continue
        candidate.unlink()


def backup(directory: Path, destination: Path | None = None, *, now: datetime | None = None) -> Path:
    directory = directory.expanduser().resolve()
    source_path = directory / "board.sqlite3"
    if not source_path.is_file():
        raise FileNotFoundError(f"No existing board database at {source_path}; nothing was backed up.")
    now = now or datetime.now(timezone.utc)
    automatic = destination is None
    backup_dir = directory / "backups"
    if automatic:
        if backup_dir.is_symlink():
            raise ValueError("Automatic backup directory must not be a symlink")
        destination = backup_dir / ("board-" + now.strftime(STAMP) + ".sqlite3")
    destination = destination.expanduser().resolve()
    if not automatic and destination.parent == backup_dir.resolve() and BACKUP_NAME.fullmatch(destination.name):
        raise ValueError("Timestamp names in the backup directory are reserved for automatic backups")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting existing files, including the live DB.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        snapshot(source_path, destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    # Explicit/custom destinations are never managed by the retention policy.
    if automatic:
        prune_backups(backup_dir, source_path, now - timedelta(days=30))
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?", type=Path)
    args = parser.parse_args()
    directory = Path(os.environ.get("CORRESPONDENCE_DATA_DIR", "~/Library/Application Support/Correspondence"))
    print(backup(directory, args.destination))


if __name__ == "__main__":
    main()
