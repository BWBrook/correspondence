import json
import stat

import pytest
from fastapi.testclient import TestClient

from correspondence import attention, autocontinue
from correspondence.turns import declared_next, read_turns
from correspondence.web import create_app

HEADERS = {'X-Correspondence-Client': 'browser', 'Origin': 'http://127.0.0.1:8765'}


@pytest.fixture
def board(tmp_path, monkeypatch):
    fake = tmp_path / 'fake-codex'
    fake.write_text('#!/bin/sh\necho Queued.\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv('CORRESPONDENCE_CODEX', str(fake))
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8765') as client:
        store = client.app.state.store
        store.set_user_name('Barry W. Brook')
        store.create_project('earth', 'Earth')
        a = store.pairs('earth')[0]['id']
        b = store.create_pair('earth', 'Budyko')['id']
        for pair in (a, b):
            for agent in ('Claude', 'Codex'):
                store.set_pair_agent(pair, agent, f'{agent.lower()}-session-{pair}')
        yield client, store, a, b


def add_turn(store, pair, agent, minute, message, *, session=None, machine=None):
    row = {'project': 'earth', 'agent': agent, 'session_id': session or store.pair_agents(pair)[agent],
           'recorded_at': f'2030-01-01T00:{minute:02d}:00+00:00', 'message': message, 'machine': machine}
    with (store.directory / 'turns.jsonl').open('a') as f:
        f.write(json.dumps(row) + '\n')
    return row


def project(client):
    result = client.get('/api/projects')
    assert result.status_code == 200
    return next(p for p in result.json() if p['id'] == 'earth')


def post(client, path, payload=None):
    return client.post(path, json=payload or {}, headers=HEADERS)


def decisions(client, pair):
    return client.get(f'/api/pairs/{pair}/attention').json()['decisions']


def test_explicit_requests_need_human_target_and_real_pinned_turn(board):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'Done.\nNext: Barry W. Brook — ready for your next project')
    assert project(client)['status']['kind'] == 'activity'
    assert decisions(client, a) == []
    add_turn(store, a, 'Codex', 2, 'Question\nNext: Claude — decision needed: Which method?')
    add_turn(store, a, 'Claude', 3, 'Question\nNext: User — decision needed: Pick A or B', session='advisor-session')
    add_turn(store, a, 'Claude', 4, '<heartbeat>\nNext: Barry W. Brook — decision needed: Ignore me')
    assert decisions(client, a) == []
    add_turn(store, a, 'Claude', 5, 'Please choose.\nNext: Barry W. Brook — decision needed: Pick A or B')
    p = project(client)
    assert p['status']['kind'] == 'decision' and p['live'] is True
    assert decisions(client, a)[0]['summary'] == 'Pick A or B'
    assert decisions(client, b) == []
    assert declared_next('Next: Barry W. Brook — decision needed: A?','Barry W. Brook')['agent'] == 'User'
    assert declared_next('Next: Barry W. Brooklyn — decision needed: A?','Barry W. Brook') is None
    assert declared_next('Next: User — decision needed: A?','Barry W. Brook')['agent'] == 'User'


def test_visit_clears_only_human_activity_and_never_decisions(board):
    client, store, a, b = board
    thread = store.create_thread('Codex', 'earth', 'Review', 'Please read')['id']
    store.post_message('Claude', thread, 'New finding')
    add_turn(store, a, 'Claude', 1, 'Choose.\nNext: User — decision needed: Red or blue?')
    before = store.read_thread('User', thread)['thread']['read_markers']
    assert before == {'User': 0, 'Claude': 0, 'Codex': 0}
    assert project(client)['unread_count'] == 2
    with store.connect() as con:
        assert not con.execute("SELECT 1 FROM settings WHERE key LIKE 'human.%'").fetchall()
    assert post(client, '/api/projects/earth/seen').json()['messages_marked'] == 2
    after = store.read_thread('User', thread)['thread']['read_markers']
    assert after['User'] > 0 and after['Claude'] == after['Codex'] == 0
    assert project(client)['status']['kind'] == 'decision'
    assert project(client)['unread_count'] == 0
    store.post_message('Codex', thread, 'After the visit')
    assert project(client)['unread_count'] == 1
    assert len(decisions(client, a)) == 1


def test_other_agent_cannot_clear_request_but_requester_can_resume(board):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'Choose.\nNext: User — decision needed: A?')
    add_turn(store, a, 'Codex', 2, 'Other work complete.\nNext: Claude')
    add_turn(store, b, 'Claude', 3, 'Unrelated done.\nNext: Codex')
    assert len(decisions(client, a)) == 1
    add_turn(store, a, 'Claude', 4, '<heartbeat>\nDone')
    assert len(decisions(client, a)) == 1
    add_turn(store, a, 'Claude', 5, 'Applied your answer.\nNext: Codex')
    assert decisions(client, a) == []


def test_handling_is_exact_pair_and_request_scoped_and_does_not_resume_auto(board):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'Choose.\nNext: User — decision needed: A?')
    add_turn(store, a, 'Codex', 2, 'Choose too.\nNext: User — decision needed: B?')
    add_turn(store, b, 'Claude', 3, 'Choose elsewhere.\nNext: User — decision needed: C?')
    first = decisions(client, a)[1]
    assert post(client, f'/api/pairs/{b}/attention/handle', {'key': first['key']}).status_code == 409
    assert post(client, f'/api/pairs/{a}/attention/handle', {'key': first['key']}).status_code == 200
    assert len(decisions(client, a)) == len(decisions(client, b)) == 1
    assert not store.auto_continue(a)['running'] and not store.list_dispatches(a)
    assert post(client, f'/api/pairs/{a}/attention/handle', {'key': first['key']}).status_code == 409
    add_turn(store, a, 'Claude', 4, 'New question.\nNext: User — decision needed: D?')
    assert {d['summary'] for d in decisions(client, a)} == {'B?', 'D?'}
    assert post(client, f'/api/pairs/{a}/attention/handle', {'key': first['key']}).status_code == 409


def test_activity_and_run_states_keep_pairs_independent(board):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'Done.\nNext: Codex')
    assert project(client)['status']['kind'] == 'activity'
    post(client, '/api/projects/earth/seen')
    assert project(client)['status']['kind'] == 'quiet' and project(client)['live'] is False
    store.start_auto(b, 3, 80)
    assert project(client)['status']['kind'] == 'running'
    post(client, '/api/projects/earth/seen')
    assert project(client)['status']['kind'] == 'running' and project(client)['live'] is True
    add_turn(store, a, 'Claude', 2, 'Choose.\nNext: User — decision needed: A?')
    assert project(client)['status']['kind'] == 'decision'
    assert next(p for p in project(client)['status']['pairs'] if p['id'] == b)['running']
    store.stop_auto(b)
    post(client, '/api/projects/earth/seen')
    key = decisions(client, a)[0]['key']
    post(client, f'/api/pairs/{a}/attention/handle', {'key': key})
    assert project(client)['status']['kind'] == 'quiet'
    store.start_auto(b, 2, 80)
    store.stop_auto(b)
    assert project(client)['status']['kind'] == 'activity'
    post(client, '/api/projects/earth/seen')
    assert project(client)['status']['kind'] == 'quiet'


def test_unseen_turns_heartbeats_and_pending_manual_delivery(board):
    client, store, a, b = board
    post(client, '/api/projects/earth/seen')
    add_turn(store, a, 'Claude', 1, '<heartbeat>\nInfrastructure only')
    assert project(client)['live'] is False
    d = store.create_dispatch(a, 'Claude', 'Manual prompt')
    assert project(client)['live'] is True and project(client)['status']['kind'] == 'quiet'
    store.claim_dispatch(d['id']);store.finish_dispatch(d['id'], True, '')
    with store.connect(write=True) as con:
        con.execute("UPDATE dispatches SET requested_at='2029-01-01T00:00:00+00:00' WHERE id=?", (d['id'],))
    add_turn(store, a, 'Claude', 2, '<heartbeat>\nInfrastructure reply')
    assert project(client)['live'] is False
    add_turn(store, a, 'Claude', 3, 'New science.\nNext: Codex')
    assert project(client)['status']['kind'] == 'activity'
    assert project(client)['last_activity_at'].startswith('2030-01-01T00:03')
    post(client, '/api/projects/earth/seen')
    add_turn(store, b, 'Codex', 4, 'New independent turn.\nNext: Claude')
    assert project(client)['status']['kind'] == 'activity'


def test_snapshot_keeps_arrivals_after_capture_and_seen_does_not_regress(board, monkeypatch):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'First.\nNext: Codex')
    original = store.mark_project_seen
    def append_after_snapshot(project_id, activity):
        add_turn(store, a, 'Claude', 2, 'Arrived after snapshot.\nNext: Codex')
        return original(project_id, activity)
    monkeypatch.setattr(store, 'mark_project_seen', append_after_snapshot)
    post(client, '/api/projects/earth/seen')
    assert project(client)['status']['kind'] == 'activity'
    monkeypatch.setattr(store, 'mark_project_seen', original)
    newer = attention.activity_snapshots(store, read_turns(store.directory))['earth']
    older = {'turns': [attention.event_key(read_turns(store.directory)[1])], 'stops': {}}
    original('earth', newer);original('earth', older)
    assert project(client)['status']['kind'] == 'quiet'


def test_archived_sections_never_mutate_or_show_active_status(board):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'Choose.\nNext: User — decision needed: A?')
    p = project(client)
    archived = store.archive_project('earth', p['state_token'])
    archived_id = archived['id']
    assert client.get('/api/projects').json() == []
    archived_listing = client.get('/api/projects?archived=true').json()
    assert 'status' not in archived_listing[0] and 'live' not in archived_listing[0]
    assert post(client, f'/api/projects/{archived_id}/seen').status_code == 404
    assert client.get(f'/api/pairs/{a}/attention').status_code == 404
    assert post(client, f'/api/pairs/{a}/attention/handle', {'key': 'old'}).status_code == 404


def test_global_mark_all_acknowledges_turns_stops_without_handling_decisions(board):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'Done.\nNext: Codex')
    store.start_auto(b, 2, 80);store.stop_auto(b)
    assert project(client)['live']
    assert post(client, '/api/read-all').status_code == 200
    assert project(client)['live'] is False
    add_turn(store, a, 'Codex', 2, 'Choose.\nNext: User — decision needed: A?')
    post(client, '/api/read-all')
    assert project(client)['status']['kind'] == 'decision'


def test_auto_stops_only_pair_with_unresolved_request_even_after_coagent_reply(board):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'Choose.\nNext: User — decision needed: A?')
    add_turn(store, a, 'Codex', 2, 'Other work.\nNext: Claude')
    add_turn(store, b, 'Claude', 3, 'Ready.\nNext: Codex')
    store.start_auto(a, 2, 80);store.start_auto(b, 2, 80)
    results = autocontinue.step(store, read_turns(store.directory))
    assert not store.auto_continue(a)['running'] and 'needs your decision: A?' in store.auto_continue(a)['stopped_reason']
    assert store.auto_continue(b)['running'] and store.list_dispatches(b)[0]['target'] == 'Codex'
    assert store.list_dispatches(a) == []
    assert any('sent dispatch' in r for r in results)


def test_seen_generation_does_not_leak_into_replacement_section(board):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'Old section.\nNext: Codex')
    post(client, '/api/projects/earth/seen')
    assert project(client)['status']['kind'] == 'quiet'
    old = project(client)
    archived = store.archive_project('earth', old['state_token'])
    store.create_project('earth', 'Earth replacement')
    replacement = store.pairs('earth')[0]['id']
    store.set_pair_agent(replacement, 'Claude', f'claude-session-{a}')
    assert project(client)['status']['kind'] == 'activity'
    post(client, '/api/projects/earth/seen')
    restored = store.restore_project(archived['id'], archived['state_token'])
    assert restored['id'] != 'earth'
    statuses = client.get('/api/projects').json()
    assert next(p for p in statuses if p['id'] == 'earth')['status']['kind'] == 'quiet'


def test_decision_summary_preserves_scientific_punctuation_and_continue_instructions(board):
    from correspondence.turns import annotate, continue_prompts
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'Choose.\n**Next:** **Barry W. Brook** — decision needed: C_60 or C_61 (finite response)?')
    assert decisions(client, a)[0]['summary'] == 'C_60 or C_61 (finite response)?'
    assert declared_next('Next: Claude (review the draft)')['reason'] == 'review the draft'
    raw = read_turns(store.directory)[0]
    turn = annotate(raw, store.pairs(), store.pair_agents(), store.user_name())
    prompts = continue_prompts(turn, 'Earth', store.user_name())
    for variant in prompts.values():
        assert 'Next: Barry W. Brook — decision needed: <question>' in variant
        assert 'C_60 or C_61 (finite response)?' in variant


def test_same_second_later_event_is_unseen_and_new_request_is_not_handled(board):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 1, 'First.\nNext: User — decision needed: A?')
    first = decisions(client, a)[0]
    post(client, '/api/projects/earth/seen')
    post(client, f'/api/pairs/{a}/attention/handle', {'key': first['key']})
    old_snapshot = attention.activity_snapshots(store, read_turns(store.directory))['earth']
    assert project(client)['status']['kind'] == 'quiet'
    add_turn(store, a, 'Claude', 1, 'Second.\nNext: User — decision needed: B?')
    assert project(client)['status']['pairs'][0]['new_activity']
    second = decisions(client, a)[0]
    assert second['key'] != first['key'] and second['summary'] == 'B?'
    post(client, '/api/projects/earth/seen')
    store.mark_project_seen('earth', old_snapshot)
    assert not project(client)['status']['pairs'][0]['new_activity']
    assert decisions(client, a) == [second]


def test_event_identity_uses_turn_id_when_same_second_content_is_identical(board):
    client, store, a, b = board
    entry = add_turn(store, a, 'Claude', 1, 'Identical.\nNext: Codex')
    entry['turn_id'] = 'different-turn-id'
    post(client, '/api/projects/earth/seen')
    with (store.directory / 'turns.jsonl').open('a') as f:
        f.write(json.dumps(entry) + '\n')
    assert project(client)['status']['kind'] == 'activity'
