#!/usr/bin/env python3
"""Step 46 Test Case B — CONTROL-PLANE escalation validation (SRS FR-5).

**This is explicitly NOT an IoT-to-escalation test.** It exists because
the deployed anomaly detector's NODE_OVERLOAD rule
(`lambda_functions/anomaly_detector/detector.py`'s `_check_numeric_field`)
can only ever produce `Severity.MEDIUM` for an overload — the "overload"
numeric band for every subsystem's overloadable field is a fixed range
strictly inside/adjacent to "normal" and disjoint from the
"corruption_above" range that would otherwise map to a HIGH/CRITICAL
DATA_CORRUPTION classification instead. There is no telemetry value, and
therefore no `fault_injection.overload.py` output, that the real detector
will ever classify as `NODE_OVERLOAD` at `HIGH` or `CRITICAL` severity.
Confirmed by direct code inspection before writing this script, per Step
46 Phase 3's explicit instruction: "If the existing implementation makes
a particular fault difficult to exercise correctly, DO NOT invent a new
methodology simply to force it" -- forcing this would mean either
inventing a nonexistent detector rule or fabricating a live IoT
payload the real detector could never produce; neither is acceptable.

Instead, this script invokes the EXISTING, DEPLOYED
`SentinelGridRemediationTrigger` Lambda directly with a hand-crafted
`DetectionResult`-shaped payload (`detected=true`,
`fault_type="NODE_OVERLOAD"`, `severity="HIGH"`) -- exactly the same
wire shape and the same Lambda the real Analyzer already calls for every
genuine detection (see `lambda_functions/remediation_trigger/handler.py`'s
own `_extract_detection_result`/`lambda_handler` docstring: it validates
the shape, evaluates whether to trigger, and calls the REAL, unmodified
`stepfunctions:StartExecution` with this exact input, unwrapped -- the
identical code path a genuine detection would take from this point
onward). This exercises the REAL Planner -> CheckAutomaticRemediation ->
Escalate path end-to-end; only the origin of the input (this script,
not a live telemetry reading through the Analyzer) is synthetic, and
that is stated honestly everywhere this result is reported.

No new AWS resource is created. No existing Lambda/Step Functions code
is modified. This is ONE authorized, explicit, controlled live
invocation of an already-deployed function -- not a loop, not repeated
invocations beyond what this one test case requires.

Usage:
    python3 scripts/step46_escalation_control_plane_test.py \\
        --node-id power-step46-caseB-overload-high-001 \\
        --subsystem power \\
        --output docs/step46_caseB_events.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import boto3  # noqa: E402

PROFILE = "sentinelgrid"
REGION = "ap-south-1"
FUNCTION_NAME = "SentinelGridRemediationTrigger"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--subsystem", required=True, choices=["power", "water", "traffic"])
    parser.add_argument("--output", type=str, required=True)
    args = parser.parse_args()

    now = datetime.now(timezone.utc).isoformat()

    detection_result = {
        "detected": True,
        "fault_type": "NODE_OVERLOAD",
        "severity": "HIGH",
        "node_id": args.node_id,
        "subsystem": args.subsystem,
        "timestamp": now,
        "reason": (
            "Step 46 Test Case B -- SYNTHETIC control-plane escalation validation. "
            "This DetectionResult was hand-crafted, not produced by a real telemetry "
            "reading through the deployed detector (whose real NODE_OVERLOAD rule can "
            "only ever produce MEDIUM severity). It is used to validate the "
            "Planner -> CheckAutomaticRemediation -> Escalate path end-to-end via the "
            "real, unmodified SentinelGridRemediationTrigger -> Step Functions -> "
            "Planner pipeline."
        ),
        "evidence": {"field": "load", "value": 250.0, "note": "synthetic evidence for Step 46 control-plane test"},
    }

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    lambda_client = session.client("lambda")

    invoke_start = datetime.now(timezone.utc).isoformat()
    print(f"Invoking {FUNCTION_NAME} directly with a synthetic HIGH-severity NODE_OVERLOAD DetectionResult...")
    print(json.dumps(detection_result, indent=2))

    response = lambda_client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(detection_result).encode("utf-8"),
    )
    invoke_end = datetime.now(timezone.utc).isoformat()

    payload_bytes = response["Payload"].read()
    try:
        response_payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        response_payload = {"raw": payload_bytes.decode("utf-8", errors="replace")}

    print(f"\nLambda response (StatusCode={response.get('StatusCode')}, FunctionError={response.get('FunctionError')}):")
    print(json.dumps(response_payload, indent=2))

    summary = {
        "test_id": "step46-caseB-control-plane-escalation",
        "test_type": "CONTROL_PLANE_ESCALATION_VALIDATION",
        "not_an_iot_test": True,
        "target_node_id": args.node_id,
        "subsystem": args.subsystem,
        "invoked_function": FUNCTION_NAME,
        "invoke_start": invoke_start,
        "invoke_end": invoke_end,
        "detection_result_sent": detection_result,
        "lambda_status_code": response.get("StatusCode"),
        "lambda_function_error": response.get("FunctionError"),
        "lambda_response_payload": response_payload,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nRun summary written to: {output_path}")


if __name__ == "__main__":
    main()
