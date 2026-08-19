from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _rtsp_url_from_env(
    camera_type: str,
    *,
    url_variable: str = "RTSP_URL",
    path_variable: str = "RTSP_PATH",
    default_path: str = "Streaming/Channels/101",
) -> str:
    explicit_url = os.getenv(url_variable, "").strip()
    if explicit_url:
        return explicit_url

    host = os.getenv("RTSP_HOST", "").strip()
    username = os.getenv("RTSP_USERNAME", "").strip()
    password = os.getenv("RTSP_PASSWORD", "")
    port = int(os.getenv("RTSP_PORT", "554"))
    path = os.getenv(path_variable, default_path).strip().lstrip("/")

    if camera_type != "rtsp":
        return ""
    if not host:
        raise ValueError("RTSP_HOST is required when CAMERA_TYPE=rtsp")
    if not username or not password:
        raise ValueError(
            "RTSP_USERNAME and RTSP_PASSWORD are required when CAMERA_TYPE=rtsp"
        )

    encoded_username = quote(username, safe="")
    encoded_password = quote(password, safe="")
    return f"rtsp://{encoded_username}:{encoded_password}@{host}:{port}/{path}"


@dataclass(frozen=True)
class Settings:
    camera_type: str
    webcam_index: int
    rtsp_url: str
    rtsp_preview_url: str
    display_video: bool
    anpr_process_fps: float
    min_confidence: float
    min_plate_length: int
    detection_confirm_frames: int
    detection_confirm_seconds: float
    authorization_enabled: bool
    trigger_cooldown_seconds: float
    trigger_type: str
    gate_trigger_url: str
    database_path: Path
    api_host: str
    api_port: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        camera_type = os.getenv("CAMERA_TYPE", "webcam").strip().lower()
        trigger_type = os.getenv("TRIGGER_TYPE", "console").strip().lower()

        if camera_type not in {"webcam", "rtsp"}:
            raise ValueError("CAMERA_TYPE must be 'webcam' or 'rtsp'")
        if trigger_type not in {"console", "http"}:
            raise ValueError("TRIGGER_TYPE must be 'console' or 'http'")

        return cls(
            camera_type=camera_type,
            webcam_index=int(os.getenv("WEBCAM_INDEX", "0")),
            rtsp_url=_rtsp_url_from_env(camera_type),
            rtsp_preview_url=_rtsp_url_from_env(
                camera_type,
                url_variable="RTSP_PREVIEW_URL",
                path_variable="RTSP_PREVIEW_PATH",
                default_path="Streaming/Channels/102",
            ),
            display_video=_as_bool(os.getenv("DISPLAY_VIDEO", "true")),
            anpr_process_fps=float(os.getenv("ANPR_PROCESS_FPS", "5")),
            min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.80")),
            min_plate_length=int(os.getenv("MIN_PLATE_LENGTH", "5")),
            detection_confirm_frames=int(os.getenv("DETECTION_CONFIRM_FRAMES", "2")),
            detection_confirm_seconds=float(
                os.getenv("DETECTION_CONFIRM_SECONDS", "2")
            ),
            authorization_enabled=_as_bool(
                os.getenv("AUTHORIZATION_ENABLED", "false")
            ),
            trigger_cooldown_seconds=float(
                os.getenv("TRIGGER_COOLDOWN_SECONDS", "15")
            ),
            trigger_type=trigger_type,
            gate_trigger_url=os.getenv("GATE_TRIGGER_URL", "").strip(),
            database_path=Path(os.getenv("DATABASE_PATH", "data/anpr.db")),
            api_host=os.getenv("API_HOST", "0.0.0.0"),
            api_port=int(os.getenv("API_PORT", "8000")),
        )
