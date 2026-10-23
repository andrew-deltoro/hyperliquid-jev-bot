from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Journal:
    """One local process; WAL journal with durable budget and routing state."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY,
                created_at REAL NOT NULL,
                source TEXT NOT NULL,
                instrument TEXT NOT NULL,
                bar_ms INTEGER NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(source, instrument, bar_ms)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                created_at REAL NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY,
                reserved_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS calls_at ON calls(reserved_at);
            PRAGMA user_version=1;
        """)
        self.db.commit()

    def seen(self, source: str, instrument: str, bar_ms: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM decisions WHERE source=? AND instrument=? AND bar_ms=?",
                (source, instrument, bar_ms),
            ).fetchone()
            is not None
        )

    def record(self, source: str, instrument: str, bar_ms: int, status: str, payload: dict):
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO decisions
                (created_at,source,instrument,bar_ms,status,payload) VALUES (?,?,?,?,?,?)""",
                (
                    time.time(),
                    source,
                    instrument,
                    bar_ms,
                    status,
                    json.dumps(payload, allow_nan=False),
                ),
            )

    def event(self, kind: str, detail: dict):
        with self.db:
            self.db.execute(
                "INSERT INTO events(created_at,kind,detail) VALUES (?,?,?)",
                (time.time(), kind, json.dumps(detail, allow_nan=False)),
            )

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value):
        with self.db:
            self.db.execute(
                "INSERT INTO state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, allow_nan=False)),
            )

    def reserve_call(self, limit: int, now: float | None = None) -> bool:
        """Atomic rolling-hour cap. Failed API calls still consume a reservation."""
        now = time.time() if now is None else now
        self.db.execute("BEGIN IMMEDIATE")
        try:
            count = self.db.execute(
                "SELECT count(*) FROM calls WHERE reserved_at>?", (now - 3600,)
            ).fetchone()[0]
            if count >= limit:
                self.db.rollback()
                return False
            self.db.execute("INSERT INTO calls(reserved_at) VALUES (?)", (now,))
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def recent(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def summary(self) -> dict:
        counts = self.db.execute(
            "SELECT source,status,count(*) AS count FROM decisions GROUP BY source,status"
        ).fetchall()
        return {
            "analysis_only": True,
            "orders_submitted": 0,
            "counts": [dict(row) for row in counts],
            "recent": self.recent(10),
        }

    def close(self):
        self.db.close()
