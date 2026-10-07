"""AWS Lambda adapter exposing the read-only Knowledge Base query
interface over an API Gateway HTTP API (Step 35 — SRS FR-6.3; extended
Step 40 with a fleet-listing route and CORS; extended Step 42 with a
fleet-wide event feed; extended Step 43 with a read-only CloudWatch
metrics feed).

    API Gateway HTTP API (payload format 2.0) event
        -> lambda_handler
        -> route dispatch on event["routeKey"]
        -> validate node_id / limit / order / subsystem / state / event_type /
           severity / status / start / end / fault_type
        -> KnowledgeQueryService.* / MetricsQueryService.get_metrics
        -> {"statusCode": ..., "headers": ..., "body": json string}

This module contains NO DynamoDB logic of its own — all persistence
access lives in `query_service.py` (mirroring how `anomaly_detector/handler.py`
delegates all DynamoDB access to `event_store.py`). It contains NO
CloudWatch logic of its own either — that lives in `metrics_service.py`
(Step 43). This file itself never imports boto3.

**Routes** (exactly these seven, all GET, matching
infrastructure/knowledge-query.yaml's `AWS::ApiGatewayV2::Route` resources
by their exact `RouteKey`):
  - `GET /nodes`                       -> fleet listing (Step 40)
  - `GET /events`                      -> fleet-wide event feed (Step 42)
  - `GET /metrics`                     -> CloudWatch metrics feed (Step 43)
  - `GET /nodes/{node_id}`             -> current state
  - `GET /nodes/{node_id}/anomalies`   -> anomaly history
  - `GET /nodes/{node_id}/recoveries`  -> recovery history
  - `GET /nodes/{node_id}/summary`     -> combined view

There is no POST/PUT/PATCH/DELETE route anywhere in this API — the HTTP
API itself only has these seven GET routes defined, so an unsupported
method never reaches this Lambda at all (API Gateway returns its own 404
before invoking anything, since there is no matching route and no
catch-all `$default` route). The `_ROUTE_*` handling below is therefore a
closed, fixed set; an unrecognized `routeKey` (which should not happen in
production, but could in a malformed manually-crafted test event) is
handled defensively as HTTP 400, never as a 500 or a silent pass-through.

**`GET /nodes`, `GET /events`, and `GET /metrics` have no `node_id` path
parameter** — all three are dispatched BEFORE this handler's node_id
extraction/validation step, unlike the other four routes, all of which
require one.

**Errors**: every response is `{"error": "<CODE>", "message": "<safe text>"}`
with an appropriate HTTP status — 400 for a bad node_id/limit/order/
subsystem/state/event_type/severity/status/start/end/fault_type (or a
contradictory filter combination — see `GET /events`'s own section
below), 404 for "no current state exists for this node" (per-node routes
only), 500 for a genuine DynamoDB failure (`KnowledgeQueryError`), a
genuine CloudWatch failure (`MetricsQueryError`), or any other unexpected
exception. No response body ever includes a stack trace, a boto3
exception's raw text, an AWS request ID, or any table name — full
details are logged to CloudWatch (via `logger.exception`/`logger.error`)
but never returned to the caller. There is no HTTP 503 here: this Lambda
has exactly two dependency classes (DynamoDB reads, CloudWatch metric
reads) and no currently-implemented way to distinguish a "transient
dependency outage" from any other failure in either, so introducing a
503 now would be an unverified distinction — see infrastructure/README.md's
Step 35 section.

**`GET /metrics` (Step 43)**: exposes the seven approved SentinelGrid
CloudWatch metrics (`shared/metrics.py`'s own fixed contract) through
this same read-only API — NEVER an arbitrary namespace or metric name
(see `metrics_service.py`'s own extensive docstring for the full
allowlist/dimension-combination/period design). `start`/`end` (ISO-8601,
optional, default last 1 hour, max 24-hour window) and optional
`subsystem`/`fault_type` filters are the only accepted parameters.

**CORS (Step 40)**: now enabled, at the API Gateway HTTP API level (see
infrastructure/knowledge-query.yaml's `KnowledgeQueryApi.CorsConfiguration`)
— NOT in this Lambda's own response headers. HTTP APIs handle CORS
(including the preflight `OPTIONS` request) entirely within API Gateway
itself when `CorsConfiguration` is set; this Lambda is never invoked for
a preflight request and does not need to, and does not, add any
`Access-Control-*` header of its own. Allowed origin is an explicit,
configurable value (the `FrontendOrigin` template parameter — defaults to
`http://localhost:5173` for local frontend development), never `"*"`.
Only `GET` is allowed — there is no other method to allow, since this API
has none. See infrastructure/README.md's Step 40 section for the exact
configuration and reasoning.

**Still unauthenticated, by design**: enabling CORS makes this API
reachable from a browser tab; it does not add any authentication or
authorization. This is an explicit, documented, deliberate limitation
carried over unchanged from Step 35 — see this package's README
"Security / authentication limitation" section. The interface remains
strictly read-only, which bounds the impact.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .metrics_service import (
    DEFAULT_WINDOW_SECONDS,
    MAX_WINDOW_SECONDS,
    NAMESPACE as METRICS_NAMESPACE,
    MetricsQueryError,
    MetricsQueryService,
)
from .query_service import (
    DEFAULT_EVENT_LIMIT,
    DEFAULT_LIMIT,
    DEFAULT_LIST_LIMIT,
    MAX_EVENT_LIMIT,
    MAX_LIMIT,
    MAX_LIST_LIMIT,
    KnowledgeQueryError,
    KnowledgeQueryService,
)

logger = logging.getLogger(__name__)
# See anomaly_detector/handler.py for why this is necessary on Lambda.
logger.setLevel(logging.INFO)

_MAX_NODE_ID_LENGTH = 128
_MIN_LIMIT = 1

# Independent copies of the backend's own closed vocabularies (Step 40) —
# NOT imported from lambda_functions.anomaly_detector.detector /
# shared.node_state_store, matching this package's established
# self-containment convention (see this package's README "Purpose": no
# dependency on the detection/remediation pipeline's own code). Kept in
# sync BY HAND with detector.py's VALID_SUBSYSTEMS and
# shared/node_state_store.py's NodeState enum — both are small, stable,
# already-closed sets.
_VALID_SUBSYSTEMS = ("power", "water", "traffic")
_VALID_NODE_STATES = ("HEALTHY", "FAULT_DETECTED", "RECOVERING", "RECOVERED", "ESCALATION_REQUIRED", "UNKNOWN")

# Step 42 additions to this package's own independent vocabulary copies
# (see the comment above — kept in sync BY HAND with
# lambda_functions/anomaly_detector/detector.py's `Severity` enum and
# lambda_functions/remediation/recovery_event_store.py's `RecoveryStatus`
# enum, for the same self-containment reason).
_VALID_EVENT_TYPES = ("anomaly", "recovery")
_VALID_SEVERITIES = ("NORMAL", "LOW", "MEDIUM", "HIGH", "CRITICAL")
_VALID_RECOVERY_STATUSES = ("SUCCESS", "FAILURE", "ESCALATION_REQUIRED")

# Step 43 addition to this package's own independent vocabulary copies
# (see the comment above) — kept in sync BY HAND with
# lambda_functions/anomaly_detector/detector.py's `FaultType` enum,
# EXCLUDING `NONE`: metrics are only ever published (shared/metrics.py's
# `MetricsClient._put`) for a real fault, never for `NONE`, so `NONE` is
# not a value `GET /metrics`'s `fault_type` filter could ever usefully
# match and is excluded from this closed vocabulary.
_VALID_FAULT_TYPES = ("SENSOR_DROPOUT", "NETWORK_PARTITION", "DATA_CORRUPTION", "NODE_OVERLOAD", "INVALID_INPUT")

# How far a caller-supplied `end` may exceed "now" before being rejected
# as a future timestamp — a small allowance for ordinary clock skew
# between the browser/client and this Lambda's own clock, not a loophole
# for a genuinely future window.
_FUTURE_END_TOLERANCE_SECONDS = 60

_ROUTE_LIST_NODES = "GET /nodes"
_ROUTE_LIST_EVENTS = "GET /events"
_ROUTE_METRICS = "GET /metrics"
_ROUTE_CURRENT_STATE = "GET /nodes/{node_id}"
_ROUTE_ANOMALIES = "GET /nodes/{node_id}/anomalies"
_ROUTE_RECOVERIES = "GET /nodes/{node_id}/recoveries"
_ROUTE_SUMMARY = "GET /nodes/{node_id}/summary"

# Built once per execution environment (Lambda reuses this across warm
# invocations), exactly like every other module-level store/client
# singleton in this project (_event_store, _node_state_store, ...).
# Constructing it never touches AWS — see query_service.py/metrics_service.py.
_query_service = KnowledgeQueryService()
_metrics_service = MetricsQueryService()


class _ApiError(Exception):
    """Internal control-flow exception carrying an HTTP status code, a
    stable machine-readable error code, and a safe, user-facing message.
    Never escapes `lambda_handler` — always caught and turned into a JSON
    error response.
    """

    def __init__(self, status_code: int, error_code: str, message: str):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(message)


def _validate_node_id(raw: Any) -> str:
    """`node_id` must be present, a string, non-empty after trimming
    surrounding whitespace, and no longer than `_MAX_NODE_ID_LENGTH`
    characters. The trimmed value is what is actually used for every
    downstream query. This is the only shape-validation applied — no
    character-set restriction is needed, since `node_id` only ever
    reaches DynamoDB as an `ExpressionAttributeValues` placeholder (see
    query_service.py), never interpolated into an expression string, so
    it cannot be used to inject DynamoDB syntax regardless of content.
    """
    if not isinstance(raw, str):
        raise _ApiError(400, "INVALID_NODE_ID", "node_id must be a non-empty string.")
    node_id = raw.strip()
    if not node_id:
        raise _ApiError(400, "INVALID_NODE_ID", "node_id must not be empty.")
    if len(node_id) > _MAX_NODE_ID_LENGTH:
        raise _ApiError(400, "INVALID_NODE_ID", f"node_id must not exceed {_MAX_NODE_ID_LENGTH} characters.")
    return node_id


def _validate_limit(raw: Optional[str], *, default: int, max_limit: int) -> int:
    """`limit` is optional (defaults to `default`); when present it must
    parse as an integer between `_MIN_LIMIT` and `max_limit` inclusive.
    Rejects non-numeric strings, floats-as-strings (e.g. "20.5"), and
    out-of-range values — this is the only place bounding how many items
    a single request can read, so it is intentionally strict rather than
    clamping silently.

    `default`/`max_limit` are caller-supplied (not module constants
    referenced directly) so this one function serves both the original
    three per-node history routes (`DEFAULT_LIMIT`/`MAX_LIMIT`, unchanged
    since Step 35) and the Step 40 fleet-listing route
    (`DEFAULT_LIST_LIMIT`/`MAX_LIST_LIMIT`) without duplicating this
    validation logic or silently sharing one route's bounds with another.
    """
    if raw is None:
        return default
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        raise _ApiError(400, "INVALID_LIMIT", "limit must be an integer.")
    if limit < _MIN_LIMIT or limit > max_limit:
        raise _ApiError(400, "INVALID_LIMIT", f"limit must be between {_MIN_LIMIT} and {max_limit}.")
    return limit


def _validate_order(raw: Optional[str]) -> str:
    """`order` is optional (defaults to 'desc'); when present it must be
    exactly 'asc' or 'desc'."""
    if raw is None:
        return "desc"
    if raw not in ("asc", "desc"):
        raise _ApiError(400, "INVALID_ORDER", "order must be 'asc' or 'desc'.")
    return raw


def _validate_optional_choice(raw: Optional[str], *, valid_values: tuple, error_code: str, field_name: str) -> Optional[str]:
    """Shared validator for the Step 40 `GET /nodes` optional
    `subsystem`/`state` filter query parameters: `None` (absent) passes
    through unchanged (no filter applied); any non-empty value must be
    an exact, case-sensitive match to one of `valid_values` — this
    project's own closed vocabularies, not an arbitrary string reaching
    DynamoDB (see query_service.py's own "No user-controlled DynamoDB
    expression TEXT" reasoning: this validation is a belt-and-suspenders
    usability check, not the only thing standing between a request and
    DynamoDB, but it does mean an obviously-wrong filter value is
    rejected with a clear 400 rather than silently matching zero items).
    """
    if raw is None:
        return None
    if raw not in valid_values:
        raise _ApiError(400, error_code, f"{field_name} must be one of {list(valid_values)}.")
    return raw


def _validate_subsystem(raw: Optional[str]) -> Optional[str]:
    return _validate_optional_choice(raw, valid_values=_VALID_SUBSYSTEMS, error_code="INVALID_SUBSYSTEM", field_name="subsystem")


def _validate_node_state(raw: Optional[str]) -> Optional[str]:
    return _validate_optional_choice(raw, valid_values=_VALID_NODE_STATES, error_code="INVALID_STATE", field_name="state")


def _validate_event_type(raw: Optional[str]) -> Optional[str]:
    return _validate_optional_choice(raw, valid_values=_VALID_EVENT_TYPES, error_code="INVALID_EVENT_TYPE", field_name="event_type")


def _validate_severity(raw: Optional[str]) -> Optional[str]:
    return _validate_optional_choice(raw, valid_values=_VALID_SEVERITIES, error_code="INVALID_SEVERITY", field_name="severity")


def _validate_recovery_status(raw: Optional[str]) -> Optional[str]:
    return _validate_optional_choice(raw, valid_values=_VALID_RECOVERY_STATUSES, error_code="INVALID_STATUS", field_name="status")


def _validate_fault_type(raw: Optional[str]) -> Optional[str]:
    return _validate_optional_choice(raw, valid_values=_VALID_FAULT_TYPES, error_code="INVALID_FAULT_TYPE", field_name="fault_type")


def _parse_iso8601(raw: str, *, error_code: str, field_name: str) -> datetime:
    """Parses a required ISO-8601 timestamp string (Step 43, `GET
    /metrics`'s `start`/`end`). Accepts a trailing 'Z' (not handled by
    `datetime.fromisoformat` on the Python versions this project targets)
    by normalizing it to '+00:00' first. A naive (no offset) timestamp is
    assumed UTC, matching `shared/metrics.compute_elapsed_ms`'s own
    handling of naive telemetry timestamps elsewhere in this project.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise _ApiError(400, error_code, f"{field_name} must be a non-empty ISO-8601 timestamp string.")
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise _ApiError(400, error_code, f"{field_name} is not a valid ISO-8601 timestamp.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _validate_time_range(start_raw: Optional[str], end_raw: Optional[str]) -> tuple:
    """Resolves and validates `GET /metrics`'s `start`/`end` query
    parameters into a `(start, end)` pair of timezone-aware `datetime`s.

    - Both absent: defaults to the last `DEFAULT_WINDOW_SECONDS` (1 hour)
      ending now.
    - Only one given: the other is derived (`end` defaults to now;
      `start` defaults to `end - DEFAULT_WINDOW_SECONDS`) rather than
      requiring both-or-neither.
    - Each, when given, must be a valid ISO-8601 timestamp
      (`400 INVALID_START`/`400 INVALID_END` otherwise).
    - `end` must be after `start` (`400 INVALID_TIME_RANGE`).
    - The resolved window must not exceed `MAX_WINDOW_SECONDS` (24 hours)
      (`400 TIME_RANGE_TOO_LARGE`).
    - `end` must not be in the future beyond a small clock-skew tolerance
      (`400 FUTURE_END_TIMESTAMP`) — a bounded metrics query has no
      legitimate reason to ask CloudWatch about a window that has not
      happened yet.
    """
    now = datetime.now(timezone.utc)

    if start_raw is None and end_raw is None:
        end = now
        start = end - timedelta(seconds=DEFAULT_WINDOW_SECONDS)
        return start, end

    end = _parse_iso8601(end_raw, error_code="INVALID_END", field_name="end") if end_raw is not None else now
    start = (
        _parse_iso8601(start_raw, error_code="INVALID_START", field_name="start")
        if start_raw is not None
        else end - timedelta(seconds=DEFAULT_WINDOW_SECONDS)
    )

    if end <= start:
        raise _ApiError(400, "INVALID_TIME_RANGE", "end must be after start.")
    if (end - start).total_seconds() > MAX_WINDOW_SECONDS:
        raise _ApiError(400, "TIME_RANGE_TOO_LARGE", f"the time range must not exceed {MAX_WINDOW_SECONDS} seconds (24 hours).")
    if end > now + timedelta(seconds=_FUTURE_END_TOLERANCE_SECONDS):
        raise _ApiError(400, "FUTURE_END_TIMESTAMP", "end must not be in the future.")

    return start, end


def _response(status_code: int, body: dict) -> dict:
    """Builds an API Gateway HTTP API (payload format 2.0) Lambda-proxy
    response. No `Access-Control-*` header is added here — CORS (Step 40)
    is handled entirely by API Gateway's own `CorsConfiguration`, not by
    this Lambda — see module docstring. `sort_keys=True` only for
    deterministic, easily-diffable output; it has no semantic meaning to
    a JSON consumer.
    """
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, sort_keys=True),
    }


def _error_response(status_code: int, error_code: str, message: str) -> dict:
    return _response(status_code, {"error": error_code, "message": message})


def lambda_handler(event: Any, context: Any = None) -> dict:
    """AWS Lambda entry point for the SentinelGridKnowledgeQuery function.

    Never raises: every code path below — validation failure, not-found,
    a `KnowledgeQueryError` from a real DynamoDB failure, or any other
    unexpected exception — is caught and converted into a structured JSON
    error response with an appropriate HTTP status code, so API Gateway
    always receives a valid Lambda-proxy response rather than reporting
    an opaque 502 for an unhandled Lambda exception.
    """
    route_key = event.get("routeKey") if isinstance(event, dict) else None
    path_params = (event.get("pathParameters") or {}) if isinstance(event, dict) else {}
    query_params = (event.get("queryStringParameters") or {}) if isinstance(event, dict) else {}

    try:
        if route_key == _ROUTE_LIST_NODES:
            # The one route with no {node_id} path parameter -- dispatched
            # before _validate_node_id, which every other route needs.
            limit = _validate_limit(query_params.get("limit"), default=DEFAULT_LIST_LIMIT, max_limit=MAX_LIST_LIMIT)
            order = _validate_order(query_params.get("order"))
            subsystem = _validate_subsystem(query_params.get("subsystem"))
            state_filter = _validate_node_state(query_params.get("state"))
            nodes = _query_service.list_nodes(limit=limit, order=order, subsystem=subsystem, state=state_filter)
            body = {"nodes": nodes, "count": len(nodes), "limit": limit, "order": order}
            if subsystem is not None:
                body["subsystem"] = subsystem
            if state_filter is not None:
                body["state"] = state_filter
            return _response(200, body)

        if route_key == _ROUTE_LIST_EVENTS:
            # Also has no {node_id} path parameter -- dispatched before
            # _validate_node_id, same reasoning as GET /nodes above.
            limit = _validate_limit(query_params.get("limit"), default=DEFAULT_EVENT_LIMIT, max_limit=MAX_EVENT_LIMIT)
            order = _validate_order(query_params.get("order"))
            event_type = _validate_event_type(query_params.get("event_type"))
            subsystem = _validate_subsystem(query_params.get("subsystem"))
            severity = _validate_severity(query_params.get("severity"))
            status = _validate_recovery_status(query_params.get("status"))

            if status is not None and event_type == "anomaly":
                # `status` exists only on SentinelGridRecoveryEvents --
                # anomaly events have no status field at all, so this
                # combination can never match anything. Rejected as a
                # contradictory request rather than silently returning
                # an empty result (see query_service.py's list_events()
                # docstring "status" section).
                raise _ApiError(
                    400,
                    "INVALID_STATUS",
                    "status only applies to recovery events; remove event_type=anomaly or drop the status filter.",
                )

            events = _query_service.list_events(
                limit=limit, order=order, event_type=event_type, subsystem=subsystem, severity=severity, status=status
            )
            body = {"events": events, "count": len(events), "limit": limit, "order": order}
            if event_type is not None:
                body["event_type"] = event_type
            if subsystem is not None:
                body["subsystem"] = subsystem
            if severity is not None:
                body["severity"] = severity
            if status is not None:
                body["status"] = status
            return _response(200, body)

        if route_key == _ROUTE_METRICS:
            # Also has no {node_id} path parameter -- dispatched before
            # _validate_node_id, same reasoning as GET /nodes/GET /events
            # above.
            start, end = _validate_time_range(query_params.get("start"), query_params.get("end"))
            subsystem = _validate_subsystem(query_params.get("subsystem"))
            fault_type = _validate_fault_type(query_params.get("fault_type"))

            result = _metrics_service.get_metrics(start=start, end=end, subsystem=subsystem, fault_type=fault_type)
            body = {
                "namespace": METRICS_NAMESPACE,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "period_seconds": result["period_seconds"],
                "series": result["series"],
            }
            if subsystem is not None:
                body["subsystem"] = subsystem
            if fault_type is not None:
                body["fault_type"] = fault_type
            return _response(200, body)

        node_id = _validate_node_id(path_params.get("node_id"))

        if route_key == _ROUTE_CURRENT_STATE:
            state = _query_service.get_current_state(node_id)
            if state is None:
                raise _ApiError(404, "NODE_NOT_FOUND", "No current state exists for the requested node.")
            return _response(200, {"node": state})

        if route_key == _ROUTE_ANOMALIES:
            limit = _validate_limit(query_params.get("limit"), default=DEFAULT_LIMIT, max_limit=MAX_LIMIT)
            order = _validate_order(query_params.get("order"))
            items = _query_service.get_anomaly_history(node_id, limit=limit, order=order)
            return _response(200, {"node_id": node_id, "count": len(items), "items": items})

        if route_key == _ROUTE_RECOVERIES:
            limit = _validate_limit(query_params.get("limit"), default=DEFAULT_LIMIT, max_limit=MAX_LIMIT)
            order = _validate_order(query_params.get("order"))
            items = _query_service.get_recovery_history(node_id, limit=limit, order=order)
            return _response(200, {"node_id": node_id, "count": len(items), "items": items})

        if route_key == _ROUTE_SUMMARY:
            limit = _validate_limit(query_params.get("limit"), default=DEFAULT_LIMIT, max_limit=MAX_LIMIT)
            order = _validate_order(query_params.get("order"))
            summary = _query_service.get_summary(node_id, limit=limit, order=order)
            if summary is None:
                raise _ApiError(404, "NODE_NOT_FOUND", "No current state exists for the requested node.")
            return _response(
                200,
                {
                    "node_id": node_id,
                    "current_state": summary["current_state"],
                    "anomalies": {"count": len(summary["anomalies"]), "items": summary["anomalies"]},
                    "recoveries": {"count": len(summary["recoveries"]), "items": summary["recoveries"]},
                },
            )

        # Defensive only — API Gateway never forwards a request that
        # doesn't match one of the six routes above (see module
        # docstring); this guards a malformed/manually-crafted test event.
        logger.error("SentinelGrid knowledge query: unrecognized routeKey=%r", route_key)
        raise _ApiError(400, "UNSUPPORTED_ROUTE", "Unsupported request route.")

    except _ApiError as exc:
        logger.info("SentinelGrid knowledge query rejected [%s]: %s", exc.error_code, exc.message)
        return _error_response(exc.status_code, exc.error_code, exc.message)
    except KnowledgeQueryError:
        logger.exception("SentinelGrid knowledge query: DynamoDB read failed")
        return _error_response(500, "INTERNAL_ERROR", "An internal error occurred while processing the request.")
    except MetricsQueryError:
        logger.exception("SentinelGrid knowledge query: CloudWatch metrics read failed")
        return _error_response(500, "INTERNAL_ERROR", "An internal error occurred while processing the request.")
    except Exception:
        logger.exception("SentinelGrid knowledge query: unexpected internal failure")
        return _error_response(500, "INTERNAL_ERROR", "An internal error occurred while processing the request.")
