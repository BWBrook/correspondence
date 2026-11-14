"""Pair retirement preserves history while removing the exchange from active control."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from correspondence import attention, autocontinue
from correspondence.store import Conflict, NotFound, SCHEMA_VERSION, Store
from correspondence.turns import read_turns
from correspondence.web import create_app

HEADERS = {'X-Correspondence-Client': 'browser', 'Origin': 'http://127.0.0.1:8765'}


@pytest.fixture
def board(tmp_path):
    store = Store(tmp_path)
    store.set_user_name('Barry')
    store.create_project('earth', 'Earth')
    a = store.pairs('earth')[0]['id']
    b = store.create_pair('earth', 'Budyko–Sellers')['id']
    for pair, label in ((a, 'main'), (b, 'budyko')):
        for agent in ('Claude', 'Codex'):
            store.set_pair_agent(pair, agent, f'{agent.lower()}-{label}-session', 'wsl')
            add_turn(store, pair, agent, f'{label}: finished.\nNext: Barry')
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8765') as client:
        yield client, store, a, b


def add_turn(store, pair, agent, message):
    entry = {'project': 'earth', 'agent': agent, 'session_id': store.pair_agents(pair)[agent],
             'machine': 'wsl', 'recorded_at': '2026-11-14T00:00:00+00:00', 'message': message}
    with (store.directory / 'turns.jsonl').open('a') as f:
        f.write(json.dumps(entry) + '\n')


def archive(store, pair):
    return store.archive_pair(pair, store.pair(pair)['state_token'])


def post(client, path, body=None):
    return client.post('/api' + path, json=body or {}, headers=HEADERS)


def test_archive_stops_only_its_run_and_preserves_configuration_history(board):
    client, store, a, b = board
    thread = store.create_thread('Claude', 'earth', 'Joint discussion', 'Opening')['id']
    store.update_pair(a, discussion_ids=[thread])
    pins = store.pair_agents(a)
    ar, br = store.start_auto(a, 4, 80), store.start_auto(b, 5, 80)
    auto = store.create_dispatch(a, 'Claude', 'Auto', run=ar['started_at'], after_turn='a-turn')
    manual = store.create_dispatch(a, 'Codex', 'Manual')
    peer = store.create_dispatch(b, 'Claude', 'Peer')
    archived = archive(store, a)
    assert archived['archived_at'] and archived['discussion_ids'] == [thread]
    assert store.pair_agents(a, archived=True) == pins
    assert store.pair_machines(a, archived=True) == {'Claude': 'wsl', 'Codex': 'wsl'}
    assert {p['id'] for p in store.pairs('earth')} == {b}
    assert store.pairs('earth', archived=True) == [archived]
    history = store.list_dispatches(a, archived=True)
    assert {d['id'] for d in history} == {auto['id'], manual['id']}
    assert all(d['status'] == 'failed' and d['detail'] == 'Cancelled: pair archived' for d in history)
    assert store.list_dispatches(b)[0]['id'] == peer['id'] and store.list_dispatches(b)[0]['status'] == 'queued'
    assert store.auto_continue(b)['running']
    assert [r['pair_id'] for r in store.running_autos()] == [b]
    assert client.get(f'/api/threads/{thread}').status_code == 200
    restored = store.restore_pair(a, archived['state_token'])
    assert not restored['archived_at'] and restored['discussion_ids'] == [thread]
    assert restored['state_token'] != archived['state_token']
    assert not store.auto_continue(a)['running']
    assert store.auto_continue(a)['stopped_reason'] == 'The pair was archived'
    assert all(d['status'] == 'failed' for d in store.list_dispatches(a))


def test_claimed_work_finishes_but_archive_prevents_new_claims(board):
    client, store, a, b = board
    claimed = store.create_dispatch(a, 'Claude', 'Already picked up')
    waiting = store.create_dispatch(a, 'Codex', 'Not yet picked up')
    assert store.claim_dispatch(claimed['id'], 'wsl')
    archive(store, a)
    assert not store.claim_dispatch(waiting['id'], 'wsl')
    assert store.waiting_dispatches('Claude', 'wsl') == []
    assert store.waiting_dispatches('Codex', 'wsl') == []
    done = store.finish_dispatch(claimed['id'], True, 'Completed after archive', machine='wsl')
    assert done['status'] == 'delivered'
    with pytest.raises(NotFound):
        store.cancel_dispatch(claimed['id'])
    assert store.list_dispatches(a, archived=True)[1]['detail'] == 'Completed after archive'


def test_archived_pair_is_read_only_and_lifecycle_tokens_reject_stale_actions(board):
    client, store, a, b = board
    old = store.pair(a)
    store.update_pair(a, name='Renamed main')
    with pytest.raises(Conflict):
        store.archive_pair(a, old['state_token'])
    current = store.pair(a)
    store.set_pair_agent(a, 'Claude', 'claude-replacement-session', 'wsl')
    with pytest.raises(Conflict):
        store.archive_pair(a, current['state_token'])
    retired = archive(store, a)
    for action in (
        lambda: store.update_pair(a, name='Cannot change'),
        lambda: store.set_pair_agent(a, 'Claude', None),
        lambda: store.start_auto(a, 2, 80),
        lambda: store.stop_auto(a),
        lambda: store.handle_decision(a, 'Claude', 'request'),
        lambda: store.create_dispatch(a, 'Codex', 'Stale prompt', expected_pins=store.pair_agents(a, archived=True)),
    ):
        with pytest.raises(NotFound):
            action()
    assert post(client, f'/pairs/{a}/archive', {'expected_state': old['state_token']}).status_code == 409
    assert post(client, f'/pairs/{a}/attention/handle', {'key': 'request'}).status_code == 404
    restored = store.restore_pair(a, retired['state_token'])
    assert post(client, f'/pairs/{a}/archive', {'expected_state': retired['state_token']}).status_code == 409
    assert post(client, f'/pairs/{a}/restore', {'expected_state': restored['state_token']}).status_code == 409


def test_names_and_session_membership_remain_reserved(board):
    client, store, a, b = board
    archive(store, a)
    with pytest.raises(Conflict, match='archived pair; restore'):
        store.create_pair('earth', 'main')
    with pytest.raises(Conflict):
        store.update_pair(b, name='Main')
    with pytest.raises(Conflict):
        store.set_pair_agent(b, 'Codex', 'codex-main-session', 'wsl')
    sessions = client.get(f'/api/pairs/{b}/agents').json()['sessions']
    retired = next(s for s in sessions if s['session_id'] == 'codex-main-session')
    assert retired['pair_id'] == a and retired['pair_archived']
    assert retired['pair_name'] == 'Main (archived)'


def test_history_is_explicitly_scoped_and_read_only(board):
    client, store, a, b = board
    dispatch = store.create_dispatch(a, 'Claude', 'Old prompt')
    retired = archive(store, a)
    assert client.get(f'/api/turns?pair_id={a}').status_code == 404
    assert client.get(f'/api/dispatches?pair_id={a}').status_code == 404
    assert client.get(f'/api/pairs/{a}/agents').status_code == 404
    turns = client.get(f'/api/turns?project_id=earth&pair_id={a}&archived=true').json()
    assert len(turns) == 2 and all(t['pair_id'] == a and 'continue_prompts' not in t for t in turns)
    assert client.get(f'/api/dispatches?pair_id={a}&archived=true').json()[0]['id'] == dispatch['id']
    assert client.get(f'/api/pairs/{a}/agents?archived=true').json()['pins'] == store.pair_agents(a, archived=True)
    assert client.get(f'/api/turns?pair_id={b}&archived=true').status_code == 404
    assert client.get(f'/api/dispatches?pair_id={b}&archived=true').status_code == 404
    assert client.get(f'/api/pairs/{b}/agents?archived=true').status_code == 404
    assert client.get('/api/turns?archived=true').status_code == 422
    assert all(t['pair_id'] == b for t in client.get('/api/turns').json())
    assert all(t['pair_id'] == b for t in client.get('/api/turns?project_id=earth').json())
    assert store.pair(a, archived=True) == retired


def test_archiving_every_pair_keeps_section_and_history_without_unassigned_turns(board):
    client, store, a, b = board
    archive(store, a)
    retired = archive(store, b)
    assert store.pairs('earth') == []
    assert client.get('/api/turns').json() == []
    assert client.get('/api/turns?project_id=earth').json() == []
    assert len(client.get('/api/projects/earth/pairs?archived=true').json()) == 2
    assert store.list_projects('User')[0]['id'] == 'earth'
    new = store.create_pair('earth', 'Next experiment')
    assert store.pairs('earth') == [new]
    restored = store.restore_pair(b, retired['state_token'])
    assert {p['id'] for p in store.pairs('earth')} == {new['id'], restored['id']}


def test_archived_decision_and_run_no_longer_affect_project_status(board):
    client, store, a, b = board
    add_turn(store, a, 'Claude', 'Question.\nNext: Barry — decision needed: Pick A or B.')
    store.start_auto(b, 3, 80)
    assert client.get('/api/projects').json()[0]['status']['kind'] == 'decision'
    archive(store, a)
    project = client.get('/api/projects').json()[0]
    assert project['status']['kind'] == 'running'
    assert [p['id'] for p in project['status']['pairs']] == [b]
    assert all(d['pair_id'] == b for d in store.machine_pins('wsl'))
    archive(store, b)
    project = client.get('/api/projects').json()[0]
    assert project['status']['kind'] == 'quiet' and not project['live']


def test_archiving_original_pair_keeps_seen_generation_stable(board):
    client, store, a, b = board
    attention.acknowledge_section(store, 'earth', read_turns(store.directory))
    assert client.get('/api/projects').json()[0]['status']['kind'] == 'quiet'
    before = store.human_attention_state()['seen']
    archive(store, a)
    assert client.get('/api/projects').json()[0]['status']['kind'] == 'quiet'
    assert store.human_attention_state()['seen'] == before
    attention.acknowledge_section(store, 'earth', read_turns(store.directory))
    assert list(store.human_attention_state()['seen']) == [str(a)]


def test_whole_section_archive_hides_retired_pairs_and_invalidates_old_tokens(board):
    client, store, a, b = board
    retired = archive(store, a)
    project = store.archive_project('earth', store.list_projects('User')[0]['state_token'])
    assert client.get(f'/api/turns?pair_id={a}&archived=true').status_code == 404
    with pytest.raises(NotFound):
        store.pairs(project['id'], archived=True)
    restored = store.restore_project(project['id'], project['state_token'])
    assert restored['id'] == 'earth'
    with pytest.raises(Conflict):
        store.restore_pair(a, retired['state_token'])
    assert store.pairs('earth', archived=True)[0]['id'] == a
    assert [p['id'] for p in store.pairs('earth')] == [b]


def test_auto_snapshot_cannot_send_after_pair_archival(board, monkeypatch):
    client, store, a, b = board
    store.start_auto(a, 3, 80)
    original = store.running_autos
    def stale_runs():
        runs = original()
        archive(store, a)
        return runs
    monkeypatch.setattr(store, 'running_autos', stale_runs)
    assert autocontinue.step(store, read_turns(store.directory)) == []
    assert store.list_dispatches(a, archived=True) == []


def test_schema11_upgrade_preserves_every_existing_column_and_foreign_key(tmp_path):
    store = Store(tmp_path)
    store.create_project('earth', 'Earth')
    a = store.pairs('earth')[0]['id']
    b = store.create_pair('earth', 'Worktree')['id']
    message = store.create_thread('Claude', 'earth', 'History', 'Keep exactly', request_id='opening')['first_message_id']
    thread = store.get_message('User', message)['message']['thread_id']
    store.mark_read('Codex', thread, message)
    for pair in (a, b):
        store.set_pair_agent(pair, 'Claude', f'claude-history-{pair}')
        store.set_pair_agent(pair, 'Codex', f'codex-history-{pair}')
        store.start_auto(pair, 4, 80)
        store.create_dispatch(pair, 'Claude', f'Prompt {pair}')
    with sqlite3.connect(store.path) as con:
        con.execute('ALTER TABLE pairs DROP COLUMN archived_at')
        con.execute('ALTER TABLE pairs DROP COLUMN state_token')
        con.execute('PRAGMA user_version=11')
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        columns = {t: [r[1] for r in con.execute(f'PRAGMA table_info({t})')] for t in tables}
        before = {t: con.execute(f'SELECT * FROM {t} ORDER BY rowid').fetchall() for t in tables}
    upgraded = Store(tmp_path)
    with sqlite3.connect(upgraded.path) as con:
        for table in tables:
            names = ','.join(columns[table])
            assert con.execute(f'SELECT {names} FROM {table} ORDER BY rowid').fetchall() == before[table]
        assert con.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION == 12
        assert con.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    pairs = upgraded.pairs('earth')
    assert all(p['archived_at'] is None and len(p['state_token']) == 32 for p in pairs)
    assert len({p['state_token'] for p in pairs}) == 2
    assert Store(tmp_path).pairs() == pairs


def test_delayed_browser_actions_cannot_reactivate_or_modify_restored_pair(board):
    client, store, a, b = board
    original = store.pair(a)
    pins = store.pair_agents(a)
    retired = archive(store, a)
    restored = store.restore_pair(a, retired['state_token'])
    assert restored['state_token'] != original['state_token']
    delayed = (
        (f'/pairs/{a}', {'name': 'Delayed rename'}),
        (f'/pairs/{a}/agents', {'agent': 'Codex', 'session_id': None}),
        (f'/pairs/{a}/auto', {'hops': 3, 'ceiling': 80}),
        ('/dispatches', {'pair_id': a, 'expected_pins': pins, 'target': 'Codex', 'body': 'Delayed pre-archive prompt'}),
    )
    for path, payload in delayed:
        assert post(client, path, payload | {'expected_state': original['state_token']}).status_code == 409
        assert post(client, path, payload).status_code == 422
    assert store.pair_agents(a) == pins
    assert store.pair(a) == restored
    assert not store.auto_continue(a)['running']
    assert store.list_dispatches(a) == []
    assert post(client, f'/pairs/{a}', {'expected_state': restored['state_token'], 'name': 'Fresh intentional rename'}).status_code == 200
