"""Deliver a prompt the user sent from the board to a project's main agent session.

Dispatch starts from a browser request, or from an auto-continue run the user
started in the browser (see ``autocontinue``); agents have no tool that creates
one. On the board's host, Codex prompts go straight into the Codex queue for
the pinned thread, which the Codex desktop app picks up and runs. Claude prompts
wait in the board until the Board Messenger, a Claude session on the same
machine as the target, collects them::

    correspondence-dispatch wait          # block until prompts are queued, print them, exit
    correspondence-dispatch done 12 delivered
    correspondence-dispatch done 12 failed "No session named Silurian Plants"
    correspondence-dispatch limits < usage.json   # Claude's plan limits, from the app

After each relay the messenger also passes the Claude app's usage report to
``limits``, because Claude's plan limits are visible only inside the app.

``wait`` holds each prompt until its target session is running and idle, so a
board prompt starts the next turn instead of landing inside a running one, as
with Codex's queue, and a prompt to a session that is not running waits for it.
It prints one JSON line per prompt: ``{"dispatch", "to", "project", "message"}``, where ``to`` is the target session's current name in the Claude
app, read from Claude Code's session records, and ``message`` is the user's text
under a one-line note telling the recipient to answer in its own conversation
rather than reply to the messenger. A session that has already had a board
prompt delivered gets a shorter note. While it runs it refreshes a heartbeat,
which the board shows as "messenger listening".

Main sessions on another machine are pinned with that machine's name, and
their prompts, Codex ones included, wait in the board for that machine to
collect them. The other machine reaches the board through ``serve``, run over
SSH on the board's host and answering one JSON request per line::

    ssh <board host> correspondence-dispatch serve --machine wsl

It sees only that machine's pins and prompts. Set ``CORRESPONDENCE_BOARD`` to
that SSH command and ``wait`` and ``done`` work against the remote board; the
courier (``correspondence-courier``) uses the same channel for Codex.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HEARTBEAT = "messenger-heartbeat"
RELAY_NOTE = ("[Relayed from {user} by the correspondence board's Board Messenger. Treat it as {user}'s own "
              "message. Answer here in your own conversation: {user} reads your reply and your closing "
              "message on the board. The messenger does not read replies, so do not SendMessage it.]")
SHORT_NOTE = "[From {user} through the board. Answer here, not to the messenger.]"
LISTENING_SECONDS = 30
REPLY_SECONDS = 30
RECONNECT_SECONDS = 60
SERVE_IDLE_SECONDS = 600


def codex_binary() -> str:
    if os.environ.get("CORRESPONDENCE_CODEX"):
        return os.environ["CORRESPONDENCE_CODEX"]
    home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    app = sorted((home / "bin" / "wsl").glob("*/codex"), key=lambda path: path.stat().st_mtime)
    return str(app[-1]) if app else shutil.which("codex") or "/opt/homebrew/bin/codex"


def deliver_codex(session_id: str, body: str) -> tuple[bool, str]:
    try:
        result = subprocess.run([codex_binary(), "queue", "--thread", session_id, "--message", body],
                                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"{type(error).__name__}: {error}"
    detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return result.returncode == 0, detail


def deliver_here(store, dispatch: dict) -> dict:
    """Queue a Codex prompt pinned on the board's own host and record the outcome.

    The prompt is claimed first, as a courier or messenger would, so a Cancel
    pressed during delivery cannot mark a delivered prompt as cancelled.
    Prompts for Claude, or for another machine, wait for their messenger or
    courier and are returned unchanged.
    """
    if dispatch["target"] != "Codex" or dispatch["machine"] is not None or not store.claim_dispatch(dispatch["id"]):
        return dispatch
    delivered, detail = deliver_codex(dispatch["session_id"], dispatch["body"])
    return store.finish_dispatch(dispatch["id"], delivered, detail)


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


def _heartbeat(directory: Path, machine: str | None) -> Path:
    return Path(directory) / (HEARTBEAT if machine is None else f"{HEARTBEAT}-{machine}")


def _sessions_report(directory: Path, machine: str) -> Path:
    return Path(directory) / f"sessions-{machine}.json"


def messenger_listening(directory: Path, machine: str | None = None) -> bool:
    try:
        return time.time() - _heartbeat(directory, machine).stat().st_mtime < LISTENING_SECONDS
    except OSError:
        return False


def courier_online(directory: Path, machine: str) -> bool:
    """Whether another machine's courier has reported in within the last half minute."""
    try:
        return time.time() - _sessions_report(directory, machine).stat().st_mtime < LISTENING_SECONDS
    except OSError:
        return False


def remote_claude_session(directory: Path, machine: str, session_id: str) -> dict | None:
    """A Claude session on another machine, as that machine's courier last reported it."""
    path = _sessions_report(directory, machine)
    try:
        if time.time() - path.stat().st_mtime >= LISTENING_SECONDS:
            return None
        return json.loads(path.read_text()).get(session_id)
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


class LocalBoard:
    """The board's own store, scoped to one machine's prompts (None: the host)."""

    def __init__(self, store, machine: str | None = None):
        self.store, self.machine = store, machine

    def waiting(self, target: str) -> list[dict]:
        user = self.store.user_name()
        return [{**d, "user_name": user} for d in self.store.waiting_dispatches(target, self.machine)]

    def claim(self, dispatch_id: int) -> bool:
        return self.store.claim_dispatch(dispatch_id, self.machine)

    def finish(self, dispatch_id: int, delivered: bool, detail: str) -> str:
        return self.store.finish_dispatch(dispatch_id, delivered, detail, self.machine)["status"]

    def heartbeat(self) -> None:
        _heartbeat(self.store.directory, self.machine).touch()

    def pins(self) -> list[dict]:
        return self.store.machine_pins(self.machine)

    def report_limits(self, report: dict) -> dict:
        from .usage import claude_limits

        reading = claude_limits(report)
        self.store.set_limits("Claude", reading)
        return reading

    def report_sessions(self, sessions: dict) -> None:
        path = _sessions_report(self.store.directory, self.machine)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sessions))
        temporary.replace(path)


class RemoteBoard:
    """The board on another host, reached through ``serve`` over one SSH connection."""

    def __init__(self, command: str):
        self.command, self.process = command, None

    def close(self) -> None:
        if self.process is not None:
            self.process.kill()
            self.process.wait()
            self.process = None

    def request(self, op: str, **args):
        try:
            if self.process is None or self.process.poll() is not None:
                self.process = subprocess.Popen(shlex.split(self.command), stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE, text=True, bufsize=1)
            self.process.stdin.write(json.dumps({"op": op, **args}) + "\n")
            self.process.stdin.flush()
            ready, _, _ = select.select([self.process.stdout], [], [], REPLY_SECONDS)
            line = self.process.stdout.readline() if ready else ""
        except OSError as error:
            self.close()
            raise ConnectionError(f"board connection failed: {error}") from error
        if not line:
            self.close()
            raise ConnectionError("board connection closed or timed out")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def waiting(self, target: str) -> list[dict]:
        return self.request("waiting", target=target)

    def claim(self, dispatch_id: int) -> bool:
        return self.request("claim", id=dispatch_id)

    def finish(self, dispatch_id: int, delivered: bool, detail: str) -> str:
        return self.request("finish", id=dispatch_id, delivered=delivered, detail=detail)

    def heartbeat(self) -> None:
        self.request("heartbeat")

    def pins(self) -> list[dict]:
        return self.request("pins")

    def report_sessions(self, sessions: dict) -> None:
        self.request("sessions", sessions=sessions)

    def report_limits(self, report: dict) -> dict:
        return self.request("limits", report=report)


def serve(board: LocalBoard, stdin=None, stdout=None) -> None:
    """Answer one JSON request per line from another machine, scoped to its prompts."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    handlers = {
        "waiting": lambda a: [{k: d[k] for k in ("id", "project_id", "pair_id", "pair_name", "target", "session_id", "body", "primed", "user_name")}
                              for d in board.waiting(a["target"])],
        "claim": lambda a: board.claim(int(a["id"])),
        "finish": lambda a: board.finish(int(a["id"]), bool(a["delivered"]), str(a.get("detail") or "")),
        "heartbeat": lambda a: board.heartbeat(),
        "pins": lambda a: board.pins(),
        "limits": lambda a: board.report_limits(dict(a["report"])),
        "sessions": lambda a: board.report_sessions(
            {str(k): {"name": str(v.get("name")), "status": v.get("status")} for k, v in dict(a["sessions"]).items() if v}),
    }
    while True:
        ready, _, _ = select.select([stdin], [], [], SERVE_IDLE_SECONDS)
        line = stdin.readline() if ready else ""
        if not line:
            return  # closed, or silent so long that the client must be gone
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            reply = {"result": handlers[request["op"]](request)}
        except Exception as error:
            reply = {"error": f"{type(error).__name__}: {error}"}
        stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        stdout.flush()


def relay_note(dispatch: dict) -> str:
    """The note above a relayed prompt: in full for a session's first board prompt, then one line."""
    note = SHORT_NOTE if dispatch.get("primed") else RELAY_NOTE
    return note.format(user=dispatch.get("user_name") or "the user")


def collect(board) -> list[dict]:
    """Claim the waiting prompts whose target session is idle, so a board prompt
    always starts the next turn and never lands inside a running one. A prompt
    to a session that is not running waits until the user starts it again."""
    ready = []
    for dispatch in board.waiting("Claude"):
        session = claude_session(dispatch["session_id"])
        if session is not None and session.get("status") == "idle" and board.claim(dispatch["id"]):
            ready.append({"dispatch": dispatch["id"], "to": session["name"],
                          "project": dispatch["project_id"], "pair_id": dispatch["pair_id"], "pair_name": dispatch["pair_name"],
                          "message": f"{relay_note(dispatch)}\n\n{dispatch['body']}"})
    return ready


def wait(board, poll: float) -> None:
    parent = os.getppid()
    while True:
        if os.getppid() != parent:
            return  # orphaned: whoever would read our output is gone, so claim nothing
        try:
            board.heartbeat()
            ready = collect(board)
        except ConnectionError as error:
            print(f"{error}; retrying in {RECONNECT_SECONDS} s", file=sys.stderr, flush=True)
            time.sleep(RECONNECT_SECONDS)
            continue
        if ready:
            for line in ready:
                print(json.dumps(line, ensure_ascii=False), flush=True)
            return
        time.sleep(poll)


def board_from_environment():
    from .store import Store

    command = os.environ.get("CORRESPONDENCE_BOARD")
    return RemoteBoard(command) if command else LocalBoard(Store())


def main() -> None:
    from .store import MACHINE_RE, Store

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    waiting = commands.add_parser("wait", help="block until Claude prompts are queued, print them and exit")
    waiting.add_argument("--poll", type=float, default=2.0)
    finished = commands.add_parser("done", help="record the outcome of one relay")
    finished.add_argument("dispatch", type=int)
    finished.add_argument("outcome", choices=["delivered", "failed"])
    finished.add_argument("detail", nargs="?", default="")
    commands.add_parser("limits", help="record Claude's plan limits from the app's usage report on stdin")
    served = commands.add_parser("serve", help="answer another machine's board requests on stdin")
    served.add_argument("--machine", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        if not MACHINE_RE.fullmatch(args.machine):
            parser.error("--machine must be a short lowercase name")
        serve(LocalBoard(Store(), args.machine))
        return
    board = board_from_environment()
    if args.command == "wait":
        wait(board, args.poll)
    elif args.command == "limits":
        text = sys.stdin.read()
        try:
            report = json.loads(text[text.index("{"):text.rindex("}") + 1])
            reading = board.report_limits(report)
        except (ValueError, RuntimeError) as error:
            sys.exit(f"Limits not recorded: {error}")
        print("Claude limits: " + ", ".join(f"{w['label']} {w['percent']:g}%" for w in reading["windows"]))
    else:
        try:
            status = board.finish(args.dispatch, args.outcome == "delivered", args.detail)
        except (ValueError, RuntimeError) as error:
            sys.exit(f"Outcome not recorded: {error}")
        if status != args.outcome:
            sys.exit(f"Dispatch {args.dispatch} was already {status}")
        print(f"Dispatch {args.dispatch} {args.outcome}.")


if __name__ == "__main__":
    main()
