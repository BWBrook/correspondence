"""Token use per turn and plan-limit readings, from the agents' own records.

Each turn's usage is ``{"input", "cached", "output"}``: new (uncached) input
tokens, input tokens read from the prompt cache, and output tokens including
reasoning. Claude's comes from the transcript's per-message ``usage`` since
the turn's prompt; Codex's is the difference in the rollout's cumulative
``token_count`` across the turn. Subagents and delegated jobs that write their
own transcripts or rollouts are not included.

A limit reading is ``{"read_at", "windows": [{"label", "percent",
"resets_at"}], "credits"}``. Codex writes its readings into every rollout
``token_count``. Claude's plan limits are visible only to sessions in the
Claude app, so the Board Messenger reports them after each relay (see
``correspondence-dispatch limits``).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

USAGE_KEYS = ("input", "cached", "output")


def claude_turn_usage(transcript_path: str | None) -> dict | None:
    """Tokens used since the latest prompt in a Claude transcript."""
    if not transcript_path or not Path(transcript_path).is_file():
        return None
    messages: dict = {}
    with open(transcript_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            message = entry.get("message") or {}
            if entry.get("type") == "user" and not entry.get("isMeta") and _is_prompt(message.get("content")):
                messages = {}
            elif entry.get("type") == "assistant" and isinstance(message.get("usage"), dict):
                messages[message.get("id") or len(messages)] = message["usage"]
    if not messages:
        return None
    total = dict.fromkeys(USAGE_KEYS, 0)
    for usage in messages.values():
        total["input"] += int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
        total["cached"] += int(usage.get("cache_read_input_tokens") or 0)
        total["output"] += int(usage.get("output_tokens") or 0)
    return total


def _is_prompt(content) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "text" for block in content)


def feed_codex(state: dict, entry: dict) -> None:
    """Track a rollout's cumulative tokens, the total when the latest turn started, and its limits."""
    payload = entry.get("payload") if isinstance(entry, dict) else None
    if entry.get("type") != "event_msg" or not isinstance(payload, dict):
        return
    if payload.get("type") == "token_count":
        total = (payload.get("info") or {}).get("total_token_usage")
        if isinstance(total, dict):
            state["total"] = {k: int(total.get(k) or 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
        if isinstance(payload.get("rate_limits"), dict):
            state["limits"] = codex_limits(payload["rate_limits"], entry.get("timestamp"))
    elif payload.get("type") == "task_started":
        state["baseline"] = state.get("total")


def codex_turn(state: dict) -> dict | None:
    """The tokens used since the latest turn started, from ``feed_codex`` state."""
    end = state.get("total")
    if not end:
        return None
    start = state.get("baseline") or dict.fromkeys(end, 0)
    used = {k: end[k] - start.get(k, 0) for k in end}
    return {"input": used["input_tokens"] - used["cached_input_tokens"], "cached": used["cached_input_tokens"],
            "output": used["output_tokens"]}


def codex_turn_usage(path: Path | None, turn_id: str | None = None) -> tuple[dict | None, dict | None]:
    """A Codex turn's usage and the latest limit reading, from its rollout file."""
    if path is None or not Path(path).is_file():
        return None, None
    from .courier import read_rollout

    state: dict = {}
    for raw in read_rollout(Path(path), 0).splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload") or {}
        if turn_id and payload.get("type") == "task_started" and payload.get("turn_id") != turn_id and state.get("found"):
            break  # a later turn: stop at the one asked for
        feed_codex(state, entry)
        if payload.get("type") == "task_started" and (not turn_id or payload.get("turn_id") == turn_id):
            state["found"] = True
    return codex_turn(state), state.get("limits")


def _iso(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def _window_label(minutes) -> str:
    return {300: "5-hour", 10080: "Weekly"}.get(minutes, f"{minutes}-minute" if minutes else "Limit")


def codex_limits(rate_limits: dict, at=None) -> dict:
    windows = []
    for key in ("primary", "secondary"):
        window = rate_limits.get(key)
        if isinstance(window, dict) and window.get("used_percent") is not None:
            windows.append({"label": _window_label(window.get("window_minutes")), "percent": float(window["used_percent"]),
                            "resets_at": _iso(window.get("resets_at"))})
    credits = rate_limits.get("credits") or {}
    balance = None
    if credits.get("has_credits") and not credits.get("unlimited"):
        try:
            balance = float(credits.get("balance"))
        except (TypeError, ValueError):
            balance = None
    return {"read_at": _iso(at) or now(), "windows": windows, "credits": balance}


def claude_limits(report: dict, at=None) -> dict:
    """Normalise the Claude app's usage report (its ``plan`` object, or the whole report)."""
    plan = report.get("plan", report) if isinstance(report, dict) else {}
    if not isinstance(plan, dict) or plan.get("status") not in (None, "ok"):
        raise ValueError(f"No plan limits in this report (status {plan.get('status') if isinstance(plan, dict) else None})")
    windows = []
    for window in plan.get("windows") or []:
        if not isinstance(window, dict) or window.get("percentUsed") is None:
            continue
        windows.append({"label": str(window.get("label") or "Limit")[:60], "percent": float(window["percentUsed"]),
                        "resets_at": _iso(window.get("resetsAt"))})
    if not windows:
        raise ValueError("The report has no limit windows")
    return {"read_at": _iso(at) or now(), "windows": windows, "credits": None}


def highest(limits: dict | None, at: str | None = None) -> dict | None:
    """The fullest window in a reading that has not reset since it was read."""
    at = at or now()
    windows = [w for w in (limits or {}).get("windows") or [] if not w.get("resets_at") or w["resets_at"] > at]
    return max(windows, key=lambda w: w["percent"]) if windows else None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
