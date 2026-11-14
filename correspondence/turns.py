"""Read the turn ledger that ``correspondence-turn-hook`` appends to.

The board shows each agent's closing messages as recorded. A turn is ``main``
when it comes from a named pair's pinned session for that agent, ``other`` when
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
from datetime import datetime
from pathlib import Path

FIELDS = ("recorded_at", "agent", "project", "session_id", "turn_id", "model", "message", "machine", "usage", "limits")


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
    turns.reverse()  # within one second, the line appended last is the newer turn
    turns.sort(key=lambda turn: turn["recorded_at"], reverse=True)
    return turns


def declared_next(message: str | None, user_name: str = "User") -> dict | None:
    """The ``Next:`` declaration on a closing message's last line, if any."""
    lines = [line.strip() for line in (message or "").splitlines() if line.strip()]
    if not lines:
        return None
    names = {"claude": "Claude", "codex": "Codex", "user": "User", user_name.lower(): "User"}
    labels = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    match = re.match(rf"^(?:>\s*)?[*_`]*next[*_`]*\s*:[*_`]*\s*[*_`]*({labels})[*_`]*(?=$|[\s–—:,.;(\-])\s*([-–—:,.;(]?)\s*(.*?)$",
                     lines[-1], re.IGNORECASE)
    if not match:
        return None
    reason = match.group(3).strip()
    if match.group(2) == "(" and reason.endswith(")"):
        reason = reason[:-1].rstrip()
    return {"agent": names[match.group(1).lower()], "reason": reason or None}


def annotate(turn: dict, pairs: list[dict], pins: dict, user_name: str = "User") -> dict:
    project_pairs = [p for p in pairs if p["project_id"] == turn["project"]]
    pair = next((p for p in project_pairs
                 if turn["session_id"] and pins.get(p["id"], {}).get(turn["agent"]) == turn["session_id"]), None)
    has_pin = any(turn["agent"] in pins.get(p["id"], {}) for p in project_pairs)
    role = "main" if pair else "other" if has_pin else None
    automation = (turn["message"] or "").lstrip().startswith("<heartbeat>")
    return {**turn, "role": role, "automation": automation, "next": declared_next(turn["message"], user_name),
            "pair_id": pair["id"] if pair else None, "pair_name": pair["name"] if pair else None,
            "discussion_ids": pair["discussion_ids"] if pair else []}


def turn_key(turn: dict | None) -> str | None:
    return f"{turn['session_id']}@{turn['recorded_at']}" if turn else None


def latest_main_turn(turns: list[dict], pair: dict, pins: dict, user_name: str = "User") -> dict | None:
    return next((t for t in (annotate(t, [pair], pins, user_name) for t in turns if t["project"] == pair["project_id"])
                 if t["pair_id"] == pair["id"] and not t["automation"]), None)


def link_discussions(turns: list[dict], history: list[dict], posts, pairs: list[dict]) -> list[dict]:
    counts = {}
    for pair in pairs:
        counts[pair["project_id"]] = counts.get(pair["project_id"], 0) + 1
    for turn in turns:
        previous = next((t for t in history if t["session_id"] == turn["session_id"] and t["project"] == turn["project"]
                         and t["recorded_at"][:19] < turn["recorded_at"][:19]), None)
        allowed = turn["discussion_ids"]
        # Agent/time windows cannot identify which simultaneous chat posted.
        # Multi-pair sections require explicit discussion links instead of guessing.
        scoped = bool(allowed) or counts.get(turn["project"], 0) <= 1
        found = posts(turn["project"], turn["agent"], previous["recorded_at"][:19], turn["recorded_at"][:19]) if previous and scoped else []
        threads = {}
        for post in found:
            if allowed and post["thread_id"] not in allowed:
                continue
            entry = threads.setdefault(post["thread_id"], {"thread_id": post["thread_id"], "title": post["title"], "message_ids": []})
            entry["message_ids"].append(post["id"])
        turn["discussions"] = list(threads.values())
    return turns


def is_current(turn: dict) -> bool:
    return turn["role"] != "other" and not turn["automation"]


def project_turns(turns: list[dict], project_id: str, limit: int, pairs: list[dict], pins: dict,
                  user_name: str = "User", pair_id: int | None = None) -> list[dict]:
    annotated = (annotate(t, pairs, pins, user_name) for t in turns if t["project"] == project_id)
    return [t for t in annotated if pair_id is None or t["pair_id"] == pair_id][:limit]


def latest_per_pair(turns: list[dict], pairs: list[dict], pins: dict, user_name: str = "User") -> list[dict]:
    latest = {}
    for turn in (annotate(t, pairs, pins, user_name) for t in turns):
        if is_current(turn):
            latest.setdefault((turn["project"], turn["pair_id"]), turn)
    return list(latest.values())


def project_sessions(directory: Path, project_id: str, pairs: list[dict], pins: dict) -> list[dict]:
    sessions = {}
    for raw in read_turns(directory):
        if raw["project"] != project_id or not raw["session_id"]:
            continue
        turn = annotate(raw, pairs, pins)
        key = (turn["agent"], turn["session_id"])
        if key not in sessions:
            sessions[key] = {"agent": turn["agent"], "session_id": turn["session_id"], "last_at": turn["recorded_at"],
                             "turns": 0, "automation": turn["automation"], "pair_id": turn["pair_id"],
                             "pair_name": turn["pair_name"], "excerpt": (turn["message"] or "")[:160], "machine": turn.get("machine")}
        sessions[key]["turns"] += 1
    return list(sessions.values())


def answered_at(turns: list[dict], dispatch: dict) -> str | None:
    replies = [t["recorded_at"] for t in turns if t["session_id"] == dispatch["session_id"]
               and t["project"] == dispatch["project_id"] and t["agent"] == dispatch["target"]
               and t.get("machine") == dispatch.get("machine")
               and t["recorded_at"][:19] > dispatch["requested_at"][:19]]
    return min(replies) if replies else None


def stamp(value: str) -> str:
    """A turn time as the board shows it, in the board host's time zone: ``7 Nov 2026 at 4:08 pm``."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    hour = moment.hour % 12 or 12
    return f"{moment.day} {moment:%b %Y} at {hour}:{moment:%M} {'am' if moment.hour < 12 else 'pm'}"


def continue_prompts(turn: dict, project_name: str, user_name: str) -> dict:
    """The Continue prompt quoting a turn: ``full`` for a session's first board prompt, then ``short``."""
    quoted = "\n".join(f"> {line}" for line in (turn.get("message") or "(no closing message recorded)").split("\n"))
    at = stamp(turn["recorded_at"])
    context = f"Pair: {turn['pair_name']}.\n" if turn.get("pair_name") else ""
    if turn.get("discussion_ids"):
        context += "Board discussions: " + ", ".join(f"#{id}" for id in turn["discussion_ids"]) + ".\n"
    context = context + "\n" if context else ""
    decision_note = (f"If you need a decision or approval, close with Next: {user_name} — decision needed: <question>. "
                     f"Plain Next: {user_name} means a handoff without a decision request.")
    return {
        "short": f"{turn['agent']} replied on {at}, closing with:\n\n{context}{quoted}\n\n{decision_note}",
        "full": (f"Continue — from {user_name}, sent through the board.\n\n"
                 f"{turn['agent']}'s latest turn in {project_name} ended at {at}. "
                 f"Its closing message, relayed word for word:\n\n{context}{quoted}\n\nTake your next turn. {decision_note}"),
    }


def latest_limits(turns: list[dict], agent: str) -> dict | None:
    """The newest limit reading recorded with an agent's turns (the ledger is newest first)."""
    return next((t["limits"] for t in turns if t["agent"] == agent and t.get("limits")), None)


def daily_usage(turns: list[dict], project_id: str | None = None, days: int = 7) -> list[dict]:
    """Tokens per local day, project and agent over the last ``days`` days that saw turns."""
    totals: dict = {}
    for turn in turns:
        if not turn.get("usage") or (project_id and turn["project"] != project_id):
            continue
        day = datetime.fromisoformat(turn["recorded_at"].replace("Z", "+00:00")).astimezone().date().isoformat()
        row = totals.setdefault((day, turn["project"], turn["agent"]), {
            "day": day, "project": turn["project"], "agent": turn["agent"], "turns": 0, "input": 0, "cached": 0, "output": 0})
        row["turns"] += 1
        for key in ("input", "cached", "output"):
            row[key] += int(turn["usage"].get(key) or 0)
    recent = sorted({key[0] for key in totals}, reverse=True)[:days]
    return sorted((row for key, row in totals.items() if key[0] in recent),
                  key=lambda r: (r["day"], r["project"], r["agent"]), reverse=True)
