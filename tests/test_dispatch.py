import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from correspondence.store import Conflict, Store
from correspondence.web import create_app

HEADERS = {"X-Correspondence-Client": "browser", "Origin": "http://127.0.0.1:8765"}
MAIN, OTHER = "01a17cc1-main-thread", "01a18111-heartbeat"


def turn(agent, session, at, message):
    return json.dumps({"recorded_at": at, "agent": agent, "project": "p", "session_id": session, "message": message})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    fake = tmp_path / "codex"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {tmp_path}/codex-args\necho "Queued message q1 for thread $3."\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CORRESPONDENCE_CODEX", str(fake))
    (tmp_path / "turns.jsonl").write_text("\n".join([
        turn("Codex", MAIN, "2026-11-04T11:20:18+00:00", "Main turn."),
        turn("Codex", OTHER, "2026-11-04T15:00:42+00:00", "<heartbeat>\n<decision>DONT_NOTIFY</decision>"),
    ]) + "\n")
    Store(tmp_path).create_project("p", "P")
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as client:
        yield client, tmp_path


def test_pins_mark_other_sessions_and_heartbeats(setup):
    client, _ = setup
    sessions = client.get("/api/projects/p/agents").json()["sessions"]
    assert {s["session_id"]: s["automation"] for s in sessions} == {MAIN: False, OTHER: True}
    assert client.get("/api/turns").json()[0]["message"] == "Main turn."
    assert client.post("/api/projects/p/agents", json={"agent": "Codex", "session_id": MAIN}, headers=HEADERS).json()["pins"] == {"Codex": MAIN}
    turns = client.get("/api/turns?project_id=p").json()
    assert [(t["role"], t["automation"]) for t in turns] == [("other", True), ("main", False)]
    assert client.post("/api/projects/p/agents", json={"agent": "Codex", "session_id": None}, headers=HEADERS).json()["pins"] == {}


def test_codex_dispatch_queues_on_the_pinned_thread_and_tracks_the_answer(setup):
    client, tmp_path = setup
    body = {"project_id": "p", "target": "Codex", "body": "Continue, please."}
    assert client.post("/api/dispatches", json=body, headers=HEADERS).status_code == 409
    client.post("/api/projects/p/agents", json={"agent": "Codex", "session_id": MAIN}, headers=HEADERS)
    sent = client.post("/api/dispatches", json=body, headers=HEADERS)
    assert sent.status_code == 201 and sent.json()["status"] == "delivered"
    assert (tmp_path / "codex-args").read_text().split("\n")[:5] == ["queue", "--thread", MAIN, "--message", "Continue, please."]
    assert client.post("/api/dispatches", json=body, headers=HEADERS).status_code == 409
    assert client.get("/api/dispatches?project_id=p").json()[0]["answered_at"] is None
    with open(tmp_path / "turns.jsonl", "a") as ledger:
        ledger.write(turn("Codex", MAIN, "2099-01-01T00:00:00+00:00", "Done.") + "\n")
    assert client.get("/api/dispatches?project_id=p").json()[0]["answered_at"] == "2099-01-01T00:00:00+00:00"


def test_dispatch_is_browser_only_and_claude_waits_for_the_messenger(setup):
    client, _ = setup
    body = {"project_id": "p", "target": "Codex", "body": "x"}
    assert client.post("/api/dispatches", json=body).status_code == 403
    claude = client.post("/api/dispatches", json={**body, "target": "Claude"}, headers=HEADERS)
    assert claude.status_code == 409 and "Messenger" in claude.json()["detail"]


def test_failed_delivery_is_recorded(setup, monkeypatch):
    client, tmp_path = setup
    monkeypatch.setenv("CORRESPONDENCE_CODEX", str(tmp_path / "missing-codex"))
    client.post("/api/projects/p/agents", json={"agent": "Codex", "session_id": MAIN}, headers=HEADERS)
    sent = client.post("/api/dispatches", json={"project_id": "p", "target": "Codex", "body": "x"}, headers=HEADERS).json()
    assert sent["status"] == "failed" and "FileNotFoundError" in sent["detail"]
    assert client.post("/api/dispatches", json={"project_id": "p", "target": "Codex", "body": "x"}, headers=HEADERS).status_code == 201


def test_archive_carries_pins_and_dispatches(tmp_path):
    store = Store(tmp_path)
    store.create_project("p", "P")
    store.set_project_agent("p", "Codex", MAIN)
    store.finish_dispatch(store.create_dispatch("p", "Codex", "x")["id"], True, "")
    token = store.list_projects("Barry")[0]["state_token"]
    archived = store.archive_project("p", token)
    with store.connect() as con:
        assert con.execute("SELECT project_id FROM project_agents").fetchone()[0] == archived["id"]
        assert con.execute("SELECT project_id FROM dispatches").fetchone()[0] == archived["id"]
