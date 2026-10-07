"""Fault abstraction: a base Fault type plus the four supported fault kinds.

Dropout and partition faults suppress publishing for their target node(s).
Corruption and overload faults instead rewrite the telemetry data before it
reaches the publisher. Deciding *when* a fault applies (targeting, timing,
triggering) lives here and in engine.py — never inside the subsystem node
classes, which stay responsible only for normal telemetry generation.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
import random
import uuid

from shared.telemetry import Telemetry
from fault_injection.corruption import CORRUPTORS
from fault_injection.overload import OVERLOADERS


class FaultType(str, Enum):
    SENSOR_DROPOUT = "sensor_dropout"
    DATA_CORRUPTION = "data_corruption"
    NETWORK_PARTITION = "network_partition"
    NODE_OVERLOAD = "node_overload"


class FaultStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"


class Fault(ABC):
    """Common fault bookkeeping: identity, targeting, timing, status.

    A fault with a `duration_seconds` expires that many seconds after
    injection. A fault with no duration is one-shot: it applies once to
    each of its target nodes and then completes on its own.
    """

    FAULT_TYPE: FaultType

    def __init__(
        self,
        target_node_ids: List[str],
        duration_seconds: Optional[float] = None,
        injected_at: Optional[datetime] = None,
        rng: Optional[random.Random] = None,
    ):
        self.fault_id = str(uuid.uuid4())
        self.target_node_ids = list(target_node_ids)
        self.duration_seconds = duration_seconds
        self.injected_at = injected_at or datetime.now(timezone.utc)
        self.status = FaultStatus.ACTIVE
        self._rng = rng or random.Random()
        self._consumed_node_ids = set()

    @property
    def fault_type(self) -> FaultType:
        return self.FAULT_TYPE

    def affects(self, node_id: str) -> bool:
        return node_id in self.target_node_ids

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.status == FaultStatus.COMPLETED:
            return True
        if self.duration_seconds is not None:
            now = now or datetime.now(timezone.utc)
            elapsed = (now - self.injected_at).total_seconds()
            return elapsed >= self.duration_seconds
        return False

    def _mark_consumed(self, node_id: str) -> None:
        self._consumed_node_ids.add(node_id)
        if self.duration_seconds is None and self._consumed_node_ids >= set(self.target_node_ids):
            self.status = FaultStatus.COMPLETED

    def blocks_publishing(self, node_id: str) -> bool:
        """Whether this fault should suppress publishing for node_id right now."""
        return False

    @abstractmethod
    def transform_telemetry(self, telemetry: Telemetry) -> Telemetry:
        """Rewrite telemetry data for an affected node. No-op if unaffected."""

    def to_log_dict(self) -> dict:
        return {
            "fault_id": self.fault_id,
            "fault_type": self.fault_type.value,
            "target_nodes": self.target_node_ids,
            "injected_at": self.injected_at.isoformat(),
            "duration_seconds": self.duration_seconds,
            "status": self.status.value,
        }


class SensorDropoutFault(Fault):
    """FR-2.1 — target node(s) stop publishing for a configurable duration."""

    FAULT_TYPE = FaultType.SENSOR_DROPOUT

    def blocks_publishing(self, node_id: str) -> bool:
        return self.affects(node_id) and not self.is_expired()

    def transform_telemetry(self, telemetry: Telemetry) -> Telemetry:
        return telemetry


class NetworkPartitionFault(Fault):
    """FR-2.3 — target node(s) become unreachable from the publishing path."""

    FAULT_TYPE = FaultType.NETWORK_PARTITION

    def blocks_publishing(self, node_id: str) -> bool:
        return self.affects(node_id) and not self.is_expired()

    def transform_telemetry(self, telemetry: Telemetry) -> Telemetry:
        return telemetry


class DataCorruptionFault(Fault):
    """FR-2.2 — target node(s) report out-of-range or malformed readings.

    The node's status is left as-is: a corrupted sensor typically still
    reports itself online while sending bad data. Detecting that mismatch
    is the future Analyzer's job, not this engine's.
    """

    FAULT_TYPE = FaultType.DATA_CORRUPTION

    def transform_telemetry(self, telemetry: Telemetry) -> Telemetry:
        if not self.affects(telemetry.node_id):
            return telemetry
        corruptor = CORRUPTORS.get(telemetry.subsystem)
        if corruptor is not None:
            telemetry.data = corruptor(telemetry.data, self._rng)
        self._mark_consumed(telemetry.node_id)
        return telemetry


class NodeOverloadFault(Fault):
    """FR-2.4 — target node(s) produce a burst of abnormally high readings."""

    FAULT_TYPE = FaultType.NODE_OVERLOAD

    def transform_telemetry(self, telemetry: Telemetry) -> Telemetry:
        if not self.affects(telemetry.node_id):
            return telemetry
        overloader = OVERLOADERS.get(telemetry.subsystem)
        if overloader is not None:
            telemetry.data = overloader(telemetry.data, self._rng)
        self._mark_consumed(telemetry.node_id)
        return telemetry
