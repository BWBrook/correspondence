import json

import pytest
from fastapi.testclient import TestClient

from correspondence.web import create_app


def turn(project, agent, at, message):
    return {"recorded_at": at, "agent": agent, "project": project, "session_id": "s", "turn_id": None,
            "model": None, "cwd": "/private/path", "message": message}


@pytest.fixture
def client(tmp_path):
    lines = [
        json.dumps(turn("a", "Claude", "2026-11-04T10:00:00+00:00", "first")),
        "not json",
        json.dumps({"agent": "Codex"}),
        json.dumps(turn("b", "Codex", "2026-11-04T10:05:00+00:00", "other project")),
        json.dumps(turn("a", "Codex", "2026-11-04T10:10:00+00:00", "second")),
    ]
    (tmp_path / "turns.jsonl").write_text("\n".join(lines) + "\n")
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as client:
        yield client


def test_latest_turn_per_project_newest_first(client):
    turns = client.get("/api/turns").json()
    assert [(t["project"], t["message"]) for t in turns] == [("a", "second"), ("b", "other project")]
    assert "cwd" not in turns[0]


def test_project_turns_newest_first_and_limited(client):
    assert [t["message"] for t in client.get("/api/turns?project_id=a").json()] == ["second", "first"]
    assert [t["message"] for t in client.get("/api/turns?project_id=a&limit=1").json()] == ["second"]
    assert client.get("/api/turns?project_id=missing").json() == []


def test_missing_ledger_is_empty(tmp_path):
    with TestClient(create_app(tmp_path / "fresh"), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/turns").json() == []
