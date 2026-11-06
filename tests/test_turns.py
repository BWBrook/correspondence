import json

import pytest
from fastapi.testclient import TestClient

from correspondence.store import Store
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


def line(agent, session, at, message):
    return json.dumps({"recorded_at": at, "agent": agent, "project": "p", "session_id": session, "message": message}) + "\n"


def test_turns_carry_next_declarations_and_the_discussions_posted_during_them(tmp_path):
    store = Store(tmp_path)
    store.set_user_name("Ada")
    store.create_project("p", "P")
    store.create_project("q", "Q")
    with store.connect(write=True) as con:
        con.execute("INSERT INTO threads(id,project_id,title,created_at,updated_at) VALUES (1,'p','Plan','2026','2026'),(2,'p','Review','2026','2026'),(3,'q','Elsewhere','2026','2026')")
        for id, thread, author, at in [(1, 1, "Claude", "2026-11-06T09:00:00.5+00:00"),  # before the window
                                       (2, 1, "Claude", "2026-11-06T10:00:01.2+00:00"),
                                       (3, 2, "Claude", "2026-11-06T10:30:00.0+00:00"),
                                       (4, 2, "Claude", "2026-11-06T11:00:00.9+00:00"),  # same second as the turn
                                       (5, 2, "Codex", "2026-11-06T10:40:00.0+00:00"),  # another agent
                                       (6, 3, "Claude", "2026-11-06T10:40:00.0+00:00"),  # another project
                                       (7, 1, "Claude", "2026-11-06T11:00:01.0+00:00")]:  # after the turn
            con.execute("INSERT INTO messages(id,thread_id,author,body,kind,created_at) VALUES (?,?,?,?,?,?)", (id, thread, author, "x", "note", at))
    (tmp_path / "turns.jsonl").write_text(
        line("Claude", "c1", "2026-11-06T10:00:00+00:00", "First.\n\nNext: Codex")
        + line("Claude", "c2", "2026-11-06T10:50:00+00:00", "An advisor.")
        + line("Claude", "c1", "2026-11-06T11:00:00+00:00", "Posted the review.\n\n**Next:** Ada — choose the threshold")
        + line("Codex", "x1", "2026-11-06T10:45:00+00:00", "Next steps: none"))
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as client:
        turns = {(t["session_id"], t["recorded_at"][11:16]): t for t in client.get("/api/turns?project_id=p").json()}
        latest = turns[("c1", "11:00")]
        assert latest["next"] == {"agent": "User", "reason": "choose the threshold"}
        assert latest["discussions"] == [{"thread_id": 1, "title": "Plan", "message_ids": [2]},
                                         {"thread_id": 2, "title": "Review", "message_ids": [3, 4]}]
        assert turns[("c1", "10:00")]["next"] == {"agent": "Codex", "reason": None}
        assert turns[("c1", "10:00")]["discussions"] == []  # a session's first turn has no window
        assert turns[("x1", "10:45")]["next"] is None
        board = client.get("/api/turns").json()
        assert [t["discussions"] for t in board if t["project"] == "p"] == [latest["discussions"]]
