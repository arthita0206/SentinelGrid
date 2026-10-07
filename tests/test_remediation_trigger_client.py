"""Unit tests for the anomaly-detector-side remediation trigger client
(lambda_functions/anomaly_detector/remediation_trigger_client.py).

All Lambda invoke calls are mocked via a fake client object injected at
construction time. No real AWS calls are made anywhere in this file.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from lambda_functions.anomaly_detector.detector import DetectionResult
from lambda_functions.anomaly_detector.remediation_trigger_client import (
    TRIGGER_FUNCTION_ARN_ENV_VAR,
    RemediationTriggerClient,
    RemediationTriggerSubmissionError,
)

_FAKE_ARN = "arn:aws:lambda:ap-south-1:401429382000:function:SentinelGridRemediationTrigger"


def _result(**overrides) -> DetectionResult:
    defaults = dict(
        detected=True,
        fault_type="NODE_OVERLOAD",
        severity="MEDIUM",
        node_id="power-007",
        subsystem="power",
        timestamp="2026-01-01T00:05:00+00:00",
        reason="Field 'load' value 97.8 is within the overload band (95.0, 100.0).",
        evidence={"field": "load", "value": 97.8},
    )
    defaults.update(overrides)
    return DetectionResult(**defaults)


# --- Submission: correct call shape ---


def test_submit_invokes_with_event_invocation_type_and_exact_unwrapped_payload():
    fake_client = MagicMock()
    store = RemediationTriggerClient(function_arn=_FAKE_ARN, client=fake_client)

    result = _result()
    store.submit(result)

    fake_client.invoke.assert_called_once()
    kwargs = fake_client.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == _FAKE_ARN
    assert kwargs["InvocationType"] == "Event"
    assert json.loads(kwargs["Payload"]) == result.to_dict()
    assert "detection_result" not in json.loads(kwargs["Payload"])  # never wrapped


# --- Missing ARN ---


def test_submit_raises_when_function_arn_not_configured():
    fake_client = MagicMock()
    store = RemediationTriggerClient(function_arn=None, client=fake_client)

    with pytest.raises(RemediationTriggerSubmissionError):
        store.submit(_result())

    fake_client.invoke.assert_not_called()


def test_constructor_reads_arn_from_environment_when_not_passed(monkeypatch):
    monkeypatch.setenv(TRIGGER_FUNCTION_ARN_ENV_VAR, _FAKE_ARN)
    store = RemediationTriggerClient()
    assert store.function_arn == _FAKE_ARN


def test_constructor_prefers_explicit_arn_over_environment(monkeypatch):
    monkeypatch.setenv(TRIGGER_FUNCTION_ARN_ENV_VAR, "arn:aws:lambda:ap-south-1:401429382000:function:Wrong")
    store = RemediationTriggerClient(function_arn=_FAKE_ARN)
    assert store.function_arn == _FAKE_ARN


# --- Failure behavior ---


def test_submit_raises_submission_error_on_client_error():
    fake_client = MagicMock()
    fake_client.invoke.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}, "Invoke"
    )
    store = RemediationTriggerClient(function_arn=_FAKE_ARN, client=fake_client)

    with pytest.raises(RemediationTriggerSubmissionError):
        store.submit(_result())


def test_submit_raises_submission_error_on_botocore_error():
    fake_client = MagicMock()
    fake_client.invoke.side_effect = BotoCoreError()
    store = RemediationTriggerClient(function_arn=_FAKE_ARN, client=fake_client)

    with pytest.raises(RemediationTriggerSubmissionError):
        store.submit(_result())


def test_submission_error_message_does_not_leak_credentials():
    fake_client = MagicMock()
    fake_client.invoke.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "Invoke"
    )
    store = RemediationTriggerClient(function_arn=_FAKE_ARN, client=fake_client)

    with pytest.raises(RemediationTriggerSubmissionError) as exc_info:
        store.submit(_result())

    message = str(exc_info.value).lower()
    assert "secret" not in message
    assert "access_key" not in message


# --- No real AWS calls / lazy construction ---


def test_no_boto3_session_created_when_client_is_injected():
    fake_client = MagicMock()
    store = RemediationTriggerClient(function_arn=_FAKE_ARN, client=fake_client)

    with patch("lambda_functions.anomaly_detector.remediation_trigger_client.boto3.Session") as mock_session_cls:
        store.submit(_result())
        mock_session_cls.assert_not_called()


def test_constructing_client_does_not_touch_aws():
    with patch("lambda_functions.anomaly_detector.remediation_trigger_client.boto3.Session") as mock_session_cls:
        RemediationTriggerClient(function_arn=_FAKE_ARN)  # no client injected, no submit() called
        mock_session_cls.assert_not_called()


def test_lazy_client_uses_profile_and_region_when_configured():
    with patch("lambda_functions.anomaly_detector.remediation_trigger_client.boto3.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        store = RemediationTriggerClient(function_arn=_FAKE_ARN, region="ap-south-1", profile="sentinelgrid")
        store.submit(_result())

        mock_session_cls.assert_called_once_with(profile_name="sentinelgrid", region_name="ap-south-1")
        mock_session.client.assert_called_once_with("lambda", region_name="ap-south-1")


def test_lazy_client_omits_profile_when_not_configured():
    with patch("lambda_functions.anomaly_detector.remediation_trigger_client.boto3.Session") as mock_session_cls:
        mock_session_cls.return_value = MagicMock()

        store = RemediationTriggerClient(function_arn=_FAKE_ARN, region="ap-south-1")
        store.submit(_result())

        mock_session_cls.assert_called_once_with(region_name="ap-south-1")
