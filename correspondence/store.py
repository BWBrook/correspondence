"""Shared transactional SQLite store; reads never acknowledge messages."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

AUTHORS = {"Barry", "Claude", "Codex"}
LEGACY_AUTHORS = {"Claude": "Fable", "Codex": "Astra"}
KINDS = {"note", "question", "proposal", "evidence", "decision", "counterexample"}
DISPATCH_TARGETS = {"Claude", "Codex"}
DISPATCH_GAP_SECONDS = 15
MACHINE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass


def author_name(author: str) -> str:
    if author not in AUTHORS:
        raise ValueError("Author must be Barry, Claude, or Codex")
    return author


def clean_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain 1–{maximum} characters")
    return value.strip()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory or os.environ.get("CORRESPONDENCE_DATA_DIR", "~/Library/Application Support/Correspondence")).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "board.sqlite3"
        with self.connect(transaction=False) as con:
            self._enable_wal(con)
            self._upgrade(con)
        self.path.chmod(0o600)

    @staticmethod
    def _enable_wal(con):
        # Switching journal mode can return SQLITE_BUSY immediately despite the
        # connection busy timeout when initializers race. Bound this retry phase
        # explicitly; restore the normal timeout before taking the migration lock.
        timeout = con.execute("PRAGMA busy_timeout").fetchone()[0]
        con.execute("PRAGMA busy_timeout=0")
        deadline = time.monotonic() + timeout / 1000
        try:
            while True:
                try:
                    con.execute("PRAGMA journal_mode=WAL").fetchone()
                    return
                except sqlite3.OperationalError as error:
                    remaining = deadline - time.monotonic()
                    if (getattr(error, "sqlite_errorcode", 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or remaining <= 0:
                        raise
                    time.sleep(min(0.05, remaining))
        finally:
            con.execute(f"PRAGMA busy_timeout={timeout}")

    @staticmethod
    def _fingerprint(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @classmethod
    def _upgrade(cls, con):
        # SQLite's table-rebuild procedure: disable FK enforcement before BEGIN,
        # serialize all schema checks/DDL, validate the replacement, then re-enable.
        con.execute("PRAGMA foreign_keys=OFF")
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("""CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    repo_path TEXT, created_at TEXT NOT NULL
                )""")
            con.execute("""CREATE TABLE IF NOT EXISTS threads (
                    id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )""")
            con.execute("""CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES threads(id),
                    author TEXT NOT NULL CHECK(author IN ('Barry','Astra','Fable')),
                    model TEXT, body TEXT NOT NULL, kind TEXT NOT NULL,
                    parent_id INTEGER REFERENCES messages(id), created_at TEXT NOT NULL
                )""")
            con.execute("""CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id,id)""")
            con.execute("""CREATE TABLE IF NOT EXISTS read_markers (
                    author TEXT NOT NULL, thread_id INTEGER NOT NULL REFERENCES threads(id),
                    message_id INTEGER NOT NULL REFERENCES messages(id), PRIMARY KEY(author,thread_id)
                )""")
            con.execute("""CREATE TABLE IF NOT EXISTS requests (
                    author TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, result_id INTEGER NOT NULL, PRIMARY KEY(author,request_id)
                )""")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                for row in con.execute("SELECT rowid, operation, result_id FROM requests").fetchall():
                    if row["operation"] == "post_message":
                        m = con.execute("SELECT thread_id, body, kind, parent_id FROM messages WHERE id=?", (row["result_id"],)).fetchone()
                        payload = [m["thread_id"], m["body"], m["kind"], m["parent_id"]]
                    else:
                        t = con.execute("SELECT project_id, title FROM threads WHERE id=?", (row["result_id"],)).fetchone()
                        m = con.execute("SELECT body, kind FROM messages WHERE thread_id=? ORDER BY id LIMIT 1", (row["result_id"],)).fetchone()
                        payload = [t["project_id"], t["title"], m["body"], m["kind"]]
                    con.execute("UPDATE requests SET fingerprint=? WHERE rowid=?", (cls._fingerprint(payload), row["rowid"]))
            if version < 2:
                con.execute("ALTER TABLE projects ADD COLUMN archived_at TEXT")
                con.execute("ALTER TABLE projects ADD COLUMN archived_original_id TEXT")
            if version < 3:
                con.execute("ALTER TABLE projects ADD COLUMN state_token TEXT")
                for row in con.execute("SELECT id FROM projects").fetchall():
                    con.execute("UPDATE projects SET state_token=? WHERE id=?", (uuid4().hex, row["id"]))
            if version < 4:
                cls._upgrade_participants(con)
                if con.execute("PRAGMA foreign_key_check").fetchall():
                    raise sqlite3.IntegrityError("Foreign key violation during participant migration")
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.IntegrityError("Integrity failure during participant migration")
                con.execute("PRAGMA user_version=4")
            if version < 5:
                con.execute("""CREATE TABLE IF NOT EXISTS project_agents (
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    agent TEXT NOT NULL CHECK(agent IN ('Claude','Codex')),
                    session_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, agent))""")
                con.execute("""CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    target TEXT NOT NULL CHECK(target IN ('Claude','Codex')),
                    session_id TEXT NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','delivered','failed')),
                    requested_at TEXT NOT NULL, delivered_at TEXT, detail TEXT)""")
                con.execute("CREATE INDEX IF NOT EXISTS dispatches_project ON dispatches(project_id, id)")
                con.execute("PRAGMA user_version=5")
            if version < 6:
                if "claimed_at" not in {row[1] for row in con.execute("PRAGMA table_info(dispatches)")}:
                    con.execute("ALTER TABLE dispatches ADD COLUMN claimed_at TEXT")
                con.execute("PRAGMA user_version=6")
            if version < 7:
                for table in ("project_agents", "dispatches"):
                    if "machine" not in {row[1] for row in con.execute(f"PRAGMA table_info({table})")}:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN machine TEXT")
                con.execute("PRAGMA user_version=7")
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _upgrade_participants(con):
        # Copy every historical column verbatim. The broader CHECK permits copied
        # history; the INSERT trigger only permits current authors after migration.
        con.execute("""CREATE TABLE messages_new (
            id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES threads(id),
            author TEXT NOT NULL CHECK(author IN ('Barry','Claude','Codex','Astra','Fable')),
            model TEXT, body TEXT NOT NULL, kind TEXT NOT NULL,
            parent_id INTEGER REFERENCES messages(id), created_at TEXT NOT NULL)""")
        con.execute("""INSERT INTO messages_new(id,thread_id,author,model,body,kind,parent_id,created_at)
            SELECT id,thread_id,author,model,body,kind,parent_id,created_at FROM messages""")
        con.execute("""CREATE TABLE read_markers_new (
            author TEXT NOT NULL CHECK(author IN ('Barry','Claude','Codex')),
            thread_id INTEGER NOT NULL REFERENCES threads(id),
            message_id INTEGER NOT NULL REFERENCES messages(id), PRIMARY KEY(author,thread_id))""")
        con.execute("""INSERT INTO read_markers_new(author,thread_id,message_id)
            SELECT CASE author WHEN 'Fable' THEN 'Claude' WHEN 'Astra' THEN 'Codex' ELSE author END,
                thread_id,MAX(message_id) FROM read_markers GROUP BY
                CASE author WHEN 'Fable' THEN 'Claude' WHEN 'Astra' THEN 'Codex' ELSE author END,thread_id""")
        con.execute("DROP TABLE read_markers")
        con.execute("DROP TABLE messages")
        con.execute("ALTER TABLE messages_new RENAME TO messages")
        con.execute("ALTER TABLE read_markers_new RENAME TO read_markers")
        con.execute("CREATE INDEX messages_thread ON messages(thread_id,id)")
        con.execute("""CREATE TRIGGER messages_current_author BEFORE INSERT ON messages
            WHEN NEW.author NOT IN ('Barry','Claude','Codex')
            BEGIN SELECT RAISE(ABORT, 'Use a current participant identity'); END""")
        con.execute("""CREATE TRIGGER messages_no_update BEFORE UPDATE ON messages
            BEGIN SELECT RAISE(ABORT, 'Messages are append-only'); END""")
        con.execute("""CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages
            BEGIN SELECT RAISE(ABORT, 'Messages are append-only'); END""")

    @contextmanager
    def connect(self, write=False, transaction=True):
        con = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        try:
            if transaction:
                con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _require(con, table, key):
        row = con.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise NotFound(f"{table.rstrip('s').capitalize()} {key!r} not found")
        return dict(row)

    @staticmethod
    def _request(con, author, request_id, operation, payload, result_id=None):
        if request_id is None:
            return None
        clean_text(request_id, "request_id", 200)
        fingerprint = Store._fingerprint(payload)
        if result_id is not None:
            con.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (author, request_id, operation, fingerprint, result_id))
            return result_id
        rows = con.execute("SELECT * FROM requests WHERE author IN (?,?) AND request_id=?",
                           (author, LEGACY_AUTHORS.get(author, author), request_id)).fetchall()
        if rows:
            if any(row["operation"] != operation or row["fingerprint"] != fingerprint for row in rows):
                raise Conflict("request_id has already been used with different content")
            if len({row["result_id"] for row in rows}) != 1:
                raise Conflict("request_id has conflicting historical and current results")
            return rows[0]["result_id"]
        return None

    @staticmethod
    def _message_args(author, body, kind, model):
        author_name(author)
        clean_text(body, "body", 100_000)
        body = body.rstrip()
        if kind not in KINDS:
            raise ValueError("Unknown message kind")
        if model is not None:
            model = clean_text(model, "model", 200)
        return body, model

    @staticmethod
    def _insert_message(con, thread_id, author, model, body, kind, parent_id=None):
        if parent_id is not None:
            parent = Store._require(con, "messages", parent_id)
            if parent["thread_id"] != thread_id:
                raise ValueError("Reply parent must belong to the same discussion")
        timestamp = now()
        cursor = con.execute("INSERT INTO messages(thread_id,author,model,body,kind,parent_id,created_at) VALUES (?,?,?,?,?,?,?)", (thread_id, author, model, body, kind, parent_id, timestamp))
        con.execute("UPDATE threads SET updated_at=? WHERE id=?", (timestamp, thread_id))
        return cursor.lastrowid

    @staticmethod
    def _thread_records(con, threads, author):
        """Batch explicit acknowledgement metadata within the caller's snapshot."""
        if not threads:
            return []
        records = {thread["id"]: dict(thread, read_markers={name: 0 for name in ("Barry", "Claude", "Codex")},
                                      unread_counts={name: 0 for name in ("Barry", "Claude", "Codex")},
                                      message_count=0, first_message_id=None, last_message_id=None,
                                      first_unread_message_id=None, last_author=None) for thread in threads}
        placeholders = ",".join("?" for _ in records)
        stats = con.execute(f"""WITH participants(author,legacy) AS (VALUES ('Barry','Barry'),('Claude','Fable'),('Codex','Astra'))
            SELECT m.thread_id, p.author participant, COALESCE(r.message_id,0) marker,
                COUNT(*) message_count, MIN(m.id) first_message_id, MAX(m.id) last_message_id,
                SUM(m.id>COALESCE(r.message_id,0) AND m.author NOT IN (p.author,p.legacy)) unread_count,
                MIN(CASE WHEN m.id>COALESCE(r.message_id,0) AND m.author NOT IN (p.author,p.legacy) THEN m.id END) first_unread_message_id
            FROM messages m CROSS JOIN participants p
            LEFT JOIN read_markers r ON r.thread_id=m.thread_id AND r.author=p.author
            WHERE m.thread_id IN ({placeholders}) GROUP BY m.thread_id,p.author""", tuple(records))
        for row in stats:
            record = records[row["thread_id"]]
            participant = row["participant"]
            record["read_markers"][participant] = row["marker"]
            record["unread_counts"][participant] = row["unread_count"]
            if participant == author:
                for field in ("message_count", "first_message_id", "last_message_id", "first_unread_message_id"):
                    record[field] = row[field]
        last_ids = [record["last_message_id"] for record in records.values() if record["last_message_id"] is not None]
        last_authors = {}
        if last_ids:
            last_authors = dict(con.execute(f"SELECT id,author FROM messages WHERE id IN ({','.join('?' for _ in last_ids)})", last_ids))
        for record in records.values():
            record["last_author"] = last_authors.get(record["last_message_id"])
            record["unread_count"] = record["unread_counts"][author]
            record["last_read_message_id"] = record["read_markers"][author]
        return list(records.values())

    @staticmethod
    def _thread(con, thread_id, author, *, archived=False):
        thread = Store._visible_thread(con, thread_id, archived=archived)
        return Store._thread_records(con, [thread], author)[0]

    @staticmethod
    def _visible_thread(con, thread_id, *, archived=False):
        thread = Store._require(con, "threads", thread_id)
        Store._project(con, thread["project_id"], archived=archived)
        return thread

    @staticmethod
    def _archive_flag(archived):
        if type(archived) is not bool:
            raise ValueError("archived must be boolean")
        return archived

    @staticmethod
    def _project(con, project_id, *, archived=False):
        Store._archive_flag(archived)
        project = Store._require(con, "projects", project_id)
        if (project["archived_at"] is not None) != archived:
            raise NotFound("Not found")
        return project

    @staticmethod
    def _rekey_project(con, old_id, new_id):
        # Both references change atomically; deferred FK checking occurs at commit.
        con.execute("PRAGMA defer_foreign_keys=ON")
        con.execute("UPDATE projects SET id=? WHERE id=?", (new_id, old_id))
        con.execute("UPDATE threads SET project_id=? WHERE project_id=?", (new_id, old_id))
        con.execute("UPDATE project_agents SET project_id=? WHERE project_id=?", (new_id, old_id))
        con.execute("UPDATE dispatches SET project_id=? WHERE project_id=?", (new_id, old_id))

    @staticmethod
    def _project_state(con, project_id, expected_state):
        project = Store._require(con, "projects", project_id)
        if not isinstance(expected_state, str) or project["state_token"] != expected_state:
            raise Conflict("Project state changed; refresh before trying again")
        return project

    def archive_project(self, project_id, expected_state: str):
        """Preserve a section under a new archival key, freeing its active slug."""
        with self.connect(write=True) as con:
            project = self._project_state(con, project_id, expected_state)
            if project["archived_at"] is not None:
                raise Conflict("Project state changed; refresh before trying again")
            number = 2
            while True:
                suffix = f"-archived-{number}"
                archive_id = project_id[:100-len(suffix)].rstrip("-") + suffix
                if not con.execute("SELECT 1 FROM projects WHERE id=?", (archive_id,)).fetchone():
                    break
                number += 1
            self._rekey_project(con, project_id, archive_id)
            con.execute("UPDATE projects SET archived_at=?,archived_original_id=?,state_token=? WHERE id=?",
                        (now(), project_id, uuid4().hex, archive_id))
            return self._require(con, "projects", archive_id)

    def restore_project(self, project_id, expected_state: str):
        """Restore without overwriting active projects or reusing their display names."""
        with self.connect(write=True) as con:
            project = self._project_state(con, project_id, expected_state)
            if project["archived_at"] is None:
                raise Conflict("Project state changed; refresh before trying again")
            original = project["archived_original_id"]
            restored_id, number = original, 2
            while con.execute("SELECT 1 FROM projects WHERE id=? AND id!=?", (restored_id, project_id)).fetchone():
                suffix = f"-restored-{number}"
                restored_id = original[:100-len(suffix)].rstrip("-") + suffix
                number += 1
            name, number = project["name"], 2
            while con.execute("SELECT 1 FROM projects WHERE name=? COLLATE NOCASE AND archived_at IS NULL AND id!=?",
                              (name, project_id)).fetchone():
                suffix = f" (restored {number})"
                name = project["name"][:200-len(suffix)] + suffix
                number += 1
            self._rekey_project(con, project_id, restored_id)
            con.execute("UPDATE projects SET name=?,archived_at=NULL,archived_original_id=NULL,state_token=? WHERE id=?", (name, uuid4().hex, restored_id))
            return self._require(con, "projects", restored_id)

    def project_agents(self, project_id=None):
        """Main Claude session and Codex thread per project: {project: {agent: session}}."""
        with self.connect() as con:
            if project_id is not None:
                self._project(con, project_id)
            rows = con.execute("SELECT project_id, agent, session_id FROM project_agents"
                               + (" WHERE project_id=?" if project_id is not None else ""),
                               (project_id,) if project_id is not None else ()).fetchall()
        pins = {}
        for row in rows:
            pins.setdefault(row["project_id"], {})[row["agent"]] = row["session_id"]
        return pins.get(project_id, {}) if project_id is not None else pins

    def pin_machines(self, project_id):
        """Machine each main session runs on, {agent: machine}; None is the board's own host."""
        with self.connect() as con:
            self._project(con, project_id)
            return {row["agent"]: row["machine"] for row in con.execute(
                "SELECT agent, machine FROM project_agents WHERE project_id=?", (project_id,))}

    def machine_pins(self, machine):
        """Every main session pinned on one machine, for that machine's courier."""
        with self.connect() as con:
            return [dict(row) for row in con.execute(
                "SELECT project_id, agent, session_id FROM project_agents WHERE machine=? ORDER BY project_id, agent", (machine,))]

    def set_project_agent(self, project_id, agent, session_id, machine=None):
        if agent not in DISPATCH_TARGETS:
            raise ValueError("Agent must be Claude or Codex")
        if session_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{7,99}", session_id):
            raise ValueError("Session id must be 8–100 letters, digits or hyphens")
        if machine is not None and not MACHINE_RE.fullmatch(machine):
            raise ValueError("Machine must be a short lowercase name")
        with self.connect(write=True) as con:
            self._project(con, project_id)
            if session_id is None:
                con.execute("DELETE FROM project_agents WHERE project_id=? AND agent=?", (project_id, agent))
            else:
                con.execute("""INSERT INTO project_agents(project_id,agent,session_id,updated_at,machine) VALUES (?,?,?,?,?)
                    ON CONFLICT(project_id,agent) DO UPDATE SET session_id=excluded.session_id,
                    updated_at=excluded.updated_at, machine=excluded.machine""",
                            (project_id, agent, session_id, now(), machine))
        return self.project_agents(project_id)

    def create_dispatch(self, project_id, target, body):
        """Record Barry's prompt for a project's main agent; refuse a duplicate in flight."""
        if target not in DISPATCH_TARGETS:
            raise ValueError("Target must be Claude or Codex")
        body = clean_text(body, "Prompt", 50_000)
        with self.connect(write=True) as con:
            self._project(con, project_id)
            pin = con.execute("SELECT session_id, machine FROM project_agents WHERE project_id=? AND agent=?", (project_id, target)).fetchone()
            if pin is None:
                raise Conflict(f"Set this project's main {target} session first")
            cutoff = datetime.fromtimestamp(time.time() - DISPATCH_GAP_SECONDS, timezone.utc).isoformat(timespec="microseconds")
            if con.execute("""SELECT 1 FROM dispatches WHERE project_id=? AND target=?
                    AND (status='queued' OR (status='delivered' AND delivered_at>?))""", (project_id, target, cutoff)).fetchone():
                raise Conflict(f"A prompt to {target} was just sent; wait a few seconds before sending another")
            cursor = con.execute("""INSERT INTO dispatches(project_id,target,session_id,body,status,requested_at,machine)
                VALUES (?,?,?,?,'queued',?,?)""", (project_id, target, pin["session_id"], body, now(), pin["machine"]))
            return self._require(con, "dispatches", cursor.lastrowid)

    def finish_dispatch(self, dispatch_id, delivered, detail, machine=None):
        with self.connect(write=True) as con:
            dispatch = self._require(con, "dispatches", dispatch_id)
            if dispatch["machine"] != machine:
                raise Conflict(f"Dispatch {dispatch_id} is not for this machine")
            con.execute("UPDATE dispatches SET status=?, delivered_at=?, detail=? WHERE id=? AND status='queued'",
                        ("delivered" if delivered else "failed", now() if delivered else None, (detail or "")[:2000], dispatch_id))
            return self._require(con, "dispatches", dispatch_id)

    def waiting_dispatches(self, target, machine=None):
        """Unclaimed prompts for one agent on one machine (None: the board's host)."""
        with self.connect() as con:
            return [dict(r) for r in con.execute(
                """SELECT * FROM dispatches WHERE target=? AND machine IS ? AND status='queued' AND claimed_at IS NULL
                ORDER BY id""", (target, machine))]

    def claim_dispatch(self, dispatch_id, machine=None):
        """Hand one waiting prompt to the machine's messenger or courier; False if already taken."""
        with self.connect(write=True) as con:
            return con.execute("""UPDATE dispatches SET claimed_at=? WHERE id=? AND machine IS ? AND status='queued'
                AND claimed_at IS NULL""", (now(), dispatch_id, machine)).rowcount == 1

    def cancel_dispatch(self, dispatch_id):
        with self.connect(write=True) as con:
            dispatch = self._require(con, "dispatches", dispatch_id)
            if dispatch["status"] != "queued" or dispatch["claimed_at"] is not None:
                raise Conflict("Only a prompt the messenger has not yet picked up can be cancelled")
            con.execute("UPDATE dispatches SET status='failed', detail='Cancelled by Barry' WHERE id=?", (dispatch_id,))
            return self._require(con, "dispatches", dispatch_id)

    def list_dispatches(self, project_id, limit=10):
        with self.connect() as con:
            self._project(con, project_id)
            return [dict(row) for row in con.execute(
                "SELECT * FROM dispatches WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit))]

    def create_project(self, id: str, name: str, description: str = "", repo_path: str | None = None):
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", id) or len(id) > 100:
            raise ValueError("Project id must be a lowercase hyphenated slug, up to 100 characters")
        name = clean_text(name, "name", 200)
        if len(description) > 10_000:
            raise ValueError("Description is too long")
        if repo_path is not None:
            repo_path = clean_text(repo_path, "repo_path", 1000)
        with self.connect(write=True) as con:
            existing = con.execute("SELECT * FROM projects WHERE id=?", (id,)).fetchone()
            if existing:
                self._project(con, id)
                if (existing["name"], existing["description"], existing["repo_path"]) != (name, description, repo_path):
                    raise Conflict("Project slug already exists with different details")
                return dict(existing)
            con.execute("INSERT INTO projects(id,name,description,repo_path,created_at,state_token) VALUES (?,?,?,?,?,?)", (id, name, description, repo_path, now(), uuid4().hex))
            return self._require(con, "projects", id)

    def list_projects(self, author, *, archived=False):
        self._archive_flag(archived)
        author_name(author)
        with self.connect() as con:
            projects = [dict(row, unread_counts={name: 0 for name in ("Barry", "Claude", "Codex")})
                        for row in con.execute("""SELECT p.*,
                            (SELECT MAX(t.updated_at) FROM threads t WHERE t.project_id=p.id) last_activity_at
                            FROM projects p WHERE (p.archived_at IS NOT NULL)=? ORDER BY p.name COLLATE NOCASE, p.id LIMIT 1000""", (archived,))]
            if not projects:
                return []
            by_id = {project["id"]: project for project in projects}
            placeholders = ",".join("?" for _ in projects)
            counts = con.execute(f"""WITH participants(author,legacy) AS (VALUES ('Barry','Barry'),('Claude','Fable'),('Codex','Astra'))
                SELECT t.project_id,p.author participant,COUNT(*) unread_count
                FROM messages m JOIN threads t ON t.id=m.thread_id CROSS JOIN participants p
                LEFT JOIN read_markers r ON r.thread_id=t.id AND r.author=p.author
                WHERE t.project_id IN ({placeholders}) AND m.id>COALESCE(r.message_id,0) AND m.author NOT IN (p.author,p.legacy)
                GROUP BY t.project_id,p.author""", tuple(by_id))
            for row in counts:
                by_id[row["project_id"]]["unread_counts"][row["participant"]] = row["unread_count"]
            for project in projects:
                project["unread_count"] = project["unread_counts"][author]
            return projects

    def list_threads(self, author, project_id=None, unread_only=False, limit=200, *, archived=False):
        self._archive_flag(archived)
        author_name(author)
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.connect() as con:
            rows = con.execute("""SELECT t.* FROM threads t JOIN projects p ON p.id=t.project_id WHERE (p.archived_at IS NOT NULL)=? AND (? IS NULL OR t.project_id=?)
                AND (?=0 OR EXISTS (SELECT 1 FROM messages m LEFT JOIN read_markers r
                ON r.thread_id=t.id AND r.author=? WHERE m.thread_id=t.id AND m.author NOT IN (?,?)
                AND m.id>COALESCE(r.message_id,0))) ORDER BY t.updated_at DESC, t.id DESC LIMIT ?""",
                (archived, project_id, project_id, unread_only, author, author, LEGACY_AUTHORS.get(author, author), limit)).fetchall()
            records = self._thread_records(con, [dict(row) for row in rows], author)
            last_ids = [record["last_message_id"] for record in records if record["last_message_id"]]
            excerpts = dict(con.execute(f"SELECT id,substr(body,1,320) FROM messages WHERE id IN ({','.join('?' for _ in last_ids)})", last_ids)) if last_ids else {}
            for record in records:
                record["last_excerpt"] = excerpts.get(record["last_message_id"])
            return records

    def create_thread(self, author, project_id, title, body, kind="note", model=None, request_id=None):
        title = clean_text(title, "title", 300)
        body, model = self._message_args(author, body, kind, model)
        payload = [project_id, title, body, kind]
        with self.connect(write=True) as con:
            result_id = self._request(con, author, request_id, "create_thread", payload)
            if result_id is not None:
                return self._thread(con, result_id, author)
            self._project(con, project_id)
            timestamp = now()
            result_id = con.execute("INSERT INTO threads(project_id,title,created_at,updated_at) VALUES (?,?,?,?)", (project_id,title,timestamp,timestamp)).lastrowid
            self._insert_message(con, result_id, author, model, body, kind)
            self._request(con, author, request_id, "create_thread", payload, result_id)
            return self._thread(con, result_id, author)

    def read_thread(self, author, thread_id, after=0, limit=100, max_bytes=200_000, *, mode="oldest", before=None, archived=False):
        self._archive_flag(archived)
        author_name(author)
        if after < 0 or not 1 <= limit <= 500:
            raise ValueError("after must be nonnegative; limit must be between 1 and 500")
        if not 1_000 <= max_bytes <= 10_000_000:
            raise ValueError("max_bytes must be between 1000 and 10000000")
        if mode not in {"oldest", "latest", "unread"}:
            raise ValueError("mode must be oldest, latest, or unread")
        if before is not None and (not isinstance(before, int) or before < 1):
            raise ValueError("before must be a positive message id")
        if (before is not None and after != 0) or (mode != "oldest" and (after != 0 or before is not None)):
            raise ValueError("Use either after, before, or a latest/unread mode, not a combination")
        with self.connect() as con:
            thread = self._thread(con, thread_id, author, archived=archived)
            if mode == "unread":
                first_unread = thread["first_unread_message_id"]
                if first_unread is None:
                    mode = "latest"
                else:
                    after = first_unread - 1
            backwards = mode == "latest" or before is not None
            if before is not None:
                candidates = con.execute("SELECT * FROM messages WHERE thread_id=? AND id<? ORDER BY id DESC LIMIT ?", (thread_id, before, limit))
            elif backwards:
                candidates = con.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?", (thread_id, limit))
            else:
                candidates = con.execute("SELECT * FROM messages WHERE thread_id=? AND id>? ORDER BY id LIMIT ?", (thread_id, after, limit))
            # Always return at least one message so a single oversized body cannot stall paging.
            total, rows = 0, []
            for row in candidates:
                size = len(row["body"].encode("utf-8"))
                if rows and total + size > max_bytes:
                    break
                total += size
                rows.append(dict(row))
            if backwards:
                rows.reverse()
            first = rows[0]["id"] if rows else None
            last = rows[-1]["id"] if rows else None
            return {"thread": thread, "messages": rows,
                    "has_more": bool(rows and thread["last_message_id"] > last),
                    "next_after": last if rows else after,
                    "has_before": bool(rows and thread["first_message_id"] < first),
                    "previous_before": first}

    def get_message(self, author, message_id, *, archived=False):
        self._archive_flag(archived)
        author_name(author)
        with self.connect() as con:
            message = self._require(con, "messages", message_id)
            return {"thread": self._thread(con, message["thread_id"], author, archived=archived), "message": message}

    def post_message(self, author, thread_id, body, kind="note", parent_id=None, model=None, request_id=None):
        body, model = self._message_args(author, body, kind, model)
        payload = [thread_id, body, kind, parent_id]
        with self.connect(write=True) as con:
            self._visible_thread(con, thread_id)
            result_id = self._request(con, author, request_id, "post_message", payload)
            if result_id is not None:
                return self._require(con, "messages", result_id)
            result_id = self._insert_message(con, thread_id, author, model, body, kind, parent_id)
            self._request(con, author, request_id, "post_message", payload, result_id)
            return self._require(con, "messages", result_id)

    def mark_read(self, author, thread_id, message_id):
        author_name(author)
        with self.connect(write=True) as con:
            self._visible_thread(con, thread_id)
            message = self._require(con, "messages", message_id)
            if message["thread_id"] != thread_id:
                raise ValueError("Read marker must reference a message in this discussion")
            con.execute("""INSERT INTO read_markers VALUES (?,?,?) ON CONFLICT(author,thread_id)
                DO UPDATE SET message_id=MAX(read_markers.message_id,excluded.message_id)""", (author, thread_id, message_id))
            marker = con.execute("SELECT message_id FROM read_markers WHERE author=? AND thread_id=?", (author,thread_id)).fetchone()[0]
            return {"thread_id": thread_id, "last_read_message_id": marker}

    def mark_all_read(self, author):
        """Explicitly acknowledge this transaction's whole-board snapshot."""
        author_name(author)
        with self.connect(write=True) as con:
            # BEGIN IMMEDIATE holds other writers until these boundaries commit.
            # Query the board directly: project/thread listing limits must not apply.
            snapshots = con.execute("""SELECT m.thread_id, MAX(m.id) last_message_id,
                SUM(m.id>COALESCE(r.message_id,0) AND m.author NOT IN (?,?)) unread_count
                FROM messages m JOIN threads t ON t.id=m.thread_id JOIN projects p ON p.id=t.project_id
                LEFT JOIN read_markers r ON r.thread_id=m.thread_id AND r.author=?
                WHERE p.archived_at IS NULL GROUP BY m.thread_id""", (author, LEGACY_AUTHORS.get(author, author), author)).fetchall()
            con.executemany("""INSERT INTO read_markers(author,thread_id,message_id) VALUES (?,?,?)
                ON CONFLICT(author,thread_id) DO UPDATE
                SET message_id=MAX(read_markers.message_id,excluded.message_id)""",
                ((author, row["thread_id"], row["last_message_id"]) for row in snapshots))
            return {"threads_marked": sum(row["unread_count"] > 0 for row in snapshots),
                    "messages_marked": sum(row["unread_count"] for row in snapshots)}

    def set_thread_status(self, author, thread_id, status, model=None):
        author_name(author)
        if status not in {"open", "resolved"}:
            raise ValueError("status must be open or resolved")
        if model is not None:
            model = clean_text(model, "model", 200)
        with self.connect(write=True) as con:
            thread = self._visible_thread(con, thread_id)
            if thread["status"] != status:
                con.execute("UPDATE threads SET status=? WHERE id=?", (status,thread_id))
                self._insert_message(con, thread_id, author, model, f"Discussion status changed from {thread['status']} to {status}.", "note")
            return self._thread(con, thread_id, author)

    def search_messages(self, author, q, project_id=None, limit=100, *, thread_id=None, before=None, archived=False):
        self._archive_flag(archived)
        author_name(author)
        q = clean_text(q, "q", 500)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        for name, value in (("thread_id", thread_id), ("before", before)):
            if value is not None and (type(value) is not int or not 1 <= value <= 2**63 - 1):
                raise ValueError(f"{name} must be a positive SQLite integer id")
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.connect() as con:
            rows = con.execute("""SELECT m.id, m.thread_id, m.author, m.model, m.kind, m.parent_id, m.created_at, m.body,
                t.title thread_title, t.project_id, p.name project_name
                FROM messages m JOIN threads t ON m.thread_id=t.id JOIN projects p ON t.project_id=p.id
                WHERE (p.archived_at IS NOT NULL)=? AND (m.body LIKE ? ESCAPE '\\' OR t.title LIKE ? ESCAPE '\\')
                AND (? IS NULL OR t.project_id=?) AND (? IS NULL OR m.thread_id=?)
                AND (? IS NULL OR m.id<?) ORDER BY m.id DESC LIMIT ?""",
                (archived,pattern,pattern,project_id,project_id,thread_id,thread_id,before,before,limit))
            return [self._snippet(dict(row), q) for row in rows]

    @staticmethod
    def _snippet(row, q, width=300):
        # Search returns an excerpt around the first match, not whole bodies, to bound assistant context.
        body = row.pop("body")
        index = body.lower().find(q.lower())
        start = max(0, index - width // 3) if index >= 0 else 0
        end = min(len(body), start + width)
        row["snippet"] = ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")
        row["body_length"] = len(body)
        return row

    @staticmethod
    def _agent_id(value, name, *, optional=False, zero=False):
        if value is None and optional:
            return
        if type(value) is not int or not (0 if zero else 1) <= value <= 2**63 - 1:
            raise ValueError(f"{name} must be a {'nonnegative' if zero else 'positive'} SQLite integer id")

    @staticmethod
    def _agent_options(project_id=None, thread_id=None, limit=20, max_bytes=None, *, thread_page=False):
        if project_id is not None and (not isinstance(project_id, str) or len(project_id) > 100
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", project_id)):
            raise ValueError("project_id must be a lowercase hyphenated slug, up to 100 characters")
        Store._agent_id(thread_id, "thread_id", optional=True)
        maximum = 1000 if thread_page else 500
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        if max_bytes is not None and (type(max_bytes) is not int or not 1000 <= max_bytes <= 10_000_000):
            raise ValueError("max_bytes must be between 1000 and 10000000")

    @staticmethod
    def _literal_pattern(q):
        return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    @staticmethod
    def _items_bytes(items):
        return len(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _bounded_messages(rows, max_bytes, limit=None):
        """Budget the compact JSON message array, admitting a first oversized item."""
        items, size, has_more = [], 2, False  # JSON array brackets count too.
        for row in rows:
            item = dict(row)
            item_size = Store._items_bytes([item]) - 2
            candidate_size = size + item_size + bool(items)
            if items and ((limit is not None and len(items) >= limit) or candidate_size > max_bytes):
                has_more = True
                break
            items.append(item)
            size = candidate_size
        return items, size, size > max_bytes, has_more

    @staticmethod
    def _snapshot_bound(con, after, through):
        Store._agent_id(after, "after", zero=True)
        Store._agent_id(through, "through", optional=True, zero=True)
        maximum = con.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
        if through is None:
            through = maximum
        if after > through or through > maximum:
            raise ValueError("Require after <= through <= the current maximum message id")
        return through

    def inbox(self, author, project_id=None, thread_id=None, after=0, through=None, limit=20, unread_only=False, *, archived=False):
        """Compact chronological updates in (after, through]; unread_only is live triage, not durable synchronization.

        total counts this request's remaining matching interval. Exhausted pages
        advance next_after to through, including empty/filtered gaps. returned_bytes
        measures the UTF-8 compact JSON items array. No acknowledgement is changed.
        """
        self._archive_flag(archived)
        author_name(author)
        self._agent_options(project_id, thread_id, limit)
        if type(unread_only) is not bool:
            raise ValueError("unread_only must be boolean")
        with self.connect() as con:
            through = self._snapshot_bound(con, after, through)
            where = """FROM messages m JOIN threads t ON t.id=m.thread_id JOIN projects p ON p.id=t.project_id
                LEFT JOIN read_markers r ON r.thread_id=m.thread_id AND r.author=?
                WHERE (p.archived_at IS NOT NULL)=? AND m.id>? AND m.id<=? AND (? IS NULL OR t.project_id=?)
                AND (? IS NULL OR m.thread_id=?)
                AND (?=0 OR (m.id>COALESCE(r.message_id,0) AND m.author NOT IN (?,?)))"""
            parameters = (author, archived, after, through, project_id, project_id, thread_id, thread_id, unread_only, author, LEGACY_AUTHORS.get(author, author))
            total = con.execute("SELECT COUNT(*) " + where, parameters).fetchone()[0]
            rows = con.execute("SELECT m.*,t.project_id,t.title thread_title,p.name project_name " + where + " ORDER BY m.id LIMIT ?", (*parameters, limit))
            items = [self._snippet(dict(row), "") for row in rows]
            has_more = total > len(items)
            return {"items": items, "has_more": has_more,
                    "next_after": items[-1]["id"] if has_more else through,
                    "through": through, "total": total, "returned_bytes": self._items_bytes(items)}

    def read_updates(self, author, thread_id, after=None, through=None, limit=20, max_bytes=25000, *, archived=False):
        """Read full updates without a latest-page fallback or implicit acknowledgement.

        after=None starts at the participant's marker; explicit after ignores it.
        through/next_after are global checkpoints, not necessarily valid thread
        acknowledgement IDs. The byte budget counts the UTF-8 JSON messages array.
        One oversized first message is returned whole and explicitly flagged.
        """
        self._archive_flag(archived)
        author_name(author)
        self._agent_options(thread_id=thread_id, limit=limit, max_bytes=max_bytes)
        self._agent_id(thread_id, "thread_id")
        self._agent_id(after, "after", optional=True, zero=True)
        with self.connect() as con:
            thread = self._thread(con, thread_id, author, archived=archived)
            if after is None:
                after = thread["last_read_message_id"]
            through = self._snapshot_bound(con, after, through)
            rows = con.execute("SELECT * FROM messages WHERE thread_id=? AND id>? AND id<=? ORDER BY id LIMIT ?",
                               (thread_id, after, through, limit + 1))
            messages, size, oversized, has_more = self._bounded_messages(rows, max_bytes, limit)
            return {"thread": thread, "messages": messages, "has_more": has_more,
                    "next_after": messages[-1]["id"] if has_more else through,
                    "through": through, "returned_bytes": size, "oversized": oversized}

    def get_messages(self, author, message_ids, include_parents=False, max_bytes=25000, *, archived=False):
        """Read requested IDs in request order, then unique direct parents; expose all missing/omitted IDs."""
        self._archive_flag(archived)
        author_name(author)
        self._agent_options(max_bytes=max_bytes)
        if not isinstance(message_ids, (list, tuple)) or not 1 <= len(message_ids) <= 50:
            raise ValueError("message_ids must contain 1–50 IDs")
        for message_id in message_ids:
            self._agent_id(message_id, "message_ids")
        if type(include_parents) is not bool:
            raise ValueError("include_parents must be boolean")
        requested = list(dict.fromkeys(message_ids))
        with self.connect() as con:
            def fetch(ids):
                if not ids:
                    return {}
                rows = con.execute(f"""SELECT m.*,t.project_id,t.title thread_title
                    FROM messages m JOIN threads t ON t.id=m.thread_id JOIN projects p ON p.id=t.project_id
                    WHERE (p.archived_at IS NOT NULL)=? AND m.id IN ({','.join('?' for _ in ids)})""", (archived, *ids))
                return {row["id"]: dict(row) for row in rows}

            found = fetch(requested)
            missing = [message_id for message_id in requested if message_id not in found]
            candidates = [message_id for message_id in requested if message_id in found]
            if include_parents:
                parents = list(dict.fromkeys(found[message_id]["parent_id"] for message_id in candidates
                                             if found[message_id]["parent_id"] is not None
                                             and found[message_id]["parent_id"] not in requested))
                parent_rows = fetch(parents)
                found.update(parent_rows)
                missing.extend(message_id for message_id in parents if message_id not in parent_rows)
                candidates.extend(message_id for message_id in parents if message_id in parent_rows)
            messages, size, oversized, _ = self._bounded_messages((found[message_id] for message_id in candidates), max_bytes)
            return {"messages": messages, "missing_ids": missing, "omitted_ids": candidates[len(messages):],
                    "returned_bytes": size, "oversized": oversized}

    def search_messages_page(self, author, q, project_id=None, thread_id=None, author_filter=None,
                             kind=None, since=None, after=0, before=None, limit=20, *, archived=False):
        """Search body text only; scope/filter intersections and descending immutable-ID pages."""
        self._archive_flag(archived)
        author_name(author)
        self._agent_options(project_id, thread_id, limit)
        self._agent_id(after, "after", zero=True)
        self._agent_id(before, "before", optional=True)
        q = clean_text(q, "q", 500)
        if author_filter is not None and author_filter not in AUTHORS | set(LEGACY_AUTHORS.values()):
            raise ValueError("Unknown author filter")
        if kind is not None and kind not in KINDS:
            raise ValueError("Unknown message kind")
        if since is not None:
            try:
                timestamp = datetime.fromisoformat(since)
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("Timezone required")
                since = timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("since must be a timezone-aware ISO8601 timestamp") from error
        with self.connect() as con:
            rows = con.execute("""SELECT m.*,t.project_id,t.title thread_title,p.name project_name
                FROM messages m JOIN threads t ON t.id=m.thread_id JOIN projects p ON p.id=t.project_id
                WHERE (p.archived_at IS NOT NULL)=? AND m.body LIKE ? ESCAPE '\\' AND (? IS NULL OR t.project_id=?)
                AND (? IS NULL OR m.thread_id=?) AND (? IS NULL OR m.author IN (?,?))
                AND (? IS NULL OR m.kind=?) AND (? IS NULL OR m.created_at>=?)
                AND m.id>? AND (? IS NULL OR m.id<?) ORDER BY m.id DESC LIMIT ?""",
                (archived, self._literal_pattern(q), project_id, project_id, thread_id, thread_id,
                 author_filter, author_filter, LEGACY_AUTHORS.get(author_filter, author_filter), kind, kind, since, since, after, before, before, limit + 1)).fetchall()
            items = [self._snippet(dict(row), q) for row in rows[:limit]]
            return {"items": items, "has_more": len(rows) > limit,
                    "next_before": items[-1]["id"] if items else before}

    @staticmethod
    def _compact_thread(record):
        fields = ("id", "project_id", "title", "status", "updated_at", "message_count",
                  "last_message_id", "unread_count", "last_read_message_id", "first_unread_message_id",
                  "read_markers", "unread_counts", "last_author")
        return {field: record[field] for field in fields}

    def _threads_page(self, author, project_id, unread_only, limit, before, q=None, *, archived=False):
        self._archive_flag(archived)
        author_name(author)
        self._agent_options(project_id, limit=limit, thread_page=True)
        self._agent_id(before, "before", optional=True)
        if type(unread_only) is not bool:
            raise ValueError("unread_only must be boolean")
        pattern = None if q is None else self._literal_pattern(clean_text(q, "q", 500))
        with self.connect() as con:
            rows = con.execute("""SELECT t.* FROM threads t JOIN projects p ON p.id=t.project_id
                WHERE (p.archived_at IS NOT NULL)=? AND (? IS NULL OR t.project_id=?) AND (? IS NULL OR t.id<?)
                AND (? IS NULL OR t.title LIKE ? ESCAPE '\\')
                AND (?=0 OR EXISTS (SELECT 1 FROM messages m LEFT JOIN read_markers r
                    ON r.thread_id=t.id AND r.author=? WHERE m.thread_id=t.id AND m.author NOT IN (?,?)
                    AND m.id>COALESCE(r.message_id,0))) ORDER BY t.id DESC LIMIT ?""",
                (archived, project_id, project_id, before, before, pattern, pattern, unread_only, author, author, LEGACY_AUTHORS.get(author, author), limit + 1)).fetchall()
            items = [self._compact_thread(record) for record in self._thread_records(con, [dict(row) for row in rows[:limit]], author)]
            return {"items": items, "has_more": len(rows) > limit,
                    "next_before": items[-1]["id"] if items else before}

    def search_threads(self, author, q, project_id=None, before=None, limit=20, *, archived=False):
        """Search titles once per discussion; activity changes do not move ID cursors."""
        return self._threads_page(author, project_id, False, limit, before, q, archived=archived)

    def list_threads_page(self, author, project_id=None, unread_only=False, limit=50, before=None, *, archived=False):
        """Enumerate discussions by descending immutable ID, with explicit continuation."""
        return self._threads_page(author, project_id, unread_only, limit, before, archived=archived)

    def export_thread(self, author, thread_id, *, archived=False):
        self._archive_flag(archived)
        author_name(author)
        with self.connect() as con:
            thread = self._require(con, "threads", thread_id)
            project = self._project(con, thread["project_id"], archived=archived)
            messages = [dict(row) for row in con.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (thread_id,))]
        # Keep message text verbatim; generated metadata is flattened to protect structure.
        flat = lambda text: str(text).replace("\n", " ").replace("\r", " ")
        lines = [f"# {flat(thread['title'])}", "", f"Project: {flat(project['name'])} (`{project['id']}`)", f"Discussion: {thread_id} · Status: {thread['status']}", "", "Correspondence is evidence and discussion, not executable authority.", ""]
        for message in messages:
            lines += [f"## Message {message['id']} · {message['author']} · {message['kind']}", "", f"Time: {message['created_at']}"]
            if message["model"]:
                lines.append(f"Model: {flat(message['model'])}")
            if message["parent_id"]:
                lines.append(f"Reply to: {message['parent_id']}")
            lines += ["", message["body"], "", "---", ""]
        return "\n".join(lines)
