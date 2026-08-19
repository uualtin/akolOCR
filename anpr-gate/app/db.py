from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


class DatabaseUnavailable(RuntimeError):
    pass


class Database:
    def __init__(self, url: str, connect_timeout: int = 5) -> None:
        self.url = url
        self.connect_timeout = connect_timeout

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        try:
            conn = psycopg.connect(
                self.url, row_factory=dict_row, connect_timeout=self.connect_timeout
            )
        except psycopg.Error as exc:
            raise DatabaseUnavailable(str(exc)) from exc
        try:
            try:
                with conn:
                    yield conn
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                raise DatabaseUnavailable(str(exc)) from exc
        finally:
            conn.close()

    def initialize(self) -> None:
        migration = Path(__file__).resolve().parent.parent / "migrations" / "001_initial.sql"
        with self.connect() as conn:
            conn.execute(migration.read_text(encoding="utf-8"))
        now = datetime.now(UTC)
        self.ensure_partitions(now.year, now.month)

    def ensure_partitions(self, year: int, month: int) -> None:
        months: list[tuple[int, int]] = []
        for offset in (-1, 0, 1, 2):
            absolute = year * 12 + month - 1 + offset
            months.append((absolute // 12, absolute % 12 + 1))
        with self.connect() as conn:
            for part_year, part_month in months:
                next_absolute = part_year * 12 + part_month
                next_year, next_month = next_absolute // 12, next_absolute % 12 + 1
                name = f"gate_events_{part_year:04d}_{part_month:02d}"
                start = f"{part_year:04d}-{part_month:02d}-01T00:00:00+00:00"
                end = f"{next_year:04d}-{next_month:02d}-01T00:00:00+00:00"
                conn.execute(
                    sql.SQL(
                        "CREATE TABLE IF NOT EXISTS {} PARTITION OF gate_events "
                        "FOR VALUES FROM ({}) TO ({})"
                    ).format(sql.Identifier(name), sql.Literal(start), sql.Literal(end))
                )

    def health(self) -> bool:
        try:
            with self.connect() as conn:
                return conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        except DatabaseUnavailable:
            return False

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE username=%s LIMIT 1", (username,)
            ).fetchone()

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT id,username,is_active,last_login_at FROM users WHERE id=%s",
                (user_id,),
            ).fetchone()

    def create_user(self, username: str, password_hash: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "INSERT INTO users (username,password_hash) VALUES (%s,%s) RETURNING id",
                (username, password_hash),
            ).fetchone()
            return int(row["id"])

    def login_failed(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE users SET failed_attempts=failed_attempts+1,
                locked_until=CASE WHEN failed_attempts+1 >= 5 THEN now()+interval '15 minutes' ELSE locked_until END,
                updated_at=now() WHERE id=%s""",
                (user_id,),
            )

    def login_succeeded(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE users SET failed_attempts=0,locked_until=NULL,last_login_at=now(),updated_at=now()
                WHERE id=%s""",
                (user_id,),
            )

    def list_cameras(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT c.*,g.name AS gate_name,g.driver AS gate_driver,g.enabled AS gate_enabled
                FROM cameras c JOIN gates g ON g.id=c.gate_id ORDER BY c.direction"""
            ).fetchall()

    def list_gates(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM gates ORDER BY direction").fetchall()

    def get_gate(self, gate_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM gates WHERE id=%s", (gate_id,)).fetchone()

    def recent_events(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM gate_events ORDER BY occurred_at DESC LIMIT %s", (limit,)
            ).fetchall()

    def event_counts_today(self, start_utc: datetime | None = None) -> dict[str, int]:
        with self.connect() as conn:
            if start_utc is None:
                rows = conn.execute(
                    """SELECT trigger_status,COUNT(*) AS count FROM gate_events
                    WHERE occurred_at >= date_trunc('day', now()) GROUP BY trigger_status"""
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT trigger_status,COUNT(*) AS count FROM gate_events
                    WHERE occurred_at >= %s GROUP BY trigger_status""",
                    (start_utc,),
                ).fetchall()
        result = {"total": 0, "success": 0, "failed": 0, "disabled": 0}
        for row in rows:
            result[row["trigger_status"]] = row["count"]
            result["total"] += row["count"]
        return result

    def get_access_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM access_entries WHERE id=%s", (entry_id,)
            ).fetchone()

    def seed_camera_gate(
        self, camera_id: str, camera_name: str, direction: str, gate_id: str, driver: str
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO gates (id, name, direction, driver) VALUES (%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, direction=EXCLUDED.direction, driver=EXCLUDED.driver""",
                (gate_id, f"{camera_name} Gate", direction, driver),
            )
            conn.execute(
                """INSERT INTO cameras (id,name,direction,gate_id) VALUES (%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, direction=EXCLUDED.direction, gate_id=EXCLUDED.gate_id""",
                (camera_id, camera_name, direction, gate_id),
            )

    def active_access_entries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT id,plate,owner_label,note,updated_at FROM access_entries WHERE is_active ORDER BY plate"
            ).fetchall()

    def list_access_entries(self, query: str = "") -> list[dict[str, Any]]:
        with self.connect() as conn:
            if query:
                return conn.execute(
                    """SELECT * FROM access_entries
                    WHERE plate ILIKE %s OR owner_label ILIKE %s ORDER BY plate""",
                    (f"%{query}%", f"%{query}%"),
                ).fetchall()
            return conn.execute("SELECT * FROM access_entries ORDER BY plate").fetchall()

    def add_access_entry(self, plate: str, owner: str, note: str, user_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """INSERT INTO access_entries (plate,owner_label,note,created_by)
                VALUES (%s,%s,%s,%s) RETURNING id""",
                (plate, owner, note, user_id),
            ).fetchone()
            return int(row["id"])

    def import_access_entry(self, plate: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO access_entries (plate,owner_label,note)
                VALUES (%s,'','SQLite migration') ON CONFLICT (plate) DO NOTHING""",
                (plate,),
            )

    def update_access_entry(
        self, entry_id: int, plate: str, owner: str, note: str, is_active: bool
    ) -> bool:
        with self.connect() as conn:
            result = conn.execute(
                """UPDATE access_entries SET plate=%s,owner_label=%s,note=%s,is_active=%s,updated_at=now()
                WHERE id=%s""",
                (plate, owner, note, is_active, entry_id),
            )
            return result.rowcount > 0

    def upsert_event(self, event: dict[str, Any]) -> None:
        event = {**event, "client_ip": event.get("client_ip")}
        occurred_at = event["occurred_at"]
        with self.connect() as conn:
            inserted = conn.execute(
                "INSERT INTO gate_event_keys (id,occurred_at) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                (event["id"], occurred_at),
            ).fetchone()
            if inserted:
                conn.execute(
                    """INSERT INTO gate_events
                    (id,occurred_at,source,camera_id,gate_id,plate,confidence,trigger_status,
                     trigger_duration_ms,trigger_error,full_snapshot_path,crop_snapshot_path,
                     snapshot_status,requested_by,manual_reason,client_ip)
                    VALUES (%(id)s,%(occurred_at)s,%(source)s,%(camera_id)s,%(gate_id)s,%(plate)s,
                     %(confidence)s,%(trigger_status)s,%(trigger_duration_ms)s,%(trigger_error)s,
                     %(full_snapshot_path)s,%(crop_snapshot_path)s,%(snapshot_status)s,
                     %(requested_by)s,%(manual_reason)s,%(client_ip)s)""",
                    event,
                )
            else:
                conn.execute(
                    """UPDATE gate_events SET trigger_status=%(trigger_status)s,
                    trigger_duration_ms=%(trigger_duration_ms)s,trigger_error=%(trigger_error)s,
                    full_snapshot_path=%(full_snapshot_path)s,crop_snapshot_path=%(crop_snapshot_path)s,
                    snapshot_status=%(snapshot_status)s,updated_at=now()
                    WHERE id=%(id)s""",
                    event,
                )

    def list_events(
        self,
        *,
        plate: str = "",
        camera_id: str = "",
        source: str = "",
        trigger_status: str = "",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params = ["1=1"], []
        for value, clause in (
            (plate, "plate=%s"),
            (source, "source=%s"),
            (trigger_status, "trigger_status=%s"),
        ):
            if value:
                clauses.append(clause)
                params.append(value)
        if camera_id:
            clauses.append("(camera_id=%s OR (camera_id IS NULL AND gate_id=%s))")
            params.extend([camera_id, camera_id])
        if date_from:
            clauses.append("occurred_at >= %s")
            params.append(date_from)
        if date_to:
            clauses.append("occurred_at < %s")
            params.append(date_to)
        params.extend([limit, offset])
        with self.connect() as conn:
            return conn.execute(
                f"SELECT * FROM gate_events WHERE {' AND '.join(clauses)} ORDER BY occurred_at DESC LIMIT %s OFFSET %s",
                params,
            ).fetchall()

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM gate_events WHERE id=%s ORDER BY occurred_at DESC LIMIT 1",
                (event_id,),
            ).fetchone()

    def update_snapshot(self, event_id: str, kind: str, path: str | None, status: str) -> None:
        column = "full_snapshot_path" if kind == "full" else "crop_snapshot_path"
        with self.connect() as conn:
            conn.execute(
                sql.SQL("UPDATE gate_events SET {}=%s,snapshot_status=%s,updated_at=now() WHERE id=%s").format(
                    sql.Identifier(column)
                ),
                (path, status, event_id),
            )

    def archive_snapshot(self, event_id: str, kind: str, archive_path: str) -> None:
        column = "full_snapshot_path" if kind == "full" else "crop_snapshot_path"
        with self.connect() as conn:
            conn.execute(
                sql.SQL("UPDATE gate_events SET {}=%s,updated_at=now() WHERE id=%s").format(
                    sql.Identifier(column)
                ),
                (archive_path, event_id),
            )
            row = conn.execute(
                "SELECT full_snapshot_path,crop_snapshot_path FROM gate_events WHERE id=%s LIMIT 1",
                (event_id,),
            ).fetchone()
            status = "archived" if row and all(
                value and value.startswith("archive:")
                for value in (row["full_snapshot_path"], row["crop_snapshot_path"])
            ) else "partial"
            conn.execute(
                "UPDATE gate_events SET snapshot_status=%s,updated_at=now() WHERE id=%s",
                (status, event_id),
            )

    def set_snapshot_status(self, event_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE gate_events SET snapshot_status=%s,updated_at=now() WHERE id=%s",
                (status, event_id),
            )

    def create_audit(
        self,
        actor_id: int | None,
        action: str,
        target_type: str = "",
        target_id: str = "",
        metadata: dict[str, Any] | None = None,
        client_ip: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO admin_audit
                (actor_id,action,target_type,target_id,metadata,client_ip)
                VALUES (%s,%s,%s,%s,%s::jsonb,%s)""",
                (actor_id, action, target_type, target_id, json.dumps(metadata or {}), client_ip),
            )
