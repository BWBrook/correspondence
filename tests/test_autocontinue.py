import json
import stat

import pytest
from fastapi.testclient import TestClient

from correspondence import autocontinue
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
    return autocontinue.step(store, read_turns(store.directory))


def deliver(store, dispatch_id):
    store.claim_dispatch(dispatch_id)
    store.finish_dispatch(dispatch_id, True, "")
    with store.connect(write=True) as con:  # well past the guard against double sends
        con.execute("UPDATE dispatches SET delivered_at='2026-01-01T00:00:00+00:00'")


def test_a_run_hands_off_once_per_turn_until_its_budget_is_spent(board):
    add_turn(board, "Claude", CLAUDE, "2099-01-01T00:00:00+00:00", "Reviewed.\nNext: Codex — check the arithmetic")  # a fast clock
    board.start_auto("p", 2, 80)
    assert run(board) == ["p: sent dispatch 1 to Codex"]
    sent = board.list_dispatches("p", 1)[0]
    assert (sent["origin"], sent["status"]) == ("auto", "delivered")
    assert sent["body"].startswith("Continue — from User, sent through the board.\n\nClaude's latest turn in Project P")
    with board.connect(write=True) as con:
        con.execute("UPDATE dispatches SET delivered_at='2026-01-01T00:00:00+00:00'")
    assert run(board) == []  # that turn has had its prompt, however the clocks compare: wait for the answer
    add_turn(board, "Codex", ADVISOR, "2099-01-01T00:01:00+00:00", "Advice.\nNext: Claude")
    assert run(board) == []  # advisor turns never drive a run
    add_turn(board, "Codex", CODEX, "2099-01-01T00:02:00+00:00", "Checked.\nNext: Claude")
    assert run(board) == ["p: sent dispatch 2 to Claude"]
    assert board.auto_continue("p")["hops_left"] == 0
    deliver(board, 2)
    add_turn(board, "Claude", CLAUDE, "2099-01-01T00:03:00+00:00", "Done.\nNext: Codex")
    assert run(board) == ["p: stopped: The hand-offs set for this run are used up"]
    assert board.auto_continue("p")["running"] is False


def test_a_primed_session_gets_the_short_prompt(board):
    add_turn(board, "Claude", CLAUDE, "2026-11-07T01:00:00+00:00", "First.\nNext: Codex")
    deliver(board, board.create_dispatch("p", "Codex", "earlier prompt")["id"])
    add_turn(board, "Claude", CLAUDE, "2026-11-07T02:00:00+00:00", "Second.\nNext: Codex")
    board.start_auto("p", 3, 80)
    run(board)
    assert board.list_dispatches("p", 1)[0]["body"].startswith("Claude replied on ")


@pytest.mark.parametrize("message, reason", [
    ("Over to you.\nNext: User — choose the data source", "Claude handed this to you: choose the data source"),
    ("No declaration here.", "Claude's turn named no one to go next"),
    ("More to do.\nNext: Claude", "Claude asked to continue its own work"),
])
def test_a_run_stops_where_the_user_must_decide(board, message, reason):
    add_turn(board, "Claude", CLAUDE, "2026-11-07T01:00:00+00:00", message)
    board.start_auto("p", 5, 80)
    assert run(board) == [f"p: stopped: {reason}"]
    assert board.auto_continue("p")["stopped_reason"] == reason and not board.list_dispatches("p")


def test_a_run_stops_at_the_ceiling_of_a_current_limit_reading(board):
    add_turn(board, "Codex", CODEX, "2026-11-07T01:00:00+00:00", "Done.\nNext: Claude",
             limits={"windows": [{"label": "Weekly", "percent": 82.0, "resets_at": "2099-01-01T00:00:00+00:00"}]})
    board.start_auto("p", 5, 80)
    assert run(board) == ["p: stopped: Codex's Weekly limit is at 82% (ceiling 80%)"]
    board.set_limits("Claude", {"windows": [{"label": "5-hour limit", "percent": 95.0, "resets_at": "2026-01-01T00:00:00+00:00"}]})
    board.start_auto("p", 5, 90)  # Claude's window has reset since that reading, so it no longer counts
    assert run(board) == ["p: sent dispatch 1 to Claude"]
    board.set_limits("Claude", {"windows": [{"label": "5-hour limit", "percent": 91.0, "resets_at": None}]})
    assert run(board) == ["p: stopped: Claude's 5-hour limit is at 91% (ceiling 90%)"]
    assert board.list_dispatches("p", 1)[0]["detail"] == "Cancelled: auto-continue stopped"  # it never reaches Claude


def test_a_failed_or_cancelled_prompt_stops_the_run(board):
    add_turn(board, "Codex", CODEX, "2026-11-07T01:00:00+00:00", "Done.\nNext: Claude")
    board.start_auto("p", 5, 80)
    run(board)
    board.cancel_dispatch(1)
    assert run(board) == ["p: stopped: A prompt to Claude failed: Cancelled by User"]


def test_a_run_waits_while_any_prompt_in_the_project_is_on_its_way(board):
    board.create_dispatch("p", "Codex", "a prompt the courier has not yet delivered")
    add_turn(board, "Codex", CODEX, "2026-11-07T01:00:00+00:00", "Started by hand.\nNext: Claude")
    board.start_auto("p", 5, 80)
    assert run(board) == [] and len(board.list_dispatches("p")) == 1  # Codex is not done: do not wake Claude too
    board.claim_dispatch(1)
    board.finish_dispatch(1, True, "")
    assert run(board) == ["p: sent dispatch 2 to Claude"]
    deliver(board, 2)
    run_id, turn = board.auto_continue("p")["started_at"], board.list_dispatches("p", 1)[0]["after_turn"]
    with pytest.raises(Conflict):  # a round that read the turn before another send recorded it cannot send it again
        board.create_dispatch("p", "Codex", "again", after_turn=turn, run=run_id)


def test_stop_beats_any_send_decided_before_it_and_withdraws_the_waiting_prompt(board):
    add_turn(board, "Codex", CODEX, "2026-11-07T01:00:00+00:00", "Done.\nNext: Claude")
    first = board.start_auto("p", 5, 80)
    run(board)  # a Claude prompt now waits for the messenger
    stopped = board.stop_auto("p")
    assert stopped["stopped_reason"] == "Stopped by User"
    assert board.list_dispatches("p", 1)[0]["detail"] == "Cancelled: auto-continue stopped"
    with pytest.raises(Conflict):  # a round that read the run before the Stop cannot send
        board.create_dispatch("p", "Codex", "late", run=first["started_at"])
    second = board.start_auto("p", 5, 80)
    board.stop_auto("p", "stale decision", run=first["started_at"])  # nor end the run started since
    assert board.auto_continue("p")["running"] and board.auto_continue("p")["started_at"] == second["started_at"]
    assert run(board) == ["p: sent dispatch 2 to Claude"]  # the new run retries the turn whose prompt was withdrawn
    with pytest.raises(Conflict):
        board.start_auto("p", 5, 80)  # one run at a time


def test_a_run_needs_both_main_sessions_and_a_valid_budget(board):
    for hops, ceiling in ((0, 80), (3, 150)):
        with pytest.raises(ValueError):
            board.start_auto("p", hops, ceiling)
    board.set_project_agent("p", "Codex", None)
    with pytest.raises(Conflict):
        board.start_auto("p", 3, 80)
    board.set_project_agent("p", "Codex", CODEX)
    started = board.start_auto("p", 3, 80)["started_at"]
    board.set_project_agent("p", "Codex", None)
    with pytest.raises(Conflict, match="no longer chosen"):  # a round that read the pins before the change
        board.create_dispatch("p", "Claude", "x", run=started)
    assert run(board) == ["p: stopped: A main session is no longer chosen"]


def test_endpoints_start_stop_and_record_the_turn_each_prompt_follows(board):
    add_turn(board, "Claude", CLAUDE, "2026-11-07T01:00:00+00:00", "Reviewed.\nNext: Codex")
    with TestClient(create_app(board.directory), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/projects/p/auto").json()["running"] is False
        started = client.post("/api/projects/p/auto", json={"hops": 4, "ceiling": 85}, headers=HEADERS).json()
        assert started["running"] and started["hops_left"] == 3  # the first hand-off went at once
        assert client.post("/api/projects/p/auto/stop", json={}, headers=HEADERS).json()["running"] is False
        assert client.post("/api/projects/p/auto", json={"hops": 4}, headers=HEADERS).status_code == 422
        with board.connect(write=True) as con:
            con.execute("UPDATE dispatches SET delivered_at='2026-01-01T00:00:00+00:00'")
        manual = client.post("/api/dispatches", json={"project_id": "p", "target": "Codex", "body": "x"}, headers=HEADERS).json()
        assert manual["origin"] is None and manual["after_turn"] == f"{CLAUDE}@2026-11-07T01:00:00+00:00"
        assert client.get("/api/usage?project_id=p").json() == {"limits": {"Claude": None, "Codex": None}, "days": []}
        assert set(client.get("/api/turns?project_id=p").json()[0]["continue_prompts"]) == {"short", "full"}
