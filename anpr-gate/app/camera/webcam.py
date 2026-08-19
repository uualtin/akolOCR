from __future__ import annotations

import cv2
import numpy as np

from .base import Camera


class WebcamCamera(Camera):
    def __init__(self, index: int) -> None:
        self.index = index
        self._capture: cv2.VideoCapture | None = None

    def open(self) -> bool:
        self.release()
        # On macOS, explicitly use AVFoundation and open only the configured
        # device index. This avoids backend probing/fallback behaviour.
        self._capture = cv2.VideoCapture(self.index, cv2.CAP_AVFOUNDATION)
        connected = self._capture.isOpened()
        if connected:
            print(f"[CAMERA] webcam connected index={self.index}", flush=True)
        else:
            print(f"[CAMERA] webcam connection failed index={self.index}", flush=True)
        return connected

    def read(self) -> np.ndarray | None:
        if self._capture is None or not self._capture.isOpened():
            return None
        ok, frame = self._capture.read()
        return frame if ok else None

    def release(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
