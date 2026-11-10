"""SQLite storage. One database file per installation.

All row ids that leave this installation (assignments, segments, ratings) are
UUIDs, so databases from several raters/laptops can later be merged without
id collisions. Each database also has its own `instance_id`.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS annotators (
    code TEXT PRIMARY KEY,
    age INTEGER,
    gender TEXT,
    created_at TEXT NOT NULL,
    instance_id TEXT
);
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    modality TEXT NOT NULL,
    source_type TEXT NOT NULL,
    location TEXT NOT NULL,
    filename TEXT,
    duration_s REAL,
    text TEXT,
    text_hash TEXT,
    metadata TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    annotator_code TEXT NOT NULL REFERENCES annotators(code),
    item_id TEXT NOT NULL REFERENCES items(item_id),
    status TEXT NOT NULL,              -- active | completed
    reserved_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    completed_at TEXT,
    watched_intervals TEXT,            -- JSON [[start, end], ...] (video/audio)
    watched_proportion REAL,
    scrolled_to_end INTEGER,
    reading_time_s REAL,
    no_info INTEGER NOT NULL DEFAULT 0,
    no_info_explanation TEXT,
    instance_id TEXT,
    UNIQUE (annotator_code, item_id)
);
CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES assignments(id),
    start_s REAL,
    end_s REAL,
    char_start INTEGER,
    char_end INTEGER,
    span_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ratings (
    id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL REFERENCES segments(id),
    variable TEXT NOT NULL,
    score REAL NOT NULL,
    explanation TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    annotator_code TEXT,
    action TEXT NOT NULL,
    object_id TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS sources (    -- sources added from the interface
    id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,                  -- JSON, same shape as a config source
    added_at TEXT NOT NULL,
    added_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_assign_item ON assignments(item_id);
CREATE INDEX IF NOT EXISTS idx_seg_assign ON segments(assignment_id);
CREATE INDEX IF NOT EXISTS idx_rating_seg ON ratings(segment_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.executescript(SCHEMA)  # executescript manages its own transaction
        con.close()
        with self.connect() as con:
            row = con.execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO meta(key, value) VALUES ('instance_id', ?)", (new_id(),)
                )
                con.execute(
                    "INSERT INTO meta(key, value) VALUES ('created_at', ?)", (now_iso(),)
                )
        with self.connect() as con:
            self.instance_id = con.execute(
                "SELECT value FROM meta WHERE key='instance_id'"
            ).fetchone()[0]

    @contextmanager
    def connect(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """A short-lived connection; commits on success, rolls back on error.

        `immediate=True` takes the write lock up front, which makes
        read-then-write sequences (like assigning an item) safe under
        concurrent requests.
        """
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        try:
            con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield con
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def audit(
        self,
        con: sqlite3.Connection,
        action: str,
        annotator_code: str | None = None,
        object_id: str | None = None,
        payload: Any = None,
    ) -> None:
        con.execute(
            "INSERT INTO audit_log(ts, annotator_code, action, object_id, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                now_iso(),
                annotator_code,
                action,
                object_id,
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            ),
        )
