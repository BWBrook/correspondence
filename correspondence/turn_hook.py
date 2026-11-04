"""Record each agent turn's closing message in a turn ledger.

Claude Code and Codex both run this as a ``Stop`` hook, reading the hook's JSON
input on stdin::

    correspondence-turn-hook --agent Claude --project silurian-plants

Each turn appends one JSON line to ``turns.jsonl`` in the board's data
directory (``CORRESPONDENCE_DATA_DIR``): the time the hook ran, agent,
project, session, turn, working directory, model and closing message. The
hook never blocks or fails a turn: errors go to ``turn-hook-errors.log``
beside the ledger and the exit status is always 0.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("CORRESPONDENCE_DATA_DIR", "~/Library/Application Support/Correspondence")).expanduser()


def last_text_from_transcript(path: str | None) -> str | None:
    if not path or not Path(path).is_file():
        return None
    last = None
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            content = entry.get("message", {}).get("content")
            texts = [block.get("text", "") for block in content or [] if isinstance(block, dict) and block.get("type") == "text"]
            if any(t.strip() for t in texts):
                last = "\n\n".join(t for t in texts if t.strip())
    return last


def record(payload: dict, agent: str, project: str) -> dict:
    message = payload.get("last_assistant_message")
    if message is None:
        message = last_text_from_transcript(payload.get("transcript_path"))
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent": agent,
        "project": project,
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "cwd": payload.get("cwd"),
        "model": payload.get("model"),
        "event": payload.get("hook_event_name"),
        "stop_hook_active": payload.get("stop_hook_active"),
        "message": message,
    }


def append(entry: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "turns.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", required=True, choices=["Claude", "Codex"])
    parser.add_argument("--project", required=True, help="board project id")
    args = parser.parse_args()
    directory = data_dir()
    try:
        append(record(json.load(sys.stdin), args.agent, args.project), directory)
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
