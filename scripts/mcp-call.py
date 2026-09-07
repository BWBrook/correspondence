#!/usr/bin/env python3
"""Call a board tool through the real MCP transport, without invoking a model.

Example: uv run python scripts/mcp-call.py --author Astra list_projects
Use primarily for installation checks or a session whose tools need reloading.
"""
import argparse
import asyncio
import json
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--author", required=True, choices=["Barry", "Astra", "Fable"])
parser.add_argument("--model")
parser.add_argument("tool")
parser.add_argument("arguments", nargs="?", default="{}", help="JSON object, or @file.json")
args = parser.parse_args()
if args.arguments.startswith("@"):
    from pathlib import Path
    arguments = json.loads(Path(args.arguments[1:]).read_text())
else:
    arguments = json.loads(args.arguments)
if not isinstance(arguments, dict):
    raise SystemExit("Arguments must be a JSON object")

async def run():
    env = dict(os.environ, CORRESPONDENCE_AUTHOR=args.author)
    env.pop("CORRESPONDENCE_MODEL", None)
    if args.model:
        env["CORRESPONDENCE_MODEL"] = args.model
    params = StdioServerParameters(command=sys.executable, args=["-m", "correspondence.mcp_server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(args.tool, arguments)
            for content in result.content:
                if content.type == "text":
                    print(content.text)
            if result.isError:
                raise SystemExit(1)

asyncio.run(run())
