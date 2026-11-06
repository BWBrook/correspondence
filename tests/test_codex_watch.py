import json
import subprocess
import sys

from correspondence import codex_watch

THREAD = "01a0fb34-2415-73e1-8822-a0da876d84fa"


def line(kind, payload, timestamp="2026-11-06T02:31:10.948Z"):
    return json.dumps({"timestamp": timestamp, "type": kind, "payload": payload}) + "\n"


def turn(turn_id, message, model="gpt-6-astra", timestamp="2026-11-06T02:31:10.948Z"):
    return (line("event_msg", {"type": "task_started", "turn_id": turn_id}, timestamp)
            + line("turn_context", {"turn_id": turn_id, "model": model, "cwd": "/home/b/papers/project"}, timestamp)
            + line("response_item", {"type": "message", "content": "working"}, timestamp)
            + line("event_msg", {"type": "task_complete", "turn_id": turn_id, "last_agent_message": message}, timestamp))


def rollout(home):
    path = home / "sessions/2026/10/02" / f"rollout-2026-10-02T16-01-33-{THREAD}.jsonl"
    path.parent.mkdir(parents=True)
    return path


def watch(home, data, forward=None):
    command = [sys.executable, "-m", "correspondence.codex_watch", "--once", "--machine", "wsl",
               "--thread", f"project={THREAD}"] + (["--forward", forward] if forward else [])
    return subprocess.run(command, text=True, capture_output=True,
                          env={"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home), "CORRESPONDENCE_DATA_DIR": str(data)})


def ledger(directory):
    path = directory / "turns.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_new_turns_are_recorded_but_history_is_not_replayed(tmp_path):
    home, data = tmp_path / "codex", tmp_path / "data"
    path = rollout(home)
    path.write_text(turn("old", "Before the watcher started."))
    assert watch(home, data).returncode == 0 and ledger(data) == []
    with open(path, "a") as handle:
        handle.write(turn("new", "A fresh closing message.", model="gpt-6.1-terra", timestamp="2026-11-06T03:00:00.5Z"))
    watch(home, data)
    [entry] = ledger(data)
    assert entry["agent"] == "Codex" and entry["project"] == "project" and entry["session_id"] == THREAD
    assert entry["turn_id"] == "new" and entry["model"] == "gpt-6.1-terra" and entry["machine"] == "wsl"
    assert entry["message"] == "A fresh closing message." and entry["recorded_at"] == "2026-11-06T03:00:00+00:00"
    watch(home, data)
    assert len(ledger(data)) == 1


def test_a_half_written_line_waits_for_the_rest(tmp_path):
    home, data = tmp_path / "codex", tmp_path / "data"
    path = rollout(home)
    path.write_text(turn("old", "Earlier."))
    watch(home, data)
    whole = turn("t2", "Split across two writes.")
    cut = len(whole) - 20
    with open(path, "a") as handle:
        handle.write(whole[:cut])
    watch(home, data)
    assert ledger(data) == []
    with open(path, "a") as handle:
        handle.write(whole[cut:])
    watch(home, data)
    assert [e["message"] for e in ledger(data)] == ["Split across two writes."]


def test_model_survives_a_restart_between_context_and_completion(tmp_path):
    home, data = tmp_path / "codex", tmp_path / "data"
    path = rollout(home)
    whole = turn("t1", "Finished after a restart.")
    complete_at = whole.rindex(line("event_msg", {"type": "task_complete", "turn_id": "t1",
                                                   "last_agent_message": "Finished after a restart."}))
    path.write_text(turn("old", "Earlier.") + whole[:complete_at])
    watch(home, data)
    with open(path, "a") as handle:
        handle.write(whole[complete_at:])
    watch(home, data)
    [entry] = ledger(data)
    assert entry["model"] == "gpt-6-astra" and entry["cwd"] == "/home/b/papers/project"


def test_forwarded_turns_wait_in_the_spool_while_the_board_is_unreachable(tmp_path):
    home, data, board = tmp_path / "codex", tmp_path / "data", tmp_path / "board"
    path = rollout(home)
    path.write_text("")
    watch(home, data, "false")
    with open(path, "a") as handle:
        handle.write(turn("t1", "Sent while offline."))
    result = watch(home, data, "false")
    assert "forward failed" in result.stderr and ledger(board) == []
    receiver = f"env CORRESPONDENCE_DATA_DIR={board} {sys.executable} -m correspondence.turn_hook --append"
    watch(home, data, receiver)
    assert [e["message"] for e in ledger(board)] == ["Sent while offline."]
    assert (data / "turns-spool.jsonl").read_text() == ""


def test_thread_arguments_name_a_project(tmp_path):
    assert codex_watch.parse_threads([f"project={THREAD}"]) == {THREAD: "project"}
    assert watch(tmp_path, tmp_path, None).returncode == 0
    bad = subprocess.run([sys.executable, "-m", "correspondence.codex_watch", "--once", "--thread", THREAD],
                         text=True, capture_output=True)
    assert bad.returncode != 0
