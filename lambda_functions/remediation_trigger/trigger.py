"""Deterministic local decision layer for the Anomaly Detector -> Step
Functions trigger bridge.

    DetectionResult -> evaluate() -> TriggerDecision
                                          |
                                          v
                        {should_trigger, execution_name, detection_result}

This module decides WHETHER an already-computed `DetectionResult` should
start the deployed remediation Step Functions workflow
(`SentinelGridRemediationWorkflow`, see
step_functions/remediation_workflow.json), and if so, WHAT exact payload
and WHAT deterministic execution name that start would use. It does
**not** run anomaly detection itself (`detect_telemetry()` in
`lambda_functions/anomaly_detector/detector.py` remains the sole source
of truth for that), and it does **not** call AWS Step Functions itself —
see this package's README for the future AWS wiring that will eventually
call `evaluate()` and act on its result.

Pure, deterministic, stdlib-only: no AWS SDK, no network, no subprocess,
no randomness, no wall-clock time. `evaluate()` is a total function of
its single argument.
"""

import hashlib
from dataclasses import dataclass
from typing import Any, Optional

from lambda_functions.anomaly_detector.detector import DetectionResult

# Amazon States Language execution names must be 1-80 characters and must
# not contain whitespace or any of: < > { } [ ] ? * " # % \ ^ | ~ ` $ & , ; : /
# (see AWS Step Functions' StartExecution `name` constraints). Building
# the name from only a fixed ASCII prefix plus a hex digest guarantees
# compliance by construction — no runtime validation of the character set
# is needed.
_EXECUTION_NAME_PREFIX = "sentinelgrid-remediation-"
_EXECUTION_NAME_HASH_LENGTH = 32  # hex chars taken from the sha256 digest

# ASCII Unit Separator (0x1F): essentially never appears in real
# node_id/timestamp/fault_type values, unlike "|" or "-", which
# plausibly could. Used purely to make the canonical join unambiguous —
# see `compute_execution_name`'s docstring.
_FIELD_SEPARATOR = "\x1f"


class InvalidDetectionResultError(TypeError):
    """Raised when `evaluate()` is given something other than a real
    `DetectionResult` instance.

    This is a programmer-error signal, not a data-quality one: the real
    caller of `evaluate()` is expected to be code that just received a
    `DetectionResult` directly from `detect_telemetry()` (which itself
    never raises and always returns a valid `DetectionResult` — see its
    own docstring), so a wrong type here means the caller is misusing
    this module, not that untrusted external input was malformed. Raising
    loudly matches this project's "do not hide programmer errors"
    convention, and is deliberately different from how the Lambda
    adapters in `lambda_functions/remediation/` handle *external* JSON
    input (see their README's "Error-handling strategy").
    """


@dataclass
class TriggerDecision:
    """The result of evaluating one `DetectionResult` for remediation
    triggering.

    `detection_result` is `None` exactly when `should_trigger` is
    `False` — there is nothing to send. When `should_trigger` is `True`,
    `detection_result` is the **exact, unmodified** `DetectionResult.
    to_dict()` — never wrapped (e.g. never `{"detection_result": {...}}`)
    — because the deployed state machine's `CheckAnomaly` state expects
    its own execution input to already be a DetectionResult-shaped
    object (see step_functions/remediation_workflow.json and this
    package's README).
    """

    should_trigger: bool
    reason: str
    detection_result: Optional[dict] = None
    execution_name: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "should_trigger": self.should_trigger,
            "reason": self.reason,
            "detection_result": self.detection_result,
            "execution_name": self.execution_name,
        }


def compute_execution_name(node_id: str, timestamp: str, fault_type: str) -> str:
    """Deterministically derives a Step Functions execution name from the
    three fields that, together, identify one underlying anomaly:
    `node_id`, `timestamp`, and `fault_type`.

    This is this project's whole idempotency strategy for triggering
    (see this package's README "Idempotency strategy" section for the
    full reasoning): AWS Step Functions' own `StartExecution` API already
    rejects a second call with a `name` that matches a currently-running
    or recently-closed execution of the same state machine
    (`ExecutionAlreadyExists`). By deriving the name deterministically
    from an anomaly's own identity, any duplicate delivery of the exact
    same anomaly (e.g. an at-least-once IoT redelivery, or a retried
    Lambda invocation) naturally collapses onto the same execution name
    and is safely rejected by AWS itself — no separate idempotency table
    is needed.

    The three fields are joined with an ASCII Unit Separator (0x1F)
    rather than a printable character like "|" specifically because a
    printable separator could in principle appear inside a real
    `node_id`, making two genuinely different (node_id, timestamp,
    fault_type) triples hash identically (e.g. node_id="a|b",
    timestamp="c" vs. node_id="a", timestamp="b|c"). 0x1F essentially
    never appears in real telemetry identifiers, so this ambiguity is
    avoided in practice.

    Uses `hashlib.sha256` (Python standard library) — not a random UUID
    and not the current wall-clock time — so the result is a pure,
    reproducible function of the three inputs alone.
    """
    canonical = _FIELD_SEPARATOR.join([node_id, timestamp, fault_type])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_EXECUTION_NAME_HASH_LENGTH]
    return f"{_EXECUTION_NAME_PREFIX}{digest}"


def evaluate(detection: Any) -> TriggerDecision:
    """Decides whether `detection` should trigger the remediation
    workflow, and if so, computes the exact payload and execution name.

    Triggers (`should_trigger=True`) when, and only when:
      - `detection.detected` is `True`, AND
      - `detection.node_id`, `detection.timestamp`, and
        `detection.fault_type` are all present (non-empty).

    Deliberately does NOT branch on `fault_type` or `severity` beyond
    that presence check — e.g. SENSOR_DROPOUT, NODE_OVERLOAD at any
    severity, and INVALID_INPUT (when it has a usable identity) all
    trigger identically. Whether a given fault is ultimately handled
    automatically or escalated is entirely `RemediationEngine.plan()`'s
    decision (made later, inside the Step Functions workflow) — this
    function does not duplicate that policy. See this package's README
    "Local trigger contract" section for the full reasoning, including
    why an INVALID_INPUT result that lacks even a node_id/timestamp
    cannot and should not trigger anything.

    Raises `InvalidDetectionResultError` if `detection` is not a real
    `DetectionResult` instance. Never mutates its argument, never raises
    for any *valid* `DetectionResult` (including detected=False), never
    performs I/O, and never uses randomness or wall-clock time.
    """
    if not isinstance(detection, DetectionResult):
        raise InvalidDetectionResultError(f"evaluate() expects a DetectionResult, got {type(detection).__name__}")

    if not detection.detected:
        return TriggerDecision(
            should_trigger=False,
            reason="No fault was detected; the remediation workflow should not be triggered.",
        )

    if not detection.node_id or not detection.timestamp or not detection.fault_type:
        return TriggerDecision(
            should_trigger=False,
            reason=(
                "DetectionResult is missing node_id/timestamp/fault_type; "
                "there is not enough identity to safely trigger remediation."
            ),
        )

    execution_name = compute_execution_name(detection.node_id, detection.timestamp, detection.fault_type)
    return TriggerDecision(
        should_trigger=True,
        reason=f"{detection.fault_type} detected on node_id={detection.node_id!r}; triggering remediation workflow.",
        detection_result=detection.to_dict(),
        execution_name=execution_name,
    )
