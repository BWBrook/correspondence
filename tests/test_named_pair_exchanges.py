import json
import stat
from datetime import datetime,timedelta,timezone

import pytest
from fastapi.testclient import TestClient

from correspondence import autocontinue,web
from correspondence.store import Store
from correspondence.turns import read_turns

HEADERS={'X-Correspondence-Client':'browser','Origin':'http://127.0.0.1:8765'}


@pytest.fixture
def exchanges(tmp_path,monkeypatch):
    fake=tmp_path/'queue'
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{tmp_path}/queue-args"\necho Queued\n')
    fake.chmod(fake.stat().st_mode|stat.S_IEXEC)
    monkeypatch.setenv('CORRESPONDENCE_CODEX',str(fake))
    monkeypatch.setattr(web,'AUTO_SECONDS',3600)
    store=Store(tmp_path);store.create_project('terra','Terra Prisca')
    a=store.pairs('terra')[0];b=store.create_pair('terra','Budyko–Sellers')
    store.update_pair(a['id'],name='Main / haze-world')
    sessions={a['id']:{'Claude':'claude-main-lead','Codex':'codex-main-lead'},b['id']:{'Claude':'claude-budyko-lead','Codex':'codex-budyko-lead'}}
    base=datetime.now(timezone.utc)+timedelta(seconds=10)
    def turn(pair,agent,text,offset=0,session=None):
        entry={'project':'terra','agent':agent,'session_id':session or sessions[pair][agent],
               'recorded_at':(base+timedelta(seconds=offset)).isoformat(),'message':text,'cwd':'/same/registered/cwd'}
        with (tmp_path/'turns.jsonl').open('a') as f:f.write(json.dumps(entry)+'\n')
    for pair in sessions:
        for agent in ('Claude','Codex'):turn(pair,agent,'Registered.',-100)
    with TestClient(web.create_app(tmp_path),base_url='http://127.0.0.1:8765') as client:
        for pair,agents in sessions.items():
            for agent,session in agents.items():
                assert client.post(f'/api/pairs/{pair}/agents',headers=HEADERS,json={"expected_state": client.app.state.store.pair(pair)["state_token"], 'agent':agent,'session_id':session}).status_code==200
        yield store,client,a['id'],b['id'],sessions,turn,tmp_path


def test_interleaved_same_section_exchanges_use_only_their_pair(exchanges):
    store,client,a,b,sessions,turn,tmp=exchanges
    turn(a,'Claude','HAZE ONLY\nNext: Codex',0)
    turn(b,'Claude','BUDYKO ONLY\nNext: Codex',1)
    for pair in (a,b):
        response=client.post(f'/api/pairs/{pair}/auto',headers=HEADERS,json={"expected_state": client.app.state.store.pair(pair)["state_token"], 'hops':2,'ceiling':80})
        assert response.status_code==200 and response.json()['hops_left']==1
    da=client.get(f'/api/dispatches?pair_id={a}').json();db=client.get(f'/api/dispatches?pair_id={b}').json()
    assert len(da)==len(db)==1 and da[0]['id']!=db[0]['id']
    assert da[0]['session_id']==sessions[a]['Codex'] and db[0]['session_id']==sessions[b]['Codex']
    assert 'HAZE ONLY' in da[0]['body'] and 'BUDYKO ONLY' not in da[0]['body']
    assert 'BUDYKO ONLY' in db[0]['body'] and 'HAZE ONLY' not in db[0]['body']
    queued=(tmp/'queue-args').read_text();assert sessions[a]['Codex'] in queued and sessions[b]['Codex'] in queued
    turn(b,'Codex','BUDYKO RESPONSE\nNext: User — my pair is done',2)
    turn(a,'Codex','UNPINNED ADVISOR\nNext: Claude',3,session='unassigned-codex-advisor')
    autocontinue.step(store,read_turns(store.directory))
    assert store.auto_continue(a)['running'] and not store.auto_continue(b)['running']
    assert len(store.list_dispatches(a))==1
    assert client.get(f'/api/dispatches?pair_id={a}').json()[0]['answered_at'] is None
    assert client.get(f'/api/dispatches?pair_id={b}').json()[0]['answered_at'] is not None
    ta=client.get(f'/api/turns?project_id=terra&pair_id={a}').json()
    assert all(t['session_id'] in sessions[a].values() for t in ta)
    assert 'BUDYKO RESPONSE' not in json.dumps(ta) and 'UNPINNED ADVISOR' not in json.dumps(ta)


def test_stop_cancel_and_pin_changes_do_not_touch_the_other_pair(exchanges):
    store,client,a,b,sessions,turn,tmp=exchanges
    for pair in (a,b):
        turn(pair,'Codex',f'Finished {pair}\nNext: Claude')
        assert client.post(f'/api/pairs/{pair}/auto',headers=HEADERS,json={"expected_state": client.app.state.store.pair(pair)["state_token"], 'hops':3,'ceiling':80}).status_code==200
    da=store.list_dispatches(a)[0];db=store.list_dispatches(b)[0]
    assert da['status']==db['status']=='queued'
    assert store.claim_dispatch(db['id'])
    client.post(f'/api/pairs/{a}/auto/stop',headers=HEADERS,json={}).raise_for_status()
    assert store.list_dispatches(a)[0]['status']=='failed'
    other=store.list_dispatches(b)[0]
    assert other['status']=='queued' and other['claimed_at'] and store.auto_continue(b)['running']
    duplicate=client.post(f'/api/pairs/{a}/agents',headers=HEADERS,json={"expected_state": client.app.state.store.pair(a)["state_token"], 'agent':'Claude','session_id':sessions[b]['Claude']})
    assert duplicate.status_code==409
    assert store.pair_agents(a)['Claude']==sessions[a]['Claude']
    assert store.pair_agents(b)['Claude']==sessions[b]['Claude']
    assert store.list_dispatches(b)[0]==other


def test_discussion_links_and_section_boundaries_are_explicit(exchanges):
    store,client,a,b,sessions,turn,tmp=exchanges
    ta=store.create_thread('User','terra','Haze','Start')['id']
    tb=store.create_thread('User','terra','Budyko','Start')['id']
    store.update_pair(a,discussion_ids=[ta]);store.update_pair(b,discussion_ids=[tb])
    now=datetime.now(timezone.utc)
    for pair in (a,b):turn(pair,'Claude','Earlier',-30)
    store.post_message('Claude',ta,'Haze post');store.post_message('Claude',tb,'Budyko post')
    for pair in (a,b):turn(pair,'Claude','Later\nNext: Codex',30)
    for pair,thread in ((a,ta),(b,tb)):
        listed=client.get(f'/api/turns?pair_id={pair}').json()
        assert [d['thread_id'] for d in listed[0]['discussions']]==[thread]
    store.update_pair(a,discussion_ids=[])
    assert client.get(f'/api/turns?pair_id={a}').json()[0]['discussions']==[]
    store.create_project('other','Other')
    foreign=store.create_thread('User','other','Foreign','Start')['id']
    assert client.post(f'/api/pairs/{a}',headers=HEADERS,json={"expected_state": client.app.state.store.pair(a)["state_token"], 'discussion_ids':[foreign]}).status_code==422
    assert client.get(f'/api/turns?project_id=other&pair_id={a}').status_code==404


def test_shared_account_limit_stops_both_runs_without_mixing_prompts(exchanges):
    store,client,a,b,sessions,turn,tmp=exchanges
    for pair in (a,b):
        turn(pair,'Claude','Review\nNext: Codex')
        store.start_auto(pair,3,80)
    store.set_limits('Claude',{'read_at':datetime.now(timezone.utc).isoformat(),'windows':[{'label':'Weekly limit','percent':81,'resets_at':None}]})
    autocontinue.step(store,read_turns(store.directory))
    assert not store.auto_continue(a)['running'] and not store.auto_continue(b)['running']
    assert not store.list_dispatches(a) and not store.list_dispatches(b)


def test_a_turn_without_a_session_cannot_become_a_main_turn(exchanges):
    from correspondence.turns import latest_main_turn
    store,client,a,b,sessions,turn,tmp=exchanges
    store.set_pair_agent(a,'Claude',None)
    phantom={'project':'terra','agent':'Claude','session_id':None,'recorded_at':'2099-01-01T00:00:00+00:00','message':'Next: Codex'}
    assert latest_main_turn([phantom],store.pair(a),store.pair_agents()) is None


def test_worker_rejects_a_source_moved_into_the_other_pair(exchanges):
    store,client,a,b,sessions,turn,tmp=exchanges
    turn(a,'Claude','OLD SOURCE NOW IN THE OTHER PAIR\nNext: Codex')
    stale_pins=store.pair_agents()
    run=store.start_auto(a,3,80)
    run.update(project_id='terra',name='Terra Prisca',pair_name='Main / haze-world')
    old_source=sessions[a]['Claude']
    store.set_pair_agent(a,'Claude','replacement-claude-main')
    store.set_pair_agent(b,'Claude',old_source)
    assert autocontinue.advance(store,run,read_turns(store.directory),stale_pins,'User',{'Claude':None,'Codex':None}) is None
    assert not store.list_dispatches(a) and not (tmp/'queue-args').exists()
    assert store.auto_continue(a)['hops_left']==3
