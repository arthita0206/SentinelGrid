"""Submits anomalous DetectionResults to the SentinelGridRemediationTrigger
Lambda, asynchronously.

Kept as its own module, separate from handler.py and detector.py, so the
detector stays completely AWS-independent (see detector.py's own
docstring) — this is the one place in the anomaly_detector package that
talks to AWS Lambda invoke, mirroring exactly how event_store.py is the
one place in this package that talks to DynamoDB.

The target Lambda (SentinelGridRemediationTrigger — see
infrastructure/remediation-trigger.yaml, Step 21) already owns
reconstructing a DetectionResult from the plain dict payload and
deciding whether to start the remediation Step Functions workflow (see
lambda_functions/remediation_trigger/). Nothing here duplicates that
decision — this module's only job is submitting the async invocation.
"""

import json
import logging
import os
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .detector import DetectionResult

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # see handler.py for why this is necessary on Lambda

TRIGGER_FUNCTION_ARN_ENV_VAR = "REMEDIATION_TRIGGER_FUNCTION_ARN"


class RemediationTriggerSubmissionError(RuntimeError):
    """Raised when the asynchronous Lambda Invoke API call itself fails —
    i.e. AWS never accepted/queued the event. This says nothing about
    whether the remediation trigger's own downstream processing (or the
    Step Functions execution it may start) later succeeds — by design,
    an `InvocationType="Event"` call is a fire-and-forget handoff once
    AWS accepts it, and this module has no visibility beyond that point.
    """


class RemediationTriggerClient:
    """Submits one DetectionResult to the remediation trigger Lambda via
    an asynchronous (`InvocationType="Event"`) invocation.

    Accepts an optional pre-built Lambda client (`client=`) for
    dependency injection — tests pass a fake/mock here, so no real AWS
    call is ever made during the test suite, mirroring
    `AnomalyEventStore`'s own `table=` injection pattern. Production code
    (see handler.py) lets it build its own client lazily on first use:
    constructing a `RemediationTriggerClient()` never touches AWS by
    itself.

    `function_arn` is resolved once, at construction time, from the
    `REMEDIATION_TRIGGER_FUNCTION_ARN` environment variable if not passed
    explicitly — matching how handler.py's module-level `_event_store`
    singleton is also built once per execution environment and reused
    across warm invocations.
    """

    def __init__(
        self,
        function_arn: Optional[str] = None,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        client: Optional[Any] = None,
    ):
        self.function_arn = function_arn or os.environ.get(TRIGGER_FUNCTION_ARN_ENV_VAR)
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

    def submit(self, result: DetectionResult) -> None:
        """Asynchronously invokes the remediation trigger Lambda with the
        exact, unwrapped `DetectionResult.to_dict()` as its JSON payload —
        never `{"detection_result": ...}`, never a remediation plan or
        result, matching the trigger Lambda's own documented input
        contract exactly (see lambda_functions/remediation_trigger/README.md).

        Raises `RemediationTriggerSubmissionError` if `function_arn` is
        not configured, or if the Invoke API call itself fails — this
        method never silently swallows a failed submission. Callers
        (handler.py) are expected to let this propagate, per this
        project's fail-fast convention (see event_store.py's own
        persistence-failure policy for the same reasoning applied
        elsewhere).
        """
        if not self.function_arn:
            raise RemediationTriggerSubmissionError(
                f"{TRIGGER_FUNCTION_ARN_ENV_VAR} is not configured; cannot submit remediation trigger."
            )

        client = self._get_client()
        payload = json.dumps(result.to_dict()).encode("utf-8")

        try:
            client.invoke(
                FunctionName=self.function_arn,
                InvocationType="Event",
                Payload=payload,
            )
        except (ClientError, BotoCoreError) as exc:
            raise RemediationTriggerSubmissionError(
                f"Failed to submit remediation trigger invocation for node '{result.node_id}' "
                f"at '{result.timestamp}'."
            ) from exc
