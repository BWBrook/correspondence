"""Shared transactional SQLite store; reads never acknowledge messages."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

AUTHORS = {"Barry", "Astra", "Fable"}
KINDS = {"note", "question", "proposal", "evidence", "decision", "counterexample"}


class NotFound(ValueError):
    pass


class Conflict(ValueError):
    pass


def author_name(author: str) -> str:
    if author not in AUTHORS:
        raise ValueError("Author must be Barry, Astra, or Fable")
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
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    repo_path TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS threads (
                    id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES threads(id),
                    author TEXT NOT NULL CHECK(author IN ('Barry','Astra','Fable')),
                    model TEXT, body TEXT NOT NULL, kind TEXT NOT NULL,
                    parent_id INTEGER REFERENCES messages(id), created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id,id);
                CREATE TABLE IF NOT EXISTS read_markers (
                    author TEXT NOT NULL, thread_id INTEGER NOT NULL REFERENCES threads(id),
                    message_id INTEGER NOT NULL REFERENCES messages(id), PRIMARY KEY(author,thread_id)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    author TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, result_id INTEGER NOT NULL, PRIMARY KEY(author,request_id)
                );
                CREATE TRIGGER IF NOT EXISTS messages_no_update BEFORE UPDATE ON messages
                    BEGIN SELECT RAISE(ABORT, 'Messages are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS messages_no_delete BEFORE DELETE ON messages
                    BEGIN SELECT RAISE(ABORT, 'Messages are append-only'); END;
            """)
            self._upgrade(con)
        self.path.chmod(0o600)

    @staticmethod
    def _fingerprint(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @classmethod
    def _upgrade(cls, con):
        # Version 1 dropped the model label from request fingerprints; recompute stored hashes once.
        if con.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        con.execute("BEGIN IMMEDIATE")
        for row in con.execute("SELECT rowid, operation, result_id FROM requests").fetchall():
            if row["operation"] == "post_message":
                m = con.execute("SELECT thread_id, body, kind, parent_id FROM messages WHERE id=?", (row["result_id"],)).fetchone()
                payload = [m["thread_id"], m["body"], m["kind"], m["parent_id"]]
            else:
                t = con.execute("SELECT project_id, title FROM threads WHERE id=?", (row["result_id"],)).fetchone()
                m = con.execute("SELECT body, kind FROM messages WHERE thread_id=? ORDER BY id LIMIT 1", (row["result_id"],)).fetchone()
                payload = [t["project_id"], t["title"], m["body"], m["kind"]]
            con.execute("UPDATE requests SET fingerprint=? WHERE rowid=?", (cls._fingerprint(payload), row["rowid"]))
        con.execute("PRAGMA user_version=1")
        con.commit()

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
        row = con.execute("SELECT * FROM requests WHERE author=? AND request_id=?", (author, request_id)).fetchone()
        if row:
            if row["operation"] != operation or row["fingerprint"] != fingerprint:
                raise Conflict("request_id has already been used with different content")
            return row["result_id"]
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
        records = {thread["id"]: dict(thread, read_markers={name: 0 for name in ("Barry", "Astra", "Fable")},
                                      unread_counts={name: 0 for name in ("Barry", "Astra", "Fable")},
                                      message_count=0, first_message_id=None, last_message_id=None,
                                      first_unread_message_id=None, last_author=None) for thread in threads}
        placeholders = ",".join("?" for _ in records)
        stats = con.execute(f"""WITH participants(author) AS (VALUES ('Barry'),('Astra'),('Fable'))
            SELECT m.thread_id, p.author participant, COALESCE(r.message_id,0) marker,
                COUNT(*) message_count, MIN(m.id) first_message_id, MAX(m.id) last_message_id,
                SUM(m.id>COALESCE(r.message_id,0) AND m.author!=p.author) unread_count,
                MIN(CASE WHEN m.id>COALESCE(r.message_id,0) AND m.author!=p.author THEN m.id END) first_unread_message_id
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
    def _thread(con, thread_id, author):
        thread = Store._require(con, "threads", thread_id)
        return Store._thread_records(con, [thread], author)[0]

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
                if (existing["name"], existing["description"], existing["repo_path"]) != (name, description, repo_path):
                    raise Conflict("Project slug already exists with different details")
                return dict(existing)
            con.execute("INSERT INTO projects VALUES (?,?,?,?,?)", (id, name, description, repo_path, now()))
            return self._require(con, "projects", id)

    def list_projects(self, author):
        author_name(author)
        with self.connect() as con:
            projects = [dict(row, unread_counts={name: 0 for name in ("Barry", "Astra", "Fable")})
                        for row in con.execute("""SELECT p.*,
                            (SELECT MAX(t.updated_at) FROM threads t WHERE t.project_id=p.id) last_activity_at
                            FROM projects p ORDER BY p.name COLLATE NOCASE, p.id LIMIT 1000""")]
            if not projects:
                return []
            by_id = {project["id"]: project for project in projects}
            placeholders = ",".join("?" for _ in projects)
            counts = con.execute(f"""WITH participants(author) AS (VALUES ('Barry'),('Astra'),('Fable'))
                SELECT t.project_id,p.author participant,COUNT(*) unread_count
                FROM messages m JOIN threads t ON t.id=m.thread_id CROSS JOIN participants p
                LEFT JOIN read_markers r ON r.thread_id=t.id AND r.author=p.author
                WHERE t.project_id IN ({placeholders}) AND m.id>COALESCE(r.message_id,0) AND m.author!=p.author
                GROUP BY t.project_id,p.author""", tuple(by_id))
            for row in counts:
                by_id[row["project_id"]]["unread_counts"][row["participant"]] = row["unread_count"]
            for project in projects:
                project["unread_count"] = project["unread_counts"][author]
            return projects

    def list_threads(self, author, project_id=None, unread_only=False, limit=200):
        author_name(author)
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.connect() as con:
            rows = con.execute("""SELECT t.* FROM threads t WHERE (? IS NULL OR t.project_id=?)
                AND (?=0 OR EXISTS (SELECT 1 FROM messages m LEFT JOIN read_markers r
                ON r.thread_id=t.id AND r.author=? WHERE m.thread_id=t.id AND m.author!=?
                AND m.id>COALESCE(r.message_id,0))) ORDER BY t.updated_at DESC, t.id DESC LIMIT ?""",
                (project_id, project_id, unread_only, author, author, limit)).fetchall()
            return self._thread_records(con, [dict(row) for row in rows], author)

    def create_thread(self, author, project_id, title, body, kind="note", model=None, request_id=None):
        title = clean_text(title, "title", 300)
        body, model = self._message_args(author, body, kind, model)
        payload = [project_id, title, body, kind]
        with self.connect(write=True) as con:
            result_id = self._request(con, author, request_id, "create_thread", payload)
            if result_id is not None:
                return self._thread(con, result_id, author)
            self._require(con, "projects", project_id)
            timestamp = now()
            result_id = con.execute("INSERT INTO threads(project_id,title,created_at,updated_at) VALUES (?,?,?,?)", (project_id,title,timestamp,timestamp)).lastrowid
            self._insert_message(con, result_id, author, model, body, kind)
            self._request(con, author, request_id, "create_thread", payload, result_id)
            return self._thread(con, result_id, author)

    def read_thread(self, author, thread_id, after=0, limit=100, max_bytes=200_000, *, mode="oldest", before=None):
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
            thread = self._thread(con, thread_id, author)
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

    def get_message(self, author, message_id):
        author_name(author)
        with self.connect() as con:
            message = self._require(con, "messages", message_id)
            return {"thread": self._thread(con, message["thread_id"], author), "message": message}

    def post_message(self, author, thread_id, body, kind="note", parent_id=None, model=None, request_id=None):
        body, model = self._message_args(author, body, kind, model)
        payload = [thread_id, body, kind, parent_id]
        with self.connect(write=True) as con:
            result_id = self._request(con, author, request_id, "post_message", payload)
            if result_id is not None:
                return self._require(con, "messages", result_id)
            self._require(con, "threads", thread_id)
            result_id = self._insert_message(con, thread_id, author, model, body, kind, parent_id)
            self._request(con, author, request_id, "post_message", payload, result_id)
            return self._require(con, "messages", result_id)

    def mark_read(self, author, thread_id, message_id):
        author_name(author)
        with self.connect(write=True) as con:
            self._require(con, "threads", thread_id)
            message = self._require(con, "messages", message_id)
            if message["thread_id"] != thread_id:
                raise ValueError("Read marker must reference a message in this discussion")
            con.execute("""INSERT INTO read_markers VALUES (?,?,?) ON CONFLICT(author,thread_id)
                DO UPDATE SET message_id=MAX(read_markers.message_id,excluded.message_id)""", (author, thread_id, message_id))
            marker = con.execute("SELECT message_id FROM read_markers WHERE author=? AND thread_id=?", (author,thread_id)).fetchone()[0]
            return {"thread_id": thread_id, "last_read_message_id": marker}

    def set_thread_status(self, author, thread_id, status, model=None):
        author_name(author)
        if status not in {"open", "resolved"}:
            raise ValueError("status must be open or resolved")
        if model is not None:
            model = clean_text(model, "model", 200)
        with self.connect(write=True) as con:
            thread = self._require(con, "threads", thread_id)
            if thread["status"] != status:
                con.execute("UPDATE threads SET status=? WHERE id=?", (status,thread_id))
                self._insert_message(con, thread_id, author, model, f"Discussion status changed from {thread['status']} to {status}.", "note")
            return self._thread(con, thread_id, author)

    def search_messages(self, author, q, project_id=None, limit=100, *, thread_id=None, before=None):
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
                WHERE (m.body LIKE ? ESCAPE '\\' OR t.title LIKE ? ESCAPE '\\')
                AND (? IS NULL OR t.project_id=?) AND (? IS NULL OR m.thread_id=?)
                AND (? IS NULL OR m.id<?) ORDER BY m.id DESC LIMIT ?""",
                (pattern,pattern,project_id,project_id,thread_id,thread_id,before,before,limit))
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

    def export_thread(self, author, thread_id):
        author_name(author)
        with self.connect() as con:
            thread = self._require(con, "threads", thread_id)
            project = self._require(con, "projects", thread["project_id"])
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
