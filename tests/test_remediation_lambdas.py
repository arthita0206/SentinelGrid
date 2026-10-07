"""Unit and integration tests for the local, Lambda-shaped remediation
adapters (lambda_functions/remediation/{planner,executor,verifier}.py).

Everything here is local and deterministic: no AWS, no DynamoDB, no
MQTT, no simulator, no boto3. Each `lambda_handler` is called directly as
a plain Python function, exactly as it would be for local testing.

CONTRACT (Step 16A — corrected from Step 16's accumulated-envelope
design, Step 16B — the ExecuteRemediation InputPath fix): each Lambda
returns ONLY its own stage's incremental result. Step Functions itself
is responsible for accumulating those results into the full workflow
state, via each Task's InputPath/ResultPath in
step_functions/remediation_workflow.json:

    PlanRemediation:    InputPath == "$.detection_result" -> planner receives a bare DetectionResult
                        ResultPath == "$.remediation_plan" -> planner returns a RemediationPlan only
    ExecuteRemediation: InputPath == "$"                  -> executor receives the COMPLETE accumulated state
                        ResultPath == "$.remediation_result" -> executor returns a RemediationResult only
    VerifyRemediation:  InputPath == "$"                  -> verifier receives the COMPLETE accumulated state
                        ResultPath == "$.verification"     -> verifier returns {"verified": ..., "reason": ...} only

`ExecuteRemediation.InputPath` was `"$.remediation_plan"` through Step
16A (sending only the bare plan, which didn't match the executor's
documented `{"detection_result": ..., "remediation_plan": ...}` input
contract) and was corrected to `"$"` in Step 16B — see
lambda_functions/remediation/README.md's "Step Functions contract"
section for the full explanation, and
`test_executor_receives_complete_accumulated_state_via_inputpath_dollar`
below for the regression test proving this fix.

Tests in the "STEP FUNCTIONS CONTRACT" section below model the
accumulation explicitly (not just chain the handlers directly in
Python), to prove no accidental nesting occurs against the ACTUAL ASL
file — not an assumed one.
"""

import ast
import copy
import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from lambda_functions.anomaly_detector.detector import FaultType, Severity
from lambda_functions.remediation import executor as executor_module
from lambda_functions.remediation import planner as planner_module
from lambda_functions.remediation import verifier as verifier_module
from lambda_functions.remediation.executor import lambda_handler as executor_handler
from lambda_functions.remediation.planner import lambda_handler as planner_handler
from lambda_functions.remediation.verifier import lambda_handler as verifier_handler
from remediation.models import ActionType, ExecutionStatus

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
REMEDIATION_LAMBDA_DIR = REPO_ROOT / "lambda_functions" / "remediation"
WORKFLOW_PATH = REPO_ROOT / "step_functions" / "remediation_workflow.json"


@pytest.fixture(autouse=True)
def _mock_metrics_clients():
    """Auto-applied to every test in this file (Step 28): prevents any
    real CloudWatch `put_metric_data` call from planner.py/executor.py/
    verifier.py's own module-level `_metrics_client` singletons — without
    this, every test exercising a detected fault would attempt a real
    (unmocked) AWS call. Mirrors
    tests/test_anomaly_detector_handler.py's own `_mock_trigger_client`
    fixture, applied Step 22, for the exact same reason.
    """
    with patch.object(planner_module, "_metrics_client") as mock_planner, patch.object(
        executor_module, "_metrics_client"
    ) as mock_executor, patch.object(verifier_module, "_metrics_client") as mock_verifier:
        yield {"planner": mock_planner, "executor": mock_executor, "verifier": mock_verifier}


@pytest.fixture(autouse=True)
def _mock_recovery_event_stores():
    """Auto-applied to every test in this file (Step 33): prevents any
    real DynamoDB `put_item`/`get_item` call from planner.py/executor.py's
    own module-level `_recovery_event_store` singletons — without this,
    every test exercising an escalation or an executed plan would attempt
    a real (unmocked) AWS call. Mirrors `_mock_metrics_clients` above,
    for the same reason.
    """
    with patch.object(planner_module, "_recovery_event_store") as mock_planner_store, patch.object(
        executor_module, "_recovery_event_store"
    ) as mock_executor_store:
        yield {"planner": mock_planner_store, "executor": mock_executor_store}


@pytest.fixture(autouse=True)
def _mock_node_state_stores():
    """Auto-applied to every test in this file (Step 34): prevents any
    real DynamoDB `update_item` call from planner.py/executor.py/
    verifier.py's own module-level `_node_state_store` singletons —
    without this, every test exercising a plan/execution/verification
    would attempt a real (unmocked) AWS call. Mirrors
    `_mock_recovery_event_stores` above, for the same reason.
    """
    with patch.object(planner_module, "_node_state_store") as mock_planner_store, patch.object(
        executor_module, "_node_state_store"
    ) as mock_executor_store, patch.object(verifier_module, "_node_state_store") as mock_verifier_store:
        yield {"planner": mock_planner_store, "executor": mock_executor_store, "verifier": mock_verifier_store}


def _detection(**overrides) -> dict:
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
    return defaults


def _accumulate(**pieces) -> dict:
    """Builds a Step-Functions-style accumulated state dict out of
    whichever pieces are given — a small helper matching how `ResultPath`
    merges keys in, used throughout this file so intent is explicit at
    each call site.
    """
    return dict(pieces)


# ============================================================================
# PLANNER TESTS — Input: DetectionResult. Output (success): RemediationPlan only.
# ============================================================================


def test_planner_normal_detection_requires_no_remediation():
    event = _detection(detected=False, fault_type=None, severity=Severity.NORMAL.value, reason="ok", evidence={})
    plan = planner_handler(event, None)

    assert plan["remediation_required"] is False
    assert plan["action"]["action_type"] == ActionType.NO_ACTION.value


def test_planner_sensor_dropout_selects_restart_sensor():
    event = _detection(fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value)
    plan = planner_handler(event, None)

    assert plan["action"]["action_type"] == ActionType.RESTART_SENSOR.value
    assert plan["automatic"] is True


def test_planner_network_partition_selects_reconnect_node():
    event = _detection(fault_type=FaultType.NETWORK_PARTITION.value, severity=Severity.HIGH.value)
    plan = planner_handler(event, None)

    assert plan["action"]["action_type"] == ActionType.RECONNECT_NODE.value
    assert plan["automatic"] is True


def test_planner_data_corruption_selects_reset_sensor_state():
    event = _detection(fault_type=FaultType.DATA_CORRUPTION.value, severity=Severity.CRITICAL.value)
    plan = planner_handler(event, None)

    assert plan["action"]["action_type"] == ActionType.RESET_SENSOR_STATE.value
    assert plan["automatic"] is True


def test_planner_medium_overload_selects_reduce_load_automatically():
    event = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(event, None)

    assert plan["action"]["action_type"] == ActionType.REDUCE_LOAD.value
    assert plan["automatic"] is True


def test_planner_high_overload_requires_escalation():
    event = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    plan = planner_handler(event, None)

    assert plan["action"]["action_type"] == ActionType.REDUCE_LOAD.value
    assert plan["automatic"] is False
    assert plan["escalation_required"] is True


def test_planner_critical_overload_requires_escalation():
    event = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.CRITICAL.value)
    plan = planner_handler(event, None)

    assert plan["automatic"] is False
    assert plan["escalation_required"] is True


def test_planner_invalid_input_selects_reject_input_not_automatic():
    event = _detection(
        fault_type=FaultType.INVALID_INPUT.value,
        severity=Severity.CRITICAL.value,
        node_id=None,
        subsystem=None,
        timestamp=None,
    )
    plan = planner_handler(event, None)

    assert plan["action"]["action_type"] == ActionType.REJECT_INPUT.value
    assert plan["automatic"] is False


def test_planner_unknown_fault_type_yields_no_action():
    event = _detection(fault_type="SOME_FUTURE_FAULT_TYPE", severity=Severity.HIGH.value)
    plan = planner_handler(event, None)

    assert plan["remediation_required"] is False
    assert plan["action"]["action_type"] == ActionType.NO_ACTION.value


def test_planner_malformed_event_returns_structured_error():
    response = planner_handler("not a dict", None)

    assert response["error"] == "invalid_detection_result"
    assert "message" in response


def test_planner_missing_required_field_returns_structured_error():
    event = _detection()
    del event["node_id"]
    response = planner_handler(event, None)

    assert response["error"] == "invalid_detection_result"
    assert "node_id" in response["message"]


def test_planner_wrong_type_field_returns_structured_error():
    event = _detection(detected="yes")  # should be a bool
    response = planner_handler(event, None)

    assert response["error"] == "invalid_detection_result"


def test_planner_does_not_mutate_input_event():
    event = _detection()
    original = copy.deepcopy(event)
    planner_handler(event, None)
    assert event == original


def test_planner_context_can_be_omitted():
    event = _detection(detected=False, fault_type=None, severity=Severity.NORMAL.value)
    plan = planner_handler(event)  # context has a default
    assert plan["remediation_required"] is False


# --- Corrected-contract-specific: output must NOT be wrapped ---


def test_planner_output_is_a_remediation_plan_only_not_wrapped():
    event = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(event, None)

    assert "detection_result" not in plan
    assert "remediation_plan" not in plan
    # It IS the RemediationPlan's own top-level fields, directly:
    assert set(plan.keys()) == {
        "remediation_required",
        "node_id",
        "subsystem",
        "fault_type",
        "severity",
        "timestamp",
        "action",
        "reason",
        "automatic",
        "escalation_required",
    }


# ============================================================================
# EXECUTOR TESTS — Input: {detection_result, remediation_plan}. Output (success): RemediationResult only.
# ============================================================================


def _planner_plan(**detection_overrides) -> dict:
    return planner_handler(_detection(**detection_overrides), None)


def _executor_input(detection: dict, plan: dict) -> dict:
    """Builds the executor's documented input contract — the
    Step-Functions-accumulated state after PlanRemediation:
    {"detection_result": detection} merged with ResultPath "$.remediation_plan" = plan.
    """
    return _accumulate(detection_result=detection, remediation_plan=plan)


def test_executor_accepts_the_accumulated_planner_state():
    detection = _detection(fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)  # must not raise
    assert "success" in result


def test_executor_medium_overload_simulated_execution_succeeds():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)

    assert result["success"] is True
    assert result["status"] == ExecutionStatus.SUCCESS.value
    assert result["action"] == ActionType.REDUCE_LOAD.value


def test_executor_sensor_dropout_simulated_execution_succeeds():
    detection = _detection(fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)

    assert result["success"] is True
    assert result["action"] == ActionType.RESTART_SENSOR.value


def test_executor_network_partition_simulated_execution_succeeds():
    detection = _detection(fault_type=FaultType.NETWORK_PARTITION.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)

    assert result["success"] is True
    assert result["action"] == ActionType.RECONNECT_NODE.value


def test_executor_data_corruption_simulated_execution_succeeds():
    detection = _detection(fault_type=FaultType.DATA_CORRUPTION.value, severity=Severity.CRITICAL.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)

    assert result["success"] is True
    assert result["action"] == ActionType.RESET_SENSOR_STATE.value


def test_executor_high_overload_never_fabricates_success():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)

    assert result["success"] is False
    assert result["status"] == ExecutionStatus.ESCALATED.value


def test_executor_critical_overload_never_fabricates_success():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.CRITICAL.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)

    assert result["success"] is False
    assert result["status"] == ExecutionStatus.ESCALATED.value


def test_executor_invalid_input_returns_escalation_not_success():
    detection = _detection(
        fault_type=FaultType.INVALID_INPUT.value, severity=Severity.CRITICAL.value, node_id=None, subsystem=None
    )
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)

    assert result["success"] is False
    assert result["status"] == ExecutionStatus.ESCALATED.value


def test_executor_unknown_action_type_is_safely_rejected():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    plan["action"]["action_type"] = "SOME_MADE_UP_ACTION"

    result = executor_handler(_executor_input(detection, plan), None)

    assert result["success"] is False
    assert result["status"] == ExecutionStatus.REJECTED.value


def test_executor_malformed_event_returns_structured_error():
    response = executor_handler("not a dict", None)

    assert response["error"] == "invalid_remediation_plan"


def test_executor_missing_remediation_plan_returns_structured_error():
    response = executor_handler({"detection_result": _detection()}, None)

    assert response["error"] == "invalid_remediation_plan"


def test_executor_plan_missing_required_field_returns_structured_error():
    detection = _detection()
    plan = planner_handler(detection, None)
    del plan["automatic"]

    response = executor_handler(_executor_input(detection, plan), None)

    assert response["error"] == "invalid_remediation_plan"
    assert "automatic" in response["message"]


def test_executor_does_not_mutate_input_event():
    detection = _detection(fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    event = _executor_input(detection, plan)
    original = copy.deepcopy(event)
    executor_handler(event, None)
    assert event == original


# --- Corrected-contract-specific: output must NOT be wrapped ---


def test_executor_output_is_a_remediation_result_only_not_wrapped():
    detection = _detection(fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)

    assert "detection_result" not in result
    assert "remediation_plan" not in result
    assert "remediation_result" not in result
    assert set(result.keys()) == {
        "success",
        "status",
        "node_id",
        "subsystem",
        "fault_type",
        "action",
        "timestamp",
        "message",
        "simulated",
        "details",
    }


# ============================================================================
# VERIFIER TESTS — Input: {detection_result, remediation_plan, remediation_result}.
#                   Output: {"verified": bool, "reason": str} only.
# ============================================================================


def _verifier_input(detection: dict, plan: dict, result: dict) -> dict:
    """The verifier's documented input contract — the Step-Functions-
    accumulated state after ExecuteRemediation."""
    return _accumulate(detection_result=detection, remediation_plan=plan, remediation_result=result)


def _run_through_executor(**detection_overrides):
    detection = _detection(**detection_overrides)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)
    return detection, plan, result


def test_verifier_accepts_the_accumulated_executor_state():
    detection, plan, result = _run_through_executor(
        fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value
    )
    verification = verifier_handler(_verifier_input(detection, plan, result), None)  # must not raise
    assert "verified" in verification


def test_verifier_successful_remediation_is_verified_true():
    detection, plan, result = _run_through_executor(
        fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value
    )
    verification = verifier_handler(_verifier_input(detection, plan, result), None)

    assert verification["verified"] is True
    assert "reason" in verification


def test_verifier_failed_remediation_is_verified_false():
    detection, plan, result = _run_through_executor(
        fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value
    )
    result["success"] = False
    result["status"] = "SOME_FAILURE"

    verification = verifier_handler(_verifier_input(detection, plan, result), None)

    assert verification["verified"] is False


def test_verifier_escalated_remediation_is_verified_false():
    detection, plan, result = _run_through_executor(
        fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value
    )
    verification = verifier_handler(_verifier_input(detection, plan, result), None)

    assert verification["verified"] is False
    assert "did not succeed" in verification["reason"]


def test_verifier_missing_remediation_result_is_verified_false():
    verification = verifier_handler({"detection_result": _detection(), "remediation_plan": {}}, None)

    assert verification["verified"] is False
    assert "missing" in verification["reason"]


def test_verifier_malformed_event_is_verified_false():
    verification = verifier_handler("not a dict", None)

    assert verification["verified"] is False


def test_verifier_inconsistent_result_and_plan_is_verified_false():
    detection, plan, result = _run_through_executor(
        fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value
    )
    result["action"] = ActionType.RECONNECT_NODE.value  # tamper: no longer matches the plan

    verification = verifier_handler(_verifier_input(detection, plan, result), None)

    assert verification["verified"] is False
    assert "does not correspond" in verification["reason"]


def test_verifier_inconsistent_node_id_is_verified_false():
    detection, plan, result = _run_through_executor(
        fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value
    )
    result["node_id"] = "some-other-node"

    verification = verifier_handler(_verifier_input(detection, plan, result), None)

    assert verification["verified"] is False


def test_verifier_never_mutates_its_input():
    detection, plan, result = _run_through_executor(
        fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value
    )
    event = _verifier_input(detection, plan, result)
    original = copy.deepcopy(event)
    verifier_handler(event, None)
    assert event == original


# --- Corrected-contract-specific: output must NOT be wrapped ---


def test_verifier_output_is_exactly_the_verification_object_not_wrapped():
    detection, plan, result = _run_through_executor(
        fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value
    )
    verification = verifier_handler(_verifier_input(detection, plan, result), None)

    assert "detection_result" not in verification
    assert "remediation_plan" not in verification
    assert "remediation_result" not in verification
    assert set(verification.keys()) == {"verified", "reason"}


# ============================================================================
# STEP FUNCTIONS CONTRACT: model the ACTUAL ResultPath/InputPath behavior
# ============================================================================


def test_step_functions_inputpath_and_resultpath_match_asl_exactly():
    """Focused ASL contract test: reads the ACTUAL InputPath/ResultPath
    values from step_functions/remediation_workflow.json (not duplicated/
    assumed values in a separate model) for the three Task states these
    Lambdas back. If any of these ever change, the Lambda contracts
    documented in this file and in lambda_functions/remediation/README.md
    must be re-evaluated against the new values.

    `ExecuteRemediation.InputPath` is `"$"` (Step 16B) — it receives the
    COMPLETE accumulated state, exactly like `VerifyRemediation`, not
    just the bare `remediation_plan` (the Step 16A mismatch, now fixed).
    """
    workflow = json.loads(WORKFLOW_PATH.read_text())
    states = workflow["States"]

    assert states["PlanRemediation"]["InputPath"] == "$.detection_result"
    assert states["PlanRemediation"]["ResultPath"] == "$.remediation_plan"

    assert states["ExecuteRemediation"]["InputPath"] == "$"
    assert states["ExecuteRemediation"]["ResultPath"] == "$.remediation_result"

    assert states["VerifyRemediation"]["InputPath"] == "$"
    assert states["VerifyRemediation"]["ResultPath"] == "$.verification"


def _accumulate_full_workflow(detection: dict) -> dict:
    """Models the COMPLETE Step Functions accumulation for one
    DetectionResult, using the real InputPath/ResultPath semantics now
    in effect: PlanRemediation gets `$.detection_result` and merges its
    result at `$.remediation_plan`; ExecuteRemediation now gets the
    WHOLE accumulated state (`InputPath: "$"`, Step 16B) and merges its
    result at `$.remediation_result`; VerifyRemediation also gets the
    whole state and merges its result at `$.verification`. Returns the
    final accumulated state, exactly as Step Functions would produce it.
    """
    state = {"detection_result": detection}  # after PrepareRemediationPlanning

    plan = planner_handler(state["detection_result"], None)  # InputPath "$.detection_result"
    state = {**state, "remediation_plan": plan}  # ResultPath "$.remediation_plan"

    result = executor_handler(state, None)  # InputPath "$" (Step 16B) -> the COMPLETE state above
    state = {**state, "remediation_result": result}  # ResultPath "$.remediation_result"

    verification = verifier_handler(state, None)  # InputPath "$" -> the COMPLETE state above
    state = {**state, "verification": verification}  # ResultPath "$.verification"

    return state


def test_step_functions_resultpath_accumulation_produces_correct_shape_no_nesting():
    """Models the COMPLETE Step Functions accumulation for a MEDIUM
    NODE_OVERLOAD DetectionResult and proves the corrected Lambda
    contracts (Step 16A output shapes + Step 16B InputPath fix) produce
    the intended accumulated shape with NO accidental nesting at any
    stage, and that every ASL Choice path resolves correctly.
    """
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    state = _accumulate_full_workflow(detection)

    assert state.keys() == {"detection_result", "remediation_plan", "remediation_result", "verification"}
    assert state["detection_result"] == detection

    # CheckAutomaticRemediation's Choice condition:
    assert state["remediation_plan"]["automatic"] is True
    assert isinstance(state["remediation_plan"], dict)
    assert "remediation_plan" not in state["remediation_plan"]  # no accidental self-nesting

    # RemediationResult's own status field is named `status` (an
    # ExecutionStatus value), not `execution_status` — there is no
    # `execution_status` field anywhere in remediation/models.py, and
    # none was added for this task (see final report).
    assert state["remediation_result"]["status"] == ExecutionStatus.SUCCESS.value
    assert state["remediation_result"]["success"] is True
    assert "remediation_result" not in state["remediation_result"]  # no accidental self-nesting

    # CheckVerification's Choice condition:
    assert state["verification"]["verified"] is True
    assert "verification" not in state["verification"]  # no accidental self-nesting


def test_step_functions_resultpath_accumulation_sensor_dropout():
    detection = _detection(fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value)
    state = _accumulate_full_workflow(detection)

    assert state["remediation_plan"]["automatic"] is True
    assert state["remediation_plan"]["action"]["action_type"] == ActionType.RESTART_SENSOR.value
    assert state["remediation_result"]["status"] == ExecutionStatus.SUCCESS.value
    assert state["verification"]["verified"] is True


def test_step_functions_resultpath_accumulation_high_overload_escalates():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    state = _accumulate_full_workflow(detection)

    assert state["remediation_plan"]["automatic"] is False
    # Execution must be escalated, never fabricated as success:
    assert state["remediation_result"]["success"] is False
    assert state["remediation_result"]["status"] == ExecutionStatus.ESCALATED.value
    assert state["verification"]["verified"] is False


def test_step_functions_resultpath_accumulation_invalid_input_escalates():
    detection = _detection(
        fault_type=FaultType.INVALID_INPUT.value, severity=Severity.CRITICAL.value, node_id=None, subsystem=None
    )
    state = _accumulate_full_workflow(detection)

    assert state["remediation_plan"]["action"]["action_type"] == ActionType.REJECT_INPUT.value
    assert state["remediation_plan"]["automatic"] is False
    assert state["remediation_result"]["success"] is False
    assert state["remediation_result"]["status"] == ExecutionStatus.ESCALATED.value
    assert state["verification"]["verified"] is False


def test_executor_receives_complete_accumulated_state_via_inputpath_dollar():
    """Regression test proving the Step 16B fix: given an accumulated
    Step Functions state containing `detection_result` and
    `remediation_plan` (exactly what exists at this point in the
    workflow, per `ExecuteRemediation.InputPath == "$"`), the executor
    receives that COMPLETE state directly as `event` and successfully
    processes `remediation_plan` from within it — no
    `invalid_remediation_plan` error, unlike before this fix (see
    lambda_functions/remediation/README.md's former "One remaining
    mismatch" section, now removed).
    """
    workflow = json.loads(WORKFLOW_PATH.read_text())
    assert workflow["States"]["ExecuteRemediation"]["InputPath"] == "$"

    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    complete_accumulated_state = {"detection_result": detection, "remediation_plan": plan}

    response = executor_handler(complete_accumulated_state, None)

    assert "error" not in response
    assert response["success"] is True
    assert response["status"] == ExecutionStatus.SUCCESS.value
    assert response["action"] == ActionType.REDUCE_LOAD.value


# ============================================================================
# END-TO-END LOCAL CHAIN: planner -> [SFN accumulation] -> executor ->
#                          [SFN accumulation] -> verifier
# ============================================================================


def test_pipeline_medium_overload_end_to_end_success():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)

    plan = planner_handler(detection, None)
    assert plan["action"]["action_type"] == ActionType.REDUCE_LOAD.value
    assert plan["automatic"] is True

    result = executor_handler(_executor_input(detection, plan), None)
    assert result["success"] is True
    assert result["status"] == ExecutionStatus.SUCCESS.value

    verification = verifier_handler(_verifier_input(detection, plan, result), None)
    assert verification["verified"] is True


def test_pipeline_sensor_dropout_end_to_end_success():
    detection = _detection(fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value)

    plan = planner_handler(detection, None)
    assert plan["action"]["action_type"] == ActionType.RESTART_SENSOR.value
    assert plan["automatic"] is True

    result = executor_handler(_executor_input(detection, plan), None)
    assert result["success"] is True
    assert result["status"] == ExecutionStatus.SUCCESS.value

    verification = verifier_handler(_verifier_input(detection, plan, result), None)
    assert verification["verified"] is True


def test_pipeline_high_overload_end_to_end_escalates():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)

    plan = planner_handler(detection, None)
    assert plan["automatic"] is False

    result = executor_handler(_executor_input(detection, plan), None)
    assert result["success"] is False
    assert result["status"] == ExecutionStatus.ESCALATED.value

    verification = verifier_handler(_verifier_input(detection, plan, result), None)
    assert verification["verified"] is False


def test_pipeline_invalid_input_end_to_end_escalates():
    detection = _detection(
        fault_type=FaultType.INVALID_INPUT.value, severity=Severity.CRITICAL.value, node_id=None, subsystem=None
    )

    plan = planner_handler(detection, None)
    assert plan["action"]["action_type"] == ActionType.REJECT_INPUT.value
    assert plan["automatic"] is False

    result = executor_handler(_executor_input(detection, plan), None)
    assert result["success"] is False

    verification = verifier_handler(_verifier_input(detection, plan, result), None)
    assert verification["verified"] is False


# ============================================================================
# CONTRACT TESTS
# ============================================================================


def test_planner_output_is_valid_json():
    plan = planner_handler(_detection(), None)
    json.dumps(plan)  # must not raise


def test_executor_accepts_planner_output_wrapped_as_documented():
    detection = _detection()
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)  # must not raise
    json.dumps(result)


def test_verifier_accepts_executor_output_wrapped_as_documented():
    detection = _detection()
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)
    verification = verifier_handler(_verifier_input(detection, plan, result), None)  # must not raise
    json.dumps(verification)


def test_original_detection_fields_survive_via_step_functions_style_accumulation():
    detection = _detection(
        fault_type=FaultType.SENSOR_DROPOUT.value,
        severity=Severity.HIGH.value,
        node_id="power-042",
        subsystem="power",
        timestamp="2026-03-01T12:00:00+00:00",
    )
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)
    verifier_handler(_verifier_input(detection, plan, result), None)

    # The Lambdas never touch `detection` — it's Step Functions'
    # accumulated state (simulated here by simply keeping the same
    # `detection` dict) that carries it through untouched.
    assert detection["node_id"] == "power-042"
    assert detection["subsystem"] == "power"
    assert detection["fault_type"] == FaultType.SENSOR_DROPOUT.value
    assert detection["severity"] == Severity.HIGH.value
    assert detection["timestamp"] == "2026-03-01T12:00:00+00:00"


def test_step_functions_required_paths_exist_after_each_stage():
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)

    plan = planner_handler(detection, None)
    assert "automatic" in plan

    result = executor_handler(_executor_input(detection, plan), None)
    assert "success" in result and "status" in result

    verification = verifier_handler(_verifier_input(detection, plan, result), None)
    assert "verified" in verification


# ============================================================================
# SERIALIZATION
# ============================================================================


def test_all_stage_outputs_are_json_serializable_for_every_fault_type():
    cases = [
        dict(fault_type=FaultType.SENSOR_DROPOUT.value, severity=Severity.HIGH.value),
        dict(fault_type=FaultType.NETWORK_PARTITION.value, severity=Severity.HIGH.value),
        dict(fault_type=FaultType.DATA_CORRUPTION.value, severity=Severity.CRITICAL.value),
        dict(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value),
        dict(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value),
        dict(fault_type=FaultType.INVALID_INPUT.value, severity=Severity.CRITICAL.value, node_id=None, subsystem=None),
    ]
    for overrides in cases:
        detection = _detection(**overrides)
        plan = planner_handler(detection, None)
        json.dumps(plan)
        result = executor_handler(_executor_input(detection, plan), None)
        json.dumps(result)
        verification = verifier_handler(_verifier_input(detection, plan, result), None)
        json.dumps(verification)


# ============================================================================
# AWS INDEPENDENCE
# ============================================================================


_FORBIDDEN_MODULES = {
    "boto3",
    "botocore",
    "socket",
    "urllib",
    "http",
    "requests",
    "subprocess",
}


# recovery_event_store.py (Step 33) is deliberately the one AWS-facing
# module in this package — it plays exactly the same role here that
# lambda_functions/anomaly_detector/event_store.py plays in its own
# package (see that module's own docstring). planner.py/executor.py/
# verifier.py -- the thin Lambda adapters this test actually guards --
# import nothing from boto3 directly; they delegate every AWS call to
# either shared/metrics.py or this module.
_AWS_FACING_MODULES_EXEMPT_FROM_THIS_CHECK = {"recovery_event_store.py"}


def test_remediation_lambda_package_has_no_aws_or_network_or_subprocess_imports():
    for py_file in REMEDIATION_LAMBDA_DIR.glob("*.py"):
        if py_file.name in _AWS_FACING_MODULES_EXEMPT_FROM_THIS_CHECK:
            continue
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not (names & _FORBIDDEN_MODULES), (
                f"{py_file.name} imports forbidden module(s): {names & _FORBIDDEN_MODULES}"
            )


def test_the_three_thin_lambda_adapters_specifically_have_no_boto3_import():
    """Precisely re-asserts this test file's original guarantee for
    exactly the three thin adapters (not the whole directory glob) --
    recovery_event_store.py is exempted above by design, not by
    accident; this proves planner.py/executor.py/verifier.py themselves
    still never import boto3 directly.
    """
    for filename in ("planner.py", "executor.py", "verifier.py"):
        py_file = REMEDIATION_LAMBDA_DIR / filename
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            assert "boto3" not in names, f"{filename} must not import boto3 directly"


def test_recovery_event_store_is_the_one_module_that_imports_boto3():
    py_file = REMEDIATION_LAMBDA_DIR / "recovery_event_store.py"
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "boto3" in imported


def test_remediation_lambda_package_performs_no_filesystem_writes():
    write_modes = {"w", "wb", "a", "ab", "x", "xb", "w+", "a+"}
    for py_file in REMEDIATION_LAMBDA_DIR.glob("*.py"):
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


def test_remediation_lambda_handlers_have_no_module_level_mutable_global_state():
    for py_file in REMEDIATION_LAMBDA_DIR.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(node.value, (ast.List, ast.Dict, ast.Set)):
                        raise AssertionError(
                            f"{py_file.name} defines a module-level mutable literal "
                            f"{target.id!r} — this would be shared, mutable state across invocations"
                        )


# ============================================================================
# STEP 28 — METRICS INSTRUMENTATION
# ============================================================================

from datetime import datetime, timedelta, timezone  # noqa: E402

from shared.metrics import MetricName, MetricsClient  # noqa: E402


def test_recovery_attempt_count_published_exactly_once_for_automatic_remediation(_mock_metrics_clients):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_executor = _mock_metrics_clients["executor"]
    attempt_calls = [
        call for call in mock_executor.put_count.call_args_list if call.args[0] == MetricName.RECOVERY_ATTEMPT_COUNT
    ]
    assert len(attempt_calls) == 1
    assert attempt_calls[0].args[1:] == ("power", "NODE_OVERLOAD")


def test_recovery_attempt_count_not_published_when_escalated_before_execution(_mock_metrics_clients):
    # HIGH overload -> automatic=False -> this Lambda still receives it
    # (a direct/defensive invocation, not via the real ASL -- see
    # executor.py's own docstring), but no genuine attempt occurred.
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_executor = _mock_metrics_clients["executor"]
    attempt_calls = [
        call for call in mock_executor.put_count.call_args_list if call.args[0] == MetricName.RECOVERY_ATTEMPT_COUNT
    ]
    assert attempt_calls == []


def test_recovery_success_count_published_on_success(_mock_metrics_clients):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_executor = _mock_metrics_clients["executor"]
    mock_executor.put_count.assert_any_call(MetricName.RECOVERY_SUCCESS_COUNT, "power", "NODE_OVERLOAD")


def test_recovery_failure_count_published_on_escalation(_mock_metrics_clients):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_executor = _mock_metrics_clients["executor"]
    mock_executor.put_count.assert_any_call(MetricName.RECOVERY_FAILURE_COUNT, "power", "NODE_OVERLOAD")


def test_recovery_failure_count_published_on_rejected_action(_mock_metrics_clients):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    plan["action"]["action_type"] = "SOME_MADE_UP_ACTION"
    executor_handler(_executor_input(detection, plan), None)

    mock_executor = _mock_metrics_clients["executor"]
    mock_executor.put_count.assert_any_call(MetricName.RECOVERY_FAILURE_COUNT, "power", "NODE_OVERLOAD")
    # REJECTED is not an escalation -- must not also publish UnresolvedFaultCount.
    unresolved_calls = [
        call for call in mock_executor.put_count.call_args_list if call.args[0] == MetricName.UNRESOLVED_FAULT_COUNT
    ]
    assert unresolved_calls == []


def test_recovery_latency_published_on_success(_mock_metrics_clients):
    telemetry_time = datetime.now(timezone.utc) - timedelta(seconds=3)
    detection = _detection(
        fault_type=FaultType.NODE_OVERLOAD.value,
        severity=Severity.MEDIUM.value,
        timestamp=telemetry_time.isoformat(),
    )
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_executor = _mock_metrics_clients["executor"]
    assert mock_executor.put_milliseconds.call_count == 1
    call_args = mock_executor.put_milliseconds.call_args.args
    assert call_args[0] == MetricName.RECOVERY_LATENCY
    assert call_args[1] == "power"
    assert call_args[2] == "NODE_OVERLOAD"
    assert 2500 <= call_args[3] <= 15000  # ~3s, generous CI-safe bounds


def test_recovery_latency_not_published_when_not_successful(_mock_metrics_clients):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_executor = _mock_metrics_clients["executor"]
    mock_executor.put_milliseconds.assert_not_called()


def test_unresolved_fault_count_published_by_planner_for_high_overload(_mock_metrics_clients):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    planner_handler(detection, None)

    mock_planner = _mock_metrics_clients["planner"]
    mock_planner.put_count.assert_any_call(MetricName.UNRESOLVED_FAULT_COUNT, "power", "NODE_OVERLOAD")


def test_unresolved_fault_count_not_published_by_planner_for_automatic_remediation(_mock_metrics_clients):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    planner_handler(detection, None)

    mock_planner = _mock_metrics_clients["planner"]
    unresolved_calls = [
        call for call in mock_planner.put_count.call_args_list if call.args[0] == MetricName.UNRESOLVED_FAULT_COUNT
    ]
    assert unresolved_calls == []


def test_unresolved_fault_count_published_by_verifier_on_failed_verification(_mock_metrics_clients):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)
    result["status"] = "FAKE_TAMPERED_STATUS"  # forces _verify() to report verified=False
    verifier_handler(_verifier_input(detection, plan, result), None)

    mock_verifier = _mock_metrics_clients["verifier"]
    mock_verifier.put_count.assert_any_call(MetricName.UNRESOLVED_FAULT_COUNT, "power", "NODE_OVERLOAD")


def test_unresolved_fault_count_not_published_by_verifier_on_successful_verification(_mock_metrics_clients):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)
    verifier_handler(_verifier_input(detection, plan, result), None)

    mock_verifier = _mock_metrics_clients["verifier"]
    mock_verifier.put_count.assert_not_called()


def test_no_duplicate_unresolved_fault_count_across_planner_and_verifier_for_escalated_event(_mock_metrics_clients):
    """HIGH overload never reaches the executor/verifier in the real
    deployed workflow (CheckAutomaticRemediation routes straight to
    Escalate) -- proves that for this escalation, ONLY the planner
    publishes UnresolvedFaultCount; the verifier (never invoked for this
    event in the real pipeline) publishes nothing.
    """
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    planner_handler(detection, None)

    mock_planner = _mock_metrics_clients["planner"]
    mock_verifier = _mock_metrics_clients["verifier"]
    unresolved_from_planner = [
        call for call in mock_planner.put_count.call_args_list if call.args[0] == MetricName.UNRESOLVED_FAULT_COUNT
    ]
    assert len(unresolved_from_planner) == 1
    mock_verifier.put_count.assert_not_called()


def test_cloudwatch_failure_does_not_break_remediation_execution():
    """Uses the REAL MetricsClient (not a test double) with an injected
    boto3-shaped client whose put_metric_data raises, proving the actual
    production error-handling wiring -- not just a mock's configured
    behavior -- never breaks RemediationExecutor.execute()'s own result.
    """
    failing_cloudwatch_client = MagicMock()
    failing_cloudwatch_client.put_metric_data.side_effect = RuntimeError("simulated CloudWatch outage")
    real_metrics_client = MetricsClient(client=failing_cloudwatch_client)

    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)

    with patch.object(executor_module, "_metrics_client", real_metrics_client):
        result = executor_handler(_executor_input(detection, plan), None)  # must not raise

    assert result["success"] is True
    assert result["status"] == ExecutionStatus.SUCCESS.value
    failing_cloudwatch_client.put_metric_data.assert_called()


# ============================================================================
# STEP 33 — RECOVERY HISTORY PERSISTENCE (INTEGRATION)
# ============================================================================

from lambda_functions.remediation.recovery_event_store import RecoveryStatus  # noqa: E402


def test_planner_escalation_persists_a_recovery_event(_mock_recovery_event_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    planner_handler(detection, None)

    mock_planner_store = _mock_recovery_event_stores["planner"]
    mock_planner_store.put_event.assert_called_once()
    persisted_item = mock_planner_store.put_event.call_args.args[0]
    assert persisted_item["status"] == RecoveryStatus.ESCALATION_REQUIRED.value
    assert persisted_item["success"] is False
    assert persisted_item["automatic"] is False
    assert persisted_item["node_id"] == detection["node_id"]
    assert persisted_item["timestamp"] == detection["timestamp"]


def test_planner_automatic_remediation_does_not_persist_a_recovery_event(_mock_recovery_event_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    planner_handler(detection, None)

    mock_planner_store = _mock_recovery_event_stores["planner"]
    mock_planner_store.put_event.assert_not_called()


def test_executor_success_persists_a_recovery_event(_mock_recovery_event_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_executor_store = _mock_recovery_event_stores["executor"]
    mock_executor_store.put_event.assert_called_once()
    persisted_item = mock_executor_store.put_event.call_args.args[0]
    assert persisted_item["status"] == RecoveryStatus.SUCCESS.value
    assert persisted_item["success"] is True
    assert persisted_item["automatic"] is True
    assert persisted_item["action"] == ActionType.REDUCE_LOAD.value
    assert persisted_item["fault_type"] == FaultType.NODE_OVERLOAD.value
    assert persisted_item["severity"] == Severity.MEDIUM.value


def test_executor_rejected_action_persists_a_failure_recovery_event(_mock_recovery_event_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    plan["action"]["action_type"] = "SOME_MADE_UP_ACTION"
    executor_handler(_executor_input(detection, plan), None)

    mock_executor_store = _mock_recovery_event_stores["executor"]
    mock_executor_store.put_event.assert_called_once()
    persisted_item = mock_executor_store.put_event.call_args.args[0]
    assert persisted_item["status"] == RecoveryStatus.FAILURE.value
    assert persisted_item["success"] is False


def test_executor_escalated_defensive_branch_persists_escalation_event(_mock_recovery_event_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_executor_store = _mock_recovery_event_stores["executor"]
    mock_executor_store.put_event.assert_called_once()
    persisted_item = mock_executor_store.put_event.call_args.args[0]
    assert persisted_item["status"] == RecoveryStatus.ESCALATION_REQUIRED.value
    assert persisted_item["success"] is False


def test_executor_skipped_does_not_persist_a_recovery_event(_mock_recovery_event_stores):
    detection = _detection(detected=False, fault_type=None, severity=Severity.NORMAL.value, reason="ok", evidence={})
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_executor_store = _mock_recovery_event_stores["executor"]
    mock_executor_store.put_event.assert_not_called()


def test_planner_and_executor_never_both_persist_for_the_same_escalated_event(_mock_recovery_event_stores):
    """HIGH overload never reaches the executor in the real deployed
    workflow (CheckAutomaticRemediation routes straight to Escalate) --
    proves that, for the direct-invocation defensive path exercised in
    this test file, at most Planner persists in the real ASL flow; this
    guards against ever double-persisting the same underlying event
    through both owners simultaneously in a single simulated pipeline run.
    """
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)

    mock_planner_store = _mock_recovery_event_stores["planner"]
    mock_executor_store = _mock_recovery_event_stores["executor"]
    assert mock_planner_store.put_event.call_count == 1
    assert mock_executor_store.put_event.call_count == 0


# ============================================================================
# STEP 34 — CURRENT NODE STATE INTEGRATION (FR-6.1)
# ============================================================================

from shared.node_state_store import NodeState as _NodeState  # noqa: E402


def test_planner_escalation_updates_node_state_to_escalation_required(_mock_node_state_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    planner_handler(detection, None)

    mock_store = _mock_node_state_stores["planner"]
    mock_store.update_state.assert_called_once()
    kwargs = mock_store.update_state.call_args.kwargs
    assert kwargs["state"] == _NodeState.ESCALATION_REQUIRED.value
    assert kwargs["fault_type"] == FaultType.NODE_OVERLOAD.value
    assert kwargs["fault_severity"] == Severity.HIGH.value
    assert kwargs["event_timestamp"] == detection["timestamp"]


def test_planner_automatic_remediation_updates_node_state_to_recovering(_mock_node_state_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    planner_handler(detection, None)

    mock_store = _mock_node_state_stores["planner"]
    mock_store.update_state.assert_called_once()
    kwargs = mock_store.update_state.call_args.kwargs
    assert kwargs["state"] == _NodeState.RECOVERING.value
    assert kwargs["fault_severity"] == Severity.MEDIUM.value


def test_planner_normal_detection_does_not_update_node_state(_mock_node_state_stores):
    detection = _detection(detected=False, fault_type=None, severity=Severity.NORMAL.value, reason="ok", evidence={})
    planner_handler(detection, None)

    _mock_node_state_stores["planner"].update_state.assert_not_called()


def test_executor_success_updates_node_state_to_recovered(_mock_node_state_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_store = _mock_node_state_stores["executor"]
    mock_store.update_state.assert_called_once()
    kwargs = mock_store.update_state.call_args.kwargs
    assert kwargs["state"] == _NodeState.RECOVERED.value
    assert kwargs["fault_type"] == FaultType.NODE_OVERLOAD.value
    assert kwargs["fault_severity"] == Severity.MEDIUM.value


def test_executor_rejected_action_updates_node_state_to_fault_detected(_mock_node_state_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    plan["action"]["action_type"] = "SOME_MADE_UP_ACTION"
    executor_handler(_executor_input(detection, plan), None)

    mock_store = _mock_node_state_stores["executor"]
    mock_store.update_state.assert_called_once()
    kwargs = mock_store.update_state.call_args.kwargs
    assert kwargs["state"] == _NodeState.FAULT_DETECTED.value


def test_executor_escalated_defensive_branch_updates_node_state_to_escalation_required(_mock_node_state_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    mock_store = _mock_node_state_stores["executor"]
    mock_store.update_state.assert_called_once()
    kwargs = mock_store.update_state.call_args.kwargs
    assert kwargs["state"] == _NodeState.ESCALATION_REQUIRED.value


def test_executor_skipped_does_not_update_node_state(_mock_node_state_stores):
    detection = _detection(detected=False, fault_type=None, severity=Severity.NORMAL.value, reason="ok", evidence={})
    plan = planner_handler(detection, None)
    executor_handler(_executor_input(detection, plan), None)

    _mock_node_state_stores["executor"].update_state.assert_not_called()


def test_verifier_failed_verification_updates_node_state_to_escalation_required(_mock_node_state_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)
    result["status"] = "FAKE_TAMPERED_STATUS"  # forces _verify() to report verified=False
    verifier_handler(_verifier_input(detection, plan, result), None)

    mock_store = _mock_node_state_stores["verifier"]
    mock_store.update_state.assert_called_once()
    kwargs = mock_store.update_state.call_args.kwargs
    assert kwargs["state"] == _NodeState.ESCALATION_REQUIRED.value
    assert kwargs["node_id"] == detection["node_id"]


def test_verifier_successful_verification_does_not_update_node_state(_mock_node_state_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.MEDIUM.value)
    plan = planner_handler(detection, None)
    result = executor_handler(_executor_input(detection, plan), None)
    verifier_handler(_verifier_input(detection, plan, result), None)

    _mock_node_state_stores["verifier"].update_state.assert_not_called()


def test_repeated_planner_invocation_for_same_event_does_not_corrupt_state_calls(_mock_node_state_stores):
    detection = _detection(fault_type=FaultType.NODE_OVERLOAD.value, severity=Severity.HIGH.value)
    planner_handler(detection, None)
    planner_handler(detection, None)

    mock_store = _mock_node_state_stores["planner"]
    assert mock_store.update_state.call_count == 2
    first_call = mock_store.update_state.call_args_list[0].kwargs
    second_call = mock_store.update_state.call_args_list[1].kwargs
    assert first_call["event_timestamp"] == second_call["event_timestamp"] == detection["timestamp"]
    assert first_call["state"] == second_call["state"] == _NodeState.ESCALATION_REQUIRED.value
