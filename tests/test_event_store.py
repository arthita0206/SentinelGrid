"""Unit tests for the anomaly-event persistence layer
(lambda_functions/anomaly_detector/event_store.py).

All DynamoDB interactions are mocked via a fake Table object injected at
construction time. No real AWS calls are made anywhere in this file.
"""

import decimal
import math
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from lambda_functions.anomaly_detector.detector import DetectionResult
from lambda_functions.anomaly_detector.event_store import (
    AnomalyEventPersistenceError,
    AnomalyEventStore,
    PersistenceOutcome,
)


def _result(**overrides) -> DetectionResult:
    defaults = dict(
        detected=True,
        fault_type="NODE_OVERLOAD",
        severity="MEDIUM",
        node_id="power-007",
        subsystem="power",
        timestamp="2026-01-01T00:05:00+00:00",
        reason="Field 'load' value 97.8 is within the overload band (95.0, 100.0).",
        evidence={"field": "load", "value": 97.8, "overload_range": (95.0, 100.0)},
    )
    defaults.update(overrides)
    return DetectionResult(**defaults)


def _conditional_check_failed():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "duplicate"}}, "PutItem"
    )


# --- Serialization (evidence preserved, DynamoDB-compatible types) ---


def test_put_event_builds_item_with_all_required_fields():
    fake_table = MagicMock()
    store = AnomalyEventStore(table=fake_table)

    store.put_event(_result())

    fake_table.put_item.assert_called_once()
    item = fake_table.put_item.call_args.kwargs["Item"]

    assert item["node_id"] == "power-007"
    assert item["timestamp"] == "2026-01-01T00:05:00+00:00"
    assert item["subsystem"] == "power"
    assert item["fault_type"] == "NODE_OVERLOAD"
    assert item["severity"] == "MEDIUM"
    assert item["reason"] == _result().reason
    assert item["detected"] is True


def test_put_event_converts_floats_to_decimal():
    fake_table = MagicMock()
    store = AnomalyEventStore(table=fake_table)

    store.put_event(_result())

    item = fake_table.put_item.call_args.kwargs["Item"]
    assert isinstance(item["evidence"]["value"], decimal.Decimal)
    assert item["evidence"]["value"] == decimal.Decimal("97.8")


def test_put_event_converts_tuples_to_lists():
    fake_table = MagicMock()
    store = AnomalyEventStore(table=fake_table)

    store.put_event(_result())

    item = fake_table.put_item.call_args.kwargs["Item"]
    assert item["evidence"]["overload_range"] == [decimal.Decimal("95.0"), decimal.Decimal("100.0")]
    assert isinstance(item["evidence"]["overload_range"], list)


def test_put_event_preserves_nested_evidence_structure():
    result = _result(
        fault_type="SENSOR_DROPOUT",
        severity="HIGH",
        evidence={"missing_fields": ["frequency"], "data": {"load": 50.0, "voltage": 230.0}},
    )
    fake_table = MagicMock()
    store = AnomalyEventStore(table=fake_table)

    store.put_event(result)

    item = fake_table.put_item.call_args.kwargs["Item"]
    assert item["evidence"]["missing_fields"] == ["frequency"]
    assert item["evidence"]["data"]["load"] == decimal.Decimal("50.0")
    assert item["evidence"]["data"]["voltage"] == decimal.Decimal("230.0")


def test_put_event_handles_nan_and_infinite_evidence_values_as_strings():
    result = _result(
        fault_type="DATA_CORRUPTION",
        severity="CRITICAL",
        evidence={"field": "frequency", "value": float("nan")},
    )
    fake_table = MagicMock()
    store = AnomalyEventStore(table=fake_table)

    store.put_event(result)

    item = fake_table.put_item.call_args.kwargs["Item"]
    assert item["evidence"]["value"] == "nan"

    result_inf = _result(evidence={"field": "flow_rate", "value": float("inf")})
    store.put_event(result_inf)
    item_inf = fake_table.put_item.call_args.kwargs["Item"]
    assert item_inf["evidence"]["value"] == "inf"


def test_put_event_preserves_booleans_and_none():
    result = _result(evidence={"flag": True, "missing": None})
    fake_table = MagicMock()
    store = AnomalyEventStore(table=fake_table)

    store.put_event(result)

    item = fake_table.put_item.call_args.kwargs["Item"]
    assert item["evidence"]["flag"] is True
    assert item["evidence"]["missing"] is None


# --- Idempotent conditional write ---


def test_put_event_uses_conditional_expression_on_both_keys():
    fake_table = MagicMock()
    store = AnomalyEventStore(table=fake_table)

    store.put_event(_result())

    kwargs = fake_table.put_item.call_args.kwargs
    assert "attribute_not_exists(#pk)" in kwargs["ConditionExpression"]
    assert "attribute_not_exists(#sk)" in kwargs["ConditionExpression"]
    assert kwargs["ExpressionAttributeNames"] == {"#pk": "node_id", "#sk": "timestamp"}


def test_duplicate_write_is_treated_as_success_not_error():
    fake_table = MagicMock()
    fake_table.put_item.side_effect = _conditional_check_failed()
    store = AnomalyEventStore(table=fake_table)

    store.put_event(_result())  # must not raise


def test_fresh_write_returns_newly_persisted_outcome():
    fake_table = MagicMock()
    store = AnomalyEventStore(table=fake_table)

    outcome = store.put_event(_result())

    assert outcome == PersistenceOutcome.NEWLY_PERSISTED


def test_duplicate_write_returns_duplicate_already_exists_outcome():
    fake_table = MagicMock()
    fake_table.put_item.side_effect = _conditional_check_failed()
    store = AnomalyEventStore(table=fake_table)

    outcome = store.put_event(_result())

    assert outcome == PersistenceOutcome.DUPLICATE_ALREADY_EXISTS


# --- Failure behavior ---


def test_other_client_error_raises_persistence_error():
    fake_table = MagicMock()
    fake_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
        "PutItem",
    )
    store = AnomalyEventStore(table=fake_table)

    with pytest.raises(AnomalyEventPersistenceError):
        store.put_event(_result())


def test_botocore_error_raises_persistence_error():
    fake_table = MagicMock()
    fake_table.put_item.side_effect = BotoCoreError()
    store = AnomalyEventStore(table=fake_table)

    with pytest.raises(AnomalyEventPersistenceError):
        store.put_event(_result())


def test_persistence_error_message_does_not_leak_credentials():
    fake_table = MagicMock()
    fake_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "PutItem"
    )
    store = AnomalyEventStore(table=fake_table)

    with pytest.raises(AnomalyEventPersistenceError) as exc_info:
        store.put_event(_result())

    message = str(exc_info.value).lower()
    assert "secret" not in message
    assert "access_key" not in message


# --- No real AWS calls ---


def test_no_boto3_session_created_when_table_is_injected():
    fake_table = MagicMock()
    store = AnomalyEventStore(table=fake_table)

    with patch("lambda_functions.anomaly_detector.event_store.boto3.Session") as mock_session_cls:
        store.put_event(_result())
        mock_session_cls.assert_not_called()


def test_constructing_store_does_not_touch_aws():
    with patch("lambda_functions.anomaly_detector.event_store.boto3.Session") as mock_session_cls:
        AnomalyEventStore()  # no table injected, no put_event called
        mock_session_cls.assert_not_called()


def test_lazy_client_uses_profile_and_region_when_configured():
    with patch("lambda_functions.anomaly_detector.event_store.boto3.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        store = AnomalyEventStore(region="ap-south-1", profile="sentinelgrid")
        store.put_event(_result())

        mock_session_cls.assert_called_once_with(profile_name="sentinelgrid", region_name="ap-south-1")
        mock_session.resource.assert_called_once_with("dynamodb", region_name="ap-south-1")


def test_lazy_client_omits_profile_when_not_configured():
    with patch("lambda_functions.anomaly_detector.event_store.boto3.Session") as mock_session_cls:
        mock_session_cls.return_value = MagicMock()

        store = AnomalyEventStore(region="ap-south-1")
        store.put_event(_result())

        mock_session_cls.assert_called_once_with(region_name="ap-south-1")
