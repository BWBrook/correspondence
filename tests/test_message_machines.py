import json
import sqlite3

import pytest

from correspondence.store import Conflict, Store, author_label


def rows(con, table, columns):
    names = ','.join(f'"{name}"' for name in columns)
    order = ','.join(str(i + 1) for i in range(len(columns)))
    return con.execute(f'SELECT {names} FROM "{table}" ORDER BY {order}').fetchall()


def test_schema_12_upgrade_preserves_every_original_column_and_leaves_old_machines_null(tmp_path):
    before = Store(tmp_path)
    before.set_user_name('Barry')
    before.create_project('p', 'Project')
    thread = before.create_thread('Claude', 'p', 'Historical', 'Old opening', request_id='opening')
    old_post = before.post_message('Codex', thread['id'], 'Old reply', request_id='reply')
    before.mark_read('User', thread['id'], old_post['id'])
    with sqlite3.connect(before.path) as con:
        con.execute('ALTER TABLE messages DROP COLUMN machine')
        con.execute('PRAGMA user_version=12')
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        snapshots = {}
        for table in tables:
            columns = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            snapshots[table] = columns, rows(con, table, columns)
    after = Store(tmp_path, machine='mini')
    with sqlite3.connect(after.path) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 14
        assert con.execute('SELECT machine FROM messages').fetchall() == [(None,), (None,)]
        for table, (columns, original) in snapshots.items():
            assert rows(con, table, columns) == original
        assert con.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    historical = after.read_thread('User', thread['id'])['messages']
    assert [m['author_label'] for m in historical] == ['Claude', 'Codex']
    new = after.post_message('Claude', thread['id'], 'New reply')
    assert (new['machine'], new['author_label']) == ('mini', 'Claude@mini')


def test_server_bound_stamp_applies_to_thread_reply_and_status_but_never_human(tmp_path):
    store = Store(tmp_path, machine='mini')
    store.set_user_name('Barry')
    store.create_project('p', 'Project')
    created = store.create_thread('Claude', 'p', 'Stamped', 'Opening')
    assert (created['last_author'], created['last_machine'], created['last_author_label']) == ('Claude', 'mini', 'Claude@mini')
    reply = store.post_message('Codex', created['id'], 'Review')
    assert (reply['author'], reply['machine'], reply['author_label']) == ('Codex', 'mini', 'Codex@mini')
    changed = store.set_thread_status('Claude', created['id'], 'resolved')
    assert changed['last_author_label'] == 'Claude@mini'
    assert store.read_thread('User', created['id'])['messages'][-1]['machine'] == 'mini'
    human = store.post_message('User', created['id'], 'Approved in chat')
    assert (human['author'], human['machine'], human['author_label']) == ('User', None, 'Barry')
    human_thread = store.create_thread('User', 'p', 'Human', 'Human opening')
    assert (human_thread['last_machine'], human_thread['last_author_label']) == (None, 'Barry')
    store.set_thread_status('User', human_thread['id'], 'resolved')
    assert store.read_thread('Claude', human_thread['id'])['messages'][-1]['machine'] is None
    with pytest.raises(TypeError):
        store.post_message('Claude', created['id'], 'Spoof', machine='wsl')
    with pytest.raises(ValueError):
        Store(tmp_path, machine='mini\n@wsl')


def test_every_public_message_and_thread_read_exposes_stamp_without_changing_author(tmp_path):
    mini = Store(tmp_path, machine='mini')
    wsl = Store(tmp_path, machine='wsl')
    macbook = Store(tmp_path, machine='macbook')
    mini.create_project('p', 'Project')
    created = mini.create_thread('Claude', 'p', 'Machine evidence', 'evidence opening')
    tid = created['id']
    parent = wsl.post_message('Codex', tid, 'evidence parent')
    child = macbook.post_message('Claude', tid, 'evidence reply', parent_id=parent['id'])
    pages = [
        mini.read_thread('User', tid)['messages'],
        [mini.get_message('User', child['id'])['message']],
        mini.get_messages('User', [child['id']], include_parents=True)['messages'],
        mini.read_updates('User', tid)['messages'],
        mini.inbox('User', project_id='p')['items'],
        mini.search_messages('User', 'evidence'),
        mini.search_messages_page('User', 'evidence')['items'],
    ]
    for messages in pages:
        assert messages
        for message in messages:
            assert message['author'] in ('Claude', 'Codex')
            assert message['author_label'] == f"{message['author']}@{message['machine']}"
    summaries = [
        mini.read_thread('User', tid)['thread'],
        mini.get_message('User', child['id'])['thread'],
        mini.read_updates('User', tid)['thread'],
        mini.list_threads('User')[0],
        mini.list_threads_page('User')['items'][0],
        mini.search_threads('User', 'Machine')['items'][0],
    ]
    for summary in summaries:
        assert (summary['last_author'], summary['last_machine'], summary['last_author_label']) == ('Claude', 'macbook', 'Claude@macbook')


def test_machine_does_not_split_filters_read_markers_or_request_namespaces(tmp_path):
    mini = Store(tmp_path, machine='mini')
    macbook = Store(tmp_path, machine='macbook')
    mini.create_project('p', 'Project')
    first = mini.create_thread('Claude', 'p', 'Retry', 'Opening', request_id='thread-key')
    retried = macbook.create_thread('Claude', 'p', 'Retry', 'Opening', request_id='thread-key')
    assert first == retried
    posted = mini.post_message('Claude', first['id'], 'Shared retry', request_id='post-key')
    assert macbook.post_message('Claude', first['id'], 'Shared retry', request_id='post-key') == posted
    with pytest.raises(Conflict):
        macbook.post_message('Claude', first['id'], 'Changed content', request_id='post-key')
    elsewhere = macbook.post_message('Claude', first['id'], 'Other machine')
    other_author = macbook.post_message('Codex', first['id'], 'Shared retry', request_id='post-key')
    assert other_author['id'] != posted['id']
    filtered = mini.search_messages_page('User', 'machine', author_filter='Claude')['items']
    assert [m['id'] for m in filtered] == [elsewhere['id']]
    assert {m['machine'] for m in mini.search_messages_page('User', 'retry', author_filter='Claude')['items']} == {'mini'}
    assert mini.read_thread('Claude', first['id'])['thread']['unread_count'] == 1
    mini.mark_read('Claude', first['id'], other_author['id'])
    assert macbook.read_thread('Claude', first['id'])['thread']['unread_count'] == 0
    with mini.connect() as con:
        assert {r[0] for r in con.execute('SELECT author FROM requests')} == {'Claude', 'Codex'}
        assert {r[0] for r in con.execute('SELECT author FROM read_markers')} == {'Claude'}
    with pytest.raises(ValueError):
        mini.search_messages_page('User', 'retry', author_filter='Claude@mini')


def test_message_budgets_count_machine_labels_and_exports_keep_human_plain(tmp_path):
    store = Store(tmp_path, machine='macbook')
    store.set_user_name('Barry')
    store.create_project('p', 'Project')
    tid = store.create_thread('Claude', 'p', 'Budget', 'é' * 200)['id']
    for _ in range(3):
        store.post_message('Codex', tid, 'é' * 200)
    expected = [m['id'] for m in store.read_thread('User', tid)['messages']]
    seen, after = [], 0
    while True:
        page = store.read_updates('User', tid, after=after, max_bytes=1000)
        assert page['returned_bytes'] == len(json.dumps(page['messages'], ensure_ascii=False, separators=(',', ':')).encode())
        assert page['returned_bytes'] <= 1000
        assert all(m['author_label'].endswith('@macbook') for m in page['messages'])
        seen.extend(m['id'] for m in page['messages'])
        if not page['has_more']:
            break
        after = page['next_after']
    assert seen == expected
    human = store.post_message('User', tid, 'Answer')
    text = store.export_thread('User', tid)
    assert 'Claude@macbook' in text and 'Codex@macbook' in text
    assert f"Message {human['id']} · Barry · note" in text and 'Barry@' not in text
    assert author_label('Fable', None, 'Barry') == 'Fable'
