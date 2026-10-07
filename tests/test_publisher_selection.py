"""Unit tests for simulator.__main__.build_publisher (Step 4).

Constructing an AwsIotPublisher never touches the network (it connects
lazily on first publish), so these tests can exercise aws_iot/both mode
selection without any AWS credentials or mocking boto3.
"""

import pytest

from simulator.__main__ import build_publisher
from simulator.publisher import LocalPublisher, MultiPublisher
from simulator.aws_publisher import AwsIotPublisher


def _config(mode: str) -> dict:
    return {
        "publisher": {"mode": mode},
        "aws": {
            "region": "ap-south-1",
            "profile": "sentinelgrid",
            "iot": {"topic": "sentinelgrid/telemetry", "endpoint_url": None},
        },
    }


def test_default_mode_is_local_when_publisher_section_missing():
    publisher = build_publisher({})
    assert isinstance(publisher, LocalPublisher)


def test_local_mode_returns_local_publisher():
    publisher = build_publisher(_config("local"))
    assert isinstance(publisher, LocalPublisher)


def test_aws_iot_mode_returns_aws_iot_publisher_without_network_calls():
    publisher = build_publisher(_config("aws_iot"))
    assert isinstance(publisher, AwsIotPublisher)
    assert publisher.region == "ap-south-1"
    assert publisher.topic == "sentinelgrid/telemetry"
    assert publisher.profile == "sentinelgrid"


def test_both_mode_returns_multi_publisher_with_local_and_aws():
    publisher = build_publisher(_config("both"))
    assert isinstance(publisher, MultiPublisher)
    assert any(isinstance(p, LocalPublisher) for p in publisher._publishers)
    assert any(isinstance(p, AwsIotPublisher) for p in publisher._publishers)


def test_unknown_mode_raises_clear_error():
    with pytest.raises(ValueError):
        build_publisher(_config("carrier-pigeon"))
