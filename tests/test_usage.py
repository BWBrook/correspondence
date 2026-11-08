import json
import subprocess
import sys

import pytest

from correspondence.courier import scan
from correspondence.dispatch import LocalBoard
from correspondence.store import Store
from correspondence.turns import continue_prompts, daily_usage, latest_limits
from correspondence.usage import claude_limits, claude_turn_usage, codex_turn_usage


def write(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def assistant(mid, inp, created, read, out):
    return {"type": "assistant", "message": {"id": mid, "usage": {
        "input_tokens": inp, "cache_creation_input_tokens": created, "cache_read_input_tokens": read, "output_tokens": out}}}


def test_claude_usage_counts_each_message_once_since_the_prompt(tmp_path):
    transcript = write(tmp_path / "t.jsonl", [
        {"type": "user", "message": {"content": "earlier prompt"}},
        assistant("m0", 999, 0, 0, 999),
        {"type": "user", "message": {"content": [{"type": "text", "text": "Continue"}]}},
        assistant("m1", 2, 100, 1000, 50),
        assistant("m1", 2, 100, 1000, 50),  # one message split over two transcript lines
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "user", "isMeta": True, "message": {"content": "meta"}},
        {"type": "assistant", "isSidechain": True, "message": {"id": "s", "usage": {"output_tokens": 7777}}},
        assistant("m2", 3, 0, 1100, 20),
    ])
    assert claude_turn_usage(str(transcript)) == {"input": 105, "cached": 2100, "output": 70}
    assert claude_turn_usage(None) is None


def token_count(at, inp, cached, out, used=6.0):
    return {"timestamp": at, "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}, "rate_limits": {
        "primary": {"used_percent": used, "window_minutes": 10080, "resets_at": 1794614781}, "secondary": None,
        "credits": {"has_credits": True, "unlimited": False, "balance": "39855.11"}}}}


def rollout(tmp_path):
    return write(tmp_path / "rollout.jsonl", [
        {"type": "turn_context", "payload": {"model": "gpt-x", "cwd": "/w"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        token_count("2026-11-07T01:00:00Z", 1000, 800, 50),
        {"timestamp": "2026-11-07T01:00:01Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "One."}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t2"}},
        token_count("2026-11-07T02:00:00Z", 5000, 4500, 80, used=7.5),
        {"timestamp": "2026-11-07T02:00:01Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t2", "last_agent_message": "Two."}},
    ])


def test_codex_usage_is_the_difference_across_the_turn(tmp_path):
    path = rollout(tmp_path)
    usage, limits = codex_turn_usage(path, "t2")
    assert usage == {"input": 300, "cached": 3700, "output": 30}
    assert limits["windows"] == [{"label": "Weekly", "percent": 7.5, "resets_at": "2026-11-14T00:06:21+00:00"}]
    assert limits["credits"] == pytest.approx(39855.11)
    assert codex_turn_usage(path, "t1")[0] == {"input": 200, "cached": 800, "output": 50}
    assert codex_turn_usage(path)[0] == {"input": 300, "cached": 3700, "output": 30}


def test_courier_scan_carries_usage_across_reads(tmp_path):
    path = rollout(tmp_path)
    data = path.read_bytes()
    split = data.index(b'{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t2"')
    context = {}
    first, offset = scan(path, 0, context)
    assert [t["usage"] for t in first] == [{"input": 200, "cached": 800, "output": 50}, {"input": 300, "cached": 3700, "output": 30}]
    json.dumps(context)  # the courier saves this state between rounds
    path.write_bytes(data[:split])
    context = {}
    part, offset = scan(path, 0, context)
    path.write_bytes(data)
    rest, _ = scan(path, offset, json.loads(json.dumps(context)))
    assert rest[0]["usage"] == {"input": 300, "cached": 3700, "output": 30} and rest[0]["limits"]["windows"][0]["percent"] == 7.5


def test_hook_records_claude_usage(tmp_path):
    transcript = write(tmp_path / "t.jsonl", [{"type": "user", "message": {"content": "Go"}}, assistant("m", 1, 2, 3, 4)])
    payload = {"hook_event_name": "Stop", "session_id": "s", "transcript_path": str(transcript), "last_assistant_message": "Done."}
    subprocess.run([sys.executable, "-m", "correspondence.turn_hook", "--agent", "Claude", "--project", "p"],
                   input=json.dumps(payload), text=True, env={"CORRESPONDENCE_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
                   check=True)
    line = json.loads((tmp_path / "turns.jsonl").read_text())
    assert line["usage"] == {"input": 3, "cached": 3, "output": 4} and "limits" not in line


APP_REPORT = {"plan": {"status": "ok", "plan": "Team", "windows": [
    {"label": "5-hour limit", "percentUsed": 21, "resetsAt": "2026-11-07T14:30:00.322Z"},
    {"label": "Weekly · all models", "percentUsed": 34, "resetsAt": "2026-11-09T14:00:00.322Z"}]},
    "context": {"tokensUsed": 1}}


def test_claude_limits_come_from_the_app_report(tmp_path):
    reading = claude_limits(APP_REPORT)
    assert [(w["label"], w["percent"]) for w in reading["windows"]] == [("5-hour limit", 21.0), ("Weekly · all models", 34.0)]
    with pytest.raises(ValueError):
        claude_limits({"plan": {"status": "unavailable"}})
    board = LocalBoard(Store(tmp_path), "wsl")
    board.report_limits(APP_REPORT)
    assert board.store.limits("Claude")["windows"][1]["percent"] == 34.0


def test_limits_command_accepts_the_report_with_surrounding_text(tmp_path):
    env = {"CORRESPONDENCE_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    Store(tmp_path)
    result = subprocess.run([sys.executable, "-m", "correspondence.dispatch", "limits"],
                            input="Usage report:\n" + json.dumps(APP_REPORT) + "\n", text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Claude limits: 5-hour limit 21%, Weekly · all models 34%"
    bad = subprocess.run([sys.executable, "-m", "correspondence.dispatch", "limits"], input="nothing",
                         text=True, capture_output=True, env=env)
    assert bad.returncode != 0 and "Limits not recorded" in bad.stderr


def test_daily_usage_and_latest_limits():
    turns = [
        {"recorded_at": "2026-11-07T05:00:00+00:00", "project": "p", "agent": "Codex", "usage": {"input": 1, "cached": 2, "output": 3},
         "limits": {"windows": [{"label": "Weekly", "percent": 9.0}]}},
        {"recorded_at": "2026-11-07T04:00:00+00:00", "project": "p", "agent": "Codex", "usage": {"input": 10, "cached": 20, "output": 30},
         "limits": {"windows": [{"label": "Weekly", "percent": 8.0}]}},
        {"recorded_at": "2026-11-07T03:00:00+00:00", "project": "q", "agent": "Claude", "usage": {"input": 5, "cached": 5, "output": 5}},
        {"recorded_at": "2026-11-07T02:00:00+00:00", "project": "p", "agent": "Claude"},
    ]
    rows = daily_usage(turns, "p")
    assert [(r["agent"], r["turns"], r["input"], r["cached"], r["output"]) for r in rows] == [("Codex", 2, 11, 22, 33)]
    assert {r["project"] for r in daily_usage(turns)} == {"p", "q"}
    assert latest_limits(turns, "Codex")["windows"][0]["percent"] == 9.0
    assert latest_limits(turns, "Claude") is None


def test_continue_prompts_match_the_board_wording(monkeypatch):
    monkeypatch.setenv("TZ", "Australia/Hobart")
    import time
    time.tzset()
    turn = {"agent": "Codex", "recorded_at": "2026-11-07T05:08:57+00:00", "message": "Line one.\nNext: Claude"}
    prompts = continue_prompts(turn, "Dwarf-Nova Memory", "Barry")
    assert prompts["short"] == "Codex replied on 7 Nov 2026 at 4:08 pm, closing with:\n\n> Line one.\n> Next: Claude"
    assert prompts["full"].startswith("Continue — from Barry, sent through the board.\n\nCodex's latest turn in "
                                      "Dwarf-Nova Memory ended at 7 Nov 2026 at 4:08 pm.")
    assert prompts["full"].endswith("> Next: Claude\n\nTake your next turn.")


def test_courier_resuming_without_a_token_count_recovers_it(tmp_path):
    path = rollout(tmp_path)
    data = path.read_bytes()
    split = data.index(b'{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t2"')
    rest, _ = scan(path, split, {"model": "gpt-x", "cwd": "/w"})  # state from a courier before usage existed
    assert rest[0]["usage"] == {"input": 300, "cached": 3700, "output": 30}
    restored = {key: None for key in ("model", "cwd", "total", "baseline", "limits")}  # as watch() restores it
    rest, _ = scan(path, split, restored)
    assert rest[0]["usage"] == {"input": 300, "cached": 3700, "output": 30}
