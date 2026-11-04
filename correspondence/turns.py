"""Read the turn ledger that ``correspondence-turn-hook`` appends to.

The ledger is display-only: the board shows each agent's closing messages as
recorded and never acts on them.
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


def project_turns(directory: Path, project_id: str, limit: int) -> list[dict]:
    return [turn for turn in read_turns(directory) if turn["project"] == project_id][:limit]


def latest_per_project(directory: Path) -> list[dict]:
    latest = {}
    for turn in read_turns(directory):
        latest.setdefault(turn["project"], turn)
    return list(latest.values())
