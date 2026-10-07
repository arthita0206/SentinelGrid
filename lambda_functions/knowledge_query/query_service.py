"""Read-only query service over the SentinelGrid persistent Knowledge Base
(Step 35 — SRS FR-6.3: "Provide a query interface for retrieving current
node state and fault/recovery history."; extended Step 40 with a
fleet-wide listing read).

This is the ONE module in `lambda_functions/knowledge_query/` that talks
to DynamoDB — `handler.py` (the HTTP/API Gateway adapter) never imports
boto3 itself, exactly mirroring how `event_store.py`/`recovery_event_store.py`/
`node_state_store.py` are each the sole AWS-facing module in their own
package. `KnowledgeQueryService` is fully testable without live AWS via
constructor-injected DynamoDB Table resources (`node_state_table=`,
`anomaly_events_table=`, `recovery_events_table=`), mirroring every other
store class in this project.

**Reads three EXISTING tables — creates, defines, and owns none of them**:
  - `SentinelGridNodeState`   (Step 34) — `GetItem` by `node_id`
    (`get_current_state`), or a bounded `Scan` across the fleet
    (`list_nodes`, Step 40 — see that method's own docstring for exactly
    why `Scan` is used here and how it stays safe/bounded).
  - `SentinelGridAnomalyEvents` (pre-Step-33) — Query only, by `node_id`,
    ordered/limited server-side via `ScanIndexForward`/`Limit`.
  - `SentinelGridRecoveryEvents` (Step 33) — Query only, by `node_id`,
    same ordering/limiting approach.

**Strictly read-only, by construction**: this module contains no call to
`put_item`, `update_item`, `delete_item`, or `batch_write_item` anywhere —
see `tests/test_knowledge_query_service.py`'s dedicated source-scan test,
which fails the build if one is ever added. There is deliberately no
write path here at all, not even a disabled one.

**No user-controlled DynamoDB expression TEXT**: `node_id`, and (Step 40)
`list_nodes`'s optional `subsystem`/`state` filter values, are the only
caller-supplied values that ever reach DynamoDB — always as
`ExpressionAttributeValues` placeholders, never interpolated into a
`KeyConditionExpression`/`ProjectionExpression`/`FilterExpression` string
itself. Every expression's TEXT is a fixed constant built by this module;
a caller can change which value is matched, never the query's shape or
inject arbitrary DynamoDB syntax.

**Ordering/limiting for the three per-node routes is done by DynamoDB
itself, not Python**: `Query`'s own `ScanIndexForward` (`False` for
descending/newest-first — the default, matching how a fault/recovery
timeline is normally read — `True` for ascending) and `Limit` parameters
are used directly; `get_anomaly_history`/`get_recovery_history` never
fetch more items than requested and re-sort/truncate them locally.
`list_nodes` (Step 40) is the one exception to "ordering is DynamoDB's
job" — see its own docstring for why a bounded, Python-side sort by
`node_id` is the only honest option for a table with no sort key.

**Failure semantics**: any DynamoDB failure (`ClientError`/`BotoCoreError`)
raises `KnowledgeQueryError` — never silently swallowed, never returned as
if it were an empty/not-found result. `handler.py` catches this and maps
it to a safe HTTP 500. A missing item (no current state row / no history
items) is NOT an error — it is a normal, expected outcome represented by
`None` (for `get_current_state`/`get_summary`) or an empty list (for the
history queries), so callers can distinguish "genuinely not found" from
"failed to look up" without inspecting exception details.
"""

import decimal
import logging
import os
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # see anomaly_detector/handler.py for why this is necessary on Lambda

NODE_STATE_TABLE_NAME_ENV_VAR = "NODE_STATE_TABLE_NAME"
ANOMALY_EVENTS_TABLE_NAME_ENV_VAR = "ANOMALY_EVENTS_TABLE_NAME"
RECOVERY_EVENTS_TABLE_NAME_ENV_VAR = "RECOVERY_EVENTS_TABLE_NAME"

DEFAULT_NODE_STATE_TABLE_NAME = "SentinelGridNodeState"
DEFAULT_ANOMALY_EVENTS_TABLE_NAME = "SentinelGridAnomalyEvents"
DEFAULT_RECOVERY_EVENTS_TABLE_NAME = "SentinelGridRecoveryEvents"

# Matches infrastructure/{node-state,anomaly-events,recovery-events}-storage.yaml's
# KeySchema exactly. The two history tables share the same (node_id,
# timestamp) key shape; the node-state table has node_id only.
_PARTITION_KEY = "node_id"
_SORT_KEY = "timestamp"

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Separate bounds for list_nodes() (Step 40, GET /nodes) — deliberately
# NOT the same constants as DEFAULT_LIMIT/MAX_LIMIT above, which remain
# exactly as Step 35 left them for the three existing per-node routes.
# A fleet listing is a different kind of query (up to the whole,
# currently-≤500-node fleet in one bounded read) with its own, larger,
# independently-justified ceiling — see list_nodes()'s docstring.
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500

# Bounds for list_events() (Step 42, GET /events) — the CLIENT-FACING
# limit on how many merged events are returned. Independent of both
# pairs above: a fleet-wide EVENT feed is neither a per-node history read
# nor a fleet-wide STATE snapshot, so it gets its own, separately
# justified ceiling per Step 42's own explicit spec (default 50, max
# 100 — deliberately smaller than MAX_LIST_LIMIT, since an "operations
# feed" of 100 recent events is already a lot to show in one table).
DEFAULT_EVENT_LIMIT = 50
MAX_EVENT_LIMIT = 100

# The INTERNAL (never client-controlled) cap on how many items are
# examined per underlying table (SentinelGridAnomalyEvents,
# SentinelGridRecoveryEvents) in one Scan call before merging/sorting/
# truncating to the requested `limit` (see list_events()'s own
# docstring for the full reasoning). Fixed at this project's own SRS
# NFR-3-derived precedent — the exact same 500 already used as
# MAX_LIST_LIMIT for list_nodes() — not exposed as a request parameter
# anywhere.
_EVENT_TABLE_SCAN_LIMIT = 500

EVENT_TYPE_ANOMALY = "anomaly"
EVENT_TYPE_RECOVERY = "recovery"


class KnowledgeQueryError(RuntimeError):
    """Raised for a genuine DynamoDB failure while serving a query.

    Never raised for "not found" — that is a normal, expected outcome
    represented by a `None`/empty return (see module docstring "Failure
    semantics"), not an error condition.
    """


def _to_native_json(value: Any) -> Any:
    """Recursively converts a DynamoDB item (as returned by boto3's
    resource-level `get_item`/`query`) into plain, `json.dumps`-safe
    Python types.

    The one real gotcha this exists to handle: boto3's resource interface
    returns every DynamoDB Number as a `decimal.Decimal`, which
    `json.dumps()` cannot serialize on its own — this is the write-side
    float->Decimal conversion (`event_store.py`/`node_state_store.py`)
    reversed for reads. A whole-valued Decimal becomes a plain `int`
    (e.g. a severity/status field is never numeric here, but a future
    numeric field should round-trip exactly); anything with a fractional
    part becomes a `float`. Dicts/lists are converted recursively so a
    nested structure (e.g. an anomaly event's `evidence` map) is safe too.
    """
    if isinstance(value, decimal.Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {key: _to_native_json(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_to_native_json(val) for val in value]
    return value


def _normalize_anomaly_event(item: dict) -> dict:
    """Maps one raw `SentinelGridAnomalyEvents` item onto the Step 42
    unified event shape (see `list_events()`'s docstring for the full
    field-by-field rationale). Every field the source table actually has
    is carried through UNCHANGED (never relabeled, never recomputed);
    every field only the OTHER event type has is explicit `None` — never
    fabricated, never omitted (a consistent, flat shape is easier for a
    frontend to render generically than a shape that varies key-by-key).
    """
    return {
        "event_type": EVENT_TYPE_ANOMALY,
        "node_id": item.get("node_id"),
        "timestamp": item.get("timestamp"),
        "subsystem": item.get("subsystem"),
        "fault_type": item.get("fault_type"),
        "severity": item.get("severity"),
        "reason": item.get("reason"),
        "detected": item.get("detected"),
        "evidence": item.get("evidence"),
        "action": None,
        "automatic": None,
        "status": None,
        "success": None,
        "recovery_started_at": None,
        "recorded_at": None,
    }


def _normalize_recovery_event(item: dict) -> dict:
    """Maps one raw `SentinelGridRecoveryEvents` item onto the Step 42
    unified event shape. `timestamp` is carried through EXACTLY as the
    source table stores it — that table's own `timestamp` attribute is
    the recovery event's primary/identity timestamp (its sort key), per
    the existing `RecoveryEvent` schema. `recovery_event_store.py`
    happens to set it equal to the originating detection's own
    timestamp (see that module's docstring) — this function does NOT
    relabel it as "detection time" anywhere, it is simply passed through
    under its own existing name, `timestamp`, exactly as Step 42
    requires. `recovery_started_at`/`recorded_at` are preserved as their
    own SEPARATE fields, never collapsed into `timestamp`.
    """
    return {
        "event_type": EVENT_TYPE_RECOVERY,
        "node_id": item.get("node_id"),
        "timestamp": item.get("timestamp"),
        "subsystem": item.get("subsystem"),
        "fault_type": item.get("fault_type"),
        "severity": item.get("severity"),
        "reason": item.get("reason"),
        "detected": None,
        "evidence": None,
        "action": item.get("action"),
        "automatic": item.get("automatic"),
        "status": item.get("status"),
        "success": item.get("success"),
        "recovery_started_at": item.get("recovery_started_at"),
        "recorded_at": item.get("recorded_at"),
    }


class KnowledgeQueryService:
    """Read-only reader over the three Knowledge Base tables.

    Accepts optional pre-built DynamoDB Table resources
    (`node_state_table=`/`anomaly_events_table=`/`recovery_events_table=`)
    for dependency injection — tests pass fakes/mocks here, so no real AWS
    call is ever made during the test suite, mirroring every other store
    class in this project (`AnomalyEventStore`, `RecoveryEventStore`,
    `NodeStateStore`). Each table name resolves independently: an explicit
    constructor argument, else its own environment variable, else its
    documented default — so the deployed Lambda's environment
    configuration (see infrastructure/knowledge-query.yaml) is the source
    of truth without hardcoding any table name in more than this one
    place.
    """

    def __init__(
        self,
        node_state_table_name: Optional[str] = None,
        anomaly_events_table_name: Optional[str] = None,
        recovery_events_table_name: Optional[str] = None,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        node_state_table: Optional[Any] = None,
        anomaly_events_table: Optional[Any] = None,
        recovery_events_table: Optional[Any] = None,
    ):
        self.node_state_table_name = node_state_table_name or os.environ.get(
            NODE_STATE_TABLE_NAME_ENV_VAR, DEFAULT_NODE_STATE_TABLE_NAME
        )
        self.anomaly_events_table_name = anomaly_events_table_name or os.environ.get(
            ANOMALY_EVENTS_TABLE_NAME_ENV_VAR, DEFAULT_ANOMALY_EVENTS_TABLE_NAME
        )
        self.recovery_events_table_name = recovery_events_table_name or os.environ.get(
            RECOVERY_EVENTS_TABLE_NAME_ENV_VAR, DEFAULT_RECOVERY_EVENTS_TABLE_NAME
        )
        self.region = region
        self.profile = profile
        self._node_state_table = node_state_table
        self._anomaly_events_table = anomaly_events_table
        self._recovery_events_table = recovery_events_table
        self._resource = None

    def _get_resource(self):
        # Lazy, shared across all three tables (one boto3 Session/resource
        # for this instance) — constructing a KnowledgeQueryService never
        # touches AWS by itself; only the first actual query does.
        if self._resource is None:
            if self.profile:
                session = boto3.Session(profile_name=self.profile, region_name=self.region)
            else:
                session = boto3.Session(region_name=self.region)
            self._resource = session.resource("dynamodb", region_name=self.region)
        return self._resource

    def _get_node_state_table(self):
        if self._node_state_table is None:
            self._node_state_table = self._get_resource().Table(self.node_state_table_name)
        return self._node_state_table

    def _get_anomaly_events_table(self):
        if self._anomaly_events_table is None:
            self._anomaly_events_table = self._get_resource().Table(self.anomaly_events_table_name)
        return self._anomaly_events_table

    def _get_recovery_events_table(self):
        if self._recovery_events_table is None:
            self._recovery_events_table = self._get_resource().Table(self.recovery_events_table_name)
        return self._recovery_events_table

    def get_current_state(self, node_id: str) -> Optional[dict]:
        """Returns the current-state item for `node_id` (SentinelGridNodeState),
        or `None` if no item exists for it. `node_id` is the table's sole
        partition key, so this is always a single `GetItem` — never a Scan.

        Raises `KnowledgeQueryError` for any DynamoDB failure.
        """
        table = self._get_node_state_table()
        try:
            response = table.get_item(Key={_PARTITION_KEY: node_id})
        except (ClientError, BotoCoreError) as exc:
            logger.error("SentinelGrid knowledge query: current-state GetItem failed for node_id=%s: %s", node_id, exc)
            raise KnowledgeQueryError(f"Failed to read current state for node '{node_id}'.") from exc

        item = response.get("Item")
        if item is None:
            return None
        return _to_native_json(item)

    def list_nodes(
        self,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        order: str = "desc",
        subsystem: Optional[str] = None,
        state: Optional[str] = None,
    ) -> list:
        """Returns up to `limit` current-state items from
        SentinelGridNodeState (Step 40, SRS FR-6.3's fleet-view gap —
        the four Step 35 routes only ever supported a lookup by a
        specific, already-known `node_id`).

        **Why Scan, and why that is safe here**: `SentinelGridNodeState`'s
        only key is `node_id` (see infrastructure/node-state-storage.yaml)
        — there is no secondary index a fleet-wide read could `Query`
        against, and creating one is explicitly out of scope for this
        change (a GSI is real, standing infrastructure and cost, not
        justified for a single bounded read pattern at this project's
        target scale). A `Scan` is therefore the only way to read "all
        current node state", and it is made safe the same way every other
        method in this class is safe against an unbounded read: `Limit`
        is ALWAYS passed (never optional, never caller-supplied beyond
        `MAX_LIST_LIMIT`), bounding this call to at most `MAX_LIST_LIMIT`
        (500) items examined — matching this project's own SRS NFR-3
        target fleet size, not an arbitrary number. This is the same
        "bounded Scan is acceptable at this project's scale" reasoning
        Step 37/38's own scale testing already relied on, applied here to
        a read instead of a test harness.

        **What `order` actually means — read this before assuming
        "desc" means "most recently updated"**: DynamoDB `Scan` does NOT
        return items in `node_id` order, timestamp order, or any other
        meaningful order — its result order is an implementation detail
        with no contract. There is no sort key on this table for a
        native, server-side ordering the way `_query_history` gets one
        "for free" from the two history tables' `(node_id, timestamp)`
        key. To give `order` an honest, deterministic meaning at all,
        this method sorts the ALREADY-FETCHED (bounded, ≤`limit`-item)
        page by `node_id` in Python after the Scan completes — safe and
        cheap precisely because it is bounded, never a full-table
        operation. `order='desc'`/`'asc'` therefore means "descending/
        ascending by `node_id`" — NOT "newest telemetry first" and NOT a
        guarantee about the full table beyond whatever this one bounded
        Scan happened to examine. Do not present this as recency
        ordering anywhere it is surfaced.

        **Filtering caveat (documented, not hidden)**: `subsystem`/`state`,
        when given, are applied as a DynamoDB `FilterExpression` — safe
        (both reach DynamoDB only as `ExpressionAttributeValues`, never
        interpolated into any expression string) but NOT complete at
        scale: `Limit` bounds items EXAMINED, and `FilterExpression` is
        applied only to that already-limited window, so a filtered call
        can return fewer than `limit` items — or none — even if more
        matching nodes exist beyond what this one Scan call examined.
        This is a genuine limitation of a single bounded Scan without a
        matching GSI, not a bug; a caller needing a guaranteed-complete
        filtered fleet view would need pagination across
        `LastEvaluatedKey` (deliberately not implemented here — out of
        scope for this change) or a GSI (deliberately not created here).

        Returns `[]` (never raises for this) when no items exist or none
        match. Raises `KnowledgeQueryError` for a genuine DynamoDB
        failure, exactly like every other method in this class.
        """
        table = self._get_node_state_table()

        scan_kwargs: dict = {"Limit": limit}
        names: dict = {}
        values: dict = {}
        filter_parts = []

        if subsystem is not None:
            filter_parts.append("#subsystem = :subsystem")
            names["#subsystem"] = "subsystem"
            values[":subsystem"] = subsystem
        if state is not None:
            filter_parts.append("#state = :state")
            names["#state"] = "state"
            values[":state"] = state

        if filter_parts:
            scan_kwargs["FilterExpression"] = " AND ".join(filter_parts)
            scan_kwargs["ExpressionAttributeNames"] = names
            scan_kwargs["ExpressionAttributeValues"] = values

        try:
            response = table.scan(**scan_kwargs)
        except (ClientError, BotoCoreError) as exc:
            logger.error("SentinelGrid knowledge query: node-state Scan failed: %s", exc)
            raise KnowledgeQueryError("Failed to list current node state.") from exc

        items = [_to_native_json(item) for item in response.get("Items", [])]
        items.sort(key=lambda item: item.get(_PARTITION_KEY, ""), reverse=(order == "desc"))
        return items

    def _query_history(self, table, node_id: str, limit: int, order: str) -> list:
        """Shared Query logic for the two append-only history tables
        (SentinelGridAnomalyEvents / SentinelGridRecoveryEvents), both
        keyed `(node_id HASH, timestamp RANGE)`. `order='asc'` sets
        `ScanIndexForward=True` (oldest first); `order='desc'` (the
        default) sets `ScanIndexForward=False` (newest first) — DynamoDB
        performs the ordering natively, using the sort key's own index;
        nothing is fetched then re-sorted in Python. `Limit` is passed
        straight through to DynamoDB, so at most `limit` items are ever
        read off the table for this call.
        """
        try:
            response = table.query(
                KeyConditionExpression="#pk = :node_id",
                ExpressionAttributeNames={"#pk": _PARTITION_KEY},
                ExpressionAttributeValues={":node_id": node_id},
                ScanIndexForward=(order == "asc"),
                Limit=limit,
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("SentinelGrid knowledge query: history Query failed for node_id=%s: %s", node_id, exc)
            raise KnowledgeQueryError(f"Failed to read history for node '{node_id}'.") from exc

        return [_to_native_json(item) for item in response.get("Items", [])]

    def get_anomaly_history(self, node_id: str, limit: int = DEFAULT_LIMIT, order: str = "desc") -> list:
        """Returns up to `limit` anomaly-event items for `node_id` from
        SentinelGridAnomalyEvents, ordered by `timestamp` per `order`
        ('asc'/'desc'). Always a `Query` — never a `Scan`. Returns `[]`
        (not an error) when the node has no anomaly history.
        """
        return self._query_history(self._get_anomaly_events_table(), node_id, limit, order)

    def get_recovery_history(self, node_id: str, limit: int = DEFAULT_LIMIT, order: str = "desc") -> list:
        """Returns up to `limit` recovery-event items for `node_id` from
        SentinelGridRecoveryEvents, ordered by `timestamp` per `order`
        ('asc'/'desc'). Always a `Query` — never a `Scan`. Returns `[]`
        (not an error) when the node has no recovery history.
        """
        return self._query_history(self._get_recovery_events_table(), node_id, limit, order)

    def get_summary(self, node_id: str, limit: int = DEFAULT_LIMIT, order: str = "desc") -> Optional[dict]:
        """Returns the combined operator-facing view: current state plus
        recent anomaly/recovery history, all for one `node_id`.

        Returns `None` (not a fabricated partial summary) if no current
        state exists for the node — per Step 35's explicit requirement,
        a node with no current-state row is reported as not-found rather
        than a summary built only from whatever history happens to exist.
        The two history queries are only issued once a current-state item
        is confirmed to exist.
        """
        current_state = self.get_current_state(node_id)
        if current_state is None:
            return None

        anomalies = self.get_anomaly_history(node_id, limit=limit, order=order)
        recoveries = self.get_recovery_history(node_id, limit=limit, order=order)
        return {
            "current_state": current_state,
            "anomalies": anomalies,
            "recoveries": recoveries,
        }

    def _scan_events_table(self, table, *, subsystem: Optional[str], severity: Optional[str], status: Optional[str]) -> list:
        """Bounded `Scan` of ONE events table (anomaly or recovery),
        optionally filtered. `Limit` is ALWAYS `_EVENT_TABLE_SCAN_LIMIT`
        — an internal constant, never a caller-supplied value — bounding
        this call to at most that many items EXAMINED, exactly like
        `list_nodes()`'s own Scan. `status` is only ever passed by
        `list_events()` for the recovery-events table (anomaly events
        have no `status` attribute at all — see that method's own
        docstring for why `status` is not even attempted there).
        """
        scan_kwargs: dict = {"Limit": _EVENT_TABLE_SCAN_LIMIT}
        names: dict = {}
        values: dict = {}
        filter_parts = []

        if subsystem is not None:
            filter_parts.append("#subsystem = :subsystem")
            names["#subsystem"] = "subsystem"
            values[":subsystem"] = subsystem
        if severity is not None:
            filter_parts.append("#severity = :severity")
            names["#severity"] = "severity"
            values[":severity"] = severity
        if status is not None:
            filter_parts.append("#status = :status")
            names["#status"] = "status"
            values[":status"] = status

        if filter_parts:
            scan_kwargs["FilterExpression"] = " AND ".join(filter_parts)
            scan_kwargs["ExpressionAttributeNames"] = names
            scan_kwargs["ExpressionAttributeValues"] = values

        try:
            response = table.scan(**scan_kwargs)
        except (ClientError, BotoCoreError) as exc:
            logger.error("SentinelGrid knowledge query: events Scan failed: %s", exc)
            raise KnowledgeQueryError("Failed to list events.") from exc

        return [_to_native_json(item) for item in response.get("Items", [])]

    def list_events(
        self,
        *,
        limit: int = DEFAULT_EVENT_LIMIT,
        order: str = "desc",
        event_type: Optional[str] = None,
        subsystem: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list:
        """Returns up to `limit` merged, normalized events (anomalies and/
        or recoveries) across the WHOLE fleet — the read `GET /nodes/{id}
        /anomalies`/`/recoveries` cannot provide, since both require an
        already-known `node_id` (Step 42, filling that gap without
        requiring one browser request per node).

        **Why `Scan`, and why that is safe here**: `SentinelGridAnomalyEvents`/
        `SentinelGridRecoveryEvents` are both keyed `(node_id HASH,
        timestamp RANGE)` — there is NO fleet-wide (cross-node) timestamp
        index either table could be `Query`-ed against; a `Query` only
        ever works within one already-known partition (`node_id`). A
        bounded `Scan` per table is therefore the only way to read
        "recent events across the fleet", made safe the same way
        `list_nodes()` is: `Limit` is ALWAYS `_EVENT_TABLE_SCAN_LIMIT`
        (500, an INTERNAL constant — never caller-supplied, never
        exposed as a request parameter), bounding each underlying Scan
        call to at most that many items examined.

        **This is a bounded, recent-ish feed — NOT a complete, correct,
        globally-chronological historical query.** Be precise about what
        this does and does not guarantee:
          - Each table is Scanned independently (up to
            `_EVENT_TABLE_SCAN_LIMIT` items each), the two result sets
            are merged, sorted in Python by each event's own `timestamp`
            (descending by default), then truncated to `limit`.
          - `Scan` does NOT guarantee it returns a table's newest items
            first, or at all, once a table holds more items than
            `_EVENT_TABLE_SCAN_LIMIT` — DynamoDB `Scan` order is an
            implementation detail with no chronological contract. At
            this project's CURRENT table sizes (order of tens of items —
            verified directly, well under `_EVENT_TABLE_SCAN_LIMIT`),
            every item in each table is examined every call, so the
            merge+sort below IS in practice globally correct today. If a
            table ever grows past `_EVENT_TABLE_SCAN_LIMIT` items, this
            method can no longer guarantee the single newest global
            event is always included — a real, disclosed limitation of
            a bounded Scan without a fleet-wide GSI (deliberately not
            created for this change), not a bug.
          - Never claim, anywhere this is surfaced, that it represents
            "the entire historical event database" — it is a bounded
            operations feed, sized for an operator's dashboard, not a
            complete audit export.

        **`order` sorts by each event's own `timestamp`** — for an
        anomaly event, its detection timestamp; for a recovery event,
        that table's own `timestamp` attribute (its sort key) — see
        `_normalize_recovery_event`'s docstring for why this is NOT
        relabeled as "detection time" despite the two often being equal
        by construction. `order='desc'` (default) is newest-timestamp-
        first among whatever was examined; `'asc'` is oldest-first.

        **`event_type`** ('anomaly'/'recovery'/`None` for both) selects
        which table(s) are scanned at all — 'anomaly' skips the recovery
        table's Scan entirely (and vice versa), so a caller who only
        wants one kind never pays for the other table's read.

        **`subsystem`/`severity`** are applied as a `FilterExpression` on
        each scanned table (both tables have these attributes) — same
        "Limit bounds items examined, filter applies only to that
        window" caveat as `list_nodes()`'s own filters (documented
        there; applies identically here).

        **`status`** exists ONLY on `SentinelGridRecoveryEvents` — it is
        therefore NEVER sent to the anomaly-events Scan at all (not even
        as a filter that would trivially match nothing — simply not
        applied there). `handler.py` rejects `status` combined with
        `event_type='anomaly'` outright (400) as a contradictory request,
        before this method is ever called with that combination.

        Returns `[]` (never raises for this) when nothing matches.
        Raises `KnowledgeQueryError` for a genuine DynamoDB failure.
        """
        events: list = []

        if event_type in (None, EVENT_TYPE_ANOMALY):
            events.extend(
                _normalize_anomaly_event(item)
                for item in self._scan_events_table(
                    self._get_anomaly_events_table(), subsystem=subsystem, severity=severity, status=None
                )
            )

        if event_type in (None, EVENT_TYPE_RECOVERY):
            events.extend(
                _normalize_recovery_event(item)
                for item in self._scan_events_table(
                    self._get_recovery_events_table(), subsystem=subsystem, severity=severity, status=status
                )
            )

        events.sort(key=lambda event: event.get("timestamp") or "", reverse=(order == "desc"))
        return events[:limit]
