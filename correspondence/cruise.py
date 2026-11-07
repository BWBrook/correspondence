"""Cruise: the board carries a project's hand-offs itself, within limits the user sets.

The user starts a cruise from the browser with a number of hand-offs and a
plan-limit ceiling. While it lasts, whenever the latest turn from one of the
project's main sessions declares ``Next: Claude`` or ``Next: Codex`` and no
prompt has been sent since, the board sends the Continue prompt quoting that
turn to the named agent, exactly as the user's Send would. Agents still cannot
wake each other: they only say who should go next, as they do in Manual.

A cruise stops, returning the project to Manual with the reason shown, when:

- the hand-offs are used up;
- a turn declares ``Next: User``, declares nothing, or names its own agent;
- a prompt sent since the cruise started failed;
- any plan-limit reading (Claude's or Codex's) is at or above the ceiling;
- a main session is no longer chosen.

The user can stop it at any time, or put the project on Hold.
"""
from __future__ import annotations

from .dispatch import deliver_codex
from .store import Conflict
from .turns import annotate, continue_prompts, latest_limits
from .usage import highest


def step(store, history: list[dict]) -> list[str]:
    """Advance every cruising project by at most one hand-off; return what happened."""
    log = []
    cruising = store.cruising_projects()
    if not cruising:
        return log
    pins, user = store.project_agents(), store.user_name()
    limits = {"Claude": store.limits("Claude"), "Codex": latest_limits(history, "Codex")}
    for mode in cruising:
        outcome = advance(store, mode, history, pins, user, limits)
        if outcome:
            log.append(f"{mode['project_id']}: {outcome}")
    return log


def advance(store, mode: dict, history: list[dict], pins: dict, user: str, limits: dict) -> str | None:
    project = mode["project_id"]

    def stop(reason: str) -> str:
        store.set_project_mode(project, "manual", reason=reason)
        return f"stopped: {reason}"

    project_pins = pins.get(project, {})
    if set(project_pins) != {"Claude", "Codex"}:
        return stop("A main session is no longer chosen")
    latest = next((t for t in (annotate(t, pins, user) for t in history if t["project"] == project)
                   if t["role"] == "main" and not t["automation"]), None)
    recent = store.list_dispatches(project, 1)
    last = recent[0] if recent else None
    if last and last["status"] == "failed" and last["requested_at"] >= mode["started_at"]:
        return stop(f"A prompt to {last['target']} failed: {last['detail'] or 'no detail'}")
    if latest is None or (last and last["requested_at"][:19] >= latest["recorded_at"][:19]):
        return None  # nothing new, or a prompt is already out for this turn
    declared = (latest["next"] or {}).get("agent")
    if declared is None:
        return stop(f"{latest['agent']}'s turn named no one to go next")
    if declared == "User":
        reason = latest["next"]["reason"]
        return stop(f"{latest['agent']} handed this to you" + (f": {reason}" if reason else ""))
    if declared == latest["agent"]:
        return stop(f"{latest['agent']} asked to continue its own work")
    for agent, reading in limits.items():
        window = highest(reading)
        if window and window["percent"] >= mode["ceiling"]:
            return stop(f"{agent}'s {window['label']} limit is at {window['percent']:g}% "
                        f"(ceiling {mode['ceiling']}%)")
    if (mode["hops_left"] or 0) <= 0:
        return stop("The hand-offs set for this cruise are used up")
    primed = project_pins[declared] in store.primed_sessions([project_pins[declared]])
    prompts = continue_prompts(latest, mode["name"], user)
    try:
        dispatch = store.create_dispatch(project, declared, prompts["short" if primed else "full"], origin="cruise")
    except Conflict:
        return None  # a send moments ago: try again on the next round
    if declared == "Codex" and dispatch["machine"] is None:
        delivered, detail = deliver_codex(dispatch["session_id"], dispatch["body"])
        store.finish_dispatch(dispatch["id"], delivered, detail)
    return f"sent dispatch {dispatch['id']} to {declared}"
