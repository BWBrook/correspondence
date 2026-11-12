from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sqlite3

import pytest

spec = importlib.util.spec_from_file_location("backup", Path(__file__).parents[1] / "scripts/backup.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path):
    with sqlite3.connect(tmp_path / "board.sqlite3") as con:
        con.execute("CREATE TABLE evidence (body TEXT)")
        con.execute("INSERT INTO evidence VALUES ('preserved')")
    return tmp_path


def old_file(directory, age=31):
    folder = directory / "backups"
    folder.mkdir(exist_ok=True)
    name = "board-" + (NOW - timedelta(days=age)).strftime(backup.STAMP) + ".sqlite3"
    target = folder / name
    target.write_text("old snapshot")
    return target


def test_snapshot_integrity_and_retention_boundary(database):
    expired = old_file(database)
    boundary = old_file(database, 30)
    recent = old_file(database, 29)
    result = backup.backup(database, now=NOW)
    with sqlite3.connect(result) as con:
        assert con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert con.execute("SELECT body FROM evidence").fetchone() == ("preserved",)
    assert result.stat().st_mode & 0o777 == 0o600
    assert not expired.exists()
    assert boundary.exists() and recent.exists()


def test_failure_removes_partial_snapshot_and_never_prunes(database, monkeypatch):
    expired = old_file(database)
    def fail(source, destination):
        destination.write_text("partial")
        raise RuntimeError("Backup integrity check failed")
    monkeypatch.setattr(backup, "snapshot", fail)
    with pytest.raises(RuntimeError, match="integrity"):
        backup.backup(database, now=NOW)
    assert list(expired.parent.iterdir()) == [expired]


def test_corrupt_source_preserves_existing_backups(database):
    expired = old_file(database)
    (database / "board.sqlite3").write_text("corrupted source")
    with pytest.raises(sqlite3.DatabaseError):
        backup.backup(database, now=NOW)
    assert list(expired.parent.iterdir()) == [expired]


def test_retention_ignores_unrelated_invalid_names_links_and_directories(database):
    folder = database / "backups"
    folder.mkdir()
    protected = []
    for name in ["manual.sqlite3", "previous-launch-agent.plist", "board-20261301T120000000000Z.sqlite3"]:
        file = folder / name
        file.write_text("keep")
        protected.append(file)
    link = old_file(database, 32)
    link.unlink()
    link.symlink_to(database / "board.sqlite3")
    hardlink = old_file(database, 33)
    hardlink.unlink()
    hardlink.hardlink_to(database / "board.sqlite3")
    subdir = old_file(database, 34)
    subdir.unlink()
    subdir.mkdir()
    backup.backup(database, now=NOW)
    assert all(file.exists() for file in protected + [link, hardlink, subdir])
    with sqlite3.connect(database / "board.sqlite3") as con:
        assert con.execute("SELECT body FROM evidence").fetchone() == ("preserved",)


def test_retention_does_not_touch_other_owners(database, monkeypatch):
    expired = old_file(database)
    monkeypatch.setattr(backup.os, "getuid", lambda: expired.stat().st_uid + 1)
    backup.backup(database, now=NOW)
    assert expired.exists()


def test_custom_destination_never_prunes_and_cannot_enter_managed_namespace(database):
    expired = old_file(database)
    custom = database / "backups/manual.sqlite3"
    backup.backup(database, custom, now=NOW)
    assert custom.exists() and expired.exists()
    reserved = database / "backups" / ("board-" + NOW.strftime(backup.STAMP) + ".sqlite3")
    with pytest.raises(ValueError, match="reserved"):
        backup.backup(database, reserved, now=NOW)
    assert not reserved.exists()
    backup.backup(database, now=NOW)
    assert custom.exists()


def test_never_overwrites_existing_destination_or_live_database(database):
    live = database / "board.sqlite3"
    before = live.read_bytes()
    with pytest.raises(FileExistsError):
        backup.backup(database, live, now=NOW)
    assert live.read_bytes() == before
    result = backup.backup(database, now=NOW)
    with pytest.raises(FileExistsError):
        backup.backup(database, now=NOW)
    assert result.exists()


def test_missing_source_and_redirected_default_directory_are_refused(tmp_path, database):
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        backup.backup(missing, now=NOW)
    assert not missing.exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    (database / "backups").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        backup.backup(database, now=NOW)
    assert list(outside.iterdir()) == []


def test_offsite_copies_snapshot_and_ledger_prunes_old_copies_and_reports_failures(database, tmp_path_factory):
    import gzip
    import os
    remote = tmp_path_factory.mktemp("remote")
    stale = remote / "board-20260801T000000Z.sqlite3.gz"
    stale.write_bytes(b"old")
    os.utime(stale, (0, 0))
    unrelated = remote / "notes.txt"
    unrelated.write_text("kept")
    os.utime(unrelated, (0, 0))
    (database / "turns.jsonl").write_text('{"recorded_at": "2026-09-14T12:00:00+00:00"}\n')
    blocked = database / "not-a-directory"
    blocked.write_text("")
    (database / "offsite-backup").write_text(f"# destinations\n{remote}\n{blocked}/sub\n")
    snapshot = backup.backup(database, now=NOW)
    failures = backup.offsite(database, snapshot, "20260914T120000Z")
    assert len(failures) == 1 and failures[0].startswith(f"{blocked}/sub:")
    assert gzip.decompress((remote / "board-20260914T120000Z.sqlite3.gz").read_bytes()) == snapshot.read_bytes()
    assert gzip.decompress((remote / "turns-20260914T120000Z.jsonl.gz").read_bytes()) == (database / "turns.jsonl").read_bytes()
    assert not stale.exists() and unrelated.exists()
