"""AWS-facing Lambda adapter for the remediation trigger.

    Lambda event -> _extract_detection_result() -> evaluate() -> (if
    should_trigger) stepfunctions:StartExecution

This is the **only** module in `lambda_functions/remediation_trigger/`
that imports `boto3` or talks to AWS. `trigger.py`'s `evaluate()` and
`compute_execution_name()` remain pure, deterministic, and
AWS-independent — see `trigger.py`'s own module docstring and this
package's README "AWS dependency boundary" section. This mirrors exactly
how `lambda_functions/anomaly_detector/detector.py` stays AWS-independent
while `event_store.py` is the one module in that package that imports
boto3.

**Not deployed as of Step 21's local-authoring point** — see
infrastructure/remediation-trigger.yaml and
infrastructure/deploy-remediation-trigger.sh for the deployment this
module is built for.
"""

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from lambda_functions.anomaly_detector.detector import DetectionResult

from .trigger import InvalidDetectionResultError, evaluate

logger = logging.getLogger(__name__)
# See lambda_functions/anomaly_detector/handler.py for why this is
# necessary on Lambda: the Python runtime's root logger defaults to
# WARNING, which would otherwise silently drop every logger.info() call
# below.
logger.setLevel(logging.INFO)

STATE_MACHINE_ARN_ENV_VAR = "REMEDIATION_STATE_MACHINE_ARN"

_REQUIRED_FIELDS = ("detected", "fault_type", "severity", "node_id", "subsystem", "timestamp", "reason")
_OPTIONAL_STR_FIELDS = ("fault_type", "node_id", "subsystem", "timestamp")


class MissingStateMachineArnError(RuntimeError):
    """Raised when REMEDIATION_STATE_MACHINE_ARN is not configured.

    This is a deployment/configuration error, not a data-quality one —
    it is allowed to propagate and fail the invocation loudly rather
    than silently skipping the StartExecution call, matching this
    project's fail-fast convention for genuine misconfiguration (see
    lambda_functions/anomaly_detector/event_store.py's own persistence
    failure policy for the same reasoning applied elsewhere).
    """


class TriggerExecutionError(RuntimeError):
    """Raised when stepfunctions:StartExecution fails for any reason
    OTHER than the execution already existing (which is treated as a
    successful, idempotent no-op — see `lambda_handler`'s docstring).
    """


def _extract_detection_result(event: Any) -> DetectionResult:
    """Validates and reconstructs a DetectionResult from a plain dict.

    Deliberately mirrors `lambda_functions/remediation/planner.py`'s own
    `_extract_detection_result` field-for-field: both adapters consume
    the identical `DetectionResult.to_dict()` wire shape. Kept as an
    independent copy — not a shared import — because each Lambda
    package in this project (`anomaly_detector/`, `remediation/`,
    `remediation_trigger/`) is deployed as its own self-contained zip
    (see infrastructure/package-remediation-trigger.sh); sharing this
    small adapter function across packages would couple their
    deployment artifacts together for no benefit.
    """
    if not isinstance(event, dict):
        raise InvalidDetectionResultError(f"event must be a dict, got {type(event).__name__}")

    missing = [f for f in _REQUIRED_FIELDS if f not in event]
    if missing:
        raise InvalidDetectionResultError(f"event is missing required field(s): {missing}")

    if not isinstance(event["detected"], bool):
        raise InvalidDetectionResultError(f"'detected' must be a bool, got {type(event['detected']).__name__}")
    if not isinstance(event["severity"], str):
        raise InvalidDetectionResultError(f"'severity' must be a str, got {type(event['severity']).__name__}")
    if not isinstance(event["reason"], str):
        raise InvalidDetectionResultError(f"'reason' must be a str, got {type(event['reason']).__name__}")

    for field_name in _OPTIONAL_STR_FIELDS:
        value = event[field_name]
        if value is not None and not isinstance(value, str):
            raise InvalidDetectionResultError(
                f"'{field_name}' must be a str or None, got {type(value).__name__}"
            )

    evidence = event.get("evidence", {})
    if not isinstance(evidence, dict):
        raise InvalidDetectionResultError(f"'evidence' must be a dict, got {type(evidence).__name__}")

    return DetectionResult(
        detected=event["detected"],
        fault_type=event["fault_type"],
        severity=event["severity"],
        node_id=event["node_id"],
        subsystem=event["subsystem"],
        timestamp=event["timestamp"],
        reason=event["reason"],
        evidence=evidence,
    )


def lambda_handler(event: Any, context: Any = None) -> dict:
    """AWS Lambda entry point for the remediation trigger.

    On malformed input, returns `{"error": "invalid_detection_result",
    "message": "..."}`, matching the structured-error convention already
    used by `lambda_functions/remediation/{planner,executor}.py`.

    On valid input with `should_trigger=False`, returns
    `{"triggered": False, "started_execution": False, "reason": "...",
    "execution_name": None}` — `stepfunctions:StartExecution` is never
    called.

    On valid input with `should_trigger=True`, calls `StartExecution`
    with `stateMachineArn` from the `REMEDIATION_STATE_MACHINE_ARN`
    environment variable, `name` from `decision.execution_name`
    (deterministic — see `trigger.py`), and `input` set to the exact,
    unwrapped JSON serialization of `decision.detection_result` — never
    `{"detection_result": {...}}` — because the deployed state machine's
    `CheckAnomaly` state expects its raw execution input to already be a
    `DetectionResult` (proven live in Step 19).

    A duplicate `StartExecution` call for the same deterministic name
    (AWS returns `ExecutionAlreadyExists`) is treated as a successful,
    idempotent no-op — `{"triggered": True, "started_execution": False,
    "already_existed": True, ...}` — mirroring exactly how
    `event_store.py` already treats a `ConditionalCheckFailedException`
    as success, not failure. Any other `StartExecution` failure raises
    `TriggerExecutionError` rather than being swallowed or reported as a
    false success. A missing `REMEDIATION_STATE_MACHINE_ARN` raises
    `MissingStateMachineArnError`.

    Never mutates `event`. The only AWS call this function ever makes is
    `stepfunctions:StartExecution` — never `lambda:Invoke`, never any
    DynamoDB or IoT API.
    """
    try:
        detection = _extract_detection_result(event)
    except InvalidDetectionResultError as exc:
        return {"error": "invalid_detection_result", "message": str(exc)}

    decision = evaluate(detection)

    if not decision.should_trigger:
        return {
            "triggered": False,
            "started_execution": False,
            "already_existed": False,
            "execution_name": None,
            "reason": decision.reason,
        }

    state_machine_arn = os.environ.get(STATE_MACHINE_ARN_ENV_VAR)
    if not state_machine_arn:
        logger.error(
            "SentinelGrid remediation trigger: %s is not configured; cannot start remediation execution.",
            STATE_MACHINE_ARN_ENV_VAR,
        )
        raise MissingStateMachineArnError(f"{STATE_MACHINE_ARN_ENV_VAR} environment variable is not set")

    client = boto3.client("stepfunctions")
    try:
        response = client.start_execution(
            stateMachineArn=state_machine_arn,
            name=decision.execution_name,
            input=json.dumps(decision.detection_result),
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code == "ExecutionAlreadyExists":
            logger.info(
                "SentinelGrid remediation execution already exists for execution_name=%s; "
                "treating as idempotent no-op.",
                decision.execution_name,
            )
            return {
                "triggered": True,
                "started_execution": False,
                "already_existed": True,
                "execution_name": decision.execution_name,
                "reason": decision.reason,
            }
        logger.exception(
            "SentinelGrid remediation StartExecution FAILED for execution_name=%s",
            decision.execution_name,
        )
        raise TriggerExecutionError(
            f"Failed to start remediation execution '{decision.execution_name}'."
        ) from exc

    logger.info(
        "SentinelGrid remediation execution started: execution_name=%s executionArn=%s",
        decision.execution_name,
        response.get("executionArn"),
    )
    return {
        "triggered": True,
        "started_execution": True,
        "already_existed": False,
        "execution_name": decision.execution_name,
        "execution_arn": response.get("executionArn"),
        "reason": decision.reason,
    }
