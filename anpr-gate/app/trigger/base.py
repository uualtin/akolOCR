from abc import ABC, abstractmethod


class GateTrigger(ABC):
    @abstractmethod
    def open(self, plate: str) -> None:
        """Ask the gate to open for a normalized plate."""
