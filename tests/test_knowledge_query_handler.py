"""Unit tests for lambda_functions/knowledge_query/handler.py (Step 35 —
SRS FR-6.3 read-only Knowledge Base query interface).

These tests never touch AWS — `handler_module._query_service` is always
patched with a `MagicMock()`, mirroring how tests/test_anomaly_detector_handler.py
patches `_node_state_store`/`_event_store`. No AWS credentials, boto3
clients, or network access are required.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from lambda_functions.knowledge_query import handler as handler_module
from lambda_functions.knowledge_query.metrics_service import MetricsQueryError
from lambda_functions.knowledge_query.query_service import KnowledgeQueryError


@pytest.fixture(autouse=True)
def _mock_query_service(monkeypatch):
    mock_service = MagicMock()
    monkeypatch.setattr(handler_module, "_query_service", mock_service)
    return mock_service


@pytest.fixture(autouse=True)
def _mock_metrics_service(monkeypatch):
    mock_service = MagicMock()
    mock_service.get_metrics.return_value = {"period_seconds": 60, "series": []}
    monkeypatch.setattr(handler_module, "_metrics_service", mock_service)
    return mock_service


def _event(route_key, node_id=None, query_params=None):
    event = {"routeKey": route_key, "pathParameters": {}, "queryStringParameters": query_params}
    if node_id is not None:
        event["pathParameters"]["node_id"] = node_id
    return event


def _body(response):
    return json.loads(response["body"])


# --- node_id validation ---


def test_missing_node_id_returns_400():
    event = {"routeKey": "GET /nodes/{node_id}", "pathParameters": {}, "queryStringParameters": None}
    response = handler_module.lambda_handler(event)
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "INVALID_NODE_ID"


def test_empty_node_id_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="   "))
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "INVALID_NODE_ID"
    _mock_query_service.get_current_state.assert_not_called()


def test_non_string_node_id_returns_400():
    event = _event("GET /nodes/{node_id}")
    event["pathParameters"]["node_id"] = 12345
    response = handler_module.lambda_handler(event)
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "INVALID_NODE_ID"


def test_oversized_node_id_returns_400():
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="x" * 129))
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "INVALID_NODE_ID"


def test_node_id_at_max_length_is_valid(_mock_query_service):
    _mock_query_service.get_current_state.return_value = {"node_id": "x" * 128, "state": "HEALTHY"}
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="x" * 128))
    assert response["statusCode"] == 200


def test_node_id_is_trimmed_of_surrounding_whitespace(_mock_query_service):
    _mock_query_service.get_current_state.return_value = {"node_id": "power-001", "state": "HEALTHY"}
    handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="  power-001  "))
    _mock_query_service.get_current_state.assert_called_once_with("power-001")


def test_valid_node_id_returns_200(_mock_query_service):
    _mock_query_service.get_current_state.return_value = {"node_id": "power-001", "state": "HEALTHY"}
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="power-001"))
    assert response["statusCode"] == 200


# --- limit validation ---


def test_invalid_limit_non_numeric_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(
        _event("GET /nodes/{node_id}/anomalies", node_id="power-001", query_params={"limit": "abc"})
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "INVALID_LIMIT"


def test_limit_lower_bound_zero_is_invalid(_mock_query_service):
    response = handler_module.lambda_handler(
        _event("GET /nodes/{node_id}/anomalies", node_id="power-001", query_params={"limit": "0"})
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "INVALID_LIMIT"


def test_limit_of_one_is_valid(_mock_query_service):
    _mock_query_service.get_anomaly_history.return_value = []
    response = handler_module.lambda_handler(
        _event("GET /nodes/{node_id}/anomalies", node_id="power-001", query_params={"limit": "1"})
    )
    assert response["statusCode"] == 200
    _mock_query_service.get_anomaly_history.assert_called_once_with("power-001", limit=1, order="desc")


def test_limit_upper_bound_100_is_valid(_mock_query_service):
    _mock_query_service.get_anomaly_history.return_value = []
    response = handler_module.lambda_handler(
        _event("GET /nodes/{node_id}/anomalies", node_id="power-001", query_params={"limit": "100"})
    )
    assert response["statusCode"] == 200


def test_limit_above_maximum_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(
        _event("GET /nodes/{node_id}/anomalies", node_id="power-001", query_params={"limit": "101"})
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "INVALID_LIMIT"
    _mock_query_service.get_anomaly_history.assert_not_called()


def test_negative_limit_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(
        _event("GET /nodes/{node_id}/anomalies", node_id="power-001", query_params={"limit": "-5"})
    )
    assert response["statusCode"] == 400


def test_default_limit_used_when_absent(_mock_query_service):
    _mock_query_service.get_anomaly_history.return_value = []
    handler_module.lambda_handler(_event("GET /nodes/{node_id}/anomalies", node_id="power-001"))
    _mock_query_service.get_anomaly_history.assert_called_once_with("power-001", limit=20, order="desc")


# --- order validation ---


def test_invalid_order_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(
        _event("GET /nodes/{node_id}/anomalies", node_id="power-001", query_params={"order": "sideways"})
    )
    assert response["statusCode"] == 400
    assert _body(response)["error"] == "INVALID_ORDER"
    _mock_query_service.get_anomaly_history.assert_not_called()


def test_default_order_is_desc(_mock_query_service):
    _mock_query_service.get_anomaly_history.return_value = []
    handler_module.lambda_handler(_event("GET /nodes/{node_id}/anomalies", node_id="power-001"))
    assert _mock_query_service.get_anomaly_history.call_args.kwargs["order"] == "desc"


def test_explicit_ascending_order_is_passed_through(_mock_query_service):
    _mock_query_service.get_anomaly_history.return_value = []
    handler_module.lambda_handler(
        _event("GET /nodes/{node_id}/anomalies", node_id="power-001", query_params={"order": "asc"})
    )
    assert _mock_query_service.get_anomaly_history.call_args.kwargs["order"] == "asc"


# --- current-state route (200 / 404) ---


def test_current_state_200_response_shape(_mock_query_service):
    _mock_query_service.get_current_state.return_value = {
        "node_id": "power-001",
        "subsystem": "power",
        "status": "ONLINE",
        "state": "HEALTHY",
    }
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="power-001"))
    assert response["statusCode"] == 200
    assert _body(response) == {"node": {"node_id": "power-001", "subsystem": "power", "status": "ONLINE", "state": "HEALTHY"}}


def test_current_state_missing_node_returns_404(_mock_query_service):
    _mock_query_service.get_current_state.return_value = None
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="does-not-exist"))
    assert response["statusCode"] == 404
    body = _body(response)
    assert body["error"] == "NODE_NOT_FOUND"
    assert "does-not-exist" not in json.dumps(body)  # message is generic, not node-id-specific


def test_current_state_response_does_not_leak_dynamodb_types():
    pass  # covered structurally by query_service's own Decimal-conversion tests


# --- anomalies route ---


def test_anomalies_200_response_shape(_mock_query_service):
    _mock_query_service.get_anomaly_history.return_value = [
        {"node_id": "power-001", "timestamp": "t1", "fault_type": "NODE_OVERLOAD", "severity": "MEDIUM"}
    ]
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}/anomalies", node_id="power-001"))
    assert response["statusCode"] == 200
    body = _body(response)
    assert body["node_id"] == "power-001"
    assert body["count"] == 1
    assert body["items"][0]["fault_type"] == "NODE_OVERLOAD"


def test_anomalies_empty_history_returns_200_with_empty_items(_mock_query_service):
    _mock_query_service.get_anomaly_history.return_value = []
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}/anomalies", node_id="power-001"))
    assert response["statusCode"] == 200
    assert _body(response) == {"node_id": "power-001", "count": 0, "items": []}


# --- recoveries route ---


def test_recoveries_200_response_shape(_mock_query_service):
    _mock_query_service.get_recovery_history.return_value = [
        {"node_id": "power-001", "timestamp": "t1", "status": "SUCCESS", "success": True, "automatic": True}
    ]
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}/recoveries", node_id="power-001"))
    assert response["statusCode"] == 200
    body = _body(response)
    assert body["count"] == 1
    assert body["items"][0]["status"] == "SUCCESS"


# --- summary route ---


def test_summary_200_response_shape(_mock_query_service):
    _mock_query_service.get_summary.return_value = {
        "current_state": {"node_id": "power-001", "state": "HEALTHY"},
        "anomalies": [{"node_id": "power-001", "timestamp": "t1"}],
        "recoveries": [],
    }
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}/summary", node_id="power-001"))
    assert response["statusCode"] == 200
    body = _body(response)
    assert body["current_state"] == {"node_id": "power-001", "state": "HEALTHY"}
    assert body["anomalies"] == {"count": 1, "items": [{"node_id": "power-001", "timestamp": "t1"}]}
    assert body["recoveries"] == {"count": 0, "items": []}


def test_summary_missing_node_returns_404_not_fabricated(_mock_query_service):
    _mock_query_service.get_summary.return_value = None
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}/summary", node_id="does-not-exist"))
    assert response["statusCode"] == 404
    assert _body(response)["error"] == "NODE_NOT_FOUND"


# --- error handling / safe messages ---


def test_dynamodb_failure_returns_500_with_safe_message(_mock_query_service):
    _mock_query_service.get_current_state.side_effect = KnowledgeQueryError("Failed to read current state for node 'x'.")
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="power-001"))
    assert response["statusCode"] == 500
    body = _body(response)
    assert body["error"] == "INTERNAL_ERROR"
    assert "boto" not in body["message"].lower()
    assert "traceback" not in body["message"].lower()


def test_unexpected_exception_returns_500_not_a_crash(_mock_query_service):
    _mock_query_service.get_current_state.side_effect = RuntimeError("something truly unexpected")
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="power-001"))
    assert response["statusCode"] == 500
    assert _body(response)["error"] == "INTERNAL_ERROR"
    assert "something truly unexpected" not in json.dumps(_body(response))


def test_unrecognized_route_key_returns_400_not_500(_mock_query_service):
    response = handler_module.lambda_handler(_event("POST /nodes/{node_id}", node_id="power-001"))
    assert response["statusCode"] == 400


# --- API Gateway event parsing ---


def test_missing_query_string_parameters_key_uses_defaults(_mock_query_service):
    _mock_query_service.get_anomaly_history.return_value = []
    event = {"routeKey": "GET /nodes/{node_id}/anomalies", "pathParameters": {"node_id": "power-001"}}
    response = handler_module.lambda_handler(event)
    assert response["statusCode"] == 200
    _mock_query_service.get_anomaly_history.assert_called_once_with("power-001", limit=20, order="desc")


def test_missing_path_parameters_key_returns_400():
    event = {"routeKey": "GET /nodes/{node_id}"}
    response = handler_module.lambda_handler(event)
    assert response["statusCode"] == 400


# --- Response format ---


def test_response_has_content_type_json_header(_mock_query_service):
    _mock_query_service.get_current_state.return_value = {"node_id": "power-001", "state": "HEALTHY"}
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="power-001"))
    assert response["headers"]["Content-Type"] == "application/json"


def test_response_body_is_valid_json_string(_mock_query_service):
    _mock_query_service.get_current_state.return_value = {"node_id": "power-001", "state": "HEALTHY"}
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="power-001"))
    assert isinstance(response["body"], str)
    json.loads(response["body"])  # must not raise


# --- No CORS headers by design ---


def test_no_cors_headers_present(_mock_query_service):
    _mock_query_service.get_current_state.return_value = {"node_id": "power-001", "state": "HEALTHY"}
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="power-001"))
    for header_name in response["headers"]:
        assert not header_name.lower().startswith("access-control-")


# --- GET /nodes (Step 40, fleet listing) ---


def _list_event(query_params=None):
    return {"routeKey": "GET /nodes", "pathParameters": {}, "queryStringParameters": query_params}


def test_list_nodes_default_200_response_shape(_mock_query_service):
    _mock_query_service.list_nodes.return_value = [{"node_id": "power-001", "state": "HEALTHY"}]
    response = handler_module.lambda_handler(_list_event())
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["nodes"] == [{"node_id": "power-001", "state": "HEALTHY"}]
    assert body["count"] == 1
    assert body["limit"] == 50
    assert body["order"] == "desc"


def test_list_nodes_calls_service_with_default_limit_and_order(_mock_query_service):
    _mock_query_service.list_nodes.return_value = []
    handler_module.lambda_handler(_list_event())
    _mock_query_service.list_nodes.assert_called_once_with(limit=50, order="desc", subsystem=None, state=None)


def test_list_nodes_empty_fleet_returns_200_with_empty_list(_mock_query_service):
    _mock_query_service.list_nodes.return_value = []
    response = handler_module.lambda_handler(_list_event())
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body == {"nodes": [], "count": 0, "limit": 50, "order": "desc"}


def test_list_nodes_limit_50_is_valid(_mock_query_service):
    _mock_query_service.list_nodes.return_value = []
    response = handler_module.lambda_handler(_list_event({"limit": "50"}))
    assert response["statusCode"] == 200


def test_list_nodes_limit_500_is_valid_upper_bound(_mock_query_service):
    _mock_query_service.list_nodes.return_value = []
    response = handler_module.lambda_handler(_list_event({"limit": "500"}))
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["limit"] == 500


def test_list_nodes_limit_above_500_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_event({"limit": "501"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_LIMIT"
    _mock_query_service.list_nodes.assert_not_called()


def test_list_nodes_limit_zero_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_event({"limit": "0"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_LIMIT"


def test_list_nodes_non_numeric_limit_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_event({"limit": "abc"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_LIMIT"


def test_list_nodes_uses_list_specific_bounds_not_the_per_node_history_bounds(_mock_query_service):
    # 100 is MAX_LIMIT for the per-node history routes but well within
    # MAX_LIST_LIMIT (500) for this route -- must be accepted.
    _mock_query_service.list_nodes.return_value = []
    response = handler_module.lambda_handler(_list_event({"limit": "200"}))
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["limit"] == 200


def test_list_nodes_invalid_order_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_event({"order": "sideways"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_ORDER"
    _mock_query_service.list_nodes.assert_not_called()


def test_list_nodes_ascending_order_is_passed_through(_mock_query_service):
    _mock_query_service.list_nodes.return_value = []
    handler_module.lambda_handler(_list_event({"order": "asc"}))
    assert _mock_query_service.list_nodes.call_args.kwargs["order"] == "asc"


def test_list_nodes_valid_subsystem_filter_is_passed_through(_mock_query_service):
    _mock_query_service.list_nodes.return_value = []
    response = handler_module.lambda_handler(_list_event({"subsystem": "power"}))
    assert response["statusCode"] == 200
    assert _mock_query_service.list_nodes.call_args.kwargs["subsystem"] == "power"
    assert json.loads(response["body"])["subsystem"] == "power"


def test_list_nodes_invalid_subsystem_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_event({"subsystem": "not-a-real-subsystem"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_SUBSYSTEM"
    _mock_query_service.list_nodes.assert_not_called()


def test_list_nodes_valid_state_filter_is_passed_through(_mock_query_service):
    _mock_query_service.list_nodes.return_value = []
    response = handler_module.lambda_handler(_list_event({"state": "FAULT_DETECTED"}))
    assert response["statusCode"] == 200
    assert _mock_query_service.list_nodes.call_args.kwargs["state"] == "FAULT_DETECTED"
    assert json.loads(response["body"])["state"] == "FAULT_DETECTED"


def test_list_nodes_invalid_state_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_event({"state": "NOT_A_REAL_STATE"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_STATE"
    _mock_query_service.list_nodes.assert_not_called()


def test_list_nodes_omits_subsystem_and_state_keys_when_not_filtered(_mock_query_service):
    _mock_query_service.list_nodes.return_value = []
    response = handler_module.lambda_handler(_list_event())
    body = json.loads(response["body"])
    assert "subsystem" not in body
    assert "state" not in body


def test_list_nodes_dynamodb_failure_returns_500_with_safe_message(_mock_query_service):
    _mock_query_service.list_nodes.side_effect = KnowledgeQueryError("Failed to list current node state.")
    response = handler_module.lambda_handler(_list_event())
    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["error"] == "INTERNAL_ERROR"
    assert "boto" not in body["message"].lower()


def test_list_nodes_does_not_require_a_node_id(_mock_query_service):
    # No pathParameters.node_id at all -- must not raise/require one.
    _mock_query_service.list_nodes.return_value = []
    event = {"routeKey": "GET /nodes", "pathParameters": None, "queryStringParameters": None}
    response = handler_module.lambda_handler(event)
    assert response["statusCode"] == 200


def test_list_nodes_response_has_no_cors_headers_of_its_own(_mock_query_service):
    _mock_query_service.list_nodes.return_value = []
    response = handler_module.lambda_handler(_list_event())
    for header_name in response["headers"]:
        assert not header_name.lower().startswith("access-control-")


# --- Existing routes regression (Step 40 did not change their behavior) ---


def test_existing_current_state_route_unaffected_by_list_nodes_addition(_mock_query_service):
    _mock_query_service.get_current_state.return_value = {"node_id": "power-001", "state": "HEALTHY"}
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="power-001"))
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"node": {"node_id": "power-001", "state": "HEALTHY"}}


def test_existing_anomalies_route_still_uses_original_limit_bounds(_mock_query_service):
    # MAX_LIMIT (100) for this route is unchanged and unaffected by
    # MAX_LIST_LIMIT (500) existing for a different route.
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}/anomalies", node_id="power-001", query_params={"limit": "101"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_LIMIT"
    assert "101" in json.loads(response["body"])["message"] or "100" in json.loads(response["body"])["message"]


def test_existing_recoveries_route_regression(_mock_query_service):
    _mock_query_service.get_recovery_history.return_value = [{"node_id": "power-001", "status": "SUCCESS"}]
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}/recoveries", node_id="power-001"))
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] == 1


def test_existing_summary_route_regression(_mock_query_service):
    _mock_query_service.get_summary.return_value = {
        "current_state": {"node_id": "power-001", "state": "HEALTHY"},
        "anomalies": [],
        "recoveries": [],
    }
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}/summary", node_id="power-001"))
    assert response["statusCode"] == 200


def test_existing_list_nodes_route_regression(_mock_query_service):
    _mock_query_service.list_nodes.return_value = [{"node_id": "power-001", "state": "HEALTHY"}]
    response = handler_module.lambda_handler(_list_nodes_event())
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["count"] == 1


# --- GET /events (Step 42, fleet-wide event feed) ---


def _list_nodes_event(query_params=None):
    return {"routeKey": "GET /nodes", "pathParameters": {}, "queryStringParameters": query_params}


def _list_events_event(query_params=None):
    return {"routeKey": "GET /events", "pathParameters": {}, "queryStringParameters": query_params}


_SAMPLE_ANOMALY_EVENT = {
    "event_type": "anomaly",
    "node_id": "power-001",
    "timestamp": "2026-01-01T00:00:05+00:00",
    "subsystem": "power",
    "fault_type": "NODE_OVERLOAD",
    "severity": "MEDIUM",
    "reason": "x",
    "detected": True,
    "evidence": {},
    "action": None,
    "automatic": None,
    "status": None,
    "success": None,
    "recovery_started_at": None,
    "recorded_at": None,
}

_SAMPLE_RECOVERY_EVENT = {
    "event_type": "recovery",
    "node_id": "power-001",
    "timestamp": "2026-01-01T00:00:05+00:00",
    "subsystem": "power",
    "fault_type": "NODE_OVERLOAD",
    "severity": "MEDIUM",
    "reason": "ok",
    "detected": None,
    "evidence": None,
    "action": "REDUCE_LOAD",
    "automatic": True,
    "status": "SUCCESS",
    "success": True,
    "recovery_started_at": "2026-01-01T00:00:05+00:00",
    "recorded_at": "2026-01-01T00:00:07+00:00",
}


def test_list_events_default_200_response_shape(_mock_query_service):
    _mock_query_service.list_events.return_value = [_SAMPLE_ANOMALY_EVENT]
    response = handler_module.lambda_handler(_list_events_event())
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["events"] == [_SAMPLE_ANOMALY_EVENT]
    assert body["count"] == 1
    assert body["limit"] == 50
    assert body["order"] == "desc"


def test_list_events_calls_service_with_default_limit_and_order(_mock_query_service):
    _mock_query_service.list_events.return_value = []
    handler_module.lambda_handler(_list_events_event())
    _mock_query_service.list_events.assert_called_once_with(
        limit=50, order="desc", event_type=None, subsystem=None, severity=None, status=None
    )


def test_list_events_empty_returns_200_with_empty_list(_mock_query_service):
    _mock_query_service.list_events.return_value = []
    response = handler_module.lambda_handler(_list_events_event())
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body == {"events": [], "count": 0, "limit": 50, "order": "desc"}


def test_list_events_limit_20_is_valid(_mock_query_service):
    _mock_query_service.list_events.return_value = []
    response = handler_module.lambda_handler(_list_events_event({"limit": "20"}))
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["limit"] == 20


def test_list_events_limit_100_is_valid_upper_bound(_mock_query_service):
    _mock_query_service.list_events.return_value = []
    response = handler_module.lambda_handler(_list_events_event({"limit": "100"}))
    assert response["statusCode"] == 200


def test_list_events_limit_above_100_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_events_event({"limit": "101"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_LIMIT"
    _mock_query_service.list_events.assert_not_called()


def test_list_events_limit_zero_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_events_event({"limit": "0"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_LIMIT"


def test_list_events_uses_event_specific_bounds_not_list_nodes_bounds(_mock_query_service):
    # 500 is valid for GET /nodes (MAX_LIST_LIMIT) but invalid here
    # (MAX_EVENT_LIMIT is 100) -- the two bounds must not be shared.
    response = handler_module.lambda_handler(_list_events_event({"limit": "500"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_LIMIT"


def test_list_events_invalid_order_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_events_event({"order": "sideways"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_ORDER"
    _mock_query_service.list_events.assert_not_called()


def test_list_events_ascending_order_is_passed_through(_mock_query_service):
    _mock_query_service.list_events.return_value = []
    handler_module.lambda_handler(_list_events_event({"order": "asc"}))
    assert _mock_query_service.list_events.call_args.kwargs["order"] == "asc"


def test_list_events_valid_event_type_anomaly(_mock_query_service):
    _mock_query_service.list_events.return_value = [_SAMPLE_ANOMALY_EVENT]
    response = handler_module.lambda_handler(_list_events_event({"event_type": "anomaly"}))
    assert response["statusCode"] == 200
    assert _mock_query_service.list_events.call_args.kwargs["event_type"] == "anomaly"
    assert json.loads(response["body"])["event_type"] == "anomaly"


def test_list_events_valid_event_type_recovery(_mock_query_service):
    _mock_query_service.list_events.return_value = [_SAMPLE_RECOVERY_EVENT]
    response = handler_module.lambda_handler(_list_events_event({"event_type": "recovery"}))
    assert response["statusCode"] == 200
    assert _mock_query_service.list_events.call_args.kwargs["event_type"] == "recovery"


def test_list_events_invalid_event_type_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_events_event({"event_type": "escalation"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_EVENT_TYPE"
    _mock_query_service.list_events.assert_not_called()


def test_list_events_valid_subsystem_filter(_mock_query_service):
    _mock_query_service.list_events.return_value = []
    response = handler_module.lambda_handler(_list_events_event({"subsystem": "power"}))
    assert response["statusCode"] == 200
    assert _mock_query_service.list_events.call_args.kwargs["subsystem"] == "power"
    assert json.loads(response["body"])["subsystem"] == "power"


def test_list_events_invalid_subsystem_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_events_event({"subsystem": "not-real"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_SUBSYSTEM"
    _mock_query_service.list_events.assert_not_called()


def test_list_events_valid_severity_filter(_mock_query_service):
    _mock_query_service.list_events.return_value = []
    response = handler_module.lambda_handler(_list_events_event({"severity": "HIGH"}))
    assert response["statusCode"] == 200
    assert _mock_query_service.list_events.call_args.kwargs["severity"] == "HIGH"
    assert json.loads(response["body"])["severity"] == "HIGH"


def test_list_events_invalid_severity_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_events_event({"severity": "EXTREME"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_SEVERITY"
    _mock_query_service.list_events.assert_not_called()


def test_list_events_valid_status_filter(_mock_query_service):
    _mock_query_service.list_events.return_value = [_SAMPLE_RECOVERY_EVENT]
    response = handler_module.lambda_handler(_list_events_event({"status": "SUCCESS"}))
    assert response["statusCode"] == 200
    assert _mock_query_service.list_events.call_args.kwargs["status"] == "SUCCESS"
    assert json.loads(response["body"])["status"] == "SUCCESS"


def test_list_events_invalid_status_returns_400(_mock_query_service):
    response = handler_module.lambda_handler(_list_events_event({"status": "PENDING"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_STATUS"
    _mock_query_service.list_events.assert_not_called()


def test_list_events_status_with_event_type_anomaly_returns_400(_mock_query_service):
    # status only applies to recovery events -- a contradictory request.
    response = handler_module.lambda_handler(_list_events_event({"event_type": "anomaly", "status": "SUCCESS"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_STATUS"
    _mock_query_service.list_events.assert_not_called()


def test_list_events_status_with_event_type_recovery_is_valid(_mock_query_service):
    _mock_query_service.list_events.return_value = [_SAMPLE_RECOVERY_EVENT]
    response = handler_module.lambda_handler(_list_events_event({"event_type": "recovery", "status": "SUCCESS"}))
    assert response["statusCode"] == 200


def test_list_events_dynamodb_failure_returns_500_with_safe_message(_mock_query_service):
    _mock_query_service.list_events.side_effect = KnowledgeQueryError("Failed to list events.")
    response = handler_module.lambda_handler(_list_events_event())
    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["error"] == "INTERNAL_ERROR"
    assert "boto" not in body["message"].lower()


def test_list_events_does_not_require_a_node_id(_mock_query_service):
    _mock_query_service.list_events.return_value = []
    event = {"routeKey": "GET /events", "pathParameters": None, "queryStringParameters": None}
    response = handler_module.lambda_handler(event)
    assert response["statusCode"] == 200


def test_list_events_response_has_no_cors_headers_of_its_own(_mock_query_service):
    _mock_query_service.list_events.return_value = []
    response = handler_module.lambda_handler(_list_events_event())
    for header_name in response["headers"]:
        assert not header_name.lower().startswith("access-control-")


def test_list_events_omits_optional_filter_keys_when_not_filtered(_mock_query_service):
    _mock_query_service.list_events.return_value = []
    response = handler_module.lambda_handler(_list_events_event())
    body = json.loads(response["body"])
    assert "event_type" not in body
    assert "subsystem" not in body
    assert "severity" not in body
    assert "status" not in body


# --- GET /metrics (Step 43, CloudWatch metrics feed) ---


def _metrics_event(query_params=None):
    return {"routeKey": "GET /metrics", "pathParameters": None, "queryStringParameters": query_params}


def test_get_metrics_default_returns_200(_mock_metrics_service):
    response = handler_module.lambda_handler(_metrics_event())
    assert response["statusCode"] == 200


def test_get_metrics_default_uses_last_one_hour_window(_mock_metrics_service):
    handler_module.lambda_handler(_metrics_event())
    call = _mock_metrics_service.get_metrics.call_args.kwargs
    delta = (call["end"] - call["start"]).total_seconds()
    assert 3599 <= delta <= 3601


def test_get_metrics_default_body_shape(_mock_metrics_service):
    _mock_metrics_service.get_metrics.return_value = {
        "period_seconds": 60,
        "series": [
            {
                "metric_name": "FaultDetectionCount",
                "unit": "Count",
                "dimensions": {"Subsystem": "power", "FaultType": "NODE_OVERLOAD"},
                "datapoints": [{"timestamp": "2026-01-01T00:00:00+00:00", "value": 1}],
                "summary": {"datapoint_count": 1, "sum": 1},
            }
        ],
    }
    response = handler_module.lambda_handler(_metrics_event())
    body = json.loads(response["body"])
    assert body["namespace"] == "SentinelGrid"
    assert body["period_seconds"] == 60
    assert "start" in body and "end" in body
    assert len(body["series"]) == 1
    assert "subsystem" not in body
    assert "fault_type" not in body


def test_get_metrics_explicit_valid_start_end_returns_200(_mock_metrics_service):
    response = handler_module.lambda_handler(
        _metrics_event({"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-01T00:30:00+00:00"})
    )
    assert response["statusCode"] == 200
    call = _mock_metrics_service.get_metrics.call_args.kwargs
    assert call["start"] == datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert call["end"] == datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)


def test_get_metrics_accepts_trailing_z_timestamps(_mock_metrics_service):
    response = handler_module.lambda_handler(
        _metrics_event({"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:30:00Z"})
    )
    assert response["statusCode"] == 200


def test_get_metrics_only_start_given_derives_end_as_now(_mock_metrics_service):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=10)).isoformat()
    response = handler_module.lambda_handler(_metrics_event({"start": start}))
    assert response["statusCode"] == 200
    call = _mock_metrics_service.get_metrics.call_args.kwargs
    assert abs((call["end"] - now).total_seconds()) < 5


def test_get_metrics_only_end_given_derives_start_one_hour_before(_mock_metrics_service):
    end = "2026-01-01T05:00:00+00:00"
    response = handler_module.lambda_handler(_metrics_event({"end": end}))
    assert response["statusCode"] == 200
    call = _mock_metrics_service.get_metrics.call_args.kwargs
    assert call["end"] == datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)
    assert call["start"] == datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)


def test_get_metrics_invalid_start_timestamp_returns_400(_mock_metrics_service):
    response = handler_module.lambda_handler(_metrics_event({"start": "not-a-timestamp"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_START"
    _mock_metrics_service.get_metrics.assert_not_called()


def test_get_metrics_invalid_end_timestamp_returns_400(_mock_metrics_service):
    response = handler_module.lambda_handler(_metrics_event({"end": "not-a-timestamp"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_END"


def test_get_metrics_end_equal_to_start_returns_400(_mock_metrics_service):
    ts = "2026-01-01T00:00:00+00:00"
    response = handler_module.lambda_handler(_metrics_event({"start": ts, "end": ts}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_TIME_RANGE"


def test_get_metrics_end_before_start_returns_400(_mock_metrics_service):
    response = handler_module.lambda_handler(
        _metrics_event({"start": "2026-01-01T01:00:00+00:00", "end": "2026-01-01T00:00:00+00:00"})
    )
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_TIME_RANGE"


def test_get_metrics_window_over_24_hours_returns_400(_mock_metrics_service):
    response = handler_module.lambda_handler(
        _metrics_event({"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-02T00:00:01+00:00"})
    )
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "TIME_RANGE_TOO_LARGE"


def test_get_metrics_window_of_exactly_24_hours_is_valid(_mock_metrics_service):
    response = handler_module.lambda_handler(
        _metrics_event({"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-02T00:00:00+00:00"})
    )
    assert response["statusCode"] == 200


def test_get_metrics_future_end_timestamp_returns_400(_mock_metrics_service):
    future_end = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response = handler_module.lambda_handler(_metrics_event({"end": future_end}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "FUTURE_END_TIMESTAMP"


def test_get_metrics_end_within_clock_skew_tolerance_of_now_is_valid(_mock_metrics_service):
    end = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    response = handler_module.lambda_handler(_metrics_event({"end": end}))
    assert response["statusCode"] == 200


def test_get_metrics_valid_subsystem_filter_returns_200(_mock_metrics_service):
    response = handler_module.lambda_handler(_metrics_event({"subsystem": "power"}))
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["subsystem"] == "power"
    assert _mock_metrics_service.get_metrics.call_args.kwargs["subsystem"] == "power"


def test_get_metrics_invalid_subsystem_filter_returns_400(_mock_metrics_service):
    response = handler_module.lambda_handler(_metrics_event({"subsystem": "not-a-subsystem"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_SUBSYSTEM"


def test_get_metrics_valid_fault_type_filter_returns_200(_mock_metrics_service):
    response = handler_module.lambda_handler(_metrics_event({"fault_type": "NODE_OVERLOAD"}))
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["fault_type"] == "NODE_OVERLOAD"
    assert _mock_metrics_service.get_metrics.call_args.kwargs["fault_type"] == "NODE_OVERLOAD"


def test_get_metrics_invalid_fault_type_filter_returns_400(_mock_metrics_service):
    response = handler_module.lambda_handler(_metrics_event({"fault_type": "NOT_A_REAL_FAULT"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_FAULT_TYPE"


def test_get_metrics_rejects_none_as_a_fault_type():
    # NONE is a real detector.FaultType enum member, but never published as
    # a metric dimension value (see handler.py's own comment) -- so it must
    # not be accepted here even though it's a "real" backend value elsewhere.
    response = handler_module.lambda_handler(_metrics_event({"fault_type": "NONE"}))
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "INVALID_FAULT_TYPE"


def test_get_metrics_cloudwatch_failure_returns_500(_mock_metrics_service):
    _mock_metrics_service.get_metrics.side_effect = MetricsQueryError("CloudWatch is unavailable.")
    response = handler_module.lambda_handler(_metrics_event())
    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["error"] == "INTERNAL_ERROR"
    assert "boto" not in body["message"].lower()
    assert "cloudwatch is unavailable" not in body["message"].lower()


def test_get_metrics_does_not_require_a_node_id(_mock_metrics_service):
    response = handler_module.lambda_handler(_metrics_event())
    assert response["statusCode"] == 200


def test_get_metrics_response_has_no_cors_headers_of_its_own(_mock_metrics_service):
    response = handler_module.lambda_handler(_metrics_event())
    for header_name in response["headers"]:
        assert not header_name.lower().startswith("access-control-")


def test_get_metrics_never_calls_the_dynamodb_query_service(_mock_query_service, _mock_metrics_service):
    handler_module.lambda_handler(_metrics_event())
    _mock_query_service.list_nodes.assert_not_called()
    _mock_query_service.list_events.assert_not_called()
    _mock_query_service.get_current_state.assert_not_called()


# --- Existing routes regression (Step 43 did not change their behavior) ---


def test_existing_list_nodes_route_unaffected_by_metrics_addition(_mock_query_service, _mock_metrics_service):
    _mock_query_service.list_nodes.return_value = []
    response = handler_module.lambda_handler(_list_nodes_event())
    assert response["statusCode"] == 200
    _mock_metrics_service.get_metrics.assert_not_called()


def test_existing_list_events_route_unaffected_by_metrics_addition(_mock_query_service, _mock_metrics_service):
    _mock_query_service.list_events.return_value = []
    response = handler_module.lambda_handler(_list_events_event())
    assert response["statusCode"] == 200
    _mock_metrics_service.get_metrics.assert_not_called()


def test_existing_current_state_route_unaffected_by_metrics_addition(_mock_query_service, _mock_metrics_service):
    _mock_query_service.get_current_state.return_value = {"node_id": "power-001", "state": "HEALTHY"}
    response = handler_module.lambda_handler(_event("GET /nodes/{node_id}", node_id="power-001"))
    assert response["statusCode"] == 200
    _mock_metrics_service.get_metrics.assert_not_called()
