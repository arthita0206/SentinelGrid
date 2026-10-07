"""FaultInjectionEngine: targeting, triggering, and applying faults.

    CitySimulator
        |
        v
    node.generate_telemetry()
        |
        v
    FaultInjectionEngine.apply(telemetry)  -- corruption/overload rewrite data,
        |                                     dropout/partition return None
        v
    Publisher

Automatic triggering (random or scheduled) is driven once per simulator
tick via `tick()`. Manual injection via `inject()` works regardless of the
`enabled` config flag, since it's an explicit, intentional action (CLI /
API / tests) rather than background fault generation.
"""

import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

from shared.telemetry import Telemetry
from fault_injection.faults import (
    Fault,
    FaultStatus,
    SensorDropoutFault,
    DataCorruptionFault,
    NetworkPartitionFault,
    NodeOverloadFault,
)
from fault_injection.logger import FaultLogger

FAULT_CLASSES = {
    "dropout": SensorDropoutFault,
    "sensor_dropout": SensorDropoutFault,
    "corruption": DataCorruptionFault,
    "data_corruption": DataCorruptionFault,
    "partition": NetworkPartitionFault,
    "network_partition": NetworkPartitionFault,
    "overload": NodeOverloadFault,
    "node_overload": NodeOverloadFault,
}

# Config sections that drive automatic (random/scheduled) triggering.
AUTO_FAULT_KEYS = ("dropout", "corruption", "partition", "overload")


class FaultInjectionEngine:
    def __init__(self, config: Optional[dict] = None, logger: Optional[FaultLogger] = None):
        self.config = config or {}
        self.logger = logger or FaultLogger()
        self._rng = random.Random(self.config.get("random_seed"))
        self._nodes_by_subsystem: Dict[str, List[str]] = defaultdict(list)
        self._all_node_ids: List[str] = []
        self.active_faults: List[Fault] = []
        self.fault_history: List[Fault] = []
        self._fired_schedule_indices = set()

    def register_nodes(self, nodes) -> None:
        self._nodes_by_subsystem = defaultdict(list)
        for node in nodes:
            self._nodes_by_subsystem[node.subsystem].append(node.node_id)
        self._all_node_ids = [node.node_id for node in nodes]

    def resolve_target(self, target: Union[str, List[str]]) -> List[str]:
        if isinstance(target, (list, tuple, set)):
            return list(target)
        if isinstance(target, str):
            if target.startswith("subsystem:"):
                subsystem = target.split(":", 1)[1].strip()
                return list(self._nodes_by_subsystem.get(subsystem, []))
            return [node_id.strip() for node_id in target.split(",") if node_id.strip()]
        raise ValueError(f"Unsupported fault target: {target!r}")

    def inject(
        self,
        fault_type: str,
        target: Union[str, List[str]],
        duration_seconds: Optional[float] = None,
    ) -> Fault:
        fault_cls = FAULT_CLASSES.get(fault_type)
        if fault_cls is None:
            raise ValueError(f"Unknown fault type: {fault_type!r}")

        node_ids = self.resolve_target(target)
        if not node_ids:
            raise ValueError(f"Fault target resolved to no nodes: {target!r}")

        fault = fault_cls(target_node_ids=node_ids, duration_seconds=duration_seconds, rng=self._rng)
        self.active_faults.append(fault)
        self.logger.log(fault)
        return fault

    def _expire_faults(self, now: datetime) -> None:
        still_active = []
        for fault in self.active_faults:
            if fault.status == FaultStatus.COMPLETED or fault.is_expired(now):
                fault.status = FaultStatus.COMPLETED
                self.fault_history.append(fault)
            else:
                still_active.append(fault)
        self.active_faults = still_active

    def _maybe_trigger_random(self) -> None:
        if not self.config.get("enabled") or self.config.get("mode") != "random":
            return
        if not self._all_node_ids:
            return
        for key in AUTO_FAULT_KEYS:
            section = self.config.get(key) or {}
            if not section.get("enabled"):
                continue
            probability = section.get("probability", 0.0)
            if probability <= 0:
                continue
            if self._rng.random() < probability:
                target_node = self._rng.choice(self._all_node_ids)
                self.inject(key, target_node, duration_seconds=section.get("duration_seconds"))

    def _run_schedule(self, elapsed_seconds: float) -> None:
        if not self.config.get("enabled") or self.config.get("mode") != "scheduled":
            return
        for index, entry in enumerate(self.config.get("schedule") or []):
            if index in self._fired_schedule_indices:
                continue
            if elapsed_seconds >= entry.get("delay_seconds", 0):
                self.inject(
                    entry["fault_type"],
                    entry["target"],
                    duration_seconds=entry.get("duration_seconds"),
                )
                self._fired_schedule_indices.add(index)

    def tick(self, now: Optional[datetime] = None, elapsed_seconds: float = 0.0) -> None:
        """Call once per simulator tick to expire faults and auto-trigger new ones."""
        now = now or datetime.now(timezone.utc)
        self._expire_faults(now)
        self._run_schedule(elapsed_seconds)
        self._maybe_trigger_random()

    def apply(self, telemetry: Telemetry) -> Optional[Telemetry]:
        """Apply active faults to one reading. Returns None if it should not publish."""
        self._expire_faults(datetime.now(timezone.utc))

        for fault in self.active_faults:
            if fault.affects(telemetry.node_id) and fault.blocks_publishing(telemetry.node_id):
                return None

        for fault in self.active_faults:
            if fault.affects(telemetry.node_id):
                telemetry = fault.transform_telemetry(telemetry)

        return telemetry
