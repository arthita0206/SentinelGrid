# Fault Injection

A local, cloud-agnostic engine that injects simulated faults into
SentinelGrid's telemetry so the future MAPE-K Analyzer/Planner have real
conditions to detect and heal. No AWS dependencies.

## Why this exists

A self-healing system needs something to heal from. This engine generates
that "something" locally and deterministically, so later steps (Analyzer,
Planner, recovery) can be built and demonstrated without needing a real
fault in real infrastructure.

## Flow

```
CitySimulator
    |
    v
node.generate_telemetry()
    |
    v
FaultInjectionEngine.apply(telemetry)
    |-- dropout/partition active for this node -> returns None (nothing published)
    |-- corruption/overload active for this node -> rewrites telemetry.data
    v
Publisher
```

Fault logic lives entirely in this package — `PowerNode`, `WaterNode`, and
`TrafficNode` only generate normal readings and know nothing about faults.

## The four fault types

| Fault | SRS | Effect |
|---|---|---|
| `dropout` (`SensorDropoutFault`) | FR-2.1 | Target node(s) stop publishing for a configurable duration, then resume automatically. The node itself is never removed or disabled — it keeps generating telemetry internally, it just isn't published. |
| `corruption` (`DataCorruptionFault`) | FR-2.2 | Target node(s)' next reading(s) contain out-of-range or malformed values (e.g. negative pressure, impossible frequency, a non-numeric field). One-shot by default; can be given a duration to corrupt readings for a period instead. |
| `partition` (`NetworkPartitionFault`) | FR-2.3 | Target node(s) become unreachable from the publishing path for a configurable duration — mechanically identical to dropout, but modeling a network-layer cause rather than a sensor-layer one. No real OS networking is touched. |
| `overload` (`NodeOverloadFault`) | FR-2.4 | Target node(s) report a burst of abnormally high readings (e.g. load near 100%, traffic gridlock) for a configurable duration. No real CPU/memory load is generated. |

Corrupted/overloaded readings still publish normally with `status: ONLINE`
— a corrupted or overloaded sensor typically still reports itself healthy
while sending bad data. Deciding that mismatch is suspicious is the future
Analyzer's job, not this engine's; this step only generates realistic
fault conditions.

## Targeting (FR-2.5 groundwork)

`engine.inject(fault_type, target, duration_seconds=...)` accepts:

- a single node: `"power-001"`
- multiple nodes: `"power-001,power-002"` or `["power-001", "power-002"]`
- a whole subsystem: `"subsystem:water"`

## Triggering (FR-2.5)

Two automatic modes, configured via `fault_injection.mode` in
[config/simulation_config.yaml](../config/simulation_config.yaml):

- **`scheduled`** — each entry in `fault_injection.schedule` fires once,
  a configured number of seconds after simulator start.
- **`random`** — each tick, for each enabled fault type, a seeded RNG
  roll decides whether to inject that fault against a random node
  (`fault_injection.random_seed` makes this reproducible for tests).

Automatic triggering only runs when `fault_injection.enabled: true`. It is
**`false` by default** — normal simulation stays completely fault-free
unless you explicitly enable it or manually inject a fault.

**`manual`** mode (the default) means: automatic triggering is off, but
`engine.inject(...)` still works at any time — see below.

## Manual injection

For demonstration/testing, either call the engine directly:

```python
simulator.fault_engine.inject("dropout", "power-001", duration_seconds=15)
```

or use the CLI, which runs a short local simulation with the fault applied:

```bash
python -m fault_injection dropout power-001 --duration 15
python -m fault_injection corruption water-003
python -m fault_injection partition traffic-001,traffic-002
python -m fault_injection overload subsystem:power --ticks 3
```

## Fault logging (FR-2.6)

Every injected fault is appended as one JSON line to
`logs/fault_log.jsonl` (git-ignored) via `FaultLogger`, with `fault_id`,
`fault_type`, `target_nodes`, a UTC `injected_at` timestamp,
`duration_seconds`, and `status`. This local JSONL format is intentionally
simple so it can be swapped for cloud persistence (e.g. DynamoDB) later
without changing the engine's calling code.

## What this step does NOT do

- No AWS integration of any kind.
- No anomaly detection — faults are generated, not detected.
- No recovery/self-healing — a dropped or corrupted node stays that way
  until its fault naturally expires. Nothing in this package restores a
  node automatically; that is the future recovery system's job.
