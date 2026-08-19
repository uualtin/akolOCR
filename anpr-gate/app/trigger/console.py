import logging

from .base import GateTrigger

logger = logging.getLogger("anpr.gate")


class ConsoleGateTrigger(GateTrigger):
    def __init__(self, gate_id: str = "gate") -> None:
        self.gate_id = gate_id

    def open(self, plate: str) -> None:
        logger.info(
            "gate=%s action=OPEN plate=%s",
            self.gate_id,
            plate,
            extra={"camera_id": self.gate_id},
        )
