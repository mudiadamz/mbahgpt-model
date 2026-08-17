#!/usr/bin/env python3
"""SQLite storage for chat sessions and message history. Stdlib only.

The database is the source of truth for conversation history: the browser sends
one new message at a time and the server rebuilds the context from here, so a
reload (or a second tab) never loses a thread.

Connections are opened per call rather than shared, because the HTTP server is
threaded and SQLite connections are not safe to pass between threads.
"""

import json
import os
import sqlite3

from qwen import HERE

DB_PATH = os.environ.get("OPENROUTER_DB", os.path.join(HERE, "chats.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL DEFAULT '',
    model      TEXT,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL DEFAULT '',
    reasoning  TEXT,
    sources    TEXT,   -- JSON array of {title, url} from a web search
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS messages_by_session ON messages (session_id, id);

-- Single-row-per-key settings, e.g. the user's response instructions.
CREATE TABLE IF NOT EXISTS prefs (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    pinned     INTEGER NOT NULL DEFAULT 0,
    session_id INTEGER,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Case-insensitive dedupe, so saying the same thing twice stores it once.
CREATE UNIQUE INDEX IF NOT EXISTS memories_unique ON memories (lower(text));

-- Files sent with a message. Bytes live here rather than on disk so a single
-- chats.db stays the whole backup.
CREATE TABLE IF NOT EXISTS attachments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    mime       TEXT    NOT NULL,
    kind       TEXT    NOT NULL,   -- 'image' | 'document'
    data       BLOB    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS attachments_by_message ON attachments (message_id);
"""

DEFAULT_INSTRUCTIONS = ""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")   # so deleting a session drops its messages
    conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        migrate(conn)


def migrate(conn):
    """Add columns introduced after a database was first created."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "sources" not in have:
        conn.execute("ALTER TABLE messages ADD COLUMN sources TEXT")


def now():
    return "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


# -- sessions ---------------------------------------------------------------
def create_session(model=None, title=""):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (title, model) VALUES (?, ?)", (title, model))
        return cur.lastrowid


def list_sessions():
    with connect() as conn:
        rows = conn.execute("""
            SELECT s.id, s.title, s.model, s.created_at, s.updated_at,
                   COUNT(m.id) AS messages
              FROM sessions s
              LEFT JOIN messages m ON m.session_id = s.id
             GROUP BY s.id
             ORDER BY s.updated_at DESC, s.id DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def rename_session(session_id, title):
    with connect() as conn:
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?",
                     (title.strip()[:120], session_id))


def delete_session(session_id):
    with connect() as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cur.rowcount > 0


def touch_session(session_id, model=None):
    """Bump updated_at so the sidebar keeps most-recent-first order."""
    with connect() as conn:
        if model:
            conn.execute(
                "UPDATE sessions SET updated_at = " + now() + ", model = ? WHERE id = ?",
                (model, session_id))
        else:
            conn.execute(
                "UPDATE sessions SET updated_at = " + now() + " WHERE id = ?",
                (session_id,))


def autotitle(session_id, text):
    """Name an untitled session after its first message."""
    title = " ".join(text.split())[:60]
    if not title:
        return
    with connect() as conn:
        conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ? AND title = ''",
            (title, session_id))


# -- messages ---------------------------------------------------------------
def add_message(session_id, role, content, reasoning=None, sources=None):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, reasoning, sources)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, reasoning or None,
             json.dumps(sources) if sources else None))
        return cur.lastrowid


def get_messages(session_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, content, reasoning, sources, created_at FROM messages"
            " WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    out = []
    for r in rows:
        m = dict(r)
        try:
            m["sources"] = json.loads(m["sources"]) if m["sources"] else []
        except ValueError:
            m["sources"] = []
        out.append(m)
    files = list_attachments([m["id"] for m in out])
    for m in out:
        m["attachments"] = files.get(m["id"], [])
    return out


# -- preferences ------------------------------------------------------------
def get_pref(key, default=""):
    with connect() as conn:
        row = conn.execute("SELECT value FROM prefs WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_pref(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO prefs (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))


# -- memories ---------------------------------------------------------------
def add_memory(text, session_id=None, pinned=0):
    """Store a fact. Returns its id, or None if an identical one exists."""
    text = " ".join(text.split())
    if not text:
        return None
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO memories (text, session_id, pinned)"
            " VALUES (?, ?, ?)", (text, session_id, 1 if pinned else 0))
        return cur.lastrowid if cur.rowcount else None


def list_memories():
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, text, pinned, session_id, created_at FROM memories"
            " ORDER BY pinned DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def update_memory(memory_id, text=None, pinned=None):
    sets, params = [], []
    if text is not None:
        sets.append("text = ?")
        params.append(" ".join(text.split()))
    if pinned is not None:
        sets.append("pinned = ?")
        params.append(1 if pinned else 0)
    if not sets:
        return False
    params.append(memory_id)
    with connect() as conn:
        cur = conn.execute(
            "UPDATE memories SET " + ", ".join(sets) + " WHERE id = ?", params)
        return cur.rowcount > 0


def delete_memory(memory_id):
    with connect() as conn:
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cur.rowcount > 0


# -- attachments ------------------------------------------------------------
def add_attachment(message_id, name, mime, kind, data):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO attachments (message_id, name, mime, kind, data)"
            " VALUES (?, ?, ?, ?, ?)",
            (message_id, name, mime, kind, sqlite3.Binary(data)))
        return cur.lastrowid


def list_attachments(message_ids):
    """Metadata only — the bytes are served separately, by id."""
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, message_id, name, mime, kind, length(data) AS size"
            " FROM attachments WHERE message_id IN (%s) ORDER BY id" % marks,
            list(message_ids)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["message_id"], []).append(dict(r))
    return out


def get_attachment(attachment_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT name, mime, kind, data FROM attachments WHERE id = ?",
            (attachment_id,)).fetchone()
    return dict(row) if row else None


def attachments_for_message(message_id):
    """Full rows, bytes included — used when building the model request."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT name, mime, kind, data FROM attachments"
            " WHERE message_id = ? ORDER BY id", (message_id,)).fetchall()
    return [dict(r) for r in rows]


def history_for_api(session_id):
    """Messages shaped for OpenRouter: content only, no empty turns."""
    return [{"role": m["role"], "content": m["content"]}
            for m in get_messages(session_id) if m["content"]]


if __name__ == "__main__":
    init()
    print("initialized", DB_PATH)
