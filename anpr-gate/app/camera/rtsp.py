from __future__ import annotations

import os
import logging
import threading
import time

import cv2
import numpy as np

logger = logging.getLogger("anpr.camera.rtsp")


class RtspCamera:
    def __init__(
        self,
        url: str,
        reconnect_seconds: float = 2.0,
        max_reconnect_seconds: float = 30.0,
        name: str = "rtsp",
    ) -> None:
        if not url:
            raise ValueError("RTSP_URL is required when CAMERA_TYPE=rtsp")
        self.url = url
        self.reconnect_seconds = reconnect_seconds
        self.max_reconnect_seconds = max_reconnect_seconds
        self.name = name
        self._capture: cv2.VideoCapture | None = None
        self._capture_lock = threading.Lock()
        self._frame_condition = threading.Condition()
        self._latest_frame: np.ndarray | None = None
        self._frame_sequence = 0
        self._read_sequence = 0
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._reconnect_count = 0
        self._consecutive_failures = 0
        self._last_frame_at: float | None = None

    def _create_capture(self) -> cv2.VideoCapture:
        # A wired camera should use TCP so packet loss cannot corrupt H.265
        # reference frames. The longer timeout prevents needless reconnects
        # during a keyframe interval or a brief camera/network pause.
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        return cv2.VideoCapture(
            self.url,
            cv2.CAP_FFMPEG,
            [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                15000,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                15000,
            ],
        )

    def open(self) -> bool:
        self.release()
        self._stop_event.clear()
        with self._frame_condition:
            self._latest_frame = None
            self._frame_sequence = 0
            self._read_sequence = 0
        self._worker = threading.Thread(
            target=self._capture_loop,
            name="rtsp-capture",
            daemon=True,
        )
        self._worker.start()
        with self._frame_condition:
            return self._frame_condition.wait_for(
                lambda: self._latest_frame is not None or self._stop_event.is_set(),
                timeout=8,
            )

    def _capture_loop(self) -> None:
        while not self._stop_event.is_set():
            capture = self._create_capture()
            with self._capture_lock:
                self._capture = capture

            if not capture.isOpened():
                self._reconnect_count += 1
                self._consecutive_failures += 1
                retry_delay = self._retry_delay()
                logger.warning(
                    "RTSP connection failed; retry in %.0fs",
                    retry_delay,
                    extra={"camera_id": self.name, "reconnect_seconds": retry_delay},
                )
                self._close_capture(capture)
                self._stop_event.wait(retry_delay)
                continue

            self._consecutive_failures = 0
            logger.info(
                "RTSP connected",
                extra={"camera_id": self.name, "transport": "tcp"},
            )
            while not self._stop_event.is_set():
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                with self._frame_condition:
                    self._latest_frame = frame
                    self._frame_sequence += 1
                    self._last_frame_at = time.time()
                    self._frame_condition.notify_all()

            self._close_capture(capture)
            if not self._stop_event.is_set():
                self._reconnect_count += 1
                self._consecutive_failures += 1
                retry_delay = self._retry_delay()
                logger.warning(
                    "RTSP connection lost; retry in %.0fs",
                    retry_delay,
                    extra={"camera_id": self.name, "reconnect_seconds": retry_delay},
                )
                self._stop_event.wait(retry_delay)

    def _retry_delay(self) -> float:
        multiplier = 2 ** min(max(self._consecutive_failures - 1, 0), 4)
        return min(self.reconnect_seconds * multiplier, self.max_reconnect_seconds)

    def _close_capture(self, capture: cv2.VideoCapture) -> None:
        with self._capture_lock:
            if self._capture is capture:
                self._capture = None
        capture.release()

    def read(self) -> np.ndarray | None:
        with self._frame_condition:
            has_frame = self._frame_condition.wait_for(
                lambda: self._frame_sequence > self._read_sequence
                or self._stop_event.is_set(),
                timeout=1,
            )
            if not has_frame or self._latest_frame is None:
                return None
            self._read_sequence = self._frame_sequence
            return self._latest_frame.copy()

    def snapshot(self) -> tuple[int, np.ndarray] | None:
        """Return the newest frame without consuming the recognition cursor."""
        with self._frame_condition:
            if self._latest_frame is None:
                return None
            return self._frame_sequence, self._latest_frame.copy()

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def last_frame_at(self) -> float | None:
        return self._last_frame_at

    def release(self) -> None:
        self._stop_event.set()
        with self._frame_condition:
            self._frame_condition.notify_all()

        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)
        self._worker = None

        with self._capture_lock:
            capture = self._capture
            self._capture = None
        if capture is not None:
            capture.release()
