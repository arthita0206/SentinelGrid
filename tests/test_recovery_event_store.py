"""Unit tests for lambda_functions/remediation/recovery_event_store.py
(Step 33 — persistent recovery history).

These tests never touch AWS — `RecoveryEventStore` is always constructed
with an injected fake/mock `table`, exactly mirroring
`AnomalyEventStore`'s own test pattern (see
tests/test_anomaly_detector.py-adjacent event_store tests). No AWS
credentials, boto3 clients, or network access are required.
"""

import math
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from lambda_functions.remediation.recovery_event_store import (
    DEFAULT_TABLE_NAME,
    RECOVERY_EVENTS_TABLE_NAME_ENV_VAR,
    InvalidRecoveryEventError,
    PersistenceOutcome,
    RecoveryEventConflictError,
    RecoveryEventPersistenceError,
    RecoveryEventStore,
    RecoveryStatus,
    build_escalation_event,
    build_execution_event,
)
from remediation.models import ActionType, ExecutionStatus, RemediationAction, RemediationPlan, RemediationResult


def _plan(**overrides) -> RemediationPlan:
    defaults = dict(
        remediation_required=True,
        node_id="power-007",
        subsystem="power",
        fault_type="NODE_OVERLOAD",
        severity="MEDIUM",
        timestamp="2026-01-01T00:00:00+00:00",
        action=RemediationAction(
            action_type=ActionType.REDUCE_LOAD,
            description="Simulate reducing load on the affected node.",
            parameters={"field": "load", "value": 97.8},
        ),
        reason="NODE_OVERLOAD at severity MEDIUM.",
        automatic=True,
        escalation_required=False,
    )
    defaults.update(overrides)
    return RemediationPlan(**defaults)


def _result(**overrides) -> RemediationResult:
    defaults = dict(
        success=True,
        status=ExecutionStatus.SUCCESS,
        node_id="power-007",
        subsystem="power",
        fault_type="NODE_OVERLOAD",
        action=ActionType.REDUCE_LOAD,
        timestamp="2026-01-01T00:00:00+00:00",
        message="Simulated execution of REDUCE_LOAD succeeded.",
        simulated=True,
        details={"parameters": {"field": "load", "value": 97.8}},
    )
    defaults.update(overrides)
    return RemediationResult(**defaults)


# --- 2, 3, 4. Correct table name / primary key / required fields ---


def test_default_table_name():
    assert DEFAULT_TABLE_NAME == "SentinelGridRecoveryEvents"
    store = RecoveryEventStore(table=MagicMock())
    assert store.table_name == "SentinelGridRecoveryEvents"


def test_table_name_from_environment_variable(monkeypatch):
    monkeypatch.setenv(RECOVERY_EVENTS_TABLE_NAME_ENV_VAR, "CustomRecoveryEventsTable")
    store = RecoveryEventStore(table=MagicMock())
    assert store.table_name == "CustomRecoveryEventsTable"


def test_explicit_table_name_overrides_environment(monkeypatch):
    monkeypatch.setenv(RECOVERY_EVENTS_TABLE_NAME_ENV_VAR, "EnvTable")
    store = RecoveryEventStore(table_name="ExplicitTable", table=MagicMock())
    assert store.table_name == "ExplicitTable"


# --- 5. Successful recovery event (SUCCESS) ---


def test_build_execution_event_success():
    plan = _plan()
    result = _result()
    item = build_execution_event(plan, result)

    assert item["node_id"] == "power-007"
    assert item["timestamp"] == "2026-01-01T00:00:00+00:00"
    assert item["subsystem"] == "power"
    assert item["fault_type"] == "NODE_OVERLOAD"
    assert item["severity"] == "MEDIUM"
    assert item["action"] == "REDUCE_LOAD"
    assert item["automatic"] is True
    assert item["status"] == RecoveryStatus.SUCCESS.value == "SUCCESS"
    assert item["success"] is True
    assert item["reason"] == "Simulated execution of REDUCE_LOAD succeeded."
    assert item["recovery_started_at"] == "2026-01-01T00:00:00+00:00"
    assert "recorded_at" in item and isinstance(item["recorded_at"], str)


# --- 6. Failed recovery event (REJECTED -> FAILURE) ---


def test_build_execution_event_rejected_maps_to_failure():
    plan = _plan()
    result = _result(
        success=False,
        status=ExecutionStatus.REJECTED,
        action=ActionType.NO_ACTION,
        message="Unsupported/unknown action; refusing to execute.",
    )
    item = build_execution_event(plan, result)

    assert item["status"] == RecoveryStatus.FAILURE.value == "FAILURE"
    assert item["success"] is False


def test_build_execution_event_escalated_maps_to_escalation_required():
    plan = _plan(automatic=False, escalation_required=True)
    result = _result(
        success=False,
        status=ExecutionStatus.ESCALATED,
        message="Action requires manual review/escalation.",
    )
    item = build_execution_event(plan, result)

    assert item["status"] == RecoveryStatus.ESCALATION_REQUIRED.value
    assert item["success"] is False


def test_build_execution_event_skipped_returns_none():
    plan = _plan(remediation_required=False, automatic=False, action=RemediationAction(
        action_type=ActionType.NO_ACTION, description="no fault detected", parameters={}
    ))
    result = _result(
        success=False,
        status=ExecutionStatus.SKIPPED,
        action=ActionType.NO_ACTION,
        message="No fault detected; no remediation required.",
    )
    assert build_execution_event(plan, result) is None


# --- 7. Escalation recovery event ---


def test_build_escalation_event():
    plan = _plan(
        severity="HIGH",
        automatic=False,
        escalation_required=True,
        reason="NODE_OVERLOAD at severity HIGH exceeds the automatic-remediation policy.",
    )
    item = build_escalation_event(plan)

    assert item["node_id"] == "power-007"
    assert item["timestamp"] == "2026-01-01T00:00:00+00:00"
    assert item["fault_type"] == "NODE_OVERLOAD"
    assert item["severity"] == "HIGH"
    assert item["action"] == "REDUCE_LOAD"
    assert item["automatic"] is False
    assert item["status"] == RecoveryStatus.ESCALATION_REQUIRED.value
    assert item["success"] is False
    assert "exceeds the automatic-remediation policy" in item["reason"]


# --- 1. Successful PutItem ---


def test_put_event_writes_item_and_returns_newly_persisted():
    mock_table = MagicMock()
    store = RecoveryEventStore(table=mock_table)
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))

    outcome = store.put_event(item)

    assert outcome == PersistenceOutcome.NEWLY_PERSISTED
    mock_table.put_item.assert_called_once()
    call_kwargs = mock_table.put_item.call_args.kwargs
    assert call_kwargs["Item"]["node_id"] == "power-007"
    assert "ConditionExpression" in call_kwargs


# --- 8, 9, 10. Decimal-safe / tuple-list / NaN-Infinity conversion ---


def test_float_evidence_values_are_converted_to_decimal():
    mock_table = MagicMock()
    store = RecoveryEventStore(table=mock_table)
    plan = _plan()
    result = _result()
    item = build_execution_event(plan, result)
    item["details"] = {"load_value": 97.8}  # simulate a float sneaking into an extra field

    store.put_event(item)

    import decimal
    written = mock_table.put_item.call_args.kwargs["Item"]
    assert isinstance(written["details"]["load_value"], decimal.Decimal)


def test_tuple_values_are_converted_to_list():
    mock_table = MagicMock()
    store = RecoveryEventStore(table=mock_table)
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))
    item["overload_range"] = (95.0, 100.0)

    store.put_event(item)

    written = mock_table.put_item.call_args.kwargs["Item"]
    assert written["overload_range"] == [95.0, 100.0] or written["overload_range"] == [
        __import__("decimal").Decimal("95.0"),
        __import__("decimal").Decimal("100.0"),
    ]
    assert isinstance(written["overload_range"], list)


def test_nan_and_infinity_are_converted_to_strings():
    mock_table = MagicMock()
    store = RecoveryEventStore(table=mock_table)
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))
    item["anomalous_value"] = float("nan")
    item["anomalous_inf"] = float("inf")

    store.put_event(item)

    written = mock_table.put_item.call_args.kwargs["Item"]
    assert written["anomalous_value"] == "nan"
    assert written["anomalous_inf"] == "inf"


# --- 11, 12, 13. Malformed event / missing node_id / missing timestamp ---


def test_missing_node_id_raises_before_any_dynamodb_call():
    mock_table = MagicMock()
    store = RecoveryEventStore(table=mock_table)
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))
    del item["node_id"]

    with pytest.raises(InvalidRecoveryEventError):
        store.put_event(item)
    mock_table.put_item.assert_not_called()


def test_missing_timestamp_raises_before_any_dynamodb_call():
    mock_table = MagicMock()
    store = RecoveryEventStore(table=mock_table)
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))
    del item["timestamp"]

    with pytest.raises(InvalidRecoveryEventError):
        store.put_event(item)
    mock_table.put_item.assert_not_called()


def test_empty_string_node_id_is_rejected():
    mock_table = MagicMock()
    store = RecoveryEventStore(table=mock_table)
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))
    item["node_id"] = ""

    with pytest.raises(InvalidRecoveryEventError):
        store.put_event(item)


# --- 14. Duplicate ConditionalCheckFailed handling (true idempotent retry) ---


def test_duplicate_identical_event_is_idempotent_no_op():
    mock_table = MagicMock()
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))

    conditional_check_error = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "PutItem"
    )
    mock_table.put_item.side_effect = conditional_check_error
    mock_table.get_item.return_value = {"Item": dict(item)}  # identical existing item

    store = RecoveryEventStore(table=mock_table)
    outcome = store.put_event(item)

    assert outcome == PersistenceOutcome.DUPLICATE_ALREADY_EXISTS
    mock_table.get_item.assert_called_once()
    call_kwargs = mock_table.get_item.call_args.kwargs
    assert call_kwargs["ConsistentRead"] is True


# --- 15. Conflicting duplicate handling ---


def test_conflicting_duplicate_raises_conflict_error_and_does_not_overwrite():
    mock_table = MagicMock()
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))

    conditional_check_error = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "PutItem"
    )
    mock_table.put_item.side_effect = conditional_check_error
    existing_item = dict(item)
    existing_item["status"] = "SUCCESS"  # materially different outcome
    existing_item["success"] = True
    mock_table.get_item.return_value = {"Item": existing_item}

    store = RecoveryEventStore(table=mock_table)

    with pytest.raises(RecoveryEventConflictError):
        store.put_event(item)

    # Never attempts to overwrite the conflicting item.
    assert mock_table.put_item.call_count == 1


def test_conditional_check_failed_with_no_readback_item_raises():
    mock_table = MagicMock()
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))
    mock_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "PutItem"
    )
    mock_table.get_item.return_value = {}  # no Item key at all

    store = RecoveryEventStore(table=mock_table)
    with pytest.raises(RecoveryEventPersistenceError):
        store.put_event(item)


# --- 16. DynamoDB client failure ---


def test_generic_client_error_raises_persistence_error():
    mock_table = MagicMock()
    mock_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}}, "PutItem"
    )
    store = RecoveryEventStore(table=mock_table)
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))

    with pytest.raises(RecoveryEventPersistenceError):
        store.put_event(item)


def test_botocore_error_raises_persistence_error():
    mock_table = MagicMock()
    mock_table.put_item.side_effect = BotoCoreError()
    store = RecoveryEventStore(table=mock_table)
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))

    with pytest.raises(RecoveryEventPersistenceError):
        store.put_event(item)


def test_get_item_failure_during_duplicate_resolution_raises_persistence_error():
    mock_table = MagicMock()
    mock_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "PutItem"
    )
    mock_table.get_item.side_effect = BotoCoreError()
    store = RecoveryEventStore(table=mock_table)
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))

    with pytest.raises(RecoveryEventPersistenceError):
        store.put_event(item)


# --- 17. Lazy boto3 initialization ---


def test_constructing_store_never_touches_aws():
    store = RecoveryEventStore()  # no table injected, no credentials configured
    assert store.table_name == DEFAULT_TABLE_NAME


# --- 18. Dependency injection / mocking ---


def test_injected_table_is_used_instead_of_a_real_one():
    mock_table = MagicMock()
    store = RecoveryEventStore(table=mock_table)
    assert store._get_table() is mock_table


# --- 19. No AWS dependency in local remediation engine ---


def test_remediation_models_and_engine_have_no_boto3_import():
    import ast
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    for relative_path in ("remediation/models.py", "remediation/engine.py"):
        tree = ast.parse((repo_root / relative_path).read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        assert "boto3" not in names, f"{relative_path} must not import boto3"
        assert "botocore" not in names, f"{relative_path} must not import botocore"


def test_recovery_event_store_module_not_imported_by_pure_engine():
    import ast
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    tree = ast.parse((repo_root / "remediation" / "engine.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "recovery_event_store" not in node.module


# --- Reason field is not empty / basic sanity on built items ---


def test_math_isnan_isinf_used_correctly_for_negative_infinity():
    mock_table = MagicMock()
    store = RecoveryEventStore(table=mock_table)
    item = build_escalation_event(_plan(automatic=False, escalation_required=True))
    item["negative_inf_value"] = float("-inf")

    store.put_event(item)

    written = mock_table.put_item.call_args.kwargs["Item"]
    assert written["negative_inf_value"] == "-inf"
    assert math.isinf(float(written["negative_inf_value"]))
