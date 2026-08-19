from __future__ import annotations

import sqlite3
from pathlib import Path


class DuplicatePlateError(Exception):
    pass


class PlateDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS authorized_plates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plate TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def list_plates(self) -> list[dict[str, int | str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, plate FROM authorized_plates ORDER BY id"
            ).fetchall()
        return [{"id": row["id"], "plate": row["plate"]} for row in rows]

    def add_plate(self, plate: str) -> dict[str, int | str]:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO authorized_plates (plate) VALUES (?)", (plate,)
                )
                plate_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise DuplicatePlateError(plate) from exc
        return {"id": plate_id, "plate": plate}

    def delete_plate(self, plate: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM authorized_plates WHERE plate = ?", (plate,)
            )
        return cursor.rowcount > 0

    def contains(self, plate: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM authorized_plates WHERE plate = ? LIMIT 1", (plate,)
            ).fetchone()
        return row is not None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection
