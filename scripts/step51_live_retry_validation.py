#!/usr/bin/env python3
"""Step 51 — live validation of the deployed configurable-retry ASL
(FR-5.4).

Performs exactly ONE controlled action: starts a single Step Functions
execution against the already-deployed SentinelGridRemediationWorkflow
with a synthetic, hand-crafted DetectionResult (NODE_OVERLOAD, MEDIUM
severity, automatic remediation) plus an explicit max_retries=2 field —
the same direct-Step-Functions "control-plane" testing pattern already
established in Step 32 and Step 46 Test Case B, used here for the same
reason: there is no natural way to make the real, deterministic
RemediationExecutor.execute() fail for a genuine automatic plan (see
docs/step51_remediation_retry.md "Why Test B/C are not performed live"),
so this script only exercises TEST A (success on the first attempt, no
retry needed) — the one behavior that IS naturally reachable through the
real deployed Lambdas without modifying any application code or
inventing an artificial failure hook.

Never publishes telemetry, never runs the simulator, never touches SQS/
IoT, never mutates any other AWS resource. Reads back the execution
result and history via read-only Step Functions APIs only.

Usage:
    python3 scripts/step51_live_retry_validation.py --output docs/step51_retry_validation_results.json
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import boto3  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError  # noqa: E402

PROFILE = "sentinelgrid"
REGION = "ap-south-1"
STATE_MACHINE_ARN = "arn:aws:states:ap-south-1:401429382000:stateMachine:SentinelGridRemediationWorkflow"


def _session() -> boto3.Session:
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def run_test_a(session: boto3.Session) -> dict:
    sfn = session.client("stepfunctions")

    unique_suffix = uuid.uuid4().hex[:12]
    node_id = f"power-step51-testA-{unique_suffix}"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    execution_name = f"sentinelgrid-step51-testA-{unique_suffix}"

    detection_result = {
        "detected": True,
        "fault_type": "NODE_OVERLOAD",
        "severity": "MEDIUM",
        "node_id": node_id,
        "subsystem": "power",
        "timestamp": timestamp,
        "reason": "Step 51 Test A -- SYNTHETIC control-plane retry-mechanism validation (same direct-Step-Functions pattern as Step 32 / Step 46 Test Case B). Not a real telemetry-triggered detection.",
        "evidence": {"field": "load", "value": 97.5},
        "max_retries": 2,
    }

    start_response = sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=execution_name,
        input=json.dumps(detection_result),
    )
    execution_arn = start_response["executionArn"]

    status = "RUNNING"
    for _ in range(30):
        desc = sfn.describe_execution(executionArn=execution_arn)
        status = desc["status"]
        if status != "RUNNING":
            break
        time.sleep(2)

    history = sfn.get_execution_history(executionArn=execution_arn, maxResults=200, reverseOrder=False)
    visited_states = [
        e["stateEnteredEventDetails"]["name"]
        for e in history["events"]
        if e["type"] == "TaskStateEntered" or e["type"] == "ChoiceStateEntered" or e["type"] == "PassStateEntered"
    ]

    output = json.loads(desc.get("output", "{}")) if desc.get("output") else None

    return {
        "test": "A",
        "description": "NODE_OVERLOAD MEDIUM, max_retries=2, expect success on first attempt, no retry.",
        "node_id": node_id,
        "execution_arn": execution_arn,
        "execution_name": execution_name,
        "final_status": status,
        "visited_states": visited_states,
        "output": output,
        "retry_count_in_output": output.get("retry_count") if isinstance(output, dict) else None,
        "max_retries_in_output": output.get("max_retries") if isinstance(output, dict) else None,
        "verification_verified": output.get("verification", {}).get("verified") if isinstance(output, dict) else None,
        "expected_no_retry": "CheckRetryLimit" not in visited_states and "IncrementRetryCount" not in visited_states,
        "expected_single_execute_attempt": visited_states.count("ExecuteRemediation") == 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=str, default=str(REPO_ROOT / "docs" / "step51_retry_validation_results.json"))
    args = parser.parse_args()

    try:
        session = _session()
        identity = session.client("sts").get_caller_identity()
    except (NoCredentialsError, ClientError, BotoCoreError) as exc:
        print(f"AWS authentication unavailable/expired: {exc}", file=sys.stderr)
        print("Run: aws login --profile sentinelgrid --region ap-south-1", file=sys.stderr)
        sys.exit(2)

    print(f"Authenticated as: {identity['Arn']}")
    print("Running Test A (success on first attempt) — the only live test performed this step...")
    test_a_result = run_test_a(session)

    results = {
        "identity": {"Arn": identity["Arn"], "Account": identity["Account"]},
        "state_machine_arn": STATE_MACHINE_ARN,
        "test_a": test_a_result,
        "test_b_retry_behavior": {
            "performed_live": False,
            "reason": (
                "RemediationExecutor.execute() is a deterministic pure-function simulation that ALWAYS "
                "returns success=True for any real automatic RemediationPlan (confirmed by direct code "
                "inspection of remediation/engine.py's RemediationExecutor.execute() during this step) -- "
                "there is no natural way to make a real automatic plan's execution fail without either "
                "modifying application code or engineering an artificial 'always fail' hook, both explicitly "
                "forbidden for this step. Retry behavior (max_retries=0/1/2, success-after-retry, "
                "failure-after-exhaustion, all bounds/defaulting cases) was instead validated exhaustively "
                "via 24 dedicated local ASL-interpreter tests in tests/test_step_functions_workflow.py -- "
                "see docs/step51_remediation_retry.md."
            ),
        },
        "test_c_escalation_after_retries": {
            "performed_live": False,
            "reason": "Same reason as Test B -- depends on being able to force a live failure, which is not safely possible without changing production behavior.",
        },
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"Results written to: {output_path}")
    print(f"Test A final status: {test_a_result['final_status']}")
    print(f"Test A visited states: {test_a_result['visited_states']}")


if __name__ == "__main__":
    main()
