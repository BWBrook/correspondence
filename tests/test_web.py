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
    assert client.post(f"/api/threads/{thread_id}/messages", json={"body": "forged", "author": "Astra"}, headers=HEADERS).status_code == 422
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
    thread_id = board.create_thread("Astra", "navigation", "Long discussion", "First")["id"]
    middle = board.post_message("Barry", thread_id, "Own message")
    last = board.post_message("Fable", thread_id, "Newest message")
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
