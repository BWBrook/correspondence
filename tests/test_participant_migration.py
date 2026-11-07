"""Schema-3 fixtures are historical SQL, never new writes through retired identities."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from correspondence.mcp_server import build_server
from correspondence.store import Conflict, Store
from correspondence.web import create_app


SCHEMA3 = """
CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,
 repo_path TEXT,created_at TEXT NOT NULL,archived_at TEXT,archived_original_id TEXT,state_token TEXT);
CREATE TABLE threads(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
 title TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE messages(id INTEGER PRIMARY KEY,thread_id INTEGER NOT NULL REFERENCES threads(id),
 author TEXT NOT NULL CHECK(author IN ('Barry','Astra','Fable')),model TEXT,body TEXT NOT NULL,
 kind TEXT NOT NULL,parent_id INTEGER REFERENCES messages(id),created_at TEXT NOT NULL);
CREATE INDEX messages_thread ON messages(thread_id,id);
CREATE TABLE read_markers(author TEXT NOT NULL,thread_id INTEGER NOT NULL REFERENCES threads(id),
 message_id INTEGER NOT NULL REFERENCES messages(id),PRIMARY KEY(author,thread_id));
CREATE TABLE requests(author TEXT NOT NULL,request_id TEXT NOT NULL,operation TEXT NOT NULL,
 fingerprint TEXT NOT NULL,result_id INTEGER NOT NULL,PRIMARY KEY(author,request_id));
CREATE TRIGGER messages_no_update BEFORE UPDATE ON messages BEGIN SELECT RAISE(ABORT,'Messages are append-only'); END;
CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'Messages are append-only'); END;
PRAGMA user_version=3;
"""


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "board.sqlite3"
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA3)
        con.executemany("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?)", [
            ("demo", "Demo", "Original description", "/tmp/repo", "2026-09-01", None, None, "unchanged"),
            ("archive", "Archive", "", None, "2026-08-01", "2026-09-01", "former", "archive-token")])
        con.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?)", [
            (7,"demo","Review","open","2026-09-01","2026-09-02"),
            (8,"demo","Own only","resolved","2026-09-01","2026-09-02"),
            (9,"archive","Archived","resolved","2026-08-01","2026-08-02")])
        rows = [(10,7,"Astra","GPT-6 Astra","opening","note",None,"2026-09-01"),
                (20,7,"Fable","Fable 5.1","reply","note",10,"2026-09-02"),
                (30,7,"Barry",None,"human","decision",20,"2026-09-03"),
                (40,7,"Fable","Fable 5.1","later","note",20,"2026-09-04"),
                (50,7,"Astra","GPT-6 Astra","answer","evidence",30,"2026-09-05"),
                (60,7,"Barry",None,"more","note",None,"2026-09-06"),
                (70,8,"Fable","Fable 5.1","alone","note",None,"2026-09-07"),
                (100,9,"Fable","Fable 5.1","archived","note",None,"2026-08-01")]
        con.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO read_markers VALUES (?,?,?)", [
            ("Fable",7,20),("Claude",7,30),("Astra",7,10),("Codex",7,20),("Barry",7,10),("Fable",9,100)])
        con.executemany("INSERT INTO requests VALUES (?,?,?,?,?)", [
            ("Astra","creation","create_thread",Store._fingerprint(["demo","Review","opening","note"]),7),
            ("Fable","reply","post_message",Store._fingerprint([7,"reply","note",10]),20)])
    return tmp_path


def snapshot(directory):
    with sqlite3.connect(directory / "board.sqlite3") as con:
        return {table: con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("projects","threads","messages","read_markers","requests")}


def as_user(rows):
    """Rows as version 8 stores them: the human participant becomes the token User."""
    return [tuple("User" if value == "Barry" else value for value in row) for row in rows]


def test_preserves_all_historical_rows_and_merges_acknowledgements(legacy):
    old = snapshot(legacy)
    board = Store(legacy)
    new = snapshot(legacy)
    for table in ("projects","threads","requests"):
        assert new[table] == old[table]
    assert new["messages"] == as_user(old["messages"])
    assert board.user_name() == "Barry"
    assert sorted(new["read_markers"]) == [("Claude",7,30),("Claude",9,100),("Codex",7,20),("User",7,10)]
    assert snapshot(Store(legacy).directory) == new
    with board.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 9
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert {row["name"] for row in con.execute("PRAGMA index_list(messages)")} == {"messages_thread"}
        for sql in ("UPDATE messages SET body='changed' WHERE id=10", "DELETE FROM messages WHERE id=10"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                con.execute(sql)
    reply = board.post_message("Claude", 7, "New reply", parent_id=20, model="New model")
    assert reply["id"] == 101 and reply["author"] == "Claude" and reply["parent_id"] == 20
    assert "Fable" in board.export_thread("Codex",7) and "GPT-6 Astra" in board.export_thread("Claude",7)


@pytest.mark.parametrize("participant,own", [("User",{"User"}),("Claude",{"Claude","Fable"}),("Codex",{"Codex","Astra"})])
def test_alias_own_post_rules_on_all_count_and_unread_surfaces(legacy, participant, own):
    board = Store(legacy)
    board.post_message("Claude",7,"Current Claude")
    board.post_message("Codex",7,"Current Codex")
    all_messages = board.inbox(participant)["items"]
    marker_map = {7:{"User":10,"Claude":30,"Codex":20}[participant],8:0}
    unread = [m for m in all_messages if m["id"] > marker_map[m["thread_id"]] and m["author"] not in own]
    expected_ids = [m["id"] for m in unread]
    expected_threads = {m["thread_id"] for m in unread}
    assert [m["id"] for m in board.inbox(participant,unread_only=True)["items"]] == expected_ids
    assert {t["id"] for t in board.list_threads(participant,unread_only=True)} == expected_threads
    assert {t["id"] for t in board.list_threads_page(participant,unread_only=True)["items"]} == expected_threads
    assert board.list_projects(participant)[0]["unread_count"] == len(unread)
    for t in board.search_threads(participant,"Review")["items"] + board.list_threads(participant):
        assert set(t["read_markers"]) == set(t["unread_counts"]) == {"User","Claude","Codex"}
        assert t["unread_count"] == sum(m["thread_id"] == t["id"] for m in unread)
    metadata = board.get_message(participant,10)["thread"]
    expected_first = next((m["id"] for m in unread if m["thread_id"] == 7),None)
    assert metadata["first_unread_message_id"] == expected_first
    assert board.read_thread(participant,7,mode="unread")["messages"][0]["id"] == expected_first
    assert board.read_updates(participant,7)["thread"] == metadata
    assert board.read_thread(participant,9,archived=True)["thread"]["read_markers"]["Claude"] == 100
    assert board.mark_all_read(participant) == {"threads_marked":len(expected_threads),"messages_marked":len(unread)}
    assert not board.inbox(participant,unread_only=True)["items"]


def test_current_author_filters_include_aliases_historical_filters_are_exact(legacy):
    board = Store(legacy)
    claude = board.post_message("Claude",7,"reply")
    codex = board.post_message("Codex",7,"reply")
    for name,expected in [("Claude",[claude["id"],20]),("Fable",[20]),("Codex",[codex["id"]]),("Astra",[])]:
        result = board.search_messages_page("User","reply",author_filter=name)
        assert [m["id"] for m in result["items"]] == expected
    assert not board.search_messages_page("Claude","reply",author_filter="Fable",after=20)["items"]
    assert board.search_messages_page("Claude","archived",author_filter="Claude",archived=True)["items"][0]["author"] == "Fable"


def test_permanent_alias_request_keys_return_original_and_conflict(legacy):
    board = Store(legacy)
    before = snapshot(legacy)
    assert board.create_thread("Codex","demo","Review","opening",request_id="creation")["id"] == 7
    retry = board.post_message("Claude",7,"reply",parent_id=10,request_id="reply")
    assert retry["id"] == 20 and retry["author"] == "Fable"
    assert snapshot(legacy) == before
    with pytest.raises(Conflict):
        board.post_message("Claude",7,"different",parent_id=10,request_id="reply")
    # Both namespaces can pre-exist: never choose one silently when ambiguous.
    new = board.post_message("Claude",7,"reply",parent_id=10)
    with board.connect(write=True) as con:
        con.execute("INSERT INTO requests VALUES (?,?,?,?,?)", ("Claude","reply","post_message",Store._fingerprint([7,"reply","note",10]),new["id"]))
    with pytest.raises(Conflict,match="conflicting"):
        board.post_message("Claude",7,"reply",parent_id=10,request_id="reply")
    archived = board.archive_project("demo",board.list_projects("User")[0]["state_token"])
    from correspondence.store import NotFound
    with pytest.raises(NotFound):
        board.create_thread("Codex","demo","Review","opening",request_id="creation")
    board.restore_project(archived["id"],archived["state_token"])
    assert board.create_thread("Codex","demo","Review","opening",request_id="creation")["id"] == 7


@pytest.mark.parametrize("old", ["Fable","Astra"])
def test_legacy_identity_rejected_by_store_mcp_cli_and_database(legacy, old):
    board = Store(legacy)
    for call in (lambda:board.post_message(old,7,"forbidden"),lambda:board.mark_read(old,7,60),
                 lambda:board.list_projects(old),lambda:build_server(legacy,author=old)):
        with pytest.raises(ValueError,match="Author"):
            call()
    with board.connect(write=True) as con:
        with pytest.raises(sqlite3.IntegrityError,match="current participant"):
            con.execute("INSERT INTO messages(thread_id,author,body,kind,created_at) VALUES (7,?,'forbidden','note','now')",(old,))
        with pytest.raises(sqlite3.IntegrityError,match="CHECK"):
            con.execute("INSERT INTO read_markers VALUES (?,7,60)",(old,))
    script = Path(__file__).resolve().parents[1] / "scripts/mcp-call.py"
    result = subprocess.run([sys.executable,str(script),"--author",old,"list_projects"],text=True,capture_output=True,
                            env={**os.environ,"CORRESPONDENCE_DATA_DIR":str(legacy)},timeout=15)
    assert result.returncode == 2 and json.loads(result.stdout)["error"]["code"] == "invalid_arguments"


def test_migration_rolls_back_entire_rebuild_and_reenables_foreign_keys(legacy):
    before = snapshot(legacy)
    class FailingStore(Store):
        @staticmethod
        def _upgrade_participants(con):
            Store._upgrade_participants(con)
            raise RuntimeError("Injected migration failure")
    with sqlite3.connect(legacy / "board.sqlite3",isolation_level=None) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(RuntimeError,match="Injected"):
            FailingStore._upgrade(con)
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not con.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_new'").fetchall()
    assert snapshot(legacy) == before
    assert Store(legacy).list_projects("Claude")


def test_corrupt_foreign_key_aborts_migration_without_losing_history(legacy):
    with sqlite3.connect(legacy / "board.sqlite3") as con:
        con.execute("INSERT INTO read_markers VALUES ('Barry',8,9999)")
    before = snapshot(legacy)
    with pytest.raises(sqlite3.IntegrityError,match="Foreign key"):
        Store(legacy)
    assert snapshot(legacy) == before
    with sqlite3.connect(legacy / "board.sqlite3") as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3


def test_simultaneous_migration_is_single_atomic_change(legacy):
    before = snapshot(legacy)
    gate = Barrier(6)
    def initialize(_):
        gate.wait()
        return Store(legacy)
    with ThreadPoolExecutor(max_workers=6) as pool:
        stores = list(pool.map(initialize,range(6)))
    after = snapshot(legacy)
    for table in ("threads","projects","requests"):
        assert after[table] == before[table]
    assert after["messages"] == as_user(before["messages"])
    assert all(store.read_thread("Claude",7)["thread"]["last_read_message_id"] == 30 for store in stores)


def test_current_http_posts_and_real_mcp_alias_retry(legacy):
    with TestClient(create_app(legacy),base_url="http://127.0.0.1:8765") as client:
        headers={"X-Correspondence-Client":"browser"}
        historical=client.get("/api/messages/20").json()
        assert historical["message"]["author"] == "Fable"
        assert historical["thread"]["read_markers"]["Claude"] == 30
        result=client.post("/api/threads/7/messages",json={"body":"Browser message"},headers=headers)
        assert result.status_code == 201 and result.json()["author"] == "User"
    async def run():
        params=StdioServerParameters(command=sys.executable,args=["-m","correspondence.mcp_server"],
            env={**os.environ,"CORRESPONDENCE_DATA_DIR":str(legacy),"CORRESPONDENCE_AUTHOR":"Claude"})
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                info=await session.initialize()
                assert "configured author is Claude" in info.instructions
                assert "board for Barry, Claude and Codex" in info.instructions
                tools={tool.name:tool for tool in (await session.list_tools()).tools}
                author_schema=tools["search_messages"].inputSchema["properties"]["author_filter"]
                assert author_schema["anyOf"][0]["enum"] == ["User","Claude","Codex","Astra","Fable"]
                retry=await session.call_tool("post_message",{"thread_id":7,"body":"reply","parent_id":10,"request_id":"reply","compact":False})
                assert retry.structuredContent["author"] == "Fable" and retry.structuredContent["id"] == 20
                result=await session.call_tool("post_message",{"thread_id":7,"body":"Current writer"})
                assert result.structuredContent["author"] == "Claude"
                search=await session.call_tool("search_messages",{"q":"reply","author_filter":"Claude"})
                assert search.structuredContent["items"][0]["author"] == "Fable"
    asyncio.run(run())


@pytest.mark.parametrize("version",[0,1,2])
def test_pre_archive_schema_upgrade_preserves_messages_and_retry_identity(legacy,version):
    with sqlite3.connect(legacy / "board.sqlite3") as con:
        con.execute("ALTER TABLE projects DROP COLUMN state_token")
        if version < 2:
            con.execute("ALTER TABLE projects DROP COLUMN archived_at")
            con.execute("ALTER TABLE projects DROP COLUMN archived_original_id")
        if version == 0:
            con.execute("UPDATE requests SET fingerprint=? WHERE request_id='creation'",
                        (Store._fingerprint(["demo","Review","opening","note","GPT-6 Astra"]),))
            con.execute("UPDATE requests SET fingerprint=? WHERE request_id='reply'",
                        (Store._fingerprint([7,"reply","note",10,"Fable 5.1"]),))
        con.execute(f"PRAGMA user_version={version}")
    before=snapshot(legacy)
    board=Store(legacy)
    after=snapshot(legacy)
    assert after["messages"] == as_user(before["messages"]) and after["threads"] == before["threads"]
    assert [(r[0],r[1],r[2],r[4]) for r in after["requests"]] == [(r[0],r[1],r[2],r[4]) for r in before["requests"]]
    assert board.create_thread("Codex","demo","Review","opening",request_id="creation")["id"] == 7
    assert board.post_message("Claude",7,"reply",parent_id=10,request_id="reply")["id"] == 20
    with board.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 9
        assert not con.execute("PRAGMA foreign_key_check").fetchall()


def test_fresh_simultaneous_initialization(tmp_path):
    gate=Barrier(4)
    def initialize(_):
        gate.wait()
        return Store(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores=list(pool.map(initialize,range(4)))
    stores[0].create_project("fresh","Fresh")
    first=stores[0].create_thread("Codex","fresh","Start","Hello")
    assert first["id"] == 1
    assert stores[-1].get_message("Claude",1)["message"]["author"] == "Codex"


def test_wal_initialization_retries_real_lock_and_restores_timeout(legacy,monkeypatch):
    from threading import Event
    import correspondence.store as module
    blocked=Event()
    original_sleep=module.time.sleep
    def observed_sleep(delay):
        blocked.set()
        original_sleep(delay)
    monkeypatch.setattr(module.time,"sleep",observed_sleep)
    with sqlite3.connect(legacy / "board.sqlite3",isolation_level=None) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM messages").fetchone()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(Store,legacy)
            try:
                assert blocked.wait(3), "WAL initialization should encounter the held read lock"
            finally:
                reader.commit()
            board=future.result(timeout=5)
    with board.connect() as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
        assert con.execute("PRAGMA user_version").fetchone()[0] == 9


def test_wal_lock_retry_is_bounded_and_other_errors_are_not_retried(legacy,monkeypatch):
    import correspondence.store as module
    elapsed=[0.0]
    monkeypatch.setattr(module.time,"monotonic",lambda:elapsed[0])
    monkeypatch.setattr(module.time,"sleep",lambda seconds:elapsed.__setitem__(0,elapsed[0]+seconds))
    with sqlite3.connect(legacy / "board.sqlite3",isolation_level=None) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM messages").fetchone()
        with sqlite3.connect(legacy / "board.sqlite3",isolation_level=None) as contender:
            contender.execute("PRAGMA busy_timeout=100")
            with pytest.raises(sqlite3.OperationalError,match="locked"):
                Store._enable_wal(contender)
            assert elapsed[0] == pytest.approx(0.1)
            assert contender.execute("PRAGMA busy_timeout").fetchone()[0] == 100
        reader.rollback()
    with sqlite3.connect(f"file:{legacy / 'board.sqlite3'}?mode=ro",uri=True,isolation_level=None) as readonly:
        before=elapsed[0]
        with pytest.raises(sqlite3.OperationalError,match="readonly"):
            Store._enable_wal(readonly)
        assert elapsed[0] == before
        assert readonly.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
