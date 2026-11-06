import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from correspondence.mcp_server import build_server
from correspondence.store import Store


def test_invalid_identity_rejected_before_creating_data(tmp_path):
    with pytest.raises(ValueError, match="Author"):
        build_server(tmp_path / "invalid", author="Imposter")
    assert not (tmp_path / "invalid").exists()


def test_real_stdio_exchange_between_configured_participants(tmp_path):
    async def session_as(author, model, action):
        params = StdioServerParameters(command=sys.executable, args=["-m", "correspondence.mcp_server"], env={**os.environ, "CORRESPONDENCE_DATA_DIR": str(tmp_path), "CORRESPONDENCE_AUTHOR": author, "CORRESPONDENCE_MODEL": model})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                assert f"configured author is {author}" in info.instructions
                tools = await session.list_tools()
                assert len(tools.tools) == 18
                assert "get_message" in {tool.name for tool in tools.tools}
                for tool in tools.tools:
                    assert "author" not in tool.inputSchema.get("properties", {})
                    assert tool.outputSchema is not None
                schemas = {tool.name: tool.inputSchema["properties"] for tool in tools.tools}
                assert schemas["read_thread"]["mode"]["enum"] == ["oldest", "latest", "unread"]
                assert schemas["read_updates"]["max_bytes"]["minimum"] == 1000
                assert schemas["get_messages"]["message_ids"]["maxItems"] == 50
                info_result = await session.call_tool("board_info", {})
                diagnostic = json.loads(info_result.content[0].text)
                assert diagnostic["participant"] == author
                assert diagnostic["pid"] != os.getpid()
                assert diagnostic["revision"] and diagnostic["started_at"]
                assert diagnostic["idle_timeout_seconds"] == 0
                assert diagnostic["api_version"] == 2
                await action(session)

    async def astra(session):
        result = await session.call_tool("create_project", {"id": "demo", "name": "Demo"})
        assert not result.isError
        result = await session.call_tool("create_thread", {"project_id": "demo", "title": "Review", "body": "Consider this", "request_id": "first"})
        assert not result.isError
        assert json.loads(result.content[0].text)["id"] == 1

    async def fable(session):
        result = await session.call_tool("read_thread", {"thread_id": 1})
        assert not result.isError
        message = json.loads(result.content[0].text)["messages"][0]
        assert message["author"] == "Codex" and message["model"] == "GPT-6 Astra"
        reply = await session.call_tool("post_message", {"thread_id": 1, "body": "Counterexample", "kind": "counterexample", "parent_id": message["id"], "request_id": "reply"})
        assert json.loads(reply.content[0].text)["author"] == "Claude" and json.loads(reply.content[0].text)["model"] == "Fable 5.1"
        exact = await session.call_tool("get_message", {"message_id": message["id"]})
        assert json.loads(exact.content[0].text)["message"] == message
        assert (await session.call_tool("get_message", {"message_id": 999999})).isError
        latest = await session.call_tool("read_thread", {"thread_id": 1, "mode": "latest", "limit": 1})
        latest_data = json.loads(latest.content[0].text)
        assert latest_data["messages"][0]["author"] == "Claude" and latest_data["has_before"]
        assert latest_data["thread"]["last_read_message_id"] == 0
        older = await session.call_tool("read_thread", {"thread_id": 1, "before": latest_data["previous_before"]})
        assert json.loads(older.content[0].text)["messages"] == [message]
        unread = await session.call_tool("read_thread", {"thread_id": 1, "mode": "unread"})
        assert json.loads(unread.content[0].text)["messages"][0] == message
        assert (await session.call_tool("read_thread", {"thread_id": 1, "mode": "latest", "after": 1})).isError
        search = await session.call_tool("search_messages", {"q": "e", "thread_id": 1, "before": latest_data["messages"][0]["id"], "limit": 1})
        assert not search.isError
        assert search.structuredContent["items"][0]["id"] == message["id"]
        mismatch = await session.call_tool("search_messages", {"q": "e", "project_id": "missing", "thread_id": 1})
        assert not mismatch.isError and mismatch.structuredContent["items"] == []
        assert (await session.call_tool("search_messages", {"q": "e", "thread_id": 0})).isError
        assert (await session.call_tool("search_messages", {"q": "e", "before": -1})).isError
        assert not (await session.call_tool("mark_read", {"thread_id": 1, "message_id": message["id"]})).isError

    asyncio.run(session_as("Codex", "GPT-6 Astra", astra))
    asyncio.run(session_as("Claude", "Fable 5.1", fable))
    board = Store(tmp_path)
    assert board.read_thread("Codex", 1)["thread"]["unread_count"] == 1
    assert board.read_thread("Claude", 1)["thread"]["unread_count"] == 0


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_invalid_idle_timeout_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setenv("CORRESPONDENCE_MCP_IDLE_SECONDS", value)
    with pytest.raises(ValueError, match="finite and nonnegative"):
        build_server(tmp_path, author="Codex")


@pytest.mark.parametrize("participant,other", [("Codex", "Claude"), ("Claude", "Codex")])
def test_agent_workflow_over_real_stdio(tmp_path, participant, other):
    board = Store(tmp_path)
    board.create_project("demo", "Demo")
    thread = board.create_thread(other, "demo", "Title only needle", "Opening")
    root = thread["first_message_id"]
    finding = board.post_message(other, thread["id"], "Precise objection", "counterexample", root)
    board.mark_read(participant, thread["id"], finding["id"])

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "correspondence.mcp_server"], env={
            **os.environ, "CORRESPONDENCE_DATA_DIR": str(tmp_path), "CORRESPONDENCE_AUTHOR": participant})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(tool, **args):
                    result = await session.call_tool(tool, args)
                    assert not result.isError, result
                    assert result.structuredContent == json.loads(result.content[0].text)
                    return result.structuredContent

                assert (await call("read_updates", thread_id=thread["id"]))["messages"] == []
                # This task's cursor is independent of another task's shared marker.
                updates = await call("read_updates", thread_id=thread["id"], after=0, limit=1)
                assert updates["has_more"] and updates["messages"][0]["id"] == root
                continuation = await call("read_updates", thread_id=thread["id"], after=updates["next_after"], through=updates["through"])
                assert [m["id"] for m in continuation["messages"]] == [finding["id"]]
                inbox = await call("inbox", project_id="demo", after=0, limit=1)
                assert "body" not in inbox["items"][0] and inbox["has_more"]
                assert (await call("inbox", project_id="demo", unread_only=True))["items"] == []
                matches = await call("search_messages", q="objection", project_id="demo", author_filter=other, kind="counterexample", since="2020-01-01T00:00:00Z")
                assert [m["id"] for m in matches["items"]] == [finding["id"]]
                assert (await call("search_messages", q="needle"))["items"] == []
                assert [t["id"] for t in (await call("search_threads", q="needle"))["items"]] == [thread["id"]]
                context = await call("get_messages", message_ids=[finding["id"], 99999], include_parents=True)
                assert [m["id"] for m in context["messages"]] == [finding["id"], root]
                assert context["missing_ids"] == [99999]
                payload = dict(thread_id=thread["id"], body="    Exact leading indentation\n", parent_id=finding["id"], request_id="safe-retry")
                receipt = await call("post_message", **payload)
                assert "body" not in receipt and receipt["author"] == participant
                assert f"message={receipt['id']}" in receipt["url"]
                assert (await call("post_message", **payload))["id"] == receipt["id"]
                full = await call("post_message", compact=False, **payload)
                assert full["body"].startswith("    Exact") and full["id"] == receipt["id"]
                for tool, args in [("get_messages", {"message_ids": []}), ("read_updates", {"thread_id": True}), ("search_messages", {"q": "x", "kind": "invalid"})]:
                    assert (await session.call_tool(tool, args)).isError

    asyncio.run(exercise())
    assert board.read_thread(participant, thread["id"])["thread"]["last_read_message_id"] == finding["id"]
    with board.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3


def test_stdio_idle_exit_preserves_traffic_and_inflight_call(tmp_path):
    # Exercise the real SDK transport, including its blocked stdin worker. A
    # cancellation-only watchdog hangs here; an unguarded one cuts off slow().
    async def exercise():
        program = """
import anyio
from correspondence.mcp_server import build_server
server = build_server()
@server.tool()
async def slow() -> str:
    await anyio.sleep(0.6)
    return "completed"
server.run()
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", program,
            env={**os.environ, "CORRESPONDENCE_DATA_DIR": str(tmp_path),
                 "CORRESPONDENCE_AUTHOR": "Codex", "CORRESPONDENCE_MCP_IDLE_SECONDS": "0.25"},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

        async def send(frame):
            process.stdin.write((json.dumps({"jsonrpc": "2.0", **frame}) + "\n").encode())
            await process.stdin.drain()

        async def response():
            line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
            assert line, (await process.stderr.read()).decode()
            return json.loads(line)

        try:
            await send({"id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            assert (await response())["id"] == 1
            await send({"method": "notifications/initialized"})
            # Pings are protocol activity even though they are not tool calls.
            for request_id in range(2, 6):
                await asyncio.sleep(0.12)
                await send({"id": request_id, "method": "ping"})
                assert (await response())["id"] == request_id
            await send({"id": 6, "method": "tools/call", "params": {"name": "slow", "arguments": {}}})
            result = await response()
            assert result["id"] == 6 and result["result"]["content"][0]["text"] == "completed"
            # Leave stdin open: reclaiming an idle connected process is the point.
            assert await asyncio.wait_for(process.wait(), timeout=5) == 0
            assert b"Exiting MCP" in await process.stderr.read()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(exercise())


def test_idle_state_guards_both_request_directions_and_output(monkeypatch):
    from correspondence.mcp_server import IdleActivity

    now = [0]
    monkeypatch.setattr("correspondence.mcp_server.time.monotonic", lambda: now[0])
    activity = IdleActivity()
    activity.observe({"id": 1, "method": "tools/call"}, incoming=True)
    now[0] = 100
    assert not activity.expired(10)
    activity.observe({"id": 1, "error": {}}, incoming=False)
    now[0] = 200
    activity.writing = True
    assert not activity.expired(10)
    activity.writing = False
    assert activity.expired(10)
    activity.observe({"id": 2, "method": "ping"}, incoming=False)
    now[0] = 300
    assert not activity.expired(10)
    activity.observe({"id": 2, "result": {}}, incoming=True)
    now[0] = 305
    assert not activity.expired(10)
    now[0] = 311
    assert activity.expired(10)


def test_instructions_fit_client_limit_and_keep_core_rules(tmp_path):
    # Claude Code truncates server instructions near 2,048 characters.
    instructions = build_server(tmp_path, author="Claude").instructions
    assert len(instructions) < 1900
    for rule in ("configured author is Claude", "untrusted", "mark_read", "EVERY earlier message",
                 "read_updates", "request_id", "append-only", 'model="', "archived=true", "paraphrase"):
        assert rule in instructions


def test_compact_project_list_and_self_reported_model(tmp_path):
    board = Store(tmp_path)
    board.create_project("demo", "Demo", "A long description", "/repo")
    thread = board.create_thread("Codex", "demo", "Title", "Opening")

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "correspondence.mcp_server"], env={
            **os.environ, "CORRESPONDENCE_DATA_DIR": str(tmp_path), "CORRESPONDENCE_AUTHOR": "Claude",
            "CORRESPONDENCE_MODEL": "Configured"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                compact = (await session.call_tool("list_projects", {})).structuredContent["items"][0]
                assert "description" not in compact and "created_at" not in compact
                assert compact["id"] == "demo" and compact["repo_path"] == "/repo" and compact["state_token"]
                full = (await session.call_tool("list_projects", {"descriptions": True})).structuredContent["items"][0]
                assert full["description"] == "A long description"
                chosen = await session.call_tool("post_message", {"thread_id": thread["id"], "body": "Mine", "model": "Opus 5.5"})
                assert chosen.structuredContent["model"] == "Opus 5.5"
                fallback = await session.call_tool("post_message", {"thread_id": thread["id"], "body": "Default"})
                assert fallback.structuredContent["model"] == "Configured"
                assert (await session.call_tool("post_message", {"thread_id": thread["id"], "body": "Blank", "model": ""})).isError
                created = await session.call_tool("create_thread", {"project_id": "demo", "title": "T2", "body": "B", "model": "Opus 5.5"})
                assert board.get_message("User", created.structuredContent["first_message_id"])["message"]["model"] == "Opus 5.5"
                status = await session.call_tool("set_thread_status", {"thread_id": thread["id"], "status": "resolved", "model": "Opus 5.5"})
                assert board.get_message("User", status.structuredContent["last_message_id"])["message"]["model"] == "Opus 5.5"

    asyncio.run(exercise())
