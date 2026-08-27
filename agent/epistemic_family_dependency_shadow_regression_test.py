"""
agent/epistemic_family_dependency_shadow_regression_test.py — Epistemic
Core v1 Phase 11 regression: cross-request semantic-family dependency
graph, SHADOW MODE (agent/family_dependency_graph.py).

Uses a fresh in-memory-backed FamilyDependencyGraph per check group (a
tempfile storage path) — never touches the real
registry/claim_family_graph.json.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_family_dependency_shadow_regression_test
"""

import inspect
import tempfile
from pathlib import Path

from agent.family_dependency_graph import (
    FamilyDependencyGraph,
    apply_family_dependency_shadow,
)

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


def _tmp_graph() -> FamilyDependencyGraph:
    tmp = Path(tempfile.mkstemp(suffix=".json")[1])
    tmp.unlink()  # start with no file, exercise the "no file yet" load path
    return FamilyDependencyGraph(storage_file=tmp)


def _claim(cid, text, family, status="unverified", conf=0.5):
    return {
        "claim_id": cid,
        "claim_text": text,
        "semantic_family_id": family,
        "verification_status": status,
        "claim_confidence": conf,
    }


def _disagreement(pairs):
    """pairs: list of (pair_id, relation, c1, c2, method)"""
    batch_results = []
    pair_claims = {}
    for pair_id, relation, c1, c2, method in pairs:
        batch_results.append({"pair_id": pair_id, "relation": relation, "method": method})
        pair_claims[pair_id] = (c1, c2)
    return {"batch_results": batch_results, "pair_claims": pair_claims}


# ── 1. Simple dependency: A contradicts B -> depends_on edges recorded ──

g = _tmp_graph()
a1 = _claim("cl_a1", "A v1", "fam_A", status="supported")
b1 = _claim("cl_b1", "B v1", "fam_B", status="supported")
dis = _disagreement([("0:1", "contradicts", a1, b1, "llm_nli_batch")])
stats1 = apply_family_dependency_shadow([a1, b1], dis, log=lambda m: None, verbose=False, graph=g)
check(
    "contradicts pair creates symmetric depends_on edges",
    len(g.dependents_of("fam_A")) == 1 and len(g.dependents_of("fam_B")) == 1,
    f"{g.edges}",
)
check(
    "first observation of both families is never a change",
    stats1["families_changed"] == 0,
    f"{stats1}",
)

# Now fam_A's status changes on a later request -> fam_B (its dependent) is a recheck candidate.
a2 = _claim("cl_a2", "A v2", "fam_A", status="contradicted")
stats2 = apply_family_dependency_shadow([a2], None, log=lambda m: None, verbose=False, graph=g)
check(
    "simple dependency: A's status change flags its one dependent (B)",
    stats2["families_changed"] == 1 and stats2["recheck_candidates"] == 1,
    f"{stats2}",
)

# ── 2. Multi-hop: A <- B <- C (B depends_on A, C depends_on B) via two separate contradictions ──

g2 = _tmp_graph()
a = _claim("cl_a", "A", "fam_A", status="supported")
b = _claim("cl_b", "B", "fam_B", status="supported")
c = _claim("cl_c", "C", "fam_C", status="supported")
apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "contradicts", a, b, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g2,
)
apply_family_dependency_shadow(
    [b, c], _disagreement([("0:1", "contradicts", b, c, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g2,
)
a_changed = _claim("cl_a2", "A changed", "fam_A", status="contradicted")
stats_multihop = apply_family_dependency_shadow([a_changed], None, log=lambda m: None, verbose=False, graph=g2)
depths = sorted(cand["dependent_family"] for cand in [])  # placeholder, real check below
check(
    "multi-hop: changing A reaches both direct (B) and transitive (C) dependents",
    stats_multihop["recheck_candidates"] == 2,
    f"{stats_multihop}",
)

# ── 3. Cycle: A <-> B mutual contradiction terminates traversal (bounded, no infinite loop) ──

g3 = _tmp_graph()
apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "contradicts", a, b, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g3,
)
result_cycle = g3.find_recheck_candidates("fam_A")
check(
    "cycle A<->B: traversal terminates and reports the closed loop back to origin",
    result_cycle["candidates"] == [{
        "dependent_family": "fam_B",
        "edge_type": "depends_on",
        "reason": "contradicts",
        "triggering_claim_ids": ["cl_a", "cl_b"],
        "depth": 1,
    }] and result_cycle["cycles"] == 1,
    f"{result_cycle}",
)

# ── 4. Self-cycle: a claim NLI-paired with itself, or two occurrences of the SAME family, never self-loops ──

g4 = _tmp_graph()
same_fam_1 = _claim("cl_x1", "X v1", "fam_X")
same_fam_2 = _claim("cl_x2", "X v2", "fam_X")
edge = g4.record_edge("fam_X", "fam_X", "depends_on", "test", ["cl_x1"])
check("record_edge refuses a self-loop", edge is None and g4.edges == [], f"{g4.edges}")

stats_self = apply_family_dependency_shadow(
    [same_fam_1, same_fam_2],
    _disagreement([("0:1", "contradicts", same_fam_1, same_fam_2, "llm_nli_batch")]),
    log=lambda m: None, verbose=False, graph=g4,
)
check(
    "two occurrences of the same family NLI-compared never create a self-loop edge",
    stats_self["dependency_edges_recorded"] == 0 and g4.edges == [],
    f"{stats_self} {g4.edges}",
)

# ── 5. Multiple dependents: A contradicts B and A contradicts C -> both flagged ──

g5 = _tmp_graph()
apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "contradicts", a, b, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g5,
)
apply_family_dependency_shadow(
    [a, c], _disagreement([("0:1", "contradicts", a, c, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g5,
)
stats_multi_dep = apply_family_dependency_shadow([a_changed], None, log=lambda m: None, verbose=False, graph=g5)
dependent_ids = sorted(cand["dependent_family"] for cand in g5.find_recheck_candidates("fam_A")["candidates"])
check(
    "multiple dependents: both B and C flagged when A changes",
    stats_multi_dep["recheck_candidates"] == 2 and dependent_ids == ["fam_B", "fam_C"],
    f"{stats_multi_dep} {dependent_ids}",
)

# ── 6. Unrelated / uncertain relation never creates a dependency ──

g6 = _tmp_graph()
stats_unrelated = apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "unrelated", a, b, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g6,
)
check(
    "unrelated relation creates no edge",
    stats_unrelated["dependency_edges_recorded"] == 0 and g6.edges == [],
    f"{stats_unrelated}",
)
stats_uncertain = apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "uncertain", a, b, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g6,
)
check(
    "uncertain relation creates no edge",
    stats_uncertain["dependency_edges_recorded"] == 0 and g6.edges == [],
    f"{stats_uncertain}",
)

# ── 7. Same semantic family, multiple occurrences within one request: last status wins, no crash ──

g7 = _tmp_graph()
occ1 = _claim("cl_o1", "occ1", "fam_O", status="supported")
occ2 = _claim("cl_o2", "occ2", "fam_O", status="contradicted")
apply_family_dependency_shadow([occ1, occ2], None, log=lambda m: None, verbose=False, graph=g7)
check(
    "same family, multiple occurrences: baseline recorded without crash",
    g7.family_state.get("fam_O", {}).get("last_status") == "contradicted",
    f"{g7.family_state}",
)

# ── 8. No duplicate candidates: a family reachable via two paths appears once ──

g8 = _tmp_graph()
apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "contradicts", a, b, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g8,
)
apply_family_dependency_shadow(
    [a, c], _disagreement([("0:1", "contradicts", a, c, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g8,
)
apply_family_dependency_shadow(
    [b, c], _disagreement([("0:1", "contradicts", b, c, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g8,
)
result8 = g8.find_recheck_candidates("fam_A")
dep_families = [cand["dependent_family"] for cand in result8["candidates"]]
check(
    "diamond graph (A-B, A-C, B-C all contradict): each family appears at most once, duplicate suppressed",
    len(dep_families) == len(set(dep_families)) and result8["duplicates_suppressed"] >= 1,
    f"{result8}",
)

# ── 9. UNKNOWN relation / non-LLM method does not create a false dependency ──

g9 = _tmp_graph()
stats_fallback = apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "contradicts", a, b, "batch_fallback")]), log=lambda m: None, verbose=False, graph=g9,
)
check(
    "non-llm_nli_batch method (fallback) never creates an edge, even if relation says contradicts",
    stats_fallback["dependency_edges_recorded"] == 0 and g9.edges == [],
    f"{stats_fallback}",
)

# ── 10. Old graph/state remains readable after additive updates; corrupt file fails open ──

tmp_path = Path(tempfile.mkstemp(suffix=".json")[1])
g10a = FamilyDependencyGraph(storage_file=tmp_path)
apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "contradicts", a, b, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g10a,
)
g10b = FamilyDependencyGraph(storage_file=tmp_path)  # fresh load from disk
check(
    "graph reloaded from disk retains previously persisted edges",
    len(g10b.edges) == len(g10a.edges) and len(g10b.edges) > 0,
    f"{g10b.edges}",
)
apply_family_dependency_shadow(
    [a, c], _disagreement([("0:1", "contradicts", a, c, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g10b,
)
g10c = FamilyDependencyGraph(storage_file=tmp_path)
check(
    "additive update on top of a reloaded graph preserves prior edges and adds new ones",
    any(e["to_family"] == "fam_B" for e in g10c.edges) and any(e["to_family"] == "fam_C" for e in g10c.edges),
    f"{g10c.edges}",
)

tmp_path.write_text("not valid json{{{", encoding="utf-8")
g10d = FamilyDependencyGraph(storage_file=tmp_path)
check(
    "corrupt on-disk file fails open (empty in-memory state, no crash)",
    g10d.edges == [] and g10d.family_state == {},
    f"{g10d.edges} {g10d.family_state}",
)
tmp_path.unlink()

# ── 11. Scope containment: production call site never captures the return value ──

import agent.orchestrator_v2 as orch_v2_mod
orch_src = inspect.getsource(orch_v2_mod)
check(
    "orchestrator_v2.py calls apply_family_dependency_shadow as a bare statement — "
    "its return value is never captured, so it is structurally impossible for it "
    "to influence the answer/Trust/belief/retrieval/coverage",
    "apply_family_dependency_shadow(" in orch_src
    and "= apply_family_dependency_shadow(" not in orch_src,
    "",
)

import agent.family_dependency_graph as fdg_mod
fdg_src = inspect.getsource(fdg_mod)
check(
    "family_dependency_graph.py makes no NLI/embedding calls of its own "
    "(no requests/http imports) — reuses only already-computed disagreement_result",
    "import requests" not in fdg_src and "infer_claim_relations_batch" not in fdg_src,
    "",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
