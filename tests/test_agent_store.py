"""Agent reads are bounded, resumable, and never acknowledge on a participant's behalf."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json

import pytest

from correspondence.store import NotFound, Store


@pytest.fixture
def board(tmp_path):
    store = Store(tmp_path)
    store.create_project("alpha", "Alpha")
    store.create_project("beta", "Beta")
    return store


def create(board, project="alpha", title="Analysis", author="Astra", body="Opening"):
    return board.create_thread(author, project, title, body)["id"]


def ids(items):
    return [item["id"] for item in items]


def byte_count(items):
    return len(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def markers(board):
    with board.connect() as con:
        return [tuple(row) for row in con.execute("SELECT * FROM read_markers ORDER BY author,thread_id")]


def test_inbox_snapshot_scope_and_independent_checkpoint(board):
    a = create(board)
    b = create(board, "beta")
    own = board.post_message("Fable", a, "Own message")
    reply = board.post_message("Barry", a, "é" * 400)
    first = board.inbox("Fable", limit=2)
    assert ids(first["items"]) == [1, 2]
    assert first["total"] == 4 and first["has_more"] and first["through"] == reply["id"]
    assert first["returned_bytes"] == byte_count(first["items"])
    board.mark_read("Fable", a, reply["id"])
    late = board.post_message("Astra", a, "After captured snapshot")
    second = board.inbox("Fable", after=first["next_after"], through=first["through"], limit=2)
    assert ids(second["items"]) == [own["id"], reply["id"]]
    assert not second["has_more"] and second["next_after"] == first["through"]
    assert second["total"] == 2 and all("body" not in item for item in second["items"])
    assert len(second["items"][-1]["snippet"]) == 301
    assert ids(board.inbox("Fable", after=second["next_after"])["items"]) == [late["id"]]
    assert ids(board.inbox("Fable", "alpha", a)["items"]) == [1, own["id"], reply["id"], late["id"]]
    assert not board.inbox("Fable", "beta", a)["items"]
    empty = board.inbox("Fable", thread_id=999, through=first["through"])
    assert empty["next_after"] == first["through"] and empty["total"] == 0
    assert ids(board.inbox("Fable", unread_only=True)["items"]) == [2, late["id"]]
    assert ids(board.inbox("Fable", "beta", b)["items"]) == [2]


def test_updates_empty_caught_up_and_explicit_cursor_ignores_shared_marker(board):
    a = create(board)
    last = board.post_message("Fable", a, "Own post")
    board.mark_read("Fable", a, last["id"])
    other = create(board, "beta")
    before = markers(board)
    empty = board.read_updates("Fable", a)
    assert empty["messages"] == [] and not empty["has_more"]
    assert empty["next_after"] == empty["through"] == 3
    assert empty["returned_bytes"] == 2 and not empty["oversized"]
    assert ids(board.read_updates("Fable", a, after=0)["messages"]) == [1, last["id"]]
    assert board.read_updates("Fable", other)["thread"]["unread_count"] == 1
    assert markers(board) == before
    with pytest.raises(NotFound):
        board.read_updates("Fable", 999)


def test_updates_count_byte_pages_and_oversized_utf8(board):
    a = create(board, body="é" * 400)
    b = create(board, "beta")
    expected = [1]
    for i in range(250):
        expected.append(board.post_message("Astra", a, f"{i} " + "é" * 180)["id"])
        if i % 20 == 0:
            board.post_message("Barry", b, "Interleaved gap")
    through = board.inbox("Fable", limit=1)["through"]
    count_page = board.read_updates("Fable", a, limit=7)
    assert ids(count_page["messages"]) == expected[:7] and count_page["has_more"]
    assert ids(board.read_updates("Fable", a, after=count_page["next_after"], limit=7)["messages"]) == expected[7:14]
    collected, after = [], 0
    while True:
        page = board.read_updates("Fable", a, after, through, limit=7, max_bytes=1400)
        assert len(page["messages"]) <= 7
        assert page["returned_bytes"] == byte_count(page["messages"]) <= 1400
        assert not page["oversized"]
        collected.extend(ids(page["messages"]))
        after = page["next_after"]
        if not page["has_more"]:
            break
    assert collected == expected and after == through
    large = board.post_message("Astra", a, "🦉" * 1000)
    trailing = board.post_message("Barry", a, "Small after oversized")
    page = board.read_updates("Fable", a, after=through, max_bytes=1000)
    assert ids(page["messages"]) == [large["id"]] and page["oversized"] and page["has_more"]
    assert page["returned_bytes"] == byte_count(page["messages"]) > 4000
    assert ids(board.read_updates("Fable", a, after=page["next_after"])["messages"]) == [trailing["id"]]
    assert markers(board) == []


def test_updates_metadata_and_upper_bound_share_transaction_snapshot(board, monkeypatch):
    a = create(board)
    original = board._thread
    arrivals = []

    def read_then_arrive(con, thread_id, author):
        result = original(con, thread_id, author)
        with ThreadPoolExecutor(max_workers=1) as pool:
            arrivals.append(pool.submit(board.post_message, "Barry", a, "Concurrent arrival").result())
        return result

    monkeypatch.setattr(board, "_thread", read_then_arrive)
    page = board.read_updates("Fable", a)
    assert page["thread"]["last_message_id"] == page["through"] == 1
    assert ids(page["messages"]) == [1] and arrivals[0]["id"] == 2
    monkeypatch.setattr(board, "_thread", original)
    next_page = board.read_updates("Fable", a, after=page["next_after"])
    assert ids(next_page["messages"]) == [2]


def test_inbox_total_and_page_use_one_snapshot(board, monkeypatch):
    a = create(board)
    bound = board._snapshot_bound

    def capture_then_arrive(con, after, through):
        result = bound(con, after, through)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(board.post_message, "Barry", a, "Later").result()
        return result

    monkeypatch.setattr(board, "_snapshot_bound", capture_then_arrive)
    page = board.inbox("Fable")
    assert page["total"] == 1 and ids(page["items"]) == [1] and page["through"] == 1


def test_batch_requested_order_direct_parents_missing_and_budget(board):
    a = create(board)
    parent = board.post_message("Fable", a, "Parent", parent_id=1)
    child = board.post_message("Barry", a, "Child", parent_id=parent["id"])
    sibling = board.post_message("Astra", a, "Sibling", parent_id=parent["id"])
    result = board.get_messages("Fable", [sibling["id"], 999, child["id"], sibling["id"]], True)
    assert ids(result["messages"]) == [sibling["id"], child["id"], parent["id"]]
    assert result["missing_ids"] == [999] and not result["omitted_ids"]
    assert result["returned_bytes"] == byte_count(result["messages"])
    assert all(m["project_id"] == "alpha" and m["thread_title"] == "Analysis" for m in result["messages"])
    assert all("read_markers" not in m for m in result["messages"])
    huge = board.post_message("Astra", a, "🦉" * 1000)
    small_first = board.get_messages("Fable", [1, huge["id"], child["id"]], max_bytes=1000)
    assert ids(small_first["messages"]) == [1]
    assert small_first["omitted_ids"] == [huge["id"], child["id"]] and not small_first["oversized"]
    huge_first = board.get_messages("Fable", [huge["id"], 1], max_bytes=1000)
    assert huge_first["oversized"] and huge_first["omitted_ids"] == [1]
    assert huge_first["returned_bytes"] == byte_count(huge_first["messages"])
    assert board.get_messages("Fable", [999])["messages"] == []
    assert markers(board) == []


def test_body_search_filters_literals_timestamp_and_pagination(board):
    a = create(board, title="needle title only", body="Not a match")
    b = create(board, "beta")
    target = board.post_message("Barry", a, r"needle 100% thing_under \literal", "evidence")
    decoy = board.post_message("Barry", a, "needle 1000 thingXunder", "proposal")
    elsewhere = board.post_message("Barry", b, "needle", "evidence")
    newer = board.post_message("Astra", a, "needle", "evidence")
    before = markers(board)
    page = board.search_messages_page("Fable", "needle", limit=2)
    assert ids(page["items"]) == [newer["id"], elsewhere["id"]] and page["has_more"]
    rest = board.search_messages_page("Fable", "needle", before=page["next_before"], limit=2)
    assert ids(rest["items"]) == [decoy["id"], target["id"]] and not rest["has_more"]
    assert all("body" not in item for item in page["items"])
    assert ids(board.search_messages_page("Fable", "needle", "alpha", a, "Barry", "evidence")["items"]) == [target["id"]]
    assert not board.search_messages_page("Fable", "needle", "beta", a)["items"]
    for literal in ("%", "_", "\\"):
        assert ids(board.search_messages_page("Fable", literal)["items"]) == [target["id"]]
    same_instant = datetime.fromisoformat(target["created_at"]).astimezone(
        datetime.fromisoformat("2000-01-01T00:00:00+10:00").tzinfo).isoformat()
    assert ids(board.search_messages_page("Fable", "%", since=same_instant)["items"]) == [target["id"]]
    later = (datetime.fromisoformat(target["created_at"]) + timedelta(microseconds=1)).isoformat()
    assert not board.search_messages_page("Fable", "%", since=later)["items"]
    assert ids(board.search_messages_page("Fable", "needle", after=target["id"], before=elsewhere["id"])["items"]) == [decoy["id"]]
    assert markers(board) == before


def test_thread_pages_stable_despite_activity_and_title_search(board):
    threads = [create(board, "alpha" if i % 2 == 0 else "beta", f"Needle {i}") for i in range(7)]
    board.mark_read("Barry", threads[-1], 7)
    first = board.list_threads_page("Fable", limit=3)
    assert ids(first["items"]) == list(reversed(threads[-3:])) and first["has_more"]
    shared = first["items"][0]
    assert shared["read_markers"]["Barry"] == 7 and shared["unread_counts"]["Barry"] == 0
    assert shared["last_author"] == "Astra"
    board.post_message("Barry", threads[0], "New activity in old thread")
    create(board, title="Newest thread")
    second = board.list_threads_page("Fable", limit=3, before=first["next_before"])
    third = board.list_threads_page("Fable", limit=3, before=second["next_before"])
    assert ids(first["items"] + second["items"] + third["items"]) == list(reversed(threads))
    assert not third["has_more"]
    title_hits = board.search_threads("Fable", "needle", "alpha", limit=2)
    assert ids(title_hits["items"]) == [7, 5]
    assert ids(board.search_threads("Fable", "needle", "alpha", title_hits["next_before"])["items"]) == [3, 1]
    assert not board.search_threads("Fable", "New activity")["items"]
    assert 7 not in ids(board.list_threads_page("Barry", unread_only=True)["items"])
    assert len(board.list_threads_page("Fable", limit=1000)["items"]) == 8
    assert markers(board) == [("Barry", 7, 7)]


@pytest.mark.parametrize("kwargs", [{"after": -1}, {"after": True}, {"after": 2}, {"through": 2},
                                   {"after": 1, "through": 0}, {"thread_id": 0},
                                   {"project_id": "../bad"}, {"limit": 0}, {"limit": 501}])
def test_inbox_rejects_invalid_or_future_checkpoints(board, kwargs):
    create(board)
    with pytest.raises(ValueError):
        board.inbox("Fable", **kwargs)
    assert markers(board) == []


@pytest.mark.parametrize("since", ["2026-09-20", "2026-09-20T12:00:00", "invalid", 12])
def test_search_rejects_invalid_or_ambiguous_timestamps(board, since):
    with pytest.raises(ValueError, match="timezone-aware"):
        board.search_messages_page("Fable", "needle", since=since)


def test_batch_validation_and_empty_board(tmp_path):
    empty = Store(tmp_path)
    assert empty.inbox("Fable")["through"] == empty.inbox("Fable")["next_after"] == 0
    assert not empty.list_threads_page("Fable")["items"]
    for invalid in ([], list(range(1, 52)), [0], [True], [2**63], "1"):
        with pytest.raises(ValueError):
            empty.get_messages("Fable", invalid)
    with pytest.raises(ValueError):
        empty.read_updates("Fable", 1, max_bytes=999)
