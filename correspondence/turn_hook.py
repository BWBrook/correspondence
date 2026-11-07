"""Record each agent turn's closing message in a turn ledger.

Claude Code and Codex both run this as a ``Stop`` hook, reading the hook's JSON
input on stdin::

    correspondence-turn-hook --agent Claude --project silurian-plants

Each turn appends one JSON line to ``turns.jsonl`` in the board's data
directory (``CORRESPONDENCE_DATA_DIR``): the time the hook ran, agent,
project, session, turn, working directory, model and closing message. The
hook never blocks or fails a turn: errors go to ``turn-hook-errors.log``
beside the ledger and the exit status is always 0.

On a machine other than the board's, ``--forward`` sends the line on instead::

    correspondence-turn-hook --agent Codex --project p --machine wsl \
        --forward "ssh mini /path/to/correspondence-turn-hook --append"

The line goes to ``turns-spool.jsonl`` in the local data directory first; the
whole spool is then piped to the forward command, and cleared only when that
command succeeds, so turns recorded while the board's host is unreachable go
with the next turn. ``--append`` is the receiving end: it appends the JSON
lines on stdin to the ledger and fails loudly, so the sender keeps its spool.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

AGENTS = {"Claude", "Codex"}


def data_dir() -> Path:
    default = "~/Library/Application Support/Correspondence" if sys.platform == "darwin" else "~/.local/share/correspondence"
    return Path(os.environ.get("CORRESPONDENCE_DATA_DIR", default)).expanduser()


def closing_from_transcript(path: str | None) -> tuple[str | None, str | None]:
    """Return the last assistant text in a Claude transcript and its model."""
    if not path or not Path(path).is_file():
        return None, None
    text = model = None
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            texts = [block.get("text", "") for block in message.get("content") or [] if isinstance(block, dict) and block.get("type") == "text"]
            if any(t.strip() for t in texts):
                text = "\n\n".join(t for t in texts if t.strip())
                model = message.get("model") or model
    return text, model


def record(payload: dict, agent: str, project: str, machine: str | None = None) -> dict:
    message, model = payload.get("last_assistant_message"), payload.get("model")
    if message is None or model is None:
        transcript_message, transcript_model = closing_from_transcript(payload.get("transcript_path"))
        message = transcript_message if message is None else message
        model = model or transcript_model
    usage = limits = None
    try:
        usage, limits = turn_usage(payload, agent)
    except Exception:
        pass  # usage is extra: never lose the turn over it
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent": agent,
        "project": project,
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "cwd": payload.get("cwd"),
        "model": model,
        "event": payload.get("hook_event_name"),
        "stop_hook_active": payload.get("stop_hook_active"),
        "message": message,
        **({"usage": usage} if usage else {}),
        **({"limits": limits} if limits else {}),
        **({"machine": machine} if machine else {}),
    }


def turn_usage(payload: dict, agent: str) -> tuple[dict | None, dict | None]:
    """This turn's token use and, for Codex, the latest plan-limit reading."""
    from .usage import claude_turn_usage, codex_turn_usage

    if agent == "Claude":
        return claude_turn_usage(payload.get("transcript_path")), None
    path = payload.get("transcript_path")
    if not path and payload.get("session_id"):
        from .courier import codex_home, rollout_path

        path = rollout_path(codex_home(), payload["session_id"])
    return codex_turn_usage(path, payload.get("turn_id"))


def append(entry: dict, directory: Path, name: str = "turns.jsonl") -> None:
    append_lines([json.dumps(entry, ensure_ascii=False)], directory, name)


def append_lines(lines: list[str], directory: Path, name: str = "turns.jsonl") -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / name
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write("".join(line + "\n" for line in lines))
    path.chmod(0o600)


def forward(entry: dict, directory: Path, command: str) -> None:
    """Spool the turn, then send the whole spool; keep it if sending fails."""
    append(entry, directory, "turns-spool.jsonl")
    flush(directory, command)


def flush(directory: Path, command: str) -> None:
    """Send the whole spool to the forward command; clear it only on success."""
    spool = directory / "turns-spool.jsonl"
    if not spool.exists():
        return
    with open(spool, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        pending = handle.read()
        if not pending.strip():
            return
        result = subprocess.run(shlex.split(command), input=pending, text=True, capture_output=True,
                                timeout=20)
        if result.returncode != 0:
            raise RuntimeError(f"forward failed ({result.returncode}): {result.stderr.strip()[-500:]}")
        handle.seek(0)
        handle.truncate()


def receive(stream, directory: Path) -> int:
    lines = []
    for raw in stream:
        if not raw.strip():
            continue
        entry = json.loads(raw)
        if not (isinstance(entry, dict) and entry.get("recorded_at") and entry.get("agent") in AGENTS and entry.get("project")):
            raise ValueError(f"not a turn record: {raw[:200]!r}")
        lines.append(json.dumps(entry, ensure_ascii=False))
    if lines:
        append_lines(lines, directory)
    return len(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--append", action="store_true", help="receive forwarded turn lines on stdin")
    parser.add_argument("--agent", choices=sorted(AGENTS))
    parser.add_argument("--project", help="board project id")
    parser.add_argument("--machine", help="label for the machine the agent runs on")
    parser.add_argument("--forward", help="command that receives the spooled lines on stdin")
    args = parser.parse_args()
    directory = data_dir()
    if args.append:
        receive(sys.stdin, directory)
        return
    if not (args.agent and args.project):
        parser.error("--agent and --project are required")
    try:
        entry = record(json.load(sys.stdin), args.agent, args.project, args.machine)
        if args.forward:
            forward(entry, directory, args.forward)
        else:
            append(entry, directory)
    except Exception:
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(directory / "turn-hook-errors.log", "a", encoding="utf-8") as log:
                log.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {args.agent} {args.project}\n{traceback.format_exc()}\n")
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
