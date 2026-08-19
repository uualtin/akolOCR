from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv


def read_secret(name: str, default: str = "") -> str:
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return os.getenv(name, default).strip()


def _database_url() -> str:
    explicit = read_secret("DATABASE_URL")
    if explicit:
        return explicit
    host = os.getenv("DATABASE_HOST", "postgres")
    port = int(os.getenv("DATABASE_PORT", "5432"))
    name = os.getenv("DATABASE_NAME", "anpr_gate")
    user = os.getenv("DATABASE_USER", "anpr_gate")
    password = quote_plus(read_secret("DATABASE_PASSWORD"))
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


@dataclass(frozen=True)
class ProductionSettings:
    database_url: str
    redis_url: str
    session_cookie_name: str
    session_ttl_seconds: int
    secure_cookies: bool
    site_timezone: str
    local_spool_root: Path
    archive_root: Path
    spool_limit_bytes: int
    snapshot_retention_days: int
    access_cache_ttl_seconds: int
    access_refresh_seconds: int
    preview_fps: float
    anpr_process_fps: float
    min_confidence: float
    min_plate_length: int
    confirm_frames: int
    confirm_seconds: float
    cooldown_seconds: float

    @classmethod
    def from_env(cls) -> "ProductionSettings":
        load_dotenv(interpolate=False)
        return cls(
            database_url=_database_url(),
            redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "__Host-anpr_session"),
            session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", "28800")),
            secure_cookies=os.getenv("SECURE_COOKIES", "true").lower()
            in {"1", "true", "yes", "on"},
            site_timezone=os.getenv("SITE_TIMEZONE", "Europe/Istanbul"),
            local_spool_root=Path(os.getenv("LOCAL_SPOOL_ROOT", "/spool")),
            archive_root=Path(os.getenv("ARCHIVE_ROOT", "/archive")),
            spool_limit_bytes=int(os.getenv("SPOOL_LIMIT_BYTES", str(10 * 1024**3))),
            snapshot_retention_days=int(os.getenv("SNAPSHOT_RETENTION_DAYS", "60")),
            access_cache_ttl_seconds=int(os.getenv("ACCESS_CACHE_TTL_SECONDS", "604800")),
            access_refresh_seconds=int(os.getenv("ACCESS_REFRESH_SECONDS", "30")),
            preview_fps=float(os.getenv("PREVIEW_FPS", "10")),
            anpr_process_fps=float(os.getenv("ANPR_PROCESS_FPS", "1.5")),
            min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.80")),
            min_plate_length=int(os.getenv("MIN_PLATE_LENGTH", "5")),
            confirm_frames=int(os.getenv("DETECTION_CONFIRM_FRAMES", "2")),
            confirm_seconds=float(os.getenv("DETECTION_CONFIRM_SECONDS", "2")),
            cooldown_seconds=float(os.getenv("TRIGGER_COOLDOWN_SECONDS", "15")),
        )


@dataclass(frozen=True)
class WorkerSettings:
    worker_id: str
    camera_id: str
    camera_name: str
    direction: str
    gate_id: str
    rtsp_url: str
    gate_driver: str
    gate_url: str
    gate_token: str

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        host = os.getenv("CAMERA_HOST", "").strip()
        username = quote_plus(os.getenv("CAMERA_USERNAME", "admin"))
        password = quote_plus(read_secret("CAMERA_PASSWORD"))
        path = os.getenv("CAMERA_RTSP_PATH", "Streaming/Channels/102").lstrip("/")
        explicit_url = read_secret("CAMERA_RTSP_URL")
        if not explicit_url and not host:
            raise ValueError("CAMERA_HOST or CAMERA_RTSP_URL is required")
        rtsp_url = explicit_url or f"rtsp://{username}:{password}@{host}:554/{path}"
        direction = os.getenv("CAMERA_DIRECTION", "entry").strip().lower()
        if direction not in {"entry", "exit"}:
            raise ValueError("CAMERA_DIRECTION must be entry or exit")
        return cls(
            worker_id=os.getenv("WORKER_ID", direction),
            camera_id=os.getenv("CAMERA_ID", direction),
            camera_name=os.getenv("CAMERA_NAME", direction.title()),
            direction=direction,
            gate_id=os.getenv("GATE_ID", direction),
            rtsp_url=rtsp_url,
            gate_driver=os.getenv("GATE_DRIVER", "disabled").lower(),
            gate_url=os.getenv("GATE_URL", "").strip(),
            gate_token=read_secret("GATE_TOKEN"),
        )
