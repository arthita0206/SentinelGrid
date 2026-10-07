"""Unit tests for AwsIotPublisher (Step 4).

All boto3/botocore interactions are mocked. No real AWS calls are made —
these tests must be able to run with no AWS credentials, no network
access, and no AWS account configured.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from shared.telemetry import Telemetry
from simulator.aws_publisher import AwsIotPublisher, AwsIotPublishError

TOPIC = "sentinelgrid/telemetry"
REGION = "ap-south-1"


def _make_telemetry() -> Telemetry:
    return Telemetry(
        node_id="power-001",
        subsystem="power",
        timestamp="2026-01-01T00:00:00+00:00",
        status="ONLINE",
        data={"load": 42.0, "voltage": 230.0, "frequency": 50.0},
    )


def test_requires_region_and_topic():
    with pytest.raises(ValueError):
        AwsIotPublisher(region="", topic=TOPIC)
    with pytest.raises(ValueError):
        AwsIotPublisher(region=REGION, topic="")


def test_publish_sends_correct_topic_and_json_payload():
    fake_client = MagicMock()
    publisher = AwsIotPublisher(region=REGION, topic=TOPIC, iot_data_client=fake_client)
    telemetry = _make_telemetry()

    publisher.publish(telemetry)

    fake_client.publish.assert_called_once()
    _, kwargs = fake_client.publish.call_args
    assert kwargs["topic"] == TOPIC
    assert kwargs["qos"] == 0
    assert json.loads(kwargs["payload"].decode("utf-8")) == telemetry.to_dict()


def test_no_real_boto3_session_created_when_client_is_injected():
    fake_client = MagicMock()
    publisher = AwsIotPublisher(region=REGION, topic=TOPIC, iot_data_client=fake_client)

    with patch("simulator.aws_publisher.boto3.Session") as mock_session_cls:
        publisher.publish(_make_telemetry())
        mock_session_cls.assert_not_called()


def test_uses_configured_profile_and_region_when_building_session():
    with patch("simulator.aws_publisher.boto3.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        publisher = AwsIotPublisher(
            region=REGION,
            topic=TOPIC,
            profile="sentinelgrid",
            endpoint_url="https://example-ats.iot.ap-south-1.amazonaws.com",
        )
        publisher.publish(_make_telemetry())

        mock_session_cls.assert_called_once_with(profile_name="sentinelgrid", region_name=REGION)
        mock_session.client.assert_called_once_with(
            "iot-data",
            region_name=REGION,
            endpoint_url="https://example-ats.iot.ap-south-1.amazonaws.com",
        )


def test_omits_profile_when_not_configured():
    with patch("simulator.aws_publisher.boto3.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        publisher = AwsIotPublisher(
            region=REGION,
            topic=TOPIC,
            endpoint_url="https://example-ats.iot.ap-south-1.amazonaws.com",
        )
        publisher.publish(_make_telemetry())

        mock_session_cls.assert_called_once_with(region_name=REGION)


def test_discovers_endpoint_when_not_explicitly_configured():
    fake_iot_client = MagicMock()
    fake_iot_client.describe_endpoint.return_value = {
        "endpointAddress": "abc123-ats.iot.ap-south-1.amazonaws.com"
    }
    fake_iot_data_client = MagicMock()

    fake_session = MagicMock()
    fake_session.client.side_effect = lambda service_name, **kwargs: (
        fake_iot_client if service_name == "iot" else fake_iot_data_client
    )

    publisher = AwsIotPublisher(region=REGION, topic=TOPIC, session=fake_session)
    publisher.publish(_make_telemetry())

    fake_iot_client.describe_endpoint.assert_called_once_with(endpointType="iot:Data-ATS")
    fake_session.client.assert_any_call(
        "iot-data",
        region_name=REGION,
        endpoint_url="https://abc123-ats.iot.ap-south-1.amazonaws.com",
    )
    fake_iot_data_client.publish.assert_called_once()


def test_client_error_during_publish_raises_clear_error_without_leaking_details():
    fake_client = MagicMock()
    fake_client.publish.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "Publish"
    )
    publisher = AwsIotPublisher(region=REGION, topic=TOPIC, iot_data_client=fake_client)

    with pytest.raises(AwsIotPublishError) as exc_info:
        publisher.publish(_make_telemetry())

    message = str(exc_info.value)
    assert "power-001" in message
    assert TOPIC in message
    assert "secret" not in message.lower()
    assert "access_key" not in message.lower()
    assert "token" not in message.lower()


def test_endpoint_discovery_error_raises_clear_error():
    fake_iot_client = MagicMock()
    fake_iot_client.describe_endpoint.side_effect = ClientError(
        {"Error": {"Code": "UnauthorizedException", "Message": "denied"}}, "DescribeEndpoint"
    )
    fake_session = MagicMock()
    fake_session.client.return_value = fake_iot_client

    publisher = AwsIotPublisher(region=REGION, topic=TOPIC, session=fake_session)

    with pytest.raises(AwsIotPublishError):
        publisher.publish(_make_telemetry())
