"""AWS IoT Core publisher: the AWS-backed implementation of the Publisher
interface.

This module is only imported when publisher.mode is aws_iot/both (see
simulator/__main__.py), so running the simulator in local mode never
requires boto3 to be importable or an AWS profile to be configured.

    Publisher interface
        |-- LocalPublisher (simulator/publisher.py)
        |-- AwsIotPublisher (this file) -> AWS IoT Core -> IoT Rules Engine (future step)

No AWS resources are created by this module — it only publishes MQTT
messages to a topic that is assumed to already exist (or that IoT Core
will simply receive with no subscriber, which is harmless).
"""

from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared.telemetry import Telemetry
from simulator.publisher import Publisher


class AwsIotPublishError(RuntimeError):
    """Raised when a telemetry reading could not be published to AWS IoT Core."""


class AwsIotPublisher(Publisher):
    """Publishes telemetry as JSON to an AWS IoT Core topic via the IoT Data Plane.

    Credentials are never read or stored by this class — they are resolved
    by boto3's normal credential chain: the given `profile` (an AWS CLI
    profile name) if configured, otherwise environment variables, the
    default profile, or an instance/role, exactly as boto3 would resolve
    them for any other script.
    """

    def __init__(
        self,
        region: str,
        topic: str,
        profile: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        session: Optional[boto3.Session] = None,
        iot_data_client=None,
    ):
        if not region:
            raise ValueError("AwsIotPublisher requires an AWS region")
        if not topic:
            raise ValueError("AwsIotPublisher requires an MQTT topic")

        self.region = region
        self.topic = topic
        self.profile = profile
        self._endpoint_url = endpoint_url
        self._session = session
        # Allows tests (and advanced callers) to inject a fake/pre-built
        # client directly, bypassing session creation and endpoint discovery.
        self._iot_data_client = iot_data_client

    def _get_session(self) -> boto3.Session:
        if self._session is None:
            if self.profile:
                self._session = boto3.Session(profile_name=self.profile, region_name=self.region)
            else:
                self._session = boto3.Session(region_name=self.region)
        return self._session

    def _discover_endpoint(self, session: boto3.Session) -> str:
        try:
            iot_client = session.client("iot", region_name=self.region)
            response = iot_client.describe_endpoint(endpointType="iot:Data-ATS")
        except (BotoCoreError, ClientError) as exc:
            raise AwsIotPublishError(
                "Failed to discover the AWS IoT Core data endpoint. Check that "
                "the configured AWS region/profile are correct and that the "
                "credentials have iot:DescribeEndpoint permission."
            ) from exc
        return f"https://{response['endpointAddress']}"

    def _get_client(self):
        if self._iot_data_client is not None:
            return self._iot_data_client

        session = self._get_session()
        endpoint_url = self._endpoint_url or self._discover_endpoint(session)

        try:
            self._iot_data_client = session.client(
                "iot-data", region_name=self.region, endpoint_url=endpoint_url
            )
        except (BotoCoreError, ClientError) as exc:
            raise AwsIotPublishError(
                f"Failed to create an AWS IoT Data Plane client in region '{self.region}'."
            ) from exc

        return self._iot_data_client

    def publish(self, telemetry: Telemetry) -> None:
        payload = telemetry.to_json().encode("utf-8")

        try:
            client = self._get_client()
            client.publish(topic=self.topic, qos=0, payload=payload)
        except AwsIotPublishError:
            raise
        except (BotoCoreError, ClientError) as exc:
            raise AwsIotPublishError(
                f"Failed to publish telemetry for node '{telemetry.node_id}' to "
                f"AWS IoT Core topic '{self.topic}' in region '{self.region}'."
            ) from exc
