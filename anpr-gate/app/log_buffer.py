from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime


class LogStore:
    def __init__(self, max_entries: int = 1000) -> None:
        self._entries: deque[dict[str, str | int]] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._sequence = 0

    def append(self, level: str, source: str, message: str) -> None:
        with self._lock:
            self._sequence += 1
            self._entries.append(
                {
                    "id": self._sequence,
                    "sysdate": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "level": level,
                    "source": source,
                    "message": message,
                }
            )

    def recent(self, source: str = "all", limit: int = 200) -> list[dict[str, str | int]]:
        safe_limit = max(1, min(limit, 500))
        with self._lock:
            entries = list(self._entries)
        if source != "all":
            entries = [entry for entry in entries if entry["source"] == source]
        return list(reversed(entries[-safe_limit:]))


class WebLogHandler(logging.Handler):
    def __init__(self, store: LogStore) -> None:
        super().__init__(level=logging.INFO)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("anpr.gate"):
                source = "gate"
            elif record.name.startswith("uvicorn.access"):
                source = "web"
            else:
                source = getattr(record, "camera_id", "")
                if source not in {"entry", "exit"}:
                    source = "system"
            self.store.append(record.levelname, source, record.getMessage())
        except Exception:
            self.handleError(record)
