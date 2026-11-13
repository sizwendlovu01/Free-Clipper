"""SQLite storage. One short-lived connection per call keeps it safe across worker threads.

WAL mode lets the web server read while a worker writes; busy_timeout makes concurrent writers wait
instead of failing; job claiming is a single atomic UPDATE so a job is never processed twice.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    api_token_hash TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    error TEXT,
    notes TEXT,
    options TEXT NOT NULL DEFAULT '{}',
    language TEXT,
    duration REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start REAL NOT NULL,
    "end" REAL NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    hook TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    hashtags TEXT NOT NULL DEFAULT '[]',
    template_id INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clips_job ON clips(job_id, idx);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status, updated_at);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT NOT NULL,
    style TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL; a power cut can lose only the last commit
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def query(sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def one(sql: str, args: tuple | list = ()) -> dict[str, Any] | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple | list = ()) -> int:
    with connect() as conn:
        cur = conn.execute(sql, args)
        # New row id for INSERTs; number of affected rows for UPDATE/DELETE.
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


def get_setting(key: str, default: Any = None) -> Any:
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


DEFAULT_TEMPLATES = [
    ("Bold Pop", {
        "font": "Arial", "font_size": 88, "bold": True, "uppercase": True,
        "text_color": "#FFFFFF", "highlight_color": "#FFE600", "outline_color": "#000000",
        "outline": 7, "shadow": 2, "position": "bottom", "margin_v": 560,
        "words_per_line": 3, "highlight_mode": "word", "animation": "pop",
        "box": False, "box_color": "#000000", "box_opacity": 0.6,
    }),
    ("Clean Subtitle", {
        "font": "Arial", "font_size": 58, "bold": True, "uppercase": False,
        "text_color": "#FFFFFF", "highlight_color": "#FFFFFF", "outline_color": "#000000",
        "outline": 3, "shadow": 0, "position": "bottom", "margin_v": 300,
        "words_per_line": 7, "highlight_mode": "none", "animation": "none",
        "box": True, "box_color": "#000000", "box_opacity": 0.55,
    }),
    ("Neon Karaoke", {
        "font": "Arial", "font_size": 80, "bold": True, "uppercase": True,
        "text_color": "#FFFFFF", "highlight_color": "#39FF88", "outline_color": "#101010",
        "outline": 6, "shadow": 3, "position": "bottom", "margin_v": 420,
        "words_per_line": 4, "highlight_mode": "word", "animation": "none",
        "box": False, "box_color": "#000000", "box_opacity": 0.6,
    }),
]


def init() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM templates WHERE user_id IS NULL").fetchone()[0] == 0:
            for name, style in DEFAULT_TEMPLATES:
                conn.execute(
                    "INSERT INTO templates(user_id, name, style, created_at) VALUES(NULL, ?, ?, ?)",
                    (name, json.dumps(style), now()),
                )
        # v2: built-in "Bold Pop" moved from screen-center (covers faces) to the lower third.
        # Only applied when the style is still exactly the old default.
        for row in conn.execute("SELECT id, style FROM templates WHERE user_id IS NULL AND name = 'Bold Pop'"):
            style = json.loads(row[1])
            if style.get("position") == "middle" and style.get("margin_v") == 220:
                style.update(position="bottom", margin_v=560)
                conn.execute("UPDATE templates SET style = ? WHERE id = ?", (json.dumps(style), row[0]))
        # Additive column migrations: safe to run on every start.
        _add_column(conn, "jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "jobs", "started_at", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at)")
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            log.critical("Database integrity check failed: %s. Restore from a backup (see RAILWAY_DEPLOYMENT.md).",
                         result)


log = logging.getLogger("freeclipper.db")


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def claim_job() -> str | None:
    """Atomically move the oldest queued job to 'running'. Safe across threads and processes."""
    rows = query(
        "UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = ?, updated_at = ? "
        "WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1) "
        "AND status = 'queued' RETURNING id", (now(), now()))
    return rows[0]["id"] if rows else None


def claim_clip() -> str | None:
    """Atomically pick one queued single-clip re-render whose project isn't being processed."""
    rows = query(
        "UPDATE clips SET status = 'rendering', updated_at = ? WHERE id = ("
        "  SELECT c.id FROM clips c JOIN jobs j ON j.id = c.job_id"
        "  WHERE c.status = 'queued' AND j.status IN ('done', 'failed') ORDER BY c.updated_at LIMIT 1"
        ") AND status = 'queued' RETURNING id", (now(),))
    return rows[0]["id"] if rows else None


def backup_to(path: str) -> None:
    """Consistent online copy of the database (safe while the app is writing)."""
    src = sqlite3.connect(config.DB_PATH, timeout=30)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
