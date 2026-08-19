from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from .anpr.recognizer import Detection

SAFE_EVENT_ID = re.compile(r"^[0-9a-f-]{36}$")
SAFE_CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def draw_detection(frame: np.ndarray, detection: Detection) -> np.ndarray:
    output = frame.copy()
    x1, y1, x2, y2 = detection.box
    cv2.rectangle(output, (x1, y1), (x2, y2), (0, 255, 0), 3)
    label = f"{detection.text}  {detection.confidence:.2f}"
    (width, height), baseline = cv2.getTextSize(
        label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2
    )
    top = max(0, y1 - height - baseline - 10)
    cv2.rectangle(output, (x1, top), (x1 + width + 12, y1), (0, 255, 0), -1)
    cv2.putText(
        output,
        label,
        (x1 + 6, y1 - baseline - 5),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )
    return output


class SnapshotStore:
    def __init__(self, root: Path, limit_bytes: int) -> None:
        self.root = root
        self.limit_bytes = limit_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".spool.lock"
        self._lock_path.touch(exist_ok=True)

    def save(
        self,
        event_id: str,
        camera_id: str,
        frame: np.ndarray,
        detection: Detection,
        occurred_at: datetime,
    ) -> tuple[str, str, list[str]]:
        if not SAFE_EVENT_ID.fullmatch(event_id):
            raise ValueError("Unsafe event id")
        if not SAFE_CAMERA_ID.fullmatch(camera_id):
            raise ValueError("Unsafe camera id")
        occurred_at = occurred_at.astimezone(UTC)
        relative_dir = Path(camera_id) / occurred_at.strftime("%Y/%m/%d/%H")
        target_dir = self.root / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        annotated = draw_detection(frame, detection)
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = detection.box
        x1, x2 = sorted((max(0, min(width, x1)), max(0, min(width, x2))))
        y1, y2 = sorted((max(0, min(height, y1)), max(0, min(height, y2))))
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            crop = np.zeros((1, 1, 3), dtype=np.uint8)

        full_relative = relative_dir / f"{event_id}_full.jpg"
        crop_relative = relative_dir / f"{event_id}_crop.jpg"
        self._atomic_jpeg(self.root / full_relative, annotated, 88)
        self._atomic_jpeg(self.root / crop_relative, crop, 92)
        evicted = self.enforce_limit()
        return f"local:{full_relative}", f"local:{crop_relative}", evicted

    def _atomic_jpeg(self, path: Path, frame: np.ndarray, quality: int) -> None:
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded.tobytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def enforce_limit(self) -> list[str]:
        evicted: list[str] = []
        with self._lock_path.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            files = sorted(
                (path for path in self.root.rglob("*.jpg") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
            )
            total = sum(path.stat().st_size for path in files)
            for path in files:
                if total <= self.limit_bytes:
                    break
                size = path.stat().st_size
                event_id = path.name.split("_", 1)[0]
                path.unlink(missing_ok=True)
                total -= size
                if event_id not in evicted:
                    evicted.append(event_id)
            fcntl.flock(lock, fcntl.LOCK_UN)
        return evicted

    @staticmethod
    def checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
