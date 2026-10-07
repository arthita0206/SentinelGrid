"""Generic virtual node abstraction for all simulated subsystems."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from shared.telemetry import Telemetry


class BaseNode(ABC):
    """A simulated smart-city node capable of generating telemetry."""

    def __init__(self, node_id: str, subsystem: str):
        self.node_id = node_id
        self.subsystem = subsystem
        self.status = "ONLINE"

    @abstractmethod
    def _generate_readings(self) -> dict:
        """Return this tick's subsystem-specific telemetry data."""

    def generate_telemetry(self) -> Telemetry:
        return Telemetry(
            node_id=self.node_id,
            subsystem=self.subsystem,
            timestamp=datetime.now(timezone.utc).isoformat(),
            status=self.status,
            data=self._generate_readings(),
        )
