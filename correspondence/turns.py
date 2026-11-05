"""Read the turn ledger that ``correspondence-turn-hook`` appends to.

The board shows each agent's closing messages as recorded. A turn is ``main``
when it comes from the project's pinned session for that agent, ``other`` when
the agent has a pinned session and this is a different one (an advisor or an
automation), and unpinned otherwise. Automation heartbeats are flagged.
"""
from __future__ import annotations

import json
from pathlib import Path

FIELDS = ("recorded_at", "agent", "project", "session_id", "turn_id", "model", "message")


def read_turns(directory: Path) -> list[dict]:
    path = Path(directory) / "turns.jsonl"
    if not path.is_file():
        return []
    turns = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("recorded_at") and entry.get("project"):
                turns.append({field: entry.get(field) for field in FIELDS})
    turns.sort(key=lambda turn: turn["recorded_at"], reverse=True)
    return turns


def annotate(turn: dict, pins: dict) -> dict:
    pinned = pins.get(turn["project"], {}).get(turn["agent"])
    role = None if pinned is None else "main" if pinned == turn["session_id"] else "other"
    automation = (turn["message"] or "").lstrip().startswith("<heartbeat>")
    return {**turn, "role": role, "automation": automation}


def is_current(turn: dict) -> bool:
    return turn["role"] != "other" and not turn["automation"]


def project_turns(directory: Path, project_id: str, limit: int, pins: dict) -> list[dict]:
    return [annotate(t, pins) for t in read_turns(directory) if t["project"] == project_id][:limit]


def latest_per_project(directory: Path, pins: dict) -> list[dict]:
    latest = {}
    for turn in map(lambda t: annotate(t, pins), read_turns(directory)):
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
                             "excerpt": (turn["message"] or "")[:160]}
        sessions[key]["turns"] += 1
    return list(sessions.values())


def answered_at(turns: list[dict], dispatch: dict) -> str | None:
    """Time of the first turn from the dispatched session after the request."""
    replies = [t["recorded_at"] for t in turns if t["session_id"] == dispatch["session_id"]
               and t["recorded_at"] > dispatch["requested_at"][:19]]
    return min(replies) if replies else None
