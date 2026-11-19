"""Gemini is a third agent participant: it posts, has its own unread state and can be named next."""
import sqlite3

import pytest

from correspondence.store import SCHEMA_VERSION, Store
from correspondence.turns import declared_next


def board(tmp_path, machine=None):
    store = Store(tmp_path, machine=machine)
    store.create_project("p", "Project")
    return store


def test_gemini_posts_and_keeps_its_own_unread_state(tmp_path):
    store = board(tmp_path, machine="mini")
    thread = store.create_thread("Claude", "p", "Topic", "From Claude")
    reply = store.post_message("Gemini", thread["id"], "From Gemini", model="gemini-test")
    assert reply["author"] == "Gemini" and reply["machine"] == "mini"
    listed = store.list_threads_page("Codex", "p")["items"][0]
    assert listed["unread_counts"] == {"User": 2, "Claude": 1, "Codex": 2, "Gemini": 1}
    store.mark_read("Gemini", thread["id"], reply["id"])
    listed = store.list_threads_page("Gemini", "p")["items"][0]
    assert listed["unread_counts"]["Gemini"] == 0 and listed["unread_counts"]["Codex"] == 2
    found = store.search_messages_page("Claude", "From", author_filter="Gemini")
    assert [m["id"] for m in found["items"]] == [reply["id"]]


def test_unknown_author_is_still_refused(tmp_path):
    store = board(tmp_path)
    with pytest.raises(ValueError):
        store.create_thread("Grok", "p", "Topic", "Body")


def test_upgrade_from_version_13_keeps_every_row(tmp_path):
    store = board(tmp_path)
    thread = store.create_thread("Claude", "p", "Topic", "One")
    store.post_message("Codex", thread["id"], "Two")
    store.mark_read("Codex", thread["id"], thread["first_message_id"])
    path = next(tmp_path.glob("*.sqlite3"))
    con = sqlite3.connect(path)
    before = (con.execute("SELECT * FROM messages ORDER BY id").fetchall(),
              con.execute("SELECT * FROM read_markers ORDER BY 1,2").fetchall())
    # Put the version-13 tables back, as a board made before Gemini has them.
    con.executescript("""
        PRAGMA foreign_keys=OFF;
        DROP TRIGGER messages_current_author; DROP TRIGGER messages_no_update; DROP TRIGGER messages_no_delete;
        CREATE TABLE m13 (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES threads(id),
            author TEXT NOT NULL CHECK(author IN ('User','Claude','Codex','Astra','Fable')),
            model TEXT, body TEXT NOT NULL, kind TEXT NOT NULL,
            parent_id INTEGER REFERENCES messages(id), created_at TEXT NOT NULL, machine TEXT);
        INSERT INTO m13 SELECT * FROM messages;
        CREATE TABLE r13 (author TEXT NOT NULL CHECK(author IN ('User','Claude','Codex')),
            thread_id INTEGER NOT NULL REFERENCES threads(id),
            message_id INTEGER NOT NULL REFERENCES messages(id), PRIMARY KEY(author,thread_id));
        INSERT INTO r13 SELECT * FROM read_markers;
        DROP TABLE read_markers; DROP TABLE messages;
        ALTER TABLE m13 RENAME TO messages; ALTER TABLE r13 RENAME TO read_markers;
        CREATE INDEX messages_thread ON messages(thread_id,id);
        CREATE TRIGGER messages_current_author BEFORE INSERT ON messages
            WHEN NEW.author NOT IN ('User','Claude','Codex')
            BEGIN SELECT RAISE(ABORT, 'Use a current participant identity'); END;
        PRAGMA user_version=13;
    """)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO messages(thread_id,author,body,kind,created_at) VALUES (1,'Gemini','x','note','t')")
    con.close()

    upgraded = Store(tmp_path)
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 14
    assert (con.execute("SELECT * FROM messages ORDER BY id").fetchall(),
            con.execute("SELECT * FROM read_markers ORDER BY 1,2").fetchall()) == before
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert upgraded.post_message("Gemini", thread["id"], "Three")["author"] == "Gemini"
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE messages SET body='changed' WHERE id=1")


def test_next_line_can_name_gemini():
    assert declared_next("Done.\n\nNext: Gemini — review the diff") == {"agent": "Gemini", "reason": "review the diff"}
