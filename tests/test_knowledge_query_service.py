"""Unit tests for lambda_functions/knowledge_query/query_service.py
(Step 35 — SRS FR-6.3 read-only Knowledge Base query interface).

These tests never touch AWS — `KnowledgeQueryService` is always
constructed with injected fake/mock DynamoDB Table resources, mirroring
`NodeStateStore`'s/`RecoveryEventStore`'s own test pattern. No AWS
credentials, boto3 clients, or network access are required.
"""

import decimal
import inspect
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from lambda_functions.knowledge_query import query_service as qs_module
from lambda_functions.knowledge_query.query_service import (
    ANOMALY_EVENTS_TABLE_NAME_ENV_VAR,
    DEFAULT_ANOMALY_EVENTS_TABLE_NAME,
    DEFAULT_EVENT_LIMIT,
    DEFAULT_LIMIT,
    DEFAULT_LIST_LIMIT,
    DEFAULT_NODE_STATE_TABLE_NAME,
    DEFAULT_RECOVERY_EVENTS_TABLE_NAME,
    MAX_EVENT_LIMIT,
    MAX_LIMIT,
    MAX_LIST_LIMIT,
    NODE_STATE_TABLE_NAME_ENV_VAR,
    RECOVERY_EVENTS_TABLE_NAME_ENV_VAR,
    KnowledgeQueryError,
    KnowledgeQueryService,
    _normalize_anomaly_event,
    _normalize_recovery_event,
    _to_native_json,
)


def _service(**tables) -> KnowledgeQueryService:
    return KnowledgeQueryService(
        node_state_table=tables.get("node_state_table", MagicMock()),
        anomaly_events_table=tables.get("anomaly_events_table", MagicMock()),
        recovery_events_table=tables.get("recovery_events_table", MagicMock()),
    )


# --- Table name resolution ---


def test_default_table_names():
    assert DEFAULT_NODE_STATE_TABLE_NAME == "SentinelGridNodeState"
    assert DEFAULT_ANOMALY_EVENTS_TABLE_NAME == "SentinelGridAnomalyEvents"
    assert DEFAULT_RECOVERY_EVENTS_TABLE_NAME == "SentinelGridRecoveryEvents"
    service = _service()
    assert service.node_state_table_name == DEFAULT_NODE_STATE_TABLE_NAME
    assert service.anomaly_events_table_name == DEFAULT_ANOMALY_EVENTS_TABLE_NAME
    assert service.recovery_events_table_name == DEFAULT_RECOVERY_EVENTS_TABLE_NAME


def test_table_names_from_environment_variables(monkeypatch):
    monkeypatch.setenv(NODE_STATE_TABLE_NAME_ENV_VAR, "CustomNodeState")
    monkeypatch.setenv(ANOMALY_EVENTS_TABLE_NAME_ENV_VAR, "CustomAnomalyEvents")
    monkeypatch.setenv(RECOVERY_EVENTS_TABLE_NAME_ENV_VAR, "CustomRecoveryEvents")
    service = _service()
    assert service.node_state_table_name == "CustomNodeState"
    assert service.anomaly_events_table_name == "CustomAnomalyEvents"
    assert service.recovery_events_table_name == "CustomRecoveryEvents"


def test_explicit_table_names_override_environment(monkeypatch):
    monkeypatch.setenv(NODE_STATE_TABLE_NAME_ENV_VAR, "EnvTable")
    service = KnowledgeQueryService(node_state_table_name="ExplicitTable", node_state_table=MagicMock())
    assert service.node_state_table_name == "ExplicitTable"


# --- Lazy AWS initialization / dependency injection ---


def test_constructing_service_never_touches_aws():
    service = KnowledgeQueryService()  # no tables injected, no credentials configured
    assert service.node_state_table_name == DEFAULT_NODE_STATE_TABLE_NAME
    assert service._resource is None


def test_injected_tables_are_used_instead_of_real_ones():
    node_state_table = MagicMock()
    anomaly_table = MagicMock()
    recovery_table = MagicMock()
    service = KnowledgeQueryService(
        node_state_table=node_state_table,
        anomaly_events_table=anomaly_table,
        recovery_events_table=recovery_table,
    )
    assert service._get_node_state_table() is node_state_table
    assert service._get_anomaly_events_table() is anomaly_table
    assert service._get_recovery_events_table() is recovery_table


# --- get_current_state ---


def test_get_current_state_returns_item():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"node_id": "power-001", "state": "HEALTHY", "status": "ONLINE"}}
    service = _service(node_state_table=table)

    result = service.get_current_state("power-001")

    assert result == {"node_id": "power-001", "state": "HEALTHY", "status": "ONLINE"}
    table.get_item.assert_called_once_with(Key={"node_id": "power-001"})


def test_get_current_state_uses_get_item_not_query_or_scan():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"node_id": "power-001", "state": "HEALTHY"}}
    service = _service(node_state_table=table)

    service.get_current_state("power-001")

    table.get_item.assert_called_once()
    table.query.assert_not_called()
    table.scan.assert_not_called()


def test_get_current_state_missing_item_returns_none():
    table = MagicMock()
    table.get_item.return_value = {}  # no "Item" key -- DynamoDB's own shape for a miss
    service = _service(node_state_table=table)

    assert service.get_current_state("does-not-exist") is None


def test_get_current_state_dynamodb_client_error_raises_knowledge_query_error():
    table = MagicMock()
    table.get_item.side_effect = ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem")
    service = _service(node_state_table=table)

    with pytest.raises(KnowledgeQueryError):
        service.get_current_state("power-001")


def test_get_current_state_dynamodb_botocore_error_raises_knowledge_query_error():
    table = MagicMock()
    table.get_item.side_effect = BotoCoreError()
    service = _service(node_state_table=table)

    with pytest.raises(KnowledgeQueryError):
        service.get_current_state("power-001")


def test_get_current_state_decimal_values_convert_to_native_json():
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {"node_id": "power-001", "fault_severity_score": decimal.Decimal("3"), "load": decimal.Decimal("97.8")}
    }
    service = _service(node_state_table=table)

    result = service.get_current_state("power-001")

    assert result["fault_severity_score"] == 3
    assert isinstance(result["fault_severity_score"], int)
    assert result["load"] == 97.8
    assert isinstance(result["load"], float)


# --- get_anomaly_history ---


def test_get_anomaly_history_queries_by_node_id():
    table = MagicMock()
    table.query.return_value = {"Items": [{"node_id": "power-001", "timestamp": "t1"}]}
    service = _service(anomaly_events_table=table)

    result = service.get_anomaly_history("power-001")

    assert result == [{"node_id": "power-001", "timestamp": "t1"}]
    _, kwargs = table.query.call_args
    assert kwargs["ExpressionAttributeValues"] == {":node_id": "power-001"}
    assert kwargs["KeyConditionExpression"] == "#pk = :node_id"
    assert kwargs["ExpressionAttributeNames"] == {"#pk": "node_id"}


def test_get_anomaly_history_never_uses_scan():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    service = _service(anomaly_events_table=table)

    service.get_anomaly_history("power-001")

    table.scan.assert_not_called()
    table.get_item.assert_not_called()


def test_get_anomaly_history_default_limit_and_descending_order():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    service = _service(anomaly_events_table=table)

    service.get_anomaly_history("power-001")

    _, kwargs = table.query.call_args
    assert kwargs["Limit"] == DEFAULT_LIMIT
    assert kwargs["ScanIndexForward"] is False


def test_get_anomaly_history_ascending_order_sets_scan_index_forward_true():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    service = _service(anomaly_events_table=table)

    service.get_anomaly_history("power-001", order="asc")

    _, kwargs = table.query.call_args
    assert kwargs["ScanIndexForward"] is True


def test_get_anomaly_history_respects_custom_limit():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    service = _service(anomaly_events_table=table)

    service.get_anomaly_history("power-001", limit=5)

    _, kwargs = table.query.call_args
    assert kwargs["Limit"] == 5


def test_get_anomaly_history_empty_result_returns_empty_list_not_error():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    service = _service(anomaly_events_table=table)

    assert service.get_anomaly_history("power-001") == []


def test_get_anomaly_history_dynamodb_error_raises_knowledge_query_error():
    table = MagicMock()
    table.query.side_effect = ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "Query")
    service = _service(anomaly_events_table=table)

    with pytest.raises(KnowledgeQueryError):
        service.get_anomaly_history("power-001")


def test_get_anomaly_history_decimal_conversion():
    table = MagicMock()
    table.query.return_value = {"Items": [{"node_id": "power-001", "evidence": {"load": decimal.Decimal("97.8")}}]}
    service = _service(anomaly_events_table=table)

    result = service.get_anomaly_history("power-001")

    assert result[0]["evidence"]["load"] == 97.8
    assert isinstance(result[0]["evidence"]["load"], float)


# --- get_recovery_history ---


def test_get_recovery_history_queries_by_node_id():
    table = MagicMock()
    table.query.return_value = {"Items": [{"node_id": "power-001", "timestamp": "t1", "status": "SUCCESS"}]}
    service = _service(recovery_events_table=table)

    result = service.get_recovery_history("power-001")

    assert result == [{"node_id": "power-001", "timestamp": "t1", "status": "SUCCESS"}]
    table.scan.assert_not_called()


def test_get_recovery_history_default_and_custom_ordering():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    service = _service(recovery_events_table=table)

    service.get_recovery_history("power-001")
    assert table.query.call_args.kwargs["ScanIndexForward"] is False

    service.get_recovery_history("power-001", order="asc")
    assert table.query.call_args.kwargs["ScanIndexForward"] is True


def test_get_recovery_history_dynamodb_error_raises_knowledge_query_error():
    table = MagicMock()
    table.query.side_effect = BotoCoreError()
    service = _service(recovery_events_table=table)

    with pytest.raises(KnowledgeQueryError):
        service.get_recovery_history("power-001")


def test_get_recovery_history_empty_returns_empty_list():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    service = _service(recovery_events_table=table)

    assert service.get_recovery_history("power-001") == []


# --- get_summary ---


def test_get_summary_combines_all_three_queries():
    node_state_table = MagicMock()
    node_state_table.get_item.return_value = {"Item": {"node_id": "power-001", "state": "HEALTHY"}}
    anomaly_table = MagicMock()
    anomaly_table.query.return_value = {"Items": [{"node_id": "power-001", "timestamp": "t1"}]}
    recovery_table = MagicMock()
    recovery_table.query.return_value = {"Items": [{"node_id": "power-001", "timestamp": "t1", "status": "SUCCESS"}]}

    service = KnowledgeQueryService(
        node_state_table=node_state_table,
        anomaly_events_table=anomaly_table,
        recovery_events_table=recovery_table,
    )

    summary = service.get_summary("power-001")

    assert summary["current_state"] == {"node_id": "power-001", "state": "HEALTHY"}
    assert summary["anomalies"] == [{"node_id": "power-001", "timestamp": "t1"}]
    assert summary["recoveries"] == [{"node_id": "power-001", "timestamp": "t1", "status": "SUCCESS"}]


def test_get_summary_returns_none_when_node_does_not_exist_and_skips_history_queries():
    node_state_table = MagicMock()
    node_state_table.get_item.return_value = {}
    anomaly_table = MagicMock()
    recovery_table = MagicMock()

    service = KnowledgeQueryService(
        node_state_table=node_state_table,
        anomaly_events_table=anomaly_table,
        recovery_events_table=recovery_table,
    )

    summary = service.get_summary("does-not-exist")

    assert summary is None
    anomaly_table.query.assert_not_called()
    recovery_table.query.assert_not_called()


def test_get_summary_propagates_history_query_failure():
    node_state_table = MagicMock()
    node_state_table.get_item.return_value = {"Item": {"node_id": "power-001", "state": "HEALTHY"}}
    anomaly_table = MagicMock()
    anomaly_table.query.side_effect = ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "Query")
    recovery_table = MagicMock()

    service = KnowledgeQueryService(
        node_state_table=node_state_table,
        anomaly_events_table=anomaly_table,
        recovery_events_table=recovery_table,
    )

    with pytest.raises(KnowledgeQueryError):
        service.get_summary("power-001")


# --- _to_native_json ---


def test_to_native_json_whole_decimal_becomes_int():
    assert _to_native_json(decimal.Decimal("5")) == 5
    assert isinstance(_to_native_json(decimal.Decimal("5")), int)


def test_to_native_json_fractional_decimal_becomes_float():
    assert _to_native_json(decimal.Decimal("5.5")) == 5.5
    assert isinstance(_to_native_json(decimal.Decimal("5.5")), float)


def test_to_native_json_recurses_into_nested_dicts_and_lists():
    value = {"a": [decimal.Decimal("1"), {"b": decimal.Decimal("2.5")}]}
    result = _to_native_json(value)
    assert result == {"a": [1, {"b": 2.5}]}


def test_to_native_json_passes_through_plain_types_unchanged():
    assert _to_native_json("hello") == "hello"
    assert _to_native_json(True) is True
    assert _to_native_json(None) is None


# --- Read-only guarantee ---


def test_query_service_module_never_calls_any_write_operation():
    """Static source-scan guarantee (Step 35's explicit requirement): this
    module must never call put_item/update_item/delete_item/batch_write_item
    anywhere, on any table. Fails the build the moment such a call is
    ever added, rather than relying solely on IAM to catch it at runtime.
    """
    source = inspect.getsource(qs_module)
    for forbidden in ("put_item(", "update_item(", "delete_item(", "batch_write_item("):
        assert forbidden not in source, f"query_service.py must never call {forbidden!r} -- read-only violation"


def test_query_service_only_calls_get_item_and_query_on_mock_tables():
    node_state_table = MagicMock()
    node_state_table.get_item.return_value = {"Item": {"node_id": "power-001", "state": "HEALTHY"}}
    anomaly_table = MagicMock()
    anomaly_table.query.return_value = {"Items": []}
    recovery_table = MagicMock()
    recovery_table.query.return_value = {"Items": []}

    service = KnowledgeQueryService(
        node_state_table=node_state_table,
        anomaly_events_table=anomaly_table,
        recovery_events_table=recovery_table,
    )

    service.get_summary("power-001")

    for table in (node_state_table, anomaly_table, recovery_table):
        table.put_item.assert_not_called()
        table.update_item.assert_not_called()
        table.delete_item.assert_not_called()
        table.batch_write_item.assert_not_called()
        table.scan.assert_not_called()


# --- No user-controlled DynamoDB expressions ---


def test_node_id_is_never_interpolated_into_the_expression_string():
    table = MagicMock()
    table.query.return_value = {"Items": []}
    service = _service(anomaly_events_table=table)

    malicious_node_id = "power-001' OR '1'='1"
    service.get_anomaly_history(malicious_node_id)

    _, kwargs = table.query.call_args
    assert malicious_node_id not in kwargs["KeyConditionExpression"]
    assert kwargs["ExpressionAttributeValues"][":node_id"] == malicious_node_id


# --- MAX_LIMIT / DEFAULT_LIMIT constants ---


def test_limit_constants():
    assert DEFAULT_LIMIT == 20
    assert MAX_LIMIT == 100


def test_list_limit_constants():
    assert DEFAULT_LIST_LIMIT == 50
    assert MAX_LIST_LIMIT == 500


# --- list_nodes() (Step 40) ---


def test_list_nodes_uses_scan_not_query_or_get_item():
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    service = _service(node_state_table=table)

    service.list_nodes()

    table.scan.assert_called_once()
    table.query.assert_not_called()
    table.get_item.assert_not_called()


def test_list_nodes_always_passes_a_bounded_limit_to_scan():
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    service = _service(node_state_table=table)

    service.list_nodes(limit=37)

    _, kwargs = table.scan.call_args
    assert kwargs["Limit"] == 37


def test_list_nodes_default_limit_is_used_when_not_specified():
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    service = _service(node_state_table=table)

    service.list_nodes()

    _, kwargs = table.scan.call_args
    assert kwargs["Limit"] == DEFAULT_LIST_LIMIT


def test_list_nodes_sorts_the_returned_page_by_node_id_descending_by_default():
    table = MagicMock()
    table.scan.return_value = {
        "Items": [
            {"node_id": "power-005", "state": "HEALTHY"},
            {"node_id": "power-001", "state": "HEALTHY"},
            {"node_id": "power-010", "state": "HEALTHY"},
        ]
    }
    service = _service(node_state_table=table)

    result = service.list_nodes()

    assert [item["node_id"] for item in result] == ["power-010", "power-005", "power-001"]


def test_list_nodes_sorts_ascending_when_order_is_asc():
    table = MagicMock()
    table.scan.return_value = {
        "Items": [
            {"node_id": "power-005", "state": "HEALTHY"},
            {"node_id": "power-001", "state": "HEALTHY"},
            {"node_id": "power-010", "state": "HEALTHY"},
        ]
    }
    service = _service(node_state_table=table)

    result = service.list_nodes(order="asc")

    assert [item["node_id"] for item in result] == ["power-001", "power-005", "power-010"]


def test_list_nodes_never_fetches_more_than_the_bounded_page_to_sort():
    # The sort happens purely in Python over whatever Scan already
    # returned -- no second Scan call, no pagination follow-up.
    table = MagicMock()
    table.scan.return_value = {"Items": [{"node_id": "power-001", "state": "HEALTHY"}]}
    service = _service(node_state_table=table)

    service.list_nodes(order="asc")

    assert table.scan.call_count == 1


def test_list_nodes_empty_table_returns_empty_list_not_error():
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    service = _service(node_state_table=table)

    assert service.list_nodes() == []


def test_list_nodes_subsystem_filter_builds_filter_expression():
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    service = _service(node_state_table=table)

    service.list_nodes(subsystem="power")

    _, kwargs = table.scan.call_args
    assert kwargs["FilterExpression"] == "#subsystem = :subsystem"
    assert kwargs["ExpressionAttributeNames"]["#subsystem"] == "subsystem"
    assert kwargs["ExpressionAttributeValues"][":subsystem"] == "power"


def test_list_nodes_state_filter_builds_filter_expression():
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    service = _service(node_state_table=table)

    service.list_nodes(state="FAULT_DETECTED")

    _, kwargs = table.scan.call_args
    assert kwargs["FilterExpression"] == "#state = :state"
    assert kwargs["ExpressionAttributeNames"]["#state"] == "state"
    assert kwargs["ExpressionAttributeValues"][":state"] == "FAULT_DETECTED"


def test_list_nodes_combined_subsystem_and_state_filter():
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    service = _service(node_state_table=table)

    service.list_nodes(subsystem="water", state="HEALTHY")

    _, kwargs = table.scan.call_args
    assert kwargs["FilterExpression"] == "#subsystem = :subsystem AND #state = :state"
    assert kwargs["ExpressionAttributeValues"] == {":subsystem": "water", ":state": "HEALTHY"}


def test_list_nodes_no_filter_expression_when_no_filters_given():
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    service = _service(node_state_table=table)

    service.list_nodes()

    _, kwargs = table.scan.call_args
    assert "FilterExpression" not in kwargs
    assert "ExpressionAttributeNames" not in kwargs
    assert "ExpressionAttributeValues" not in kwargs


def test_list_nodes_filter_values_never_interpolated_into_expression_text():
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    service = _service(node_state_table=table)

    malicious = "power' OR '1'='1"
    service.list_nodes(subsystem=malicious)

    _, kwargs = table.scan.call_args
    assert malicious not in kwargs["FilterExpression"]
    assert kwargs["ExpressionAttributeValues"][":subsystem"] == malicious


def test_list_nodes_decimal_conversion():
    table = MagicMock()
    table.scan.return_value = {"Items": [{"node_id": "power-001", "load_reading": decimal.Decimal("97.8")}]}
    service = _service(node_state_table=table)

    result = service.list_nodes()

    assert result[0]["load_reading"] == 97.8
    assert isinstance(result[0]["load_reading"], float)


def test_list_nodes_dynamodb_client_error_raises_knowledge_query_error():
    table = MagicMock()
    table.scan.side_effect = ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "Scan")
    service = _service(node_state_table=table)

    with pytest.raises(KnowledgeQueryError):
        service.list_nodes()


def test_list_nodes_dynamodb_botocore_error_raises_knowledge_query_error():
    table = MagicMock()
    table.scan.side_effect = BotoCoreError()
    service = _service(node_state_table=table)

    with pytest.raises(KnowledgeQueryError):
        service.list_nodes()


def test_list_nodes_items_are_not_mutated_in_place():
    table = MagicMock()
    original_item = {"node_id": "power-001", "state": "HEALTHY"}
    table.scan.return_value = {"Items": [original_item]}
    service = _service(node_state_table=table)

    service.list_nodes()

    assert original_item == {"node_id": "power-001", "state": "HEALTHY"}


def test_list_nodes_is_deterministic_for_the_same_input():
    table = MagicMock()
    table.scan.return_value = {
        "Items": [
            {"node_id": "power-002", "state": "HEALTHY"},
            {"node_id": "power-001", "state": "HEALTHY"},
        ]
    }
    service = _service(node_state_table=table)

    result_a = service.list_nodes(order="asc")
    result_b = service.list_nodes(order="asc")

    assert result_a == result_b


# --- Event limit constants (Step 42) ---


def test_event_limit_constants():
    assert DEFAULT_EVENT_LIMIT == 50
    assert MAX_EVENT_LIMIT == 100


# --- _normalize_anomaly_event / _normalize_recovery_event (Step 42) ---

_RAW_ANOMALY_ITEM = {
    "node_id": "power-001",
    "timestamp": "2026-01-01T00:00:05+00:00",
    "subsystem": "power",
    "fault_type": "NODE_OVERLOAD",
    "severity": "MEDIUM",
    "reason": "Field 'load' value 97.8 is within the overload band.",
    "evidence": {"field": "load", "value": decimal.Decimal("97.8")},
    "detected": True,
}

_RAW_RECOVERY_ITEM = {
    "node_id": "power-001",
    "timestamp": "2026-01-01T00:00:05+00:00",
    "subsystem": "power",
    "fault_type": "NODE_OVERLOAD",
    "severity": "MEDIUM",
    "action": "REDUCE_LOAD",
    "automatic": True,
    "status": "SUCCESS",
    "success": True,
    "reason": "Simulated execution of REDUCE_LOAD succeeded.",
    "recovery_started_at": "2026-01-01T00:00:05+00:00",
    "recorded_at": "2026-01-01T00:00:07+00:00",
}


def test_normalize_anomaly_event_maps_real_fields():
    result = _normalize_anomaly_event(_RAW_ANOMALY_ITEM)
    assert result["event_type"] == "anomaly"
    assert result["node_id"] == "power-001"
    assert result["timestamp"] == "2026-01-01T00:00:05+00:00"
    assert result["subsystem"] == "power"
    assert result["fault_type"] == "NODE_OVERLOAD"
    assert result["severity"] == "MEDIUM"
    assert result["detected"] is True
    assert result["evidence"] == _RAW_ANOMALY_ITEM["evidence"]


def test_normalize_anomaly_event_recovery_only_fields_are_none():
    result = _normalize_anomaly_event(_RAW_ANOMALY_ITEM)
    assert result["action"] is None
    assert result["automatic"] is None
    assert result["status"] is None
    assert result["success"] is None
    assert result["recovery_started_at"] is None
    assert result["recorded_at"] is None


def test_normalize_recovery_event_maps_real_fields():
    result = _normalize_recovery_event(_RAW_RECOVERY_ITEM)
    assert result["event_type"] == "recovery"
    assert result["node_id"] == "power-001"
    assert result["timestamp"] == "2026-01-01T00:00:05+00:00"
    assert result["action"] == "REDUCE_LOAD"
    assert result["automatic"] is True
    assert result["status"] == "SUCCESS"
    assert result["success"] is True
    assert result["recovery_started_at"] == "2026-01-01T00:00:05+00:00"
    assert result["recorded_at"] == "2026-01-01T00:00:07+00:00"


def test_normalize_recovery_event_does_not_relabel_timestamp_as_detection_time():
    # timestamp is carried through EXACTLY as stored -- even though it
    # equals the originating detection's own timestamp by construction
    # (see recovery_event_store.py), this function must not rename or
    # reinterpret it.
    result = _normalize_recovery_event(_RAW_RECOVERY_ITEM)
    assert result["timestamp"] == _RAW_RECOVERY_ITEM["timestamp"]
    assert result["recovery_started_at"] == _RAW_RECOVERY_ITEM["recovery_started_at"]
    assert result["recorded_at"] == _RAW_RECOVERY_ITEM["recorded_at"]


def test_normalize_recovery_event_anomaly_only_fields_are_none():
    result = _normalize_recovery_event(_RAW_RECOVERY_ITEM)
    assert result["detected"] is None
    assert result["evidence"] is None


# --- list_events() (Step 42) ---


def _events_service(anomaly_items=None, recovery_items=None):
    anomaly_table = MagicMock()
    anomaly_table.scan.return_value = {"Items": anomaly_items or []}
    recovery_table = MagicMock()
    recovery_table.scan.return_value = {"Items": recovery_items or []}
    service = KnowledgeQueryService(anomaly_events_table=anomaly_table, recovery_events_table=recovery_table)
    return service, anomaly_table, recovery_table


def test_list_events_uses_scan_not_query_on_both_tables():
    service, anomaly_table, recovery_table = _events_service([_RAW_ANOMALY_ITEM], [_RAW_RECOVERY_ITEM])
    service.list_events()
    anomaly_table.scan.assert_called_once()
    anomaly_table.query.assert_not_called()
    recovery_table.scan.assert_called_once()
    recovery_table.query.assert_not_called()


def test_list_events_merges_anomaly_and_recovery_events():
    service, _, _ = _events_service([_RAW_ANOMALY_ITEM], [_RAW_RECOVERY_ITEM])
    events = service.list_events()
    assert len(events) == 2
    kinds = {e["event_type"] for e in events}
    assert kinds == {"anomaly", "recovery"}


def test_list_events_sorts_by_timestamp_descending_by_default():
    older = {**_RAW_ANOMALY_ITEM, "timestamp": "2026-01-01T00:00:01+00:00", "node_id": "a"}
    newer = {**_RAW_ANOMALY_ITEM, "timestamp": "2026-01-01T00:00:09+00:00", "node_id": "b"}
    service, _, _ = _events_service([older, newer], [])
    events = service.list_events()
    assert [e["node_id"] for e in events] == ["b", "a"]


def test_list_events_sorts_ascending_when_order_is_asc():
    older = {**_RAW_ANOMALY_ITEM, "timestamp": "2026-01-01T00:00:01+00:00", "node_id": "a"}
    newer = {**_RAW_ANOMALY_ITEM, "timestamp": "2026-01-01T00:00:09+00:00", "node_id": "b"}
    service, _, _ = _events_service([older, newer], [])
    events = service.list_events(order="asc")
    assert [e["node_id"] for e in events] == ["a", "b"]


def test_list_events_sorts_by_each_events_own_primary_timestamp_across_types():
    # An anomaly at t=1 and a LATER recovery (different underlying fault)
    # at t=9 -- descending order must put the recovery first, proving the
    # sort uses each event's own timestamp, not e.g. insertion order.
    anomaly_item = {**_RAW_ANOMALY_ITEM, "timestamp": "2026-01-01T00:00:01+00:00"}
    recovery_item = {**_RAW_RECOVERY_ITEM, "timestamp": "2026-01-01T00:00:09+00:00"}
    service, _, _ = _events_service([anomaly_item], [recovery_item])
    events = service.list_events(order="desc")
    assert events[0]["event_type"] == "recovery"
    assert events[1]["event_type"] == "anomaly"


def test_list_events_enforces_the_requested_limit_after_merging():
    items = [{**_RAW_ANOMALY_ITEM, "node_id": f"power-{i:03d}", "timestamp": f"2026-01-01T00:00:{i:02d}+00:00"} for i in range(10)]
    service, _, _ = _events_service(items, [])
    events = service.list_events(limit=3)
    assert len(events) == 3


def test_list_events_event_type_anomaly_skips_recovery_scan_entirely():
    service, anomaly_table, recovery_table = _events_service([_RAW_ANOMALY_ITEM], [_RAW_RECOVERY_ITEM])
    events = service.list_events(event_type="anomaly")
    assert all(e["event_type"] == "anomaly" for e in events)
    recovery_table.scan.assert_not_called()
    anomaly_table.scan.assert_called_once()


def test_list_events_event_type_recovery_skips_anomaly_scan_entirely():
    service, anomaly_table, recovery_table = _events_service([_RAW_ANOMALY_ITEM], [_RAW_RECOVERY_ITEM])
    events = service.list_events(event_type="recovery")
    assert all(e["event_type"] == "recovery" for e in events)
    anomaly_table.scan.assert_not_called()
    recovery_table.scan.assert_called_once()


def test_list_events_subsystem_filter_applied_to_both_table_scans():
    service, anomaly_table, recovery_table = _events_service([], [])
    service.list_events(subsystem="power")
    for table in (anomaly_table, recovery_table):
        _, kwargs = table.scan.call_args
        assert kwargs["FilterExpression"] == "#subsystem = :subsystem"
        assert kwargs["ExpressionAttributeValues"][":subsystem"] == "power"


def test_list_events_severity_filter_applied_to_both_table_scans():
    service, anomaly_table, recovery_table = _events_service([], [])
    service.list_events(severity="HIGH")
    for table in (anomaly_table, recovery_table):
        _, kwargs = table.scan.call_args
        assert kwargs["FilterExpression"] == "#severity = :severity"
        assert kwargs["ExpressionAttributeValues"][":severity"] == "HIGH"


def test_list_events_status_filter_applied_only_to_recovery_scan():
    service, anomaly_table, recovery_table = _events_service([], [])
    service.list_events(status="SUCCESS")

    _, recovery_kwargs = recovery_table.scan.call_args
    assert recovery_kwargs["FilterExpression"] == "#status = :status"
    assert recovery_kwargs["ExpressionAttributeValues"][":status"] == "SUCCESS"

    _, anomaly_kwargs = anomaly_table.scan.call_args
    assert "FilterExpression" not in anomaly_kwargs or "status" not in str(anomaly_kwargs.get("FilterExpression", ""))


def test_list_events_empty_result_when_nothing_matches():
    service, _, _ = _events_service([], [])
    assert service.list_events() == []


def test_list_events_decimal_conversion():
    service, _, _ = _events_service([_RAW_ANOMALY_ITEM], [])
    events = service.list_events()
    assert events[0]["evidence"]["value"] == 97.8
    assert isinstance(events[0]["evidence"]["value"], float)


def test_list_events_dynamodb_client_error_raises_knowledge_query_error():
    anomaly_table = MagicMock()
    anomaly_table.scan.side_effect = ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "Scan")
    service = KnowledgeQueryService(anomaly_events_table=anomaly_table, recovery_events_table=MagicMock())
    with pytest.raises(KnowledgeQueryError):
        service.list_events()


def test_list_events_dynamodb_botocore_error_raises_knowledge_query_error():
    anomaly_table = MagicMock()
    anomaly_table.scan.side_effect = BotoCoreError()
    service = KnowledgeQueryService(anomaly_events_table=anomaly_table, recovery_events_table=MagicMock())
    with pytest.raises(KnowledgeQueryError):
        service.list_events()


def test_list_events_internal_scan_limit_is_fixed_not_the_client_limit():
    # The Scan's own Limit must always be the large, internal, fixed
    # constant (500) -- NEVER the small, client-facing `limit` (e.g. 5).
    service, anomaly_table, recovery_table = _events_service([], [])
    service.list_events(limit=5)
    _, anomaly_kwargs = anomaly_table.scan.call_args
    _, recovery_kwargs = recovery_table.scan.call_args
    assert anomaly_kwargs["Limit"] == 500
    assert recovery_kwargs["Limit"] == 500


def test_list_events_filter_values_never_interpolated_into_expression_text():
    service, anomaly_table, _ = _events_service([], [])
    malicious = "power' OR '1'='1"
    service.list_events(subsystem=malicious)
    _, kwargs = anomaly_table.scan.call_args
    assert malicious not in kwargs["FilterExpression"]
    assert kwargs["ExpressionAttributeValues"][":subsystem"] == malicious
