from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import GateTrigger

logger = logging.getLogger("anpr.gate")


class HttpGateTrigger(GateTrigger):
    def __init__(self, url: str, gate_id: str = "gate", timeout_seconds: float = 3.0) -> None:
        if not url:
            raise ValueError("GATE_TRIGGER_URL is required when TRIGGER_TYPE=http")
        self.url = url
        self.gate_id = gate_id
        self.timeout_seconds = timeout_seconds

    def open(self, plate: str) -> None:
        request = Request(
            self.url,
            data=json.dumps({"plate": plate}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response.read()
            logger.info(
                "gate=%s action=OPEN plate=%s",
                self.gate_id,
                plate,
                extra={"camera_id": self.gate_id},
            )
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.error(
                "gate=%s action=OPEN plate=%s error=%s",
                self.gate_id,
                plate,
                exc,
                extra={"camera_id": self.gate_id},
            )
