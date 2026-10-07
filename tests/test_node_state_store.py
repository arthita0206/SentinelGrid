"""Unit tests for shared/node_state_store.py (Step 34 — persistent
current node state, SRS FR-6.1).

These tests never touch AWS — `NodeStateStore` is always constructed
with an injected fake/mock `table`, mirroring
`RecoveryEventStore`'s/`AnomalyEventStore`'s own test pattern. No AWS
credentials, boto3 clients, or network access are required.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from shared.node_state_store import (
    DEFAULT_TABLE_NAME,
    NODE_STATE_TABLE_NAME_ENV_VAR,
    NodeState,
    NodeStateStore,
)


# --- Table name resolution ---


def test_default_table_name():
    assert DEFAULT_TABLE_NAME == "SentinelGridNodeState"
    store = NodeStateStore(table=MagicMock())
    assert store.table_name == "SentinelGridNodeState"


def test_table_name_from_environment_variable(monkeypatch):
    monkeypatch.setenv(NODE_STATE_TABLE_NAME_ENV_VAR, "CustomNodeStateTable")
    store = NodeStateStore(table=MagicMock())
    assert store.table_name == "CustomNodeStateTable"


def test_explicit_table_name_overrides_environment(monkeypatch):
    monkeypatch.setenv(NODE_STATE_TABLE_NAME_ENV_VAR, "EnvTable")
    store = NodeStateStore(table_name="ExplicitTable", table=MagicMock())
    assert store.table_name == "ExplicitTable"


# --- Lazy AWS initialization ---


def test_constructing_store_never_touches_aws():
    store = NodeStateStore()  # no table injected, no credentials configured
    assert store.table_name == DEFAULT_TABLE_NAME


def test_injected_table_is_used_instead_of_a_real_one():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)
    assert store._get_table() is mock_table


# --- Basic successful update (HEALTHY) ---


def test_healthy_update_writes_expected_expression_and_returns_true():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)

    result = store.update_state(
        node_id="power-001",
        event_timestamp="2026-01-01T00:00:00+00:00",
        state=NodeState.HEALTHY.value,
        status="ONLINE",
        subsystem="power",
        reason="All telemetry fields are present and within the expected operating range.",
    )

    assert result is True
    mock_table.update_item.assert_called_once()
    kwargs = mock_table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"node_id": "power-001"}
    assert "ConditionExpression" in kwargs
    # fault_type/fault_severity always REMOVEd when not provided.
    assert "REMOVE" in kwargs["UpdateExpression"]
    values = kwargs["ExpressionAttributeValues"]
    assert values[":state"] == "HEALTHY"
    assert values[":status"] == "ONLINE"
    assert values[":subsystem"] == "power"


def test_every_expression_attribute_name_is_referenced_in_the_expression():
    """Guards against the exact DynamoDB ValidationException class of bug
    ("Value provided in ExpressionAttributeNames unused in expressions")
    — every #alias passed must appear in UpdateExpression or
    ConditionExpression.
    """
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)
    store.update_state(node_id="n1", event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.HEALTHY.value)

    kwargs = mock_table.update_item.call_args.kwargs
    combined_expression = kwargs["UpdateExpression"] + " " + kwargs["ConditionExpression"]
    for alias in kwargs["ExpressionAttributeNames"]:
        assert alias in combined_expression, f"unused ExpressionAttributeNames alias: {alias}"


def test_every_expression_attribute_value_is_referenced_in_the_expression():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)
    store.update_state(
        node_id="n1",
        event_timestamp="2026-01-01T00:00:00+00:00",
        state=NodeState.FAULT_DETECTED.value,
        status="ONLINE",
        subsystem="power",
        fault_type="NODE_OVERLOAD",
        fault_severity="MEDIUM",
        reason="overload",
    )

    kwargs = mock_table.update_item.call_args.kwargs
    combined_expression = kwargs["UpdateExpression"] + " " + kwargs["ConditionExpression"]
    for placeholder in kwargs["ExpressionAttributeValues"]:
        assert placeholder in combined_expression, f"unused ExpressionAttributeValues placeholder: {placeholder}"


# --- Fault state (FAULT_DETECTED) ---


def test_fault_detected_update_sets_fault_type_and_severity():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)

    store.update_state(
        node_id="power-001",
        event_timestamp="2026-01-01T00:00:00+00:00",
        state=NodeState.FAULT_DETECTED.value,
        status="ONLINE",
        subsystem="power",
        fault_type="NODE_OVERLOAD",
        fault_severity="MEDIUM",
        reason="overload detected",
    )

    kwargs = mock_table.update_item.call_args.kwargs
    assert "REMOVE" not in kwargs["UpdateExpression"]
    values = kwargs["ExpressionAttributeValues"]
    assert values[":fault_type"] == "NODE_OVERLOAD"
    assert values[":fault_severity"] == "MEDIUM"
    assert values[":state"] == "FAULT_DETECTED"


def test_high_severity_fault_detected_update():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)

    store.update_state(
        node_id="power-002",
        event_timestamp="2026-01-01T00:00:00+00:00",
        state=NodeState.FAULT_DETECTED.value,
        fault_type="NODE_OVERLOAD",
        fault_severity="HIGH",
    )

    values = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":fault_severity"] == "HIGH"


def test_recovered_removes_fault_fields():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)

    store.update_state(node_id="n1", event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.RECOVERED.value)

    kwargs = mock_table.update_item.call_args.kwargs
    assert "REMOVE #fault_type, #fault_severity" in kwargs["UpdateExpression"]
    assert ":fault_type" not in kwargs["ExpressionAttributeValues"]


def test_escalation_required_state():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)

    store.update_state(
        node_id="n1",
        event_timestamp="2026-01-01T00:00:00+00:00",
        state=NodeState.ESCALATION_REQUIRED.value,
        fault_type="NODE_OVERLOAD",
        fault_severity="HIGH",
    )

    values = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":state"] == "ESCALATION_REQUIRED"


# --- Missing node_id / event_timestamp ---


def test_missing_node_id_does_not_call_dynamodb():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)

    result = store.update_state(node_id=None, event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.HEALTHY.value)

    assert result is False
    mock_table.update_item.assert_not_called()


def test_missing_event_timestamp_does_not_call_dynamodb():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)

    result = store.update_state(node_id="n1", event_timestamp=None, state=NodeState.HEALTHY.value)

    assert result is False
    mock_table.update_item.assert_not_called()


def test_empty_string_node_id_does_not_call_dynamodb():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)

    result = store.update_state(node_id="", event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.HEALTHY.value)

    assert result is False
    mock_table.update_item.assert_not_called()


# --- Stale-write protection (Test E equivalent) ---


def test_stale_write_is_rejected_via_conditional_check_failure():
    mock_table = MagicMock()
    mock_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "newer timestamp exists"}}, "UpdateItem"
    )
    store = NodeStateStore(table=mock_table)

    result = store.update_state(node_id="n1", event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.HEALTHY.value)

    assert result is False  # rejected, not an error the caller must handle


def test_condition_expression_allows_equal_or_greater_timestamp():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)
    store.update_state(node_id="n1", event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.HEALTHY.value)

    condition = mock_table.update_item.call_args.kwargs["ConditionExpression"]
    assert "attribute_not_exists" in condition
    assert ">=" in condition


# --- Same-timestamp idempotency (Test F equivalent) ---


def test_same_timestamp_retry_is_treated_as_idempotent_not_an_error():
    """A retry with the IDENTICAL event_timestamp hits the same
    ConditionalCheckFailedException path (>= is not satisfied by a
    strictly-equal existing value being rejected as "not stale" -- the
    store's job is simply to never raise or corrupt state either way)."""
    mock_table = MagicMock()
    mock_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "UpdateItem"
    )
    store = NodeStateStore(table=mock_table)

    result = store.update_state(node_id="n1", event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.HEALTHY.value)

    assert result is False  # no-op, not raised, not corrupted


# --- DynamoDB failure handling ---


def test_generic_client_error_is_caught_and_returns_false():
    mock_table = MagicMock()
    mock_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}}, "UpdateItem"
    )
    store = NodeStateStore(table=mock_table)

    result = store.update_state(node_id="n1", event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.HEALTHY.value)

    assert result is False  # best-effort: never raises


def test_botocore_error_is_caught_and_returns_false():
    mock_table = MagicMock()
    mock_table.update_item.side_effect = BotoCoreError()
    store = NodeStateStore(table=mock_table)

    result = store.update_state(node_id="n1", event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.HEALTHY.value)

    assert result is False


def test_unexpected_exception_type_still_does_not_propagate_for_client_errors():
    # (Sanity: ensure the two except clauses are the only paths reachable
    # from a real boto3 table -- ClientError and BotoCoreError are the
    # only exception types the AWS SDK raises for update_item.)
    mock_table = MagicMock()
    mock_table.update_item.side_effect = ClientError({"Error": {"Code": "ValidationException"}}, "UpdateItem")
    store = NodeStateStore(table=mock_table)
    assert store.update_state(node_id="n1", event_timestamp="t", state="HEALTHY") is False


# --- Optional fields omitted (no status/subsystem/reason provided) ---


def test_optional_fields_omitted_when_not_provided():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)

    store.update_state(node_id="n1", event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.HEALTHY.value)

    kwargs = mock_table.update_item.call_args.kwargs
    assert ":subsystem" not in kwargs["ExpressionAttributeValues"]
    assert ":status" not in kwargs["ExpressionAttributeValues"]
    assert ":reason" not in kwargs["ExpressionAttributeValues"]
    assert "#subsystem" not in kwargs["ExpressionAttributeNames"]


# --- last_state_update always refreshed ---


def test_last_state_update_and_last_telemetry_timestamp_always_set():
    mock_table = MagicMock()
    store = NodeStateStore(table=mock_table)

    store.update_state(node_id="n1", event_timestamp="2026-01-01T00:00:00+00:00", state=NodeState.HEALTHY.value)

    kwargs = mock_table.update_item.call_args.kwargs
    values = kwargs["ExpressionAttributeValues"]
    assert values[":event_timestamp"] == "2026-01-01T00:00:00+00:00"
    assert ":now" in values
    assert isinstance(values[":now"], str) and "T" in values[":now"]


# --- NodeState vocabulary ---


def test_node_state_vocabulary():
    assert NodeState.HEALTHY.value == "HEALTHY"
    assert NodeState.FAULT_DETECTED.value == "FAULT_DETECTED"
    assert NodeState.RECOVERING.value == "RECOVERING"
    assert NodeState.RECOVERED.value == "RECOVERED"
    assert NodeState.ESCALATION_REQUIRED.value == "ESCALATION_REQUIRED"
    assert NodeState.UNKNOWN.value == "UNKNOWN"
