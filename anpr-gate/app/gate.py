from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class GateResult:
    status: str
    duration_ms: int
    error: str | None = None


class ProductionGate:
    def __init__(self, driver: str, url: str = "", token: str = "") -> None:
        if driver not in {"disabled", "http"}:
            raise ValueError("GATE_DRIVER must be disabled or http")
        self.driver = driver
        self.url = url
        self.token = token

    @property
    def ready(self) -> bool:
        return self.driver == "http" and bool(self.url and self.token)

    def open(
        self,
        *,
        event_id: str,
        gate_id: str,
        source: str,
        plate: str | None = None,
        reason: str | None = None,
    ) -> GateResult:
        if not self.ready:
            return GateResult("disabled", 0, "gate driver is not configured")
        body = json.dumps(
            {
                "event_id": event_id,
                "gate_id": gate_id,
                "source": source,
                "plate": plate,
                "reason": reason,
            }
        ).encode()
        request = Request(
            self.url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Idempotency-Key": event_id,
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urlopen(request, timeout=3) as response:
                response.read(1024)
            return GateResult("success", round((time.monotonic() - started) * 1000))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            message = f"{type(exc).__name__}: {exc}"[:300]
            return GateResult(
                "failed", round((time.monotonic() - started) * 1000), message
            )
