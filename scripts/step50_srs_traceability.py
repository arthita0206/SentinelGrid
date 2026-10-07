#!/usr/bin/env python3
"""Step 50 — SentinelGrid SRS requirements traceability & compliance matrix.

This script does NOT call AWS. It is a pure data/generation script: it
defines the canonical, hand-curated requirement records (derived from
this project's actual repository and the SRS content established across
prior steps) as a single source of truth, then writes:

  - docs/step50_srs_traceability_results.json (machine-readable)
  - a Markdown table (printed to stdout / embedded in docs/srs-traceability.md)

No live AWS evidence is re-gathered here -- every fact below cites a
specific prior step's already-produced evidence artifact (docs/*.md,
docs/*.json, source files, or the deployed Step Functions ASL) rather
than re-deriving anything new. Where this session independently
re-verified something by reading the repository directly (e.g. the
Step Functions ASL's actual Retry/Catch structure, the fault_injection/
package's import graph), that is noted in the record's evidence list.

Usage:
    python3 scripts/step50_srs_traceability.py --output docs/step50_srs_traceability_results.json
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWED_STATUSES = {
    "DEMONSTRATED",
    "PARTIALLY DEMONSTRATED",
    "NOT DEMONSTRATED",
    "BLOCKED",
    "NOT APPLICABLE",
}

# Each record: requirement_id, category (FR/NFR/METRIC), requirement_text,
# srs_reference, implementation, source_files, aws_resources,
# validation_steps, evidence, status, limitations, deviation (or None),
# follow_up.
REQUIREMENTS = [
    {
        "requirement_id": "FR-1",
        "category": "FR",
        "requirement_text": (
            "City Telemetry Simulator: generate simulated smart-city telemetry "
            "for power/water/traffic subsystems. (Component named in the SRS "
            "architecture overview; granular numbered sub-clauses for this "
            "component were not included in the SRS material available to "
            "this traceability pass -- see limitation below.)"
        ),
        "srs_reference": "SRS architecture overview (component list); no numbered FR-1.x sub-clauses available to this pass",
        "implementation": (
            "Config-driven local simulator generating power/water/traffic node telemetry "
            "at a configurable interval; validated from a 60-node local run up through a "
            "live 500-node AWS-published workload."
        ),
        "source_files": ["simulator/city_simulator.py", "simulator/node.py", "simulator/subsystems/", "config/simulation_config.yaml"],
        "aws_resources": [],
        "validation_steps": ["Step 2", "Step 37", "Step 38"],
        "evidence": ["docs/scale-validation.md", "docs/step38_scale_test_results.json"],
        "status": "DEMONSTRATED",
        "limitations": [
            "Exact numbered SRS sub-clause text for FR-1 was not available to this traceability pass; "
            "this record is scoped to the SRS's own architecture-overview component name, not a fabricated "
            "sub-clause. Recommend attaching the literal SRS document to the repository for a future exact pass."
        ],
        "deviation": None,
        "follow_up": "Attach the authoritative SRS document/PDF to the repository so future traceability passes can cite exact sub-clause wording.",
    },
    {
        "requirement_id": "FR-2",
        "category": "FR",
        "requirement_text": (
            "Fault Injection Engine: inject controlled faults into simulated telemetry for "
            "testing detection/recovery. (Component named in the SRS architecture overview; "
            "granular numbered sub-clauses not available to this pass -- see limitation below.)"
        ),
        "srs_reference": "SRS architecture overview (component list); no numbered FR-2.x sub-clauses available to this pass",
        "implementation": (
            "Four fault types implemented (SensorDropoutFault, NetworkPartitionFault, "
            "DataCorruptionFault, NodeOverloadFault), each supporting targeted nodes, subsystem "
            "targeting, duration, one-shot mode, scheduled/random triggering, seedable behavior, "
            "and a JSONL fault log."
        ),
        "source_files": ["fault_injection/faults.py", "fault_injection/engine.py", "fault_injection/corruption.py", "fault_injection/overload.py", "fault_injection/logger.py"],
        "aws_resources": [],
        "validation_steps": ["Step 3", "Step 46"],
        "evidence": ["docs/step46_fault_validation.md", "docs/step46_fault_validation_results.json"],
        "status": "DEMONSTRATED",
        "limitations": [
            "The injection engine itself is fully implemented and exercised for all 4 fault types; "
            "whether each injected fault is DETECTABLE end-to-end by the deployed system is a separate "
            "question, tracked under FR-4.2/FR-4.3, not a flaw in this engine.",
            "Exact numbered SRS sub-clause text for FR-2 was not available to this pass (same caveat as FR-1).",
        ],
        "deviation": None,
        "follow_up": "Same as FR-1: attach the authoritative SRS document for exact sub-clause citation.",
    },
    {
        "requirement_id": "FR-3.1",
        "category": "FR",
        "requirement_text": "IoT Core → Kinesis Data Stream real-time ingestion.",
        "srs_reference": "SRS FR-3.1",
        "implementation": (
            "Kinesis deployment was attempted (Step 24) and blocked by an account-level "
            "SubscriptionRequiredException confirmed live via the AWS Kinesis API "
            "(re-confirmed independently in Step 49). The implemented ingestion path is "
            "IoT Core -> SQS (Standard) -> SQS Consumer Lambda -> Anomaly Detector Lambda, "
            "deployed as the practical replacement (Step 25/26)."
        ),
        "source_files": ["infrastructure/telemetry-kinesis-stream.yaml", "infrastructure/telemetry-sqs-queue.yaml", "infrastructure/sqs-consumer.yaml", "lambda_functions/sqs_consumer/"],
        "aws_resources": ["SentinelGridTelemetryQueue", "SentinelGridTelemetryDLQ", "SentinelGridSqsTelemetryConsumer"],
        "validation_steps": ["Step 24", "Step 25", "Step 26", "Step 27", "Step 38", "Step 49"],
        "evidence": ["docs/cost-resource-audit.md (Kinesis stack investigation, Step 49 section 1.1)", "infrastructure/telemetry-kinesis-stream.yaml (preserved, ROLLBACK_COMPLETE)"],
        "status": "BLOCKED",
        "limitations": [
            "The literal SRS requirement (Kinesis Data Stream) was never deployable in this AWS account. "
            "SQS is NOT Kinesis and is not represented as satisfying the literal requirement -- see the "
            "dedicated deviation record below.",
        ],
        "deviation": "kinesis_to_sqs",
        "follow_up": "If a different AWS account without the Kinesis subscription restriction becomes available, redeploy infrastructure/telemetry-kinesis-stream.yaml and reintroduce a Kinesis-based ingestion path.",
    },
    {
        "requirement_id": "FR-3.2",
        "category": "FR",
        "requirement_text": "Configurable telemetry retention, default 24 hours.",
        "srs_reference": "SRS FR-3.2",
        "implementation": (
            "The SQS ingestion buffer's message retention IS configurable (CloudFormation "
            "parameter MessageRetentionPeriodSeconds, MinValue 60 / MaxValue 1209600), but its "
            "default is 345,600 seconds (4 days) -- SQS's own out-of-the-box default, not the "
            "SRS's stated 24-hour default. Separately, the long-term telemetry history in "
            "SentinelGridTelemetry (DynamoDB) has NO configured retention/TTL at all: items "
            "persist indefinitely (12,586 items accumulated with no pruning, per Step 49)."
        ),
        "source_files": ["infrastructure/telemetry-sqs-queue.yaml"],
        "aws_resources": ["SentinelGridTelemetryQueue", "SentinelGridTelemetry"],
        "validation_steps": ["Step 25", "Step 49"],
        "evidence": ["infrastructure/telemetry-sqs-queue.yaml (Parameters.MessageRetentionPeriodSeconds)", "docs/cost-resource-audit.md (SentinelGridTelemetry item-count growth)"],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "Configurable retention exists only at the transient SQS buffer layer, with a 4-day (not 24-hour) default.",
            "The durable DynamoDB telemetry history has no retention/expiry mechanism at all -- it is not a "
            "24-hour rolling window, it is indefinite accumulation.",
            "This differs materially from Kinesis's own native, stream-level configurable retention that the "
            "original SRS requirement was almost certainly written against.",
        ],
        "deviation": "kinesis_to_sqs",
        "follow_up": "If bounded telemetry retention is required, add a DynamoDB TTL attribute to SentinelGridTelemetry, or change the SQS default to 24 hours to better match the SRS's literal default.",
    },
    {
        "requirement_id": "FR-3.3",
        "category": "FR",
        "requirement_text": "Partition telemetry by subsystem.",
        "srs_reference": "SRS FR-3.3",
        "implementation": (
            "Every telemetry record carries an explicit 'subsystem' field (shared/telemetry.py), "
            "so subsystem is available as queryable/filterable data end-to-end (e.g. CloudWatch "
            "metric dimensions, Knowledge Query API semantics). However, this is NOT implemented "
            "as physical partitioning: SentinelGridTelemetryQueue is a Standard (non-FIFO) SQS "
            "queue with no per-subsystem MessageGroupId, and SentinelGridTelemetry's DynamoDB "
            "partition key is node_id, not subsystem."
        ),
        "source_files": ["shared/telemetry.py", "infrastructure/telemetry-sqs-queue.yaml", "infrastructure/telemetry-storage.yaml"],
        "aws_resources": ["SentinelGridTelemetryQueue", "SentinelGridTelemetry"],
        "validation_steps": ["Step 5", "Step 25"],
        "evidence": ["infrastructure/telemetry-storage.yaml (KeySchema: node_id HASH, timestamp RANGE)", "infrastructure/telemetry-sqs-queue.yaml (no FifoQueue/MessageGroupId)"],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "Subsystem is present as data on every record, not as a structural partition key/shard/message-group.",
            "This is a direct consequence of the Kinesis->SQS substitution: Kinesis's partition-key-per-shard "
            "model has no direct SQS Standard equivalent, and no FIFO/MessageGroupId design was introduced "
            "to approximate it.",
        ],
        "deviation": "kinesis_to_sqs",
        "follow_up": "If physical subsystem partitioning is required, consider per-subsystem SQS queues or a FIFO queue with subsystem as MessageGroupId.",
    },
    {
        "requirement_id": "FR-4.1",
        "category": "FR",
        "requirement_text": "Threshold/rule-based anomaly detection.",
        "srs_reference": "SRS FR-4.1",
        "implementation": (
            "A local, deterministic, threshold/rule-based detector evaluates each telemetry "
            "payload against per-subsystem numeric thresholds and status/validity rules, "
            "deployed as the AnomalyDetector Lambda."
        ),
        "source_files": ["lambda_functions/anomaly_detector/detector.py", "lambda_functions/anomaly_detector/handler.py"],
        "aws_resources": ["SentinelGridAnomalyDetector"],
        "validation_steps": ["Step 7", "Step 9", "Step 19", "Step 23", "Step 27", "Step 46"],
        "evidence": ["docs/step46_fault_validation.md", "docs/step46_fault_validation_results.json"],
        "status": "DEMONSTRATED",
        "limitations": [],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "FR-4.2",
        "category": "FR",
        "requirement_text": "Missing heartbeat detection.",
        "srs_reference": "SRS FR-4.2",
        "implementation": (
            "Not independently implemented. The deployed AnomalyDetector is stateless and reacts "
            "only to telemetry it actually receives; it has no timer/heartbeat-timeout mechanism "
            "capable of noticing the ABSENCE of expected telemetry from a node. SensorDropoutFault "
            "and NetworkPartitionFault are modeled in the simulator by suppressing publication, "
            "which produces silence, not an in-band signal -- and silence alone cannot be "
            "distinguished from 'no news' by a stateless, message-triggered detector."
        ),
        "source_files": ["lambda_functions/anomaly_detector/detector.py"],
        "aws_resources": ["SentinelGridAnomalyDetector"],
        "validation_steps": ["Step 7", "Step 46"],
        "evidence": ["docs/step46_fault_validation.md (section 7.3, SENSOR_DROPOUT/NETWORK_PARTITION limitation)"],
        "status": "NOT DEMONSTRATED",
        "limitations": [
            "This is an architectural gap, not a testing gap: no stateful heartbeat-timeout detector "
            "exists anywhere in the deployed system for this requirement to be exercised against.",
        ],
        "deviation": None,
        "follow_up": "Implement a stateful heartbeat-timeout mechanism (e.g. a scheduled Lambda comparing SentinelGridNodeState's last-seen timestamp per node against an expected interval) -- explicitly NOT implemented in Step 46 per that step's own scope constraints.",
    },
    {
        "requirement_id": "FR-4.3",
        "category": "FR",
        "requirement_text": "Fault classification.",
        "srs_reference": "SRS FR-4.3",
        "implementation": (
            "The detector classifies NODE_OVERLOAD (MEDIUM/HIGH by numeric band) and "
            "DATA_CORRUPTION correctly for every fault case actually exercised end-to-end "
            "through the live AWS pipeline. SENSOR_DROPOUT and NETWORK_PARTITION classification "
            "logic exists in the detector's code but was never exercised end-to-end via live "
            "telemetry (see FR-4.2's dependency)."
        ),
        "source_files": ["lambda_functions/anomaly_detector/detector.py"],
        "aws_resources": ["SentinelGridAnomalyDetector"],
        "validation_steps": ["Step 46"],
        "evidence": ["docs/step46_fault_validation_results.json (test_cases A, A2, B, C, C2: 4/4 ground-truth cases correctly classified)"],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "Classification correctness is demonstrated only for the fault types/severities actually "
            "exercised end-to-end (NODE_OVERLOAD MEDIUM/HIGH, DATA_CORRUPTION) -- n=4 ground-truth cases, "
            "a functional sample, not a statistical accuracy proof.",
            "SENSOR_DROPOUT/NETWORK_PARTITION classification correctness is NOT generalized to be verified, "
            "since those fault types were never exercised end-to-end (see FR-4.2).",
        ],
        "deviation": None,
        "follow_up": "Once FR-4.2's heartbeat mechanism exists, extend Step-46-style ground-truth testing to SENSOR_DROPOUT/NETWORK_PARTITION.",
    },
    {
        "requirement_id": "FR-4.4",
        "category": "FR",
        "requirement_text": "Persist fault detection.",
        "srs_reference": "SRS FR-4.4",
        "implementation": "Every positive detection is persisted to SentinelGridAnomalyEvents by the AnomalyDetector Lambda.",
        "source_files": ["lambda_functions/anomaly_detector/event_store.py", "lambda_functions/anomaly_detector/handler.py"],
        "aws_resources": ["SentinelGridAnomalyEvents"],
        "validation_steps": ["Step 11", "Step 23", "Step 27", "Step 46"],
        "evidence": ["docs/step46_fault_validation_results.json (anomaly_persisted: true for every positive case)"],
        "status": "DEMONSTRATED",
        "limitations": [],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "FR-5.1",
        "category": "FR",
        "requirement_text": "Execute recovery action.",
        "srs_reference": "SRS FR-5.1",
        "implementation": "REDUCE_LOAD and RESET_SENSOR_STATE actions executed successfully by the RemediationExecutor Lambda for real detected faults.",
        "source_files": ["lambda_functions/remediation/executor.py", "remediation/engine.py"],
        "aws_resources": ["SentinelGridRemediationExecutor"],
        "validation_steps": ["Step 19", "Step 46"],
        "evidence": ["docs/step46_fault_validation_results.json (test cases A, A2, C, C2: SUCCESS)"],
        "status": "DEMONSTRATED",
        "limitations": [],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "FR-5.2",
        "category": "FR",
        "requirement_text": "Use Step Functions for recovery orchestration.",
        "srs_reference": "SRS FR-5.2",
        "implementation": "SentinelGridRemediationWorkflow (AWS Step Functions Standard workflow) orchestrates Plan -> Execute -> Verify, deployed and repeatedly exercised.",
        "source_files": ["step_functions/remediation_workflow.json", "infrastructure/remediation-workflow.yaml"],
        "aws_resources": ["SentinelGridRemediationWorkflow"],
        "validation_steps": ["Step 15", "Step 18", "Step 19", "Step 46"],
        "evidence": ["docs/step49_cost_resource_audit_results.json (26 executions, all SUCCEEDED)"],
        "status": "DEMONSTRATED",
        "limitations": [],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "FR-5.3",
        "category": "FR",
        "requirement_text": "Lambda executes/updates recovery state.",
        "srs_reference": "SRS FR-5.3",
        "implementation": "Planner/Executor/Verifier Lambdas each update SentinelGridNodeState as the workflow progresses (HEALTHY -> FAULT_DETECTED -> RECOVERING -> RECOVERED/ESCALATION_REQUIRED).",
        "source_files": ["lambda_functions/remediation/planner.py", "lambda_functions/remediation/executor.py", "lambda_functions/remediation/verifier.py"],
        "aws_resources": ["SentinelGridRemediationPlanner", "SentinelGridRemediationExecutor", "SentinelGridRemediationVerifier", "SentinelGridNodeState"],
        "validation_steps": ["Step 17", "Step 34", "Step 46"],
        "evidence": ["docs/step46_fault_validation.md (section 7.1, NodeState transition findings)"],
        "status": "DEMONSTRATED",
        "limitations": [
            "Transient FAULT_DETECTED/RECOVERING states can be short-lived because the pipeline is fast; "
            "Step 34/46 verified final state rather than capturing every transient intermediate state.",
        ],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "FR-5.4",
        "category": "FR",
        "requirement_text": "Retry failed recovery up to configurable maximum and escalate.",
        "srs_reference": "SRS FR-5.4",
        "implementation": (
            "STEP 51 UPDATE: implemented. step_functions/remediation_workflow.json now has a bounded, "
            "configurable retry loop: an optional top-level max_retries field on the workflow input "
            "(default 2, bounds [0, 5], invalid/missing values fall back to the default) is validated "
            "via a Choice+Pass sequence (ValidateMaxRetries/UseProvidedMaxRetries/UseDefaultMaxRetries), "
            "and a shared CheckRetryLimit Choice (using the native NumericLessThanPath operator -- "
            "AWS Step Functions' Retry.MaxAttempts cannot take a runtime JSONPath value) plus an "
            "IncrementRetryCount Pass (using the States.MathAdd intrinsic function) retries "
            "ExecuteRemediation up to max_retries times before escalating. A second, independently "
            "discovered gap was fixed in the same change: the ASL previously never checked "
            "RemediationResult.success at all (proceeding from ExecuteRemediation straight to "
            "VerifyRemediation regardless) -- a new CheckExecutionSuccess Choice closes this. "
            "See docs/step51_remediation_retry.md for the full design, counter-semantics proof, and "
            "validation evidence."
        ),
        "source_files": ["step_functions/remediation_workflow.json", "tests/test_step_functions_workflow.py", "scripts/step51_live_retry_validation.py"],
        "aws_resources": ["SentinelGridRemediationWorkflow"],
        "validation_steps": ["Step 15", "Step 18", "Step 51"],
        "evidence": [
            "docs/step51_remediation_retry.md",
            "docs/step51_retry_validation_results.json (live Test A: success-on-first-attempt, confirmed valid deployed ASL syntax)",
            "tests/test_step_functions_workflow.py (46 tests, 24 Step-51-specific covering max_retries=0..5, success-after-retry, exhaustion, 7 malformed/invalid cases)",
        ],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "The retry mechanism is implemented, deployed, and exhaustively validated locally (24 dedicated "
            "tests), and confirmed live to be valid AWS Step Functions syntax that does not interfere with "
            "the successful path (Test A) -- but the core 'retry an actually-failing recovery attempt' "
            "behavior was never observed live (Tests B/C), because RemediationExecutor.execute() is a "
            "deterministic pure function that always succeeds for any real automatic plan -- there is no "
            "natural live trigger for a genuine recovery failure without modifying application code or "
            "manufacturing an artificial failure hook, both explicitly out of scope for Step 51.",
            "Planner failures remain unhandled by any retry/escalation logic -- a pre-existing gap, "
            "explicitly out of scope for this requirement (FR-5.4 concerns retrying RECOVERY, not planning).",
            "RecoverySuccessCount can over-count relative to the final outcome under a verification-triggered "
            "retry sequence (documented, not fixed -- see docs/step51_remediation_retry.md section 12).",
        ],
        "deviation": None,
        "follow_up": "If a live-observed retry/escalation-after-retries demonstration is required, it would need either a genuine AWS-level transient fault (not reproducible on demand) or a deliberate, explicitly-scoped future step authorizing a temporary application-level failure hook.",
    },
    {
        "requirement_id": "FR-5.5",
        "category": "FR",
        "requirement_text": "Record recovery outcome/time.",
        "srs_reference": "SRS FR-5.5",
        "implementation": "SentinelGridRecoveryEvents persists action, automatic flag, outcome, and timestamps for every remediation attempt (SUCCESS and ESCALATION_REQUIRED alike).",
        "source_files": ["lambda_functions/remediation/recovery_event_store.py"],
        "aws_resources": ["SentinelGridRecoveryEvents"],
        "validation_steps": ["Step 33", "Step 46"],
        "evidence": ["docs/step46_fault_validation_results.json (recovery_persisted: true for every case)"],
        "status": "DEMONSTRATED",
        "limitations": [],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "FR-6.1",
        "category": "FR",
        "requirement_text": "Persist current node state in DynamoDB.",
        "srs_reference": "SRS FR-6.1",
        "implementation": "SentinelGridNodeState holds one item per node_id, reflecting current status (HEALTHY/FAULT_DETECTED/RECOVERING/RECOVERED/ESCALATION_REQUIRED/UNKNOWN-reserved) with stale-update protection.",
        "source_files": ["shared/node_state_store.py"],
        "aws_resources": ["SentinelGridNodeState"],
        "validation_steps": ["Step 34"],
        "evidence": ["docs/step46_fault_validation_results.json (final_node_state per test case)"],
        "status": "DEMONSTRATED",
        "limitations": [],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "FR-6.2",
        "category": "FR",
        "requirement_text": "Persist fault/recovery history.",
        "srs_reference": "SRS FR-6.2",
        "implementation": "SentinelGridAnomalyEvents and SentinelGridRecoveryEvents persist an append-only history of every detection and remediation attempt.",
        "source_files": ["lambda_functions/anomaly_detector/event_store.py", "lambda_functions/remediation/recovery_event_store.py"],
        "aws_resources": ["SentinelGridAnomalyEvents", "SentinelGridRecoveryEvents"],
        "validation_steps": ["Step 11", "Step 33"],
        "evidence": ["docs/step49_cost_resource_audit_results.json (21 AnomalyEvents, 21 RecoveryEvents accumulated)"],
        "status": "DEMONSTRATED",
        "limitations": [],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "FR-6.3",
        "category": "FR",
        "requirement_text": "Provide a query interface.",
        "srs_reference": "SRS FR-6.3",
        "implementation": (
            "A public HTTP API (API Gateway + SentinelGridKnowledgeQuery Lambda) exposes read-only "
            "routes: GET /nodes, /nodes/{node_id}, /nodes/{node_id}/summary, "
            "/nodes/{node_id}/anomalies, /nodes/{node_id}/recoveries, /events, /metrics -- live-tested "
            "and integrated into the frontend dashboard/events/analytics pages."
        ),
        "source_files": ["lambda_functions/knowledge_query/handler.py", "lambda_functions/knowledge_query/query_service.py", "infrastructure/knowledge-query.yaml"],
        "aws_resources": ["SentinelGridKnowledgeQuery (API ID m1uqr1hsq3)"],
        "validation_steps": ["Step 35", "Step 40", "Step 41", "Step 42", "Step 43"],
        "evidence": ["docs/step49_cost_resource_audit_results.json (api_gateway: 7 routes)"],
        "status": "DEMONSTRATED",
        "limitations": [
            "The API is public and unauthenticated (documented separately under NFR-5 and Step 47's security audit) -- "
            "this does not diminish query-interface functionality, but is a real deployment security limitation.",
        ],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "FR-7.1",
        "category": "FR",
        "requirement_text": "Publish MTTR/detection accuracy/false-positive metrics to CloudWatch.",
        "srs_reference": "SRS FR-7.1",
        "implementation": (
            "shared/metrics.py defines and publishes exactly 7 custom CloudWatch metrics: "
            "FaultDetectionCount, RecoveryAttemptCount, RecoverySuccessCount, RecoveryFailureCount, "
            "UnresolvedFaultCount, DetectionLatency, RecoveryLatency. None of these is literally "
            "named/computed as MTTR, detection-accuracy, or false-positive-rate. RecoveryLatency "
            "measures detection-timestamp-to-executor-completion, which is explicitly NOT pure MTTR."
        ),
        "source_files": ["shared/metrics.py"],
        "aws_resources": ["CloudWatch namespace SentinelGrid"],
        "validation_steps": ["Step 28", "Step 29"],
        "evidence": ["shared/metrics.py (class MetricName, 7 fixed metric names)", "docs/step49_cost_resource_audit_results.json (cloudwatch.custom_metric_names)"],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "Real, live-validated operational metrics exist and are published, but the SPECIFIC named metrics "
            "the requirement literally lists (MTTR, detection accuracy, false-positive rate) are not among "
            "them -- none is computed or published anywhere in this codebase.",
        ],
        "deviation": None,
        "follow_up": "Compute and publish explicit MTTR/detection-accuracy/false-positive-rate metrics if the SRS's literal metric names must be satisfied, using the ground-truth methodology established in Step 46 as a starting point.",
    },
    {
        "requirement_id": "FR-7.2",
        "category": "FR",
        "requirement_text": "CloudWatch dashboard.",
        "srs_reference": "SRS FR-7.2",
        "implementation": "SentinelGrid-Observability dashboard deployed, showing detection count/latency, recovery outcomes/latency, unresolved/escalated faults, and subsystem views.",
        "source_files": ["infrastructure/observability-dashboard.yaml"],
        "aws_resources": ["SentinelGrid-Observability dashboard"],
        "validation_steps": ["Step 30"],
        "evidence": ["docs/step49_cost_resource_audit_results.json (cloudwatch.dashboards)"],
        "status": "DEMONSTRATED",
        "limitations": [],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "FR-7.3",
        "category": "FR",
        "requirement_text": "Alarm/SNS escalation for unresolved faults.",
        "srs_reference": "SRS FR-7.3",
        "implementation": (
            "SentinelGrid-UnresolvedFaults-Alarm (CloudWatch Alarm on UnresolvedFaultCount) with an "
            "SNS action to SentinelGrid-UnresolvedFaults, validated end-to-end via a controlled "
            "synthetic HIGH-severity DetectionResult driving the alarm OK->ALARM transition and "
            "confirming the SNS action fired, with no manual SNS publish."
        ),
        "source_files": ["infrastructure/observability-alerting.yaml"],
        "aws_resources": ["SentinelGrid-UnresolvedFaults-Alarm", "SentinelGrid-UnresolvedFaults (SNS topic)"],
        "validation_steps": ["Step 31", "Step 32"],
        "evidence": ["docs/step49_cost_resource_audit_results.json (cloudwatch.alarms, sns)"],
        "status": "DEMONSTRATED",
        "limitations": [
            "Step 32's validation exercised the escalation/control-plane chain via a controlled synthetic "
            "DetectionResult, not a real telemetry-generated HIGH-severity overload (the detector's real "
            "overload band always yields MEDIUM -- see Step 46 Test Case B's own control-plane framing). "
            "The alarm/SNS mechanism itself is demonstrated; a naturally-occurring HIGH-severity trigger is not.",
        ],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "NFR-1",
        "category": "NFR",
        "requirement_text": "Detection within 10 seconds at least 95% of the time.",
        "srs_reference": "SRS NFR-1",
        "implementation": "Detection latency is measured end-to-end (publish to persisted AnomalyEvent) via the DetectionLatency metric and Step 36's dedicated performance test.",
        "source_files": ["shared/metrics.py", "scripts/step36_live_performance_test.py"],
        "aws_resources": ["SentinelGridAnomalyDetector"],
        "validation_steps": ["Step 36"],
        "evidence": ["docs/performance-validation.md", "docs/step36_live_test_results.json"],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "Step 36's sample was n=10: 9/10 (90%) met the 10-second threshold, one cold-start outlier failed "
            "it -- the raw sample is BELOW the stated 95% criterion. A post-hoc warm-only subset (n=9) was "
            "100% within 10s, which is encouraging but was not a pre-registered sample and does not itself "
            "constitute statistical demonstration of the 95% criterion.",
            "n=10 is far too small to statistically demonstrate a 95% threshold regardless of outcome.",
        ],
        "deviation": None,
        "follow_up": "Run a substantially larger (n>=100, ideally n>=385 for a 95% confidence interval at typical margins) detection-latency sample, including realistic cold-start conditions, before claiming NFR-1 is demonstrated.",
    },
    {
        "requirement_id": "NFR-2",
        "category": "NFR",
        "requirement_text": "Recovery within 30 seconds for 90% of cases.",
        "srs_reference": "SRS NFR-2",
        "implementation": "Recovery latency (detection-to-verified-recovery) measured in the same Step 36 performance test.",
        "source_files": ["shared/metrics.py", "scripts/step36_live_performance_test.py"],
        "aws_resources": ["SentinelGridRemediationWorkflow"],
        "validation_steps": ["Step 36"],
        "evidence": ["docs/performance-validation.md", "docs/step36_live_test_results.json"],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "Step 36's observed sample (n=10) was 10/10 (100%) within 30 seconds, which meets the stated "
            "90% criterion for that specific sample -- but n=10 is statistically insufficient to demonstrate "
            "a 90%-of-cases population claim with any meaningful confidence.",
        ],
        "deviation": None,
        "follow_up": "Same as NFR-1: run a substantially larger recovery-latency sample before claiming statistical demonstration.",
    },
    {
        "requirement_id": "NFR-3",
        "category": "NFR",
        "requirement_text": "Support 500 virtual nodes.",
        "srs_reference": "SRS NFR-3",
        "implementation": (
            "A controlled 500-node / 6,000-telemetry-message workload was published end-to-end after "
            "the Lambda account concurrency quota was raised to 400: 6,000/6,000 telemetry processed, "
            "0 missing, 0 duplicates, 500/500 NodeState records healthy, 0 consumer/analyzer errors, "
            "0 throttles, queue drained in approximately <=21 seconds, no DLQ messages."
        ),
        "source_files": ["scripts/step37_scale_test.py", "config/scale_test_config_step38.yaml"],
        "aws_resources": ["SentinelGridTelemetryQueue", "SentinelGridSqsTelemetryConsumer", "SentinelGridAnomalyDetector", "SentinelGridNodeState"],
        "validation_steps": ["Step 37", "Step 38"],
        "evidence": ["docs/scale-validation.md", "docs/step38_scale_test_results.json"],
        "status": "DEMONSTRATED",
        "limitations": [
            "Demonstrated for the specific tested workload only: a single ~ minutes-long burst of 500 nodes "
            "/ 6,000 messages. Not demonstrated: sustained multi-hour operation at this scale, or fault "
            "injection exercised concurrently at 500-node scale (fault testing in Step 46 used 1-node "
            "isolated configurations, not the full 500-node fleet).",
        ],
        "deviation": None,
        "follow_up": "If sustained or fault-under-load 500-node behavior must be demonstrated, run a dedicated long-duration or fault-during-scale test.",
    },
    {
        "requirement_id": "NFR-4",
        "category": "NFR",
        "requirement_text": "No history loss.",
        "srs_reference": "SRS NFR-4",
        "implementation": (
            "DynamoDB persistence with SQS's redrive/DLQ safety net; Step 38's 500-node test observed "
            "0 missing telemetry messages and 0 DLQ messages; Step 27's E2E test observed exactly-once "
            "processing with no duplicates or drops."
        ),
        "source_files": ["lambda_functions/sqs_consumer/handler.py", "shared/node_state_store.py"],
        "aws_resources": ["SentinelGridTelemetry", "SentinelGridAnomalyEvents", "SentinelGridRecoveryEvents", "SentinelGridTelemetryDLQ"],
        "validation_steps": ["Step 27", "Step 38"],
        "evidence": ["docs/step38_scale_test_results.json", "docs/scale-validation.md"],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "What was actually validated: zero message loss during the specific tested workloads (Steps 27, 38). "
            "This is not a permanent, absolute guarantee -- none of the 4 DynamoDB tables has Point-in-Time "
            "Recovery or deletion protection enabled (Step 47 finding F4), so a table deletion, a bulk-delete "
            "bug, or an account-level incident could still lose history with nothing to recover it.",
            "No long-duration soak test has been run to rule out slow, low-rate loss over time.",
        ],
        "deviation": None,
        "follow_up": "Enable DynamoDB PITR (Step 47's own recommendation) to convert 'observed zero loss' into a real recoverability guarantee.",
    },
    {
        "requirement_id": "NFR-5",
        "category": "NFR",
        "requirement_text": "TLS + least privilege + KMS.",
        "srs_reference": "SRS NFR-5",
        "implementation": (
            "TLS: all AWS API calls (IoT Data Plane, DynamoDB, SQS, etc.) use the AWS SDK's default "
            "HTTPS/TLS transport. Least privilege: Step 47's audit confirmed all 10 service-execution "
            "roles are scoped to exact table/queue/function/log-group ARNs, with 5 documented, mostly-"
            "unavoidable CloudWatch-metric-API wildcard exceptions. KMS: 2 of 4 DynamoDB tables "
            "(NodeState, RecoveryEvents) use explicit AWS-managed-KMS-key SSE; the other 2 "
            "(Telemetry, AnomalyEvents) rely on DynamoDB's default AWS-owned-key encryption, not a "
            "customer/AWS-managed KMS key."
        ),
        "source_files": [],
        "aws_resources": ["all SentinelGrid IAM roles", "SentinelGridNodeState", "SentinelGridRecoveryEvents"],
        "validation_steps": ["Step 47"],
        "evidence": ["docs/security-audit.md"],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "TLS reliance is on AWS SDK default behavior, not independently packet-verified in this project.",
            "Least privilege has 5 documented wildcard exceptions (4 mitigated via a namespace condition, "
            "1 -- GetMetricData -- unmitigated due to an AWS API constraint) and the root-account-usage "
            "finding (Step 47 F1, Step 48) means the OPERATING identity for the project itself is not "
            "least-privilege, even though the deployed service roles are.",
            "KMS is not uniform across all 4 tables -- 2 rely on default encryption, not an explicit KMS key.",
            "The API Gateway endpoint is public and unauthenticated (Step 47 F3), which is a separate but "
            "related access-control gap.",
        ],
        "deviation": "root_account_operations",
        "follow_up": "Complete Step 48's Phase 6 (operator identity authentication) to remove root from routine operations; consider explicit KMS SSE on the remaining 2 tables for consistency.",
    },
    {
        "requirement_id": "NFR-6",
        "category": "NFR",
        "requirement_text": "Independently deployable MAPE-K components.",
        "srs_reference": "SRS NFR-6",
        "implementation": (
            "Each MAPE-K-aligned component is its own CloudFormation stack with its own deploy script: "
            "telemetry-storage, telemetry-sqs-queue, sqs-consumer, anomaly-detector, "
            "anomaly-detection-rule, anomaly-events-storage, remediation-lambdas, remediation-trigger, "
            "remediation-workflow, recovery-events-storage, node-state-storage, knowledge-query, "
            "observability-dashboard, observability-alerting -- 14+ independently deployable stacks "
            "confirmed live in Step 49's inventory, each updatable/redeployable without touching the others."
        ),
        "source_files": ["infrastructure/"],
        "aws_resources": ["all SentinelGrid CloudFormation stacks"],
        "validation_steps": ["Step 49"],
        "evidence": ["docs/cost-resource-audit.md (section 1, CloudFormation stack inventory)"],
        "status": "DEMONSTRATED",
        "limitations": [],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "NFR-7",
        "category": "NFR",
        "requirement_text": "Simulation/fault injection decoupled from AWS SDK.",
        "srs_reference": "SRS NFR-7",
        "implementation": (
            "Directly re-verified in this step via import-level inspection: fault_injection/*.py has "
            "ZERO boto3/aws references anywhere. simulator/'s core (city_simulator.py, node.py, "
            "subsystems/) also has zero AWS coupling; only simulator/aws_publisher.py depends on boto3, "
            "and simulator/__main__.py imports it via a deferred, function-local import guarded by "
            "publisher.mode == 'aws_iot', so a local-only simulator run never imports boto3 at all."
        ),
        "source_files": ["fault_injection/", "simulator/city_simulator.py", "simulator/node.py", "simulator/subsystems/", "simulator/publisher.py", "simulator/__main__.py"],
        "aws_resources": [],
        "validation_steps": ["Step 2", "Step 3", "Step 50 (re-verified via direct import grep)"],
        "evidence": ["grep for boto3/aws across fault_injection/*.py (zero matches) and simulator/*.py (isolated to aws_publisher.py + a deferred import in __main__.py)"],
        "status": "DEMONSTRATED",
        "limitations": [],
        "deviation": None,
        "follow_up": None,
    },
    {
        "requirement_id": "NFR-8",
        "category": "NFR",
        "requirement_text": "Clear dashboard.",
        "srs_reference": "SRS NFR-8",
        "implementation": (
            "Two dashboard surfaces exist: (1) the CloudWatch SentinelGrid-Observability dashboard "
            "(Step 30, live AWS resource), and (2) a React frontend with dashboard/events/analytics "
            "pages integrated with live NodeState/anomaly/recovery/metrics data (Steps 39-44)."
        ),
        "source_files": ["infrastructure/observability-dashboard.yaml", "frontend/"],
        "aws_resources": ["SentinelGrid-Observability dashboard"],
        "validation_steps": ["Step 30", "Step 41", "Step 42", "Step 43", "Step 44"],
        "evidence": ["docs/step49_cost_resource_audit_results.json (cloudwatch.dashboards)"],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "The CloudWatch dashboard is live and demonstrated. The frontend -- arguably the clearer, more "
            "human-facing dashboard for a smart-city demo -- is NOT publicly hosted: Step 45's CloudFront "
            "deployment remains blocked by an account-verification restriction (reproduced twice). The "
            "frontend was validated only against a local dev server, not as a deployed public artifact.",
        ],
        "deviation": "cloudfront_blocked",
        "follow_up": "Resolve the CloudFront account-verification restriction with AWS Support (not retried in this or Step 45's second attempt) to complete public frontend hosting.",
    },
    {
        "requirement_id": "NFR-9",
        "category": "NFR",
        "requirement_text": "Low cost / AWS Free Tier or low-cost student constraints.",
        "srs_reference": "SRS NFR-9",
        "implementation": (
            "Real AWS Cost Explorer data shows $0.00 unblended cost across every AWS service for the "
            "complete prior month and current month-to-date. Configuration-level review confirms "
            "low-cost choices throughout: DynamoDB 100% on-demand billing, Lambda minimum memory tier "
            "with no reserved/provisioned concurrency, empty SQS queues, no orphaned billable resources."
        ),
        "source_files": [],
        "aws_resources": ["all SentinelGrid AWS resources"],
        "validation_steps": ["Step 49"],
        "evidence": ["docs/cost-resource-audit.md (section 13, actual Cost Explorer billing data)", "docs/step49_cost_resource_audit_results.json (cost_explorer)"],
        "status": "DEMONSTRATED",
        "limitations": [
            "Demonstrated for the project's life to date under its actual (light, intermittent) usage pattern. "
            "Two unbounded-growth mechanisms exist that are not yet cost-bounded structurally (unthrottled "
            "public API Gateway, 'Never Expire' CloudWatch log retention, no S3 version-lifecycle rule) -- "
            "currently negligible in absolute terms, but the guarantee of continued low cost depends on "
            "either continued low usage or a future step adding those bounds.",
        ],
        "deviation": None,
        "follow_up": "Add API Gateway stage throttling and CloudWatch log/S3 version retention policies, per Step 49's own recommended next step, to make low cost structurally guaranteed rather than usage-dependent.",
    },
    {
        "requirement_id": "METRIC-MTTR",
        "category": "METRIC",
        "requirement_text": "Mean Time To Recovery (MTTR).",
        "srs_reference": "SRS evaluation metrics",
        "implementation": (
            "No explicit MTTR aggregate metric is computed or published anywhere in this codebase. "
            "The closest related metric, RecoveryLatency (detection timestamp -> executor completion), "
            "is explicitly documented (Step 28-29) as NOT equivalent to MTTR without qualification."
        ),
        "source_files": ["shared/metrics.py"],
        "aws_resources": ["CloudWatch namespace SentinelGrid"],
        "validation_steps": ["Step 28"],
        "evidence": ["shared/metrics.py (MetricName class has no MTTR entry)"],
        "status": "NOT DEMONSTRATED",
        "limitations": [
            "RecoveryLatency data exists and could be aggregated into an MTTR-like figure, but no such "
            "aggregate computation or publication currently exists.",
        ],
        "deviation": None,
        "follow_up": "Add an explicit MTTR aggregate (e.g. a CloudWatch metric math expression or a Knowledge Query API endpoint computing mean RecoveryLatency over a window) if this literal evaluation metric must be reported.",
    },
    {
        "requirement_id": "METRIC-DETECTION-ACCURACY",
        "category": "METRIC",
        "requirement_text": "Fault-detection accuracy.",
        "srs_reference": "SRS evaluation metrics",
        "implementation": (
            "Step 46's ground-truth ground-truth methodology (independently computing expected detection "
            "via the same detect_telemetry() function, then comparing to the AWS-observed result) found "
            "4/4 correctly classified for the fault cases exercised."
        ),
        "source_files": ["lambda_functions/anomaly_detector/detector.py"],
        "aws_resources": ["SentinelGridAnomalyDetector"],
        "validation_steps": ["Step 46"],
        "evidence": ["docs/step46_fault_validation_results.json (detection_summary)"],
        "status": "PARTIALLY DEMONSTRATED",
        "limitations": [
            "n=4 is a genuine ground-truth functional sample, not a statistically meaningful accuracy "
            "computation. No aggregate 'accuracy' metric/percentage is computed or published as an "
            "ongoing system metric -- this is a one-time manual test result, not a continuous measurement.",
        ],
        "deviation": None,
        "follow_up": "Expand the ground-truth sample substantially and/or implement continuous accuracy tracking if a statistically defensible accuracy figure is required.",
    },
    {
        "requirement_id": "METRIC-FALSE-POSITIVE-RATE",
        "category": "METRIC",
        "requirement_text": "False-positive rate.",
        "srs_reference": "SRS evaluation metrics",
        "implementation": (
            "Step 46 explicitly recorded false-positive status per test case but concluded, in its own "
            "words, that 'false-positive rate not statistically demonstrated' given the small, "
            "non-representative sample. No aggregate false-positive-rate metric is computed or published."
        ),
        "source_files": ["lambda_functions/anomaly_detector/detector.py"],
        "aws_resources": ["SentinelGridAnomalyDetector"],
        "validation_steps": ["Step 46"],
        "evidence": ["docs/step46_fault_validation_results.json (false_positive_summary)", "docs/step46_fault_validation.md"],
        "status": "NOT DEMONSTRATED",
        "limitations": [
            "The sample size and composition (deliberately fault-heavy, not representative of steady-state "
            "normal-telemetry volume) makes any false-positive-rate figure from Step 46 unrepresentative "
            "even if computed.",
        ],
        "deviation": None,
        "follow_up": "Run a large, representative (majority-normal-telemetry) sample with ground-truth labeling to compute a defensible false-positive rate.",
    },
]

DEVIATIONS = [
    {
        "deviation_id": "kinesis_to_sqs",
        "title": "Kinesis Data Stream substituted with SQS for telemetry ingestion",
        "original_expectation": "SRS FR-3.1 specifies AWS IoT Core -> Amazon Kinesis Data Stream for real-time telemetry ingestion.",
        "implemented_alternative": "AWS IoT Core -> Amazon SQS (Standard queue) -> SQS Consumer Lambda -> Anomaly Detector Lambda.",
        "reason": (
            "Kinesis deployment was attempted in Step 24 and rejected by AWS with a "
            "SubscriptionRequiredException at the account level -- this AWS account is not "
            "subscribed to the Kinesis service at all. Independently re-confirmed live in Step 49 "
            "(the orphaned Kinesis CloudFormation stack's ListStreams call returns the identical error)."
        ),
        "impact": (
            "Affects FR-3.1 (not literally satisfied), FR-3.2 (retention semantics differ: SQS's "
            "4-day default vs. the SRS's stated 24-hour default, and no retention at all on the "
            "durable DynamoDB history), and FR-3.3 (subsystem is data, not a structural partition "
            "key, since SQS Standard has no Kinesis-style partition-key/shard model)."
        ),
        "affects_functional_behavior": (
            "No -- the end-to-end self-healing pipeline (detection through remediation) functions "
            "correctly on the SQS-based path, extensively validated (Steps 27, 38, 46). The deviation "
            "is at the ingestion-mechanism level, not the overall system's functional correctness."
        ),
        "approval_status": "Documented Project Deviation (no formal approval record exists in the repository)",
        "remains_open": "Effectively permanent within this AWS account (the subscription restriction is account-level, not something this project can resolve); would only be revisited if a different AWS account became available.",
    },
    {
        "deviation_id": "cloudfront_blocked",
        "title": "Public frontend hosting via CloudFront blocked",
        "original_expectation": "A publicly accessible frontend dashboard, deployable via the existing infrastructure/frontend-hosting.yaml (S3 + CloudFront + OAC).",
        "implemented_alternative": "Frontend exists and is fully integrated with live backend data (Steps 39-44) but runs only against a local dev server -- no public URL exists.",
        "reason": (
            "AWS CloudFront returned an account-verification restriction ('Your account must be "
            "verified before you can add new CloudFront resources') on the first deployment attempt "
            "(Step 45) and again, identically, on a second genuine attempt after AWS Support reportedly "
            "cleared the restriction (Step 45 resumption) -- reproduced twice, not a transient/stale error."
        ),
        "impact": "Affects NFR-8 (dashboard clarity as a publicly demonstrable artifact) and overall demo accessibility; does not affect backend functional correctness.",
        "affects_functional_behavior": "No -- this is purely a hosting/distribution limitation for the already-complete frontend.",
        "approval_status": "Documented Project Deviation (AWS-imposed restriction, not a deliberate project choice)",
        "remains_open": "Yes -- open pending AWS Support resolution; not retried per this and prior steps' explicit instructions.",
    },
    {
        "deviation_id": "root_account_operations",
        "title": "AWS root identity remains the operating identity for project operations",
        "original_expectation": "NFR-5's least-privilege principle implies routine project operations should use a scoped, non-root identity.",
        "implemented_alternative": (
            "A dedicated least-privilege IAM role (SentinelGridOperatorRole) was created (Step 48), but "
            "the human-authentication path into it remains incomplete: AWS unconditionally disallows the "
            "root user from calling sts:AssumeRole, this account has no IAM Identity Center / Organizations "
            "instance, and the remaining automatable option (a minimal IAM user + one access key) was "
            "explicitly deferred to the account owner's decision rather than created automatically."
        ),
        "reason": "Account-level constraint (no SSO) combined with an AWS-wide restriction (root cannot assume roles), plus a deliberate choice to not auto-create a long-lived credential without explicit owner approval.",
        "impact": "Affects NFR-5 (the OPERATING identity, not the deployed service-execution roles, which are already least-privilege per Step 47).",
        "affects_functional_behavior": "No -- the deployed system's runtime behavior is entirely governed by its own service-execution roles, unaffected by which identity performs deployment/maintenance operations.",
        "approval_status": "Documented Project Deviation (the account owner was presented with the choice in Step 48 and chose to stop rather than proceed)",
        "remains_open": "Yes -- open pending an account-owner decision between the two documented completion paths (docs/operator-identity.md section 7).",
    },
    {
        "deviation_id": "no_configurable_retry",
        "title": "Remediation workflow escalates immediately on any failure rather than retrying up to a configurable maximum",
        "original_expectation": "SRS FR-5.4: retry failed recovery up to a configurable maximum, then escalate.",
        "implemented_alternative": (
            "The deployed Step Functions ASL (step_functions/remediation_workflow.json) uses a single "
            "Catch block (ErrorEquals: States.ALL) on ExecuteRemediation and VerifyRemediation that "
            "routes directly to the terminal Escalate state on the first failure -- no Retry block, "
            "no attempt counter, no configurable maximum exists anywhere in the ASL or the application code."
        ),
        "reason": (
            "Not documented anywhere in the repository as a deliberate, explained trade-off -- discovered "
            "by direct inspection of the deployed ASL during this traceability step. Likely a simplification "
            "made during the Step 15-18 workflow design that was never revisited against the literal SRS wording."
        ),
        "impact": "FR-5.4 is not literally satisfied -- see that requirement's record for detail.",
        "affects_functional_behavior": (
            "Yes, in a narrow sense: a remediation attempt that fails once escalates immediately rather "
            "than being retried, which is a materially different operational behavior than the SRS specifies. "
            "It does not affect the correctness of the escalation path itself, which works as designed."
        ),
        "approval_status": "Documented Project Deviation (no approval evidence found; discovered during this traceability pass, not previously flagged in any prior step's report)",
        "remains_open": (
            "RESOLVED in Step 51: a bounded, configurable retry mechanism (Choice+Pass counter loop, "
            "default 2 / bounds [0,5]) was implemented and deployed, closing this deviation. See "
            "docs/step51_remediation_retry.md. Retained here as a historical record of the gap and its "
            "resolution, not deleted, per this project's convention of not hiding decision history."
        ),
    },
]


def build_results() -> dict:
    for r in REQUIREMENTS:
        if r["status"] not in ALLOWED_STATUSES:
            raise ValueError(f"Requirement {r['requirement_id']} has disallowed status {r['status']!r}")
    ids = [r["requirement_id"] for r in REQUIREMENTS]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate requirement_id found")

    counts = {status: 0 for status in ALLOWED_STATUSES}
    for r in REQUIREMENTS:
        counts[r["status"]] += 1

    return {
        "srs_source": (
            "No standalone SRS document file exists in the repository "
            "(/Users/arthita/Documents/SentinelGrid). The requirement IDs/wording used here are the "
            "SRS content established in this project's own conversation record (BCSE355L SentinelGrid "
            "SRS v1.0), reproduced as given for FR-3 through FR-7 and NFR-1 through NFR-9. FR-1/FR-2 "
            "are represented at the architecture-component level only, since granular numbered "
            "sub-clause text for those two sections was not available to this pass -- see each "
            "record's limitations field."
        ),
        "requirements": REQUIREMENTS,
        "deviations": DEVIATIONS,
        "summary_counts": counts,
        "total_requirements": len(REQUIREMENTS),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=str, default=str(REPO_ROOT / "docs" / "step50_srs_traceability_results.json"))
    args = parser.parse_args()

    results = build_results()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"Traceability evidence written to: {output_path}")
    print(f"Total requirements: {results['total_requirements']}")
    print(f"Summary counts: {results['summary_counts']}")


if __name__ == "__main__":
    main()
