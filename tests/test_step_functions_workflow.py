"""Structural validation tests for the local Step Functions state-machine
definition (step_functions/remediation_workflow.json).

SCOPE NOTE — read before trusting these tests for anything more than they
claim: this file performs (a) static JSON/graph-structure validation, and
(b) a tiny, purpose-built local interpreter (`_trace_workflow`, below)
that supports only the exact state features this one workflow uses
(Choice with BooleanEquals/IsPresent/IsNumeric/NumericGreaterThanEquals/
NumericLessThanEquals/NumericLessThanPath/And/Default, Pass with a
`{"key.$": "$"}`-style Parameters wrap — including literal non-`.$` keys
and the `States.MathAdd(...)` intrinsic function (Step 51) — or a
`Result`+`ResultPath` marker, Task with InputPath/ResultPath/Catch, and
Succeed). It is NOT a general Amazon States Language interpreter and
makes NO claim of AWS-side validity. Passing these tests proves the JSON
parses, the state graph is internally consistent (no dangling/unreachable
states), and that — for the representative sample inputs used here —
this specific workflow's own branching logic reaches the state we
expect. It does **not** prove this definition would be accepted by AWS
Step Functions' own service-side validation or is deployable as-is (it
still contains `${...}`-style CloudFormation substitution placeholders
instead of real Lambda ARNs). That AWS-side validation is explicitly
deferred to a later step (performed live in Step 51 for the retry
feature specifically — see docs/step51_remediation_retry.md), per this
task's instructions.

No AWS SDK is imported anywhere in this file (see
`test_no_aws_imports_in_this_test_module`), and nothing here makes a
network call of any kind.
"""

import ast
import copy
import json
import pathlib
import re

import pytest

WORKFLOW_PATH = pathlib.Path(__file__).resolve().parent.parent / "step_functions" / "remediation_workflow.json"

EXPECTED_STATES = {
    "CheckAnomaly",
    "PlanRemediation",
    "CheckAutomaticRemediation",
    "ExecuteRemediation",
    "VerifyRemediation",
    "Escalate",
    "Success",
    "NoAnomaly",
}

# CheckVerification and PrepareRemediationPlanning are additional,
# deliberately-added supporting states not in the "at minimum" list above
# — see step_functions/README.md for why each is necessary (a Task state
# cannot branch on its own result, and the documented data-flow contract
# nests DetectionResult under a "detection_result" key).

# Step 51 (FR-5.4): the bounded configurable-retry mechanism's own
# supporting states — see docs/step51_remediation_retry.md for the full
# design. Checked separately below rather than folded into
# EXPECTED_STATES so a future reader can tell at a glance which states
# predate Step 51 and which implement retry/escalation specifically.
RETRY_MECHANISM_STATES = {
    "ValidateMaxRetries",
    "UseProvidedMaxRetries",
    "UseDefaultMaxRetries",
    "CheckExecutionSuccess",
    "CheckRetryLimit",
    "IncrementRetryCount",
}

DEFAULT_MAX_RETRIES = 2
MIN_MAX_RETRIES = 0
MAX_MAX_RETRIES = 5


@pytest.fixture(scope="module")
def definition():
    with open(WORKFLOW_PATH) as f:
        return json.load(f)


# --- 1. JSON parses ---


def test_definition_file_parses_as_json():
    with open(WORKFLOW_PATH) as f:
        json.load(f)  # must not raise


# --- 2. Top-level ASL structure ---


def test_top_level_structure_has_startat_and_states(definition):
    assert "StartAt" in definition
    assert "States" in definition
    assert isinstance(definition["States"], dict)
    assert len(definition["States"]) > 0


# --- helpers used by several tests ---


def _next_targets(state: dict) -> set:
    targets = set()
    if "Next" in state:
        targets.add(state["Next"])
    for choice in state.get("Choices", []):
        if "Next" in choice:
            targets.add(choice["Next"])
    if "Default" in state:
        targets.add(state["Default"])
    for catcher in state.get("Catch", []):
        if "Next" in catcher:
            targets.add(catcher["Next"])
    return targets


def _adjacency(states: dict) -> dict:
    return {name: _next_targets(state) for name, state in states.items()}


def _reachable_from(states: dict, start: str) -> set:
    adjacency = _adjacency(states)
    seen = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for target in adjacency.get(current, ()):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


# --- 3. All referenced state names exist ---


def test_all_referenced_state_names_exist(definition):
    states = definition["States"]
    all_targets = set()
    for state in states.values():
        all_targets |= _next_targets(state)
    missing = all_targets - set(states.keys())
    assert not missing, f"References to non-existent states: {missing}"


# --- 4. No unreachable state ---


def test_no_unreachable_states(definition):
    states = definition["States"]
    reachable = _reachable_from(states, definition["StartAt"])
    unreachable = set(states.keys()) - reachable
    assert not unreachable, f"Unreachable states: {unreachable}"


# --- 5. Exactly one intended start state ---


def test_start_state_exists_and_is_unambiguous(definition):
    start_at = definition["StartAt"]
    assert isinstance(start_at, str)
    assert start_at in definition["States"]
    assert start_at == "CheckAnomaly"


# --- 16. Expected conceptual states exist ---


def test_expected_conceptual_states_exist(definition):
    missing = EXPECTED_STATES - set(definition["States"].keys())
    assert not missing, f"Missing required conceptual states: {missing}"


def test_retry_mechanism_states_exist(definition):
    missing = RETRY_MECHANISM_STATES - set(definition["States"].keys())
    assert not missing, f"Missing required Step 51 retry-mechanism states: {missing}"


# --- 17. Structural correctness of transitions ---


def test_every_choice_state_has_a_default(definition):
    for name, state in definition["States"].items():
        if state["Type"] == "Choice":
            assert "Default" in state, f"Choice state {name} has no Default"
            assert len(state.get("Choices", [])) >= 1, f"Choice state {name} has no Choices"


def test_every_task_state_has_a_resource_and_next_or_end(definition):
    for name, state in definition["States"].items():
        if state["Type"] == "Task":
            assert "Resource" in state, f"Task state {name} has no Resource"
            assert "Next" in state or state.get("End"), f"Task state {name} has no Next/End"


def test_every_terminal_state_is_succeed_or_has_end_true(definition):
    for name, state in definition["States"].items():
        has_next = "Next" in state
        is_choice = state["Type"] == "Choice"
        if not has_next and not is_choice:
            assert state["Type"] == "Succeed" or state.get("End") is True, (
                f"State {name} has neither Next nor End/Succeed — it would be a dead end"
            )


# --- 13. No account-specific hardcoded Lambda ARNs ---


def test_no_account_specific_lambda_arns(definition):
    raw = json.dumps(definition)
    # A real Lambda ARN embeds a 12-digit AWS account id.
    import re

    assert not re.search(r"arn:aws:lambda:[a-z0-9-]+:\d{12}:function:", raw), (
        "Found what looks like an account-specific Lambda ARN in the workflow definition"
    )


# --- 15. No embedded business logic duplicating the remediation engine's mapping ---


def _leaf_conditions(choice: dict):
    """Yields every leaf (non-composite) condition dict within a Choice
    rule, recursing through And/Or/Not composites (Step 51 introduces the
    first And-composite rule, ValidateMaxRetries — this helper lets the
    existing orchestration-flag allowlist check see through it rather
    than crashing on a rule with no top-level 'Variable' key).
    """
    if "And" in choice:
        for sub in choice["And"]:
            yield from _leaf_conditions(sub)
    elif "Or" in choice:
        for sub in choice["Or"]:
            yield from _leaf_conditions(sub)
    elif "Not" in choice:
        yield from _leaf_conditions(choice["Not"])
    else:
        yield choice


def test_choice_states_branch_only_on_orchestration_flags(definition):
    """Every Choice state must branch only on the small set of boolean/
    numeric orchestration flags this workflow actually owns (detected,
    automatic, verified, execution success, and — since Step 51 — the
    retry counter/budget fields it manages itself) — never on
    fault_type/action_type/severity, which would mean the fault-to-action
    mapping (remediation/engine.py's job) had been duplicated into the
    state machine.
    """
    allowed_variable_suffixes = {
        "detected",
        "remediation_plan.automatic",
        "remediation_result.success",
        "verification.verified",
        "retry_count",
        "detection_result.max_retries",
    }
    for name, state in definition["States"].items():
        if state["Type"] != "Choice":
            continue
        for choice in state["Choices"]:
            for leaf in _leaf_conditions(choice):
                variable = leaf["Variable"].lstrip("$.")
                assert variable in allowed_variable_suffixes, (
                    f"Choice state {name} branches on unexpected variable {leaf['Variable']!r}"
                )
                assert "StringEquals" not in leaf and "StringMatches" not in leaf, (
                    f"Choice state {name} uses a string-equality rule — likely duplicated fault-type logic"
                )


def test_definition_contains_no_string_equality_rules_at_all(definition):
    raw = json.dumps(definition)
    for forbidden in ("StringEquals", "StringMatches", "StringLessThan", "StringGreaterThan"):
        assert forbidden not in raw, f"Found {forbidden} in the definition — only boolean flags should drive routing"


# --- 18. JSON serialization/deserialization round-trip ---


def test_definition_round_trips_through_json(definition):
    dumped = json.dumps(definition)
    reloaded = json.loads(dumped)
    assert reloaded == definition


# --- 14. No AWS SDK usage in this test module ---


def test_no_aws_imports_in_this_test_module():
    this_file = pathlib.Path(__file__).resolve()
    forbidden = {"boto3", "botocore"}
    tree = ast.parse(this_file.read_text(), filename=str(this_file))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            continue
        assert not (names & forbidden), f"test module imports forbidden module(s): {names & forbidden}"


# ============================================================================
# A tiny, purpose-built local interpreter — NOT a general ASL interpreter.
# See this file's module docstring for exactly what it does and does not
# prove.
# ============================================================================


_PATH_PRESENT_SENTINEL = object()


def _get_path(data, path, missing=None):
    if path == "$":
        return data
    assert path.startswith("$."), path
    current = data
    for part in path[2:].split("."):
        if not isinstance(current, dict) or part not in current:
            return missing
        current = current[part]
    return current


def _path_is_present(data, path) -> bool:
    return _get_path(data, path, missing=_PATH_PRESENT_SENTINEL) is not _PATH_PRESENT_SENTINEL


def _set_path(data, path, value):
    assert path.startswith("$."), path
    parts = path[2:].split(".")
    current = data
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


_MATH_ADD_RE = re.compile(r"^States\.MathAdd\(\s*(\$\.[A-Za-z0-9_.]+)\s*,\s*(-?\d+)\s*\)$")


def _evaluate_dollar_value(data, value):
    """Evaluates the right-hand side of a `"key.$": value` Parameters
    entry: either a plain JSONPath, or — Step 51 — the one intrinsic
    function this workflow actually uses, `States.MathAdd($.path,
    literal)` (Amazon States Language's native arithmetic helper; ASL has
    no bare '+' operator). Not a general intrinsic-function evaluator —
    see this file's module docstring.
    """
    match = _MATH_ADD_RE.match(value)
    if match:
        path, literal = match.groups()
        left = _get_path(data, path)
        assert isinstance(left, (int, float)), f"States.MathAdd operand {path} is not numeric: {left!r}"
        return left + int(literal)
    return _get_path(data, value)


def _evaluate_condition(data, cond) -> bool:
    """Evaluates one Choice-rule condition, recursively handling And/Or/Not
    composites (Step 51's ValidateMaxRetries is the first user of And).
    Supports exactly the comparison operators this workflow's Choice
    states actually use — not a general ASL Choice evaluator.
    """
    if "And" in cond:
        return all(_evaluate_condition(data, sub) for sub in cond["And"])
    if "Or" in cond:
        return any(_evaluate_condition(data, sub) for sub in cond["Or"])
    if "Not" in cond:
        return not _evaluate_condition(data, cond["Not"])

    variable = cond["Variable"]
    present = _path_is_present(data, variable)
    value = _get_path(data, variable) if present else None

    if "IsPresent" in cond:
        return present == cond["IsPresent"]
    if "BooleanEquals" in cond:
        return present and value == cond["BooleanEquals"]
    if "IsNumeric" in cond:
        is_numeric = present and isinstance(value, (int, float)) and not isinstance(value, bool)
        return is_numeric == cond["IsNumeric"]
    if "NumericGreaterThanEquals" in cond:
        return present and isinstance(value, (int, float)) and value >= cond["NumericGreaterThanEquals"]
    if "NumericLessThanEquals" in cond:
        return present and isinstance(value, (int, float)) and value <= cond["NumericLessThanEquals"]
    if "NumericLessThanPath" in cond:
        other = _get_path(data, cond["NumericLessThanPath"])
        return present and isinstance(value, (int, float)) and isinstance(other, (int, float)) and value < other

    raise AssertionError(f"unsupported condition in test interpreter: {cond}")


def _trace_workflow(definition, initial_input, task_outputs, max_steps=50):
    """Walks the state machine from StartAt using `initial_input`, splicing
    in `task_outputs[state_name]` whenever a Task state is reached (an
    `Exception` instance simulates that task failing, triggering its
    Catch). Returns (final_state_name, final_data, visited_state_names).

    max_steps default is 50 (Step 51): a full max_retries=5 exhaustion
    cycle visits roughly 41 states (6 fixed prefix/plan states + 6
    attempts x 4 states/attempt + 5 retry-decision pairs x 2 states +
    1 final Escalate) — 50 leaves comfortable headroom without masking
    a genuine infinite-loop bug, which would still exceed even this
    larger bound and correctly raise below.
    """
    states = definition["States"]
    current_name = definition["StartAt"]
    data = copy.deepcopy(initial_input)
    visited = []
    _task_visit_counts = {}

    for _ in range(max_steps):
        visited.append(current_name)
        state = states[current_name]
        state_type = state["Type"]

        if state_type == "Succeed":
            return current_name, data, visited

        if state_type == "Pass":
            if "Parameters" in state:
                new_data = {}
                for key, value in state["Parameters"].items():
                    if key.endswith(".$"):
                        new_data[key[:-2]] = _evaluate_dollar_value(data, value)
                    else:
                        # A literal (non-JSONPath) value, e.g. Step 51's
                        # "max_retries": 2 default -- valid ASL, just not
                        # a path/intrinsic reference.
                        new_data[key] = copy.deepcopy(value)
                data = new_data
            if "Result" in state and "ResultPath" in state:
                _set_path(data, state["ResultPath"], copy.deepcopy(state["Result"]))
            if state.get("End"):
                return current_name, data, visited
            current_name = state["Next"]
            continue

        if state_type == "Task":
            assert current_name in task_outputs, f"no simulated output supplied for Task state {current_name}"
            configured = task_outputs[current_name]
            if isinstance(configured, list):
                # Step 51: a list of successive outcomes, consumed one per
                # visit (retries revisit the same Task state) -- lets a
                # test express "fails on attempt 1, succeeds on attempt 2"
                # without needing a stateful callable. The last entry
                # repeats if the state is somehow visited more times than
                # the list has entries (keeps a misconfigured test's
                # failure mode a clear assertion mismatch, not an IndexError).
                index = min(_task_visit_counts.get(current_name, 0), len(configured) - 1)
                outcome = configured[index]
                _task_visit_counts[current_name] = _task_visit_counts.get(current_name, 0) + 1
            else:
                outcome = configured
            if isinstance(outcome, Exception):
                catchers = state.get("Catch", [])
                assert catchers, f"{current_name} was made to fail but has no Catch"
                if "ResultPath" in catchers[0]:
                    _set_path(data, catchers[0]["ResultPath"], str(outcome))
                current_name = catchers[0]["Next"]
                continue
            if "ResultPath" in state:
                _set_path(data, state["ResultPath"], copy.deepcopy(outcome))
            current_name = state["Next"]
            continue

        if state_type == "Choice":
            matched = None
            for choice in state["Choices"]:
                if _evaluate_condition(data, choice):
                    matched = choice["Next"]
                    break
            current_name = matched or state["Default"]
            continue

        raise AssertionError(f"unsupported state type in test interpreter: {state_type}")

    raise AssertionError(f"workflow did not terminate within {max_steps} steps; visited={visited}")


# --- Representative fixtures (per this task's TEST DATA section) ---


def _normal_detection():
    return {
        "detected": False,
        "fault_type": None,
        "severity": "NORMAL",
        "node_id": "power-001",
        "subsystem": "power",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "reason": "All telemetry fields are present and within the expected operating range.",
        "evidence": {"data": {"load": 55.0}},
    }


def _medium_overload_detection():
    return {
        "detected": True,
        "fault_type": "NODE_OVERLOAD",
        "severity": "MEDIUM",
        "node_id": "power-007",
        "subsystem": "power",
        "timestamp": "2026-01-01T00:05:00+00:00",
        "reason": "Field 'load' value 97.8 is within the overload band (95.0, 100.0).",
        "evidence": {"field": "load", "value": 97.8},
    }


def _high_overload_detection():
    detection = _medium_overload_detection()
    detection["severity"] = "HIGH"
    return detection


def _invalid_input_detection():
    return {
        "detected": True,
        "fault_type": "INVALID_INPUT",
        "severity": "CRITICAL",
        "node_id": None,
        "subsystem": None,
        "timestamp": None,
        "reason": "Telemetry payload could not be parsed as JSON.",
        "evidence": {"raw_event_type": "str"},
    }


def _automatic_plan(detection, action_type="REDUCE_LOAD"):
    return {
        "remediation_required": True,
        "node_id": detection["node_id"],
        "subsystem": detection["subsystem"],
        "fault_type": detection["fault_type"],
        "severity": detection["severity"],
        "timestamp": detection["timestamp"],
        "action": {"action_type": action_type, "description": "simulated", "parameters": {}},
        "reason": "test",
        "automatic": True,
        "escalation_required": False,
    }


def _escalation_plan(detection, action_type):
    return {
        "remediation_required": True,
        "node_id": detection["node_id"],
        "subsystem": detection["subsystem"],
        "fault_type": detection["fault_type"],
        "severity": detection["severity"],
        "timestamp": detection["timestamp"],
        "action": {"action_type": action_type, "description": "simulated", "parameters": {}},
        "reason": "test",
        "automatic": False,
        "escalation_required": True,
    }


def _success_result(plan):
    return {
        "success": True,
        "status": "SUCCESS",
        "node_id": plan["node_id"],
        "subsystem": plan["subsystem"],
        "fault_type": plan["fault_type"],
        "action": plan["action"]["action_type"],
        "timestamp": plan["timestamp"],
        "message": "Simulated execution succeeded.",
        "simulated": True,
        "details": {},
    }


# --- 1. Normal DetectionResult -> NoAnomaly ---


def test_fixture_normal_detection_reaches_noanomaly(definition):
    final_state, data, visited = _trace_workflow(definition, _normal_detection(), task_outputs={})
    assert final_state == "NoAnomaly"
    assert "PlanRemediation" not in visited
    # Original detection fields are preserved untouched.
    assert data["detected"] is False
    assert data["node_id"] == "power-001"


# --- 2. Medium NODE_OVERLOAD with automatic remediation -> Success ---


def test_fixture_medium_overload_automatic_remediation_reaches_success(definition):
    detection = _medium_overload_detection()
    plan = _automatic_plan(detection, "REDUCE_LOAD")
    result = _success_result(plan)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={
            "PlanRemediation": plan,
            "ExecuteRemediation": result,
            "VerifyRemediation": {"verified": True},
        },
    )
    assert final_state == "Success"
    assert visited == [
        "CheckAnomaly",
        "PrepareRemediationPlanning",
        "ValidateMaxRetries",
        "UseDefaultMaxRetries",
        "PlanRemediation",
        "CheckAutomaticRemediation",
        "ExecuteRemediation",
        "CheckExecutionSuccess",
        "VerifyRemediation",
        "CheckVerification",
        "Success",
    ]
    # Data is preserved end-to-end, nothing discarded.
    assert data["detection_result"]["node_id"] == "power-007"
    assert data["detection_result"]["fault_type"] == "NODE_OVERLOAD"
    assert data["remediation_plan"]["action"]["action_type"] == "REDUCE_LOAD"
    assert data["remediation_result"]["status"] == "SUCCESS"
    assert data["verification"]["verified"] is True
    # Step 51: no retry was needed -- retry_count stays at its initial 0,
    # and the (defaulted) max_retries budget is visible in the output.
    assert data["retry_count"] == 0
    assert data["max_retries"] == DEFAULT_MAX_RETRIES


# --- 3. High NODE_OVERLOAD requiring escalation ---


def test_fixture_high_overload_requires_escalation(definition):
    detection = _high_overload_detection()
    plan = _escalation_plan(detection, "REDUCE_LOAD")
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={"PlanRemediation": plan},
    )
    assert final_state == "Escalate"
    assert "ExecuteRemediation" not in visited
    assert data["escalation"]["status"] == "ESCALATION_REQUIRED"
    assert data["escalation"]["manual_intervention_required"] is True
    # Original detection/plan data is not discarded by escalation.
    assert data["detection_result"]["fault_type"] == "NODE_OVERLOAD"
    assert data["remediation_plan"]["automatic"] is False


# --- 4. INVALID_INPUT requiring escalation ---


def test_fixture_invalid_input_requires_escalation(definition):
    detection = _invalid_input_detection()
    plan = _escalation_plan(detection, "REJECT_INPUT")
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={"PlanRemediation": plan},
    )
    assert final_state == "Escalate"
    assert "ExecuteRemediation" not in visited
    assert data["remediation_plan"]["action"]["action_type"] == "REJECT_INPUT"


# --- 5. Remediation execution failure -> Escalate ---


def test_fixture_remediation_execution_failure_reaches_escalate(definition):
    """Step 51: max_retries=0 explicitly requested -- a single Execute
    failure must still escalate immediately, with zero retries, exactly
    matching this test's pre-Step-51 behavior (see
    test_fixture_execution_failure_retries_then_escalates_after_budget_exhausted
    below for the default-budget, multi-attempt version of this scenario)."""
    detection = _medium_overload_detection()
    detection["max_retries"] = 0
    plan = _automatic_plan(detection)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={
            "PlanRemediation": plan,
            "ExecuteRemediation": RuntimeError("simulated Lambda failure"),
        },
    )
    assert final_state == "Escalate"
    assert "VerifyRemediation" not in visited
    assert "IncrementRetryCount" not in visited
    assert data["escalation"]["status"] == "ESCALATION_REQUIRED"
    assert data["retry_count"] == 0
    assert data["max_retries"] == 0


# --- 6. Verification failure -> Escalate ---


def test_fixture_verification_failure_reaches_escalate(definition):
    """Step 51: max_retries=0 explicitly requested -- a single failed
    verification must still escalate immediately, with zero retries."""
    detection = _medium_overload_detection()
    detection["max_retries"] = 0
    plan = _automatic_plan(detection)
    result = _success_result(plan)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={
            "PlanRemediation": plan,
            "ExecuteRemediation": result,
            "VerifyRemediation": {"verified": False},
        },
    )
    assert final_state == "Escalate"
    assert data["escalation"]["status"] == "ESCALATION_REQUIRED"
    # A failed verification must never be reported as Success.
    assert final_state != "Success"
    assert data["retry_count"] == 0
    assert "IncrementRetryCount" not in visited


# --- 7. Successful remediation (distinct fault type, for coverage) ---


def test_fixture_sensor_dropout_successful_remediation_reaches_success(definition):
    detection = {
        "detected": True,
        "fault_type": "SENSOR_DROPOUT",
        "severity": "HIGH",
        "node_id": "power-003",
        "subsystem": "power",
        "timestamp": "2026-01-01T00:10:00+00:00",
        "reason": "Telemetry status is 'OFFLINE' instead of 'ONLINE'.",
        "evidence": {"status": "OFFLINE"},
    }
    plan = _automatic_plan(detection, "RESTART_SENSOR")
    result = _success_result(plan)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={
            "PlanRemediation": plan,
            "ExecuteRemediation": result,
            "VerifyRemediation": {"verified": True},
        },
    )
    assert final_state == "Success"
    assert data["remediation_result"]["action"] == "RESTART_SENSOR"


# --- Also verifies the "verification task itself fails" escalation path ---


def test_fixture_verification_task_failure_reaches_escalate(definition):
    """Step 51: max_retries=0 explicitly requested -- a single verification
    Lambda failure must still escalate immediately, with zero retries."""
    detection = _medium_overload_detection()
    detection["max_retries"] = 0
    plan = _automatic_plan(detection)
    result = _success_result(plan)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={
            "PlanRemediation": plan,
            "ExecuteRemediation": result,
            "VerifyRemediation": RuntimeError("simulated verification Lambda failure"),
        },
    )
    assert final_state == "Escalate"
    assert data["retry_count"] == 0
    assert "IncrementRetryCount" not in visited


# ============================================================================
# Step 51 (FR-5.4): dedicated retry-mechanism tests.
#
# Covers exactly the required cases from docs/step51_remediation_retry.md /
# the Step 51 task's Phase 4: max_retries = 0, 1, 2; successful first
# attempt; success after one retry; failure after all retries exhausted;
# malformed/missing/negative/excessive max_retries configuration.
# ============================================================================


def _overload_detection_with_max_retries(max_retries=None):
    detection = _medium_overload_detection()
    if max_retries is not None:
        detection["max_retries"] = max_retries
    return detection


def test_max_retries_zero_escalates_immediately_on_execute_failure():
    """max_retries=0: the very first failure exhausts the (empty) retry
    budget and escalates -- zero total retries, one total attempt."""
    detection = _overload_detection_with_max_retries(0)
    plan = _automatic_plan(detection)
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={"PlanRemediation": plan, "ExecuteRemediation": RuntimeError("fail")},
    )
    assert final_state == "Escalate"
    assert data["retry_count"] == 0
    assert data["max_retries"] == 0
    assert visited.count("ExecuteRemediation") == 1


def test_max_retries_one_allows_exactly_one_retry_then_escalates():
    """max_retries=1: a failure that NEVER succeeds gets exactly 1 retry
    (2 total attempts) before escalating."""
    detection = _overload_detection_with_max_retries(1)
    plan = _automatic_plan(detection)
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={"PlanRemediation": plan, "ExecuteRemediation": RuntimeError("fail")},
    )
    assert final_state == "Escalate"
    assert data["retry_count"] == 1
    assert data["max_retries"] == 1
    assert visited.count("ExecuteRemediation") == 2  # 1 initial + 1 retry


def test_max_retries_two_allows_exactly_two_retries_then_escalates():
    """max_retries=2 (the documented default, also tested explicitly
    here): a failure that never succeeds gets exactly 2 retries (3 total
    attempts) before escalating -- matches the task's own worked example
    exactly (Attempt 1 -> Retry 1 -> Retry 2 -> Escalate)."""
    detection = _overload_detection_with_max_retries(2)
    plan = _automatic_plan(detection)
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={"PlanRemediation": plan, "ExecuteRemediation": RuntimeError("fail")},
    )
    assert final_state == "Escalate"
    assert data["retry_count"] == 2
    assert data["max_retries"] == 2
    assert visited.count("ExecuteRemediation") == 3  # 1 initial + 2 retries


def test_successful_first_attempt_never_touches_retry_states():
    """The existing successful path (Execute succeeds, Verify succeeds)
    must remain completely untouched by the retry mechanism -- no Choice
    on execution success/retry limit ever routes away from the happy path."""
    detection = _overload_detection_with_max_retries(2)
    plan = _automatic_plan(detection)
    result = _success_result(plan)
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={
            "PlanRemediation": plan,
            "ExecuteRemediation": result,
            "VerifyRemediation": {"verified": True},
        },
    )
    assert final_state == "Success"
    assert data["retry_count"] == 0
    assert "IncrementRetryCount" not in visited
    assert "CheckRetryLimit" not in visited
    assert visited.count("ExecuteRemediation") == 1


def test_success_after_one_retry_on_verification_failure():
    """Verification fails on the first attempt, succeeds on the retry --
    exactly the 'success after one retry' case the task requires. Uses
    the interpreter's list-of-successive-outcomes support (see
    _trace_workflow) since Execute is deterministic/identical each time
    but Verify's outcome differs by attempt."""
    detection = _overload_detection_with_max_retries(2)
    plan = _automatic_plan(detection)
    result = _success_result(plan)
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={
            "PlanRemediation": plan,
            "ExecuteRemediation": result,
            "VerifyRemediation": [{"verified": False}, {"verified": True}],
        },
    )
    assert final_state == "Success"
    assert data["retry_count"] == 1
    assert data["verification"]["verified"] is True
    assert visited.count("ExecuteRemediation") == 2
    assert visited.count("VerifyRemediation") == 2
    # Data preserved through the retry, not discarded.
    assert data["detection_result"]["node_id"] == detection["node_id"]
    assert data["remediation_plan"]["action"]["action_type"] == plan["action"]["action_type"]


def test_success_after_two_retries_on_execute_failure_then_verify_success():
    """Execute fails twice (Lambda-level exceptions), succeeds on the
    third attempt (2 retries used); Verify then succeeds -- exercises the
    ExecuteRemediation Catch->CheckRetryLimit->IncrementRetryCount loop
    specifically, not just CheckExecutionSuccess/CheckVerification."""
    detection = _overload_detection_with_max_retries(2)
    plan = _automatic_plan(detection)
    result = _success_result(plan)
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={
            "PlanRemediation": plan,
            "ExecuteRemediation": [RuntimeError("fail 1"), RuntimeError("fail 2"), result],
            "VerifyRemediation": {"verified": True},
        },
    )
    assert final_state == "Success"
    assert data["retry_count"] == 2
    assert visited.count("ExecuteRemediation") == 3
    assert visited.count("VerifyRemediation") == 1  # only reached once Execute finally succeeds


def test_failure_after_all_retries_exhausted_preserves_last_attempt_evidence():
    """Default budget (max_retries omitted -> 2), Execute always throws:
    after exhausting all retries, Escalate's output still carries the
    last attempt's error, plus detection_result/remediation_plan/
    retry_count/max_retries -- nothing silently dropped."""
    detection = _overload_detection_with_max_retries(None)  # no max_retries field at all
    plan = _automatic_plan(detection)
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={"PlanRemediation": plan, "ExecuteRemediation": RuntimeError("persistent failure")},
    )
    assert final_state == "Escalate"
    assert data["max_retries"] == DEFAULT_MAX_RETRIES
    assert data["retry_count"] == DEFAULT_MAX_RETRIES
    assert data["escalation"]["status"] == "ESCALATION_REQUIRED"
    assert data["escalation"]["manual_intervention_required"] is True
    assert "persistent failure" in data["error"]
    assert data["detection_result"]["node_id"] == detection["node_id"]
    assert data["remediation_plan"]["action"]["action_type"] == plan["action"]["action_type"]


@pytest.mark.parametrize(
    "max_retries_value",
    [
        None,  # missing entirely
        "not-a-number",  # malformed (wrong type)
        -1,  # negative
        -100,  # very negative
        6,  # excessive (just above the bound)
        1000,  # excessive (far above the bound)
        [1, 2],  # malformed (wrong type: a list)
    ],
)
def test_invalid_or_missing_max_retries_falls_back_to_documented_default(max_retries_value):
    """Every invalid/missing case must safely fall back to the documented
    default (2) rather than erroring out, clamping ambiguously, or
    allowing an unbounded retry count."""
    detection = _overload_detection_with_max_retries(max_retries_value)
    plan = _automatic_plan(detection)
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={"PlanRemediation": plan, "ExecuteRemediation": RuntimeError("fail")},
    )
    assert data["max_retries"] == DEFAULT_MAX_RETRIES
    assert final_state == "Escalate"
    assert data["retry_count"] == DEFAULT_MAX_RETRIES


@pytest.mark.parametrize("max_retries_value", [0, 1, 2, 3, 4, 5])
def test_valid_max_retries_in_bounds_is_honored_exactly(max_retries_value):
    """Every explicitly valid value in [0, 5] must be honored exactly
    (not silently defaulted or clamped) -- retry_count at escalation
    always equals the configured max_retries for a persistently failing
    attempt."""
    detection = _overload_detection_with_max_retries(max_retries_value)
    plan = _automatic_plan(detection)
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={"PlanRemediation": plan, "ExecuteRemediation": RuntimeError("fail")},
    )
    assert data["max_retries"] == max_retries_value
    assert final_state == "Escalate"
    assert data["retry_count"] == max_retries_value
    assert visited.count("ExecuteRemediation") == max_retries_value + 1


def test_non_automatic_plan_never_enters_retry_loop():
    """A HIGH/CRITICAL non-automatic plan escalates via
    CheckAutomaticRemediation exactly as before Step 51 -- max_retries
    being present in the input must not change this at all; ExecuteRemediation
    is never even reached, so the retry budget is irrelevant."""
    detection = _high_overload_detection()
    detection["max_retries"] = 5  # even a generous budget must not matter here
    plan = _escalation_plan(detection, "REDUCE_LOAD")
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    final_state, data, visited = _trace_workflow(
        definition,
        detection,
        task_outputs={"PlanRemediation": plan},
    )
    assert final_state == "Escalate"
    assert "ExecuteRemediation" not in visited
    assert "CheckRetryLimit" not in visited
    assert "IncrementRetryCount" not in visited


def test_retry_never_starts_a_new_step_functions_execution():
    """Structural guarantee: no state's actual Resource field references a
    Step-Functions execution-starting action -- retries stay entirely
    within Task/Choice/Pass transitions of this one execution (see
    docs/step51_remediation_retry.md 'Idempotency / safety'). Checks
    Resource fields specifically, not the whole raw JSON dump, since this
    file's own Comments legitimately mention 'StartExecution' by name
    while explaining that no such call is made."""
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    for name, state in definition["States"].items():
        resource = state.get("Resource", "")
        assert "startexecution" not in resource.lower(), f"{name}'s Resource references StartExecution: {resource!r}"


def test_retry_states_do_not_alter_remediation_mapping_fields():
    """None of the new retry states reference fault_type/action_type/
    severity directly -- the fault-to-action mapping stays exclusively in
    remediation/engine.py, unchanged by Step 51."""
    with open(WORKFLOW_PATH) as f:
        definition = json.load(f)
    for name in RETRY_MECHANISM_STATES:
        raw = json.dumps(definition["States"][name])
        for forbidden in ("fault_type", "action_type", "severity"):
            assert forbidden not in raw, f"{name} unexpectedly references {forbidden!r}"
