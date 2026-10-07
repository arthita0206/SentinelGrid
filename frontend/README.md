# SentinelGrid Frontend (Step 39)

A local, buildable operations-dashboard UI for SentinelGrid — the
foundation for the project's eventual main operations dashboard. Built
with **React + TypeScript + Vite + Tailwind CSS**.

**This frontend is not deployed anywhere.** It runs locally only
(`npm run dev`). No AWS resource (S3, CloudFront, Cognito, API Gateway
change, etc.) was created or modified to build it.

## Quick start

```bash
cd frontend
npm install
cp .env.example .env.local   # then edit if needed
npm run dev
```

Open the printed local URL (typically `http://localhost:5173`).

## Available scripts

| Script | Purpose |
|---|---|
| `npm run dev` | Local dev server with hot reload. |
| `npm run build` | Type-checks (`tsc -b`) then produces a production build in `dist/`. |
| `npm run preview` | Serves the production build locally, for a final local check. |
| `npm run test` | Runs the Vitest suite once (used in CI-style checks). |
| `npm run test:watch` | Runs Vitest in watch mode. |
| `npm run lint` | Runs `oxlint` over the source tree. |

## Directory structure

```
frontend/
├── src/
│   ├── components/
│   │   ├── layout/        # AppShell, Sidebar, Header
│   │   ├── common/        # StatusBadge, MetricCard, NodeTable, EventTable, ...
│   │   ├── architecture/  # ArchitectureDiagram, MapeKDiagram
│   │   ├── analytics/     # MetricChartCard
│   │   ├── dashboard/     # CityVisualization
│   │   └── icons.tsx      # Small original SVG icon set (no external/brand assets)
│   ├── pages/              # One file per route (see "Routes" below)
│   ├── services/
│   │   └── api.ts          # The ONLY module that talks to the Knowledge Query API
│   ├── hooks/               # useNodes/useEvents/useMetrics/useNodeSummary — all
│   │                        # live-only, no mock fallback, honest errors (Step 44)
│   ├── types/
│   │   ├── api.ts           # Types matching the API's exact response shapes
│   │   └── domain.ts        # Shared backend vocabulary (NodeState, Severity, ...)
│   ├── data/                 # DEMO/mock data only — see "Mock vs. live data" below
│   ├── lib/                   # utils.ts (formatting), status.ts (tone mapping),
│   │                          # dashboard.ts (Step 41 — pure derivation functions)
│   ├── test/                   # Vitest + React Testing Library tests
│   ├── App.tsx                  # Route definitions
│   ├── main.tsx                  # Entry point
│   └── index.css                  # Tailwind import + design tokens
├── .env.example
└── package.json
```

## Routes

| Path | Page | Notes |
|---|---|---|
| `/` | — | Redirects to `/dashboard`. |
| `/dashboard` | `DashboardPage` | Fleet summary, subsystem cards, simulated city view. **Live as of Step 41** (see below) — resilience metrics and recent events remain honest placeholders. |
| `/nodes` | `NodesPage` | Node table with subsystem/state filters, search, sort. **Live as of Step 40** (see below). |
| `/nodes/:nodeId` | `NodeDetailPage` | Node identity, current state, fault, anomaly/recovery history. **Live, no demo-data fallback as of Step 44** (see below) — a distinct "Node not found" state for a missing node. |
| `/events` | `EventsPage` | Unified anomaly/recovery/escalation table with filters. **Live as of Step 42** (see below) — a bounded, recent feed, not complete history. |
| `/analytics` | `AnalyticsPage` | Charts for the 7 backend CloudWatch metrics. **Live as of Step 43** (see below) — a bounded window (max 24h), never a computed SLA/accuracy figure. |
| `/architecture` | `ArchitecturePage` | Pipeline diagram + MAPE-K explainer. Static, factual content (no live/demo data distinction needed). |
| `*` | `NotFoundPage` | 404 fallback. |

## API base URL configuration

Set `VITE_API_BASE_URL` in a local `.env.local` (never committed — see
`.env.example`):

```
VITE_API_BASE_URL=https://m1uqr1hsq3.execute-api.ap-south-1.amazonaws.com
```

This is the deployed `SentinelGridKnowledgeQuery` API (Step 35, extended
Steps 40, 42, and 43) — see `../lambda_functions/knowledge_query/README.md`
for its full route documentation. No credentials are used or required;
the API is unauthenticated by design (a documented backend limitation,
not something this frontend works around).

**CORS (Step 40)**: the deployed API now allows browser calls from
`http://localhost:5173` specifically (API Gateway's `FrontendOrigin`
parameter — see `../infrastructure/knowledge-query.yaml`). Running the
dev server on a different port/host will be blocked by the browser until
the deployed API is redeployed with a matching `FrontendOrigin` — this
frontend does not work around that (no proxy, no wildcard origin).

## Which backend API this consumes

The seven existing, read-only Knowledge Query routes — nothing else:

- `GET /nodes` — fleet listing (Step 40)
- `GET /events` — fleet-wide anomaly/recovery event feed (Step 42)
- `GET /metrics` — CloudWatch metrics feed (Step 43)
- `GET /nodes/{node_id}`
- `GET /nodes/{node_id}/anomalies`
- `GET /nodes/{node_id}/recoveries`
- `GET /nodes/{node_id}/summary`

`src/services/api.ts` is the one module with this knowledge — typed
functions (`getNodes`, `getEvents`, `getMetrics`, `getNode`,
`getNodeAnomalies`, `getNodeRecoveries`, `getNodeSummary`) wrap `fetch`
calls to these exact paths, with TypeScript types in `src/types/api.ts`
matching the backend's documented response shapes byte-for-byte.

## Mock/demo data vs. live data — read this before assuming a number is real

**As of Step 40, the Knowledge Query API has a fleet-listing endpoint
(`GET /nodes`) and CORS is enabled — this changed what can be live here.**
Before Step 40, no route could return more than one node's data at a
time; that limitation is now resolved for the Nodes page specifically.

- **`/nodes` is now LIVE** (Step 40) — `useNodes` (`src/hooks/useNodes.ts`)
  calls `getNodes({ limit: 500, order: 'asc' })` directly; the existing
  subsystem/state/search/sort filtering still happens client-side, over
  whatever the live call returned, exactly as it did over demo data
  before. **This hook has NO demo-data fallback**: a successful call
  shows a green **"Live"** tag and the real table; a failed call (network
  error, wrong origin, backend error) shows an honest `ErrorState` with a
  retry button — it never silently substitutes demo data for a real
  failure. `limit=500` matches SRS NFR-3's target fleet size; see
  `../lambda_functions/knowledge_query/README.md`'s `GET /nodes` section
  for the exact bounded-Scan/ordering/filtering semantics this relies on
  (in particular: `order` sorts by `node_id`, not telemetry recency).
- **`/dashboard` is now LIVE for node-derived figures** (Step 41) — see
  the dedicated "Dashboard (Step 41)" section below for exactly which
  numbers are live, which remain honest placeholders, and why.
- **`/events` is now LIVE** (Step 42) — see the dedicated "Events (Step
  42)" section below for exactly what this feed does and does not
  guarantee.
- **`/analytics` is now LIVE** (Step 43) — see the dedicated "Analytics
  (Step 43)" section below for exactly what this feed does and does not
  guarantee. The metric *definitions* (name/unit/description) shown are
  static, real metadata (`src/data/metricDefinitions.ts` — renamed from
  `mockMetrics.ts`, since it now contains zero mock data); the plotted
  numbers themselves come from the live `GET /metrics` response.
- **`/nodes/:nodeId` is now LIVE with no demo-data fallback** (Step 44 —
  see the dedicated "Node Detail (Step 44)" section below). Through Step
  43 this page fell back to fabricated demo data on any failure,
  including a genuine "this node does not exist" 404 — indistinguishable
  from a real load except for a small tooltip. It now shows a distinct
  "Node not found" state for a 404 and an honest `ErrorState` for any
  other failure, matching every other page's no-fallback convention.
- **`/architecture`** shows only static, factual text about the real
  pipeline (verified against the actual repository) — there is no
  live-vs-demo ambiguity there.

No page in this application ever presents demo data as if it were a real
AWS measurement. **As of Step 44, no page fabricates data at all**: every
route is live-only (`useNodes`/`useEvents`/`useMetrics`/`useNodeSummary`
— no mock-fallback hook remains anywhere in this app), and every
`DataSourceTag` in the codebase is either `isMock={false}` ("Live") or
`variant="pending"` ("Not Connected," an honest absence) — the default
`'demo'` variant ("Demo data," a fabricated placeholder) is defined on
`DataSourceTag` for reuse but is not currently used by any page.

## Dashboard (Step 41)

`DashboardPage` calls `useNodes({ limit: 500, order: 'asc' })` — the
SAME hook and pattern the Nodes page (Step 40) already uses, reused as
instructed rather than duplicated. **Exactly one bounded `GET /nodes`
request** is made per page load/refresh — never one request per
subsystem, never an unbounded scan. All derived figures below come from
that single response, computed by pure functions in `src/lib/dashboard.ts`
(`deriveDashboardStats`, `deriveSubsystemStats`, `deriveSystemStatus`) —
unit-tested independently of any component (`src/test/dashboard.test.ts`).

### What is genuinely LIVE

- **Total Nodes** — `nodes.length` from the response. Note this is
  bounded by `limit=500` (SRS NFR-3's target scale) — if the table holds
  more than 500 items, this shows the fetched page's count, not
  necessarily the table's true total.
- **Healthy Nodes** — count of `state === 'HEALTHY'` only. **Does NOT
  include `RECOVERED`** — those are two different facts about a node.
- **Active Faults** — count of `state` in `FAULT_DETECTED`, `RECOVERING`,
  or `ESCALATION_REQUIRED`. **`RECOVERED` is deliberately excluded** — a
  recovered node is no longer an active problem.
- **Recovered Nodes** (renamed from the old, demo-only "Recovered
  Events" card) — count of nodes CURRENTLY in state `RECOVERED`, a
  present-tense snapshot from `SentinelGridNodeState`. **This is NOT a
  historical count of recovery events** — `GET /nodes` has no historical
  event data at all (see "Current state vs. historical events" below).
  The card is labeled "Recovered Nodes," never "Recovered Events," to
  keep that distinction honest.
- **Subsystem cards** (Power/Water/Traffic) — real per-subsystem node/
  healthy/fault counts from the same response. Always renders all three
  cards, even if a subsystem has zero live nodes (an honest zero state,
  not an omitted card). **No hardcoded 167/167/166** or any other
  scale-test-shaped number — every count comes from the API response.
- **System status badge** ("Healthy" / "Recovering" / "Fault Detected" /
  "Escalation Required" / "Unknown") — a DASHBOARD-LEVEL INTERPRETATION
  of the live fleet, NOT an AWS/CloudWatch health check. Deterministic
  precedence (`deriveSystemStatus`'s own docstring has the full
  rationale): `ESCALATION_REQUIRED` present > `FAULT_DETECTED` present >
  `RECOVERING` present > `UNKNOWN` present (and nothing worse) > every
  node `HEALTHY`/`RECOVERED` → "Healthy". An empty node list returns
  "Unknown," never a fabricated "Healthy."
- **"API Connected" / "API Unavailable" / "Connecting to API…"** — shown
  SEPARATELY from the system-status badge. This distinguishes "did the
  HTTP request succeed" (connectivity) from "what do the returned nodes
  say" (interpreted health) — a successful, empty, all-healthy response
  and a successful, all-escalated response are both "API Connected," with
  very different system-status badges next to them.
- **Simulated City & Node View** — same `CityVisualization` component as
  before, now fed the live `nodes` array directly (not
  `src/data/mockNodes.ts`). All fetched nodes are passed through
  un-filtered — nothing is silently dropped, so faulted/escalated nodes
  are never hidden. **Step 44**: each node is now a real, keyboard-
  focusable link to `/nodes/:nodeId` — a transparent HTML anchor layer
  positioned over the (now purely decorative, `aria-hidden`) SVG dots,
  rather than an anchor nested inside the SVG itself, so click/keyboard/
  screen-reader behavior matches the plain `HTMLAnchorElement` used
  everywhere else in this app. Still explicitly labeled a stylized, non-geographic
  layout (unchanged claim — the simulator has no coordinates; only the
  node identities/states shown are now live, not the positions).

### What remains an honest placeholder (NOT live, NOT fabricated)

- **System Activity & Resilience** (Detection Latency, Recovery Latency,
  Recovery Success, Unresolved Faults) — these are CloudWatch-derived
  metrics, not NodeState values, and this project has no CloudWatch
  `GetMetricData` integration from the browser (deliberately — the
  browser never calls CloudWatch directly). Each card shows "—" / "Not
  yet connected," with a **"Not Connected"** `DataSourceTag`
  (`variant="pending"`, distinct from the amber "Demo data" tag used
  elsewhere — there is no fabricated number behind these cards at all,
  so labeling them "Demo data" would be inaccurate). Real, measured
  values from controlled backend tests are linked instead:
  `docs/performance-validation.md` / `docs/scale-validation.md`.
- **Recent Events** — `GET /nodes` returns CURRENT state only, never
  historical anomaly/recovery events (see below). Rather than issuing a
  per-node history request for every visible node (potentially hundreds
  of requests — explicitly ruled out) or fabricating a synthetic events
  list, this section shows an honest `EmptyState`: *"Recent event
  history will be connected in a later step."* — also with a **"Not
  Connected"** tag.

### Current NodeState vs. historical events (why "Recent Events" isn't live)

`SentinelGridNodeState` (what `GET /nodes` reads) holds exactly ONE
row per node — its LATEST known state, overwritten in place.
`SentinelGridAnomalyEvents`/`SentinelGridRecoveryEvents` are separate,
append-only historical tables `GET /nodes` never touches. There is no
endpoint that aggregates historical events across the fleet — only
per-node history endpoints (`GET /nodes/{node_id}/anomalies` /
`/recoveries`), which the Dashboard deliberately does not call once per
visible node to avoid an unbounded number of requests. This is the exact
same distinction `../lambda_functions/knowledge_query/README.md`
documents for the backend; the Dashboard's own placeholder text says so
explicitly rather than presenting current-state data as if it were
event history.

### NodeState vs. CloudWatch metrics

Two entirely different data sources, never conflated: `NodeState`
(`GET /nodes`) is DynamoDB-backed, per-node, current-snapshot data — the
Dashboard's summary/subsystem/status cards are built from it. CloudWatch
custom metrics (`DetectionLatency`, `RecoveryLatency`, etc. — see
`shared/metrics.py`) are a completely separate system this frontend has
no integration with at all. No value on this dashboard is ever computed
by combining or approximating one from the other.

### Refresh behavior

A single **Refresh** button in the header calls the same `refetch()`
Step 40 already added to `useNodes` — no new refresh mechanism was
built. **No automatic polling** — a request is made once on mount and
again only when the operator clicks Refresh (or Retry, in the error
state, which calls the same function). The button is disabled while a
request is already in flight.

### Loading / error / empty behavior

- **Loading**: the existing `LoadingState` component (skeleton rows),
  same as the Nodes page.
- **Error**: the existing `ErrorState` component with a Retry button —
  no demo-data fallback, matching Step 40's own Nodes-page rule.
- **Empty** (`GET /nodes` returns zero nodes): an honest `EmptyState`
  ("No nodes are currently reporting state") in place of the summary/
  subsystem/city-view sections — never a page full of fabricated or
  misleadingly-zeroed cards presented as if they were meaningful.

## Events (Step 42)

`EventsPage` calls `useEvents(queryOptions)` (`src/hooks/useEvents.ts`) —
the SAME hook/no-fallback pattern `useNodes` (Step 40) already
established, reused rather than duplicated. `queryOptions` is derived
from the page's own filter `<select>`s (`event_type`, `subsystem`,
`severity`, `status`) via `useMemo`; changing any filter issues a new
**single, bounded** `GET /events` request (default `limit=50`) — never
client-side filtering over a fixed, larger fetch, and never one request
per node.

- **Live, bounded, not complete history** — a successful call shows the
  green **"Live"** tag and real events; a failed call shows an honest
  `ErrorState` (with a **"Not Connected"** tag, not "Demo data" — no
  fabricated events are ever shown in the error state) with a retry
  button. The page header and the results caption both say plainly that
  this is "a bounded, recent feed... not the complete historical
  database" — see `../lambda_functions/knowledge_query/README.md`'s `GET
  /events` section for the exact bounded-Scan/merge/sort semantics and
  honesty limitations this relies on.
- **Anomaly vs. recovery, visually distinguishable** — `EventTable.tsx`'s
  Type column renders a colored `StatusBadge` (`anomaly` → amber/warning,
  `recovery` → green/healthy, `escalation` — a recovery event whose
  `status` is `ESCALATION_REQUIRED` — → red/critical) rather than plain
  text. Recovery rows additionally show a ✓/✗ success indicator next to
  their status when `success` is `true`/`false`. Only real backend field
  values are ever rendered (e.g. `NODE_OVERLOAD`, `SENSOR_DROPOUT`,
  `REDUCE_LOAD`, `RESTART_SENSOR`) — nothing is invented.
- **Timestamp semantics carried through unchanged** — a recovery event's
  own `timestamp` is its identity/sort-key timestamp, never relabeled as
  "detection time" anywhere in the frontend, matching the backend's own
  explicit rule (`FleetEvent`'s doc comment in `src/types/api.ts` states
  this directly).
- **`status` filter is recovery-only** — selecting "Anomaly" in the
  Event type filter automatically clears any active Status filter and
  disables the Status `<select>` (with a tooltip explaining why), so the
  UI never issues the guaranteed-`400 INVALID_STATUS` combination the
  backend rejects.
- **Empty state**: "No anomaly or recovery events found." — shown either
  when the fleet genuinely has no matching events, or the current filters
  exclude everything; never an invented event.
- **Refresh behavior**: a manual **Refresh** button only, matching the
  Nodes/Dashboard pages — no automatic polling.

## Analytics (Step 43)

`AnalyticsPage` calls `useMetrics(queryOptions)` (`src/hooks/useMetrics.ts`)
— the SAME hook/no-fallback pattern `useNodes`/`useEvents` already
established, reused rather than duplicated. `queryOptions` (`start`,
`end`, `subsystem`, `fault_type`) is derived from the page's own
time-range/subsystem/fault-type controls via `useMemo`; changing any of
them (or clicking Refresh) issues a new **single, bounded** `GET
/metrics` request — never client-side aggregation over a larger fetch,
never automatic polling.

- **Live, bounded, never a computed SLA/accuracy figure** — a successful
  call shows the green **"Live"** tag and real CloudWatch-derived
  charts; a failed call shows an honest `ErrorState` (with a **"Not
  Connected"** tag, not "Demo data" — no fabricated series are ever
  shown in the error state) with a retry button. The page's own banner
  text states plainly that these are CloudWatch observational
  measurements, not a computed SLA/accuracy figure, and that no
  detection-accuracy or false-positive-rate figure exists — see
  `../lambda_functions/knowledge_query/README.md`'s "Metric semantics"
  section for the exact wording this mirrors.
- **Time-range selector**: Last 15 minutes / 1 hour (default) / 6 hours /
  24 hours (`src/lib/analytics.ts`'s `TIME_RANGE_OPTIONS`) — never a
  wider range; the backend itself rejects anything over 24 hours.
- **Subsystem/Fault type filters** — dropdowns matching this project's
  own closed vocabularies (`src/types/domain.ts`'s `SUBSYSTEMS`/
  `FAULT_TYPES`, the latter deliberately excluding `detector.py`'s
  `FaultType.NONE`, which is never a real metric dimension value).
  Server-side filtering (a new bounded request per change), never
  client-side filtering over a fixed larger fetch.
- **One card per approved metric (7 total)**, fed by
  `src/lib/analytics.ts`'s `aggregateMetricSeries` — a pure function
  (independently unit-tested, `src/test/analytics.test.ts`) that combines
  every `GET /metrics` series sharing one metric name into one chartable
  series (summing Count-unit values, averaging Milliseconds-unit values,
  across whichever Subsystem/FaultType dimension pairs the current
  filters left ungrouped), and discloses "aggregated across N series"
  when more than one dimension pair contributed — never silently
  presenting a combined number as if it came from one dimension pair.
- **Honest per-metric empty state**: "No datapoints published in the
  selected window." on any card with zero live datapoints — never a
  fabricated zero-filled bar.
- **RecoverySuccessCount vs. RecoveryFailureCount, visually
  distinguishable** — `MetricChartCard`'s bars render in a `healthy`
  (green) tone for the former and `critical` (red) tone for the latter,
  matching this project's existing status-tone vocabulary.
- **Latency averages are always labeled "Average," never "MTTR" or a
  percentile** — the `summary` line reads `Average: <n> ms` exactly;
  `RecoveryLatency`'s own card description separately, explicitly states
  it combines detect+remediate time and is not isolated remediation
  duration.
- **`UnresolvedFaultCount`'s card description** states explicitly that
  this is an observation count, not a continuously-maintained
  active-fault gauge (mirrors the backend's own exact wording).
- **CloudWatch resolution surfaced in the UI, not just docs**: the page
  shows the actual resolved window and `period_seconds` used, with a
  note when the period exceeds 60 seconds (longer windows) that this is
  an aggregation, not finer real resolution.
- **Refresh behavior**: a manual **Refresh** button that re-anchors the
  selected time range to "now" — no automatic polling anywhere on this
  page.

## Node Detail (Step 44 — removed the last mock-data fallback in the app)

`NodeDetailPage` calls `useNodeSummary(nodeId)` → `GET /nodes/{node_id}/summary`.
Through Step 43 this was the ONE remaining page in the app built on the
Step 39 `useApiResource` "try live, fall back to demo data" pattern — on
ANY failure (network error, backend 500, or a genuine 404 for a node
that doesn't exist) it silently rendered a fully-populated, fabricated
Node Identity/Current Fault/history card, with only a small tooltip
reason hinting it wasn't real. `useApiResource.ts` and
`src/data/mockEvents.ts` have been deleted (nothing else used them);
`src/data/mockNodes.ts`'s `MOCK_NODES` remains only as a component-test
fixture (`src/test/components.test.tsx`), never imported by a page.

`useNodeSummary` now follows the exact same no-fallback pattern as
`useNodes`/`useEvents`/`useMetrics` — **every hook in this app is now
live-only, with no mock-fallback hook remaining anywhere**. `NodeDetailPage`
distinguishes four states explicitly:

- **Loading** — `LoadingState`.
- **Node not found** — a genuine `404 NODE_NOT_FOUND` from the backend
  shows an honest "Node not found" `EmptyState` with a link back to
  `/nodes`. No fabricated Node Identity card, no "Demo data" tag — a
  **"Not Connected"** `DataSourceTag` instead, since nothing at all is
  being shown for this node.
- **Error** — any OTHER failure (network, CORS, 500) shows `ErrorState`
  with a Retry button, distinct wording from "Node not found" (an API
  error is not the same claim as "this node doesn't exist").
- **Success** — the real node identity, current fault, and anomaly/
  recovery history, with a green **"Live"** tag.

A "&larr; Back to Nodes" link (to `/nodes`) is shown in the page header
in every one of these four states, not just on success.

## Design system

Dark, restrained "operations console" aesthetic (see `src/index.css`'s
`@theme` block for the exact token values) — status colors are used
consistently everywhere:

| Tone | Meaning | Example use |
|---|---|---|
| `healthy` | HEALTHY / RECOVERED / SUCCESS | Node state, recovery outcome |
| `warning` | FAULT_DETECTED / MEDIUM severity | Node state, severity badge |
| `critical` | ESCALATION_REQUIRED / HIGH-CRITICAL severity / FAILURE | Node state, severity, recovery outcome |
| `info` | RECOVERING, informational indicators | Node state, header status |
| `neutral` | UNKNOWN / no data | Fallback |

## Testing

```bash
npm run test
```

131 tests across thirteen files:
- `src/test/App.test.tsx` — every route renders without throwing, including the 404 fallback (the `/nodes`, `/dashboard`, `/events`, `/analytics`, and `/nodes/:nodeId` routes' fetches are mocked so each test reaches a loaded state).
- `src/test/components.test.tsx` — key reusable components (badges, MetricCard, EmptyState, NodeTable, `Timestamp` — Step 44's exact-value-preserving timestamp component) render correctly.
- `src/test/api.test.ts` — `src/services/api.ts`'s functions (including `getNodes()` Step 40, `getEvents()` Step 42, `getMetrics()` Step 43) call the correct URL/query-string shape, decode responses correctly, and surface `ApiError`/network failures correctly, using a mocked `fetch`. No live network access is made during tests.
- `src/test/NodesPage.test.tsx` (Step 40) — loading state, live-success rendering with the "Live" indicator, honest error state on failure (with no demo-data fallback asserted), and the retry button.
- `src/test/dashboard.test.ts` (Step 41) — `deriveDashboardStats`/`deriveSubsystemStats`/`deriveSystemStatus` as pure unit tests: total/healthy/active-fault/recovered counts, the RECOVERED-is-not-a-fault rule, all-three-subsystems-always-present (including an honest zero state), the system-status precedence rule (including empty-list and UNKNOWN handling), and order-independence.
- `src/test/DashboardPage.test.tsx` (Step 41, extended Step 44) — loading, a single bounded `GET /nodes?limit=500` request (not per-subsystem calls), live totals + "Live" tag, "API Connected" + derived system status, the "Recovered Nodes" (not "Recovered Events") label, honest empty state, honest error state with no demo-data fallback, the Refresh button re-issuing the request, the Recent Events/resilience-metrics placeholders honestly linking to the real Events/Analytics pages (Step 44 — these no longer claim to be "not implemented"), the Subsystem cards linking to a pre-filtered Nodes page, and the City Visualization linking each node to its Node Detail page (Step 44).
- `src/test/EventsPage.test.tsx` (Step 42, extended Step 44) — loading state, a single bounded `GET /events` request at the default limit, live-success rendering with the "Live" indicator, honest empty state, honest error state with no demo-data fallback (asserts the **"Not Connected"** tag specifically, not "Demo data"), anomaly/recovery/escalation rows rendering visually distinguishably, filter changes re-issuing a request with the corresponding query parameter, the Refresh button re-issuing the request, the "bounded, recent feed" caption text, and each row's node ID linking to its Node Detail page (Step 44).
- `src/test/analytics.test.ts` (Step 43) — `resolveTimeRange`/`aggregateMetricSeries` as pure unit tests: each time-range option resolves to the correct bounded window (never exceeding 24 hours), single-series pass-through, sum-across-series for Count metrics, average-across-series for Milliseconds metrics, no percentile ever computed, ignoring non-matching metric names, and deterministic timestamp sorting.
- `src/test/AnalyticsPage.test.tsx` (Step 43) — loading state, a single bounded `GET /metrics` request, live-success rendering with the "Live" indicator, honest empty state, honest error state with no demo-data fallback (asserts the **"Not Connected"** tag), per-card honest "no datapoints" note, RecoverySuccessCount/RecoveryFailureCount visual distinction, the latency summary reading "Average" (never "MTTR" or a percentile), the UnresolvedFaultCount active-fault-gauge disclaimer, time-range/subsystem/fault-type filters re-issuing bounded requests, the 24-hour time-range option never exceeding 24 hours, the Refresh button re-issuing the request, and the detection-accuracy/false-positive-rate disclosure text.
- `src/test/NodeDetailPage.test.tsx` (Step 44) — loading state, live-success rendering with the "Live" indicator, a distinct "Node not found" state for a 404 (with no fabricated node data and no "Demo data"/"Live" tag), a distinct `ErrorState` for a genuine API/network failure (never confused with "Node not found"), the Retry button re-issuing the request, the "Back to Nodes" link, and honest empty anomaly/recovery history states.
- `src/test/status.test.ts` (Step 44) — `nodeStateTone`/`recoveryStatusTone`/`severityTone` as pure unit tests, including the Step 44 consistency fix (`ESCALATION_REQUIRED` now carries the same `'critical'` tone in both `nodeStateTone` and `recoveryStatusTone` — previously `'warning'` in the latter, a real inconsistency found during the Step 44 audit) and graceful degradation for an unrecognized value.

This is intentionally a small, targeted suite (per Step 39/40/41/42/43/44's
own scope), not exhaustive coverage of every component.

## What Step 39 explicitly does NOT do

- No deployment (no S3, CloudFront, or hosting of any kind).
- No new AWS resources of any kind.
- No authentication/authorization.
- No backend/Lambda/Step Functions/DynamoDB/IoT change.
- No fabricated live metrics — every demo value is clearly labeled.

## What Step 40 added, and what it still explicitly does NOT do

Added: `GET /nodes` fleet listing (backend), CORS on the existing API
(backend), `getNodes()` (frontend service), and live-wiring the Nodes
page only. See `../infrastructure/README.md`'s "Fleet Listing & CORS
(Step 40)" section and `../lambda_functions/knowledge_query/README.md`
for the full backend-side documentation.

Still explicitly out of scope:
- No frontend deployment (unchanged from Step 39).
- No Dashboard/Events/Analytics live-data wiring (see "Mock/demo data"
  above for exactly why, and what each would additionally need).
- No authentication/authorization (the API remains intentionally
  read-only but unauthenticated — see the backend README).
- No Lambda concurrency work, no 500-node scalability test (both
  explicitly out of scope for this step).
- No change to Node Detail's own behavior.

## What Step 41 added, and what it still explicitly does NOT do

Added: live-wiring the Dashboard page only (`src/lib/dashboard.ts`'s
three pure derivation functions, `DataSourceTag`'s new `pending` variant,
and `DashboardPage` itself rewritten to consume `useNodes` — the SAME
Step 40 hook, reused rather than duplicated). **No backend change of any
kind was needed or made** — `GET /nodes`'s existing response already
contained everything the Dashboard needed. See the "Dashboard (Step 41)"
section above for exactly which figures are live vs. placeholder.

Still explicitly out of scope:
- No frontend deployment (unchanged from Step 39/40).
- No Events/Analytics live-data wiring (unchanged from Step 40's own
  scope decision).
- No CloudWatch metrics integration (System Activity & Resilience
  remains a placeholder — see above).
- No historical event aggregation, no new backend endpoint, no GSI, no
  authentication (all unchanged).
- No automatic polling — manual Refresh only.
- No change to the Nodes page or Node Detail page's own behavior.

## What Step 42 added, and what it still explicitly does NOT do

Added: `GET /events` fleet-wide event feed (backend, extending the same
existing Knowledge Query API/Lambda/stack — no new API, no new Lambda, no
new table, no GSI), `getEvents()` (frontend service), `useEvents()`
(frontend hook, following the same no-fallback pattern as `useNodes`),
`EventTable.tsx`'s new anomaly/recovery/escalation visual distinction,
and live-wiring the Events page only. See
`../infrastructure/README.md`'s "Fleet-Wide Event Feed (Step 42)" section
and `../lambda_functions/knowledge_query/README.md`'s `GET /events`
section for the full backend-side documentation.

Still explicitly out of scope:
- No frontend deployment (unchanged from Step 39/40/41).
- No Analytics live-data wiring (unchanged — still needs a CloudWatch
  metrics integration that does not exist).
- No Dashboard change beyond what Step 41 already did (the Dashboard's
  own "Recent Events" placeholder was deliberately left as-is — it is
  about avoiding hundreds of per-node requests, a different problem than
  what the new bounded `GET /events` route solves for a dedicated Events
  page).
- No authentication (unchanged).
- No guaranteed-complete/correct fleet-wide historical event query — the
  new feed is honestly bounded and recent, not a complete export (see
  the "Events (Step 42)" section above and the backend README for the
  exact limitation).
- No automatic polling — manual Refresh only, matching the Nodes/
  Dashboard pages.
- No change to the Nodes, Dashboard, Node Detail, Analytics, or
  Architecture pages' own behavior.

## What Step 43 added, and what it still explicitly does NOT do

Added: `GET /metrics` CloudWatch metrics feed (backend, extending the
same existing Knowledge Query API/Lambda/stack — no new API, no new
Lambda, no new DynamoDB table, no GSI; the FIRST route in this stack
backed by CloudWatch rather than DynamoDB, via a new
`metrics_service.py` module), `getMetrics()` (frontend service),
`useMetrics()` (frontend hook, following the same no-fallback pattern as
`useNodes`/`useEvents`), `src/lib/analytics.ts`'s two pure functions
(`resolveTimeRange`, `aggregateMetricSeries`), `MetricChartCard.tsx`
rewritten to render live data with a success/failure tone distinction,
and live-wiring the Analytics page only. `src/data/mockMetrics.ts` was
renamed to `src/data/metricDefinitions.ts` and its `demoSeries()` helper
removed (dead code once Analytics no longer needed synthetic series).
See `../infrastructure/README.md`'s "CloudWatch Metrics Feed (Step 43)"
section and `../lambda_functions/knowledge_query/README.md`'s `GET
/metrics` section for the full backend-side documentation, including a
real IAM finding (`cloudwatch:GetMetricData` cannot be scoped by the
`cloudwatch:namespace` condition key the way this project's existing
`PutMetricData` grants can, discovered via live evidence during this
step's own deployment) documented in detail there.

Still explicitly out of scope:
- No frontend deployment (unchanged from Step 39/40/41/42).
- No CloudWatch alarm/dashboard/SNS modification of any kind — this
  route only reads metric data, and does not touch the existing
  observability dashboard/alerting stacks.
- No `cloudwatch:ListMetrics` call or permission — dimension
  combinations are derived from this project's own closed vocabulary
  (see the backend README's own reasoning), not discovered.
- No percentile (p50/p95/p99) computation, no MTTR label on any latency
  average, no continuously-maintained active-fault gauge for
  `UnresolvedFaultCount`, no detection-accuracy/false-positive-rate
  figure — all explicitly disclosed as NOT implemented, both in this
  page's own banner text and in the backend README's "Metric semantics"
  section.
- No change to `shared/metrics.py` or any metric name/namespace/unit/
  dimension/publishing behavior (unchanged, verified).
- No automatic polling — manual Refresh only (re-anchors the selected
  time range to "now"), matching the Nodes/Dashboard/Events pages.
- No change to the Nodes, Dashboard, Node Detail, Events, or
  Architecture pages' own behavior.

## What Step 44 added, and what it still explicitly does NOT do

Step 44 was a frontend-only integration/polish/consistency pass across
every existing page — no new functionality, no new API routes, no AWS
changes. A full audit of `frontend/src` found and fixed the following
real (not cosmetic-only) issues:

- **Removed the last mock-data fallback in the app** — `NodeDetailPage`/
  `useNodeSummary` no longer falls back to fabricated data on failure
  (see the dedicated "Node Detail (Step 44)" section above).
  `useApiResource.ts` and `src/data/mockEvents.ts` were deleted (nothing
  else used them); `src/data/mockNodes.ts` remains only as a
  component-test fixture.
- **Fixed two stale "not connected" claims on the Dashboard** — the
  "System Activity & Resilience" and "Recent Events" placeholders
  previously said CloudWatch metrics/event history were "not implemented
  yet" / "will be connected in a later step," which became FALSE once
  Steps 42/43 shipped the Events and Analytics pages. Both now honestly
  state that this specific dashboard section doesn't show that data, and
  link to the page that does (`/events`, `/analytics`).
- **Fixed a "Demo data" mislabel on the Nodes page's error state** — it
  was using `DataSourceTag`'s default `'demo'` variant on failure (no
  fabricated data is actually shown there), the same class of bug
  already fixed on the Events/Analytics pages in Steps 42/43. Now uses
  `variant="pending"` ("Not Connected"), matching every other page.
  **As of Step 44, no page in this app ever shows the `'demo'` variant.**
- **Fixed a real tone inconsistency**: `ESCALATION_REQUIRED` was
  `'critical'` in `nodeStateTone` but `'warning'` in
  `recoveryStatusTone` — the same concept, two different severity colors
  depending on which badge was showing it. Both are now `'critical'`.
- **Added node-ID navigation** where it was previously plain text: each
  row in `EventTable` (the Events page) and each node in
  `CityVisualization` (the Dashboard) now link to `/nodes/:nodeId`,
  matching `NodeTable`'s pre-existing behavior.
- **Migrated the Dashboard's hand-rolled header to the shared
  `PageHeader` component** — it was the only page not using it,
  inconsistent typography/spacing versus every other page.
- **Timestamps now always keep their exact original value accessible** —
  a new `Timestamp` component (`src/components/common/Timestamp.tsx`)
  wraps every formatted timestamp in the app in a semantic `<time
  dateTime title>` element, so the raw ISO-8601 value is never lost
  behind a locale-converted, unlabeled display string, even though the
  visible text itself is still local time with no explicit timezone
  abbreviation shown (unchanged formatting, just no longer a dead end).
- **Removed dead code**: `formatRelativeTime` (`src/lib/utils.ts`) was
  exported but never called anywhere.
- **Decorative icons are now `aria-hidden` by default** (`src/components/icons.tsx`)
  — every icon in this app's icon set is always paired with visible
  text, so exposing it to the accessibility tree was a redundant,
  unlabeled announcement; a caller can still override this via props.
- **Architecture page brought current**: the pipeline diagram
  (`ArchitectureDiagram.tsx`) was missing three real, deployed pieces —
  telemetry history (`SentinelGridTelemetry`), CloudWatch Metrics as its
  own stage, and the CloudWatch Dashboard/SNS escalation stack — all
  added, using their real resource names from `infrastructure/README.md`.
  Confirmed Kinesis is NOT shown anywhere in the diagram (it was never
  actually deployed — see `infrastructure/README.md`'s own
  "telemetry-kinesis-stream.yaml ... AWS deployment BLOCKED" section);
  an explicit note now states why SQS was used instead and that Kinesis
  is deliberately excluded from the diagram.
- Everything else audited (Sidebar/Header/AppShell routing and active-
  state indication, `StatusBadge`/`SeverityBadge`/`NodeStateBadge`
  consistency, `LoadingState`/`ErrorState`/`EmptyState` usage across
  every page, table responsive `overflow-x-auto` wrappers, focus-visible
  outlines, `role="alert"`/`role="status"` usage) was already correct
  and needed no change.

Still explicitly out of scope (per this step's own instructions):
- No frontend deployment.
- No authentication, no new backend service, no WebSockets, no polling
  beyond what already existed.
- No AWS/Lambda/API Gateway/Step Functions/IAM/CloudWatch-metric-
  publishing change of any kind — this was a frontend-only step.
- No new backend routes, no new pages, no redesign of the existing
  visual direction (colors, layout, component library all unchanged).
- No claim that NFR-1/NFR-2/NFR-3 are now fully demonstrated — unchanged
  from prior steps' own honest assessment.
- `RecoveryLatency` is still never labeled "MTTR" anywhere; no detection-
  accuracy/false-positive-rate figure was added.
