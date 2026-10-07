"""Unit tests for the local Anomaly Detector -> Step Functions trigger
decision layer (lambda_functions/remediation_trigger/trigger.py) and its
AWS-facing adapter (lambda_functions/remediation_trigger/handler.py).

The `evaluate()`-focused tests are local and deterministic: no AWS, no
DynamoDB, no MQTT, no simulator, no boto3, and no real Step Functions
execution is ever started. The `lambda_handler`-focused tests at the
bottom of this file (Step 21) mock the boto3 Step Functions client at
the AWS boundary — no real AWS call is made there either.
"""

import ast
import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from lambda_functions.anomaly_detector.detector import (
    DetectionResult,
    FaultType,
    Severity,
    detect_telemetry,
)
from lambda_functions.remediation_trigger import (
    InvalidDetectionResultError,
    TriggerDecision,
    compute_execution_name,
    evaluate,
)
from lambda_functions.remediation_trigger.handler import (
    STATE_MACHINE_ARN_ENV_VAR,
    MissingStateMachineArnError,
    TriggerExecutionError,
    lambda_handler as trigger_lambda_handler,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TRIGGER_PACKAGE_DIR = REPO_ROOT / "lambda_functions" / "remediation_trigger"
_FAKE_STATE_MACHINE_ARN = "arn:aws:states:ap-south-1:401429382000:stateMachine:SentinelGridRemediationWorkflow"


def _detection(**overrides) -> DetectionResult:
    defaults = dict(
        detected=True,
        fault_type=FaultType.NODE_OVERLOAD.value,
        severity=Severity.MEDIUM.value,
        node_id="power-007",
        subsystem="power",
        timestamp="2026-01-01T00:05:00+00:00",
        reason="Field 'load' value 97.8 is within the overload band (95.0, 100.0).",
        evidence={"field": "load", "value": 97.8},
    )
    defaults.update(overrides)
    return DetectionResult(**defaults)


# --- 1 & 2. detected=false -> no trigger, detected=true -> trigger ---


def test_detected_false_does_not_trigger():
    decision = evaluate(_detection(detected=False, fault_type=None, severity=Severity.NORMAL.value))
    assert decision.should_trigger is False
    assert decision.detection_result is None
    assert decision.execution_name is None


def test_detected_true_triggers():
    decision = evaluate(_detection())
    assert decision.should_trigger is True
    assert decision.detection_result is not None
    assert decision.execution_name is not None


# --- 3. DetectionResult payload preserved exactly ---


def test_payload_preserves_the_exact_detection_result_not_wrapped():
    detection = _detection()
    decision = evaluate(detection)

    assert decision.detection_result == detection.to_dict()
    assert "detection_result" not in decision.detection_result  # never double-wrapped


# --- 4. Malformed input rejected ---


def test_non_detection_result_type_raises():
    with pytest.raises(InvalidDetectionResultError):
        evaluate("not a DetectionResult")

    with pytest.raises(InvalidDetectionResultError):
        evaluate(None)

    with pytest.raises(InvalidDetectionResultError):
        evaluate(42)


# --- 5. Missing detected field rejected ---


def test_dict_missing_detected_field_is_rejected_as_wrong_type():
    # evaluate() only accepts a real DetectionResult instance — a plain
    # dict (with or without a 'detected' key) is rejected by the same
    # type check, since evaluate()'s real caller always already has a
    # DetectionResult object in hand (see trigger.py's docstring).
    malformed = {"fault_type": "NODE_OVERLOAD", "node_id": "power-007"}
    with pytest.raises(InvalidDetectionResultError):
        evaluate(malformed)


# --- 6. Missing node_id rejected ---


def test_missing_node_id_does_not_trigger():
    decision = evaluate(_detection(node_id=None))
    assert decision.should_trigger is False
    assert decision.detection_result is None


def test_empty_string_node_id_does_not_trigger():
    decision = evaluate(_detection(node_id=""))
    assert decision.should_trigger is False


# --- 7. Missing timestamp rejected ---


def test_missing_timestamp_does_not_trigger():
    decision = evaluate(_detection(timestamp=None))
    assert decision.should_trigger is False
    assert decision.detection_result is None


# --- 8 & 9. Deterministic execution name / same DetectionResult -> same identity ---


def test_execution_name_is_deterministic_for_the_same_detection_result():
    detection = _detection()
    name_a = evaluate(detection).execution_name
    name_b = evaluate(detection).execution_name
    assert name_a == name_b
    assert name_a is not None


def test_execution_name_obeys_step_functions_naming_constraints():
    name = evaluate(_detection()).execution_name
    assert 1 <= len(name) <= 80
    forbidden_chars = set(' <>{}[]?*"#%\\^|~`$&,;:/')
    assert not (forbidden_chars & set(name))


# --- 10. Different node_id -> different execution identity ---


def test_different_node_id_yields_different_execution_name():
    name_a = evaluate(_detection(node_id="power-001")).execution_name
    name_b = evaluate(_detection(node_id="power-002")).execution_name
    assert name_a != name_b


# --- 11. Different timestamp -> different execution identity ---


def test_different_timestamp_yields_different_execution_name():
    name_a = evaluate(_detection(timestamp="2026-01-01T00:00:00+00:00")).execution_name
    name_b = evaluate(_detection(timestamp="2026-01-01T00:00:01+00:00")).execution_name
    assert name_a != name_b


# --- 12. Same node/timestamp, changed evidence -> matches documented strategy ---


def test_same_node_and_timestamp_with_different_evidence_collapses_to_same_identity():
    # Documented, deliberate idempotency policy: node_id + timestamp +
    # fault_type identify "the same underlying anomaly" — evidence is
    # diagnostic detail about that same anomaly, not a separate identity
    # dimension. Two DetectionResults sharing all three identity fields
    # are treated as duplicates even if evidence differs, since in this
    # project's real data model (detect_telemetry() is a pure function of
    # telemetry uniquely keyed by node_id+timestamp) this can only arise
    # from a non-standard/manually-constructed input — and collapsing
    # them is the conservative, safe choice (never double-trigger a real
    # corrective action) rather than the risky one.
    name_a = evaluate(_detection(evidence={"field": "load", "value": 97.8})).execution_name
    name_b = evaluate(_detection(evidence={"field": "load", "value": 99.9, "extra": "different"})).execution_name
    assert name_a == name_b


def test_execution_name_is_independent_of_severity_and_reason():
    # Only node_id + timestamp + fault_type feed the identity — severity
    # and reason text are not identity dimensions either.
    name_a = evaluate(_detection(severity=Severity.MEDIUM.value, reason="reason A")).execution_name
    name_b = evaluate(_detection(severity=Severity.HIGH.value, reason="a completely different reason")).execution_name
    assert name_a == name_b


# --- compute_execution_name direct unit tests ---


def test_compute_execution_name_matches_evaluate():
    detection = _detection()
    assert compute_execution_name(detection.node_id, detection.timestamp, detection.fault_type) == evaluate(
        detection
    ).execution_name


def test_compute_execution_name_has_stable_prefix():
    name = compute_execution_name("power-007", "2026-01-01T00:05:00+00:00", "NODE_OVERLOAD")
    assert name.startswith("sentinelgrid-remediation-")


# --- 13, 14, 15. SENSOR_DROPOUT / MEDIUM NODE_OVERLOAD / HIGH NODE_OVERLOAD trigger ---


def test_sensor_dropout_triggers():
    decision = evaluate(_detection(fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value))
    assert decision.should_trigger is True


def test_medium_node_overload_triggers():
    decision = evaluate(_detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value))
    assert decision.should_trigger is True


def test_high_node_overload_triggers():
    # The trigger does not itself decide automatic-vs-escalate policy —
    # it triggers regardless of severity, and RemediationEngine.plan()
    # (downstream, inside the Step Functions workflow) is what decides
    # HIGH severity requires escalation. See trigger.py's evaluate()
    # docstring.
    decision = evaluate(_detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value))
    assert decision.should_trigger is True


# --- 16. INVALID_INPUT behavior explicitly defined ---


def test_invalid_input_with_usable_identity_triggers():
    # e.g. an unsupported subsystem value on an otherwise well-formed
    # message: node_id/timestamp are still present, so there is a valid
    # execution identity, and the workflow's own REJECT_INPUT/escalation
    # path (already proven in earlier steps) can run.
    decision = evaluate(
        _detection(
            fault_type=FaultType.INVALID_INPUT.value,
            severity=Severity.CRITICAL.value,
            node_id="power-007",
            timestamp="2026-01-01T00:05:00+00:00",
        )
    )
    assert decision.should_trigger is True


def test_invalid_input_without_usable_identity_does_not_trigger():
    # e.g. telemetry that failed to parse as JSON at all: node_id and
    # timestamp are None, so there is nothing to key an execution name on
    # and nothing for the workflow to act on.
    decision = evaluate(
        _detection(
            fault_type=FaultType.INVALID_INPUT.value,
            severity=Severity.CRITICAL.value,
            node_id=None,
            subsystem=None,
            timestamp=None,
            evidence={"raw_event_type": "str"},
        )
    )
    assert decision.should_trigger is False


# --- 17. Normal telemetry never produces a remediation trigger ---


def test_normal_telemetry_via_real_detector_never_triggers():
    telemetry = {
        "node_id": "power-001",
        "subsystem": "power",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "status": "ONLINE",
        "data": {"load": 55.0, "voltage": 230.0, "frequency": 50.0},
    }
    detection = detect_telemetry(telemetry)  # the real, deployed detection logic
    decision = evaluate(detection)
    assert decision.should_trigger is False


# --- 18. No AWS/network/subprocess imports in the PURE decision module ---
#
# Step 21 added handler.py, the AWS-facing adapter, which legitimately
# imports boto3/botocore to call stepfunctions:StartExecution — see
# handler.py's own module docstring and this package's README "AWS
# dependency boundary" section. The architectural boundary this project
# actually promises is narrower than "the whole directory": trigger.py
# (the pure decision logic) must never import an AWS SDK, network, or
# subprocess module, and handler.py must be the ONLY file in this
# package that does.


_FORBIDDEN_MODULES = {"boto3", "botocore", "socket", "urllib", "http", "requests", "subprocess"}
_PURE_MODULE_FILES = ("trigger.py", "__init__.py")


def _imported_top_level_modules(py_file: pathlib.Path) -> set:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    return names


def test_pure_trigger_module_has_no_aws_or_network_or_subprocess_imports():
    for filename in _PURE_MODULE_FILES:
        py_file = TRIGGER_PACKAGE_DIR / filename
        names = _imported_top_level_modules(py_file)
        assert not (names & _FORBIDDEN_MODULES), f"{filename} imports forbidden module(s): {names & _FORBIDDEN_MODULES}"


def test_handler_module_is_the_only_file_that_imports_boto3():
    # Confirms the architectural boundary explicitly, rather than just
    # forbidding boto3 everywhere: handler.py is EXPECTED to import
    # boto3/botocore (it is the AWS-facing adapter), and it must be the
    # only .py file in this package that does.
    files_importing_aws_sdk = []
    for py_file in TRIGGER_PACKAGE_DIR.glob("*.py"):
        names = _imported_top_level_modules(py_file)
        if names & {"boto3", "botocore"}:
            files_importing_aws_sdk.append(py_file.name)

    assert files_importing_aws_sdk == ["handler.py"]


def test_trigger_package_performs_no_filesystem_writes():
    write_modes = {"w", "wb", "a", "ab", "x", "xb", "w+", "a+"}
    for py_file in TRIGGER_PACKAGE_DIR.glob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                args = node.args
                mode = None
                if len(args) >= 2 and isinstance(args[1], ast.Constant):
                    mode = args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                assert mode not in write_modes, f"{py_file.name} opens a file in write mode ({mode!r})"


def test_evaluate_does_not_mutate_its_argument():
    import copy

    detection = _detection()
    original = copy.deepcopy(detection)
    evaluate(detection)
    assert detection == original


# ============================================================================
# TASK 7 — Local end-to-end contract test:
#   DetectionResult -> Trigger decision -> Step Functions input
# ============================================================================


def test_end_to_end_medium_node_overload_produces_valid_step_functions_input():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)

    decision = evaluate(detection)

    assert decision.should_trigger is True
    assert decision.detection_result == detection.to_dict()
    assert decision.execution_name == compute_execution_name(
        detection.node_id, detection.timestamp, detection.fault_type
    )

    # The resulting payload must be usable as the Step Functions
    # execution input EXACTLY as-is — i.e. it must satisfy the same
    # contract step_functions/remediation_workflow.json's CheckAnomaly
    # state relies on ($.detected) without any further transformation.
    step_functions_input = decision.detection_result
    assert step_functions_input["detected"] is True

    # Prove it also feeds cleanly into the existing, already-verified
    # remediation Lambda contract chain (Step 16A/16B/19) without any
    # reshaping: the planner's real lambda_handler accepts exactly this
    # shape as its event.
    from lambda_functions.remediation import planner as planner_module
    from lambda_functions.remediation.planner import lambda_handler as planner_lambda_handler

    # Step 28/33/34: prevents real CloudWatch/DynamoDB calls from
    # planner.py's own module-level _metrics_client/_recovery_event_store/
    # _node_state_store — this MEDIUM/automatic=True case would not
    # publish UnresolvedFaultCount or persist a recovery event
    # (escalation_required is False here), but it DOES write RECOVERING
    # node state (automatic=True), so all three are patched.
    with patch.object(planner_module, "_metrics_client"), patch.object(
        planner_module, "_recovery_event_store"
    ), patch.object(planner_module, "_node_state_store"):
        plan = planner_lambda_handler(step_functions_input, None)
    assert "error" not in plan
    assert plan["action"]["action_type"] == "REDUCE_LOAD"
    assert plan["automatic"] is True


def test_trigger_decision_to_dict_is_json_serializable():
    import json

    decision = evaluate(_detection())
    json.dumps(decision.to_dict())  # must not raise

    no_trigger_decision = evaluate(_detection(detected=False, fault_type=None, severity=Severity.NORMAL.value))
    json.dumps(no_trigger_decision.to_dict())  # must not raise


# ============================================================================
# TASK 10 — AWS-facing handler tests (boto3 mocked at the AWS boundary)
# ============================================================================


def _detection_event(**overrides) -> dict:
    return _detection(**overrides).to_dict()


def _patched_client(mock_client_factory):
    return patch("lambda_functions.remediation_trigger.handler.boto3.client", return_value=mock_client_factory)


def _with_state_machine_arn(monkeypatch, arn=_FAKE_STATE_MACHINE_ARN):
    monkeypatch.setenv(STATE_MACHINE_ARN_ENV_VAR, arn)


# --- A. detected=false -> StartExecution NOT called ---


def test_handler_does_not_call_start_execution_for_normal_telemetry(monkeypatch):
    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()
    with _patched_client(mock_client):
        response = trigger_lambda_handler(
            _detection_event(detected=False, fault_type=None, severity=Severity.NORMAL.value), None
        )

    mock_client.start_execution.assert_not_called()
    assert response["triggered"] is False
    assert response["started_execution"] is False


# --- B. MEDIUM NODE_OVERLOAD -> StartExecution called exactly once, exact args ---


def test_handler_starts_execution_for_medium_node_overload_with_exact_unwrapped_input(monkeypatch):
    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()
    mock_client.start_execution.return_value = {"executionArn": "arn:aws:states:...:execution:...:fake"}

    event = _detection_event(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)

    with _patched_client(mock_client):
        response = trigger_lambda_handler(event, None)

    mock_client.start_execution.assert_called_once()
    call_kwargs = mock_client.start_execution.call_args.kwargs

    assert call_kwargs["stateMachineArn"] == _FAKE_STATE_MACHINE_ARN
    assert call_kwargs["name"] == compute_execution_name(
        event["node_id"], event["timestamp"], event["fault_type"]
    )

    sent_input = json.loads(call_kwargs["input"])
    assert sent_input == event
    assert "detection_result" not in sent_input  # never wrapped

    assert response["triggered"] is True
    assert response["started_execution"] is True


# --- C. SENSOR_DROPOUT -> StartExecution called ---


def test_handler_starts_execution_for_sensor_dropout(monkeypatch):
    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()
    mock_client.start_execution.return_value = {"executionArn": "fake"}

    event = _detection_event(fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value)
    with _patched_client(mock_client):
        trigger_lambda_handler(event, None)

    mock_client.start_execution.assert_called_once()


# --- D. HIGH NODE_OVERLOAD -> StartExecution called (trigger doesn't gate on automatic-ness) ---


def test_handler_starts_execution_for_high_node_overload(monkeypatch):
    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()
    mock_client.start_execution.return_value = {"executionArn": "fake"}

    event = _detection_event(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    with _patched_client(mock_client):
        response = trigger_lambda_handler(event, None)

    mock_client.start_execution.assert_called_once()
    assert response["triggered"] is True


# --- E. INVALID_INPUT -> follows the documented trigger contract ---


def test_handler_invalid_input_with_identity_starts_execution(monkeypatch):
    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()
    mock_client.start_execution.return_value = {"executionArn": "fake"}

    event = _detection_event(
        fault_type=FaultType.INVALID_INPUT.value,
        severity=Severity.CRITICAL.value,
        node_id="power-007",
        timestamp="2026-01-01T00:05:00+00:00",
    )
    with _patched_client(mock_client):
        response = trigger_lambda_handler(event, None)

    mock_client.start_execution.assert_called_once()
    assert response["triggered"] is True


def test_handler_invalid_input_without_identity_does_not_start_execution(monkeypatch):
    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()

    event = _detection_event(
        fault_type=FaultType.INVALID_INPUT.value,
        severity=Severity.CRITICAL.value,
        node_id=None,
        subsystem=None,
        timestamp=None,
    )
    with _patched_client(mock_client):
        response = trigger_lambda_handler(event, None)

    mock_client.start_execution.assert_not_called()
    assert response["triggered"] is False


# --- F. Duplicate StartExecution response -> idempotent success/no-op ---


def test_handler_treats_execution_already_exists_as_idempotent_success(monkeypatch):
    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()
    mock_client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "already exists"}}, "StartExecution"
    )

    event = _detection_event(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    with _patched_client(mock_client):
        response = trigger_lambda_handler(event, None)

    assert response["triggered"] is True
    assert response["started_execution"] is False
    assert response["already_existed"] is True


# --- G. Unexpected AWS error -> not falsely reported as successful ---


def test_handler_raises_on_unexpected_start_execution_error(monkeypatch):
    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()
    mock_client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "rate exceeded"}}, "StartExecution"
    )

    event = _detection_event(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    with _patched_client(mock_client):
        with pytest.raises(TriggerExecutionError):
            trigger_lambda_handler(event, None)


# --- H. Missing/invalid state-machine ARN -> fails safely ---


def test_handler_raises_when_state_machine_arn_not_configured(monkeypatch):
    monkeypatch.delenv(STATE_MACHINE_ARN_ENV_VAR, raising=False)
    mock_client = MagicMock()

    event = _detection_event(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    with _patched_client(mock_client):
        with pytest.raises(MissingStateMachineArnError):
            trigger_lambda_handler(event, None)

    mock_client.start_execution.assert_not_called()


# --- I. Malformed input -> fails per the existing handler contract ---


def test_handler_malformed_event_returns_structured_error(monkeypatch):
    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()
    with _patched_client(mock_client):
        response = trigger_lambda_handler("not a dict", None)

    assert response["error"] == "invalid_detection_result"
    mock_client.start_execution.assert_not_called()


def test_handler_does_not_mutate_input_event(monkeypatch):
    import copy

    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()
    mock_client.start_execution.return_value = {"executionArn": "fake"}

    event = _detection_event(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    original = copy.deepcopy(event)
    with _patched_client(mock_client):
        trigger_lambda_handler(event, None)

    assert event == original


def test_handler_response_is_json_serializable(monkeypatch):
    _with_state_machine_arn(monkeypatch)
    mock_client = MagicMock()
    mock_client.start_execution.return_value = {"executionArn": "fake"}

    event = _detection_event(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    with _patched_client(mock_client):
        response = trigger_lambda_handler(event, None)
    json.dumps(response)  # must not raise
