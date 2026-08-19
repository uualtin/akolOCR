from __future__ import annotations

import logging
import time

from .anpr.normalize import normalize_plate
from .anpr.recognizer import Detection
from .authorization.database import PlateDatabase
from .trigger.base import GateTrigger

logger = logging.getLogger("anpr.pipeline")


class Cooldown:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._last_triggered: dict[str, float] = {}

    def is_active(self, plate: str) -> bool:
        last_triggered = self._last_triggered.get(plate)
        return last_triggered is not None and time.monotonic() - last_triggered < self.seconds

    def mark(self, plate: str) -> None:
        self._last_triggered[plate] = time.monotonic()


class AnprPipeline:
    def __init__(
        self,
        *,
        database: PlateDatabase,
        trigger: GateTrigger,
        camera_id: str = "system",
        min_confidence: float,
        cooldown_seconds: float,
        min_plate_length: int = 5,
        detection_confirm_frames: int = 2,
        detection_confirm_seconds: float = 2.0,
    ) -> None:
        self.database = database
        self.trigger = trigger
        self.camera_id = camera_id
        self.min_confidence = min_confidence
        self.cooldown = Cooldown(cooldown_seconds)
        self.min_plate_length = min_plate_length
        self.detection_confirm_frames = max(1, detection_confirm_frames)
        self.detection_confirm_seconds = detection_confirm_seconds
        self._candidates: dict[str, tuple[int, float]] = {}

    def _is_confirmed(self, plate: str) -> bool:
        now = time.monotonic()
        hits, last_seen = self._candidates.get(plate, (0, 0.0))
        if now - last_seen > self.detection_confirm_seconds:
            hits = 0
        self._candidates[plate] = (hits + 1, now)
        self._candidates = {
            candidate: value
            for candidate, value in self._candidates.items()
            if now - value[1] <= self.detection_confirm_seconds
        }
        return hits + 1 >= self.detection_confirm_frames

    def process(self, detections: list[Detection]) -> list[Detection]:
        normalized_detections: list[Detection] = []
        for detection in detections:
            plate = normalize_plate(detection.text)
            if not plate or len(plate) < self.min_plate_length:
                continue

            normalized = Detection(plate, detection.confidence, detection.box)
            normalized_detections.append(normalized)
            if detection.confidence < self.min_confidence:
                continue
            if not self._is_confirmed(plate) or self.cooldown.is_active(plate):
                continue
            if not self.database.contains(plate):
                logger.info(
                    "plate denied: %s", plate, extra={"camera_id": self.camera_id}
                )
                continue

            self.database.add_audit(plate)
            logger.info(
                "authorized plate audited: %s",
                plate,
                extra={"camera_id": self.camera_id},
            )
            self.trigger.open(plate)
            self.cooldown.mark(plate)

        return normalized_detections
