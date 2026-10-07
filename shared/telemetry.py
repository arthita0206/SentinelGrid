"""Telemetry data model shared across the simulator and future publishers."""

from dataclasses import dataclass, field
import json


@dataclass
class Telemetry:
    """A single telemetry reading produced by a simulated node."""

    node_id: str
    subsystem: str
    timestamp: str
    status: str
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "subsystem": self.subsystem,
            "timestamp": self.timestamp,
            "status": self.status,
            "data": self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())
