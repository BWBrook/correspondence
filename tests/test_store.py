from concurrent.futures import ThreadPoolExecutor
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
    exported = board.export_thread("Barry", first)
    assert "100% precise_thing" in exported and "Reply to: 1" in exported and "Model: Fable 5.1" in exported


def test_status_audit_and_validation(board):
    first = thread(board)
    result = board.set_thread_status("Fable", first, "resolved", "Fable 5.1")
    assert result["status"] == "resolved" and result["message_count"] == 2
    board.set_thread_status("Fable", first, "resolved")
    audit = board.read_thread("Barry", first)["messages"][-1]
    assert audit["author"] == "Fable" and audit["kind"] == "evidence"
    assert audit["body"] == "Discussion status changed from open to resolved."
    with pytest.raises(ValueError):
        board.post_message("Claude", first, "Forged name")
    with pytest.raises(ValueError):
        board.post_message("Astra", first, "  ")
    with pytest.raises(ValueError):
        board.create_project("../../bad", "Invalid")
