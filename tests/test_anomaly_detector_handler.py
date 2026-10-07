"""Unit tests for the Lambda adapter (lambda_functions/anomaly_detector/handler.py).

These tests never touch AWS — `lambda_handler` is called directly as a
plain Python function, exactly as it would be for local testing. No AWS
credentials, boto3 clients, or network access are required.
"""

import inspect
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from lambda_functions.anomaly_detector import handler as handler_module
from lambda_functions.anomaly_detector.detector import DetectionResult
from lambda_functions.anomaly_detector.event_store import AnomalyEventPersistenceError, PersistenceOutcome
from lambda_functions.anomaly_detector.handler import lambda_handler
from lambda_functions.anomaly_detector.remediation_trigger_client import RemediationTriggerSubmissionError


def _telemetry(subsystem, data, node_id="node-001", timestamp="2026-01-01T00:00:00+00:00", status="ONLINE"):
    return {
        "node_id": node_id,
        "subsystem": subsystem,
        "timestamp": timestamp,
        "status": status,
        "data": data,
    }


@pytest.fixture(autouse=True)
def _mock_metrics_client():
    """Auto-applied to every test in this file (Step 28): prevents any
    real CloudWatch `put_metric_data` call from handler.py's own
    module-level `_metrics_client` — without this, every anomalous-result
    test would attempt a real (unmocked) AWS call. Tests that
    specifically want to inspect metric-publishing behavior (see the
    "STEP 28 — METRICS INSTRUMENTATION" section near the end of this
    file) receive this same mock via the fixture's own yielded value.
    """
    with patch.object(handler_module, "_metrics_client") as mock_client:
        yield mock_client


@pytest.fixture(autouse=True)
def _mock_trigger_client():
    """Auto-applied to every test in this file (Step 22): prevents any
    real AWS Lambda invoke call for remediation-trigger submission,
    exactly mirroring how each test below already mocks `_event_store`
    for persistence — without this, every anomalous-result test would
    attempt a real `_trigger_client.submit()` call and fail on
    `REMEDIATION_TRIGGER_FUNCTION_ARN is not configured`. Tests that
    specifically want to inspect trigger-submission behavior (see the
    "Remediation trigger submission" section near the end of this file)
    receive this same mock via the fixture's own yielded value.
    """
    with patch.object(handler_module, "_trigger_client") as mock_client:
        yield mock_client


@pytest.fixture(autouse=True)
def _mock_node_state_store():
    """Auto-applied to every test in this file (Step 34): prevents any
    real DynamoDB `update_item` call from handler.py's own module-level
    `_node_state_store` — without this, every test would attempt a real
    (unmocked) AWS call.
    """
    with patch.object(handler_module, "_node_state_store") as mock_store:
        yield mock_store


# --- Handler contract ---


def test_lambda_handler_signature_accepts_event_and_context():
    sig = inspect.signature(lambda_handler)
    assert list(sig.parameters.keys())[:2] == ["event", "context"]


def test_handler_delegates_to_detect_telemetry_rather_than_reimplementing_rules():
    fake_result = DetectionResult(
        detected=True,
        fault_type="NODE_OVERLOAD",
        severity="MEDIUM",
        node_id="power-001",
        subsystem="power",
        timestamp="2026-01-01T00:00:00+00:00",
        reason="fake reason from mock",
        evidence={"field": "load", "value": 999},
    )
    payload = _telemetry("power", {"load": 999.0, "voltage": 230.0, "frequency": 50.0})

    with patch.object(handler_module, "_event_store"):
        with patch(
            "lambda_functions.anomaly_detector.handler.detect_telemetry", return_value=fake_result
        ) as mock_detect:
            response = lambda_handler(payload, None)

    mock_detect.assert_called_once_with(payload)
    assert response == fake_result.to_dict()


def test_response_contains_expected_detection_result_fields():
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})
    response = lambda_handler(payload, None)

    assert set(response.keys()) == {
        "detected",
        "fault_type",
        "severity",
        "node_id",
        "subsystem",
        "timestamp",
        "reason",
        "evidence",
    }


def test_response_is_json_serializable():
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})
    response = lambda_handler(payload, None)

    json.dumps(response)  # must not raise


def test_handler_can_be_called_with_context_omitted():
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})
    response = lambda_handler(payload)  # context has a default

    assert response["detected"] is False


# --- NORMAL ---


def test_normal_power_telemetry_via_handler():
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})
    response = lambda_handler(payload, None)

    assert response["detected"] is False
    assert response["fault_type"] is None
    assert response["severity"] == "NORMAL"
    assert response["subsystem"] == "power"


def test_normal_water_telemetry_via_handler():
    payload = _telemetry("water", {"pressure": 4.0, "flow_rate": 50.0, "reservoir_level": 70.0})
    response = lambda_handler(payload, None)

    assert response["detected"] is False
    assert response["fault_type"] is None


def test_normal_traffic_telemetry_via_handler():
    payload = _telemetry("traffic", {"vehicle_count": 40, "average_speed": 45.0, "signal_state": "GREEN"})
    response = lambda_handler(payload, None)

    assert response["detected"] is False
    assert response["fault_type"] is None


# --- ANOMALY ---


def test_sensor_dropout_via_handler():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0}, status="OFFLINE")
    with patch.object(handler_module, "_event_store"):
        response = lambda_handler(payload, None)

    assert response["detected"] is True
    assert response["fault_type"] == "SENSOR_DROPOUT"
    assert response["severity"] == "HIGH"


def test_data_corruption_via_handler():
    payload = _telemetry("power", {"load": 50.0, "voltage": 650.0, "frequency": 50.0})
    with patch.object(handler_module, "_event_store"):
        response = lambda_handler(payload, None)

    assert response["detected"] is True
    assert response["fault_type"] == "DATA_CORRUPTION"
    assert response["evidence"]["field"] == "voltage"


def test_node_overload_via_handler():
    payload = _telemetry("power", {"load": 97.5, "voltage": 230.0, "frequency": 50.0})
    with patch.object(handler_module, "_event_store"):
        response = lambda_handler(payload, None)

    assert response["detected"] is True
    assert response["fault_type"] == "NODE_OVERLOAD"
    assert response["severity"] == "MEDIUM"


# --- INVALID ---


def test_malformed_event_json_string_returns_invalid_input():
    response = lambda_handler("{not valid json", None)

    assert response["detected"] is True
    assert response["fault_type"] == "INVALID_INPUT"
    assert response["severity"] == "CRITICAL"


def test_missing_required_telemetry_field_returns_invalid_input():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0})
    del payload["node_id"]

    response = lambda_handler(payload, None)

    assert response["fault_type"] == "INVALID_INPUT"


def test_unsupported_subsystem_returns_invalid_input():
    payload = _telemetry("gas", {"pressure": 1.0})

    # node_id/timestamp are both present here (only subsystem is invalid),
    # so this INVALID_INPUT result is persisted per _should_persist's
    # rule — see test_invalid_input_with_node_id_and_timestamp_present_is_persisted.
    with patch.object(handler_module, "_event_store"):
        response = lambda_handler(payload, None)

    assert response["fault_type"] == "INVALID_INPUT"


# --- Alternate accepted event shape ---


def test_json_string_event_is_parsed_and_detected():
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})
    response = lambda_handler(json.dumps(payload), None)

    assert response["detected"] is False
    assert response["node_id"] == "node-001"


# --- Observability logging ---
#
# AWS IoT invokes Lambda asynchronously, so lambda_handler's return value
# is never visible to the MQTT publisher — the INFO log below is the only
# way to observe a result operationally, hence these tests.


def test_normal_telemetry_logs_detection_result_at_info(caplog):
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})

    with caplog.at_level(logging.INFO, logger="lambda_functions.anomaly_detector.handler"):
        response = lambda_handler(payload, None)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.INFO
    assert "SentinelGrid detection result" in record.message

    logged_json = record.message.split("SentinelGrid detection result: ", 1)[1]
    logged_result = json.loads(logged_json)  # must be valid JSON
    assert logged_result == response  # logged result matches what was returned


def test_anomalous_telemetry_logs_node_overload_at_info(caplog):
    payload = _telemetry("power", {"load": 97.5, "voltage": 230.0, "frequency": 50.0})

    # An anomaly now also triggers a persistence attempt (Step 11), which
    # logs its own success line — event_store itself is mocked so no real
    # AWS call happens, but the handler's "anomaly event persisted" log
    # line still comes from handler.py's own code and is expected here.
    # The mock must explicitly report NEWLY_PERSISTED: the handler now
    # only logs "persisted" for that outcome, not for a bare/unconfigured
    # mock return value (see test_handler_logs_persisted_only_for_newly_persisted_outcome).
    # Since Step 22, a successful persistence also submits an async
    # remediation trigger invocation, which logs its own third line —
    # _trigger_client is mocked by the autouse _mock_trigger_client
    # fixture, so no real AWS call happens there either.
    with caplog.at_level(logging.INFO, logger="lambda_functions.anomaly_detector.handler"):
        with patch.object(handler_module, "_event_store") as mock_store:
            mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
            response = lambda_handler(payload, None)

    assert response["fault_type"] == "NODE_OVERLOAD"
    assert len(caplog.records) == 3
    detection_log, persistence_log, trigger_log = caplog.records
    assert "NODE_OVERLOAD" in detection_log.message
    assert "anomaly event persisted" in persistence_log.message
    assert "remediation trigger submitted" in trigger_log.message

    logged_result = json.loads(
        detection_log.message.split("SentinelGrid detection result: ", 1)[1]
    )
    assert logged_result["fault_type"] == "NODE_OVERLOAD"


def test_invalid_event_result_is_also_logged(caplog):
    with caplog.at_level(logging.INFO, logger="lambda_functions.anomaly_detector.handler"):
        response = lambda_handler("{not valid json", None)

    assert response["fault_type"] == "INVALID_INPUT"
    assert len(caplog.records) == 1
    assert "INVALID_INPUT" in caplog.records[0].message


def test_logging_does_not_change_the_returned_result(caplog):
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})

    with caplog.at_level(logging.INFO, logger="lambda_functions.anomaly_detector.handler"):
        response_with_logging = lambda_handler(payload, None)

    caplog.clear()
    response_without_capture = lambda_handler(payload, None)

    assert response_with_logging == response_without_capture


def test_handler_still_delegates_to_detect_telemetry_with_logging_present(caplog):
    fake_result = DetectionResult(
        detected=True,
        fault_type="NODE_OVERLOAD",
        severity="MEDIUM",
        node_id="power-001",
        subsystem="power",
        timestamp="2026-01-01T00:00:00+00:00",
        reason="fake reason from mock",
        evidence={"field": "load", "value": 999},
    )
    payload = _telemetry("power", {"load": 999.0, "voltage": 230.0, "frequency": 50.0})

    with caplog.at_level(logging.INFO, logger="lambda_functions.anomaly_detector.handler"):
        with patch.object(handler_module, "_event_store"):
            with patch(
                "lambda_functions.anomaly_detector.handler.detect_telemetry", return_value=fake_result
            ) as mock_detect:
                response = lambda_handler(payload, None)

    mock_detect.assert_called_once_with(payload)
    assert response == fake_result.to_dict()
    assert "NODE_OVERLOAD" in caplog.records[0].message


def test_no_sensitive_values_are_logged(caplog):
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})
    # Extraneous, secret-shaped field on the incoming event — must never
    # be echoed into the log, since the log only ever serializes the
    # DetectionResult, not the raw event.
    payload["aws_secret_access_key"] = "FAKESECRETVALUEFORTESTONLY"

    with caplog.at_level(logging.INFO, logger="lambda_functions.anomaly_detector.handler"):
        lambda_handler(payload, None)

    assert len(caplog.records) == 1
    logged_text = caplog.records[0].message
    assert "FAKESECRETVALUEFORTESTONLY" not in logged_text
    assert "aws_secret_access_key" not in logged_text


# --- Logger configuration itself (regression test for the real AWS bug) ---
#
# The tests above all use `caplog.at_level(logging.INFO, ...)`, which
# *forces* the logger's level for the duration of the test — so they would
# still pass even if the module never configured its own level. That's
# exactly what happened in production: the handler's logger.info() call
# never reached CloudWatch because nothing set this logger's level above
# Python's default (WARNING), and none of the caplog-based tests above
# caught it, since caplog was quietly doing that job for them. The tests
# below check the module's actual logger configuration directly, with no
# caplog level override, so they would fail again if that fix regressed.


def test_module_logger_is_configured_for_info_level():
    assert handler_module.logger.getEffectiveLevel() <= logging.INFO


def test_info_log_is_emitted_without_any_test_harness_forcing_the_level():
    """Attaches a plain handler directly to the module's own logger and
    sets no level anywhere in the test itself — proving the module's own
    `logger.setLevel(logging.INFO)` is what makes the record emit, not a
    testing convenience."""

    class _CollectingHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    collector = _CollectingHandler()
    handler_module.logger.addHandler(collector)
    try:
        payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})
        response = lambda_handler(payload, None)
    finally:
        handler_module.logger.removeHandler(collector)

    assert response["detected"] is False  # return value unaffected by logging
    assert len(collector.records) == 1
    assert collector.records[0].levelno == logging.INFO
    assert "SentinelGrid detection result" in collector.records[0].getMessage()


# --- Anomaly-event persistence (Step 11) ---
#
# `_event_store` is a module-level singleton (see handler.py) so it can be
# reused across warm Lambda invocations. These tests replace it with a
# MagicMock via `patch.object`, so no real AWS/DynamoDB call is ever made.


def test_normal_result_does_not_call_event_store():
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        response = lambda_handler(payload, None)

    assert response["detected"] is False
    mock_store.put_event.assert_not_called()


def test_node_overload_calls_event_store_with_correct_result():
    payload = _telemetry("power", {"load": 97.5, "voltage": 230.0, "frequency": 50.0}, node_id="power-007")

    with patch.object(handler_module, "_event_store") as mock_store:
        response = lambda_handler(payload, None)

    assert response["fault_type"] == "NODE_OVERLOAD"
    mock_store.put_event.assert_called_once()
    persisted_result = mock_store.put_event.call_args.args[0]
    assert isinstance(persisted_result, DetectionResult)
    assert persisted_result.node_id == "power-007"
    assert persisted_result.fault_type == "NODE_OVERLOAD"
    assert persisted_result.severity == "MEDIUM"
    assert persisted_result.detected is True


def test_data_corruption_calls_event_store_with_correct_fault_type_and_severity():
    payload = _telemetry("power", {"load": 50.0, "voltage": 650.0, "frequency": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        response = lambda_handler(payload, None)

    assert response["fault_type"] == "DATA_CORRUPTION"
    mock_store.put_event.assert_called_once()
    persisted_result = mock_store.put_event.call_args.args[0]
    assert persisted_result.fault_type == "DATA_CORRUPTION"
    assert persisted_result.severity == response["severity"]


def test_sensor_dropout_calls_event_store():
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0}, status="OFFLINE")

    with patch.object(handler_module, "_event_store") as mock_store:
        response = lambda_handler(payload, None)

    assert response["fault_type"] == "SENSOR_DROPOUT"
    mock_store.put_event.assert_called_once()
    persisted_result = mock_store.put_event.call_args.args[0]
    assert persisted_result.fault_type == "SENSOR_DROPOUT"


def test_invalid_input_without_node_id_does_not_call_event_store():
    # No node_id/timestamp to key the record on -> not persisted, even
    # though detected=True for INVALID_INPUT.
    payload = _telemetry("power", {"load": 50.0, "voltage": 230.0, "frequency": 50.0})
    del payload["node_id"]

    with patch.object(handler_module, "_event_store") as mock_store:
        response = lambda_handler(payload, None)

    assert response["fault_type"] == "INVALID_INPUT"
    mock_store.put_event.assert_not_called()


def test_invalid_input_with_node_id_and_timestamp_present_is_persisted():
    # Unsupported subsystem, but node_id/timestamp are both valid -> there
    # is something to key the record on, so it is persisted.
    payload = _telemetry("gas", {"pressure": 1.0}, node_id="node-042")

    with patch.object(handler_module, "_event_store") as mock_store:
        response = lambda_handler(payload, None)

    assert response["fault_type"] == "INVALID_INPUT"
    mock_store.put_event.assert_called_once()
    persisted_result = mock_store.put_event.call_args.args[0]
    assert persisted_result.node_id == "node-042"


def test_malformed_json_event_does_not_call_event_store():
    # No node_id/timestamp could ever be extracted from unparseable JSON.
    with patch.object(handler_module, "_event_store") as mock_store:
        response = lambda_handler("{not valid json", None)

    assert response["fault_type"] == "INVALID_INPUT"
    mock_store.put_event.assert_not_called()


def test_persistence_failure_propagates_and_is_logged(caplog):
    payload = _telemetry("power", {"load": 97.5, "voltage": 230.0, "frequency": 50.0})

    mock_store = MagicMock()
    mock_store.put_event.side_effect = AnomalyEventPersistenceError("simulated DynamoDB failure")

    with caplog.at_level(logging.INFO, logger="lambda_functions.anomaly_detector.handler"):
        with patch.object(handler_module, "_event_store", mock_store):
            with pytest.raises(AnomalyEventPersistenceError):
                lambda_handler(payload, None)

    # The detection-result log must still have been emitted before the
    # persistence attempt...
    assert any("SentinelGrid detection result" in r.message for r in caplog.records)
    # ...and the failure itself must be logged, not swallowed.
    assert any("persistence FAILED" in r.message for r in caplog.records)


def test_persistence_failure_does_not_silently_report_success(caplog):
    """The handler must not catch-and-continue on a persistence failure —
    there must be no path where it returns a normal-looking response after
    a failed write."""
    payload = _telemetry("power", {"load": 97.5, "voltage": 230.0, "frequency": 50.0})

    mock_store = MagicMock()
    mock_store.put_event.side_effect = AnomalyEventPersistenceError("simulated DynamoDB failure")

    response = None
    with caplog.at_level(logging.INFO, logger="lambda_functions.anomaly_detector.handler"):
        with patch.object(handler_module, "_event_store", mock_store):
            try:
                response = lambda_handler(payload, None)
            except AnomalyEventPersistenceError:
                pass

    assert response is None  # lambda_handler never returned a value


def test_detection_result_and_log_unchanged_by_persistence():
    """Detection, its return value, and its log line must be identical
    whether or not persistence happens — persistence runs strictly after."""
    payload = _telemetry("power", {"load": 97.5, "voltage": 230.0, "frequency": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        response_with_store = lambda_handler(payload, None)

    with patch.object(handler_module, "_event_store") as mock_store_2:
        mock_store_2.put_event.side_effect = AnomalyEventPersistenceError("boom")
        try:
            lambda_handler(payload, None)
        except AnomalyEventPersistenceError:
            pass

    # Both calls reached persistence with an equivalent result, proving
    # detection itself is unaffected by whether persistence later fails.
    result_a = mock_store.put_event.call_args.args[0]
    result_b = mock_store_2.put_event.call_args.args[0]
    assert result_a.to_dict() == result_b.to_dict() == response_with_store


# --- Accurate persisted-vs-duplicate logging (regression) ---
#
# A real deployed bug: a retried/duplicate invocation logged BOTH
# "already exists ... skipping duplicate write (idempotent)" (from
# event_store.py) AND "anomaly event persisted" (from handler.py) for the
# same event, even though no new DynamoDB write occurred on that call.
# Root cause: put_event() returned None for both a fresh write and a
# duplicate skip, so the handler's success branch couldn't tell them
# apart. Fixed by having put_event() return a PersistenceOutcome; these
# tests pin the corrected behavior at the handler level so it can't
# regress silently.


def test_handler_logs_persisted_only_for_newly_persisted_outcome(caplog):
    """Test case A: a fresh write (NEWLY_PERSISTED) logs the 'persisted'
    message, and only that message — this is the normal, first-write case."""
    payload = _telemetry("power", {"load": 97.5, "voltage": 230.0, "frequency": 50.0})

    mock_store = MagicMock()
    mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED

    with caplog.at_level(logging.INFO, logger="lambda_functions.anomaly_detector.handler"):
        with patch.object(handler_module, "_event_store", mock_store):
            response = lambda_handler(payload, None)

    assert response["detected"] is True
    mock_store.put_event.assert_called_once()

    persisted_logs = [r for r in caplog.records if "anomaly event persisted" in r.message]
    assert len(persisted_logs) == 1


def test_handler_does_not_log_persisted_for_duplicate_outcome(caplog):
    """Test case B: a duplicate (DUPLICATE_ALREADY_EXISTS) must NOT log
    'anomaly event persisted' — no exception escapes, and the operation is
    still treated as successful (the response is returned normally), but
    nothing claims a new write happened."""
    payload = _telemetry("power", {"load": 97.5, "voltage": 230.0, "frequency": 50.0})

    mock_store = MagicMock()
    mock_store.put_event.return_value = PersistenceOutcome.DUPLICATE_ALREADY_EXISTS

    with caplog.at_level(logging.INFO, logger="lambda_functions.anomaly_detector.handler"):
        with patch.object(handler_module, "_event_store", mock_store):
            response = lambda_handler(payload, None)  # must not raise

    assert response["detected"] is True
    assert response["fault_type"] == "NODE_OVERLOAD"
    mock_store.put_event.assert_called_once()

    persisted_logs = [r for r in caplog.records if "anomaly event persisted" in r.message]
    assert len(persisted_logs) == 0, (
        "Handler must not log 'anomaly event persisted' for a duplicate/idempotent "
        "skip — no new DynamoDB write occurred on this call."
    )


# ============================================================================
# Remediation trigger submission (Step 22)
# ============================================================================
#
# `_mock_trigger_client` (autouse, defined near the top of this file)
# already patches `_trigger_client` for every test in this module. These
# tests capture that same mock explicitly to assert on it.


def test_normal_telemetry_does_not_submit_remediation_trigger(_mock_trigger_client):
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        response = lambda_handler(payload, None)

    assert response["detected"] is False
    mock_store.put_event.assert_not_called()
    _mock_trigger_client.submit.assert_not_called()


def test_anomalous_telemetry_submits_trigger_exactly_once_after_persistence(_mock_trigger_client):
    payload = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        response = lambda_handler(payload, None)

    assert response["detected"] is True
    mock_store.put_event.assert_called_once()
    _mock_trigger_client.submit.assert_called_once()

    submitted_result = _mock_trigger_client.submit.call_args.args[0]
    assert submitted_result.to_dict() == response
    assert submitted_result.node_id == "node-001"
    assert submitted_result.fault_type == "NODE_OVERLOAD"


def test_trigger_submitted_for_duplicate_persistence_outcome_too(_mock_trigger_client):
    # A duplicate/idempotent-skip is still a SUCCESSFUL persistence
    # outcome (see event_store.py) — the trigger should still be
    # submitted. Step Functions' own StartExecution idempotency (the
    # deterministic execution name, see lambda_functions/remediation_trigger/)
    # is what absorbs any resulting duplicate downstream, not this layer.
    payload = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.DUPLICATE_ALREADY_EXISTS
        lambda_handler(payload, None)

    _mock_trigger_client.submit.assert_called_once()


def test_persistence_failure_prevents_trigger_submission_and_propagates(_mock_trigger_client):
    payload = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})
    mock_store = MagicMock()
    mock_store.put_event.side_effect = AnomalyEventPersistenceError("simulated DynamoDB failure")

    with patch.object(handler_module, "_event_store", mock_store):
        with pytest.raises(AnomalyEventPersistenceError):
            lambda_handler(payload, None)

    _mock_trigger_client.submit.assert_not_called()


def test_trigger_submission_failure_propagates_and_is_not_swallowed(_mock_trigger_client):
    payload = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})
    _mock_trigger_client.submit.side_effect = RemediationTriggerSubmissionError("simulated Lambda invoke failure")

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        with pytest.raises(RemediationTriggerSubmissionError):
            lambda_handler(payload, None)

    mock_store.put_event.assert_called_once()  # persistence still happened
    _mock_trigger_client.submit.assert_called_once()  # submission was attempted


def test_trigger_submission_failure_is_logged(_mock_trigger_client, caplog):
    payload = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})
    _mock_trigger_client.submit.side_effect = RemediationTriggerSubmissionError("simulated Lambda invoke failure")

    with caplog.at_level(logging.ERROR, logger="lambda_functions.anomaly_detector.handler"):
        with patch.object(handler_module, "_event_store") as mock_store:
            mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
            with pytest.raises(RemediationTriggerSubmissionError):
                lambda_handler(payload, None)

    error_logs = [r for r in caplog.records if "remediation trigger submission FAILED" in r.message]
    assert len(error_logs) == 1


def test_invalid_input_without_identity_does_not_submit_trigger(_mock_trigger_client):
    # No node_id/timestamp -> _should_persist is False -> nothing
    # persisted -> nothing to trigger, matching the same reasoning
    # documented for lambda_functions/remediation_trigger/trigger.py.
    with patch.object(handler_module, "_event_store") as mock_store:
        response = lambda_handler("not valid json{{{", None)

    assert response["fault_type"] == "INVALID_INPUT"
    mock_store.put_event.assert_not_called()
    _mock_trigger_client.submit.assert_not_called()


def test_invalid_input_with_identity_submits_trigger(_mock_trigger_client):
    payload = _telemetry("unsupported_subsystem", {"load": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        response = lambda_handler(payload, None)

    assert response["fault_type"] == "INVALID_INPUT"
    mock_store.put_event.assert_called_once()
    _mock_trigger_client.submit.assert_called_once()


def test_trigger_payload_is_the_exact_unwrapped_detection_result(_mock_trigger_client):
    payload = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        response = lambda_handler(payload, None)

    submitted_result = _mock_trigger_client.submit.call_args.args[0]
    submitted_dict = submitted_result.to_dict()
    assert submitted_dict == response
    assert "detection_result" not in submitted_dict
    assert "remediation_plan" not in submitted_dict
    assert "remediation_result" not in submitted_dict


# ============================================================================
# STEP 28 — METRICS INSTRUMENTATION
# ============================================================================

from datetime import datetime, timedelta, timezone  # noqa: E402

from shared.metrics import MetricName, MetricsClient  # noqa: E402


def test_fault_detection_count_published_for_anomalous_result(_mock_metrics_client):
    payload = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        lambda_handler(payload, None)

    _mock_metrics_client.put_count.assert_any_call(MetricName.FAULT_DETECTION_COUNT, "power", "NODE_OVERLOAD")


def test_fault_detection_count_not_published_for_normal_telemetry(_mock_metrics_client):
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})

    lambda_handler(payload, None)

    _mock_metrics_client.put_count.assert_not_called()


def test_detection_latency_published_with_correct_calculation(_mock_metrics_client):
    telemetry_time = datetime.now(timezone.utc) - timedelta(seconds=2)
    payload = _telemetry(
        "power",
        {"load": 97.8, "voltage": 230.0, "frequency": 50.0},
        timestamp=telemetry_time.isoformat(),
    )

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        lambda_handler(payload, None)

    assert _mock_metrics_client.put_milliseconds.call_count == 1
    call_args = _mock_metrics_client.put_milliseconds.call_args.args
    assert call_args[0] == MetricName.DETECTION_LATENCY
    assert call_args[1] == "power"
    assert call_args[2] == "NODE_OVERLOAD"
    published_latency_ms = call_args[3]
    assert 1500 <= published_latency_ms <= 10000  # ~2s, generous CI-safe bounds


def test_invalid_telemetry_timestamp_does_not_crash_detection(_mock_metrics_client):
    payload = _telemetry(
        "power",
        {"load": 97.8, "voltage": 230.0, "frequency": 50.0},
        timestamp="not-a-real-timestamp",
    )

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        response = lambda_handler(payload, None)  # must not raise

    assert response["detected"] is True
    assert response["fault_type"] == "NODE_OVERLOAD"
    # FaultDetectionCount still published -- only the latency calculation
    # is skipped for an unparseable timestamp.
    _mock_metrics_client.put_count.assert_any_call(MetricName.FAULT_DETECTION_COUNT, "power", "NODE_OVERLOAD")
    _mock_metrics_client.put_milliseconds.assert_not_called()


def test_future_telemetry_timestamp_is_handled_safely(_mock_metrics_client):
    future_time = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = _telemetry(
        "power",
        {"load": 97.8, "voltage": 230.0, "frequency": 50.0},
        timestamp=future_time.isoformat(),
    )

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        response = lambda_handler(payload, None)  # must not raise, must not crash

    assert response["detected"] is True
    _mock_metrics_client.put_milliseconds.assert_not_called()  # negative latency is never published


def test_cloudwatch_failure_does_not_break_detection_or_persistence():
    """Uses the REAL MetricsClient (not a test double) with an injected
    boto3-shaped client whose put_metric_data raises, to prove the full,
    real production code path — handler.py -> MetricsClient._put -> a
    failing CloudWatch call — never breaks detection, logging, or
    persistence. This is a stronger guarantee than mocking
    `_metrics_client` itself: it proves the actual error-handling wiring,
    not just that a test double was configured to behave.
    """
    failing_cloudwatch_client = MagicMock()
    failing_cloudwatch_client.put_metric_data.side_effect = RuntimeError("simulated CloudWatch outage")
    real_metrics_client = MetricsClient(client=failing_cloudwatch_client)

    payload = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})

    with patch.object(handler_module, "_metrics_client", real_metrics_client):
        with patch.object(handler_module, "_event_store") as mock_store:
            mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
            response = lambda_handler(payload, None)  # must not raise

    assert response["detected"] is True
    assert response["fault_type"] == "NODE_OVERLOAD"
    mock_store.put_event.assert_called_once()  # persistence still happened
    failing_cloudwatch_client.put_metric_data.assert_called()  # publication was attempted


# ============================================================================
# STEP 34 — CURRENT NODE STATE INTEGRATION (FR-6.1)
# ============================================================================

from botocore.exceptions import BotoCoreError  # noqa: E402

from shared.node_state_store import NodeState, NodeStateStore  # noqa: E402


def test_normal_telemetry_updates_node_state_to_healthy(_mock_node_state_store):
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0}, status="ONLINE")

    lambda_handler(payload, None)

    _mock_node_state_store.update_state.assert_called_once()
    kwargs = _mock_node_state_store.update_state.call_args.kwargs
    assert kwargs["state"] == NodeState.HEALTHY.value
    assert kwargs["status"] == "ONLINE"
    assert kwargs["subsystem"] == "power"
    assert kwargs["fault_type"] is None
    assert kwargs["fault_severity"] is None
    assert kwargs["node_id"] == "node-001"
    assert kwargs["event_timestamp"] == "2026-01-01T00:00:00+00:00"


def test_medium_overload_updates_node_state_to_fault_detected(_mock_node_state_store):
    payload = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0}, status="ONLINE")

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        lambda_handler(payload, None)

    kwargs = _mock_node_state_store.update_state.call_args.kwargs
    assert kwargs["state"] == NodeState.FAULT_DETECTED.value
    assert kwargs["fault_type"] == "NODE_OVERLOAD"
    assert kwargs["fault_severity"] == "MEDIUM"
    assert kwargs["status"] == "ONLINE"


def test_high_severity_data_corruption_updates_node_state_to_fault_detected(_mock_node_state_store):
    payload = _telemetry("power", {"load": 50.0, "voltage": 650.0, "frequency": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        lambda_handler(payload, None)

    kwargs = _mock_node_state_store.update_state.call_args.kwargs
    assert kwargs["state"] == NodeState.FAULT_DETECTED.value
    assert kwargs["fault_type"] == "DATA_CORRUPTION"
    assert kwargs["fault_severity"] == "HIGH"


def test_invalid_input_without_node_id_does_not_update_node_state(_mock_node_state_store):
    lambda_handler("{not valid json", None)

    _mock_node_state_store.update_state.assert_not_called()


def test_invalid_input_with_usable_identity_updates_node_state_as_fault_detected(_mock_node_state_store):
    payload = _telemetry("unsupported_subsystem", {"load": 50.0})

    with patch.object(handler_module, "_event_store") as mock_store:
        mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
        lambda_handler(payload, None)

    kwargs = _mock_node_state_store.update_state.call_args.kwargs
    assert kwargs["state"] == NodeState.FAULT_DETECTED.value
    assert kwargs["fault_type"] == "INVALID_INPUT"


def test_repeated_invocation_for_same_event_calls_update_state_each_time_but_store_handles_idempotency(
    _mock_node_state_store,
):
    """The Lambda always CALLS update_state (it doesn't know about
    dedup) -- idempotency/staleness protection is the store's own job
    (see tests/test_node_state_store.py), not the handler's."""
    payload = _telemetry("power", {"load": 55.0, "voltage": 230.0, "frequency": 50.0})

    lambda_handler(payload, None)
    lambda_handler(payload, None)

    assert _mock_node_state_store.update_state.call_count == 2


def test_node_state_dynamodb_failure_does_not_break_detection_or_persistence():
    """Uses the REAL NodeStateStore (not a test double) with an injected
    boto3-shaped client whose update_item raises, to prove the full,
    real production code path — handler.py -> NodeStateStore.update_state
    -> a failing DynamoDB call — never breaks detection, logging, or
    anomaly persistence. Mirrors
    test_cloudwatch_failure_does_not_break_detection_or_persistence
    (Step 28) applied to Step 34's own best-effort store.
    """
    failing_dynamodb_table = MagicMock()
    failing_dynamodb_table.update_item.side_effect = BotoCoreError()
    real_node_state_store = NodeStateStore(table=failing_dynamodb_table)

    payload = _telemetry("power", {"load": 97.8, "voltage": 230.0, "frequency": 50.0})

    with patch.object(handler_module, "_node_state_store", real_node_state_store):
        with patch.object(handler_module, "_event_store") as mock_store:
            mock_store.put_event.return_value = PersistenceOutcome.NEWLY_PERSISTED
            response = lambda_handler(payload, None)  # must not raise

    assert response["detected"] is True
    assert response["fault_type"] == "NODE_OVERLOAD"
    mock_store.put_event.assert_called_once()  # anomaly persistence still happened
    failing_dynamodb_table.update_item.assert_called()  # node-state update was attempted
