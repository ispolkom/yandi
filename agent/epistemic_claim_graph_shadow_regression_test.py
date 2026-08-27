"""
agent/epistemic_claim_graph_shadow_regression_test.py — Epistemic Core v1
Phase 8 regression: claim_graph.py reactivated in SHADOW MODE
(agent/claim_graph_shadow.py::run_claim_graph_shadow()).

Proves: real live claims (not claim_graph.py's own sentence-extraction)
become graph nodes; edges come from already-computed NLI results with
ZERO new NLI calls (no second engine, no reimplemented heuristics);
fallback/non-LLM results never create an edge; a None disagreement_result
(early-gate or exception case) degrades gracefully to nodes-only, no
edges, no crash; strange edges (a pair both supporting and contradicting)
are flagged as a diagnostic; and — critically — the production call site
never captures this module's return value, so it structurally cannot
influence the answer/Trust/belief/retrieval/coverage.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_claim_graph_shadow_regression_test
"""

import inspect

from agent.claim_graph_shadow import run_claim_graph_shadow
import agent.claim_graph_shadow as shadow_mod
from agent.claim_graph import ClaimGraph

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


def _claim(cid, text, status="unverified", ctype="factual", conf=0.5):
    return {"claim_id": cid, "claim_text": text, "verification_status": status,
            "claim_type": ctype, "claim_confidence": conf}


# ── 1. Real live claims become graph nodes (bypassing claim_graph.py's own extraction) ──

claims = [
    _claim("cl_1", "Юпитер является крупнейшей планетой Солнечной системы."),
    _claim("cl_2", "Сатурн является второй по размеру планетой."),
    _claim("cl_3", "Меркурий является ближайшей к Солнцу планетой."),
]
stats_no_disagreement = run_claim_graph_shadow(claims, None, None, log=lambda m: None, verbose=False)
check(
    "real live claims become graph nodes (3 claims -> 3 nodes)",
    stats_no_disagreement["nodes"] == 3,
    f"{stats_no_disagreement}",
)
check(
    "no disagreement_result (None) -> zero edges, no crash",
    stats_no_disagreement["edges"] == 0 and stats_no_disagreement["nli_calls_reused"] == 0,
    f"{stats_no_disagreement}",
)

# ── 2. Edges built from already-computed NLI results, zero new NLI calls ──

disagreement_result = {
    "batch_results": [
        {"pair_id": "0:1", "relation": "contradicts", "method": "llm_nli_batch"},
        {"pair_id": "0:2", "relation": "supports", "method": "llm_nli_batch"},
        {"pair_id": "1:2", "relation": "unrelated", "method": "llm_nli_batch"},
    ],
    "pair_claims": {
        "0:1": (claims[0], claims[1]),
        "0:2": (claims[0], claims[2]),
        "1:2": (claims[1], claims[2]),
    },
}
stats = run_claim_graph_shadow(claims, disagreement_result, {"supported": 1}, log=lambda m: None, verbose=False)
check(
    "3 real NLI results -> 3 reused calls counted; 2 relations created (contradicts + supports, unrelated "
    "skipped) -> 4 directional edge-list entries (ClaimGraph.summary()'s own counting convention counts "
    "both sides of each relation: contradicts is symmetric on both nodes, supports/depends_on is one "
    "entry on each of two different nodes — reused unchanged from the pre-existing summary() method)",
    stats["nli_calls_reused"] == 3 and stats["edges"] == 4,
    f"{stats}",
)
check(
    "overhead is measured and non-negative",
    stats["overhead_ms"] >= 0,
    f"{stats}",
)

# ── 3. Fallback/non-LLM NLI results never create an edge (fails open) ──

fallback_result = {
    "batch_results": [
        {"pair_id": "0:1", "relation": "contradicts", "method": "batch_fallback"},
        {"pair_id": "0:2", "relation": "supports", "method": "batch_missing"},
    ],
    "pair_claims": {
        "0:1": (claims[0], claims[1]),
        "0:2": (claims[0], claims[2]),
    },
}
stats_fallback = run_claim_graph_shadow(claims, fallback_result, None, log=lambda m: None, verbose=False)
check(
    "fallback/missing NLI results (not llm_nli_batch) never create an edge — same fail-open principle as Phase 5/6",
    stats_fallback["edges"] == 0 and stats_fallback["nli_calls_reused"] == 0,
    f"{stats_fallback}",
)

# ── 4. Strange-edge detection: a pair that ends up both supporting and contradicting ──

contradictory_batch = {
    "batch_results": [
        {"pair_id": "0:1", "relation": "contradicts", "method": "llm_nli_batch"},
        {"pair_id": "1:0", "relation": "supports", "method": "llm_nli_batch"},
    ],
    "pair_claims": {
        "0:1": (claims[0], claims[1]),
        "1:0": (claims[1], claims[0]),
    },
}
stats_strange = run_claim_graph_shadow(claims[:2], contradictory_batch, None, log=lambda m: None, verbose=False)
check(
    "a pair simultaneously supporting AND contradicting is flagged as a strange edge",
    stats_strange["strange_edges"] > 0,
    f"{stats_strange}",
)

# ── 5. Self-pair (id1 == id2) is skipped, not treated as a self-loop edge ──

self_pair = {
    "batch_results": [{"pair_id": "0:0", "relation": "supports", "method": "llm_nli_batch"}],
    "pair_claims": {"0:0": (claims[0], claims[0])},
}
stats_self = run_claim_graph_shadow(claims, self_pair, None, log=lambda m: None, verbose=False)
check(
    "a self-pair (same claim_id on both sides) is skipped, not counted as an edge",
    stats_self["edges"] == 0,
    f"{stats_self}",
)

# ── 6. Scope containment: this module never calls claim_graph.py's own extraction, and makes no NLI calls itself ──

src = inspect.getsource(shadow_mod)
check(
    "run_claim_graph_shadow never calls ClaimGraph.extract_claims() "
    "(would stand up a second, independent claim-extraction engine)",
    ".extract_claims(" not in src,
    "",
)
# Precise checks on actual imports/calls, not substring presence anywhere —
# the module's own docstring legitimately *discusses* these names in prose
# while explaining what it deliberately does NOT do, so a blind substring
# search over the whole source (docstring included) would false-positive.
import_lines = [line.strip() for line in src.splitlines() if line.strip().startswith(("import ", "from "))]
check(
    "run_claim_graph_shadow does not import infer_claim_relations_batch/"
    "classify_claim_evidence_batch from anywhere, so it cannot call them "
    "(reuses already-computed results only, zero new NLI calls)",
    not any("infer_claim_relations_batch" in l or "classify_claim_evidence_batch" in l for l in import_lines)
    and any(l == "from agent.claim_graph import Claim, ClaimGraph" for l in import_lines),
    f"{import_lines}",
)
check(
    "run_claim_graph_shadow does not reimplement claim_graph.py's own "
    "_is_contradiction/_is_support regex heuristics as actual method calls",
    "._is_contradiction(" not in src and "._is_support(" not in src
    and "def _is_contradiction" not in src and "def _is_support" not in src,
    "",
)

# ── 7. Scope containment: the production call site never captures this module's return value ──

import agent.orchestrator_v2 as orch_v2_mod
orch_src = inspect.getsource(orch_v2_mod)
check(
    "orchestrator_v2.py calls run_claim_graph_shadow as a bare statement — "
    "its return value is never captured, so it is structurally impossible for it "
    "to influence the answer/Trust/belief/retrieval/coverage",
    "run_claim_graph_shadow(" in orch_src
    and "= run_claim_graph_shadow(" not in orch_src,
    "",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
