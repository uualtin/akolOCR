from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import GateTrigger


class HttpGateTrigger(GateTrigger):
    def __init__(self, url: str, timeout_seconds: float = 3.0) -> None:
        if not url:
            raise ValueError("GATE_TRIGGER_URL is required when TRIGGER_TYPE=http")
        self.url = url
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
            print(f"[GATE_TRIGGER] HTTP OPEN plate={plate}", flush=True)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print(f"[GATE_TRIGGER] ERROR plate={plate} error={exc}", flush=True)
