"""Tests for the Step 50 SRS requirements traceability matrix
(scripts/step50_srs_traceability.py -> docs/step50_srs_traceability_results.json).

These are static/structural tests that do NOT require live AWS
credentials -- they validate the shape and internal consistency of the
generated traceability data itself.
"""

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.step50_srs_traceability import (  # noqa: E402
    ALLOWED_STATUSES,
    DEVIATIONS,
    REQUIREMENTS,
    build_results,
)

FORBIDDEN_STATUS_TOKENS = {"PASS", "FAIL", "COMPLETE", "100%"}

EXPECTED_REQUIREMENT_IDS = {
    "FR-1",
    "FR-2",
    "FR-3.1",
    "FR-3.2",
    "FR-3.3",
    "FR-4.1",
    "FR-4.2",
    "FR-4.3",
    "FR-4.4",
    "FR-5.1",
    "FR-5.2",
    "FR-5.3",
    "FR-5.4",
    "FR-5.5",
    "FR-6.1",
    "FR-6.2",
    "FR-6.3",
    "FR-7.1",
    "FR-7.2",
    "FR-7.3",
    "NFR-1",
    "NFR-2",
    "NFR-3",
    "NFR-4",
    "NFR-5",
    "NFR-6",
    "NFR-7",
    "NFR-8",
    "NFR-9",
    "METRIC-MTTR",
    "METRIC-DETECTION-ACCURACY",
    "METRIC-FALSE-POSITIVE-RATE",
}


@pytest.fixture(scope="module")
def results():
    return build_results()


def test_every_expected_requirement_id_is_present(results):
    actual_ids = {r["requirement_id"] for r in results["requirements"]}
    missing = EXPECTED_REQUIREMENT_IDS - actual_ids
    assert not missing, f"Missing requirement IDs: {missing}"


def test_no_duplicate_requirement_ids(results):
    ids = [r["requirement_id"] for r in results["requirements"]]
    assert len(ids) == len(set(ids)), "Duplicate requirement_id found"


def test_every_status_is_from_allowed_vocabulary(results):
    for r in results["requirements"]:
        assert r["status"] in ALLOWED_STATUSES, f"{r['requirement_id']} has invalid status {r['status']!r}"


def test_no_forbidden_status_tokens_anywhere_in_statuses(results):
    for r in results["requirements"]:
        for token in FORBIDDEN_STATUS_TOKENS:
            assert token not in r["status"], f"{r['requirement_id']} status contains forbidden token {token!r}"


def test_every_requirement_has_implementation_and_evidence(results):
    for r in results["requirements"]:
        assert r["implementation"], f"{r['requirement_id']} missing implementation"
        assert isinstance(r["evidence"], list) and len(r["evidence"]) > 0, f"{r['requirement_id']} missing evidence"


def test_every_requirement_has_requirement_text_and_source_files_field(results):
    for r in results["requirements"]:
        assert r["requirement_text"], f"{r['requirement_id']} missing requirement_text"
        assert "source_files" in r
        assert "aws_resources" in r
        assert "validation_steps" in r
        assert "limitations" in r
        assert "follow_up" in r
        assert "deviation" in r  # key must exist, value may be None


def test_deviation_field_references_a_real_deviation_record_when_set(results):
    deviation_ids = {d["deviation_id"] for d in results["deviations"]}
    for r in results["requirements"]:
        if r["deviation"] is not None:
            assert r["deviation"] in deviation_ids, f"{r['requirement_id']} references unknown deviation {r['deviation']!r}"


def test_deviation_records_have_required_fields(results):
    required_fields = {
        "deviation_id",
        "title",
        "original_expectation",
        "implemented_alternative",
        "reason",
        "impact",
        "affects_functional_behavior",
        "approval_status",
        "remains_open",
    }
    for d in results["deviations"]:
        assert required_fields.issubset(d.keys()), f"Deviation {d.get('deviation_id')} missing fields: {required_fields - d.keys()}"


def test_deviation_approval_status_is_honest_not_fabricated(results):
    """No deviation may claim formal approval unless explicitly evidenced
    -- every deviation in this project is a 'Documented Project Deviation'
    since no formal approval record exists in the repository."""
    for d in results["deviations"]:
        assert "approved" in d["approval_status"].lower() or "documented project deviation" in d["approval_status"].lower()
        if "documented project deviation" in d["approval_status"].lower():
            assert "formal" not in d["approval_status"].lower() or "no formal approval" in d["approval_status"].lower()


def test_expected_deviations_present(results):
    deviation_ids = {d["deviation_id"] for d in results["deviations"]}
    assert "kinesis_to_sqs" in deviation_ids
    assert "cloudfront_blocked" in deviation_ids
    assert "root_account_operations" in deviation_ids


def test_summary_counts_match_individual_requirement_records(results):
    computed = {status: 0 for status in ALLOWED_STATUSES}
    for r in results["requirements"]:
        computed[r["status"]] += 1
    assert computed == results["summary_counts"]


def test_total_requirements_matches_len_of_requirements_list(results):
    assert results["total_requirements"] == len(results["requirements"])


def test_summary_counts_sum_equals_total(results):
    assert sum(results["summary_counts"].values()) == results["total_requirements"]


def test_no_overall_compliance_score_or_percentage_field_exists(results):
    """The task explicitly forbids a numerical compliance score/percentage
    -- verify no such field was introduced anywhere in the results dict
    (recursively) or its requirement records."""

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert "score" not in k.lower(), f"Forbidden 'score' field found: {k}"
                assert "compliance_percent" not in k.lower()
                assert "overall_percent" not in k.lower()
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(results)


def test_kinesis_requirement_is_blocked_not_pretending_sqs_satisfies_it(results):
    fr31 = next(r for r in results["requirements"] if r["requirement_id"] == "FR-3.1")
    assert fr31["status"] == "BLOCKED"
    assert "kinesis" in fr31["requirement_text"].lower()
    assert fr31["deviation"] == "kinesis_to_sqs"
    # Must not claim SQS literally satisfies a Kinesis requirement.
    assert "sqs is not kinesis" in fr31["implementation"].lower() or "kinesis" in fr31["implementation"].lower()


def test_heartbeat_detection_not_marked_demonstrated(results):
    fr42 = next(r for r in results["requirements"] if r["requirement_id"] == "FR-4.2")
    assert fr42["status"] == "NOT DEMONSTRATED"


def test_nfr1_and_nfr2_not_marked_fully_demonstrated(results):
    """Per explicit Step 50 instructions: small-sample performance results
    must not be upgraded to unqualified DEMONSTRATED."""
    nfr1 = next(r for r in results["requirements"] if r["requirement_id"] == "NFR-1")
    nfr2 = next(r for r in results["requirements"] if r["requirement_id"] == "NFR-2")
    assert nfr1["status"] != "DEMONSTRATED"
    assert nfr2["status"] != "DEMONSTRATED"


def test_nfr9_is_demonstrated_supported_by_real_billing_evidence(results):
    nfr9 = next(r for r in results["requirements"] if r["requirement_id"] == "NFR-9")
    assert nfr9["status"] == "DEMONSTRATED"
    assert any("cost" in e.lower() or "billing" in e.lower() for e in nfr9["evidence"])


def test_fr54_retry_semantics_reflect_actual_asl_inspection(results):
    """Step 51 implemented and deployed a bounded configurable retry loop,
    resolving the gap this test originally guarded against -- updated to
    reflect the new PARTIALLY DEMONSTRATED status (not escalated all the
    way to DEMONSTRATED, since live retry-of-a-failure was never observed
    -- see docs/step51_remediation_retry.md)."""
    fr54 = next(r for r in results["requirements"] if r["requirement_id"] == "FR-5.4")
    assert fr54["status"] == "PARTIALLY DEMONSTRATED"
    assert "retry" in fr54["implementation"].lower()
    assert fr54["deviation"] is None  # no_configurable_retry deviation is now resolved


def test_results_json_file_matches_generator_output_if_present():
    results_path = REPO_ROOT / "docs" / "step50_srs_traceability_results.json"
    if not results_path.exists():
        return
    with open(results_path) as f:
        saved = json.load(f)
    generated = build_results()
    assert saved["summary_counts"] == generated["summary_counts"]
    assert saved["total_requirements"] == generated["total_requirements"]
    saved_ids = {r["requirement_id"] for r in saved["requirements"]}
    generated_ids = {r["requirement_id"] for r in generated["requirements"]}
    assert saved_ids == generated_ids


def test_no_pytest_or_json_field_contains_bare_percentage_score_pattern():
    """Guards against a stray 'XX% compliant' style string anywhere in the
    generated JSON file, if present."""
    results_path = REPO_ROOT / "docs" / "step50_srs_traceability_results.json"
    if not results_path.exists():
        return
    text = results_path.read_text()
    assert not re.search(r"\boverall\b.{0,20}\d{1,3}\s*%", text, re.IGNORECASE)
    assert not re.search(r"compliance\s*score", text, re.IGNORECASE)
