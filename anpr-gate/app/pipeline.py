from __future__ import annotations

import time

from .anpr.normalize import normalize_plate
from .anpr.recognizer import Detection
from .authorization.service import AuthorizationService
from .trigger.base import GateTrigger


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
        min_confidence: float,
        authorization_enabled: bool,
        authorization: AuthorizationService,
        trigger: GateTrigger,
        cooldown_seconds: float,
        min_plate_length: int = 5,
        detection_confirm_frames: int = 2,
        detection_confirm_seconds: float = 2.0,
    ) -> None:
        self.min_confidence = min_confidence
        self.authorization_enabled = authorization_enabled
        self.authorization = authorization
        self.trigger = trigger
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
        hits += 1
        self._candidates[plate] = (hits, now)

        expired = [
            candidate
            for candidate, (_, seen_at) in self._candidates.items()
            if now - seen_at > self.detection_confirm_seconds
        ]
        for candidate in expired:
            self._candidates.pop(candidate, None)

        return hits >= self.detection_confirm_frames

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

            if not self._is_confirmed(plate):
                print(f"[CANDIDATE] plate={plate} waiting=confirmation", flush=True)
                continue

            if self.cooldown.is_active(plate):
                continue

            print(
                f"[ANPR] plate={plate} confidence={detection.confidence:.2f}",
                flush=True,
            )

            if self.authorization_enabled:
                if not self.authorization.is_allowed(plate):
                    print(f"[DENIED] plate={plate}", flush=True)
                    continue
                print(f"[AUTHORIZED] plate={plate}", flush=True)

            self.trigger.open(plate)
            self.cooldown.mark(plate)

        return normalized_detections
