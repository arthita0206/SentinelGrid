"""Unit tests for the SQS consumer Lambda adapter
(lambda_functions/sqs_consumer/handler.py).

These tests never touch AWS — `lambda_handler` is called directly as a
plain Python function, and the module-level `_analyzer_client` is always
mocked out. No AWS credentials, boto3 clients, or network access are
required.
"""

import inspect
import json
from unittest.mock import patch

import pytest

from lambda_functions.sqs_consumer import handler as handler_module
from lambda_functions.sqs_consumer.anomaly_detector_client import AnomalyDetectorInvocationError
from lambda_functions.sqs_consumer.handler import lambda_handler


def _telemetry(node_id="node-001", subsystem="power", timestamp="2026-01-01T00:00:00+00:00"):
    return {
        "node_id": node_id,
        "subsystem": subsystem,
        "timestamp": timestamp,
        "status": "ONLINE",
        "data": {"load": 55.0, "voltage": 230.0, "frequency": 50.0},
    }


def _record(message_id, body):
    body_str = body if isinstance(body, str) else json.dumps(body)
    return {
        "messageId": message_id,
        "receiptHandle": f"receipt-{message_id}",
        "body": body_str,
        "eventSource": "aws:sqs",
    }


@pytest.fixture(autouse=True)
def _mock_analyzer_client():
    """Auto-applied to every test: prevents any real AWS Lambda invoke —
    mirrors lambda_functions/anomaly_detector/handler.py's own
    `_mock_trigger_client` fixture pattern in
    tests/test_anomaly_detector_handler.py.
    """
    with patch.object(handler_module, "_analyzer_client") as mock_client:
        mock_client.invoke.return_value = {"detected": False}
        yield mock_client


def test_lambda_handler_signature_accepts_event_and_context():
    sig = inspect.signature(lambda_handler)
    assert list(sig.parameters.keys())[:2] == ["event", "context"]


# --- A. One valid record: body parses, detector receives exact telemetry dict, no batch failure ---


def test_single_valid_record_invokes_analyzer_with_exact_payload_and_reports_no_failure(_mock_analyzer_client):
    payload = _telemetry()
    event = {"Records": [_record("msg-1", payload)]}

    response = lambda_handler(event, None)

    _mock_analyzer_client.invoke.assert_called_once_with(payload)
    assert response == {"batchItemFailures": []}


# --- B. Multiple valid records all processed exactly once ---


def test_multiple_valid_records_each_processed_exactly_once(_mock_analyzer_client):
    payloads = [_telemetry(node_id=f"node-{i}") for i in range(3)]
    event = {"Records": [_record(f"msg-{i}", p) for i, p in enumerate(payloads)]}

    response = lambda_handler(event, None)

    assert _mock_analyzer_client.invoke.call_count == 3
    called_with = [call.args[0] for call in _mock_analyzer_client.invoke.call_args_list]
    assert called_with == payloads
    assert response == {"batchItemFailures": []}


# --- C. Invalid JSON body -> messageId in batchItemFailures ---


def test_invalid_json_body_is_reported_as_batch_item_failure(_mock_analyzer_client):
    event = {"Records": [_record("msg-bad-json", "{not valid json")]}

    response = lambda_handler(event, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "msg-bad-json"}]}
    _mock_analyzer_client.invoke.assert_not_called()


# --- D. JSON array / non-object -> messageId in batchItemFailures ---


@pytest.mark.parametrize("body", ["[1, 2, 3]", "\"just a string\"", "42", "null"])
def test_non_object_json_body_is_reported_as_batch_item_failure(_mock_analyzer_client, body):
    event = {"Records": [_record("msg-non-object", body)]}

    response = lambda_handler(event, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "msg-non-object"}]}
    _mock_analyzer_client.invoke.assert_not_called()


# --- E. One failed + one successful record: successful completes, failed reported ---


def test_one_failed_and_one_successful_record_are_both_correctly_reported(_mock_analyzer_client):
    good_payload = _telemetry(node_id="node-good")
    event = {
        "Records": [
            _record("msg-good", good_payload),
            _record("msg-bad", "{not valid json"),
        ]
    }

    response = lambda_handler(event, None)

    _mock_analyzer_client.invoke.assert_called_once_with(good_payload)
    assert response == {"batchItemFailures": [{"itemIdentifier": "msg-bad"}]}


# --- F. Existing anomaly detector exception -> failed message reported, others continue ---


def test_analyzer_invocation_failure_is_reported_without_aborting_the_batch(_mock_analyzer_client):
    good_payload = _telemetry(node_id="node-good")
    failing_payload = _telemetry(node_id="node-failing")

    def _invoke_side_effect(payload):
        if payload["node_id"] == "node-failing":
            raise AnomalyDetectorInvocationError("simulated analyzer failure")
        return {"detected": False}

    _mock_analyzer_client.invoke.side_effect = _invoke_side_effect

    event = {
        "Records": [
            _record("msg-failing", failing_payload),
            _record("msg-good", good_payload),
        ]
    }

    response = lambda_handler(event, None)

    assert response == {"batchItemFailures": [{"itemIdentifier": "msg-failing"}]}
    assert _mock_analyzer_client.invoke.call_count == 2


# --- G. Empty Records -> empty batchItemFailures ---


def test_empty_records_list_returns_empty_batch_item_failures(_mock_analyzer_client):
    response = lambda_handler({"Records": []}, None)
    assert response == {"batchItemFailures": []}
    _mock_analyzer_client.invoke.assert_not_called()


# --- H. No direct AWS calls required for unit tests (module import boundary) ---


def test_handler_module_does_not_import_boto3_directly():
    import ast
    from pathlib import Path

    source = Path(handler_module.__file__).read_text()
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert "boto3" not in imported_names
    assert "botocore" not in imported_names


# --- I. Consumer does not duplicate detection/persistence/remediation logic ---


def test_handler_module_does_not_import_anomaly_detector_package():
    import ast
    from pathlib import Path

    source = Path(handler_module.__file__).read_text()
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    # Matches the lambda_functions.anomaly_detector PACKAGE specifically
    # (e.g. "lambda_functions.anomaly_detector" or
    # "lambda_functions.anomaly_detector.detector") — not this module's
    # own local sibling "anomaly_detector_client", which merely shares a
    # name prefix but is not that package.
    assert not any(
        name == "lambda_functions.anomaly_detector" or name.startswith("lambda_functions.anomaly_detector.")
        for name in imported_names
    )


def test_payload_is_never_mutated_before_invocation(_mock_analyzer_client):
    payload = _telemetry()
    original = json.loads(json.dumps(payload))
    event = {"Records": [_record("msg-1", payload)]}

    lambda_handler(event, None)

    assert payload == original


def test_missing_analyzer_arn_reports_failure_without_crashing():
    with patch.object(handler_module, "_analyzer_client") as mock_client:
        mock_client.invoke.side_effect = AnomalyDetectorInvocationError(
            "Anomaly detector function ARN is not configured; cannot invoke."
        )
        event = {"Records": [_record("msg-1", _telemetry())]}

        response = lambda_handler(event, None)

        assert response == {"batchItemFailures": [{"itemIdentifier": "msg-1"}]}
