"""
agent/claim_semantic_identity_prototype.py — Epistemic Core v1 Phase 9:
OFFLINE research prototype for claim semantic (paraphrase) identity.

*** NOT WIRED INTO PRODUCTION IDENTITY. *** Phase 2's content_hash
(agent/claim_identity.py) is untouched — this module does not replace
or extend it. Nothing in agent/orchestrator/* imports this file.

Problem: Phase 2's content_hash is deliberately exact/near-exact
(whitespace/case/Unicode-composition normalization only) — two
paraphrases of the same fact are NOT expected to share a content_hash,
by design. This phase asks a different, harder question: can a
paraphrase-aware equivalence judgment be made at all, and how precise
can it be — WITHOUT inventing a third equivalence engine.

Per the plan: reuse belief_manager.py's existing pattern verbatim
(exact match -> embedding cosine prefilter -> LLM equivalence judge).
This module is a thin orchestration layer that calls
BeliefManager._embed_batch() (static) and get_belief_manager()
.llm_judge_relation() (via the production singleton) directly — it does
not reimplement embedding, cosine similarity math, or the LLM judge
prompt. The threshold (0.70) is the exact same one belief_manager.py
already uses and calibrated (see belief_manager.py:234-243's own
comment) — not re-tuned here.

Unlike Phase 5's deliberately network-free shingle fingerprinting, this
module DOES make real Ollama calls (embedding + LLM generate) — that is
inherent to the pattern being reused, not an oversight. "Offline" here
means "not wired into production claim identity", not "no network
calls".

Critical risk this phase exists to test, named explicitly in the plan:
"Jupiter has 95 moons" vs "Jupiter has 96 moons" — embedding similarity
may be very high (near-identical wording) while the two statements are
epistemically different claims. High PRECISION on this exact failure
mode is the primary thing being evaluated, not recall.
"""

from __future__ import annotations

from typing import Optional

from agent.belief_manager import BeliefManager, get_belief_manager
from agent.claim_identity import canonicalize_claim_text
from agent.claim_semantic_identity_hardening import hardening_guard

# Same threshold belief_manager.py already uses and calibrated — reused,
# not re-derived. See belief_manager.py:234-243 for the original
# calibration comment (~0.17 unrelated / ~0.54-0.64 same-topic-different-
# claim / ~0.81 even opposite statements can score close / ~0.92 near-
# paraphrase).
EMBEDDING_PREFILTER_THRESHOLD = 0.70


def classify_claim_pair_detailed(claim_a: str, claim_b: str) -> dict:
    """
    Full pipeline with diagnostics, for evaluation/reporting. Returns:
        {
            "outcome": "exact"|"equivalent"|"contradicts"|"different",
            "raw_verdict": the verdict BEFORE Phase 9B's hardening guard
                           (same value as "outcome" unless the guard
                           downgraded an "equivalent"),
            "similarity": embedding cosine similarity, or None if the
                          exact-match path was taken / embedding failed,
            "guard_reason": the hardening_guard() reason string if it
                            fired, else None.
        }

    Pipeline, identical in shape to belief_manager.py::_find_similar():
        1. exact match on normalized text (no network call)
        2. embedding cosine similarity prefilter (one batch call)
        3. LLM equivalence judge, only for pairs that pass the prefilter
        4. Phase 9B: deterministic hardening guard — downgrades an
           "equivalent" verdict to "different" if a dangerous marker
           mismatch is found (see agent/claim_semantic_identity_hardening.py).
           Never touches "contradicts"/"different"/"exact".
    """
    norm_a = canonicalize_claim_text(claim_a)
    norm_b = canonicalize_claim_text(claim_b)

    if norm_a and norm_a == norm_b:
        return {"outcome": "exact", "raw_verdict": "exact", "similarity": None, "guard_reason": None}

    if not norm_a or not norm_b:
        return {"outcome": "different", "raw_verdict": "different", "similarity": None, "guard_reason": None}

    vectors = BeliefManager._embed_batch([claim_a, claim_b])
    if vectors is None:
        # Fail-safe, same as belief_manager.py: embedding failure never
        # fabricates an equivalence.
        return {"outcome": "different", "raw_verdict": "different", "similarity": None, "guard_reason": None}

    import numpy as np
    similarity = float(np.dot(vectors[0], vectors[1]))

    if similarity < EMBEDDING_PREFILTER_THRESHOLD:
        return {"outcome": "different", "raw_verdict": "different", "similarity": similarity, "guard_reason": None}

    bm = get_belief_manager()
    relation = bm._llm_judge_relation(claim_a, claim_b)

    raw_verdict = relation if relation in ("equivalent", "contradicts") else "different"

    if raw_verdict == "equivalent":
        guard_reason = hardening_guard(claim_a, claim_b)
        if guard_reason:
            return {"outcome": "different", "raw_verdict": raw_verdict, "similarity": similarity, "guard_reason": guard_reason}

    return {"outcome": raw_verdict, "raw_verdict": raw_verdict, "similarity": similarity, "guard_reason": None}


def classify_claim_pair(claim_a: str, claim_b: str) -> str:
    """
    Returns one of: "exact", "equivalent", "contradicts", "different".
    Thin wrapper over classify_claim_pair_detailed() for callers that
    only need the final outcome — see that function for the full
    pipeline description and Phase 9B's hardening guard step.
    """
    return classify_claim_pair_detailed(claim_a, claim_b)["outcome"]
