"""Named pair isolation and preservation when upgrading the section-level schema."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest

from correspondence.store import Conflict, NotFound, SCHEMA_VERSION, Store


@pytest.fixture
def paired(tmp_path):
    store=Store(tmp_path);store.create_project('earth','Earth')
    a=store.pairs('earth')[0]['id'];b=store.create_pair('earth','Budyko–Sellers')['id']
    for pair,label in ((a,'main'),(b,'budyko')):
        store.set_pair_agent(pair,'Claude',f'claude-{label}-session')
        store.set_pair_agent(pair,'Codex',f'codex-{label}-session')
    return store,a,b


def test_pair_names_discussions_and_session_membership_are_explicit(paired):
    store,a,b=paired
    t=store.create_thread('Codex','earth','Climate','Opening')['id']
    assert store.update_pair(a,name='Haze-world',discussion_ids=[t,t])['discussion_ids']==[t]
    assert store.create_pair('earth','haze-WORLD',[t])['id']==a
    with pytest.raises(Conflict):store.create_pair('earth','HAZE-WORLD',[])
    with pytest.raises(Conflict):store.update_pair(b,name='haze-world')
    with pytest.raises(Conflict):store.set_pair_agent(b,'Codex','codex-main-session')
    assert store.pair_agents(b)['Codex']=='codex-budyko-session'
    store.create_project('other','Other')
    foreign=store.create_thread('Claude','other','Elsewhere','Opening')['id']
    with pytest.raises(ValueError):store.update_pair(a,name='Would roll back',discussion_ids=[foreign])
    assert store.pair(a)['name']=='Haze-world'
    with pytest.raises(ValueError):store.pair('earth')


def test_duplicate_pin_is_rejected_under_concurrent_writers(paired):
    store,a,b=paired
    store.set_pair_agent(a,'Codex',None);store.set_pair_agent(b,'Codex',None)
    barrier=Barrier(2)
    def pin(pair):
        barrier.wait()
        try:store.set_pair_agent(pair,'Codex','one-shared-session');return True
        except Conflict:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(pin,(a,b)))==[False,True]
    assert sum(pins.get('Codex')=='one-shared-session' for pins in store.pair_agents().values())==1


def test_cooldown_dispatches_failures_and_stop_are_pair_scoped(paired):
    store,a,b=paired
    ar=store.start_auto(a,3,80);br=store.start_auto(b,5,80)
    da=store.create_dispatch(a,'Codex','Main prompt',after_turn='main-turn',run=ar['started_at'])
    db=store.create_dispatch(b,'Codex','Budyko prompt',after_turn='budyko-turn',run=br['started_at'])
    assert (da['session_id'],db['session_id'])==('codex-main-session','codex-budyko-session')
    assert da['project_id']==db['project_id']=='earth' and da['pair_id']!=db['pair_id']
    assert store.auto_continue(a)['hops_left']==2 and store.auto_continue(b)['hops_left']==4
    assert store.prompted(a,'main-turn') and not store.prompted(b,'main-turn')
    store.stop_auto(a)
    assert store.list_dispatches(a)[0]['status']=='failed'
    assert store.list_dispatches(b)[0]['status']=='queued' and store.auto_continue(b)['running']
    assert store.failed_since(b,ar['started_at']) is None
    assert store.failed_since(a,ar['started_at'])['id']==da['id']
    assert len(store.running_autos())==1 and store.running_autos()[0]['pair_name']=='Budyko–Sellers'


def test_repin_cancel_and_machine_pins_stay_with_their_pair(paired):
    store,a,b=paired
    store.set_pair_agent(a,'Codex','codex-main-session','wsl')
    store.set_pair_agent(b,'Codex','codex-budyko-session','wsl')
    da=store.create_dispatch(a,'Codex','A');db=store.create_dispatch(b,'Codex','B')
    assert {(r['pair_id'],r['session_id']) for r in store.machine_pins('wsl')}=={(a,'codex-main-session'),(b,'codex-budyko-session')}
    store.set_pair_agent(a,'Codex','codex-new-main-session','wsl')
    assert store.list_dispatches(a)[0]['status']=='failed' and store.list_dispatches(b)[0]['status']=='queued'
    store.claim_dispatch(db['id'],'wsl');store.finish_dispatch(db['id'],True,'Delivered',machine='wsl')
    assert store.list_dispatches(b)[0]['status']=='delivered'
    # The delivered prompt's cooldown does not affect its peer pair.
    assert store.create_dispatch(a,'Codex','Another main prompt')['session_id']=='codex-new-main-session'


def test_archiving_and_restoring_carries_all_pairs_and_stops_their_runs(paired):
    store,a,b=paired
    for p in (a,b):store.start_auto(p,2,80);store.create_dispatch(p,'Claude','Waiting')
    token=store.list_projects('User')[0]['state_token']
    archived=store.archive_project('earth',token)
    with pytest.raises(NotFound):store.pair(a)
    assert store.pair_agents()=={} and store.running_autos()==[] and store.machine_pins('wsl')==[]
    restored=store.restore_project(archived['id'],archived['state_token'])
    assert restored['id']=='earth'
    assert {p['id'] for p in store.pairs('earth')}=={a,b}
    for p in (a,b):assert not store.auto_continue(p)['running'] and store.list_dispatches(p)[0]['status']=='failed'
    with store.connect() as con:assert not con.execute('PRAGMA foreign_key_check').fetchall()


def legacy_v10(tmp_path):
    legacy=tmp_path/'legacy';legacy.mkdir()
    old=Store.__new__(Store);old.directory=legacy;old.path=legacy/'board.sqlite3'
    with old.connect(transaction=False) as con:
        con.executescript('''
            CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,
                repo_path TEXT,created_at TEXT NOT NULL,archived_at TEXT,archived_original_id TEXT,state_token TEXT);
            CREATE TABLE threads(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
                title TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,thread_id INTEGER NOT NULL REFERENCES threads(id),
                author TEXT NOT NULL,model TEXT,body TEXT NOT NULL,kind TEXT NOT NULL,
                parent_id INTEGER REFERENCES messages(id),created_at TEXT NOT NULL);
            CREATE TABLE read_markers(author TEXT NOT NULL,thread_id INTEGER NOT NULL REFERENCES threads(id),
                message_id INTEGER NOT NULL REFERENCES messages(id),PRIMARY KEY(author,thread_id));
            CREATE TABLE requests(author TEXT NOT NULL,request_id TEXT NOT NULL,operation TEXT NOT NULL,
                fingerprint TEXT NOT NULL,result_id INTEGER NOT NULL,PRIMARY KEY(author,request_id));
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE project_agents(project_id TEXT NOT NULL REFERENCES projects(id),
                agent TEXT NOT NULL CHECK(agent IN ('Claude','Codex')),session_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,machine TEXT,PRIMARY KEY(project_id,agent));
            CREATE TABLE dispatches(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
                target TEXT NOT NULL CHECK(target IN ('Claude','Codex')),session_id TEXT NOT NULL,body TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','delivered','failed')),requested_at TEXT NOT NULL,
                delivered_at TEXT,detail TEXT,claimed_at TEXT,machine TEXT,origin TEXT,after_turn TEXT);
            CREATE INDEX dispatches_project ON dispatches(project_id,id);
            CREATE TABLE auto_continue(project_id TEXT PRIMARY KEY REFERENCES projects(id),
                hops_left INTEGER NOT NULL CHECK(hops_left>=0),ceiling INTEGER NOT NULL CHECK(ceiling BETWEEN 10 AND 100),
                started_at TEXT NOT NULL,stopped_at TEXT,stopped_reason TEXT,
                CHECK((stopped_at IS NULL)=(stopped_reason IS NULL)));
            CREATE TRIGGER messages_no_update BEFORE UPDATE ON messages
                BEGIN SELECT RAISE(ABORT,'Messages are append-only'); END;
            CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages
                BEGIN SELECT RAISE(ABORT,'Messages are append-only'); END;
            PRAGMA user_version=10;
        ''')
        stamp='2026-11-01T01:00:00+00:00'
        con.executemany('INSERT INTO projects VALUES (?,?,?,?,?,?,?,?)',[
            ('earth','Earth','Original description','/tmp/earth',stamp,None,None,'original-token'),
            ('other','Other','',None,stamp,None,None,'other-token')])
        con.execute('INSERT INTO threads VALUES (?,?,?,?,?,?)',(1,'earth','Science','open',stamp,stamp))
        con.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)',(1,1,'Codex','GPT-6','Unchanged words','note',None,stamp))
        con.execute('INSERT INTO read_markers VALUES (?,?,?)',('User',1,1))
        con.execute('INSERT INTO requests VALUES (?,?,?,?,?)',('Codex','one','create_thread','exact-existing-fingerprint',1))
        con.execute('INSERT INTO settings VALUES (?,?)',('user_name','Barry'))
        con.executemany('INSERT INTO project_agents VALUES (?,?,?,?,?)',[
            ('earth','Claude','claude-old-session',stamp,'wsl'),('earth','Codex','codex-old-session',stamp,None)])
        con.execute('INSERT INTO auto_continue VALUES (?,?,?,?,?,?)',('earth',4,85,stamp,None,None))
        for number,status,claim in ((11,'queued',None),(12,'queued','2026-11-01T01:00:01+00:00'),(13,'delivered','2026-11-01T01:00:02+00:00'),(14,'failed',None)):
            con.execute('''INSERT INTO dispatches(id,project_id,target,session_id,body,status,requested_at,delivered_at,detail,claimed_at,machine,origin,after_turn)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',(number,'earth','Codex','codex-old-session',f'Exact prompt {number}',status,stamp,'2026-11-01T01:00:03+00:00' if status=='delivered' else None,f'Detail {number}',claim,None,'auto',f'turn-{number}'))
    return old


def snapshot(store):
    with store.connect() as con:
        return {table:[dict(r) for r in con.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('projects','threads','messages','requests','read_markers','dispatches','project_agents','auto_continue')}


def test_v10_upgrade_preserves_every_record_and_existing_main_pairs(tmp_path):
    old=legacy_v10(tmp_path);before=snapshot(old)
    store=Store(old.directory)
    main=store.pairs('earth')[0]['id']
    assert store.pair_agents(main)=={'Claude':'claude-old-session','Codex':'codex-old-session'}
    assert store.pair_machines(main)=={'Claude':'wsl','Codex':None}
    with store.connect() as con:
        for table in ('projects','threads','messages','requests','read_markers'):
            assert [dict(r) for r in con.execute(f'SELECT * FROM {table} ORDER BY rowid')]==before[table]
        dispatches=[dict(r) for r in con.execute('SELECT * FROM dispatches ORDER BY id')]
        assert [{k:v for k,v in row.items() if k!='pair_id'} for row in dispatches]==before['dispatches']
        assert {r['pair_id'] for r in dispatches}=={main}
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='project_agents'").fetchone()
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
        assert con.execute('PRAGMA user_version').fetchone()[0]==SCHEMA_VERSION
    assert store.auto_continue(main)=={**{k:v for k,v in before['auto_continue'][0].items() if k!='project_id'},'pair_id':main,'running':True}
    assert len(store.pairs())==2
    reopened=Store(old.directory)
    assert reopened.pairs()==store.pairs() and reopened.list_dispatches(main)==dispatches[::-1]


def test_pair_upgrade_failure_rolls_back_all_changes(tmp_path,monkeypatch):
    old=legacy_v10(tmp_path);before=snapshot(old)
    original=Store._upgrade_pairs
    def broken(con):
        original(con)
        raise RuntimeError('simulated failure before commit')
    monkeypatch.setattr(Store,'_upgrade_pairs',staticmethod(broken))
    with pytest.raises(RuntimeError):Store(old.directory)
    assert snapshot(old)==before
    with old.connect() as con:
        assert con.execute('PRAGMA user_version').fetchone()[0]==10
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='pairs'").fetchone()


def test_pair_upgrade_refuses_orphaned_metadata_without_silently_dropping_it(tmp_path):
    old=legacy_v10(tmp_path)
    with sqlite3.connect(old.path) as con:
        con.execute("INSERT INTO project_agents VALUES ('missing','Claude','orphan-session','2026-01-01',NULL)")
    before=snapshot(old)
    with pytest.raises(sqlite3.IntegrityError,match='lose an existing pin'):Store(old.directory)
    assert snapshot(old)==before


@pytest.mark.parametrize('repinned',['Claude','Codex'])
def test_stale_pin_snapshot_cannot_spend_a_hop_or_send_to_a_reconfigured_pair(paired,repinned):
    store,a,b=paired
    expected=store.pair_agents(a)
    run=store.start_auto(a,4,80)
    store.set_pair_agent(a,repinned,f'new-{repinned.lower()}-session')
    if repinned=='Claude':
        store.set_pair_agent(b,'Claude',expected['Claude'])
    before=(store.auto_continue(a),store.auto_continue(b),store.list_dispatches(a),store.list_dispatches(b))
    with pytest.raises(Conflict,match='main sessions changed'):
        store.create_dispatch(a,'Codex','Stale source decision',after_turn='old-claude-turn',
                              run=run['started_at'],expected_pins=expected)
    assert (store.auto_continue(a),store.auto_continue(b),store.list_dispatches(a),store.list_dispatches(b))==before
    assert store.auto_continue(a)['hops_left']==4
    assert store.list_dispatches(a)==store.list_dispatches(b)==[]


@pytest.mark.parametrize('claimed',[False,True])
def test_repin_stops_only_its_pair_and_preserves_claimed_prompts(paired,claimed):
    store,a,b=paired
    ar=store.start_auto(a,3,80);br=store.start_auto(b,5,80)
    da=store.create_dispatch(a,'Codex','A',run=ar['started_at'])
    db=store.create_dispatch(b,'Codex','B',run=br['started_at'])
    if claimed:assert store.claim_dispatch(da['id'])
    store.set_pair_agent(a,'Claude','new-claude-main-session')
    assert not store.auto_continue(a)['running']
    assert store.auto_continue(a)['stopped_reason']=='A main session changed'
    assert store.auto_continue(a)['hops_left']==2
    assert store.list_dispatches(a)[0]['status']==('queued' if claimed else 'failed')
    assert store.auto_continue(b)['running'] and store.auto_continue(b)['hops_left']==4
    assert store.list_dispatches(b)[0]['id']==db['id'] and store.list_dispatches(b)[0]['status']=='queued'


def test_reapplying_the_same_pin_keeps_the_run_but_a_machine_change_stops_it(paired):
    store,a,b=paired
    before=store.start_auto(a,2,80)
    store.set_pair_agent(a,'Claude','claude-main-session')
    assert store.auto_continue(a)==before
    store.set_pair_agent(a,'Claude','claude-main-session','wsl')
    assert not store.auto_continue(a)['running'] and store.auto_continue(a)['hops_left']==2
