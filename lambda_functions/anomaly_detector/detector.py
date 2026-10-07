"""Deterministic, rule-based fault detector for SentinelGrid telemetry.

`detect_telemetry(payload)` is the whole public interface: it takes one
telemetry reading (the dict shape produced by `shared.telemetry.Telemetry.
to_dict()`) and returns a `DetectionResult`. It never raises — malformed
input produces a `DetectionResult` with `fault_type="INVALID_INPUT"`
rather than an exception, so one bad message can never take down whatever
loop calls this.

No machine learning, no AWS calls, no DynamoDB queries, and no dependency
on FaultInjectionEngine — every decision is a plain threshold or type
check against the payload alone. This is the local prototype for logic
that will eventually run inside an AWS Lambda function (see this
package's README for how thresholds were derived and what a future
Lambda adapter would look like — none exists yet).
"""

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class FaultType(str, Enum):
    NONE = "NONE"
    SENSOR_DROPOUT = "SENSOR_DROPOUT"
    NETWORK_PARTITION = "NETWORK_PARTITION"
    DATA_CORRUPTION = "DATA_CORRUPTION"
    NODE_OVERLOAD = "NODE_OVERLOAD"
    INVALID_INPUT = "INVALID_INPUT"


class Severity(str, Enum):
    NORMAL = "NORMAL"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class DetectionResult:
    detected: bool
    fault_type: Optional[str]
    severity: str
    node_id: Optional[str]
    subsystem: Optional[str]
    timestamp: Optional[str]
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "detected": self.detected,
            "fault_type": self.fault_type,
            "severity": self.severity,
            "node_id": self.node_id,
            "subsystem": self.subsystem,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "evidence": self.evidence,
        }


VALID_SUBSYSTEMS = ("power", "water", "traffic")

REQUIRED_DATA_FIELDS = {
    "power": ("load", "voltage", "frequency"),
    "water": ("pressure", "flow_rate", "reservoir_level"),
    "traffic": ("vehicle_count", "average_speed", "signal_state"),
}

# Ranges below are read directly off the existing simulator/fault-injection
# code, not invented independently. See README.md's "Where these numbers
# come from" section for the full derivation, field by field:
#   simulator/subsystems/power.py / water.py / traffic.py  -> "normal"
#   fault_injection/overload.py                             -> "overload"
#   fault_injection/corruption.py                            -> corroborates
#                                                                the corruption
#                                                                zone beyond
#                                                                "normal"/"overload"
#
# For a field with an "overload" band, values are checked against it FIRST:
# for power.load the overload band (95-100) sits *inside* the normal band
# (0-100), while for the other overloadable fields it extends just past
# the normal band's ceiling — checking overload first handles both shapes
# with one rule.
NUMERIC_FIELD_SPECS = {
    "power": {
        "load": {"normal": (0.0, 100.0), "overload": (95.0, 100.0)},
        "voltage": {"normal": (200.0, 250.0)},
        "frequency": {"normal": (49.0, 51.0)},
    },
    "water": {
        "pressure": {"normal": (0.0, 7.0), "overload": (7.0, 9.0)},
        "flow_rate": {"normal": (0.0, 120.0), "overload": (120.0, 250.0)},
        "reservoir_level": {"normal": (0.0, 100.0)},
    },
    "traffic": {
        "vehicle_count": {"normal": (0, 200), "overload": (200, 400)},
        "average_speed": {"normal": (0.0, 120.0)},
    },
}

CATEGORICAL_FIELD_SPECS = {
    "traffic": {
        "signal_state": {"RED", "YELLOW", "GREEN"},
    },
}

# Not part of the current telemetry schema (see README's "Network
# partition" limitation section) — this is an inert, forward-looking hook.
# It only ever matches if a future producer adds this field; today's
# simulator never sets it, so this path is not exercised by real output.
NETWORK_STATUS_FIELD = "network_status"
NETWORK_PARTITION_INDICATOR_VALUES = {"PARTITIONED", "UNREACHABLE"}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normal_result(node_id: str, subsystem: str, timestamp: str, data: dict) -> DetectionResult:
    return DetectionResult(
        detected=False,
        fault_type=None,
        severity=Severity.NORMAL.value,
        node_id=node_id,
        subsystem=subsystem,
        timestamp=timestamp,
        reason="All telemetry fields are present and within the expected operating range.",
        evidence={"data": data},
    )


def _invalid_input_result(payload: Any, reason: str, evidence: dict) -> DetectionResult:
    payload_dict = payload if isinstance(payload, dict) else {}
    return DetectionResult(
        detected=True,
        fault_type=FaultType.INVALID_INPUT.value,
        severity=Severity.CRITICAL.value,
        node_id=payload_dict.get("node_id") if isinstance(payload_dict.get("node_id"), str) else None,
        subsystem=payload_dict.get("subsystem") if isinstance(payload_dict.get("subsystem"), str) else None,
        timestamp=payload_dict.get("timestamp") if isinstance(payload_dict.get("timestamp"), str) else None,
        reason=reason,
        evidence=evidence,
    )


def _validate_structure(payload: Any) -> Optional[DetectionResult]:
    if not isinstance(payload, dict):
        return _invalid_input_result(
            payload,
            reason=f"Telemetry payload must be a dict, got {type(payload).__name__}.",
            evidence={"payload_type": type(payload).__name__},
        )

    node_id = payload.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        return _invalid_input_result(
            payload,
            reason="Telemetry payload is missing a valid 'node_id'.",
            evidence={"node_id": node_id},
        )

    subsystem = payload.get("subsystem")
    if not isinstance(subsystem, str) or subsystem not in VALID_SUBSYSTEMS:
        return _invalid_input_result(
            payload,
            reason=(
                f"Telemetry payload has an unsupported 'subsystem' value: {subsystem!r}. "
                f"Expected one of {VALID_SUBSYSTEMS}."
            ),
            evidence={"subsystem": subsystem},
        )

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        return _invalid_input_result(
            payload,
            reason="Telemetry payload is missing a valid 'timestamp'.",
            evidence={"timestamp": timestamp},
        )

    return None


def _check_network_partition_hook(payload: dict) -> Optional[DetectionResult]:
    """Forward-looking hook: only fires if a future payload carries explicit
    network-state metadata. The current telemetry schema never sets this
    field, so this always returns None against real simulator output — see
    README.md's "Network partition" limitation section for why.
    """
    indicator = payload.get(NETWORK_STATUS_FIELD)
    if indicator in NETWORK_PARTITION_INDICATOR_VALUES:
        return DetectionResult(
            detected=True,
            fault_type=FaultType.NETWORK_PARTITION.value,
            severity=Severity.HIGH.value,
            node_id=payload.get("node_id"),
            subsystem=payload.get("subsystem"),
            timestamp=payload.get("timestamp"),
            reason=f"Telemetry payload's '{NETWORK_STATUS_FIELD}' indicates the node is network-partitioned.",
            evidence={NETWORK_STATUS_FIELD: indicator},
        )
    return None


def _check_status_dropout(payload: dict) -> Optional[DetectionResult]:
    status = payload.get("status")
    if status is not None and status != "ONLINE":
        return DetectionResult(
            detected=True,
            fault_type=FaultType.SENSOR_DROPOUT.value,
            severity=Severity.HIGH.value,
            node_id=payload.get("node_id"),
            subsystem=payload.get("subsystem"),
            timestamp=payload.get("timestamp"),
            reason=f"Telemetry status is {status!r} instead of 'ONLINE'.",
            evidence={"status": status},
        )
    return None


def _check_data_container(payload: dict, subsystem: str) -> Optional[DetectionResult]:
    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        return DetectionResult(
            detected=True,
            fault_type=FaultType.SENSOR_DROPOUT.value,
            severity=Severity.HIGH.value,
            node_id=payload.get("node_id"),
            subsystem=subsystem,
            timestamp=payload.get("timestamp"),
            reason="Telemetry 'data' is missing, empty, or not an object — consistent with the node not reporting readings.",
            evidence={"data": data},
        )
    return None


def _check_missing_fields(payload: dict, subsystem: str, data: dict) -> Optional[DetectionResult]:
    missing = [f for f in REQUIRED_DATA_FIELDS[subsystem] if f not in data]
    if missing:
        return DetectionResult(
            detected=True,
            fault_type=FaultType.SENSOR_DROPOUT.value,
            severity=Severity.HIGH.value,
            node_id=payload.get("node_id"),
            subsystem=subsystem,
            timestamp=payload.get("timestamp"),
            reason=f"Telemetry is missing required field(s) for subsystem '{subsystem}': {missing}.",
            evidence={"missing_fields": missing, "data": data},
        )
    return None


def _classify_numeric_band(value: float, spec: dict) -> str:
    """Returns 'overload', 'normal', 'corruption_below', or 'corruption_above'."""
    overload_range = spec.get("overload")
    if overload_range is not None:
        ov_min, ov_max = overload_range
        if ov_min <= value <= ov_max:
            return "overload"

    normal_min, normal_max = spec["normal"]
    if normal_min <= value <= normal_max:
        return "normal"

    return "corruption_below" if value < normal_min else "corruption_above"


def _check_numeric_field(payload: dict, subsystem: str, data: dict, field_name: str) -> Optional[DetectionResult]:
    value = data[field_name]
    node_id = payload.get("node_id")
    timestamp = payload.get("timestamp")

    if not _is_number(value):
        return DetectionResult(
            detected=True,
            fault_type=FaultType.DATA_CORRUPTION.value,
            severity=Severity.CRITICAL.value,
            node_id=node_id,
            subsystem=subsystem,
            timestamp=timestamp,
            reason=f"Field '{field_name}' is not numeric (got {type(value).__name__}: {value!r}).",
            evidence={"field": field_name, "value": value, "value_type": type(value).__name__},
        )

    if not math.isfinite(value):
        return DetectionResult(
            detected=True,
            fault_type=FaultType.DATA_CORRUPTION.value,
            severity=Severity.CRITICAL.value,
            node_id=node_id,
            subsystem=subsystem,
            timestamp=timestamp,
            reason=f"Field '{field_name}' is not a finite number ({value!r}).",
            evidence={"field": field_name, "value": value},
        )

    spec = NUMERIC_FIELD_SPECS[subsystem][field_name]
    band = _classify_numeric_band(value, spec)

    if band == "normal":
        return None

    if band == "overload":
        return DetectionResult(
            detected=True,
            fault_type=FaultType.NODE_OVERLOAD.value,
            severity=Severity.MEDIUM.value,
            node_id=node_id,
            subsystem=subsystem,
            timestamp=timestamp,
            reason=f"Field '{field_name}' value {value} is within the overload band {spec['overload']}.",
            evidence={"field": field_name, "value": value, "overload_range": spec["overload"]},
        )

    # corruption_below / corruption_above: a real, finite number, but
    # physically impossible for this field given the simulator's own
    # operating envelope.
    return DetectionResult(
        detected=True,
        fault_type=FaultType.DATA_CORRUPTION.value,
        severity=Severity.HIGH.value,
        node_id=node_id,
        subsystem=subsystem,
        timestamp=timestamp,
        reason=f"Field '{field_name}' value {value} is outside the physically valid range {spec['normal']}.",
        evidence={"field": field_name, "value": value, "normal_range": spec["normal"]},
    )


def _check_categorical_field(payload: dict, subsystem: str, data: dict, field_name: str, allowed: set) -> Optional[DetectionResult]:
    value = data[field_name]
    if value in allowed:
        return None
    return DetectionResult(
        detected=True,
        fault_type=FaultType.DATA_CORRUPTION.value,
        severity=Severity.LOW.value,
        node_id=payload.get("node_id"),
        subsystem=subsystem,
        timestamp=payload.get("timestamp"),
        reason=f"Field '{field_name}' has an invalid value {value!r}; expected one of {sorted(allowed)}.",
        evidence={"field": field_name, "value": value, "allowed_values": sorted(allowed)},
    )


def detect_telemetry(payload: Dict[str, Any]) -> DetectionResult:
    """Classify one telemetry reading. Never raises."""

    structural_error = _validate_structure(payload)
    if structural_error is not None:
        return structural_error

    partition_hit = _check_network_partition_hook(payload)
    if partition_hit is not None:
        return partition_hit

    dropout_by_status = _check_status_dropout(payload)
    if dropout_by_status is not None:
        return dropout_by_status

    subsystem = payload["subsystem"]

    dropout_by_missing_data = _check_data_container(payload, subsystem)
    if dropout_by_missing_data is not None:
        return dropout_by_missing_data

    data = payload["data"]

    dropout_by_missing_fields = _check_missing_fields(payload, subsystem, data)
    if dropout_by_missing_fields is not None:
        return dropout_by_missing_fields

    for field_name in REQUIRED_DATA_FIELDS[subsystem]:
        if field_name in CATEGORICAL_FIELD_SPECS.get(subsystem, {}):
            result = _check_categorical_field(
                payload, subsystem, data, field_name, CATEGORICAL_FIELD_SPECS[subsystem][field_name]
            )
        else:
            result = _check_numeric_field(payload, subsystem, data, field_name)

        if result is not None:
            return result

    return _normal_result(payload["node_id"], subsystem, payload["timestamp"], data)
