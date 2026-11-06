import json
import subprocess
import sys

from correspondence.turn_hook import closing_from_transcript, record


def run_hook(tmp_path, stdin, agent="Codex"):
    env = {"CORRESPONDENCE_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, "-m", "correspondence.turn_hook", "--agent", agent, "--project", "p"],
        input=stdin, text=True, capture_output=True, env=env,
    )
    return result, tmp_path / "turns.jsonl"


def test_codex_stop_payload_is_appended(tmp_path):
    payload = {"hook_event_name": "Stop", "session_id": "s1", "turn_id": "t1", "cwd": "/w", "model": "m",
               "stop_hook_active": False, "last_assistant_message": "Done. Over to Claude.", "transcript_path": None}
    for _ in range(2):
        result, ledger = run_hook(tmp_path, json.dumps(payload))
        assert result.returncode == 0 and result.stdout == ""
    lines = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["agent"] == "Codex" and lines[0]["project"] == "p" and lines[0]["turn_id"] == "t1"
    assert lines[0]["message"] == "Done. Over to Claude."


def test_claude_message_falls_back_to_last_assistant_text_in_transcript(tmp_path):
    transcript = tmp_path / "t.jsonl"
    entries = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Earlier."}]}},
        {"type": "user", "message": {"content": "tool result"}},
        {"type": "assistant", "message": {"model": "claude-x", "content": [{"type": "text", "text": "Closing."}, {"type": "tool_use", "name": "x"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "y"}]}},
        "not json",
    ]
    transcript.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in entries) + "\n")
    assert closing_from_transcript(str(transcript)) == ("Closing.", "claude-x")
    entry = record({"session_id": "s", "transcript_path": str(transcript)}, "Claude", "p")
    assert entry["message"] == "Closing." and entry["model"] == "claude-x" and entry["turn_id"] is None
    entry = record({"last_assistant_message": "From payload.", "transcript_path": str(transcript)}, "Claude", "p")
    assert entry["message"] == "From payload." and entry["model"] == "claude-x"


def test_bad_input_never_fails_the_turn(tmp_path):
    result, ledger = run_hook(tmp_path, "{not json")
    assert result.returncode == 0 and result.stdout == ""
    assert not ledger.exists()
    assert "JSONDecodeError" in (tmp_path / "turn-hook-errors.log").read_text()


def run_forward(tmp_path, forward_cmd, message):
    local, env = tmp_path / "local", {"PATH": "/usr/bin:/bin"}
    payload = {"session_id": "s", "turn_id": "t", "last_assistant_message": message}
    return subprocess.run(
        [sys.executable, "-m", "correspondence.turn_hook", "--agent", "Codex", "--project", "p", "--machine", "wsl",
         "--forward", forward_cmd],
        input=json.dumps(payload), text=True, capture_output=True,
        env={**env, "CORRESPONDENCE_DATA_DIR": str(local)}), local


def test_forward_spools_while_the_board_is_unreachable_then_flushes(tmp_path):
    board = tmp_path / "board"
    receiver = f"env CORRESPONDENCE_DATA_DIR={board} {sys.executable} -m correspondence.turn_hook --append"
    result, local = run_forward(tmp_path, "false", "First, while offline.")
    assert result.returncode == 0 and not (board / "turns.jsonl").exists()
    assert "forward failed" in (local / "turn-hook-errors.log").read_text()
    result, _ = run_forward(tmp_path, receiver, "Second, back online.")
    assert result.returncode == 0
    lines = [json.loads(l) for l in (board / "turns.jsonl").read_text().splitlines()]
    assert [l["message"] for l in lines] == ["First, while offline.", "Second, back online."]
    assert lines[0]["machine"] == "wsl" and (local / "turns-spool.jsonl").read_text() == ""


def test_append_rejects_lines_that_are_not_turns(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "CORRESPONDENCE_DATA_DIR": str(tmp_path)}
    bad = subprocess.run([sys.executable, "-m", "correspondence.turn_hook", "--append"], input='{"agent": "Mallory"}\n',
                         text=True, capture_output=True, env=env)
    assert bad.returncode != 0 and not (tmp_path / "turns.jsonl").exists()


def test_resent_lines_are_shown_once(tmp_path):
    from correspondence.turns import read_turns
    line = json.dumps({"recorded_at": "2026-11-06T01:00:00+00:00", "agent": "Codex", "project": "p", "message": "m"})
    (tmp_path / "turns.jsonl").write_text(f"{line}\n{line}\n")
    assert len(read_turns(tmp_path)) == 1
