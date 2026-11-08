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
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from correspondence.dispatch import RECONNECT_SECONDS, RemoteBoard, claude_session, deliver_codex
from correspondence.turn_hook import append, closing_from_transcript, data_dir, flush
from correspondence.usage import codex_turn, feed_codex

STATE = "courier.json"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def rollout_path(home: Path, thread: str) -> Path | None:
    """The thread's rollout file; Codex compresses older ones to ``.jsonl.zst``."""
    found = sorted((home / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl"))
    found = found or sorted((home / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl.zst"))
    return found[-1] if found else None


def read_rollout(path: Path, offset: int) -> bytes:
    if path.suffix != ".zst":
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read()
    try:
        from compression import zstd
        data = zstd.decompress(path.read_bytes())
    except ImportError:
        data = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout
    return data[offset:]


def iso_seconds(timestamp: str) -> str:
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def scan(path: Path, offset: int, context: dict) -> tuple[list[dict], int]:
    """Return the turns completed after ``offset`` and the new offset.

    Only whole lines are consumed. ``context`` carries the model and working
    directory of the latest ``turn_context`` and the running token count across
    calls. Resuming without the token count (state saved by an older courier)
    recovers it from the earlier part of the file first.
    """
    if offset and context.get("total") is None:
        for raw in read_rollout(path, 0)[:offset].splitlines():
            try:
                feed_codex(context, json.loads(raw))
            except (json.JSONDecodeError, AttributeError):
                continue
    chunk = read_rollout(path, offset)
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
        feed_codex(context, entry)
        if entry.get("type") == "event_msg" and payload.get("type") == "task_complete":
            completed.append({"timestamp": entry.get("timestamp"), "turn_id": payload.get("turn_id"),
                              "message": payload.get("last_agent_message"), "model": context.get("model"),
                              "cwd": context.get("cwd"), "usage": codex_turn(context), "limits": context.get("limits")})
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
        **({"usage": turn["usage"]} if turn.get("usage") else {}),
        **({"limits": turn["limits"]} if turn.get("limits") else {}),
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

    ``state`` maps thread to read position, file size and time, and the last
    turn's time. A file that is unchanged is not read again. When a thread's
    file changes name, as when Codex compresses or restores it, the new file is
    read in full and only turns completed after the last one seen are sent. Threads no
    longer pinned are dropped, so pinning one again starts at the end of its
    file.
    """
    for thread in set(state) - set(threads):
        del state[thread]
    records = []
    for thread, project in threads.items():
        known = state.get(thread)
        path = Path(known["path"]) if known and Path(known["path"]).exists() else rollout_path(home, thread)
        if path is None:
            continue
        stat = path.stat()
        same_file = bool(known) and known.get("path") == str(path)
        if same_file and known.get("mtime") == stat.st_mtime and known.get("size") == stat.st_size:
            continue
        if same_file and path.suffix != ".zst" and known.get("offset", 0) <= stat.st_size:
            context = {key: known.get(key) for key in ("model", "cwd", "total", "baseline", "limits")}
            new, offset = scan(path, known["offset"], context)
            last = new[-1]["timestamp"] if new else known.get("last_at")
        else:
            context = {}
            turns, offset = scan(path, 0, context)
            if not known or "last_at" not in known:
                new = []
            else:
                new = [turn for turn in turns if (turn["timestamp"] or "") > (known["last_at"] or "")]
            last = turns[-1]["timestamp"] if turns else (known or {}).get("last_at")
        for turn in new:
            entry = turn_record(turn, thread, project, machine)
            append(entry, directory, "turns-spool.jsonl")
            records.append(entry)
        state[thread] = {"path": str(path), "offset": offset, "size": stat.st_size, "mtime": stat.st_mtime,
                         "project": project, "last_at": last, **context}
    return records


def announce(home: Path, directory: Path, session: str, project: str, machine: str) -> dict | None:
    """Spool a Codex thread's or Claude session's latest turn once, so the board lists it for pinning."""
    path = rollout_path(home, session)
    if path is not None:
        turns, _ = scan(path, 0, {})
        entry = turn_record(turns[-1], session, project, machine) if turns else None
    else:
        transcripts = sorted((Path.home() / ".claude" / "projects").glob(f"*/{session}.jsonl"))
        if not transcripts:
            return None
        message, model = closing_from_transcript(str(transcripts[-1]))
        if message is None:
            return None
        recorded = datetime.fromtimestamp(transcripts[-1].stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        entry = {"recorded_at": recorded, "agent": "Claude", "project": project, "session_id": session,
                 "turn_id": None, "cwd": None, "model": model, "event": "announce", "message": message,
                 "machine": machine}
    if entry:
        append(entry, directory, "turns-spool.jsonl")
    return entry


def exchange(board, log, unreported: dict) -> dict[str, str]:
    """Report pinned Claude sessions and deliver waiting Codex prompts; return the pinned Codex threads.

    ``unreported`` holds outcomes the board has not yet acknowledged, keyed by
    dispatch, so a prompt delivered just as the connection drops is reported
    on the next round instead of staying claimed with no outcome.
    """
    for dispatch_id, (delivered, detail) in list(unreported.items()):
        board.finish(dispatch_id, delivered, detail)
        del unreported[dispatch_id]
    pins = board.pins()
    board.report_sessions({p["session_id"]: claude_session(p["session_id"]) for p in pins if p["agent"] == "Claude"})
    for dispatch in board.waiting("Codex"):
        if board.claim(dispatch["id"]):
            unreported[dispatch["id"]] = deliver_codex(dispatch["session_id"], dispatch["body"])
            delivered, detail = unreported[dispatch["id"]]
            log(f"dispatch {dispatch['id']} to Codex {dispatch['session_id']}: "
                f"{'delivered' if delivered else 'failed'} {detail}".strip())
            board.finish(dispatch["id"], delivered, detail)
            del unreported[dispatch["id"]]
    return {p["session_id"]: p["project_id"] for p in pins if p["agent"] == "Codex"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--machine", required=True, help="this machine's name on the board")
    parser.add_argument("--board", required=True, help="command that runs correspondence-dispatch serve on the board's host")
    parser.add_argument("--forward", required=True, help="command that receives spooled turn lines on stdin")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between rounds")
    parser.add_argument("--once", action="store_true", help="run one round and exit")
    parser.add_argument("--announce", action="append", default=[], metavar="PROJECT=THREAD",
                        help="send this Codex thread's or Claude session's latest turn once, so it can be pinned; repeatable")
    args = parser.parse_args()
    home, directory, board = codex_home(), data_dir(), RemoteBoard(args.board)

    def log(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    for value in args.announce:
        project, _, thread = value.partition("=")
        if not (project and thread):
            parser.error(f"--announce needs PROJECT=THREAD, got {value!r}")
        entry = announce(home, directory, thread, project, args.machine)
        log(f"announced {thread} in {project}: {entry['recorded_at']}" if entry else f"no completed turn found for {thread}")

    state = load_state(directory)
    threads = {thread: entry["project"] for thread, entry in state.items() if entry.get("project")}
    board_retry = forward_retry = 0.0
    unreported: dict = {}
    while True:
        if time.monotonic() >= board_retry:
            try:
                threads = exchange(board, log, unreported)
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
