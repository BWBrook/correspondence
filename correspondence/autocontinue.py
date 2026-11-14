"""Auto-continue: the board carries a named pair's hand-offs itself, within limits the user sets.

The user starts a run from the browser with a number of hand-offs and a
plan-limit ceiling. While it runs, whenever the latest turn from one of the
pair's main sessions declares ``Next: Claude`` or ``Next: Codex`` and no
prompt has been sent since that turn arrived, the board sends the Continue
prompt quoting it to the named agent, exactly as the user's Send would. Agents
still cannot wake each other: they only say who should go next.

A run stops, showing the reason, when:

- the hand-offs are used up;
- a turn declares ``Next: User``, declares nothing, or names its own agent;
- a prompt sent since the run started failed or was cancelled;
- any current plan-limit reading (Claude's or Codex's) is at or above the ceiling;
- a main session is no longer chosen;
- the user presses Stop.

Stopping withdraws the run's prompts that no messenger or courier has picked up.
A prompt to a Claude session that is not running waits for it, and so does the run.

Each hand-off is spent in the same transaction that records the prompt, and
only while the run is still going (``Store.create_dispatch``), so a Stop can
never be overtaken by a send decided a moment earlier. Whether a turn has had
its prompt is read from the turn each prompt records (``after_turn``), never by
comparing clocks on different machines.
"""
from __future__ import annotations

from .dispatch import deliver_here
from .store import Conflict
from .turns import continue_prompts, latest_limits, latest_main_turn, turn_key
from .usage import highest


def step(store, history: list[dict]) -> list[str]:
    """Advance every running pair by at most one hand-off; return what happened."""
    runs = store.running_autos()
    if not runs:
        return []
    pins, user = store.pair_agents(), store.user_name()
    limits = {"Claude": store.limits("Claude"), "Codex": latest_limits(history, "Codex")}
    log = []
    for run in runs:
        outcome = advance(store, run, history, pins, user, limits)
        if outcome:
            log.append(f"{run['project_id']}/{run['pair_name']}: {outcome}")
    return log


def advance(store, run: dict, history: list[dict], pins: dict, user: str, limits: dict) -> str | None:
    pair_id = run["pair_id"]
    pair = store.pair(pair_id)

    def stop(reason: str) -> str:
        store.stop_auto(pair_id, reason, run=run["started_at"])
        return f"stopped: {reason}"

    project_pins = pins.get(pair_id, {})
    if set(project_pins) != {"Claude", "Codex"}:
        return stop("A main session is no longer chosen")
    failed = store.failed_since(pair_id, run["started_at"])
    if failed:
        return stop(f"A prompt to {failed['target']} failed: {failed['detail'] or 'no detail'}")
    for agent, reading in limits.items():
        window = highest(reading)
        if window and window["percent"] >= run["ceiling"]:
            label = window["label"].removesuffix(" limit")
            return stop(f"{agent}'s {label} limit is at {window['percent']:g}% (ceiling {run['ceiling']}%)")
    latest = latest_main_turn(history, pair, pins, user)
    if latest is None or store.prompted(pair_id, turn_key(latest)):
        return None  # nothing yet, or this turn's prompt is out: wait for the answer
    declared = (latest["next"] or {}).get("agent")
    if declared is None:
        return stop(f"{latest['agent']}'s turn named no one to go next")
    if declared == "User":
        reason = latest["next"]["reason"]
        return stop(f"{latest['agent']} handed this to you" + (f": {reason}" if reason else ""))
    if declared == latest["agent"]:
        return stop(f"{latest['agent']} asked to continue its own work")
    if run["hops_left"] <= 0:
        return stop("The hand-offs set for this run are used up")
    primed = bool(store.primed_sessions([project_pins[declared]]))
    prompt = continue_prompts(latest, run["name"], user)["short" if primed else "full"]
    try:
        dispatch = store.create_dispatch(pair_id, declared, prompt, after_turn=turn_key(latest), run=run["started_at"], expected_pins=project_pins)
    except Conflict:
        return None  # the state changed since this round read it: look again next round
    deliver_here(store, dispatch)
    return f"sent dispatch {dispatch['id']} to {declared}"
