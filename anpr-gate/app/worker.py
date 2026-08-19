from __future__ import annotations

import json
import logging
import signal
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import cv2
import redis

from .anpr.normalize import normalize_plate
from .anpr.recognizer import Detection, PlateRecognizer
from .camera.rtsp import RtspCamera
from .db import Database, DatabaseUnavailable
from .gate import ProductionGate
from .logging_config import configure_logging
from .prod_config import ProductionSettings, WorkerSettings
from .snapshots import SnapshotStore, draw_detection
from .worker_state import WorkerState

logger = logging.getLogger("anpr.worker")


class DetectionSelector:
    def __init__(
        self,
        min_confidence: float,
        min_length: int,
        confirm_frames: int,
        confirm_seconds: float,
        cooldown_seconds: float,
    ) -> None:
        self.min_confidence = min_confidence
        self.min_length = min_length
        self.confirm_frames = max(1, confirm_frames)
        self.confirm_seconds = confirm_seconds
        self.cooldown_seconds = cooldown_seconds
        self.candidates: dict[str, tuple[int, float]] = {}
        self.cooldowns: dict[str, float] = {}

    def normalize(self, detections: list[Detection]) -> list[Detection]:
        normalized: list[Detection] = []
        for detection in detections:
            plate = normalize_plate(detection.text)
            if plate and len(plate) >= self.min_length:
                normalized.append(Detection(plate, detection.confidence, detection.box))
        return normalized

    def confirmed(self, detections: list[Detection]) -> list[Detection]:
        now = time.monotonic()
        result: list[Detection] = []
        for detection in detections:
            if detection.confidence < self.min_confidence:
                continue
            hits, last_seen = self.candidates.get(detection.text, (0, 0.0))
            hits = 0 if now - last_seen > self.confirm_seconds else hits
            hits += 1
            self.candidates[detection.text] = (hits, now)
            if hits < self.confirm_frames:
                continue
            last_trigger = self.cooldowns.get(detection.text, 0.0)
            if now - last_trigger >= self.cooldown_seconds:
                result.append(detection)
        self.candidates = {
            plate: value
            for plate, value in self.candidates.items()
            if now - value[1] <= self.confirm_seconds
        }
        return result

    def mark(self, plate: str) -> None:
        self.cooldowns[plate] = time.monotonic()


class Worker:
    def __init__(self) -> None:
        self.settings = ProductionSettings.from_env()
        self.worker = WorkerSettings.from_env()
        self.database = Database(self.settings.database_url, connect_timeout=1)
        state_path = Path("/state") / f"{self.worker.worker_id}.sqlite3"
        self.state = WorkerState(Path(__import__("os").getenv("WORKER_STATE_PATH", state_path)))
        self.snapshots = SnapshotStore(
            self.settings.local_spool_root, self.settings.spool_limit_bytes
        )
        self.redis = redis.Redis.from_url(self.settings.redis_url)
        self.gate = ProductionGate(
            self.worker.gate_driver, self.worker.gate_url, self.worker.gate_token
        )
        self.camera = RtspCamera(self.worker.rtsp_url, name=self.worker.camera_id)
        self.recognizer = PlateRecognizer()
        self.selector = DetectionSelector(
            self.settings.min_confidence,
            self.settings.min_plate_length,
            self.settings.confirm_frames,
            self.settings.confirm_seconds,
            self.settings.cooldown_seconds,
        )
        self.stop = threading.Event()
        self.overlay_lock = threading.Lock()
        self.overlay: tuple[list[Detection], tuple[int, int], float] | None = None
        self.last_cache_refresh = 0.0
        self.last_replay = 0.0
        self.database_initialized = False
        self.database_backoff_until = 0.0

    def ensure_database(self) -> None:
        if self.database_initialized:
            return
        self.database.initialize()
        self.database.seed_camera_gate(
            self.worker.camera_id,
            self.worker.camera_name,
            self.worker.direction,
            self.worker.gate_id,
            self.worker.gate_driver,
        )
        self.database_initialized = True

    def _database_startup(self) -> None:
        try:
            self.ensure_database()
            self.refresh_access()
        except DatabaseUnavailable:
            self.database_backoff_until = time.monotonic() + 5
            logger.warning("database unavailable during worker startup")

    def refresh_access(self) -> None:
        entries = self.database.active_access_entries()
        self.state.replace_access(entries)
        self.last_cache_refresh = time.monotonic()

    def replay_outbox(self) -> None:
        for event in self.state.pending_events():
            self.database.upsert_event(event)
            self.state.delete_event(event["id"])
        for event_id, status in self.state.pending_snapshot_updates():
            self.database.set_snapshot_status(event_id, status)
            self.state.delete_snapshot_update(event_id)

    def persist(self, event: dict) -> None:
        if time.monotonic() < self.database_backoff_until:
            self.state.enqueue_event(event)
            return
        try:
            self.database.upsert_event(event)
            self.database_backoff_until = 0.0
        except DatabaseUnavailable:
            self.database_backoff_until = time.monotonic() + 5
            self.state.enqueue_event(event)

    def _mark_evicted(self, event_ids: list[str]) -> None:
        for event_id in event_ids:
            if time.monotonic() < self.database_backoff_until:
                self.state.mark_snapshot_evicted(event_id)
            else:
                try:
                    self.database.set_snapshot_status(event_id, "evicted")
                except DatabaseUnavailable:
                    self.database_backoff_until = time.monotonic() + 5
                    self.state.mark_snapshot_evicted(event_id)
            logger.critical("snapshot evicted before archive", extra={"event_id": event_id})

    def process_detection(self, frame, detection: Detection) -> None:
        if self.state.is_in_cooldown(
            detection.text, self.settings.cooldown_seconds
        ):
            return
        if not self.state.is_allowed(
            detection.text, self.settings.access_cache_ttl_seconds
        ):
            return
        event_id = str(uuid.uuid4())
        occurred_at = datetime.now(UTC)
        try:
            full_path, crop_path, evicted = self.snapshots.save(
                event_id,
                self.worker.camera_id,
                frame,
                detection,
                occurred_at,
            )
        except (OSError, RuntimeError, ValueError):
            logger.critical(
                "snapshot write failed; gate remains closed",
                exc_info=True,
                extra={"camera_id": self.worker.camera_id, "plate": detection.text},
            )
            return
        current_evicted = event_id in evicted
        self._mark_evicted([item for item in evicted if item != event_id])
        if current_evicted:
            logger.critical(
                "new snapshot evicted because spool limit is exhausted",
                extra={"event_id": event_id, "camera_id": self.worker.camera_id},
            )
        event = {
            "id": event_id,
            "occurred_at": occurred_at.isoformat(),
            "source": "automatic",
            "camera_id": self.worker.camera_id,
            "gate_id": self.worker.gate_id,
            "plate": detection.text,
            "confidence": detection.confidence,
            "trigger_status": "pending",
            "trigger_duration_ms": None,
            "trigger_error": None,
            "full_snapshot_path": full_path,
            "crop_snapshot_path": crop_path,
            "snapshot_status": "evicted" if current_evicted else "local",
            "requested_by": None,
            "manual_reason": None,
            "client_ip": None,
        }
        self.persist(event)
        # Persist cooldown before external I/O. If the process dies after the
        # relay accepts the request, a restart cannot immediately open again.
        self.selector.mark(detection.text)
        self.state.mark_triggered(detection.text)
        result = self.gate.open(
            event_id=event_id,
            gate_id=self.worker.gate_id,
            source="automatic",
            plate=detection.text,
        )
        event.update(
            trigger_status=result.status,
            trigger_duration_ms=result.duration_ms,
            trigger_error=result.error,
        )
        self.persist(event)
        logger.info(
            "gate event completed",
            extra={
                "event_id": event_id,
                "camera_id": self.worker.camera_id,
                "gate_id": self.worker.gate_id,
                "plate": detection.text,
            },
        )

    def preview_loop(self) -> None:
        interval = 1 / max(self.settings.preview_fps, 1)
        last_sequence = -1
        while not self.stop.wait(interval):
            snapshot = self.camera.snapshot()
            if snapshot is None or snapshot[0] == last_sequence:
                continue
            last_sequence, frame = snapshot
            height, width = frame.shape[:2]
            with self.overlay_lock:
                overlay = self.overlay
            if overlay is not None and time.monotonic() - overlay[2] < 1.5:
                detections, (source_width, source_height), _ = overlay
                for detection in detections:
                    scaled = Detection(
                        detection.text,
                        detection.confidence,
                        (
                            round(detection.box[0] * width / source_width),
                            round(detection.box[1] * height / source_height),
                            round(detection.box[2] * width / source_width),
                            round(detection.box[3] * height / source_height),
                        ),
                    )
                    frame = draw_detection(frame, scaled)
            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
            if ok:
                try:
                    payload = jpeg.tobytes()
                    pipeline = self.redis.pipeline(transaction=False)
                    pipeline.setex(f"preview:{self.worker.camera_id}", 5, payload)
                    pipeline.publish(f"preview-stream:{self.worker.camera_id}", payload)
                    pipeline.execute()
                except redis.RedisError:
                    pass

    def access_invalidation_loop(self) -> None:
        """Refresh both workers promptly after an access-list mutation."""
        while not self.stop.is_set():
            pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
            try:
                pubsub.subscribe("access-list:changed")
                while not self.stop.wait(0.25):
                    if pubsub.get_message(timeout=0.1):
                        if time.monotonic() < self.database_backoff_until:
                            continue
                        try:
                            self.refresh_access()
                        except DatabaseUnavailable:
                            self.database_backoff_until = time.monotonic() + 5
                            logger.warning("access-list refresh deferred; database unavailable")
            except redis.RedisError:
                self.stop.wait(2)
            finally:
                pubsub.close()

    def heartbeat(self, camera_online: bool) -> None:
        age = self.state.access_age_seconds()
        payload = {
            "worker_id": self.worker.worker_id,
            "camera_id": self.worker.camera_id,
            "camera_name": self.worker.camera_name,
            "direction": self.worker.direction,
            "gate_id": self.worker.gate_id,
            "gate_ready": self.gate.ready,
            "camera_online": camera_online,
            "camera_reconnect_count": self.camera.reconnect_count,
            "last_frame_at": self.camera.last_frame_at,
            "cache_age_seconds": age,
            "outbox_count": self.state.outbox_count(),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        try:
            self.redis.setex(
                f"heartbeat:{self.worker.camera_id}", 15, json.dumps(payload)
            )
        except redis.RedisError:
            pass

    def run(self) -> None:
        self._database_startup()
        camera_online = self.camera.open()
        preview_thread = threading.Thread(
            target=self.preview_loop, name="preview-publisher", daemon=True
        )
        invalidation_thread = threading.Thread(
            target=self.access_invalidation_loop,
            name="access-invalidation",
            daemon=True,
        )
        preview_thread.start()
        invalidation_thread.start()
        interval = 1 / max(self.settings.anpr_process_fps, 0.1)
        next_process = 0.0
        last_heartbeat = 0.0
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if (
                    now >= self.database_backoff_until
                    and now - self.last_cache_refresh >= self.settings.access_refresh_seconds
                ):
                    try:
                        self.ensure_database()
                        self.refresh_access()
                    except DatabaseUnavailable:
                        self.database_backoff_until = time.monotonic() + 5
                        self.last_cache_refresh = now
                if now >= self.database_backoff_until and now - self.last_replay >= 5:
                    try:
                        self.replay_outbox()
                    except DatabaseUnavailable:
                        self.database_backoff_until = time.monotonic() + 5
                    self.last_replay = now
                frame = self.camera.read()
                camera_online = frame is not None
                now = time.monotonic()
                if now - last_heartbeat >= 1:
                    self.heartbeat(camera_online)
                    last_heartbeat = now
                if frame is None or now < next_process:
                    continue
                next_process = now + interval
                detections = self.selector.normalize(self.recognizer.recognize(frame))
                height, width = frame.shape[:2]
                with self.overlay_lock:
                    self.overlay = (detections, (width, height), time.monotonic())
                for detection in self.selector.confirmed(detections):
                    self.process_detection(frame, detection)
        finally:
            self.stop.set()
            self.camera.release()
            preview_thread.join(timeout=3)
            invalidation_thread.join(timeout=3)


def main() -> None:
    configure_logging()
    worker = Worker()
    signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stop.set())
    worker.run()


if __name__ == "__main__":
    main()
