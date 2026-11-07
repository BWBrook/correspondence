"""Archive visibility, restoration, durable data and transport boundaries."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from correspondence.store import Conflict, NotFound, Store
from correspondence.web import create_app


@pytest.fixture
def board(tmp_path):
    store = Store(tmp_path)
    store.create_project("old", "Old", "Description", "/tmp/example")
    store.create_thread("Codex", "old", "Review needle", "needle opening", request_id="creation")
    store.post_message("Claude", 1, "needle reply", parent_id=1, model="Fable", request_id="reply")
    store.mark_read("User", 1, 1)
    store.set_thread_status("Codex", 1, "resolved")
    return store


def records(board):
    with board.connect() as con:
        return {table: [dict(row) for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("projects", "threads", "messages", "requests", "read_markers")}


def token(board, project_id):
    with board.connect() as con:
        return con.execute("SELECT state_token FROM projects WHERE id=?", (project_id,)).fetchone()[0]


def without_tokens(snapshot):
    return snapshot | {"projects": [{k: v for k, v in row.items() if k != "state_token"}
                                    for row in snapshot["projects"]]}


def test_archive_restore_preserves_contents_metadata_and_requests(board):
    original = records(board)
    archive = board.archive_project("old", token(board, "old"))
    assert archive["archived_at"] and archive["archived_original_id"] == "old"
    assert archive["id"] == "old-archived-2" and archive["name"] == "Old"
    with pytest.raises(Conflict):
        board.archive_project(archive["id"], archive["state_token"])
    snapshot = records(board)
    for table in ("messages", "requests", "read_markers"):
        assert snapshot[table] == original[table]
    assert snapshot["threads"] == [original["threads"][0] | {"project_id": archive["id"]}]
    restored = board.restore_project(archive["id"], token(board, archive["id"]))
    assert restored["id"] == "old" and restored["archived_at"] is None
    assert restored["archived_original_id"] is None
    assert without_tokens(records(board)) == without_tokens(original)
    assert token(board, "old") != original["projects"][0]["state_token"]
    assert board.create_thread("Codex", "old", "Review needle", "needle opening", request_id="creation")["id"] == 1
    assert board.post_message("Claude", 1, "needle reply", parent_id=1, model="Other", request_id="reply")["id"] == 2
    with board.connect() as con:
        assert not con.execute("PRAGMA foreign_key_check").fetchall()


def test_every_read_surface_selects_exact_archive_view(board):
    archive = board.archive_project("old", token(board, "old"))
    board.create_project("active", "Active")
    active = board.create_thread("Codex", "active", "Review needle", "needle active")
    active_message = active["last_message_id"]
    lists = [lambda flag: board.list_projects("User", archived=flag),
             lambda flag: board.list_threads("User", archived=flag),
             lambda flag: board.list_threads_page("User", archived=flag)["items"],
             lambda flag: board.search_threads("User", "needle", archived=flag)["items"]]
    for read in lists:
        assert len(read(False)) == len(read(True)) == 1
        assert read(False)[0]["id"] in ("active", active["id"])
        assert read(True)[0]["id"] in (archive["id"], 1)
    message_lists = [lambda flag: board.inbox("User", archived=flag)["items"],
                     lambda flag: board.search_messages("User", "needle", archived=flag),
                     lambda flag: board.search_messages_page("User", "needle", archived=flag)["items"]]
    for read in message_lists:
        assert {m["thread_id"] for m in read(False)} == {active["id"]}
        assert {m["thread_id"] for m in read(True)} == {1}
    for direct in [lambda flag: board.read_thread("User", 1, archived=flag),
                   lambda flag: board.read_updates("User", 1, archived=flag),
                   lambda flag: board.get_message("User", 2, archived=flag),
                   lambda flag: board.export_thread("User", 1, archived=flag)]:
        with pytest.raises(NotFound):
            direct(False)
        assert direct(True)
    with pytest.raises(NotFound):
        board.read_thread("User", active["id"], archived=True)
    batch = board.get_messages("User", [2, active_message], include_parents=True)
    assert batch["missing_ids"] == [2] and [m["id"] for m in batch["messages"]] == [active_message]
    batch = board.get_messages("User", [2, active_message], include_parents=True, archived=True)
    assert batch["missing_ids"] == [active_message] and [m["id"] for m in batch["messages"]] == [2, 1]
    assert not board.inbox("User", project_id=archive["id"])["items"]
    assert not board.search_messages_page("User", "needle", project_id="active", archived=True)["items"]


def test_archived_mutations_and_retries_fail_without_changing_state(board):
    archive = board.archive_project("old", token(board, "old"))
    before = records(board)
    writes = [lambda: board.create_project(archive["id"], "Old", "Description", "/tmp/example"),
              lambda: board.create_thread("User", archive["id"], "New", "Forbidden"),
              lambda: board.create_thread("Codex", "old", "Review needle", "needle opening", request_id="creation"),
              lambda: board.post_message("Claude", 1, "needle reply", parent_id=1, request_id="reply"),
              lambda: board.post_message("User", 1, "Forbidden"),
              lambda: board.mark_read("User", 1, 2),
              lambda: board.set_thread_status("User", 1, "resolved"),
              lambda: board.set_thread_status("User", 1, "open")]
    for write in writes:
        with pytest.raises(NotFound):
            write()
        assert records(board) == before
    assert board.mark_all_read("User") == {"threads_marked": 0, "messages_marked": 0}
    assert records(board) == before
    # Reusing the released slug does not make an archived retry accessible.
    board.create_project("old", "Fresh")
    with pytest.raises(NotFound):
        writes[2]()
    active = board.create_thread("Claude", "old", "New discussion", "new body")
    assert board.mark_all_read("User") == {"threads_marked": 1, "messages_marked": 1}
    archived_read = board.read_thread("User", 1, archived=True)
    assert archived_read["thread"]["last_read_message_id"] == 1
    assert board.read_thread("User", active["id"])["thread"]["unread_count"] == 0


def test_restore_collisions_and_repeated_cycles(board):
    first = board.archive_project("old", token(board, "old"))
    board.create_project("old", "Old")
    board.create_project("old-restored-2", "Old (restored 2)")
    before = records(board)
    restored = board.restore_project(first["id"], token(board, first["id"]))
    assert restored["id"] == "old-restored-3" and restored["name"] == "Old (restored 3)"
    assert records(board)["projects"][1:] == before["projects"][1:]
    assert board.read_thread("User", 1)["thread"]["project_id"] == restored["id"]
    assert records(board)["requests"] == before["requests"]
    for _ in range(3):
        archive = board.archive_project(restored["id"], token(board, restored["id"]))
        assert archive["archived_original_id"] == restored["id"]
        again = board.restore_project(archive["id"], token(board, archive["id"]))
        assert again | {"state_token": restored["state_token"]} == restored
        assert again["state_token"] != restored["state_token"]
    # Old request payload remains usable after a collision rename, by permanent request key.
    assert board.create_thread("Codex", "old", "Review needle", "needle opening", request_id="creation")["id"] == 1


def test_archive_collision_and_length_limits(tmp_path):
    board = Store(tmp_path)
    slug, name = "s" * 100, "N" * 200
    board.create_project(slug, name)
    board.create_project(slug[:89] + "-archived-2", "occupied archive candidate")
    archived = board.archive_project(slug, token(board, slug))
    assert len(archived["id"]) <= 100 and archived["id"].endswith("-archived-3")
    board.create_project(slug, name.lower())
    restored = board.restore_project(archived["id"], token(board, archived["id"]))
    assert len(restored["id"]) <= 100 and restored["id"].endswith("-restored-2")
    assert len(restored["name"]) <= 200 and restored["name"].endswith(" (restored 2)")


def test_concurrent_schema1_migration_and_reopen_do_not_rehash(board):
    before = records(board)
    with board.connect(write=True) as con:
        con.execute("ALTER TABLE projects DROP COLUMN archived_at")
        con.execute("ALTER TABLE projects DROP COLUMN archived_original_id")
        con.execute("ALTER TABLE projects DROP COLUMN state_token")
        con.execute("PRAGMA user_version=1")
    gate = Barrier(6)
    def initialize(_):
        gate.wait()
        return Store(board.directory)
    with ThreadPoolExecutor(max_workers=6) as pool:
        stores = list(pool.map(initialize, range(6)))
    assert without_tokens(records(stores[0])) == without_tokens(before)
    archive = stores[0].archive_project("old", token(stores[0], "old"))
    with ThreadPoolExecutor(max_workers=6) as pool:
        reopened = list(pool.map(lambda _: Store(board.directory), range(6)))
    assert records(reopened[0])["requests"] == before["requests"]
    with reopened[0].connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 9
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
    assert reopened[0].list_projects("User", archived=True)[0]["id"] == archive["id"]


def test_simultaneous_archive_restore_calls_are_atomic(board):
    expected = token(board, "old")
    def archive(_):
        try:
            return board.archive_project("old", expected)
        except (NotFound, Conflict):
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(archive, range(4)))
    assert sum(item is not None for item in results) == 1
    result = next(item for item in results if item)
    assert len(board.list_projects("User", archived=True)) == 1
    def restore(_):
        try:
            return board.restore_project(result["id"], result["state_token"])
        except (NotFound, Conflict):
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(restore, range(4)))
    assert sum(item is not None for item in results) == 1
    assert len(board.list_projects("User")) == 1 and not board.list_projects("User", archived=True)
    assert len(records(board)["messages"]) == 3
    with board.connect() as con:
        assert not con.execute("PRAGMA foreign_key_check").fetchall()


def test_http_archive_contract_and_boundary(board):
    headers = {"X-Correspondence-Client": "browser", "Origin": "http://127.0.0.1:8765"}
    with TestClient(create_app(board.directory), base_url="http://127.0.0.1:8765") as client:
        assert client.post("/api/projects/old/archive", json={}).status_code == 403
        assert client.post("/api/projects/old/archive", json={"extra": True}, headers=headers).status_code == 422
        assert client.post("/api/projects/old/archive", json={}, headers=headers | {"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/projects/old/archive", json={}, headers=headers).status_code == 422
        assert client.post("/api/projects/old/archive", json={"expected_state": "wrong"}, headers=headers).status_code == 409
        archived = client.post("/api/projects/old/archive", json={"expected_state": token(board, "old")}, headers=headers).json()
        assert client.get("/api/projects").json() == []
        assert client.get("/api/projects?archived=true").json()[0]["id"] == archived["id"]
        for endpoint in ("/api/threads/1", "/api/messages/2", "/api/threads/1/export"):
            assert client.get(endpoint).status_code == 404
            assert client.get(endpoint + "?archived=true").status_code == 200
        assert client.get("/api/threads").json() == []
        assert len(client.get("/api/threads?archived=true").json()) == 1
        assert client.get("/api/search?q=needle").json() == []
        assert len(client.get("/api/search?q=needle&archived=true").json()) == 3
        assert client.post("/api/threads/1/read", json={"message_id": 2}, headers=headers).status_code == 404
        assert client.post(f"/api/projects/{archived['id']}/restore", json={}, headers=headers).status_code == 422
        assert client.post(f"/api/projects/{archived['id']}/restore", json={"expected_state": "wrong"}, headers=headers).status_code == 409
        restored = client.post(f"/api/projects/{archived['id']}/restore", json={"expected_state": archived["state_token"]}, headers=headers)
        assert restored.status_code == 200 and restored.json()["id"] == "old"


def test_real_mcp_archive_contract(board):
    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "correspondence.mcp_server"],
            env={**os.environ, "CORRESPONDENCE_DATA_DIR": str(board.directory), "CORRESPONDENCE_AUTHOR": "Codex"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                schema = {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}
                reads = ("list_projects", "list_threads", "inbox", "read_updates", "read_thread", "get_message",
                         "get_messages", "search_messages", "search_threads", "export_thread")
                assert all(schema[name]["properties"]["archived"]["default"] is False for name in reads)
                assert (await session.call_tool("archive_project", {"project_id": "old"})).isError
                assert (await session.call_tool("archive_project", {"project_id": "old", "expected_state": "wrong"})).isError
                result = await session.call_tool("archive_project", {"project_id": "old", "expected_state": token(board, "old")})
                assert not result.isError
                archived = result.structuredContent
                assert archived["archived_original_id"] == "old"
                assert (await session.call_tool("list_projects", {})).structuredContent["items"] == []
                for tool, args in [("read_thread", {"thread_id": 1}), ("read_updates", {"thread_id": 1}),
                                   ("get_message", {"message_id": 2}), ("export_thread", {"thread_id": 1})]:
                    assert (await session.call_tool(tool, args)).isError
                    assert not (await session.call_tool(tool, args | {"archived": True})).isError
                for tool, args in [("list_projects", {}), ("list_threads", {}), ("inbox", {}),
                                   ("search_messages", {"q": "needle"}), ("search_threads", {"q": "needle"})]:
                    assert not (await session.call_tool(tool, args)).structuredContent["items"]
                    assert (await session.call_tool(tool, args | {"archived": True})).structuredContent["items"]
                batch = await session.call_tool("get_messages", {"message_ids": [2]})
                assert batch.structuredContent["missing_ids"] == [2]
                batch = await session.call_tool("get_messages", {"message_ids": [2], "archived": True, "include_parents": True})
                assert [m["id"] for m in batch.structuredContent["messages"]] == [2, 1]
                assert (await session.call_tool("post_message", {"thread_id": 1, "body": "Forbidden"})).isError
                assert (await session.call_tool("restore_project", {"project_id": archived["id"]})).isError
                assert (await session.call_tool("restore_project", {"project_id": archived["id"], "expected_state": "wrong"})).isError
                restored = await session.call_tool("restore_project", {"project_id": archived["id"], "expected_state": archived["state_token"]})
                assert restored.structuredContent["id"] == "old"
    asyncio.run(run())


def test_stale_actions_cannot_target_reused_slug_or_later_generation(board):
    original_token = token(board, "old")
    assert board.create_project("old", "Old", "Description", "/tmp/example")["state_token"] == original_token
    first_archive = board.archive_project("old", original_token)
    replacement = board.create_project("old", "Replacement")
    before = records(board)
    with pytest.raises(Conflict):
        board.archive_project("old", original_token)
    assert records(board) == before
    assert replacement["state_token"] != original_token
    restored = board.restore_project(first_archive["id"], first_archive["state_token"])
    assert restored["id"] == "old-restored-2"
    second_archive = board.archive_project(restored["id"], restored["state_token"])
    restored_again = board.restore_project(second_archive["id"], second_archive["state_token"])
    # A delayed archive must not archive the same section after its restore.
    before = records(board)
    with pytest.raises(Conflict):
        board.archive_project(restored_again["id"], restored["state_token"])
    assert records(board) == before
    third_archive = board.archive_project(restored_again["id"], restored_again["state_token"])
    assert third_archive["id"] == second_archive["id"]
    # Reusing an archive slug must not make an earlier restore valid again (ABA).
    before = records(board)
    with pytest.raises(Conflict):
        board.restore_project(second_archive["id"], second_archive["state_token"])
    assert records(board) == before
    assert len({original_token, first_archive["state_token"], restored["state_token"], second_archive["state_token"],
                restored_again["state_token"], third_archive["state_token"], replacement["state_token"]}) == 7


def test_schema2_upgrade_generates_tokens_once_and_preserves_archives(board):
    archived = board.archive_project("old", token(board, "old"))
    board.create_project("new", "New")
    before = records(board)
    with board.connect(write=True) as con:
        con.execute("ALTER TABLE projects DROP COLUMN state_token")
        con.execute("PRAGMA user_version=2")
    gate = Barrier(4)
    def initialize(_):
        gate.wait()
        return Store(board.directory)
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(initialize, range(4)))
    after = records(stores[0])
    assert without_tokens(after) == without_tokens(before)
    assert len({row["state_token"] for row in after["projects"]}) == 2
    assert all(len(row["state_token"]) == 32 for row in after["projects"])
    assert records(Store(board.directory)) == after
    assert stores[0].list_projects("User", archived=True)[0]["id"] == archived["id"]
    with board.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 9
