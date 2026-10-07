"""Data models for the local remediation layer.

Mirrors the style of `lambda_functions/anomaly_detector/detector.py`:
plain stdlib dataclasses, `str`-backed enums, and a `to_dict()` method on
every model so results are trivially JSON-serializable. No AWS types, no
external dependencies.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ActionType(str, Enum):
    """The fixed, closed set of simulated remediation actions.

    Deliberately small and literal: each member corresponds to exactly one
    row of the fault -> action mapping in this package's README. There is
    no generic/free-form action — `RemediationEngine` never invents a new
    action name, and nothing outside this enum is a valid `RemediationAction.
    action_type`.
    """

    RESTART_SENSOR = "RESTART_SENSOR"
    RECONNECT_NODE = "RECONNECT_NODE"
    RESET_SENSOR_STATE = "RESET_SENSOR_STATE"
    REDUCE_LOAD = "REDUCE_LOAD"
    REJECT_INPUT = "REJECT_INPUT"
    NO_ACTION = "NO_ACTION"


class ExecutionStatus(str, Enum):
    """What `RemediationExecutor.execute()` actually did with a plan."""

    SUCCESS = "SUCCESS"
    ESCALATED = "ESCALATED"
    SKIPPED = "SKIPPED"
    REJECTED = "REJECTED"


@dataclass
class RemediationAction:
    """One concrete, simulated corrective action and the evidence behind it.

    `parameters` carries the originating `DetectionResult.evidence` through
    unchanged — this layer never fabricates new diagnostic data, it only
    decides what to do with what the detector already found.
    """

    action_type: ActionType
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type.value,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class RemediationPlan:
    """A deterministic decision about whether/how to remediate one
    `DetectionResult`.

    `timestamp` is the originating telemetry event's timestamp (passed
    through from `DetectionResult.timestamp`), not a wall-clock planning
    time — this keeps planning a pure function of its input, with no
    dependency on the real clock. See `RemediationEngine.plan()`.
    """

    remediation_required: bool
    node_id: Optional[str]
    subsystem: Optional[str]
    fault_type: Optional[str]
    severity: Optional[str]
    timestamp: Optional[str]
    action: RemediationAction
    reason: str
    automatic: bool
    escalation_required: bool = False

    def to_dict(self) -> dict:
        return {
            "remediation_required": self.remediation_required,
            "node_id": self.node_id,
            "subsystem": self.subsystem,
            "fault_type": self.fault_type,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "action": self.action.to_dict(),
            "reason": self.reason,
            "automatic": self.automatic,
            "escalation_required": self.escalation_required,
        }


@dataclass
class RemediationResult:
    """The outcome of (simulated) execution of one `RemediationPlan`.

    `simulated` is always `True` in this step — nothing here ever touches
    real infrastructure — but the field is explicit and required, rather
    than assumed, so a future real executor can reuse this same model and
    set it to `False` without changing its shape.
    """

    success: bool
    status: ExecutionStatus
    node_id: Optional[str]
    subsystem: Optional[str]
    fault_type: Optional[str]
    action: ActionType
    timestamp: Optional[str]
    message: str
    simulated: bool = True
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "status": self.status.value,
            "node_id": self.node_id,
            "subsystem": self.subsystem,
            "fault_type": self.fault_type,
            "action": self.action.value,
            "timestamp": self.timestamp,
            "message": self.message,
            "simulated": self.simulated,
            "details": self.details,
        }
