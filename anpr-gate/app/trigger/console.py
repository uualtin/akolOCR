from .base import GateTrigger


class ConsoleGateTrigger(GateTrigger):
    def __init__(self, gate_id: str = "gate") -> None:
        self.gate_id = gate_id

    def open(self, plate: str) -> None:
        print(f"[GATE_TRIGGER] gate={self.gate_id} action=OPEN plate={plate}", flush=True)
