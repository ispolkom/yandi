"""
agent/epistemic_canonical_trust_shadow_regression_test.py — Epistemic Core
v1 Phases 13-14 regression: canonical Trust
(agent/orchestrator/epistemic/canonical_trust.py).

Filename kept from Phase 13 (when this ran in shadow) for history
continuity; Phase 14 promoted compute_canonical_trust() to the
authoritative final Trust — see the "Phase 14 cutover proof" section
below for the checks added at that point.

Deterministic Trust matrix: for each scenario, compares the OLD
production Trust (final_synthesizer_trust, i.e. SynthesisResult.
trust_level AFTER all its existing downgrade-only gates) against the
CANONICAL result, and records a divergence table — not just "it runs".
Per Phase 14 acceptance (plan section 19), FINAL production Trust must
now equal this same canonical value; the cutover-proof checks below
verify that structurally (the assignment exists, in the right order,
with nothing recomputing it afterward) rather than re-running the whole
matrix through the live pipeline (already covered by this phase's live
runs — see the consolidation report).

canonical_trust.py itself does not read claims_data/coverage/grounding
directly (that is trust_gate.py's job, unchanged this phase) — it only
combines two already-computed label strings. So this matrix's
"final_synthesizer_trust" and "trust_gate_label" columns are exactly
what those two existing, already-tested strands would have already
produced for each scenario; this test proves the COMBINATION step, not
re-deriving the strands themselves (those are covered by their own
existing suites — evaluate_claim_status_gate, apply_epistemic_trust_
adjustment, etc.).

Run: /home/iam/venv/bin/python3 -m agent.epistemic_canonical_trust_shadow_regression_test
"""

from agent.orchestrator.epistemic.canonical_trust import compute_canonical_trust
from agent.orchestrator.epistemic.trust_gate import _TRUST_ORDER

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


# Each row: (scenario name, old production trust [synthesizer strand,
# post-gates], trust_gate strand label, expected canonical)
MATRIX = [
    # 1. Strong independent support: both strands agree, high.
    ("strong_independent_support", "STRONGLY_SUPPORTED", "STRONGLY_SUPPORTED", "STRONGLY_SUPPORTED"),
    # 2. One support only: synthesizer optimistic, trust_gate more cautious
    #    (grounding gate caps at PARTIALLY_SUPPORTED for < 0.6 support coverage).
    ("one_support_only", "PARTIALLY_SUPPORTED", "PARTIALLY_SUPPORTED", "PARTIALLY_SUPPORTED"),
    # 3. Five syndicated supports (Phase 7: counts as ONE independent origin) —
    #    trust_gate's grounding gate reflects the cluster-aware count, staying
    #    capped even though the synthesizer strand's crude web_count>=3 heuristic
    #    would optimistically say STRONGLY_SUPPORTED.
    ("five_syndicated_supports", "STRONGLY_SUPPORTED", "PARTIALLY_SUPPORTED", "PARTIALLY_SUPPORTED"),
    # 4. Multiple genuinely independent supports: both strands agree, high.
    ("multiple_independent_supports", "STRONGLY_SUPPORTED", "STRONGLY_SUPPORTED", "STRONGLY_SUPPORTED"),
    # 5. Support + contradiction (disputed): claims/status.py caps at
    #    WEAKLY_SUPPORTED; trust_gate's grounding gate is less strict here.
    ("support_plus_contradiction", "WEAKLY_SUPPORTED", "PARTIALLY_SUPPORTED", "WEAKLY_SUPPORTED"),
    # 6. Strong contradiction, nothing supported: claims/status.py forces
    #    UNVERIFIED; trust_gate agrees via its own grounding gate.
    ("strong_contradiction", "UNVERIFIED", "UNVERIFIED", "UNVERIFIED"),
    # 7. Missing evidence entirely: both strands bottom out.
    ("missing_evidence", "UNVERIFIED", "UNVERIFIED", "UNVERIFIED"),
    # 8. Search attempted but nothing found: NOT FOUND != FALSE — still
    #    unverified, not contradicted, both strands agree.
    ("search_attempted_not_found", "UNVERIFIED", "UNVERIFIED", "UNVERIFIED"),
    # 9. Search error: SEARCH ERROR != CONTRADICTION — stays unverified,
    #    not downgraded further than that.
    ("search_error", "UNVERIFIED", "UNVERIFIED", "UNVERIFIED"),
    # 10. Search never attempted: same floor.
    ("search_never_attempted", "UNVERIFIED", "UNVERIFIED", "UNVERIFIED"),
    # 11. High source quality but low coverage: synthesizer strand (no
    #     coverage awareness) says STRONGLY_SUPPORTED; trust_gate's FINAL
    #     CLAIM COVERAGE gate (< 0.50 -> UNVERIFIED) catches it. This is
    #     the canonical example from the plan (raw score high, coverage
    #     insufficient -> canonical must NOT show STRONGLY_SUPPORTED).
    ("high_quality_low_coverage", "STRONGLY_SUPPORTED", "UNVERIFIED", "UNVERIFIED"),
    # 12. High coverage but poor grounding: synthesizer strand doesn't see
    #     grounding directly; trust_gate's EVIDENCE SUPPORT GROUNDING gate
    #     (< 0.3 -> UNVERIFIED) catches it.
    ("high_coverage_poor_grounding", "PARTIALLY_SUPPORTED", "UNVERIFIED", "UNVERIFIED"),
    # 13. Supported claims + one critical unsupported claim: claims/status.py's
    #     claim_supported==0 branch already caps hard; trust_gate agrees.
    ("critical_unsupported_claim", "WEAKLY_SUPPORTED", "WEAKLY_SUPPORTED", "WEAKLY_SUPPORTED"),
    # 14. Recheck (Phase 12) SUPPORTS old belief: belief confidence stays
    #     healthy, trust_gate's belief gate doesn't fire, both strands agree.
    ("recheck_supports_old_belief", "SUPPORTED", "SUPPORTED", "SUPPORTED"),
    # 15. Recheck CONTRADICTS old belief: belief confidence drops, trust_gate's
    #     belief-confidence gate (avg < 0.5 -> cap PARTIALLY_SUPPORTED) fires
    #     even though the synthesizer strand (computed earlier in the SAME
    #     request, before this belief update) still shows STRONGLY_SUPPORTED.
    ("recheck_contradicts_old_belief", "STRONGLY_SUPPORTED", "PARTIALLY_SUPPORTED", "PARTIALLY_SUPPORTED"),
    # 16. Recheck INCONCLUSIVE (Phase 12: belief untouched) -> neither strand
    #     is affected by this request's recheck at all; whatever they'd
    #     otherwise be, unaffected by the recheck itself.
    ("recheck_inconclusive", "SUPPORTED", "SUPPORTED", "SUPPORTED"),
    # 17. Dependency recheck error (Phase 12: belief untouched, error != false):
    #     same as 16 — recheck failure must never look like a downgrade signal.
    ("dependency_recheck_error", "SUPPORTED", "SUPPORTED", "SUPPORTED"),
    # 18. Semantic-family duplicate occurrences (Phase 9B/10): does not by
    #     itself change either strand — family linking is metadata, not a
    #     trust signal in either strand today.
    ("semantic_family_duplicates", "PARTIALLY_SUPPORTED", "PARTIALLY_SUPPORTED", "PARTIALLY_SUPPORTED"),
    # 19. Same evidence repeated through source clusters (Phase 5-7): the
    #     cluster-aware trust_gate strand is unaffected by raw repeat count;
    #     the cruder synthesizer strand's web_count heuristic over-counts.
    ("same_evidence_via_clusters", "STRONGLY_SUPPORTED", "PARTIALLY_SUPPORTED", "PARTIALLY_SUPPORTED"),
    # 20. Mixed claim statuses (some verified, some contradicted, some
    #     unverified): claims/status.py's disputed-claims branch caps at
    #     WEAKLY_SUPPORTED; trust_gate's own gates agree independently.
    ("mixed_claim_statuses", "WEAKLY_SUPPORTED", "WEAKLY_SUPPORTED", "WEAKLY_SUPPORTED"),
    # 21. LIVE-CAUGHT (not synthetic): a real Phase 13 live run ("Столица
    #     Франции?") produced exactly this pair and exposed a genuine
    #     pre-existing _TRUST_ORDER bug — "WEAKLY_SUPPORTED" was missing
    #     from the table, defaulting to rank 0 (BELOW UNVERIFIED's rank
    #     1), which made this scenario wrongly resolve to WEAKLY_SUPPORTED
    #     instead of the correct, stricter UNVERIFIED. Fixed in
    #     trust_gate.py's _TRUST_ORDER (see that table's own comment).
    #     This row is the regression guard for that exact fix.
    ("live_caught_weakly_supported_vs_unverified", "WEAKLY_SUPPORTED", "UNVERIFIED", "UNVERIFIED"),
]

print(f"{'scenario':<32} {'old_prod':<20} {'trust_gate':<20} {'canonical':<20} {'diverged':<9} {'stricter'}")
print("-" * 120)

for name, old_prod, trust_gate_label, expected_canonical in MATRIX:
    result = compute_canonical_trust(old_prod, trust_gate_label, log=lambda m: None, verbose=False)
    print(
        f"{name:<32} {old_prod:<20} {trust_gate_label:<20} "
        f"{result['canonical_trust']:<20} {str(result['diverged']):<9} {result['stricter_strand']}"
    )
    check(
        f"[{name}] canonical == expected ({expected_canonical})",
        result["canonical_trust"] == expected_canonical,
        f"got {result}",
    )
    # MONOTONIC SAFETY: canonical must never rank ABOVE either strand.
    check(
        f"[{name}] canonical never exceeds either strand (monotonic safety)",
        _TRUST_ORDER.get(result["canonical_trust"], 0) <= _TRUST_ORDER.get(old_prod, 0)
        and _TRUST_ORDER.get(result["canonical_trust"], 0) <= _TRUST_ORDER.get(trust_gate_label, 0),
        f"canonical={result['canonical_trust']} old_prod={old_prod} trust_gate={trust_gate_label}",
    )

print()

# ── Structural / edge-case checks beyond the matrix ──

r_none_both = compute_canonical_trust(None, None, log=lambda m: None, verbose=False)
check(
    "neither strand available -> fail-safe UNVERIFIED, not a crash",
    r_none_both["canonical_trust"] == "UNVERIFIED" and not r_none_both["diverged"],
    f"{r_none_both}",
)

r_no_gate = compute_canonical_trust("STRONGLY_SUPPORTED", None, log=lambda m: None, verbose=False)
check(
    "trust_gate strand unavailable (e.g. synthesis timed out): synthesizer strand used as-is, no crash",
    r_no_gate["canonical_trust"] == "STRONGLY_SUPPORTED" and not r_no_gate["diverged"],
    f"{r_no_gate}",
)

r_no_synth = compute_canonical_trust(None, "PARTIALLY_SUPPORTED", log=lambda m: None, verbose=False)
check(
    "synthesizer strand unavailable: trust_gate strand used as-is, no crash",
    r_no_synth["canonical_trust"] == "PARTIALLY_SUPPORTED" and not r_no_synth["diverged"],
    f"{r_no_synth}",
)

r_equal = compute_canonical_trust("PARTIALLY_SUPPORTED", "PARTIALLY_SUPPORTED", log=lambda m: None, verbose=False)
check(
    "both strands agree exactly: not diverged, stricter_strand='equal'",
    not r_equal["diverged"] and r_equal["stricter_strand"] == "equal",
    f"{r_equal}",
)

# Contradictions/lower coverage must never be able to RAISE canonical trust
# above what either strand independently allows — test the reverse
# direction explicitly (trust_gate stricter than synthesizer, and vice
# versa), both already covered by the matrix above (#11/#12 and #2
# respectively), plus a direct symmetric check:
r_a = compute_canonical_trust("UNVERIFIED", "STRONGLY_SUPPORTED", log=lambda m: None, verbose=False)
r_b = compute_canonical_trust("STRONGLY_SUPPORTED", "UNVERIFIED", log=lambda m: None, verbose=False)
check(
    "symmetric: whichever strand is UNVERIFIED wins regardless of argument order",
    r_a["canonical_trust"] == "UNVERIFIED" and r_b["canonical_trust"] == "UNVERIFIED",
    f"{r_a} {r_b}",
)

# ── Phase 14 cutover proof: canonical Trust IS the final result, exactly once ──

import inspect
import agent.orchestrator.response.writeback as writeback_mod

wb_src = inspect.getsource(writeback_mod.run_optimistic_respond)

check(
    "run_optimistic_respond DOES assign compute_canonical_trust's result "
    "into synthesis_result.trust_level (the Phase 14 cutover point)",
    'synthesis_result.trust_level = _canonical_result["canonical_trust"]' in wb_src,
    "",
)

_lines = wb_src.splitlines()
_cutover_idx = next(i for i, l in enumerate(_lines) if "_canonical_result = compute_canonical_trust(" in l)
_assign_idx = next(i for i, l in enumerate(_lines) if 'synthesis_result.trust_level = _canonical_result["canonical_trust"]' in l)
_return_idx = next(i for i, l in enumerate(_lines) if "return OrchestratorResponse(" in l)
check(
    "the cutover happens in the right order: compute canonical -> assign "
    "it onto synthesis_result.trust_level -> only THEN build the response "
    "that reads synthesis_result.trust_level",
    _cutover_idx < _assign_idx < _return_idx,
    f"compute={_cutover_idx} assign={_assign_idx} return={_return_idx}",
)
check(
    "nothing between the cutover assignment and the final return "
    "reassigns synthesis_result.trust_level again (single decision point, "
    "no downstream silent recompute)",
    not any(
        "synthesis_result.trust_level =" in l
        for l in _lines[_assign_idx + 1:_return_idx]
    ),
    "",
)
check(
    "the trace's own .trust field is also updated to the canonical value "
    "at cutover (previously it only ever held the trust_gate strand's "
    "label, set earlier and never reconciled with the final response)",
    "trace.trust = _canonical_result[" in wb_src,
    "",
)

import agent.orchestrator_v2 as orch_v2_mod

orch_src = inspect.getsource(orch_v2_mod)
check(
    "orchestrator_v2.py threads label through as epistemic_trust_gate_label "
    "(a keyword arg to run_optimistic_respond) — the cutover itself lives "
    "entirely inside writeback.py, not duplicated here",
    "epistemic_trust_gate_label=label if 'label' in locals() else None" in orch_src
    and "synthesis_result.trust_level = label" not in orch_src,
    "",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
