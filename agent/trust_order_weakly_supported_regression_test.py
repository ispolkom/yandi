"""
agent/trust_order_weakly_supported_regression_test.py — regression for a
missing "WEAKLY_SUPPORTED" entry in agent/orchestrator/epistemic/
trust_gate.py's _TRUST_ORDER, found live during Epistemic Core v1 Phase 13
(canonical Trust shadow evaluation).

Root cause: _TRUST_ORDER.get(label, 0) defaulted missing labels to 0 —
but 0 is BELOW UNVERIFIED's real rank of 1, so a missing
"WEAKLY_SUPPORTED" entry silently inverted its position relative to
UNVERIFIED. This broke _apply_trust_cap()'s core invariant (a cap must
only ever lower a label, never raise it) in both directions. See
trust_gate.py's _TRUST_ORDER definition for the full writeup and why
rank 2 is the correct value (matching this exact module's own two local
`trust_rank` copies, not a newly invented number).

Run: /home/iam/venv/bin/python3 -m agent.trust_order_weakly_supported_regression_test
"""

from agent.orchestrator.epistemic.trust_gate import _TRUST_ORDER, _apply_trust_cap

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


check(
    "WEAKLY_SUPPORTED is present in _TRUST_ORDER (was previously missing)",
    "WEAKLY_SUPPORTED" in _TRUST_ORDER,
)
check(
    "WEAKLY_SUPPORTED ranks strictly above UNVERIFIED/HYPOTHESIS",
    _TRUST_ORDER["WEAKLY_SUPPORTED"] > _TRUST_ORDER["UNVERIFIED"]
    and _TRUST_ORDER["WEAKLY_SUPPORTED"] > _TRUST_ORDER["HYPOTHESIS"],
)
check(
    "WEAKLY_SUPPORTED ranks strictly below PARTIALLY_SUPPORTED/PARTIAL",
    _TRUST_ORDER["WEAKLY_SUPPORTED"] < _TRUST_ORDER["PARTIALLY_SUPPORTED"]
    and _TRUST_ORDER["WEAKLY_SUPPORTED"] < _TRUST_ORDER["PARTIAL"],
)

# ── _apply_trust_cap invariant: result must NEVER outrank either input ──

pairs = [
    ("WEAKLY_SUPPORTED", "UNVERIFIED"),
    ("UNVERIFIED", "WEAKLY_SUPPORTED"),
    ("WEAKLY_SUPPORTED", "PARTIALLY_SUPPORTED"),
    ("PARTIALLY_SUPPORTED", "WEAKLY_SUPPORTED"),
    ("WEAKLY_SUPPORTED", "STRONGLY_SUPPORTED"),
    ("STRONGLY_SUPPORTED", "WEAKLY_SUPPORTED"),
    ("WEAKLY_SUPPORTED", "WEAKLY_SUPPORTED"),
]

for a, b in pairs:
    result = _apply_trust_cap(a, b)
    check(
        f"_apply_trust_cap({a!r}, {b!r}) == {result!r} never outranks either input "
        "(a cap must only ever lower, never raise)",
        _TRUST_ORDER[result] <= _TRUST_ORDER[a] and _TRUST_ORDER[result] <= _TRUST_ORDER[b],
        f"order(result)={_TRUST_ORDER[result]} order(a)={_TRUST_ORDER[a]} order(b)={_TRUST_ORDER[b]}",
    )

check(
    "specifically: UNVERIFIED must never be upgraded to WEAKLY_SUPPORTED "
    "via a cap call (the exact direction the old bug got wrong)",
    _apply_trust_cap("UNVERIFIED", "WEAKLY_SUPPORTED") == "UNVERIFIED",
)
check(
    "specifically: a WEAKLY_SUPPORTED current value IS correctly capped "
    "down to UNVERIFIED when the cap requires it (the other direction "
    "the old bug got wrong — a cap that could never fire)",
    _apply_trust_cap("WEAKLY_SUPPORTED", "UNVERIFIED") == "UNVERIFIED",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
