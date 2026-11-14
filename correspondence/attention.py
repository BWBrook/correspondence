"""Human attention comes from explicit requests, runs and unseen section activity."""
from __future__ import annotations

import hashlib
import json
import re

from .store import Conflict
from .turns import answered_at, annotate

DECISION_NEEDED = re.compile(r"^decision needed\s*:\s*(\S.*?)\s*$", re.IGNORECASE)


def event_key(turn):
    fields = ('agent', 'project', 'session_id', 'machine', 'recorded_at', 'turn_id', 'message')
    value = {field: turn.get(field) for field in fields}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def real_turns(turns):
    return [t for t in turns if not (t.get('message') or '').lstrip().startswith('<heartbeat>')]


def pair_decisions(store, pair, turns, *, state=None, pins=None, user=None):
    state = store.human_attention_state() if state is None else state
    pins = store.pair_agents() if pins is None else pins
    user = store.user_name() if user is None else user
    machines = store.pair_machines(pair['id'])
    latest = {}
    for raw in real_turns(turns):
        if raw['project'] != pair['project_id'] or raw['agent'] in latest:
            continue
        turn = annotate(raw, [pair], pins, user)
        if turn['pair_id'] == pair['id'] and turn.get('machine') == machines.get(turn['agent']):
            latest[turn['agent']] = turn
    decisions = []
    for agent, turn in latest.items():
        declared = turn['next'] or {}
        match = DECISION_NEEDED.match(declared.get('reason') or '') if declared.get('agent') == 'User' else None
        key = event_key(turn)
        if match and state['handled'].get(f"{pair['id']}.{agent}") != key:
            decisions.append({'key': key, 'agent': agent, 'summary': match.group(1), 'recorded_at': turn['recorded_at']})
    return decisions


def activity_snapshots(store, turns):
    state = store.human_attention_state()
    snapshots = {p['project_id']: {'turns': [], 'stops': {}} for p in store.pairs()}
    for turn in real_turns(turns):
        if turn['project'] in snapshots and turn.get('session_id'):
            seen = snapshots[turn['project']]['turns']
            seen.append(event_key(turn))
    for pair in store.pairs():
        stopped = state['runs'].get(pair['id'], {}).get('stopped_at')
        if stopped:
            snapshots[pair['project_id']]['stops'][str(pair['id'])] = stopped
    return snapshots


def acknowledge_section(store, project_id, turns):
    store.pairs(project_id)
    snapshot = activity_snapshots(store, turns).get(project_id, {'turns': [], 'stops': {}})
    return store.mark_project_seen(project_id, snapshot)


def handle_decision(store, pair_id, key, turns):
    pair = store.pair(pair_id)
    decision = next((d for d in pair_decisions(store, pair, turns) if d['key'] == key), None)
    if decision is None:
        raise Conflict('That decision request is no longer current; refresh before handling it')
    store.handle_decision(pair_id, decision['agent'], key)
    return {'decisions': pair_decisions(store, pair, turns)}


def project_statuses(store, projects, turns):
    state, pairs, pins, user = store.human_attention_state(), store.pairs(), store.pair_agents(), store.user_name()
    history, turns = turns, real_turns(turns)
    by_project = {}
    generations = {}
    for pair in pairs:
        generations.setdefault(pair['project_id'], str(pair['id']))
    latest_activity = {}
    for pair in pairs:
        run = state['runs'].get(pair['id'], {})
        seen = state['seen'].get(generations[pair['project_id']], {})
        seen_turns, seen_stops = set(seen.get('turns', [])), seen.get('stops', {})
        machines = store.pair_machines(pair['id'])
        members = [t for t in turns if t['project'] == pair['project_id']
                   and pins.get(pair['id'], {}).get(t['agent']) == t['session_id']
                   and machines.get(t['agent']) == t.get('machine')]
        new_turn = any(event_key(t) not in seen_turns for t in members)
        stopped = run.get('stopped_at')
        new_stop = bool(stopped and stopped > seen_stops.get(str(pair['id']), ''))
        pending = any(d['pair_id'] == pair['id'] and pins.get(pair['id'], {}).get(d['target']) == d['session_id']
                      and machines.get(d['target']) == d.get('machine')
                      and (d['status'] == 'queued' or not answered_at(history, d)) for d in state['dispatches'])
        status = {'id': pair['id'], 'name': pair['name'], 'running': bool(run and stopped is None),
                  'decisions': pair_decisions(store, pair, turns, state=state, pins=pins, user=user),
                  'pending': pending, 'new_activity': new_turn or new_stop}
        by_project.setdefault(pair['project_id'], []).append(status)
        times = [t['recorded_at'] for t in members] + [run.get('started_at'), stopped]
        latest_activity[pair['project_id']] = max([latest_activity.get(pair['project_id'], '')] + [t for t in times if t])
    for project in projects:
        statuses = by_project.get(project['id'], [])
        decisions = [d for p in statuses for d in p['decisions']]
        running = any(p['running'] for p in statuses)
        pending = any(p['pending'] for p in statuses)
        changed = project['unread_count'] > 0 or any(p['new_activity'] for p in statuses)
        kind = 'decision' if decisions else 'running' if running else 'activity' if changed else 'quiet'
        label = (f"{len(decisions)} decision request{'s' if len(decisions) != 1 else ''} {'need' if len(decisions) != 1 else 'needs'} your input" if decisions else
                 'Auto-continue active' if running else 'New activity; awaiting a reply' if changed and pending else
                 'New activity; exchange paused' if changed else
                 'Prompt awaiting a reply' if pending else 'Quiet')
        project['status'] = {'kind': kind, 'label': label, 'pairs': statuses}
        project['live'] = bool(decisions or running or changed or pending)
        project['last_activity_at'] = max(project.get('last_activity_at') or '', latest_activity.get(project['id'], '')) or None
    return projects
