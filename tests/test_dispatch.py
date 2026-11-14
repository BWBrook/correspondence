import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from correspondence.store import Conflict, Store
from correspondence.web import create_app

HEADERS = {"X-Correspondence-Client": "browser", "Origin": "http://127.0.0.1:8765"}
MAIN, OTHER = "01a17cc1-main-thread", "01a18111-heartbeat"


def pair_id(client):
    return client.get("/api/projects/p/pairs").json()[0]["id"]


def pair_pins(client, pair=None):
    return client.get(f"/api/pairs/{pair or pair_id(client)}/agents").json()["pins"]


def turn(agent, session, at, message):
    return json.dumps({"recorded_at": at, "agent": agent, "project": "p", "session_id": session, "message": message})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    fake = tmp_path / "codex"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {tmp_path}/codex-args\necho "Queued message q1 for thread $3."\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CORRESPONDENCE_CODEX", str(fake))
    (tmp_path / "turns.jsonl").write_text("\n".join([
        turn("Codex", MAIN, "2026-11-04T11:20:18+00:00", "Main turn."),
        turn("Codex", OTHER, "2026-11-04T15:00:42+00:00", "<heartbeat>\n<decision>DONT_NOTIFY</decision>"),
    ]) + "\n")
    Store(tmp_path).create_project("p", "P")
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as client:
        yield client, tmp_path


def test_pins_mark_other_sessions_and_heartbeats(setup):
    client, _ = setup
    sessions = client.get(f"/api/pairs/{pair_id(client)}/agents").json()["sessions"]
    assert {s["session_id"]: s["automation"] for s in sessions} == {MAIN: False, OTHER: True}
    assert client.get("/api/turns").json()[0]["message"] == "Main turn."
    assert client.post(f"/api/pairs/{pair_id(client)}/agents", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "agent": "Codex", "session_id": MAIN}, headers=HEADERS).json()["pins"] == {"Codex": MAIN}
    turns = client.get("/api/turns?project_id=p").json()
    assert [(t["role"], t["automation"]) for t in turns] == [("other", True), ("main", False)]
    assert client.post(f"/api/pairs/{pair_id(client)}/agents", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "agent": "Codex", "session_id": None}, headers=HEADERS).json()["pins"] == {}


def test_codex_dispatch_queues_on_the_pinned_thread_and_tracks_the_answer(setup):
    client, tmp_path = setup
    body = {"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Codex", "body": "Continue, please."}
    assert client.post("/api/dispatches", json=body, headers=HEADERS).status_code == 409
    client.post(f"/api/pairs/{pair_id(client)}/agents", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "agent": "Codex", "session_id": MAIN}, headers=HEADERS)
    body["expected_pins"] = pair_pins(client)
    body["expected_state"] = client.app.state.store.pair(pair_id(client))["state_token"]
    sent = client.post("/api/dispatches", json=body, headers=HEADERS)
    assert sent.status_code == 201 and sent.json()["status"] == "delivered"
    assert (tmp_path / "codex-args").read_text().split("\n")[:5] == ["queue", "--thread", MAIN, "--message", "Continue, please."]
    assert client.post("/api/dispatches", json=body, headers=HEADERS).status_code == 409
    assert client.get(f"/api/dispatches?pair_id={pair_id(client)}").json()[0]["answered_at"] is None
    with open(tmp_path / "turns.jsonl", "a") as ledger:
        ledger.write(turn("Codex", MAIN, "2099-01-01T00:00:00+00:00", "Done.") + "\n")
    assert client.get(f"/api/dispatches?pair_id={pair_id(client)}").json()[0]["answered_at"] == "2099-01-01T00:00:00+00:00"


def test_dispatch_is_browser_only(setup):
    client, _ = setup
    client.post(f"/api/pairs/{pair_id(client)}/agents", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "agent": "Codex", "session_id": MAIN}, headers=HEADERS)
    assert client.post("/api/dispatches", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Codex", "body": "x"}).status_code == 403


CLAUDE = "f4766779-d9e6-4b66-b078-9f3b4b611a97"


@pytest.fixture
def claude(setup, monkeypatch):
    client, tmp_path = setup
    sessions = tmp_path / "claude-sessions"
    sessions.mkdir()
    monkeypatch.setenv("CORRESPONDENCE_CLAUDE_SESSIONS", str(sessions))
    monkeypatch.setenv("CORRESPONDENCE_DATA_DIR", str(tmp_path))
    (sessions / "1.json").write_text(json.dumps({"pid": 999999999, "sessionId": CLAUDE, "name": "Stale name", "updatedAt": 9}))
    (sessions / "2.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": CLAUDE, "name": "Silurian Plants",
                                                 "status": "idle", "updatedAt": 1}))
    with open(tmp_path / "turns.jsonl", "a") as ledger:
        ledger.write(turn("Claude", CLAUDE, "2026-11-04T15:00:43+00:00", "Ready.") + "\n")
    client.post(f"/api/pairs/{pair_id(client)}/agents", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "agent": "Claude", "session_id": CLAUDE}, headers=HEADERS)
    return client, tmp_path


def test_claude_prompt_waits_for_the_messenger_and_is_claimed_once(claude, capsys):
    from correspondence.dispatch import LocalBoard, wait
    client, tmp_path = claude
    agents = client.get(f"/api/pairs/{pair_id(client)}/agents").json()
    assert agents["claude_live"] == {"name": "Silurian Plants", "status": "idle"} and agents["messenger_listening"] is False
    sent = client.post("/api/dispatches", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Claude", "body": "Line one.\nLine two."}, headers=HEADERS).json()
    assert sent["status"] == "queued" and sent["claimed_at"] is None
    store = Store(tmp_path)
    wait(LocalBoard(store), poll=0.01)
    event = json.loads(capsys.readouterr().out)
    assert {k: event[k] for k in ("dispatch", "to", "project")} == {"dispatch": sent["id"], "to": "Silurian Plants", "project": "p"}
    note, body = event["message"].split("\n\n", 1)
    assert body == "Line one.\nLine two." and "do not SendMessage" in note
    assert client.get(f"/api/pairs/{pair_id(client)}/agents").json()["messenger_listening"] is True
    assert store.waiting_dispatches("Claude") == [] and store.claim_dispatch(sent["id"]) is False
    assert client.post(f"/api/dispatches/{sent['id']}/cancel", json={}, headers=HEADERS).status_code == 409
    store.finish_dispatch(sent["id"], True, "")
    assert client.get(f"/api/dispatches?pair_id={pair_id(client)}").json()[0]["status"] == "delivered"


def test_a_prompt_can_be_withdrawn_until_picked_up_and_given_up_once_its_claim_goes_stale(claude):
    client, tmp_path = claude
    send = lambda: client.post("/api/dispatches", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Claude", "body": "x"}, headers=HEADERS)
    first = send().json()
    assert send().status_code == 409  # one prompt in flight per agent
    cancelled = client.post(f"/api/dispatches/{first['id']}/cancel", json={}, headers=HEADERS).json()
    assert cancelled["status"] == "failed" and cancelled["detail"] == "Cancelled by User"
    second = send().json()
    store = Store(tmp_path)
    assert store.claim_dispatch(second["id"])
    assert client.post(f"/api/dispatches/{second['id']}/cancel", json={}, headers=HEADERS).status_code == 409
    with store.connect(write=True) as con:  # the messenger took it and never reported back
        con.execute("UPDATE dispatches SET claimed_at='2026-01-01T00:00:00+00:00' WHERE id=?", (second["id"],))
    given_up = client.post(f"/api/dispatches/{second['id']}/cancel", json={}, headers=HEADERS).json()
    assert given_up["status"] == "failed" and "no outcome was reported" in given_up["detail"]
    assert store.finish_dispatch(second["id"], True, "late report")["status"] == "failed"
    third = send().json()
    store.set_pair_agent(store.pairs("p")[0]["id"], "Claude", "another-claude-session")  # never wake a session that is no longer main
    withdrawn = store.list_dispatches(store.pairs("p")[0]["id"], 1)[0]
    assert (withdrawn["id"], withdrawn["status"], withdrawn["detail"]) == (third["id"], "failed", "Cancelled: the main session changed")


def test_done_command_records_the_outcome_once(claude):
    import subprocess, sys
    client, tmp_path = claude
    sent = client.post("/api/dispatches", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Claude", "body": "x"}, headers=HEADERS).json()
    Store(tmp_path).claim_dispatch(sent["id"])
    run = lambda *a: subprocess.run([sys.executable, "-c", "from correspondence.dispatch import main; main()", "done", str(sent["id"]), *a],
                                    capture_output=True, text=True, env={**os.environ, "CORRESPONDENCE_DATA_DIR": str(tmp_path)})
    assert run("delivered").returncode == 0
    second = run("failed", "late")
    assert second.returncode != 0 and "already delivered" in second.stderr


def test_failed_delivery_is_recorded(setup, monkeypatch):
    client, tmp_path = setup
    monkeypatch.setenv("CORRESPONDENCE_CODEX", str(tmp_path / "missing-codex"))
    client.post(f"/api/pairs/{pair_id(client)}/agents", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "agent": "Codex", "session_id": MAIN}, headers=HEADERS)
    sent = client.post("/api/dispatches", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Codex", "body": "x"}, headers=HEADERS).json()
    assert sent["status"] == "failed" and "FileNotFoundError" in sent["detail"]
    assert client.post("/api/dispatches", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Codex", "body": "x"}, headers=HEADERS).status_code == 201


def test_archive_carries_pins_and_dispatches(tmp_path):
    store = Store(tmp_path)
    store.create_project("p", "P")
    store.set_pair_agent(store.pairs("p")[0]["id"], "Codex", MAIN)
    store.set_pair_agent(store.pairs("p")[0]["id"], "Claude", CLAUDE)
    sent = store.create_dispatch(store.pairs("p")[0]["id"], "Codex", "x")
    store.claim_dispatch(sent["id"])
    store.finish_dispatch(sent["id"], True, "")
    store.start_auto(store.pairs("p")[0]["id"], 3, 80)
    waiting = store.create_dispatch(store.pairs("p")[0]["id"], "Claude", "still waiting")
    token = store.list_projects("User")[0]["state_token"]
    archived = store.archive_project("p", token)
    assert store.machine_pins(None) == []  # couriers stop watching an archived project's sessions
    with store.connect() as con:
        assert con.execute("SELECT DISTINCT p.project_id FROM pair_agents a JOIN pairs p ON p.id=a.pair_id").fetchall()[0][0] == archived["id"]
        assert {r[0] for r in con.execute("SELECT project_id FROM dispatches")} == {archived["id"]}
        assert con.execute("SELECT detail FROM dispatches WHERE id=?", (waiting["id"],)).fetchone()[0] == "Cancelled: project archived"
        assert con.execute("SELECT stopped_reason FROM auto_continue").fetchone()[0] == "The project was archived or restored"


def test_claude_prompt_waits_until_its_session_is_running_and_idle(claude):
    from correspondence.dispatch import LocalBoard, collect
    client, tmp_path = claude
    record = tmp_path / "claude-sessions" / "2.json"
    live = record.read_text()
    record.unlink()
    sent = client.post("/api/dispatches", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Claude", "body": "x"}, headers=HEADERS).json()
    store = Store(tmp_path)
    assert collect(LocalBoard(store)) == []  # not running: the prompt waits instead of failing
    record.write_text(live.replace('"idle"', '"busy"'))
    assert collect(LocalBoard(store)) == [] and store.waiting_dispatches("Claude")[0]["id"] == sent["id"]
    record.write_text(live)
    assert [e["dispatch"] for e in collect(LocalBoard(store))] == [sent["id"]] and collect(LocalBoard(store)) == []


def test_pins_on_another_machine_wait_for_its_courier(setup, monkeypatch):
    from correspondence.dispatch import LocalBoard
    client, tmp_path = setup
    remote_codex, remote_claude = "01a0fb34-wsl-thread", "2114b555-wsl-session"
    with open(tmp_path / "turns.jsonl", "a") as ledger:
        for agent, session in (("Codex", remote_codex), ("Claude", remote_claude)):
            ledger.write(json.dumps({"recorded_at": "2026-11-06T03:50:42+00:00", "agent": agent, "project": "p",
                                     "session_id": session, "message": "On wsl.", "machine": "wsl"}) + "\n")
    for agent, session in (("Codex", remote_codex), ("Claude", remote_claude)):
        client.post(f"/api/pairs/{pair_id(client)}/agents", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "agent": agent, "session_id": session}, headers=HEADERS)
    agents = client.get(f"/api/pairs/{pair_id(client)}/agents").json()
    assert agents["machines"] == {"Codex": "wsl", "Claude": "wsl"} and agents["couriers"] == {"wsl": False}
    assert agents["claude_live"] is None and agents["messenger_listening"] is False
    sent = client.post("/api/dispatches", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Codex", "body": "Over to wsl."}, headers=HEADERS)
    assert sent.json()["status"] == "queued" and sent.json()["machine"] == "wsl"
    assert not (tmp_path / "codex-args").exists()
    board = LocalBoard(Store(tmp_path), "wsl")
    board.report_sessions({remote_claude: {"name": "Extinction Vortex", "status": "idle"}})
    board.heartbeat()
    agents = client.get(f"/api/pairs/{pair_id(client)}/agents").json()
    assert agents["couriers"] == {"wsl": True} and agents["messenger_listening"] is True
    assert agents["claude_live"] == {"name": "Extinction Vortex", "status": "idle"}


def test_a_session_gets_the_full_relay_note_only_until_one_prompt_is_delivered(claude, capsys):
    from correspondence.dispatch import RELAY_NOTE, SHORT_NOTE, LocalBoard, relay_note, serve, wait
    client, tmp_path = claude
    store = Store(tmp_path)
    notes = []
    for _ in range(2):
        assert client.post("/api/dispatches", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Claude", "body": "x"}, headers=HEADERS).status_code == 201
        assert client.get(f"/api/pairs/{pair_id(client)}/agents").json()["primed"] == {"Claude": bool(notes)}
        wait(LocalBoard(store), poll=0.01)
        event = json.loads(capsys.readouterr().out)
        notes.append(event["message"].split("\n\n", 1)[0])
        store.finish_dispatch(event["dispatch"], True, "")
        with store.connect(write=True) as con:
            con.execute("UPDATE dispatches SET delivered_at='2026-01-01T00:00:00+00:00' WHERE status='delivered'")
    assert notes == [RELAY_NOTE.format(user="User"), SHORT_NOTE.format(user="User")]
    client.post("/api/dispatches", json={"expected_state": client.app.state.store.pair(pair_id(client))["state_token"], "pair_id": pair_id(client), "expected_pins": pair_pins(client), "target": "Claude", "body": "x"}, headers=HEADERS)
    (tmp_path / "in.jsonl").write_text('{"op": "waiting", "target": "Claude"}\n')
    with open(tmp_path / "in.jsonl") as stdin, open(tmp_path / "out.jsonl", "w") as stdout:
        serve(LocalBoard(store), stdin, stdout)
    waiting = json.loads((tmp_path / "out.jsonl").read_text())["result"][0]
    assert waiting["primed"] and waiting["user_name"] == "User"
    store.set_user_name("Ada")
    assert relay_note(LocalBoard(store).waiting("Claude")[0]) == "[From Ada through the board. Answer here, not to the messenger.]"


def test_two_claude_pairs_deliver_and_acknowledge_only_their_own_session(claude):
    from correspondence.dispatch import LocalBoard, collect
    client, tmp_path = claude
    main = pair_id(client)
    second = client.post('/api/projects/p/pairs', json={'name': 'Worktree'}, headers=HEADERS).json()['id']
    second_claude = 'claude-worktree-session'
    with open(tmp_path / 'turns.jsonl', 'a') as ledger:
        ledger.write(turn('Claude', second_claude, '2026-11-04T15:01:00+00:00', 'Worktree ready.') + '\n')
    (tmp_path / 'claude-sessions/3.json').write_text(json.dumps({
        'pid': os.getpid(), 'sessionId': second_claude, 'name': 'Worktree build', 'status': 'idle',
    }))
    pinned = client.post(f'/api/pairs/{second}/agents', json={"expected_state": client.app.state.store.pair(second)["state_token"], 'agent': 'Claude', 'session_id': second_claude}, headers=HEADERS)
    assert pinned.status_code == 200
    main_send = client.post('/api/dispatches', json={"expected_state": client.app.state.store.pair(main)["state_token"], 'pair_id': main, 'expected_pins': pair_pins(client, main), 'target': 'Claude', 'body': 'Main only.'}, headers=HEADERS).json()
    second_send = client.post('/api/dispatches', json={"expected_state": client.app.state.store.pair(second)["state_token"], 'pair_id': second, 'expected_pins': pair_pins(client, second), 'target': 'Claude', 'body': 'Worktree only.'}, headers=HEADERS).json()
    store = Store(tmp_path)
    events = collect(LocalBoard(store))
    assert {e['pair_id']: (e['to'], e['message'].split('\n\n', 1)[1]) for e in events} == {
        main: ('Silurian Plants', 'Main only.'), second: ('Worktree build', 'Worktree only.'),
    }
    assert {e['dispatch'] for e in events} == {main_send['id'], second_send['id']}
    assert collect(LocalBoard(store)) == []
    for event in events:
        store.finish_dispatch(event['dispatch'], True, '')
    at = '2099-01-01T00:00:00+00:00'
    with open(tmp_path / 'turns.jsonl', 'a') as ledger:
        ledger.write(turn('Claude', second_claude, at, 'Worktree done.') + '\n')
        for mismatch in ({'project': 'another-project'}, {'agent': 'Codex'}, {'machine': 'wsl'}):
            wrong = json.loads(turn('Claude', CLAUDE, at, 'Not this recipient.'))
            ledger.write(json.dumps({**wrong, **mismatch}) + '\n')
    main_history = client.get(f'/api/dispatches?pair_id={main}').json()
    second_history = client.get(f'/api/dispatches?pair_id={second}').json()
    assert len(main_history) == len(second_history) == 1
    assert main_history[0]['answered_at'] is None
    assert second_history[0]['answered_at'] == at
    with open(tmp_path / 'turns.jsonl', 'a') as ledger:
        ledger.write(turn('Claude', CLAUDE, at, 'Main done.') + '\n')
    assert client.get(f'/api/dispatches?pair_id={main}').json()[0]['answered_at'] == at



@pytest.mark.parametrize('changed_agent', ['Claude', 'Codex'])
def test_stale_source_or_target_pins_cannot_dispatch_to_a_reconfigured_pair(claude, changed_agent):
    client, tmp_path = claude
    pair = pair_id(client)
    assert client.post(f'/api/pairs/{pair}/agents', json={"expected_state": client.app.state.store.pair(pair)["state_token"], 'agent': 'Codex', 'session_id': MAIN}, headers=HEADERS).status_code == 200
    old_pins = pair_pins(client, pair)
    replacement = 'replacement-' + changed_agent.lower() + '-session'
    with open(tmp_path / 'turns.jsonl', 'a') as ledger:
        ledger.write(turn(changed_agent, replacement, '2026-11-04T16:00:00+00:00', 'Replacement ready.') + '\n')
    changed = client.post(f'/api/pairs/{pair}/agents', json={"expected_state": client.app.state.store.pair(pair)["state_token"], 'agent': changed_agent, 'session_id': replacement}, headers=HEADERS)
    assert changed.status_code == 200
    body = {"expected_state": client.app.state.store.pair(pair)["state_token"], 'pair_id': pair, 'target': 'Codex', 'body': 'Continue from the displayed context.', 'expected_pins': old_pins}
    assert client.post('/api/dispatches', json=body, headers=HEADERS).status_code == 409
    assert client.get(f'/api/dispatches?pair_id={pair}').json() == []
    assert not (tmp_path / 'codex-args').exists()
    body['expected_pins'] = pair_pins(client, pair)
    sent = client.post('/api/dispatches', json=body, headers=HEADERS)
    assert sent.status_code == 201 and sent.json()['status'] == 'delivered'
    expected_target = replacement if changed_agent == 'Codex' else MAIN
    assert sent.json()['session_id'] == expected_target
    assert (tmp_path / 'codex-args').read_text().splitlines()[2] == expected_target
