"""Carry the board's traffic for a machine that the board's host cannot reach.

The courier runs as a user service on that machine (on ``wsl``, a laptop that
only connects out) and holds one SSH connection to ``correspondence-dispatch
serve`` on the board's host::

    correspondence-courier --machine wsl \\
        --board "ssh mini /path/to/correspondence-dispatch serve --machine wsl" \\
        --forward "ssh mini /path/to/correspondence-turn-hook --append"

Every few seconds it

- reads the main sessions pinned on this machine;
- reports whether each pinned Claude session is running and idle, which also
  tells the board the courier is online;
- puts each waiting Codex prompt into the Codex queue with ``codex queue``.
  The machine's Board Messenger relays Claude prompts, through the same
  ``serve`` channel;
- records Codex turns from each pinned thread's rollout file and sends them
  to the board's turn ledger.

Turn capture reads rollout files because the Codex Windows app runs no hooks,
even approved ones. Codex writes every thread to
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl``. Each
``task_complete`` event becomes the record the Stop hook would have written:
its closing message, turn ID and time, with the model and working directory
from the turn's ``turn_context``. Records go through the turn hook's spool, so
turns made while the board's host is unreachable are sent later. Read
positions are kept in ``courier.json`` in the data directory. A thread seen for
the first time starts at the end of its file, so past turns are not replayed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from correspondence.dispatch import RECONNECT_SECONDS, RemoteBoard, claude_session, deliver_codex
from correspondence.turn_hook import append, data_dir, flush

STATE = "courier.json"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def rollout_path(home: Path, thread: str) -> Path | None:
    found = sorted((home / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl"))
    return found[-1] if found else None


def iso_seconds(timestamp: str) -> str:
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def scan(path: Path, offset: int, context: dict) -> tuple[list[dict], int]:
    """Return the turns completed after ``offset`` and the new offset.

    Only whole lines are consumed. ``context`` carries the model and working
    directory of the latest ``turn_context`` across calls.
    """
    with open(path, "rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
    end = chunk.rfind(b"\n") + 1
    completed = []
    for raw in chunk[:end].splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload") or {}
        if entry.get("type") == "turn_context":
            context.update(model=payload.get("model"), cwd=payload.get("cwd"))
        elif entry.get("type") == "event_msg" and payload.get("type") == "task_complete":
            completed.append({"timestamp": entry.get("timestamp"), "turn_id": payload.get("turn_id"),
                              "message": payload.get("last_agent_message"), **context})
    return completed, offset + end


def turn_record(turn: dict, thread: str, project: str, machine: str) -> dict:
    recorded = iso_seconds(turn["timestamp"]) if turn.get("timestamp") else datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "recorded_at": recorded,
        "agent": "Codex",
        "project": project,
        "session_id": thread,
        "turn_id": turn.get("turn_id"),
        "cwd": turn.get("cwd"),
        "model": turn.get("model"),
        "event": "rollout",
        "message": turn.get("message"),
        "machine": machine,
    }


def load_state(directory: Path) -> dict:
    try:
        return json.loads((directory / STATE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(directory: Path, state: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = directory / (STATE + ".tmp")
    temporary.write_text(json.dumps(state, indent=1))
    temporary.replace(directory / STATE)


def watch(home: Path, directory: Path, threads: dict[str, str], machine: str, state: dict) -> list[dict]:
    """Spool each pinned thread's newly completed turns and return their records.

    ``state`` maps thread to read position; threads no longer pinned are
    dropped, so pinning one again starts at the end of its file.
    """
    for thread in set(state) - set(threads):
        del state[thread]
    records = []
    for thread, project in threads.items():
        known = state.get(thread)
        path = Path(known["path"]) if known and Path(known["path"]).exists() else rollout_path(home, thread)
        if path is None:
            continue
        size = path.stat().st_size
        if known is None or known.get("path") != str(path) or known.get("offset", 0) > size:
            context: dict = {}
            _, offset = scan(path, 0, context)
            state[thread] = {"path": str(path), "offset": offset, "project": project, **context}
            continue
        context = {key: known.get(key) for key in ("model", "cwd")}
        turns, offset = scan(path, known["offset"], context)
        for turn in turns:
            entry = turn_record(turn, thread, project, machine)
            append(entry, directory, "turns-spool.jsonl")
            records.append(entry)
        state[thread] = {"path": str(path), "offset": offset, "project": project, **context}
    return records


def exchange(board, log) -> dict[str, str]:
    """Report pinned Claude sessions and deliver waiting Codex prompts; return the pinned Codex threads."""
    pins = board.pins()
    board.report_sessions({p["session_id"]: claude_session(p["session_id"]) for p in pins if p["agent"] == "Claude"})
    for dispatch in board.waiting("Codex"):
        if board.claim(dispatch["id"]):
            delivered, detail = deliver_codex(dispatch["session_id"], dispatch["body"])
            board.finish(dispatch["id"], delivered, detail)
            log(f"dispatch {dispatch['id']} to Codex {dispatch['session_id']}: "
                f"{'delivered' if delivered else 'failed'} {detail}".strip())
    return {p["session_id"]: p["project_id"] for p in pins if p["agent"] == "Codex"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--machine", required=True, help="this machine's name on the board")
    parser.add_argument("--board", required=True, help="command that runs correspondence-dispatch serve on the board's host")
    parser.add_argument("--forward", required=True, help="command that receives spooled turn lines on stdin")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between rounds")
    parser.add_argument("--once", action="store_true", help="run one round and exit")
    args = parser.parse_args()
    home, directory, board = codex_home(), data_dir(), RemoteBoard(args.board)

    def log(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    state = load_state(directory)
    threads = {thread: entry["project"] for thread, entry in state.items() if entry.get("project")}
    board_retry = forward_retry = 0.0
    while True:
        if time.monotonic() >= board_retry:
            try:
                threads = exchange(board, log)
                board_retry = 0.0
            except Exception as error:
                board_retry = time.monotonic() + RECONNECT_SECONDS
                log(f"board unavailable, retrying in {RECONNECT_SECONDS} s: {error}")
        try:
            records = watch(home, directory, threads, args.machine, state)
            save_state(directory, state)
            for entry in records:
                log(f"turn {entry['recorded_at']} {entry['project']} {entry['turn_id']}")
            if records or time.monotonic() >= forward_retry:
                try:
                    flush(directory, args.forward)
                    forward_retry = 0.0
                except Exception as error:
                    forward_retry = time.monotonic() + RECONNECT_SECONDS
                    log(f"forward failed, will retry: {error}")
        except Exception:
            traceback.print_exc()
        if args.once:
            board.close()
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
