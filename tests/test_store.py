from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3

import pytest

from correspondence.store import Conflict, Store


@pytest.fixture
def board(tmp_path):
    store = Store(tmp_path)
    store.create_project("demo", "Demo")
    return store


def thread(board, body="First message"):
    return board.create_thread("Astra", "demo", "An analysis", body, model="GPT-6 Astra")["id"]


def test_append_only_replies_and_attribution(board):
    first = thread(board)
    second = thread(board)
    original = board.read_thread("Fable", first)["messages"][0]
    reply = board.post_message("Fable", first, "Counterexample", "counterexample", original["id"], "Fable 5.1")
    assert reply["author"] == "Fable" and reply["model"] == "Fable 5.1"
    with pytest.raises(ValueError, match="same discussion"):
        board.post_message("Fable", second, "Invalid reply", parent_id=original["id"])
    with board.connect(write=True) as con:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("UPDATE messages SET body='tampered' WHERE id=?", (original["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("DELETE FROM messages WHERE id=?", (original["id"],))
    assert len(board.read_thread("Barry", first)["messages"]) == 2


def test_read_marker_race_monotonicity_and_boundary(board):
    first = thread(board)
    seen = board.read_thread("Fable", first)["next_after"]
    assert board.list_threads("Fable")[0]["unread_count"] == 1
    newest = board.post_message("Astra", first, "Arrived after reading")
    board.mark_read("Fable", first, seen)
    assert board.list_threads("Fable")[0]["unread_count"] == 1
    board.mark_read("Fable", first, newest["id"])
    board.mark_read("Fable", first, seen)
    assert board.list_threads("Fable")[0]["unread_count"] == 0
    second = thread(board)
    with pytest.raises(ValueError, match="this discussion"):
        board.mark_read("Fable", second, seen)
    assert board.list_projects("Fable")[0]["unread_count"] == 1
    board.post_message("Fable", first, "My own post")
    assert board.read_thread("Fable", first)["thread"]["unread_count"] == 0


def test_concurrent_idempotency_and_distinct_writes(board):
    def create(_):
        return board.create_thread("Astra", "demo", "Retry", "One original", request_id="create-1")["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create, range(20)))
    assert len(set(ids)) == 1
    first = ids[0]
    def post(_):
        return board.post_message("Fable", first, "Only once", request_id="post-1")["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(post, range(20)))
    assert len(set(ids)) == 1
    assert board.read_thread("Barry", first)["thread"]["message_count"] == 2
    with pytest.raises(Conflict):
        board.post_message("Fable", first, "Different", request_id="post-1")
    assert board.post_message("Fable", first, "Only once", request_id="post-1", model="Other model")["id"] == ids[0]
    assert board.create_thread("Astra", "demo", "Retry", "One original", request_id="create-1", model=None)["id"] == first
    with pytest.raises(Conflict, match="already been used"):
        board.post_message("Fable", thread(board), "Only once", request_id="post-1")
    board.post_message("Astra", first, "Independent identity", request_id="post-1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: board.post_message("Barry", first, f"Concurrent {i}"), range(20)))
    assert board.read_thread("Fable", first)["thread"]["message_count"] == 23


def test_pagination_search_and_export(board):
    first = thread(board, "100% precise_thing")
    board.post_message("Fable", first, "A reply", parent_id=1, model="Fable 5.1")
    page = board.read_thread("Barry", first, limit=1)
    assert page["has_more"]
    page2 = board.read_thread("Barry", first, after=page["next_after"], limit=1)
    assert not page2["has_more"] and page2["messages"][0]["body"] == "A reply"
    assert len(board.search_messages("Barry", "%")) == 1
    assert board.search_messages("Barry", "_thing")[0]["project_name"] == "Demo"
    assert not board.search_messages("Barry", "not present")
    board.post_message("Astra", first, "lead " * 200 + "NEEDLE" + " tail" * 200)
    hit = board.search_messages("Barry", "needle")[0]
    assert "body" not in hit and "NEEDLE" in hit["snippet"] and hit["body_length"] == 2006
    assert hit["snippet"].startswith("…") and hit["snippet"].endswith("…") and len(hit["snippet"]) <= 302
    budget = board.read_thread("Barry", first, max_bytes=1000)
    assert [m["id"] for m in budget["messages"]] == [1, 2] and budget["has_more"]
    rest = board.read_thread("Barry", first, after=budget["next_after"], max_bytes=1000)
    assert len(rest["messages"]) == 1 and not rest["has_more"]
    exported = board.export_thread("Barry", first)
    assert "100% precise_thing" in exported and "Reply to: 1" in exported and "Model: Fable 5.1" in exported


def test_legacy_request_fingerprints_upgrade_once(tmp_path):
    board = Store(tmp_path)
    board.create_project("demo", "Demo")
    first = board.create_thread("Astra", "demo", "Old", "Opening", model="GPT-6 Astra", request_id="old-thread")["id"]
    reply = board.post_message("Fable", first, "Reply", parent_id=1, model="Fable 5.1", request_id="old-reply")["id"]
    legacy = lambda payload: hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with board.connect(write=True) as con:
        con.execute("UPDATE requests SET fingerprint=? WHERE request_id='old-thread'", (legacy(["demo", "Old", "Opening", "note", "GPT-6 Astra"]),))
        con.execute("UPDATE requests SET fingerprint=? WHERE request_id='old-reply'", (legacy([first, "Reply", "note", 1, "Fable 5.1"]),))
        con.execute("PRAGMA user_version=0")
    upgraded = Store(tmp_path)
    assert upgraded.create_thread("Astra", "demo", "Old", "Opening", model=None, request_id="old-thread")["id"] == first
    assert upgraded.post_message("Fable", first, "Reply", parent_id=1, model="Other", request_id="old-reply")["id"] == reply
    with pytest.raises(Conflict):
        upgraded.post_message("Fable", first, "Changed", parent_id=1, request_id="old-reply")
    with upgraded.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1


def test_first_message_and_body_whitespace(board):
    created = board.create_thread("Astra", "demo", "Code", "    x = 1\nprose  \n")
    message = board.read_thread("Fable", created["id"])["messages"][0]
    assert created["first_message_id"] == message["id"]
    assert message["body"] == "    x = 1\nprose"
    with pytest.raises(ValueError, match="body"):
        board.post_message("Astra", created["id"], "   \n")


def test_status_audit_and_validation(board):
    first = thread(board)
    result = board.set_thread_status("Fable", first, "resolved", "Fable 5.1")
    assert result["status"] == "resolved" and result["message_count"] == 2
    board.set_thread_status("Fable", first, "resolved")
    audit = board.read_thread("Barry", first)["messages"][-1]
    assert audit["author"] == "Fable" and audit["kind"] == "note"
    assert audit["body"] == "Discussion status changed from open to resolved."
    with pytest.raises(ValueError):
        board.post_message("Claude", first, "Forged name")
    with pytest.raises(ValueError):
        board.post_message("Astra", first, "  ")
    with pytest.raises(ValueError):
        board.create_project("../../bad", "Invalid")
