from .base import GateTrigger


class ConsoleGateTrigger(GateTrigger):
    def open(self, plate: str) -> None:
        print(f"[GATE_TRIGGER] OPEN plate={plate}", flush=True)
