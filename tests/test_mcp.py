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
                assert len(tools.tools) == 12
                assert "get_message" in {tool.name for tool in tools.tools}
                for tool in tools.tools:
                    assert "author" not in tool.inputSchema.get("properties", {})
                info_result = await session.call_tool("board_info", {})
                diagnostic = json.loads(info_result.content[0].text)
                assert diagnostic["participant"] == author
                assert diagnostic["pid"] != os.getpid()
                assert diagnostic["revision"] and diagnostic["started_at"]
                assert diagnostic["idle_timeout_seconds"] == 0
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
        assert message["author"] == "Astra" and message["model"] == "GPT-6 Astra"
        reply = await session.call_tool("post_message", {"thread_id": 1, "body": "Counterexample", "kind": "counterexample", "parent_id": message["id"], "request_id": "reply"})
        assert json.loads(reply.content[0].text)["author"] == "Fable" and json.loads(reply.content[0].text)["model"] == "Fable 5.1"
        exact = await session.call_tool("get_message", {"message_id": message["id"]})
        assert json.loads(exact.content[0].text)["message"] == message
        assert (await session.call_tool("get_message", {"message_id": 999999})).isError
        latest = await session.call_tool("read_thread", {"thread_id": 1, "mode": "latest", "limit": 1})
        latest_data = json.loads(latest.content[0].text)
        assert latest_data["messages"][0]["author"] == "Fable" and latest_data["has_before"]
        assert latest_data["thread"]["last_read_message_id"] == 0
        older = await session.call_tool("read_thread", {"thread_id": 1, "before": latest_data["previous_before"]})
        assert json.loads(older.content[0].text)["messages"] == [message]
        unread = await session.call_tool("read_thread", {"thread_id": 1, "mode": "unread"})
        assert json.loads(unread.content[0].text)["messages"][0] == message
        assert (await session.call_tool("read_thread", {"thread_id": 1, "mode": "latest", "after": 1})).isError
        search = await session.call_tool("search_messages", {"q": "e", "thread_id": 1, "before": latest_data["messages"][0]["id"], "limit": 1})
        assert not search.isError
        assert json.loads(search.content[0].text)["id"] == message["id"]
        mismatch = await session.call_tool("search_messages", {"q": "e", "project_id": "missing", "thread_id": 1})
        assert not mismatch.isError and mismatch.content == []
        assert (await session.call_tool("search_messages", {"q": "e", "thread_id": 0})).isError
        assert (await session.call_tool("search_messages", {"q": "e", "before": -1})).isError
        assert not (await session.call_tool("mark_read", {"thread_id": 1, "message_id": message["id"]})).isError

    asyncio.run(session_as("Astra", "GPT-6 Astra", astra))
    asyncio.run(session_as("Fable", "Fable 5.1", fable))
    board = Store(tmp_path)
    assert board.read_thread("Astra", 1)["thread"]["unread_count"] == 1
    assert board.read_thread("Fable", 1)["thread"]["unread_count"] == 0


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_invalid_idle_timeout_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setenv("CORRESPONDENCE_MCP_IDLE_SECONDS", value)
    with pytest.raises(ValueError, match="finite and nonnegative"):
        build_server(tmp_path, author="Astra")


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
                 "CORRESPONDENCE_AUTHOR": "Astra", "CORRESPONDENCE_MCP_IDLE_SECONDS": "0.25"},
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
