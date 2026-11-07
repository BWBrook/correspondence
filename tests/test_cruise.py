import json
import stat

import pytest
from fastapi.testclient import TestClient

from correspondence import cruise
from correspondence.store import Conflict, Store
from correspondence.turns import read_turns
from correspondence.web import create_app

HEADERS = {"X-Correspondence-Client": "browser", "Origin": "http://127.0.0.1:8765"}
CLAUDE, CODEX, ADVISOR = "claude-main-session", "codex-main-thread", "codex-advisor-thread"


@pytest.fixture
def board(tmp_path, monkeypatch):
    fake = tmp_path / "codex"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {tmp_path}/codex-args\necho "Queued."\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CORRESPONDENCE_CODEX", str(fake))
    store = Store(tmp_path)
    store.create_project("p", "Project P")
    store.set_project_agent("p", "Claude", CLAUDE)
    store.set_project_agent("p", "Codex", CODEX)
    return store


def add_turn(store, agent, session, at, message, limits=None):
    entry = {"recorded_at": at, "agent": agent, "project": "p", "session_id": session, "message": message}
    if limits:
        entry["limits"] = limits
    with open(store.directory / "turns.jsonl", "a") as ledger:
        ledger.write(json.dumps(entry) + "\n")


def run(store):
    return cruise.step(store, read_turns(store.directory))


def test_cruise_hands_off_until_its_budget_is_spent(board):
    add_turn(board, "Claude", CLAUDE, "2026-11-07T01:00:00+00:00", "Reviewed.\nNext: Codex — check the arithmetic")
    board.set_project_mode("p", "cruise", hops=2, ceiling=80)
    assert run(board) == ["p: sent dispatch 1 to Codex"]
    sent = board.list_dispatches("p", 1)[0]
    assert sent["origin"] == "cruise" and sent["status"] == "delivered"
    assert sent["body"].startswith("Continue — from User, sent through the board.\n\nClaude's latest turn in Project P")
    assert run(board) == []  # the prompt is out: wait for the answer
    add_turn(board, "Codex", ADVISOR, "2099-01-01T00:00:00+00:00", "Advice.\nNext: Claude")
    assert run(board) == []  # pinned sessions only
    add_turn(board, "Codex", CODEX, "2099-01-01T00:01:00+00:00", "Checked.\nNext: Claude")
    assert run(board) == ["p: sent dispatch 2 to Claude"]
    assert board.project_mode("p")["hops_left"] == 0
    board.finish_dispatch(2, True, "")
    add_turn(board, "Claude", CLAUDE, "2099-01-01T00:02:00+00:00", "Done.\nNext: Codex")
    assert run(board) == ["p: stopped: The hand-offs set for this cruise are used up"]
    mode = board.project_mode("p")
    assert mode["mode"] == "manual" and mode["stopped_reason"] == "The hand-offs set for this cruise are used up"


def test_a_primed_session_gets_the_short_prompt(board):
    add_turn(board, "Claude", CLAUDE, "2026-11-07T01:00:00+00:00", "First.\nNext: Codex")
    board.create_dispatch("p", "Codex", "earlier prompt")
    board.finish_dispatch(1, True, "")
    with board.connect(write=True) as con:  # past the guard against double sends
        con.execute("UPDATE dispatches SET requested_at='2026-11-07T01:00:01', delivered_at='2026-11-07T01:00:01'")
    add_turn(board, "Claude", CLAUDE, "2099-01-01T00:00:00+00:00", "Second.\nNext: Codex")
    board.set_project_mode("p", "cruise", hops=3, ceiling=80)
    run(board)
    assert board.list_dispatches("p", 1)[0]["body"].startswith("Claude replied on ")


@pytest.mark.parametrize("message, reason", [
    ("Over to you.\nNext: User — choose the data source", "Claude handed this to you: choose the data source"),
    ("No declaration here.", "Claude's turn named no one to go next"),
    ("More to do.\nNext: Claude", "Claude asked to continue its own work"),
])
def test_cruise_stops_where_the_user_must_decide(board, message, reason):
    add_turn(board, "Claude", CLAUDE, "2026-11-07T01:00:00+00:00", message)
    board.set_project_mode("p", "cruise", hops=5, ceiling=80)
    assert run(board) == [f"p: stopped: {reason}"]
    assert board.project_mode("p")["mode"] == "manual" and not board.list_dispatches("p")


def test_cruise_stops_at_the_limit_ceiling(board):
    add_turn(board, "Codex", CODEX, "2026-11-07T01:00:00+00:00", "Done.\nNext: Claude",
             limits={"windows": [{"label": "Weekly", "percent": 82.0, "resets_at": None}]})
    board.set_project_mode("p", "cruise", hops=5, ceiling=80)
    assert run(board) == ["p: stopped: Codex's Weekly limit is at 82% (ceiling 80%)"]
    board.set_limits("Claude", {"windows": [{"label": "5-hour limit", "percent": 50.0, "resets_at": None}]})
    board.set_project_mode("p", "cruise", hops=5, ceiling=90)
    assert run(board) == ["p: sent dispatch 1 to Claude"]


def test_cruise_stops_after_a_failed_prompt(board):
    add_turn(board, "Codex", CODEX, "2026-11-07T01:00:00+00:00", "Done.\nNext: Claude")
    board.set_project_mode("p", "cruise", hops=5, ceiling=80)
    run(board)
    board.finish_dispatch(1, False, "Ambiguous recipient")
    assert run(board) == ["p: stopped: A prompt to Claude failed: Ambiguous recipient"]


def test_hold_cancels_waiting_prompts_and_blocks_sends(board):
    board.create_dispatch("p", "Claude", "Waiting prompt")
    board.set_project_mode("p", "hold")
    assert board.list_dispatches("p", 1)[0]["detail"] == "Cancelled: project put on hold"
    with pytest.raises(Conflict, match="on hold"):
        board.create_dispatch("p", "Codex", "Another")
    board.set_project_mode("p", "manual")
    board.create_dispatch("p", "Codex", "Another")


def test_stopping_a_cruise_records_who_stopped_it(board):
    board.set_project_mode("p", "cruise", hops=3, ceiling=80)
    assert board.set_project_mode("p", "hold")["stopped_reason"] == "Stopped by User"


def test_cruise_needs_both_main_sessions_and_a_valid_budget(board):
    with pytest.raises(ValueError):
        board.set_project_mode("p", "cruise", hops=0, ceiling=80)
    with pytest.raises(ValueError):
        board.set_project_mode("p", "cruise", hops=3, ceiling=150)
    board.set_project_agent("p", "Codex", None)
    with pytest.raises(Conflict):
        board.set_project_mode("p", "cruise", hops=3, ceiling=80)


def test_mode_and_usage_endpoints(board):
    add_turn(board, "Claude", CLAUDE, "2026-11-07T01:00:00+00:00", "Reviewed.\nNext: Codex")
    with TestClient(create_app(board.directory), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/projects/p/mode").json()["mode"] == "manual"
        started = client.post("/api/projects/p/mode", json={"mode": "cruise", "hops": 4, "ceiling": 85}, headers=HEADERS)
        assert started.status_code == 200 and started.json()["hops_left"] == 3  # the first hand-off went at once
        assert client.get("/api/dispatches?project_id=p").json()[0]["origin"] == "cruise"
        assert client.post("/api/projects/p/mode", json={"mode": "hold"}, headers=HEADERS).json()["mode"] == "hold"
        refused = client.post("/api/dispatches", json={"project_id": "p", "target": "Claude", "body": "x"}, headers=HEADERS)
        assert refused.status_code == 409
        assert client.post("/api/projects/p/mode", json={"mode": "cruise"}, headers=HEADERS).status_code == 422
        usage = client.get("/api/usage?project_id=p").json()
        assert usage == {"limits": {"Claude": None, "Codex": None}, "days": []}
        turn = client.get("/api/turns?project_id=p").json()[0]
        assert set(turn["continue_prompts"]) == {"short", "full"}
