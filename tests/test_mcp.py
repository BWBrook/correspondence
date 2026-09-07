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
                assert len(tools.tools) == 10
                for tool in tools.tools:
                    assert "author" not in tool.inputSchema.get("properties", {})
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
        assert not (await session.call_tool("mark_read", {"thread_id": 1, "message_id": message["id"]})).isError

    asyncio.run(session_as("Astra", "GPT-6 Astra", astra))
    asyncio.run(session_as("Fable", "Fable 5.1", fable))
    board = Store(tmp_path)
    assert board.read_thread("Astra", 1)["thread"]["unread_count"] == 1
    assert board.read_thread("Fable", 1)["thread"]["unread_count"] == 0
