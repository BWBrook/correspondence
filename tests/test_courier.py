import json
import os
import stat
import subprocess
import sys

import pytest

from correspondence import courier
from correspondence.dispatch import (LocalBoard, RemoteBoard, courier_online, remote_claude_session, serve, wait)
from correspondence.store import Conflict, Store

THREAD = "01a0fb34-2415-73e1-8822-a0da876d84fa"
CLAUDE = "2114b555-0c74-405f-bb3d-7860a2e979f1"
HOST_CLAUDE = "f4766779-d9e6-4b66-b078-9f3b4b611a97"


def pair_id(store, project="project"):
    return store.pairs(project)[0]["id"]


def line(kind, payload, timestamp="2026-11-06T02:31:10.948Z"):
    return json.dumps({"timestamp": timestamp, "type": kind, "payload": payload}) + "\n"


def turn(turn_id, message, model="gpt-6-astra", timestamp="2026-11-06T02:31:10.948Z"):
    return (line("event_msg", {"type": "task_started", "turn_id": turn_id}, timestamp)
            + line("turn_context", {"turn_id": turn_id, "model": model, "cwd": "/home/b/papers/project"}, timestamp)
            + line("response_item", {"type": "message", "content": "working"}, timestamp)
            + line("event_msg", {"type": "task_complete", "turn_id": turn_id, "last_agent_message": message}, timestamp))


@pytest.fixture
def world(tmp_path):
    board, local, home, sessions = (tmp_path / name for name in ("board", "local", "codex", "claude-sessions"))
    sessions.mkdir()
    store = Store(board)
    store.create_project("project", "Project")
    store.set_pair_agent(pair_id(store, "project"), "Codex", THREAD, "wsl")
    store.set_pair_agent(pair_id(store, "project"), "Claude", CLAUDE, "wsl")
    rollout = home / "sessions/2026/10/02" / f"rollout-2026-10-02T16-01-33-{THREAD}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(turn("old", "Before the courier started."))
    fake = tmp_path / "fake-codex"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> {tmp_path}/codex-args\necho "Queued message q1 for thread $3."\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": "/usr/bin:/bin", "CODEX_HOME": str(home), "CORRESPONDENCE_DATA_DIR": str(local),
           "CORRESPONDENCE_CODEX": str(fake), "CORRESPONDENCE_CLAUDE_SESSIONS": str(sessions)}
    serve_cmd = f"env CORRESPONDENCE_DATA_DIR={board} {sys.executable} -m correspondence.dispatch serve --machine wsl"
    receiver = f"env CORRESPONDENCE_DATA_DIR={board} {sys.executable} -m correspondence.turn_hook --append"

    def run(board_cmd=serve_cmd, forward=receiver):
        return subprocess.run([sys.executable, "-m", "correspondence.courier", "--once", "--machine", "wsl",
                               "--board", board_cmd, "--forward", forward], text=True, capture_output=True, env=env)

    return {"store": store, "board": board, "local": local, "rollout": rollout, "sessions": sessions, "run": run,
            "serve": serve_cmd, "tmp": tmp_path, "env": env}


def ledger(directory):
    path = directory / "turns.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_pinned_thread_turns_reach_the_board_but_history_is_not_replayed(world):
    assert world["run"]().returncode == 0 and ledger(world["board"]) == []
    with open(world["rollout"], "a") as handle:
        handle.write(turn("new", "A fresh closing message.", model="gpt-6.1-terra", timestamp="2026-11-06T03:00:00.5Z"))
    world["run"]()
    [entry] = ledger(world["board"])
    assert entry["agent"] == "Codex" and entry["project"] == "project" and entry["session_id"] == THREAD
    assert entry["turn_id"] == "new" and entry["model"] == "gpt-6.1-terra" and entry["machine"] == "wsl"
    assert entry["message"] == "A fresh closing message." and entry["recorded_at"] == "2026-11-06T03:00:00+00:00"
    world["run"]()
    assert len(ledger(world["board"])) == 1


def test_unpinned_threads_are_not_watched(world):
    world["run"]()
    world["store"].set_pair_agent(pair_id(world["store"], "project"), "Codex", None)
    with open(world["rollout"], "a") as handle:
        handle.write(turn("t1", "Nobody is listening."))
    world["run"]()
    assert ledger(world["board"]) == []
    assert THREAD not in json.loads((world["local"] / courier.STATE).read_text())


def test_a_half_written_line_waits_and_the_model_survives_a_restart(world, tmp_path):
    state, threads = {}, {THREAD: "project"}
    courier.watch(tmp_path / "codex", tmp_path / "spool", threads, "wsl", state)
    whole = turn("t2", "Split across writes.")
    cut = whole.rindex('{"timestamp"') + 30
    with open(world["rollout"], "a") as handle:
        handle.write(whole[:cut])
    assert courier.watch(tmp_path / "codex", tmp_path / "spool", threads, "wsl", state) == []
    state = json.loads(json.dumps(state))
    with open(world["rollout"], "a") as handle:
        handle.write(whole[cut:])
    [entry] = courier.watch(tmp_path / "codex", tmp_path / "spool", threads, "wsl", state)
    assert entry["message"] == "Split across writes." and entry["model"] == "gpt-6-astra"


def test_turns_wait_while_the_board_is_unreachable(world):
    world["run"]()
    with open(world["rollout"], "a") as handle:
        handle.write(turn("t1", "Sent while offline."))
    result = world["run"](board_cmd="false", forward="false")
    assert "board unavailable" in result.stderr and "forward failed" in result.stderr
    assert ledger(world["board"]) == []
    world["run"]()
    assert [e["message"] for e in ledger(world["board"])] == ["Sent while offline."]
    assert (world["local"] / "turns-spool.jsonl").read_text() == ""


def test_codex_prompts_for_this_machine_are_queued_locally(world):
    store = world["store"]
    store.create_project("host", "Host project")
    store.set_pair_agent(pair_id(store, "host"), "Codex", "01a17cc1-host-thread")
    mine = store.create_dispatch(pair_id(store, "project"), "Codex", "Continue, please.")
    theirs = store.create_dispatch(pair_id(store, "host"), "Codex", "Not for wsl.")
    assert mine["machine"] == "wsl" and theirs["machine"] is None
    result = world["run"]()
    assert "delivered" in result.stderr
    assert (world["tmp"] / "codex-args").read_text().split("\n")[:5] == ["queue", "--thread", THREAD, "--message", "Continue, please."]
    status = {d["id"]: d["status"] for d in store.list_dispatches(pair_id(store, "project")) + store.list_dispatches(pair_id(store, "host"))}
    assert status == {mine["id"]: "delivered", theirs["id"]: "queued"}


def test_a_delivery_whose_report_is_lost_is_reported_next_round(world, monkeypatch):
    monkeypatch.setenv("CORRESPONDENCE_CODEX", world["env"]["CORRESPONDENCE_CODEX"])
    store = world["store"]
    sent = store.create_dispatch(pair_id(store, "project"), "Codex", "Continue, please.")

    class Dropping(LocalBoard):
        drops = 1

        def finish(self, *args):
            if self.drops:
                self.drops -= 1
                raise ConnectionError("board connection closed or timed out")
            return super().finish(*args)

    board, unreported = Dropping(store, "wsl"), {}
    with pytest.raises(ConnectionError):
        courier.exchange(board, lambda text: None, unreported)
    assert store.list_dispatches(pair_id(store, "project"), 1)[0]["status"] == "queued" and sent["id"] in unreported
    courier.exchange(board, lambda text: None, unreported)
    assert store.list_dispatches(pair_id(store, "project"), 1)[0]["status"] == "delivered" and unreported == {}
    assert (world["tmp"] / "codex-args").read_text().count("queue") == 1  # delivered once, reported once


def test_courier_reports_claude_sessions_and_its_own_presence(world):
    board = world["board"]
    assert courier_online(board, "wsl") is False
    (world["sessions"] / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": CLAUDE,
                                                         "name": "Extinction Vortex", "status": "busy"}))
    world["run"]()
    assert courier_online(board, "wsl") is True
    assert remote_claude_session(board, "wsl", CLAUDE) == {"name": "Extinction Vortex", "status": "busy"}


def test_serve_is_scoped_to_its_machine(world, tmp_path):
    store = world["store"]
    store.create_project("host", "Host project")
    store.set_pair_agent(pair_id(store, "host"), "Claude", HOST_CLAUDE)
    host = store.create_dispatch(pair_id(store, "host"), "Claude", "For the Mac mini.")
    requests = [{"op": "waiting", "target": "Claude"}, {"op": "claim", "id": host["id"]},
                {"op": "finish", "id": host["id"], "delivered": True}, {"op": "nonsense"}]
    source, sink = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    source.write_text("".join(json.dumps(r) + "\n" for r in requests))
    with open(source) as stdin, open(sink, "w") as stdout:
        serve(LocalBoard(store, "wsl"), stdin, stdout)
    replies = [json.loads(l) for l in sink.read_text().splitlines()]
    assert replies[0] == {"result": []} and replies[1] == {"result": False}
    assert "not for this machine" in replies[2]["error"] and "error" in replies[3]
    assert store.list_dispatches(pair_id(store, "host"))[0]["status"] == "queued"
    with pytest.raises(Conflict):
        LocalBoard(store, None).store.finish_dispatch(store.create_dispatch(pair_id(store, "project"), "Claude", "x")["id"], True, "")


def test_remote_messenger_relays_a_claude_prompt(world, capsys, monkeypatch):
    monkeypatch.setenv("CORRESPONDENCE_CLAUDE_SESSIONS", str(world["sessions"]))
    (world["sessions"] / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": CLAUDE,
                                                         "name": "Extinction Vortex", "status": "idle"}))
    sent = world["store"].create_dispatch(pair_id(world["store"], "project"), "Claude", "Your turn.")
    assert LocalBoard(world["store"]).waiting("Claude") == []
    board = RemoteBoard(world["serve"])
    try:
        wait(board, poll=0.01)
        event = json.loads(capsys.readouterr().out)
        assert event["dispatch"] == sent["id"] and event["to"] == "Extinction Vortex"
        assert event["message"].endswith("\n\nYour turn.")
        assert board.finish(sent["id"], True, "") == "delivered"
    finally:
        board.close()
    assert (world["board"] / "messenger-heartbeat-wsl").exists()
    assert not (world["board"] / "messenger-heartbeat").exists()


def test_announce_sends_a_new_threads_latest_turn_so_it_can_be_pinned(world):
    world["store"].set_pair_agent(pair_id(world["store"], "project"), "Codex", None)
    with open(world["rollout"], "a") as handle:
        handle.write(turn("latest", "The newest closing message."))
    result = subprocess.run([sys.executable, "-m", "correspondence.courier", "--once", "--machine", "wsl",
                             "--board", world["serve"], "--forward",
                             f"env CORRESPONDENCE_DATA_DIR={world['board']} {sys.executable} -m correspondence.turn_hook --append",
                             "--announce", f"project={THREAD}"],
                            text=True, capture_output=True, env=world["env"])
    assert "announced" in result.stderr
    assert [(e["turn_id"], e["machine"]) for e in ledger(world["board"])] == [("latest", "wsl")]


def test_compressed_threads_are_read_and_a_restored_file_sends_only_new_turns(world, tmp_path):
    from compression import zstd
    rollout, home = world["rollout"], tmp_path / "codex"
    packed = rollout.with_name(rollout.name + ".zst")
    packed.write_bytes(zstd.compress(rollout.read_bytes()))
    old_text = rollout.read_text()
    rollout.unlink()
    state, threads = {}, {THREAD: "project"}
    assert courier.watch(home, tmp_path / "spool", threads, "wsl", state) == []
    assert state[THREAD]["path"].endswith(".zst") and state[THREAD]["model"] == "gpt-6-astra"
    assert courier.watch(home, tmp_path / "spool", threads, "wsl", state) == []
    rollout.write_text(old_text + turn("resumed", "Back from the archive.", timestamp="2026-11-07T00:00:00Z"))
    packed.unlink()
    assert [e["turn_id"] for e in courier.watch(home, tmp_path / "spool", threads, "wsl", state)] == ["resumed"]
    rollout.unlink()
    rollout.with_name("rollout-2026-11-08T00-00-00-" + THREAD + ".jsonl").write_text(
        turn("fresh", "A new file with only new turns.", timestamp="2026-11-08T00:00:00Z"))
    assert [e["turn_id"] for e in courier.watch(home, tmp_path / "spool", threads, "wsl", state)] == ["fresh"]


def test_announce_introduces_a_claude_session(world, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    transcript = tmp_path / ".claude/projects/-Users-b-games-x" / f"{CLAUDE}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5",
                          "content": [{"type": "text", "text": "Ready when you are."}]}}) + "\n")
    entry = courier.announce(tmp_path / "codex", tmp_path / "spool", CLAUDE, "project", "macbook")
    assert (entry["agent"], entry["model"], entry["message"], entry["machine"]) == ("Claude", "claude-opus-5-5", "Ready when you are.", "macbook")
    assert courier.announce(tmp_path / "codex", tmp_path / "spool", "no-such-session-id", "project", "macbook") is None


def test_two_remote_codex_pairs_are_delivered_and_watched_separately(world):
    store = world['store']
    main = pair_id(store)
    second = store.create_pair('project', 'Worktree')['id']
    second_thread = '01a0fb34-worktree-thread'
    store.set_pair_agent(second, 'Codex', second_thread, 'wsl')
    other_rollout = world['rollout'].with_name('rollout-2026-10-02T16-01-34-' + second_thread + '.jsonl')
    other_rollout.write_text(turn('old-second', 'Earlier worktree turn.'))
    assert world['run']().returncode == 0
    assert ledger(world['board']) == []
    first = store.create_dispatch(main, 'Codex', 'Main pair prompt.')
    other = store.create_dispatch(second, 'Codex', 'Worktree pair prompt.')
    result = world['run']()
    assert result.returncode == 0
    args = (world['tmp'] / 'codex-args').read_text().splitlines()
    assert args == ['queue', '--thread', THREAD, '--message', 'Main pair prompt.',
                    'queue', '--thread', second_thread, '--message', 'Worktree pair prompt.']
    assert [(d['id'], d['pair_id'], d['session_id'], d['status']) for d in store.list_dispatches(main)] == [(first['id'], main, THREAD, 'delivered')]
    assert [(d['id'], d['pair_id'], d['session_id'], d['status']) for d in store.list_dispatches(second)] == [(other['id'], second, second_thread, 'delivered')]
    with open(world['rollout'], 'a') as handle:
        handle.write(turn('main-next', 'Main reply.', timestamp='2026-11-06T05:00:00Z'))
    with open(other_rollout, 'a') as handle:
        handle.write(turn('worktree-next', 'Worktree reply.', timestamp='2026-11-06T05:00:01Z'))
    assert world['run']().returncode == 0
    assert {(r['session_id'], r['message']) for r in ledger(world['board'])} == {
        (THREAD, 'Main reply.'), (second_thread, 'Worktree reply.'),
    }
    store.set_pair_agent(second, 'Codex', None)
    with open(other_rollout, 'a') as handle:
        handle.write(turn('unwatched', 'Do not capture.', timestamp='2026-11-06T05:00:02Z'))
    with open(world['rollout'], 'a') as handle:
        handle.write(turn('still-watched', 'Main continues.', timestamp='2026-11-06T05:00:03Z'))
    assert world['run']().returncode == 0
    assert ledger(world['board'])[-1]['message'] == 'Main continues.'
    assert 'Do not capture.' not in {r['message'] for r in ledger(world['board'])}


def test_lost_delivery_outcome_does_not_duplicate_either_pairs_prompt(world, monkeypatch):
    monkeypatch.setenv('CORRESPONDENCE_CODEX', world['env']['CORRESPONDENCE_CODEX'])
    store = world['store']
    main = pair_id(store)
    second = store.create_pair('project', 'Worktree')['id']
    second_thread = '01a0fb34-worktree-thread'
    store.set_pair_agent(second, 'Codex', second_thread, 'wsl')
    first = store.create_dispatch(main, 'Codex', 'Main only.')
    other = store.create_dispatch(second, 'Codex', 'Worktree only.')

    class Dropping(LocalBoard):
        drops = 1

        def finish(self, *args):
            if self.drops:
                self.drops -= 1
                raise ConnectionError('Lost acknowledgement')
            return super().finish(*args)

    board, unreported = Dropping(store, 'wsl'), {}
    with pytest.raises(ConnectionError):
        courier.exchange(board, lambda text: None, unreported)
    assert set(unreported) == {first['id']}
    assert store.list_dispatches(second)[0]['claimed_at'] is None
    threads = courier.exchange(board, lambda text: None, unreported)
    assert threads == {THREAD: 'project', second_thread: 'project'}
    assert unreported == {}
    courier.exchange(board, lambda text: None, unreported)
    args = (world['tmp'] / 'codex-args').read_text().splitlines()
    assert args == ['queue', '--thread', THREAD, '--message', 'Main only.',
                    'queue', '--thread', second_thread, '--message', 'Worktree only.']
    assert store.list_dispatches(main)[0]['status'] == store.list_dispatches(second)[0]['status'] == 'delivered'
    assert store.list_dispatches(main)[0]['id'] == first['id']
    assert store.list_dispatches(second)[0]['id'] == other['id']
