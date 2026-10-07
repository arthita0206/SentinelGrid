"""AWS Lambda adapter that bridges SQS-buffered telemetry to the existing
Analyzer (SentinelGridAnomalyDetector Lambda).

    SQS event -> per-record: parse body JSON -> synchronous invoke of the
    already-deployed Analyzer -> per-record success/failure
        -> {"batchItemFailures": [...]} (SQS partial-batch-failure report)

This module owns ALL AWS-SQS-event-shape handling (Records/messageId/body
parsing, batchItemFailures) — see infrastructure/sqs-consumer.yaml's
EventSourceMapping (FunctionResponseTypes: [ReportBatchItemFailures]),
which is what makes AWS Lambda honor a partial response at all. It
contains NO detection, persistence, or remediation-trigger logic of its
own: that is entirely the Analyzer's responsibility, invoked via
`AnomalyDetectorClient` (see anomaly_detector_client.py and this
package's README for why this is a Lambda-to-Lambda invoke rather than a
second, embedded copy of the Analyzer's code).

This function never deletes or otherwise manages SQS messages directly —
the AWS Lambda event source mapping polls, and (based on this function's
returned `batchItemFailures`) deletes successfully processed messages and
leaves failed ones for redelivery/eventual DLQ routing. This module's
only job is to report, per message, whether it succeeded.
"""

import json
import logging
import os
from typing import Any, Optional

from .anomaly_detector_client import AnomalyDetectorClient, AnomalyDetectorInvocationError

logger = logging.getLogger(__name__)
# See lambda_functions/anomaly_detector/handler.py for why this is
# necessary on Lambda: the Python runtime's root logger defaults to
# WARNING, which would otherwise silently drop every logger.info() call
# below.
logger.setLevel(logging.INFO)

ANOMALY_DETECTOR_FUNCTION_ARN_ENV_VAR = "ANOMALY_DETECTOR_FUNCTION_ARN"

# Built once per execution environment (Lambda reuses this across warm
# invocations), mirroring exactly how anomaly_detector/handler.py builds
# its own module-level `_trigger_client` singleton. Constructing it does
# not touch AWS — the ARN is read from the environment once here, and the
# underlying boto3 client is built lazily on first actual invoke() call.
_analyzer_client = AnomalyDetectorClient(function_arn=os.environ.get(ANOMALY_DETECTOR_FUNCTION_ARN_ENV_VAR))


class InvalidSqsMessageBodyError(ValueError):
    """Raised when an SQS record's `body` is not valid JSON, or parses to
    something other than a JSON object (e.g. a bare string, number, or
    array). Caught within `_process_record` — never propagates out of
    `lambda_handler` — and always results in that one message being
    reported via `batchItemFailures`.
    """


def _parse_body(body: str) -> dict:
    """Parses one SQS record's `body` as a telemetry-shaped JSON object.

    Raises `InvalidSqsMessageBodyError` for malformed JSON or a JSON
    value that isn't an object (dict) — telemetry-schema-specific
    validation (node_id/subsystem/timestamp/status/data) is deliberately
    NOT duplicated here: the Analyzer itself already performs that via
    `detect_telemetry()`, returning a structured INVALID_INPUT result for
    a malformed-but-valid-JSON-object payload. This function's only job
    is confirming there is a JSON object to hand off at all.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InvalidSqsMessageBodyError(f"body is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise InvalidSqsMessageBodyError(
            f"body must be a JSON object, got {type(payload).__name__}"
        )
    return payload


def _process_record(record: dict) -> Optional[str]:
    """Processes one SQS record. Returns `None` on success, or the
    record's `messageId` (str) if it should be reported as a batch item
    failure.

    Never raises: every anticipated failure mode (malformed body,
    Analyzer invocation failure) is caught here and converted into a
    returned messageId, so one bad record can never abort processing of
    the rest of the batch.
    """
    message_id = record.get("messageId")

    try:
        payload = _parse_body(record.get("body", ""))
    except InvalidSqsMessageBodyError as exc:
        logger.error("SentinelGrid SQS consumer: messageId=%s invalid message body: %s", message_id, exc)
        return message_id

    node_id = payload.get("node_id")
    subsystem = payload.get("subsystem")

    try:
        _analyzer_client.invoke(payload)
    except AnomalyDetectorInvocationError as exc:
        logger.error(
            "SentinelGrid SQS consumer: messageId=%s node_id=%s subsystem=%s anomaly detector "
            "invocation FAILED: %s",
            message_id,
            node_id,
            subsystem,
            exc,
        )
        return message_id

    logger.info(
        "SentinelGrid SQS consumer: messageId=%s node_id=%s subsystem=%s processed successfully",
        message_id,
        node_id,
        subsystem,
    )
    return None


def lambda_handler(event: Any, context: Any = None) -> dict:
    """AWS Lambda entry point for the SQS -> Analyzer bridge.

    `event` is the standard AWS Lambda SQS event shape:
    `{"Records": [{"messageId": ..., "body": "...", ...}, ...]}`. Each
    record is processed independently via `_process_record`; a failure
    on one record never prevents the rest of the batch from being
    attempted.

    Returns `{"batchItemFailures": [{"itemIdentifier": messageId}, ...]}`
    — only for records that failed. An empty `Records` list, or a batch
    where every record succeeded, returns `{"batchItemFailures": []}`.
    This exact shape is what AWS Lambda's SQS partial-batch-response
    feature (FunctionResponseTypes: ReportBatchItemFailures — see
    infrastructure/sqs-consumer.yaml) requires to delete only the
    successfully processed messages and leave failed ones for redelivery.
    """
    records = event.get("Records", []) if isinstance(event, dict) else []

    batch_item_failures = []
    for record in records:
        failed_message_id = _process_record(record)
        if failed_message_id is not None:
            batch_item_failures.append({"itemIdentifier": failed_message_id})

    return {"batchItemFailures": batch_item_failures}
