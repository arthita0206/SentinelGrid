"""Synchronously invokes the already-deployed SentinelGridAnomalyDetector
Lambda with a raw telemetry payload.

Kept as its own module, separate from handler.py, mirroring exactly how
lambda_functions/anomaly_detector/remediation_trigger_client.py is the
one module in ITS package that talks to AWS Lambda invoke — this is the
same pattern applied one hop earlier in the pipeline.

Step 26's architecture decision (see this package's README, "Why
Lambda-to-Lambda invoke" section): the SQS consumer does NOT import
anything from lambda_functions.anomaly_detector and does NOT reimplement
detection, persistence, or remediation-trigger submission. It treats the
deployed SentinelGridAnomalyDetector Lambda as an opaque, already-tested
service, invoked by ARN — exactly the same telemetry payload shape the
Analyzer already accepts when IoT invokes it directly. This keeps the
consumer's own deployment package free of any dependency on the
Analyzer's source tree, so the Step 22 packaging ripple-effect (any
package that imports lambda_functions.anomaly_detector must include ALL
of that package's eagerly-imported modules) simply does not apply here.

Unlike remediation_trigger_client.py's fire-and-forget asynchronous
(`InvocationType="Event"`) submission, this call is SYNCHRONOUS
(`InvocationType="RequestResponse"`): the SQS consumer must know whether
this specific message's processing succeeded or failed *before* it can
correctly populate SQS's partial-batch-failure response — an async
invoke would only confirm AWS accepted the event, never whether the
Analyzer's own detection/persistence/trigger logic actually succeeded.
"""

import json
import logging
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # see anomaly_detector/handler.py for why this is necessary on Lambda


class AnomalyDetectorInvocationError(RuntimeError):
    """Raised when the synchronous Analyzer invocation could not be
    completed successfully — either the Invoke API call itself failed,
    or AWS accepted the call but the Analyzer's own execution raised an
    unhandled exception (surfaced as `FunctionError` in the Invoke
    response, e.g. an AnomalyEventPersistenceError or
    RemediationTriggerSubmissionError inside the Analyzer). Both cases
    mean this one message was NOT successfully processed, and the caller
    (handler.py) is expected to report it via `batchItemFailures` rather
    than let SQS silently delete it.
    """


class AnomalyDetectorClient:
    """Synchronously invokes the SentinelGridAnomalyDetector Lambda.

    Accepts an optional pre-built Lambda client (`client=`) for
    dependency injection — tests pass a fake/mock here, so no real AWS
    call is ever made during the test suite, mirroring
    RemediationTriggerClient's own `client=` injection pattern.

    `function_arn` is resolved once, at construction time, from the
    caller (handler.py reads it from the
    ANOMALY_DETECTOR_FUNCTION_ARN environment variable) — matching how
    RemediationTriggerClient resolves its own target ARN once per
    execution environment and reuses it across warm invocations.
    """

    def __init__(
        self,
        function_arn: Optional[str] = None,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        client: Optional[Any] = None,
    ):
        self.function_arn = function_arn
        self.region = region
        self.profile = profile
        self._client = client

    def _get_client(self):
        if self._client is None:
            if self.profile:
                session = boto3.Session(profile_name=self.profile, region_name=self.region)
            else:
                session = boto3.Session(region_name=self.region)
            self._client = session.client("lambda", region_name=self.region)
        return self._client

    def invoke(self, payload: dict) -> dict:
        """Synchronously invokes the Analyzer with `payload` as its exact,
        unwrapped telemetry dict — never wrapped in an extra envelope,
        matching the Analyzer's own documented input contract (see
        lambda_functions/anomaly_detector/handler.py's `_extract_payload`).

        Returns the Analyzer's decoded JSON response payload (its
        `DetectionResult.to_dict()`) on success.

        Raises `AnomalyDetectorInvocationError` if `function_arn` is not
        configured, if the Invoke API call itself fails, or if the
        Analyzer's own execution raised an unhandled exception
        (`FunctionError` present in the response) — this method never
        silently treats either case as success.
        """
        if not self.function_arn:
            raise AnomalyDetectorInvocationError(
                "Anomaly detector function ARN is not configured; cannot invoke."
            )

        client = self._get_client()
        request_payload = json.dumps(payload).encode("utf-8")

        try:
            response = client.invoke(
                FunctionName=self.function_arn,
                InvocationType="RequestResponse",
                Payload=request_payload,
            )
        except (ClientError, BotoCoreError) as exc:
            raise AnomalyDetectorInvocationError(
                f"Failed to invoke the anomaly detector: {exc}"
            ) from exc

        response_payload_raw = response["Payload"].read()

        if response.get("FunctionError"):
            raise AnomalyDetectorInvocationError(
                f"Anomaly detector reported FunctionError={response['FunctionError']!r}: "
                f"{response_payload_raw!r}"
            )

        try:
            return json.loads(response_payload_raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AnomalyDetectorInvocationError(
                f"Anomaly detector returned a non-JSON response: {response_payload_raw!r}"
            ) from exc
