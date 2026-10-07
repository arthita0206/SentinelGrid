"""Local integration layer: wires the existing anomaly detector directly
into the existing remediation engine, entirely locally.

    raw telemetry
        |
        v
    detect_telemetry()          (lambda_functions/anomaly_detector/detector.py)
        |
        v
    DetectionResult
        |
        v
    RemediationEngine.plan()    (remediation/engine.py)
        |
        v
    RemediationPlan
        |
        v  (only when remediation_plan.remediation_required)
    RemediationExecutor.execute()
        |
        v
    RemediationResult, or None if no remediation was required

This module proves the local software pipeline end-to-end. It does
**not** call `lambda_functions.anomaly_detector.handler` (the AWS Lambda
adapter) or `event_store` (DynamoDB persistence) — no AWS SDK, no network
I/O, no persistence of any kind. See `remediation/README.md` for the full
architecture and the reasoning behind deferring AWS integration.

Neither the detector nor the remediation engine's own rules are
duplicated or altered here: this module only calls the existing public
entry points (`detect_telemetry`, `RemediationEngine.plan`,
`RemediationExecutor.execute`) in sequence and packages their outputs.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from lambda_functions.anomaly_detector.detector import DetectionResult, detect_telemetry

from .engine import RemediationEngine, RemediationExecutor
from .models import RemediationPlan, RemediationResult


@dataclass
class RemediationIntegrationResult:
    """The full, structured outcome of running one telemetry reading
    through detection and then remediation.

    `remediation_result` is `None` exactly when
    `remediation_plan.remediation_required` is `False` — no execution was
    attempted because none was needed, never because something failed
    silently. When remediation *was* required, `remediation_result` is
    always populated (whether the simulated outcome was success or
    escalation — see `RemediationExecutor.execute()`).
    """

    detection_result: DetectionResult
    remediation_plan: RemediationPlan
    remediation_result: Optional[RemediationResult]

    def to_dict(self) -> dict:
        return {
            "detection_result": self.detection_result.to_dict(),
            "remediation_plan": self.remediation_plan.to_dict(),
            "remediation_result": (
                self.remediation_result.to_dict() if self.remediation_result is not None else None
            ),
        }


def process_telemetry(telemetry: Dict[str, Any]) -> RemediationIntegrationResult:
    """Runs one telemetry reading through detection, then remediation.

    Deterministic and fully local: `detect_telemetry()` never raises (see
    its own docstring — malformed input yields an INVALID_INPUT
    `DetectionResult` instead), and `RemediationEngine`/`RemediationExecutor`
    are both pure functions of their input, so the same `telemetry` dict
    always produces an equal `RemediationIntegrationResult`. A fresh
    `RemediationEngine`/`RemediationExecutor` is constructed per call —
    neither type holds any instance state, so this introduces no shared
    mutable state between calls.
    """
    detection_result = detect_telemetry(telemetry)

    engine = RemediationEngine()
    remediation_plan = engine.plan(detection_result)

    remediation_result: Optional[RemediationResult] = None
    if remediation_plan.remediation_required:
        executor = RemediationExecutor()
        remediation_result = executor.execute(remediation_plan)

    return RemediationIntegrationResult(
        detection_result=detection_result,
        remediation_plan=remediation_plan,
        remediation_result=remediation_result,
    )
