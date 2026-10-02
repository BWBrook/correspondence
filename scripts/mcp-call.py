#!/usr/bin/env python3
"""Call a board tool through MCP without invoking a model.

Example: uv run python scripts/mcp-call.py --author Astra list_projects
Arguments may be a JSON object, @file.json, or - for stdin JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class CLIError(Exception):
    def __init__(self, code, message, status=1):
        super().__init__(message)
        self.code, self.status = code, status


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError("invalid_arguments", message, 2)


def parse_input(argv):
    parser = Parser(description=__doc__, add_help=False)
    parser.add_argument("--help", action="store_true", help="Show usage as JSON")
    parser.add_argument("--author", choices=["Barry", "Astra", "Fable"])
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=30, help="Whole-session timeout in seconds (default: 30)")
    discovery = parser.add_mutually_exclusive_group()
    discovery.add_argument("--list-tools", action="store_true", help="List available tool names and descriptions")
    discovery.add_argument("--describe", metavar="TOOL", help="Show a tool's complete schema")
    parser.add_argument("--body-file", metavar="FILE", help="Read body for post_message/create_thread from UTF-8 file, or - for stdin")
    parser.add_argument("tool", nargs="?")
    parser.add_argument("arguments", nargs="?", help="JSON object, @file.json, or - for stdin")
    args = parser.parse_args(argv)
    if args.help:
        return args, {"help": parser.format_help()}
    if not args.author:
        parser.error("--author is required")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and greater than zero")
    if args.list_tools or args.describe:
        if args.tool is not None or args.arguments is not None or args.body_file is not None:
            parser.error("Tool discovery cannot be combined with a tool call or --body-file")
        return args, {}
    if not args.tool:
        parser.error("A tool, --list-tools, or --describe TOOL is required")
    if args.body_file is not None and args.tool not in {"post_message", "create_thread"}:
        parser.error("--body-file is only valid for post_message and create_thread")
    if args.arguments == "-" and args.body_file == "-":
        parser.error("stdin cannot supply both arguments and body")
    try:
        source = args.arguments or "{}"
        if source == "-":
            source = sys.stdin.read()
        elif source.startswith("@"):
            source = Path(source[1:]).read_text(encoding="utf-8")
        arguments = json.loads(source)
        if not isinstance(arguments, dict):
            parser.error("Arguments must be a JSON object")
        if args.body_file is not None:
            if "body" in arguments:
                parser.error("Provide body in arguments or --body-file, not both")
            arguments["body"] = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CLIError("invalid_arguments", str(exc), 2) from None
    return args, arguments


def decode_result(result):
    if result.structuredContent is not None:
        return result.structuredContent
    values = []
    for content in result.content:
        if content.type == "text":
            try:
                values.append(json.loads(content.text))
            except json.JSONDecodeError:
                values.append(content.text)
    return values[0] if len(values) == 1 else values


async def run(args, arguments, error_log):
    env = dict(os.environ, CORRESPONDENCE_AUTHOR=args.author)
    env.pop("CORRESPONDENCE_MODEL", None)
    if args.model:
        env["CORRESPONDENCE_MODEL"] = args.model
    # Keep SDK request chatter off stderr; warnings and errors remain captured.
    program = "import logging; logging.disable(logging.INFO); from correspondence.mcp_server import main; main()"
    params = StdioServerParameters(command=sys.executable, args=["-c", program], env=env)
    async with asyncio.timeout(args.timeout):
        async with stdio_client(params, errlog=error_log) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if args.list_tools or args.describe:
                    tools = []
                    cursor = None
                    while True:
                        page = await session.list_tools(cursor=cursor)
                        tools.extend(page.tools)
                        cursor = page.nextCursor
                        if not cursor:
                            break
                    if args.list_tools:
                        return [{"name": tool.name, "description": tool.description} for tool in tools]
                    for tool in tools:
                        if tool.name == args.describe:
                            return tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    raise CLIError("unknown_tool", f"Unknown tool: {args.describe}")
                result = await session.call_tool(args.tool, arguments)
                value = decode_result(result)
                if result.isError:
                    message = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    raise CLIError("tool_error", message)
                return value


def error_leaves(exc):
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for child in exc.exceptions for leaf in error_leaves(child)]
    return [exc]


def main(argv=None):
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as error_log:
        try:
            args, arguments = parse_input(argv)
            value = arguments if args.help else asyncio.run(run(args, arguments, error_log))
            envelope, status = {"ok": True, "result": value}, 0
        except (Exception, KeyboardInterrupt) as exc:
            leaves = error_leaves(exc)
            error = next((leaf for leaf in leaves if isinstance(leaf, CLIError)), None)
            if error is None:
                if any(isinstance(leaf, TimeoutError) for leaf in leaves):
                    message = "MCP session timed out."
                    if args.tool in {"post_message", "create_thread"}:
                        message += " A write may have completed; inspect the board or retry with the same request_id."
                    elif args.tool in {"create_project", "mark_read", "set_thread_status", "archive_project", "restore_project"}:
                        message += " A write may have completed; inspect the board before retrying."
                    error = CLIError("timeout", message)
                elif any(isinstance(leaf, KeyboardInterrupt) for leaf in leaves):
                    error = CLIError("interrupted", "Interrupted; a write may have completed.", 130)
                else:
                    message = "; ".join(dict.fromkeys(str(leaf) or type(leaf).__name__ for leaf in leaves))
                    error = CLIError("transport_error", message)
            envelope = {"ok": False, "error": {"code": error.code, "message": str(error)}}
            status = error.status
            print(f"{error.code}: {error}", file=sys.stderr)
        error_log.seek(0)
        diagnostics = error_log.read().strip().splitlines()
        if diagnostics:
            # A server startup traceback is reduced to its final diagnostic line.
            print(f"server: {diagnostics[-1]}", file=sys.stderr)
        print(json.dumps(envelope, ensure_ascii=False))
        return status


if __name__ == "__main__":
    raise SystemExit(main())
