from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class CameraSettings:
    camera_id: str
    label: str
    rtsp_url: str
    gate_open_url: str


@dataclass(frozen=True)
class Settings:
    cameras: tuple[CameraSettings, CameraSettings]
    gate_trigger_type: str
    anpr_process_fps: float
    min_confidence: float
    min_plate_length: int
    detection_confirm_frames: int
    detection_confirm_seconds: float
    trigger_cooldown_seconds: float
    database_path: Path
    api_host: str
    api_port: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        entry_url = os.getenv("ENTRY_RTSP_URL", os.getenv("RTSP_URL", "")).strip()
        exit_url = os.getenv("EXIT_RTSP_URL", entry_url).strip()
        trigger_type = os.getenv("GATE_TRIGGER_TYPE", "console").strip().lower()
        if trigger_type not in {"console", "http"}:
            raise ValueError("GATE_TRIGGER_TYPE must be 'console' or 'http'")

        cameras = (
            CameraSettings(
                camera_id="entry",
                label="Giriş",
                rtsp_url=entry_url,
                gate_open_url=os.getenv("ENTRY_GATE_OPEN_URL", "").strip(),
            ),
            CameraSettings(
                camera_id="exit",
                label="Çıkış",
                rtsp_url=exit_url,
                gate_open_url=os.getenv("EXIT_GATE_OPEN_URL", "").strip(),
            ),
        )
        if trigger_type == "http":
            missing = [camera.camera_id for camera in cameras if not camera.gate_open_url]
            if missing:
                raise ValueError(
                    "Gate OPEN URL is required for HTTP mode: " + ", ".join(missing)
                )

        return cls(
            cameras=cameras,
            gate_trigger_type=trigger_type,
            anpr_process_fps=float(os.getenv("ANPR_PROCESS_FPS", "1.5")),
            min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.80")),
            min_plate_length=int(os.getenv("MIN_PLATE_LENGTH", "5")),
            detection_confirm_frames=int(os.getenv("DETECTION_CONFIRM_FRAMES", "2")),
            detection_confirm_seconds=float(
                os.getenv("DETECTION_CONFIRM_SECONDS", "2")
            ),
            trigger_cooldown_seconds=float(
                os.getenv("TRIGGER_COOLDOWN_SECONDS", "15")
            ),
            database_path=Path(os.getenv("DATABASE_PATH", "data/anpr.db")),
            api_host=os.getenv("API_HOST", "0.0.0.0"),
            api_port=int(os.getenv("API_PORT", "8000")),
        )
