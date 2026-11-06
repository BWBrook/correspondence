"""Read the turn ledger that ``correspondence-turn-hook`` appends to.

The board shows each agent's closing messages as recorded. A turn is ``main``
when it comes from the project's pinned session for that agent, ``other`` when
the agent has a pinned session and this is a different one (an advisor or an
automation), and unpinned otherwise. Automation heartbeats are flagged.

An agent declares who acts next by ending its closing message with a line
``Next: Claude``, ``Next: Codex`` or ``Next: User`` (the user's name also
works), optionally followed by a reason. Each turn also lists the discussions
in its project that the same agent posted to since that session's previous
turn.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

NEXT_LINE = re.compile(r"^next\s*:\s*(\w+)\s*(?:[-–—:,.;(]\s*)?(.*?)\)?$", re.IGNORECASE)

FIELDS = ("recorded_at", "agent", "project", "session_id", "turn_id", "model", "message", "machine")


def read_turns(directory: Path) -> list[dict]:
    path = Path(directory) / "turns.jsonl"
    if not path.is_file():
        return []
    turns, seen = [], set()
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line in seen:
                continue  # a forwarded spool resent after a partial failure
            seen.add(line)
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("recorded_at") and entry.get("project"):
                turns.append({field: entry.get(field) for field in FIELDS})
    turns.sort(key=lambda turn: turn["recorded_at"], reverse=True)
    return turns


def declared_next(message: str | None, user_name: str = "User") -> dict | None:
    """The ``Next:`` declaration on a closing message's last line, if any."""
    lines = [line.strip() for line in (message or "").splitlines() if line.strip()]
    if not lines:
        return None
    match = NEXT_LINE.match(re.sub(r"[*_`>]", "", lines[-1]).strip())
    if not match:
        return None
    names = {"claude": "Claude", "codex": "Codex", "user": "User", user_name.lower(): "User"}
    agent = names.get(match.group(1).lower())
    return {"agent": agent, "reason": match.group(2).strip() or None} if agent else None


def annotate(turn: dict, pins: dict, user_name: str = "User") -> dict:
    pinned = pins.get(turn["project"], {}).get(turn["agent"])
    role = None if pinned is None else "main" if pinned == turn["session_id"] else "other"
    automation = (turn["message"] or "").lstrip().startswith("<heartbeat>")
    return {**turn, "role": role, "automation": automation, "next": declared_next(turn["message"], user_name)}


def link_discussions(turns: list[dict], history: list[dict], posts) -> list[dict]:
    """Add the discussions each turn's agent posted to since its session's previous turn.

    ``history`` is the whole ledger, newest first; ``posts(project, agent,
    after, through)`` returns that agent's posts in the window, compared to
    the second. A session's first recorded turn has no window.
    """
    for turn in turns:
        previous = next((t for t in history if t["session_id"] == turn["session_id"] and t["project"] == turn["project"]
                         and t["recorded_at"][:19] < turn["recorded_at"][:19]), None)
        found = posts(turn["project"], turn["agent"], previous["recorded_at"][:19], turn["recorded_at"][:19]) if previous else []
        threads = {}
        for post in found:
            entry = threads.setdefault(post["thread_id"], {"thread_id": post["thread_id"], "title": post["title"], "message_ids": []})
            entry["message_ids"].append(post["id"])
        turn["discussions"] = list(threads.values())
    return turns


def is_current(turn: dict) -> bool:
    return turn["role"] != "other" and not turn["automation"]


def project_turns(turns: list[dict], project_id: str, limit: int, pins: dict, user_name: str = "User") -> list[dict]:
    return [annotate(t, pins, user_name) for t in turns if t["project"] == project_id][:limit]


def latest_per_project(turns: list[dict], pins: dict, user_name: str = "User") -> list[dict]:
    latest = {}
    for turn in (annotate(t, pins, user_name) for t in turns):
        if is_current(turn):
            latest.setdefault(turn["project"], turn)
    return list(latest.values())


def project_sessions(directory: Path, project_id: str) -> list[dict]:
    """Every session seen in a project's turns, newest first, for choosing pins."""
    sessions = {}
    for turn in read_turns(directory):
        if turn["project"] != project_id or not turn["session_id"]:
            continue
        key = (turn["agent"], turn["session_id"])
        if key not in sessions:
            sessions[key] = {"agent": turn["agent"], "session_id": turn["session_id"], "last_at": turn["recorded_at"],
                             "turns": 0, "automation": (turn["message"] or "").lstrip().startswith("<heartbeat>"),
                             "excerpt": (turn["message"] or "")[:160], "machine": turn.get("machine")}
        sessions[key]["turns"] += 1
    return list(sessions.values())


def answered_at(turns: list[dict], dispatch: dict) -> str | None:
    """Time of the first turn from the dispatched session after the request."""
    replies = [t["recorded_at"] for t in turns if t["session_id"] == dispatch["session_id"]
               and t["recorded_at"] > dispatch["requested_at"][:19]]
    return min(replies) if replies else None
