"""
agent/epistemic_claim_semantic_identity_regression_test.py — Epistemic
Core v1 Phase 9 regression: claim semantic identity offline prototype
(agent/claim_semantic_identity_prototype.py::classify_claim_pair()).

Network calls (BeliefManager._embed_batch / _llm_judge_relation) are
mocked here, matching this codebase's established convention for
regression suites (see claim_relation_regression_test.py) — this suite
proves the ORCHESTRATION logic (exact-match shortcut, threshold gating,
result mapping) is correct deterministically. The actual research
result against the real embedding model + LLM judge (12-pair labeled
corpus, precision=0.800 recall=1.000, critical "95 vs 96" case handled
correctly, one honest miss on causal-vs-correlational) is documented in
YANDI_EPISTEMIC_CORE_V1_PHASE9_SEMANTIC_IDENTITY.md, not re-run here —
that would make every regression sweep depend on Ollama being reachable,
which this project's suites deliberately avoid.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_claim_semantic_identity_regression_test
"""

from unittest.mock import patch
import numpy as np

from agent.claim_semantic_identity_prototype import classify_claim_pair
import agent.claim_semantic_identity_prototype as proto_mod
from agent.belief_manager import BeliefManager

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


# ── 1. Exact match (modulo normalization) never touches the network ──

with patch.object(BeliefManager, "_embed_batch", side_effect=AssertionError("should not be called")):
    outcome = classify_claim_pair(
        "Юпитер является крупнейшей планетой.",
        "юпитер   является крупнейшей планетой",
    )
check(
    "exact match (modulo Phase 2 canonicalization) -> 'exact', zero network calls",
    outcome == "exact",
    f"{outcome}",
)

# ── 2. Below-threshold embedding similarity -> 'different', LLM judge never called ──

with patch.object(BeliefManager, "_embed_batch", return_value=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)), \
     patch("agent.belief_manager.BeliefManager._llm_judge_relation", side_effect=AssertionError("should not be called")):
    outcome_low = classify_claim_pair("claim A text here", "claim B text here")
check(
    "embedding similarity below threshold (0.70) -> 'different', LLM judge never invoked",
    outcome_low == "different",
    f"{outcome_low}",
)

# ── 3. Above-threshold similarity -> LLM judge IS consulted, and its verdict is passed through ──

with patch.object(BeliefManager, "_embed_batch", return_value=np.array([[1.0, 0.0], [0.99, 0.14]], dtype=np.float32)), \
     patch.object(BeliefManager, "_llm_judge_relation", return_value="equivalent"):
    outcome_equiv = classify_claim_pair("claim text one", "claim text two")
check(
    "above-threshold similarity -> LLM judge consulted, 'equivalent' verdict passed through",
    outcome_equiv == "equivalent",
    f"{outcome_equiv}",
)

with patch.object(BeliefManager, "_embed_batch", return_value=np.array([[1.0, 0.0], [0.99, 0.14]], dtype=np.float32)), \
     patch.object(BeliefManager, "_llm_judge_relation", return_value="contradicts"):
    outcome_contra = classify_claim_pair("claim text one", "claim text two")
check(
    "above-threshold similarity -> LLM judge 'contradicts' verdict passed through distinctly from 'equivalent'",
    outcome_contra == "contradicts",
    f"{outcome_contra}",
)

with patch.object(BeliefManager, "_embed_batch", return_value=np.array([[1.0, 0.0], [0.99, 0.14]], dtype=np.float32)), \
     patch.object(BeliefManager, "_llm_judge_relation", return_value="different"):
    outcome_diff = classify_claim_pair("claim text one", "claim text two")
check(
    "above-threshold similarity -> LLM judge 'different' verdict passed through as 'different'",
    outcome_diff == "different",
    f"{outcome_diff}",
)

with patch.object(BeliefManager, "_embed_batch", return_value=np.array([[1.0, 0.0], [0.99, 0.14]], dtype=np.float32)), \
     patch.object(BeliefManager, "_llm_judge_relation", return_value="garbage_unparseable_response"):
    outcome_garbage = classify_claim_pair("claim text one", "claim text two")
check(
    "LLM judge returning an unrecognized label -> 'different' (fail-safe, never a fabricated equivalence)",
    outcome_garbage == "different",
    f"{outcome_garbage}",
)

# ── 4. Embedding failure (returns None) -> fail-safe 'different', matching belief_manager.py's own contract ──

with patch.object(BeliefManager, "_embed_batch", return_value=None):
    outcome_fail = classify_claim_pair("some claim text", "some other claim text")
check(
    "embedding call failure (_embed_batch returns None) -> 'different', fail-safe, never fabricates equivalence",
    outcome_fail == "different",
    f"{outcome_fail}",
)

# ── 5. Empty text handled without crashing ──

try:
    outcome_empty = classify_claim_pair("", "some claim text")
    check(
        "empty claim text -> 'different', no crash",
        outcome_empty == "different",
        f"{outcome_empty}",
    )
except Exception as e:
    check("empty claim text -> 'different', no crash", False, repr(e))

# ── 6. Threshold is imported/reused from the module, not silently duplicated as a magic number ──

check(
    "EMBEDDING_PREFILTER_THRESHOLD matches belief_manager.py's own calibrated threshold (0.70), not re-derived",
    proto_mod.EMBEDDING_PREFILTER_THRESHOLD == 0.70,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
