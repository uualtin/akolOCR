from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .db import Database, DatabaseUnavailable
from .logging_config import configure_logging
from .prod_config import ProductionSettings
from .snapshots import SnapshotStore

logger = logging.getLogger("anpr.archive")


class ArchiveService:
    def __init__(self) -> None:
        self.settings = ProductionSettings.from_env()
        self.database = Database(self.settings.database_url)
        self.stop = False
        self.last_daily: str | None = None

    def write_health(self, **updates) -> None:
        path = self.settings.archive_root / ".anpr-health.json"
        current = {}
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        current.update(updates)
        current["updated_at"] = datetime.now(UTC).isoformat()
        fd, temporary = tempfile.mkstemp(prefix=".health-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(current, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def sync(self) -> None:
        self.settings.archive_root.mkdir(parents=True, exist_ok=True)
        for source in self.settings.local_spool_root.rglob("*.jpg"):
            if not source.is_file():
                continue
            relative = source.relative_to(self.settings.local_spool_root)
            target = self.settings.archive_root / "snapshots" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=target.parent)
            os.close(fd)
            temporary_path = Path(temporary)
            try:
                shutil.copy2(source, temporary_path)
                if source.stat().st_size != temporary_path.stat().st_size:
                    raise IOError("archive size verification failed")
                if SnapshotStore.checksum(source) != SnapshotStore.checksum(temporary_path):
                    raise IOError("archive checksum verification failed")
                os.replace(temporary_path, target)
                event_id, kind_part = source.name.split("_", 1)
                kind = "full" if kind_part.startswith("full") else "crop"
                self.database.archive_snapshot(
                    event_id, kind, f"archive:snapshots/{relative}"
                )
                source.unlink()
            except (OSError, DatabaseUnavailable):
                logger.exception("snapshot archive failed", extra={"event_id": source.name[:36]})
            finally:
                temporary_path.unlink(missing_ok=True)
        self.write_health(last_sync_at=datetime.now(UTC).isoformat(), last_error=None)

    def expire_snapshots(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=self.settings.snapshot_retention_days)
        snapshot_root = self.settings.archive_root / "snapshots"
        if not snapshot_root.exists():
            return
        for path in snapshot_root.rglob("*.jpg"):
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if modified >= cutoff:
                continue
            event_id, kind_part = path.name.split("_", 1)
            kind = "full" if kind_part.startswith("full") else "crop"
            try:
                self.database.update_snapshot(event_id, kind, None, "expired")
                path.unlink(missing_ok=True)
            except DatabaseUnavailable:
                logger.exception("snapshot expiry database update failed", extra={"event_id": event_id})

    def backup_database(self) -> None:
        parsed = urlsplit(self.settings.database_url)
        backup_dir = self.settings.archive_root / "backups" / "postgres" / "daily"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = backup_dir / f"anpr_gate_{stamp}.sql.gz"
        environment = os.environ.copy()
        environment["PGPASSWORD"] = unquote(parsed.password or "")
        command = [
            "pg_dump",
            "--host", parsed.hostname or "postgres",
            "--port", str(parsed.port or 5432),
            "--username", unquote(parsed.username or "anpr_gate"),
            "--dbname", parsed.path.lstrip("/"),
            "--no-owner",
            "--no-privileges",
        ]
        with gzip.open(target, "wb", compresslevel=6) as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, env=environment, check=False)
        if result.returncode:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"pg_dump failed: {result.stderr.decode(errors='replace')[:300]}")
        # Detect truncated/corrupt gzip output before rotating or reporting it.
        try:
            with gzip.open(target, "rb") as backup:
                for _ in iter(lambda: backup.read(1024 * 1024), b""):
                    pass
        except (OSError, EOFError):
            target.unlink(missing_ok=True)
            raise

        monthly = self.settings.archive_root / "backups" / "postgres" / "monthly"
        monthly.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        monthly_target = monthly / f"anpr_gate_{now.strftime('%Y%m')}.sql.gz"
        if not monthly_target.exists():
            shutil.copy2(target, monthly_target)
        for path in backup_dir.glob("*.sql.gz"):
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) < now - timedelta(days=30):
                path.unlink()
        for path in monthly.glob("*.sql.gz"):
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) < now - timedelta(days=366):
                path.unlink()
        self.write_health(last_backup_at=datetime.now(UTC).isoformat(), last_backup=str(target))

    def run(self) -> None:
        while not self.stop:
            try:
                self.database.initialize()
                self.sync()
                today = datetime.now(UTC).date().isoformat()
                if self.last_daily != today:
                    self.expire_snapshots()
                    self.backup_database()
                    self.last_daily = today
            except Exception:
                logger.exception("archive cycle failed")
                try:
                    self.write_health(last_error="archive cycle failed")
                except OSError:
                    pass
            for _ in range(3600):
                if self.stop:
                    return
                time.sleep(1)


def main() -> None:
    configure_logging()
    service = ArchiveService()
    signal.signal(signal.SIGTERM, lambda *_: setattr(service, "stop", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(service, "stop", True))
    service.run()


if __name__ == "__main__":
    main()
