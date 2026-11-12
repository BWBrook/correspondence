#!/usr/bin/env python3
"""Create a checked SQLite snapshot; retain 30 days of automatic backups; copy them off the host.

Off-host copies go to each destination listed, one per line, in the data
directory's ``offsite-backup`` file: ``host:/path`` for a machine reached over
SSH, or a local path. The file stays out of the repository because it names
machines. Each automatic run sends a gzipped copy of the new snapshot and of
the turn ledger (``turns.jsonl``, which is not in the database), then removes
copies older than 30 days from that destination. A destination that cannot be
reached is reported and retried with the next day's snapshot.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile


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


RETAIN_DAYS = 30


def destinations(directory: Path) -> list[str]:
    path = directory / "offsite-backup"
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def _split(destination: str) -> tuple[str | None, str]:
    """``host:/path`` names a machine reached over SSH; anything else is a local path."""
    host, separator, path = destination.partition(":")
    return (host, path) if separator and "/" not in host else (None, destination)


def offsite(directory: Path, snapshot_path: Path, stamp: str) -> list[str]:
    """Send gzipped copies of a snapshot and the turn ledger to each destination; return the failures."""
    targets = destinations(directory)
    if not targets:
        return []
    failures = []
    with tempfile.TemporaryDirectory(dir=directory) as staging:
        staged = [Path(staging) / f"board-{stamp}.sqlite3.gz", Path(staging) / f"turns-{stamp}.jsonl.gz"]
        with open(snapshot_path, "rb") as source, gzip.open(staged[0], "wb", compresslevel=9) as target:
            target.write(source.read())
        ledger = directory / "turns.jsonl"
        with open(ledger, "rb") if ledger.is_file() else open(os.devnull, "rb") as source:
            fcntl.flock(source, fcntl.LOCK_SH)  # the turn hook appends under an exclusive lock
            data = source.read()
        with gzip.open(staged[1], "wb", compresslevel=9) as target:
            target.write(data)
        for path in staged:
            path.chmod(0o600)
        for destination in targets:
            host, folder = _split(destination)
            folder = shlex.quote(folder)
            prepare = (f"mkdir -p {folder} && chmod 700 {folder} && find {folder} -maxdepth 1 -type f "
                       f"\\( -name 'board-*.sqlite3.gz' -o -name 'turns-*.jsonl.gz' \\) -mtime +{RETAIN_DAYS} -delete")
            shell = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, prepare] if host else ["sh", "-c", prepare]
            for command in (shell, ["rsync", "-t", *map(str, staged), destination.rstrip("/") + "/"]):
                result = subprocess.run(command, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
                if result.returncode != 0:
                    failures.append(f"{destination}: {(result.stderr or result.stdout).strip()[-300:]}")
                    break
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?", type=Path)
    args = parser.parse_args()
    directory = Path(os.environ.get("CORRESPONDENCE_DATA_DIR", "~/Library/Application Support/Correspondence"))
    now = datetime.now(timezone.utc)
    result = backup(directory, args.destination, now=now)
    print(result)
    if args.destination is None:
        failures = offsite(directory.expanduser().resolve(), result, now.strftime("%Y%m%dT%H%M%SZ"))
        for line in failures:
            print(f"Off-host copy failed: {line}", file=sys.stderr)
        if failures:
            sys.exit(1)


if __name__ == "__main__":
    main()
