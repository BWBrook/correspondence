import json
import subprocess
import sys

from correspondence.turn_hook import last_text_from_transcript, record


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
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Closing."}, {"type": "tool_use", "name": "x"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "y"}]}},
        "not json",
    ]
    transcript.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in entries) + "\n")
    assert last_text_from_transcript(str(transcript)) == "Closing."
    entry = record({"session_id": "s", "transcript_path": str(transcript)}, "Claude", "p")
    assert entry["message"] == "Closing." and entry["turn_id"] is None


def test_bad_input_never_fails_the_turn(tmp_path):
    result, ledger = run_hook(tmp_path, "{not json")
    assert result.returncode == 0 and result.stdout == ""
    assert not ledger.exists()
    assert "JSONDecodeError" in (tmp_path / "turn-hook-errors.log").read_text()
