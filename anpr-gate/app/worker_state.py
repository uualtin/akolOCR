from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class WorkerState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS access_cache (
                    plate TEXT PRIMARY KEY,
                    owner_label TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS state_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_outbox (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshot_status_outbox (
                    event_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trigger_cooldowns (
                    plate TEXT PRIMARY KEY,
                    triggered_at REAL NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def replace_access(self, entries: list[dict[str, Any]]) -> None:
        now = str(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM access_cache")
            conn.executemany(
                "INSERT INTO access_cache (plate,owner_label,note) VALUES (?,?,?)",
                [
                    (entry["plate"], entry.get("owner_label", ""), entry.get("note", ""))
                    for entry in entries
                ],
            )
            conn.execute(
                "INSERT INTO state_meta (key,value) VALUES ('access_refreshed_at',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now,),
            )

    def access_age_seconds(self) -> float | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key='access_refreshed_at'"
            ).fetchone()
        return None if row is None else max(0.0, time.time() - float(row[0]))

    def is_allowed(self, plate: str, ttl_seconds: int) -> bool:
        age = self.access_age_seconds()
        if age is None or age > ttl_seconds:
            return False
        with self._connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM access_cache WHERE plate=? LIMIT 1", (plate,)
                ).fetchone()
                is not None
            )

    def is_in_cooldown(self, plate: str, cooldown_seconds: float) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT triggered_at FROM trigger_cooldowns WHERE plate=?", (plate,)
            ).fetchone()
        return bool(row and time.time() - float(row[0]) < cooldown_seconds)

    def mark_triggered(self, plate: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO trigger_cooldowns (plate,triggered_at) VALUES (?,?)
                ON CONFLICT(plate) DO UPDATE SET triggered_at=excluded.triggered_at""",
                (plate, time.time()),
            )

    def enqueue_event(self, event: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO event_outbox (id,payload,updated_at) VALUES (?,?,?)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at""",
                (event["id"], json.dumps(event, ensure_ascii=False), time.time()),
            )

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM event_outbox ORDER BY updated_at LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete_event(self, event_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM event_outbox WHERE id=?", (event_id,))

    def outbox_count(self) -> int:
        with self._connect() as conn:
            events = int(conn.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0])
            updates = int(
                conn.execute("SELECT COUNT(*) FROM snapshot_status_outbox").fetchone()[0]
            )
            return events + updates

    def mark_snapshot_evicted(self, event_id: str) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM event_outbox WHERE id=?", (event_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO snapshot_status_outbox (event_id,status,updated_at)
                    VALUES (?, 'evicted', ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                    status=excluded.status,updated_at=excluded.updated_at""",
                    (event_id, time.time()),
                )
                return
            payload = json.loads(row[0])
            payload["snapshot_status"] = "evicted"
            conn.execute(
                "UPDATE event_outbox SET payload=?,updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), time.time(), event_id),
            )

    def pending_snapshot_updates(self, limit: int = 100) -> list[tuple[str, str]]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT event_id,status FROM snapshot_status_outbox ORDER BY updated_at LIMIT ?",
                (limit,),
            ).fetchall()

    def delete_snapshot_update(self, event_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM snapshot_status_outbox WHERE event_id=?", (event_id,)
            )
