"""Record Codex turns from the thread's rollout file where Stop hooks do not run.

The Codex Windows app does not run hooks, even approved ones, so on a machine
whose Codex threads live in that app the turn ledger is fed from each thread's
own history instead::

    correspondence-codex-watch --machine wsl \\
        --forward "ssh mini /path/to/correspondence-turn-hook --append" \\
        --thread project=01a0fb34-2415-73e1-8822-a0da876d84fa

Codex writes every thread to ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl``.
Each ``task_complete`` event becomes the record the Stop hook would have
written: its closing message, turn ID and time, with the model and working
directory from the turn's ``turn_context``. Records go through the turn hook's
spool, so turns made while the board's host is unreachable are sent later.

The read position of each thread is kept in ``codex-watch.json`` in the data
directory. A thread seen for the first time starts at the end of its file, so
past turns are not replayed.
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

from correspondence.turn_hook import append, data_dir, flush

STATE = "codex-watch.json"
RETRY_SECONDS = 60


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


def turn_record(turn: dict, thread: str, project: str, machine: str | None) -> dict:
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
        **({"machine": machine} if machine else {}),
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


def poll(home: Path, directory: Path, threads: dict[str, str], machine: str | None, state: dict,
         spool: bool) -> list[dict]:
    """Record each thread's newly completed turns, in the spool or the ledger, and return them."""
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
            state[thread] = {"path": str(path), "offset": offset, **context}
            continue
        context = {key: known.get(key) for key in ("model", "cwd")}
        turns, offset = scan(path, known["offset"], context)
        for turn in turns:
            entry = turn_record(turn, thread, project, machine)
            append(entry, directory, "turns-spool.jsonl" if spool else "turns.jsonl")
            records.append(entry)
        state[thread] = {"path": str(path), "offset": offset, **context}
    return records


def parse_threads(values: list[str]) -> dict[str, str]:
    threads = {}
    for value in values:
        project, _, thread = value.partition("=")
        if not (project and thread):
            raise argparse.ArgumentTypeError(f"--thread needs PROJECT=THREAD, got {value!r}")
        threads[thread] = project
    return threads


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--thread", action="append", required=True, metavar="PROJECT=THREAD",
                        help="board project id and Codex thread id; repeat for more threads")
    parser.add_argument("--machine", help="label for the machine the agent runs on")
    parser.add_argument("--forward", help="command that receives the spooled lines on stdin")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between polls")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    args = parser.parse_args()
    try:
        threads = parse_threads(args.thread)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    home, directory = codex_home(), data_dir()
    last_failure = 0.0
    while True:
        state = load_state(directory)
        try:
            records = poll(home, directory, threads, args.machine, state, spool=bool(args.forward))
            save_state(directory, state)
            for entry in records:
                print(f"{entry['recorded_at']} {entry['project']} {entry['turn_id']}", flush=True)
            if args.forward and (records or time.monotonic() - last_failure >= RETRY_SECONDS):
                try:
                    flush(directory, args.forward)
                    last_failure = 0.0
                except Exception as error:
                    last_failure = time.monotonic()
                    print(f"forward failed, will retry: {error}", file=sys.stderr, flush=True)
        except Exception:
            traceback.print_exc()
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
