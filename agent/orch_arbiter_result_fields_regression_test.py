"""
agent/orch_arbiter_result_fields_regression_test.py — regression for a
pre-existing bug found while doing the final live-matrix verification of
the PET_AGENT_BOUNDARY_AUDIT.md refactor: agent/orch_arbiter.py::arbitrate()
constructed ArbiterResult(..., raw=...) in 3 of its 4 return sites, but
ArbiterResult (agent/orch_schemas.py) has no `raw` field at all
(verdict, explanation, confidence, details, final_answer) - guaranteed
TypeError on every rule-based verdict, every successful LLM arbitration,
and every LLM-arbitration-failed-with-exception path. Only the
"NO_RESPONSE, no validations at all" path (which never passed raw=)
happened not to crash.

This is exactly the same class of bug as NodeValidation's reason/
explanation mismatch fixed in the same session (see
agent/orch_validator_yandi_transport_regression_test.py) - a second,
independent instance in the same file's chain (validate_parallel ->
arbitrate), discovered live: `python3 agent/orchestrator_v2.py "..."
--web --validate` crashed with "ArbiterResult.__init__() got an
unexpected keyword argument 'raw'" during this refactor's final AFTER
benchmark run, right after validate_parallel() (already fixed) produced
real disagree votes for the first time ever.

Run: /home/iam/venv/bin/python3 -m agent.orch_arbiter_result_fields_regression_test
"""
from __future__ import annotations

from agent.orch_arbiter import arbitrate
from agent.orch_schemas import NodeValidation, ValidationResult

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# ── NO_RESPONSE path (the one site that never crashed) ──
empty = ValidationResult(validations=[], agree_count=0, disagree_count=0, timed_out=["a", "b"])
result = arbitrate("q", "a", empty)
check(
    "arbitrate() with no validations at all returns NO_RESPONSE without crashing",
    result.verdict == "NO_RESPONSE",
    f"verdict={result.verdict!r}",
)

# ── rule-based REJECTED path (previously: guaranteed TypeError on `raw=`) ──
rejected_val = ValidationResult(
    validations=[
        NodeValidation(node_id="a", verdict="disagree", confidence=0.7, explanation="неверно", latency=1.0),
        NodeValidation(node_id="b", verdict="disagree", confidence=0.7, explanation="ошибка", latency=1.0),
        NodeValidation(node_id="c", verdict="disagree", confidence=0.7, explanation="плохо", latency=1.0),
    ],
    agree_count=0, disagree_count=3, timed_out=[],
)
result = arbitrate("q", "a", rejected_val, use_llm=False)
check(
    "arbitrate() on a clear-cut REJECTED case (rule-based path, use_llm=False) "
    "returns a real ArbiterResult instead of raising TypeError on "
    "ArbiterResult(raw=...)",
    result.verdict == "REJECTED" and isinstance(result.explanation, str) and result.explanation,
    f"result={result!r}",
)
check(
    "the rule-based path's diagnostic text lands in .details (a real "
    "ArbiterResult field), not a fabricated 'raw' field",
    result.details.get("raw") == "[rule-based]",
    f"details={result.details!r}",
)

# ── rule-based VERIFIED path ──
verified_val = ValidationResult(
    validations=[
        NodeValidation(node_id="a", verdict="agree", confidence=0.7, explanation="верно", latency=1.0),
        NodeValidation(node_id="b", verdict="agree", confidence=0.7, explanation="точно", latency=1.0),
    ],
    agree_count=2, disagree_count=0, timed_out=[],
)
result = arbitrate("q", "a", verified_val, use_llm=False)
check(
    "arbitrate() on a clear-cut VERIFIED case also returns cleanly",
    result.verdict == "VERIFIED",
    f"verdict={result.verdict!r}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
