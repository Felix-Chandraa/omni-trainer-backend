"""Local transactional metadata + append-only event foundation for R2.

SQLite is an engineering baseline, not the approved production datastore.
No truth/AV stream is claimed by this store.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .models import Attempt, State

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, instructor_id TEXT NOT NULL, student_id TEXT NOT NULL,
  created_utc REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS exercises (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  scenario_version TEXT NOT NULL, created_utc REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY, exercise_id TEXT NOT NULL REFERENCES exercises(id),
  aircraft_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation > 0),
  state TEXT NOT NULL, flight_owner TEXT NOT NULL, payload_owner TEXT NOT NULL,
  authority_epoch INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 0,
  ended_reason TEXT, created_utc REAL NOT NULL, updated_utc REAL NOT NULL,
  UNIQUE(exercise_id, generation)
);
CREATE UNIQUE INDEX IF NOT EXISTS active_attempt_per_exercise
  ON attempts(exercise_id) WHERE state != 'Ended';
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT REFERENCES attempts(id),
  event_type TEXT NOT NULL, payload TEXT NOT NULL, occurred_utc REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
  command_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
  fingerprint TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL,
  occurred_utc REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 5000")
        # executescript manages its own transaction; it would implicitly commit
        # a BEGIN issued by our transaction() context manager.
        with self._lock:
            self.db.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def attempt(self, attempt_id: str) -> Attempt:
        with self._lock:
            r = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if r is None:
            raise KeyError(f"unknown attempt: {attempt_id}")
        return Attempt(r["id"], r["exercise_id"], r["aircraft_id"],
                       r["generation"], State(r["state"]), r["flight_owner"],
                       r["payload_owner"], r["authority_epoch"], r["revision"])

    def event(self, db: sqlite3.Connection, attempt_id: str | None, name: str, **data):
        db.execute("INSERT INTO events(attempt_id,event_type,payload,occurred_utc) VALUES(?,?,?,?)",
                   (attempt_id, name, json.dumps(data, sort_keys=True, allow_nan=False), time.time()))

    def events(self, attempt_id: str | None = None) -> list[dict]:
        with self._lock:
            if attempt_id is None:
                rows = self.db.execute("SELECT * FROM events ORDER BY seq").fetchall()
            else:
                rows = self.db.execute("SELECT * FROM events WHERE attempt_id=? ORDER BY seq", (attempt_id,)).fetchall()
        return [dict(seq=r["seq"], attempt_id=r["attempt_id"], event_type=r["event_type"],
                     payload=json.loads(r["payload"]), occurred_utc=r["occurred_utc"]) for r in rows]

    def close(self):
        with self._lock:
            self.db.close()
