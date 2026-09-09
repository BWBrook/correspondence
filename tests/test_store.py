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


@pytest.fixture
def long_board(board):
    primary = thread(board)
    other = thread(board, "A different discussion")
    ids = [board.read_thread("Fable", primary)["messages"][0]["id"]]
    for i in range(1, 275):
        body = "🦘" * 700 if i % 23 == 0 else f"Message {i}: évidence. " * 12
        message = board.post_message("Fable" if i % 3 == 0 else "Astra", primary, body)
        ids.append(message["id"])
        if i % 5 == 0:
            board.post_message("Barry", other, "Interleave global message ids")
    return board, primary, ids


def test_long_thread_bidirectional_count_and_byte_pages(long_board):
    board, thread_id, expected = long_board

    def check_page(page, limit, budget):
        messages = page["messages"]
        ids = [m["id"] for m in messages]
        assert ids == sorted(ids) and len(ids) <= limit
        assert len(ids) == 1 or sum(len(m["body"].encode("utf-8")) for m in messages) <= budget
        assert page["next_after"] == ids[-1] and page["previous_before"] == ids[0]
        assert page["has_before"] == (ids[0] != expected[0])
        assert page["has_more"] == (ids[-1] != expected[-1])
        return ids

    for limit in [1, 31, 500]:
        for budget in [1000, 5000, 200_000]:
            page = board.read_thread("Fable", thread_id, limit=limit, max_bytes=budget)
            forward = []
            while True:
                forward.extend(check_page(page, limit, budget))
                if not page["has_more"]:
                    break
                page = board.read_thread("Fable", thread_id, page["next_after"], limit, budget)
            assert forward == expected
            page = board.read_thread("Fable", thread_id, limit=limit, max_bytes=budget, mode="latest")
            backwards = []
            while True:
                backwards = check_page(page, limit, budget) + backwards
                if not page["has_before"]:
                    break
                page = board.read_thread("Fable", thread_id, limit=limit, max_bytes=budget, before=page["previous_before"])
            assert backwards == expected
    assert board.read_thread("Fable", thread_id)["thread"]["last_read_message_id"] == 0


def test_unread_entry_skips_own_posts_and_latest_fallback(long_board):
    board, thread_id, ids = long_board
    board.mark_read("Fable", thread_id, ids[119])
    # Index 120 is Fable's own post; unread starts at index 121.
    page = board.read_thread("Fable", thread_id, limit=5, mode="unread")
    assert page["thread"]["first_unread_message_id"] == ids[121]
    assert [m["id"] for m in page["messages"]] == ids[121:126]
    assert page["thread"]["last_read_message_id"] == ids[119]
    assert page["thread"]["last_message_id"] == ids[-1]
    assert board.list_threads("Fable", "demo", unread_only=True)[0]["first_unread_message_id"] == ids[121]
    board.mark_read("Fable", thread_id, ids[-1])
    own = board.post_message("Fable", thread_id, "My latest post does not create unread work")
    fallback = board.read_thread("Fable", thread_id, limit=3, mode="unread")
    assert fallback == board.read_thread("Fable", thread_id, limit=3, mode="latest")
    assert fallback["messages"][-1]["id"] == own["id"]
    assert fallback["thread"]["first_unread_message_id"] is None
    assert fallback["thread"]["unread_count"] == 0


def test_exact_message_lookup_and_navigation_validation(board):
    from correspondence.store import NotFound
    first = thread(board)
    second = thread(board)
    message = board.post_message("Astra", second, "Exact target")
    result = board.get_message("Fable", message["id"])
    assert result["message"] == message and result["thread"]["id"] == second
    assert result["thread"]["last_read_message_id"] == 0
    with pytest.raises(NotFound):
        board.get_message("Fable", message["id"] + 1)
    invalid = [{"mode": "unknown"}, {"mode": "latest", "after": 1},
               {"mode": "unread", "after": 1}, {"mode": "latest", "before": 2},
               {"mode": "unread", "before": 2}, {"after": 1, "before": 2},
               {"before": 0}, {"before": -1}]
    for kwargs in invalid:
        with pytest.raises(ValueError):
            board.read_thread("Fable", first, **kwargs)
    empty = board.read_thread("Fable", first, before=1)
    assert empty["messages"] == [] and empty["previous_before"] is None
    assert not empty["has_more"] and not empty["has_before"]
    empty = board.read_thread("Fable", first, after=message["id"])
    assert empty["messages"] == [] and empty["next_after"] == message["id"]


def test_read_snapshot_and_explicit_whole_thread_acknowledgement(board, monkeypatch):
    thread_id = thread(board)
    original = board._thread
    arrived = []

    def race_after_metadata(con, requested_thread, author):
        metadata = original(con, requested_thread, author)
        if not arrived:
            arrived.append(board.post_message("Astra", thread_id, "Arrived after snapshot began"))
        return metadata

    monkeypatch.setattr(board, "_thread", race_after_metadata)
    snapshot = board.read_thread("Fable", thread_id, mode="latest")
    assert len(snapshot["messages"]) == 1
    assert snapshot["thread"]["last_message_id"] < arrived[0]["id"]
    assert snapshot["thread"]["message_count"] == 1
    monkeypatch.setattr(board, "_thread", original)
    board.mark_read("Fable", thread_id, snapshot["thread"]["last_message_id"])
    current = board.read_thread("Fable", thread_id, mode="unread")
    assert current["thread"]["unread_count"] == 1
    assert current["messages"][0]["id"] == arrived[0]["id"]
