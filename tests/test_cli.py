"""CLI contract checks against real isolated stdio servers."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mcp-call.py"


@pytest.fixture
def cli(tmp_path):
    def call(*args, stdin=None, author="Astra"):
        process = subprocess.run(
            [sys.executable, str(SCRIPT), "--author", author, *args],
            input=stdin, text=True, capture_output=True, cwd=ROOT, timeout=15,
            env={**os.environ, "CORRESPONDENCE_DATA_DIR": str(tmp_path / "data"),
                 "CORRESPONDENCE_MCP_IDLE_SECONDS": "0"},
        )
        assert len(process.stdout.splitlines()) == 1, process
        payload = json.loads(process.stdout)
        assert payload["ok"] == (process.returncode == 0), process
        assert "Traceback" not in process.stderr
        assert "Processing request of type" not in process.stderr
        if process.returncode:
            assert payload["error"]["code"] and payload["error"]["message"]
            assert process.stderr.strip()
        else:
            assert process.stderr == "", process.stderr
        return payload, process
    return call


def items(value):
    # The MCP list tools return a structured envelope; preserve it at CLI level.
    assert isinstance(value, dict)
    return value.get("items", value.get("result"))


def test_empty_list_object_many_and_tool_error(cli):
    result, _ = cli("list_projects")
    assert items(result["result"]) == []
    result, _ = cli("create_project", '{"id":"demo","name":"Demo"}')
    assert result["result"]["id"] == "demo"
    cli("create_project", '{"id":"second","name":"Second"}')
    result, _ = cli("list_projects")
    assert {project["id"] for project in items(result["result"])} == {"demo", "second"}
    result, _ = cli("get_message", '{"message_id":99999}')
    assert result["error"]["code"] == "tool_error"
    result, _ = cli("get_message", '{}')
    assert result["error"]["code"] == "tool_error"


def test_discovery_returns_names_and_input_schema(cli):
    result, _ = cli("--list-tools")
    assert "post_message" in {tool["name"] for tool in result["result"]}
    result, _ = cli("--describe", "post_message")
    assert result["result"]["name"] == "post_message"
    assert "thread_id" in result["result"]["inputSchema"]["required"]
    assert "author" not in result["result"]["inputSchema"]["properties"]
    result, _ = cli("--describe", "unknown")
    assert result["error"]["code"] == "unknown_tool"


def test_file_and_stdin_body_keep_whitespace_and_request_retry(cli, tmp_path):
    cli("create_project", '{"id":"demo","name":"Demo"}')
    body = "    indented start\n\n  UTF-8: café 森林\n"
    body_file = tmp_path / "body.txt"
    body_file.write_text(body, encoding="utf-8")
    arguments = {"project_id": "demo", "title": "Review", "request_id": "thread-retry"}
    args_file = tmp_path / "arguments.json"
    args_file.write_text(json.dumps(arguments), encoding="utf-8")
    result, _ = cli("create_thread", f"@{args_file}", "--body-file", str(body_file))
    thread = result["result"]
    retried, _ = cli("create_thread", "-", "--body-file", str(body_file), stdin=json.dumps(arguments))
    assert retried["result"]["id"] == thread["id"]
    result, _ = cli("get_message", json.dumps({"message_id": thread["first_message_id"]}))
    assert result["result"]["message"]["body"] == body.rstrip()
    reply = {"thread_id": thread["id"], "request_id": "reply-retry"}
    result, _ = cli("post_message", json.dumps(reply), "--body-file", "-", stdin=body, author="Fable")
    repeated, _ = cli("post_message", json.dumps(reply), "--body-file", "-", stdin=body, author="Fable")
    assert repeated["result"]["id"] == result["result"]["id"]
    exact, _ = cli("get_message", json.dumps({"message_id": result["result"]["id"]}))
    assert exact["result"]["message"]["body"] == body.rstrip()
    assert exact["result"]["message"]["author"] == "Fable"


@pytest.mark.parametrize("arguments", [
    ["get_message", "{"], ["get_message", "[]"],
    ["post_message", '{"body":"ambiguous"}', "--body-file", "missing.txt"],
    ["post_message", "-", "--body-file", "-"],
    ["list_projects", "--body-file", "missing.txt"],
    ["get_message", "@missing.json"],
    ["--list-tools", "get_message"],
    ["--timeout", "nan", "list_projects"],
    ["--timeout", "0", "list_projects"],
])
def test_invalid_input_is_json_without_traceback(cli, arguments):
    result, process = cli(*arguments)
    assert result["error"]["code"] == "invalid_arguments"
    assert process.returncode == 2


def test_timeout_is_bounded_and_json(cli):
    result, _ = cli("--timeout", "0.000001", "list_projects")
    assert result["error"]["code"] == "timeout"


def test_fallback_decoding_preserves_lists_text_and_structured_data():
    spec = importlib.util.spec_from_file_location("board_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def result(texts, structured=None):
        return SimpleNamespace(structuredContent=structured,
                               content=[SimpleNamespace(type="text", text=text) for text in texts])
    assert module.decode_result(result([])) == []
    assert module.decode_result(result(['{"id":1}', '{"id":2}'])) == [{"id": 1}, {"id": 2}]
    assert module.decode_result(result(["Markdown export"])) == "Markdown export"
    assert module.decode_result(result(["ignored"], {"items": []})) == {"items": []}


def test_server_startup_failure_is_json_without_traceback(cli, tmp_path):
    (tmp_path / "data").write_text("Not a directory", encoding="utf-8")
    result, process = cli("list_projects")
    assert result["error"]["code"] == "transport_error"
    assert "FileExistsError" in process.stderr
