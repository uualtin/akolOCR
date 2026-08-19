from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class Camera(ABC):
    @abstractmethod
    def open(self) -> bool:
        """Open the camera and return whether it connected."""

    @abstractmethod
    def read(self) -> np.ndarray | None:
        """Return the next BGR frame, or None when unavailable."""

    @abstractmethod
    def release(self) -> None:
        """Release camera resources."""
