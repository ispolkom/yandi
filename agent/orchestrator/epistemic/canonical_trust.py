"""
agent/orchestrator/epistemic/canonical_trust.py — Epistemic Core v1
Phase 13: canonical Trust, SHADOW MODE ONLY.

WHY THIS EXISTS (full audit: YANDI_EPISTEMIC_TRUST_CONSOLIDATION_REPORT.md):

Tracing the real call sites (not variable names) shows the live pipeline
computes TWO genuinely independent, never-cross-referenced Trust values
per request:

1. The "synthesizer strand": agent/orch_synthesizer.py's trust_raw
   formula (claim_validity_score, evidence_score, source_agreement, a
   crude web_count-based source_quality unaware of Phases 5-7's source-
   independence work, and two PERMANENTLY HARDCODED components —
   hypothesis_consistency's fallback, reflection_success=0.3 always,
   historical_reliability=0.4 always) -> SynthesisResult.trust_level.
   This value is then downgrade-only mutated by:
     - agent/orchestrator/claims/status.py::evaluate_claim_status_gate()
       (claim verification_status counts),
     - agent/orchestrator/epistemic/existence_contract.py::
       apply_existence_query_contract() (existence-question CORE-claim
       check),
     - agent/orchestrator/response/writeback.py's own inline reflection-
       mistake downgrade (STRONGLY_SUPPORTED -> PARTIALLY_SUPPORTED ->
       WEAKLY_SUPPORTED).
   This fully-downgraded value is what becomes OrchestratorResponse.
   trust_level — what the user actually sees.

2. The "trust_gate strand": agent/orchestrator/epistemic/trust_gate.py::
   apply_epistemic_trust_adjustment() computes `label` from the
   epistemic classification (agent/epistemic_router.py's own
   trust_score/max_trust_cap), testability/domain rules, the FINAL CLAIM
   COVERAGE gate (< 0.50 -> UNVERIFIED, < 0.80 -> capped at
   PARTIALLY_SUPPORTED), the EVIDENCE SUPPORT GROUNDING gate (< 0.3 ->
   UNVERIFIED, < 0.6 -> capped at PARTIALLY_SUPPORTED), and a belief-
   confidence gate. This is a STRICTER, more epistemically rigorous
   computation than strand 1 — but its only effect is `trace.trust =
   label` (a side effect inside the function itself) and a discarded
   local variable at its one call site (orchestrator_v2.py). Nothing
   downstream ever reads it. The user never sees it.

CANONICAL DEFINITION (deliberately not a new formula — see this
module's own STOP-condition discipline: no new thresholds, no new
weights, no new gates):

    canonical_trust = the STRICTER (lower-ranked) of:
        - final_synthesizer_trust  (strand 1, AFTER all its existing
          downgrade-only gates have already run — claim status,
          existence contract, reflection)
        - trust_gate_label         (strand 2, already fully gated by
          coverage/grounding/belief-confidence)

    using the EXACT SAME _TRUST_ORDER ranking and _apply_trust_cap()
    function trust_gate.py already defines and the codebase already
    trusts — reused verbatim, not reimplemented.

This is a MIN operation over two already-monotonic (downgrade-only)
chains, so it inherits both chains' monotonic-safety properties for
free: canonical_trust can never exceed either strand, so it can never
show a higher label than either strand's own hard gates would allow
(coverage/grounding gates cannot be "jumped", per the plan's explicit
requirement). No claim status, no belief semantics, and no
verification_status vocabulary are touched — this module reads two
already-computed strings and returns the lower-ranked one.

SHADOW CONTRACT: this module computes a value, logs it, and returns it
for the caller to store as trace metadata ONLY (an observation/outcome
field, never assigned to OrchestratorResponse.trust_level or
SynthesisResult.trust_level). See its one call site in
agent/orchestrator/response/writeback.py for the structural guarantee
(the return value there is never assigned into the response).

PERFORMANCE: zero network/embedding/LLM calls. Pure comparison of two
already-computed strings via a lookup table.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.orchestrator.epistemic.trust_gate import _TRUST_ORDER, _apply_trust_cap


def compute_canonical_trust_shadow(
    final_synthesizer_trust: Optional[str],
    trust_gate_label: Optional[str],
    log,
    verbose: bool,
) -> Dict[str, Any]:
    """
    Returns {"canonical_trust", "diverged", "stricter_strand", "reason"}.

    Both inputs may legitimately be None (e.g. trust_gate_label is never
    computed on the synthesis-timeout path) — in that case whichever
    side IS available wins outright; if neither is available, returns
    "UNVERIFIED" (the same fail-safe default both existing strands
    already use for "nothing computed").
    """
    if not final_synthesizer_trust and not trust_gate_label:
        return {
            "canonical_trust": "UNVERIFIED",
            "diverged": False,
            "stricter_strand": "neither_available",
            "reason": "neither strand produced a value",
        }

    if not trust_gate_label:
        return {
            "canonical_trust": final_synthesizer_trust,
            "diverged": False,
            "stricter_strand": "synthesizer_only",
            "reason": "trust_gate strand unavailable (e.g. synthesis timed out)",
        }

    if not final_synthesizer_trust:
        return {
            "canonical_trust": trust_gate_label,
            "diverged": False,
            "stricter_strand": "trust_gate_only",
            "reason": "synthesizer strand unavailable",
        }

    canonical = _apply_trust_cap(final_synthesizer_trust, trust_gate_label)
    diverged = canonical != final_synthesizer_trust or final_synthesizer_trust != trust_gate_label

    if _TRUST_ORDER.get(trust_gate_label, 0) < _TRUST_ORDER.get(final_synthesizer_trust, 0):
        stricter_strand = "trust_gate"
    elif _TRUST_ORDER.get(final_synthesizer_trust, 0) < _TRUST_ORDER.get(trust_gate_label, 0):
        stricter_strand = "synthesizer"
    else:
        stricter_strand = "equal"

    result = {
        "canonical_trust": canonical,
        "diverged": final_synthesizer_trust != trust_gate_label,
        "stricter_strand": stricter_strand,
        "reason": (
            f"synthesizer_strand={final_synthesizer_trust} "
            f"trust_gate_strand={trust_gate_label} "
            f"-> canonical={canonical}"
        ),
    }

    if verbose:
        log(
            "[Canonical Trust Shadow] "
            f"synthesizer_strand={final_synthesizer_trust} "
            f"trust_gate_strand={trust_gate_label} "
            f"canonical={canonical} "
            f"diverged={result['diverged']} "
            f"stricter_strand={stricter_strand}"
        )

    return result
