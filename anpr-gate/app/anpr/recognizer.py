from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

import numpy as np
import onnxruntime as ort
from fast_alpr import ALPR


@dataclass(frozen=True)
class Detection:
    text: str
    confidence: float
    box: tuple[int, int, int, int]


class PlateRecognizer:
    def __init__(self) -> None:
        # CoreML cannot execute the detector's dynamic empty output when a frame
        # contains no candidate plates. Prefer ONNX Runtime's CPU provider for
        # predictable behaviour on macOS as well as the other supported hosts.
        providers = ["CPUExecutionProvider"]
        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = 2
        session_options.inter_op_num_threads = 1
        session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self._alpr = ALPR(
            detector_model="yolo-v9-t-384-license-plate-end2end",
            detector_providers=providers,
            detector_sess_options=session_options,
            ocr_model="cct-xs-v2-global-model",
            ocr_device="cpu",
            ocr_providers=providers,
            ocr_sess_options=session_options,
        )

    def recognize(self, frame: np.ndarray) -> list[Detection]:
        detections: list[Detection] = []
        for result in self._alpr.predict(frame):
            if result.ocr is None or not result.ocr.text:
                continue

            raw_confidence = result.ocr.confidence
            if isinstance(raw_confidence, (list, tuple, np.ndarray)):
                confidence = float(mean(raw_confidence)) if len(raw_confidence) else 0.0
            else:
                confidence = float(raw_confidence)

            bbox = result.detection.bounding_box
            detections.append(
                Detection(
                    text=result.ocr.text,
                    confidence=confidence,
                    box=(int(bbox.x1), int(bbox.y1), int(bbox.x2), int(bbox.y2)),
                )
            )
        return detections
