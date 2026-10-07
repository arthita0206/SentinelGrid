# SentinelGrid

SentinelGrid is a software-only, simulation-based self-healing cloud-IoT
architecture for smart-city infrastructure, built as an academic cloud
architecture project.

There is no physical hardware involved — all IoT devices and telemetry are
simulated in software.

## Simulated subsystems

The project simulates three smart-city subsystems:

1. **Power** — power grid nodes
2. **Water** — water network nodes
3. **Traffic** — traffic sensor nodes

## Architecture: MAPE-K

SentinelGrid will eventually implement a self-healing control loop based on
the MAPE-K autonomic computing model:

**Monitor → Analyze → Plan → Execute → Knowledge**

using AWS managed/serverless services, including:

- AWS IoT Core
- Amazon Kinesis Data Streams
- AWS Lambda
- AWS Step Functions
- Amazon DynamoDB
- Amazon CloudWatch
- Amazon SNS
- AWS IAM
- AWS KMS

## Incremental development

This project is being built incrementally, one well-defined step at a time.
AWS resources are **not** configured until the project explicitly reaches
that stage.

**Current stage: Step 5 — persistent telemetry storage (infrastructure defined, not yet deployed).**

Step 1 established the local Python project structure, configuration, and
test scaffolding. Step 2 added a fully local telemetry simulator (see
[Local simulator](#local-simulator) below). Step 3 added a local fault
injection engine (see [Fault injection](#fault-injection) below). Step 4
added an optional `AwsIotPublisher` so telemetry can be published to AWS
IoT Core (see [AWS IoT publishing](#aws-iot-publishing) below) — **local
mode remains the default**. Step 5 adds CloudFormation infrastructure
(DynamoDB + an IoT topic rule + IAM role) so published telemetry can be
persisted (see [Persistent telemetry storage](#persistent-telemetry-storage)
below) — **this infrastructure is defined but has not been deployed to
any AWS account by this project.** Anomaly detection and
recovery/self-healing are **not implemented yet**.

## Project structure

```
SentinelGrid/
├── simulator/          # Telemetry simulation logic (cloud-agnostic)
├── fault_injection/     # Fault/anomaly injection logic (cloud-agnostic)
├── shared/               # Code shared across simulation & cloud layers
├── config/               # Simulation configuration
├── tests/                # Unit tests
├── infrastructure/       # AWS infrastructure-as-code (CloudFormation, Step 5)
├── lambda_functions/     # AWS Lambda functions (anomaly detection)
├── remediation/          # Local, simulated remediation engine (Step 12, no AWS yet)
├── step_functions/       # AWS Step Functions definitions (future step)
```

Simulation logic (`simulator/`, `fault_injection/`, `shared/`) is kept
independent of AWS-specific code (`infrastructure/`, `lambda_functions/`,
`step_functions/`) to maintain a clean separation between simulation logic
and cloud integration.

## Getting started (local development)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pytest
```

## Configuration

Local simulation defaults live in [config/simulation_config.yaml](config/simulation_config.yaml):

| Setting                       | Default |
|--------------------------------|---------|
| `power.num_nodes`               | 20      |
| `water.num_nodes`               | 20      |
| `traffic.num_nodes`             | 20      |
| `telemetry.interval_seconds`    | 5       |
| `publisher.mode`                 | `local` |
| `aws.region`                      | `ap-south-1` |
| `aws.profile`                      | `sentinelgrid` |
| `aws.iot.topic`                     | `sentinelgrid/telemetry` |

## Local simulator

The simulator runs entirely locally by default — no AWS setup is required.

It generates synthetic telemetry for the configured number of nodes in each
of the three subsystems, once per telemetry interval, and hands each
reading to a `Publisher`. `LocalPublisher` prints it as JSON to stdout;
`AwsIotPublisher` (Step 4) instead publishes it to AWS IoT Core:

```
CitySimulator -> Telemetry objects -> Publisher interface
                                           |-- LocalPublisher (default)
                                           |-- AwsIotPublisher (Step 4, opt-in)
```

### Telemetry fields

Every reading includes `node_id`, `subsystem`, a UTC `timestamp`, `status`,
and a `data` object with subsystem-specific fields:

- **Power** — `load` (%), `voltage` (V), `frequency` (Hz)
- **Water** — `pressure` (bar), `flow_rate` (L/s), `reservoir_level` (%)
- **Traffic** — `vehicle_count`, `average_speed` (km/h), `signal_state` (RED/YELLOW/GREEN)

Values start from a randomized baseline per node and drift with small
natural variation on each tick. They are illustrative synthetic data, not
real-world validated sensor values.

### Running the simulator

```bash
python -m simulator
```

Runs until stopped with Ctrl+C.

## Fault injection

A local `FaultInjectionEngine` (in [fault_injection/](fault_injection/))
sits between telemetry generation and publishing, so the subsystem node
classes stay focused on normal readings:

```
CitySimulator -> node.generate_telemetry() -> FaultInjectionEngine.apply() -> Publisher
```

It supports four fault types:

| Fault | Effect |
|---|---|
| `dropout` | Target node(s) stop publishing for a configurable duration, then resume automatically. |
| `corruption` | Target node(s) report out-of-range or malformed values (e.g. negative pressure, impossible frequency). |
| `partition` | Target node(s) become unreachable from the publishing path for a configurable duration. |
| `overload` | Target node(s) report a burst of abnormally high readings (e.g. near-100% load, traffic gridlock). |

Faults can target a single node (`power-001`), a list of nodes
(`power-001,power-002`), or a whole subsystem (`subsystem:water`), and can
be triggered **manually** (via the engine's `inject()` API or the
`python -m fault_injection` CLI), on a **schedule**, or **randomly** with a
configurable, seedable probability. Automatic (random/scheduled)
triggering is **disabled by default** — normal simulation output stays
completely fault-free unless explicitly requested. Every injected fault is
logged to `logs/fault_log.jsonl` with its ID, type, target(s), UTC
timestamp, duration, and status. See
[fault_injection/README.md](fault_injection/README.md) for full details.

This step does not implement AWS integration, anomaly detection, or
recovery — faults are generated and logged, not detected or healed.

## AWS IoT publishing

Step 4 adds `AwsIotPublisher` (in
[simulator/aws_publisher.py](simulator/aws_publisher.py)) as a second
implementation of the same `Publisher` interface `LocalPublisher` already
implements — `CitySimulator` and `FaultInjectionEngine` are unchanged.
Which publisher(s) are used is controlled by `publisher.mode` in
[config/simulation_config.yaml](config/simulation_config.yaml):

| Mode | Behavior |
|---|---|
| `local` (default) | Prints JSON to stdout, exactly as in Steps 1–3. No AWS involved, no `boto3` calls made. |
| `aws_iot` | Publishes each `Telemetry.to_json()` reading as an MQTT message to the configured AWS IoT Core topic. |
| `both` | Does both, via `MultiPublisher`. |

**Local mode is the default and is unaffected by this step.** Running
`python -m simulator` with no config changes behaves exactly as before.

### AWS configuration

```yaml
aws:
  region: ap-south-1
  profile: sentinelgrid
  iot:
    topic: sentinelgrid/telemetry
    endpoint_url: null   # auto-discovered via iot:DescribeEndpoint if left null
```

- **No credentials are ever stored in this repo.** `AwsIotPublisher` never
  reads or handles access keys/secrets itself — it uses `boto3`'s normal
  credential resolution: the `profile` above (an AWS CLI profile you
  configure locally with `aws configure --profile sentinelgrid`) if set,
  otherwise environment variables, the default profile, or an instance
  role. `.gitignore` already excludes `.aws/` and other credential files.
- `endpoint_url` is optional. If left `null`, the publisher discovers your
  account's IoT Data-ATS endpoint automatically the first time it
  publishes (one `iot:DescribeEndpoint` call), so nothing account-specific
  needs to be hardcoded.
- Region/profile are read once per publisher instance; `aws_iot`/`both`
  mode is only imported when selected, so `boto3` and an AWS profile are
  never required to run in local mode.

### Running in AWS IoT mode

```bash
aws configure --profile sentinelgrid   # one-time, outside this project
# then, with publisher.mode: aws_iot (or both) in config/simulation_config.yaml:
python -m simulator
```

**This step does not create any AWS resources.** It only publishes MQTT
messages to a topic name; the AWS account, IoT Core access, an IoT
endpoint, and any rule/subscriber that consumes the topic are all manual
setup outside this codebase (see below).

### What this step does NOT do

- No AWS resources (IoT Things, certificates, policies, rules, streams,
  Lambda, DynamoDB, etc.) are created, modified, or deleted.
- No anomaly detection, fault classification, remediation, or dashboard —
  this step only gets telemetry as far as an IoT Core topic.
- `LocalPublisher` and all Step 1–3 behavior are unchanged.

## Persistent telemetry storage

Step 5 adds CloudFormation infrastructure, under
[infrastructure/telemetry-storage.yaml](infrastructure/telemetry-storage.yaml),
that continues the pipeline already publishing telemetry to AWS IoT Core:

```
AwsIotPublisher -> AWS IoT Core (topic: sentinelgrid/telemetry)
    -> IoT Topic Rule: sentinelgrid_telemetry_to_dynamodb
    -> DynamoDB table: SentinelGridTelemetry
```

It defines three resources: a `PAY_PER_REQUEST` DynamoDB table keyed on
`node_id` (partition) and `timestamp` (sort) — matching the telemetry
payload's own fields — an IoT topic rule that selects every message on
`sentinelgrid/telemetry` and writes it into that table via the
`DynamoDBv2` action, and a dedicated IAM role (trusted only by
`iot.amazonaws.com`, granted only `dynamodb:PutItem` on that one table —
no wildcards, no admin access). Full details, including the exact
resources, key semantics, and how the rule maps to DynamoDB, are in
[infrastructure/README.md](infrastructure/README.md).

**This template has not been deployed.** Writing and testing it creates
nothing in AWS; deployment is a deliberate, separate, manual step (see
that README for the exact command and prerequisites).

## Frontend (Step 39)

> Note: this top-level README's narrative above predates most of the
> project (it was last substantially updated around Step 5); the backend
> has since progressed much further — see `infrastructure/README.md`,
> `lambda_functions/*/README.md`, and `docs/` for the current, accurate
> state of the deployed AWS pipeline (IoT → SQS → detection →
> remediation → Knowledge Query API). This section documents the
> **frontend**, added in Step 39.

A local, buildable operations-dashboard UI lives in
[`frontend/`](frontend/) — React + TypeScript + Vite + Tailwind CSS, with
routes for a dashboard, node fleet view, node detail, events, analytics,
and an architecture explainer. It is a **read-only consumer** of the
existing `SentinelGridKnowledgeQuery` API (Step 35) — no backend,
infrastructure, Lambda, or AWS resource was created or modified to build
it, and it is **not deployed anywhere**.

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

See [`frontend/README.md`](frontend/README.md) for full setup
instructions, the directory structure, exactly which pages use live vs.
demo data (and why — the current API has no fleet-listing endpoint), and
test/build instructions.
