"""
agent/orch_claim_status_gate_mixed_certainty_regression_test.py —
regression for a correctness bug found live (pasted real production log,
PRE-PUSH GATE review, Blocker 3): retrieval classified the IARC "very hot
beverages" evidence relation as uncertain (claim ended up
verification_status="unverified"), yet the rendered answer stated it as
established fact, with no inline marker at all.

Root cause: evaluate_claim_status_gate() (agent/orchestrator/claims/
status.py) already had a P0-A mechanism (YANDI_FINAL_EPISTEMIC_AUDIT_AND_
FIX.md) that prepends an inline "⚠️ ВАЖНО" notice to synthesis_result.answer
when claim status contradicts the generated text — but only for two
ALL-OR-NOTHING cases: every claim contradicted, or verified=0 AND
supported=0. The moment even ONE claim in the same answer reached
"supported", the whole per-claim marking mechanism went silent — any other
claim mixed into the same answer with status unverified/candidate
(claims_verified==0 branch's `else`, i.e. claims_supported>0) or disputed
(claims_disputed>0 branch) got NO notice at all, only a blanket confidence
cap invisible in the rendered text. This is exactly the live-observed
bypass: a genuinely uncertain claim rides along, unflagged, next to a
supported one.

Fix: extended the SAME already-established P0-A notice pattern (prepend an
"⚠️" body notice, idempotent via the same startswith guard) to these two
previously-silent branches. No thresholds changed, no new gate added, no
pipeline reordering — same mechanism, same file, closing the two cases it
didn't cover.

Run: /home/iam/venv/bin/python3 -m agent.orch_claim_status_gate_mixed_certainty_regression_test
"""
from __future__ import annotations

from agent.orch_schemas import SynthesisResult
from agent.orchestrator.claims.status import evaluate_claim_status_gate

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


def _log(msg):
    pass


def _synth(answer="Кофе классифицирован IARC как канцероген группы 2B.", trust="PARTIALLY_SUPPORTED"):
    return SynthesisResult(answer=answer, confidence=0.7, sources=[], trust_level=trust)


# ── the exact live-observed shape: one supported claim, one unverified
#    (IARC-style) claim mixed into the same answer ──
mixed_claims = [
    {"verification_status": "supported"},
    {"verification_status": "unverified"},
]
synth = _synth()
evaluate_claim_status_gate(mixed_claims, synth, _log)
check(
    "mixed supported+unverified claims: the rendered answer now carries an "
    "inline uncertainty notice instead of silently presenting the "
    "unverified claim as fact",
    synth.answer.startswith("⚠️"),
    f"answer={synth.answer!r}",
)
check(
    "the mixed-certainty notice explicitly says not to treat the "
    "unverified part as established fact",
    "не получили" in synth.answer or "не считай" in synth.answer.lower(),
    f"answer={synth.answer!r}",
)

# ── candidate (never reached evidence-relation stage) behaves the same
#    way as unverified for this gate ──
mixed_candidate = [
    {"verification_status": "supported"},
    {"verification_status": "candidate"},
]
synth2 = _synth()
evaluate_claim_status_gate(mixed_candidate, synth2, _log)
check(
    "mixed supported+candidate claims also get the inline notice",
    synth2.answer.startswith("⚠️"),
    f"answer={synth2.answer!r}",
)

# ── disputed claims (supports AND contradicts both exist) previously only
#    capped trust/confidence, no inline marker ──
disputed_claims = [
    {"verification_status": "disputed"},
]
synth3 = _synth()
evaluate_claim_status_gate(disputed_claims, synth3, _log)
check(
    "a disputed claim now gets an inline notice, not just a silent trust cap",
    synth3.answer.startswith("⚠️"),
    f"answer={synth3.answer!r}",
)

# ── control: all claims verified — must NOT be touched (no false positives) ──
verified_claims = [
    {"verification_status": "verified"},
    {"verification_status": "verified"},
]
synth4 = _synth()
original = synth4.answer
evaluate_claim_status_gate(verified_claims, synth4, _log)
check(
    "all-verified case is untouched - no notice added when nothing is "
    "actually uncertain",
    synth4.answer == original,
    f"answer={synth4.answer!r}",
)

# ── control: pre-existing P0-A all-unsupported case still fires exactly as
#    before (fix must not have broken the original mechanism) ──
all_unverified = [
    {"verification_status": "unverified"},
    {"verification_status": "candidate"},
]
synth5 = _synth()
evaluate_claim_status_gate(all_unverified, synth5, _log)
check(
    "pre-existing P0-A all-unsupported notice still fires (fix is additive, "
    "not a regression of the original mechanism)",
    synth5.answer.startswith("⚠️ ВАЖНО:") and "не получило" in synth5.answer,
    f"answer={synth5.answer!r}",
)

# ── control: pre-existing P0-A all-contradicted case still fires exactly
#    as before ──
all_contradicted = [
    {"verification_status": "contradicted"},
]
synth6 = _synth()
evaluate_claim_status_gate(all_contradicted, synth6, _log)
check(
    "pre-existing P0-A all-contradicted notice still fires (fix is "
    "additive, not a regression of the original mechanism)",
    synth6.answer.startswith("⚠️ ВАЖНО:") and "ОПРОВЕРГНУТА" in synth6.answer,
    f"answer={synth6.answer!r}",
)

# ── idempotency: gate must never double-prepend if answer already starts
#    with a notice (guards against double-invocation) ──
synth7 = _synth(answer="⚠️ уже есть заметка\n\nостальной текст")
evaluate_claim_status_gate(mixed_claims, synth7, _log)
check(
    "idempotency guard: does not double-prepend a second notice onto an "
    "answer that already starts with one",
    synth7.answer.count("⚠️") == 1,
    f"answer={synth7.answer!r}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
