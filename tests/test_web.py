import pytest
from fastapi.testclient import TestClient

from correspondence.web import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as client:
        yield client


HEADERS = {"X-Correspondence-Client": "browser", "Origin": "http://127.0.0.1:8765"}


def test_browser_identity_and_end_to_end(client):
    assert client.get("/api/health").json()["participant"] == "Barry"
    assert client.post("/api/projects", json={"id": "demo", "name": "Demo"}, headers=HEADERS).status_code == 201
    result = client.post("/api/threads", json={"project_id": "demo", "title": "Test", "body": "Initial", "request_id": "thread-key"}, headers=HEADERS)
    assert result.status_code == 201
    thread_id = result.json()["id"]
    message = client.get(f"/api/threads/{thread_id}").json()["messages"][0]
    assert message["author"] == "Barry" and message["model"] is None
    assert client.post(f"/api/threads/{thread_id}/messages", json={"body": "forged", "author": "Codex"}, headers=HEADERS).status_code == 422
    assert client.post(f"/api/threads/{thread_id}/messages", json={"body": "Reply", "parent_id": message["id"]}, headers=HEADERS).status_code == 201
    assert client.post(f"/api/threads/{thread_id}/read", json={"message_id": message["id"]}, headers=HEADERS).status_code == 200
    assert client.post(f"/api/threads/{thread_id}/status", json={"status": "resolved"}, headers=HEADERS).json()["status"] == "resolved"
    assert len(client.get("/api/search?q=Reply").json()) == 1
    exported = client.get(f"/api/threads/{thread_id}/export")
    assert "text/markdown" in exported.headers["content-type"] and "Barry" in exported.text
    assert client.get("/api/threads/999").status_code == 404


def test_host_origin_content_type_and_header_boundary(client):
    assert client.get("/api/health", headers={"Host": "attacker.example:8765"}).status_code == 400
    assert client.get("/api/health", headers={"Host": "localhost.attacker.example"}).status_code == 400
    payload = {"id": "demo", "name": "Demo"}
    assert client.post("/api/projects", json=payload).status_code == 403
    assert client.post("/api/projects", json=payload, headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/projects", json=payload, headers={**HEADERS, "Origin": "null"}).status_code == 403
    assert client.post("/api/projects", content='{}', headers={**HEADERS, "Content-Type": "text/plain"}).status_code == 415
    response = client.options("/api/projects", headers={"Origin": "https://evil.example", "Access-Control-Request-Headers": "X-Correspondence-Client"})
    assert "access-control-allow-origin" not in response.headers
    assert client.get("/api/projects").json() == []


def test_read_navigation_and_exact_message_api(client):
    board = client.app.state.store
    board.create_project("navigation", "Navigation")
    thread_id = board.create_thread("Codex", "navigation", "Long discussion", "First")["id"]
    middle = board.post_message("Barry", thread_id, "Own message")
    last = board.post_message("Claude", thread_id, "Newest message")
    latest = client.get(f"/api/threads/{thread_id}?mode=latest&limit=1").json()
    assert latest["messages"] == [last]
    assert latest["has_before"] and not latest["has_more"]
    older = client.get(f"/api/threads/{thread_id}?before={last['id']}&limit=1").json()
    assert older["messages"] == [middle] and older["has_more"]
    first_id = latest["thread"]["first_message_id"]
    assert client.get(f"/api/messages/{first_id}").json()["thread"]["id"] == thread_id
    assert client.get("/api/messages/999999").status_code == 404
    assert client.get(f"/api/threads/{thread_id}?mode=latest&after=1").status_code == 422
    assert client.get(f"/api/threads/{thread_id}?mode=unread&before=1").status_code == 422
    assert client.get(f"/api/threads/{thread_id}?after=1&before=3").status_code == 422
    assert client.get(f"/api/threads/{thread_id}?before=0").status_code == 422
    assert client.get(f"/api/threads/{thread_id}?mode=bad").status_code == 422
    assert board.read_thread("Barry", thread_id)["thread"]["last_read_message_id"] == 0
    client.post(f"/api/threads/{thread_id}/read", json={"message_id": first_id}, headers=HEADERS)
    unread = client.get(f"/api/threads/{thread_id}?mode=unread").json()
    assert unread["messages"] == [last]  # Barry's own intervening post is skipped.
    assert unread["thread"]["first_unread_message_id"] == last["id"]


def test_health_reports_stable_process_snapshot(client, monkeypatch):
    first = client.get("/api/health").json()
    assert len(first["revision"]) == 40
    assert isinstance(first["dirty"], bool)
    assert first["pid"] > 0 and first["started_at"]
    monkeypatch.setattr("correspondence.runtime.subprocess.check_output", lambda *a, **kw: "changed")
    assert client.get("/api/health").json() == first


def test_process_info_captures_revision_dirty_and_unavailable(tmp_path):
    import subprocess
    from correspondence.runtime import capture_process_info

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "Initial")
    clean = capture_process_info(tmp_path)
    assert clean["revision"] == git("rev-parse", "HEAD") and clean["dirty"] is False
    (tmp_path / "new.py").write_text("# new code\n")
    assert capture_process_info(tmp_path)["dirty"] is True
    missing = capture_process_info(tmp_path / "absent")
    assert missing["revision"] is None and missing["dirty"] is None


def test_access_logging_filters_only_successful_poll_requests():
    import logging
    from correspondence.runtime import PollAccessFilter, access_log_config

    def record(method, path, status):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1", method, path, "1.1", status), None)

    poll_filter = PollAccessFilter()
    assert not poll_filter.filter(record("GET", "/api/projects", 200))
    assert not poll_filter.filter(record("GET", "/api/threads?project_id=demo", 200))
    for method, path, status in [("GET", "/api/threads", 422), ("GET", "/api/projects", 500),
                                 ("POST", "/api/threads", 201), ("GET", "/api/threads/1", 200),
                                 ("GET", "/api/health", 200), ("GET", "/api/threads/", 307)]:
        assert poll_filter.filter(record(method, path, status))
    config = access_log_config()
    assert config["handlers"]["access"]["filters"] == ["polls"]
    from uvicorn.logging import AccessFormatter
    rendered = AccessFormatter(fmt=config["formatters"]["access"]["fmt"],
                               datefmt=config["formatters"]["access"]["datefmt"], use_colors=False).format(
                                   record("GET", "/api/health", 200))
    assert rendered[:4].isdigit() and "T" in rendered[:20] and '"GET /api/health HTTP/1.1" 200' in rendered


def test_search_endpoint_scope_cursor_and_validation(client):
    board = client.app.state.store
    board.create_project("search", "Search")
    board.create_project("other", "Other")
    first = board.create_thread("Codex", "search", "One", "Match old")["id"]
    second = board.create_thread("Claude", "other", "Two", "Match elsewhere")["id"]
    latest = board.post_message("Claude", first, "Match recent")
    response = client.get("/api/search", params={"q": "match", "thread_id": first, "limit": 1})
    assert response.status_code == 200
    assert [m["id"] for m in response.json()] == [latest["id"]]
    older = client.get("/api/search", params={"q": "match", "project_id": "search", "thread_id": first, "before": latest["id"]}).json()
    assert len(older) == 1 and older[0]["thread_id"] == first
    assert len(client.get("/api/search?q=match").json()) == 3
    assert client.get("/api/search", params={"q": "match", "project_id": "search", "thread_id": second}).json() == []
    assert client.get("/api/search?q=match&thread_id=999999").json() == []
    for field in ("thread_id", "before"):
        for value in (0, -1, "abc", 2**63):
            assert client.get("/api/search", params={"q": "match", field: value}).status_code == 422
    assert board.read_thread("Barry", first)["thread"]["last_read_message_id"] == 0


def test_mark_all_read_http_identity_payload_and_origin(client):
    board = client.app.state.store
    board.create_project("bulk", "Bulk")
    thread_id = board.create_thread("Codex", "bulk", "Unread", "Opening")["id"]
    assert client.post("/api/read-all", json={}, headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/read-all", json={}).status_code == 403
    assert client.post("/api/read-all", content="{}", headers={**HEADERS, "Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/read-all", json={"author": "Claude"}, headers=HEADERS).status_code == 422
    assert client.post("/api/read-all", json={"thread_id": thread_id}, headers=HEADERS).status_code == 422
    assert client.post("/api/read-all", headers={**HEADERS, "Content-Type": "application/json"}).status_code == 422
    assert board.read_thread("Barry", thread_id)["thread"]["read_markers"] == {"Barry": 0, "Codex": 0, "Claude": 0}
    response = client.post("/api/read-all", json={}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"threads_marked": 1, "messages_marked": 1}
    metadata = board.read_thread("Barry", thread_id)["thread"]
    assert metadata["read_markers"] == {"Barry": metadata["last_message_id"], "Codex": 0, "Claude": 0}
    assert client.post("/api/read-all", json={}, headers=HEADERS).json() == {"threads_marked": 0, "messages_marked": 0}
