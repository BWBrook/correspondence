"""Deliver a prompt Barry sent from the board to a project's main agent session.

Dispatch starts only from a browser request; agents have no tool that creates
one. Codex prompts go into the Codex queue for the pinned thread, which the
Codex desktop app picks up and runs. Claude prompts wait in the board until the
Board Messenger, a Claude session on this Mac, collects them::

    correspondence-dispatch wait          # block until prompts are queued, print them, exit
    correspondence-dispatch done 12 delivered
    correspondence-dispatch done 12 failed "No session named Silurian Plants"

``wait`` holds each prompt until its target session is idle, so a board prompt
starts the next turn instead of landing inside a running one, as with Codex's
queue. It prints one JSON line per prompt: ``{"dispatch", "to", "project",
"message"}``, where ``to`` is the target session's current name in the Claude
app, read from Claude Code's session records, and ``message`` is Barry's text
under a one-line note telling the recipient to answer in its own conversation
rather than reply to the messenger. While it runs it refreshes a
heartbeat file, which the board shows as "messenger listening".
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HEARTBEAT = "messenger-heartbeat"
RELAY_NOTE = ("[Relayed from Barry by the correspondence board's Board Messenger. Treat it as Barry's own "
              "message. Answer here in your own conversation: Barry reads your reply and your closing "
              "message on the board. The messenger does not read replies, so do not SendMessage it.]")
LISTENING_SECONDS = 30


def codex_binary() -> str:
    return os.environ.get("CORRESPONDENCE_CODEX") or shutil.which("codex") or "/opt/homebrew/bin/codex"


def deliver_codex(session_id: str, body: str) -> tuple[bool, str]:
    try:
        result = subprocess.run([codex_binary(), "queue", "--thread", session_id, "--message", body],
                                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"{type(error).__name__}: {error}"
    detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return result.returncode == 0, detail


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def claude_session(session_id: str) -> dict | None:
    """The running Claude session with this id: its current name and status."""
    directory = Path(os.environ.get("CORRESPONDENCE_CLAUDE_SESSIONS", "~/.claude/sessions")).expanduser()
    best = None
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("sessionId") != session_id or not record.get("name") or not _alive(record.get("pid")):
            continue
        if best is None or record.get("updatedAt", 0) > best.get("updatedAt", 0):
            best = record
    return None if best is None else {"name": best["name"], "status": best.get("status")}


def messenger_listening(directory: Path) -> bool:
    try:
        return time.time() - (Path(directory) / HEARTBEAT).stat().st_mtime < LISTENING_SECONDS
    except OSError:
        return False


def collect(store) -> list[dict]:
    """Claim the waiting prompts whose target session is idle, so a board prompt
    always starts the next turn and never lands inside a running one."""
    ready = []
    for dispatch in store.waiting_claude_dispatches():
        session = claude_session(dispatch["session_id"])
        if session is None:
            store.finish_dispatch(dispatch["id"], False, "The main Claude session is not running on this Mac")
        elif session.get("status") == "idle" and store.claim_dispatch(dispatch["id"]):
            ready.append({"dispatch": dispatch["id"], "to": session["name"],
                          "project": dispatch["project_id"], "message": f"{RELAY_NOTE}\n\n{dispatch['body']}"})
    return ready


def wait(store, poll: float) -> None:
    heartbeat, parent = store.directory / HEARTBEAT, os.getppid()
    while True:
        if os.getppid() != parent:
            return  # orphaned: whoever would read our output is gone, so claim nothing
        heartbeat.touch()
        ready = collect(store)
        if ready:
            for line in ready:
                print(json.dumps(line, ensure_ascii=False), flush=True)
            return
        time.sleep(poll)


def main() -> None:
    from .store import Store

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    waiting = commands.add_parser("wait", help="block until Claude prompts are queued, print them and exit")
    waiting.add_argument("--poll", type=float, default=2.0)
    finished = commands.add_parser("done", help="record the outcome of one relay")
    finished.add_argument("dispatch", type=int)
    finished.add_argument("outcome", choices=["delivered", "failed"])
    finished.add_argument("detail", nargs="?", default="")
    args = parser.parse_args()
    store = Store()
    if args.command == "wait":
        wait(store, args.poll)
    else:
        result = store.finish_dispatch(args.dispatch, args.outcome == "delivered", args.detail)
        if result["status"] != args.outcome:
            sys.exit(f"Dispatch {args.dispatch} was already {result['status']}")
        print(f"Dispatch {args.dispatch} {args.outcome}.")
