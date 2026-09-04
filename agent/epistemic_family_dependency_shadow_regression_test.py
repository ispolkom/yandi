"""
agent/epistemic_family_dependency_shadow_regression_test.py — Epistemic
Core v1 Phase 11 regression: cross-request semantic-family dependency
graph, SHADOW MODE (agent/family_dependency_graph.py).

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): FamilyDependencyGraph is SQL-only
now (semantic_edge + family_status_state + recheck_event), no
storage_file, no `.edges`/`.family_state`/`.recheck_log` in-memory
attributes to inspect directly from tests. A small fake claim_family/
semantic_edge/family_status_state/recheck_event connection stands in
for the real bastion-protected tables, patched fresh per graph via
_fresh_graph() below.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_family_dependency_shadow_regression_test
"""

import contextlib
import inspect
import json

from agent.family_dependency_graph import (
    FamilyDependencyGraph,
    apply_family_dependency_shadow,
)
import agent.family_dependency_graph as fdg_mod

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


class _FDFakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None
        self._results = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        upper = " ".join(sql.split()).upper()
        self._result = None
        self._results = None
        conn = self.conn
        if upper.startswith("INSERT IGNORE INTO CLAIM_FAMILY"):
            family_id, domain, canonical_text, created_at, updated_at = params
            conn.families.setdefault(family_id, {
                "family_id": family_id, "domain": domain, "canonical_text": canonical_text,
                "created_at": created_at, "updated_at": updated_at,
            })
        elif upper.startswith("SELECT * FROM SEMANTIC_EDGE WHERE FAMILY_A=%S AND FAMILY_B=%S AND EDGE_TYPE=%S"):
            fam_a, fam_b, edge_type = params
            self._result = next(
                (dict(e) for e in conn.edges.values() if e["family_a"] == fam_a and e["family_b"] == fam_b and e["edge_type"] == edge_type),
                None,
            )
        elif upper.startswith("INSERT INTO SEMANTIC_EDGE"):
            edge_id, family_a, family_b, edge_type, reason, triggering, created_at, last_seen_at = params
            conn.edges[edge_id] = {
                "edge_id": edge_id, "family_a": family_a, "family_b": family_b, "edge_type": edge_type,
                "reason": reason, "observation_count": 1, "triggering_claim_ids": triggering,
                "created_at": created_at, "last_seen_at": last_seen_at,
            }
        elif upper.startswith("UPDATE SEMANTIC_EDGE SET OBSERVATION_COUNT"):
            last_seen_at, triggering, edge_id = params
            e = conn.edges[edge_id]
            e["observation_count"] += 1
            e["last_seen_at"] = last_seen_at
            e["triggering_claim_ids"] = triggering
        elif upper.startswith("SELECT * FROM SEMANTIC_EDGE WHERE FAMILY_B=%S AND EDGE_TYPE='DEPENDS_ON'"):
            (family_b,) = params
            self._results = [dict(e) for e in conn.edges.values() if e["family_b"] == family_b and e["edge_type"] == "depends_on"]
        elif upper.startswith("SELECT * FROM SEMANTIC_EDGE WHERE EDGE_TYPE='CONTRADICTS'"):
            self._results = [dict(e) for e in conn.edges.values() if e["edge_type"] == "contradicts"]
        elif upper.startswith("SELECT * FROM FAMILY_STATUS_STATE WHERE FAMILY_ID=%S"):
            (family_id,) = params
            self._result = dict(conn.status[family_id]) if family_id in conn.status else None
        elif upper.startswith("INSERT INTO FAMILY_STATUS_STATE"):
            family_id, last_status, updated_at = params
            conn.status[family_id] = {"family_id": family_id, "last_status": last_status, "updated_at": updated_at}
        elif upper.startswith("SELECT * FROM RECHECK_EVENT WHERE FAMILY_ID=%S ORDER BY STARTED_AT DESC LIMIT 1"):
            (family_id,) = params
            rows = [r for r in conn.rechecks if r["family_id"] == family_id]
            rows.sort(key=lambda r: r["started_at"])
            self._result = dict(rows[-1]) if rows else None
        elif upper.startswith("INSERT INTO RECHECK_EVENT"):
            family_id, run_id, trigger_reason, started_at, outcome, reason = params
            conn.rechecks.append({
                "family_id": family_id, "run_id": run_id, "trigger_reason": trigger_reason,
                "started_at": started_at, "outcome": outcome, "reason": reason,
            })

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results or []


class _FDFakeConnection:
    def __init__(self):
        self.families = {}
        self.edges = {}
        self.status = {}
        self.rechecks = []

    def cursor(self):
        return _FDFakeCursor(self)

    def commit(self):
        pass


def _fresh_graph():
    """A brand-new FamilyDependencyGraph backed by a fresh, empty fake
    claim_family/semantic_edge/family_status_state/recheck_event store."""
    conn = _FDFakeConnection()

    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    fdg_mod.get_connection = _fake_get_connection
    return FamilyDependencyGraph()


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

g = _fresh_graph()
a1 = _claim("cl_a1", "A v1", "fam_A", status="supported")
b1 = _claim("cl_b1", "B v1", "fam_B", status="supported")
dis = _disagreement([("0:1", "contradicts", a1, b1, "llm_nli_batch")])
stats1 = apply_family_dependency_shadow([a1, b1], dis, log=lambda m: None, verbose=False, graph=g)
check(
    "contradicts pair creates symmetric depends_on edges",
    len(g.dependents_of("fam_A")) == 1 and len(g.dependents_of("fam_B")) == 1,
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

g2 = _fresh_graph()
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
check(
    "multi-hop: changing A reaches both direct (B) and transitive (C) dependents",
    stats_multihop["recheck_candidates"] == 2,
    f"{stats_multihop}",
)

# ── 3. Cycle: A <-> B mutual contradiction terminates traversal (bounded, no infinite loop) ──

g3 = _fresh_graph()
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

g4 = _fresh_graph()
same_fam_1 = _claim("cl_x1", "X v1", "fam_X")
same_fam_2 = _claim("cl_x2", "X v2", "fam_X")
edge = g4.record_edge("fam_X", "fam_X", "depends_on", "test", ["cl_x1"])
check("record_edge refuses a self-loop", edge is None and g4.dependents_of("fam_X") == [])

stats_self = apply_family_dependency_shadow(
    [same_fam_1, same_fam_2],
    _disagreement([("0:1", "contradicts", same_fam_1, same_fam_2, "llm_nli_batch")]),
    log=lambda m: None, verbose=False, graph=g4,
)
check(
    "two occurrences of the same family NLI-compared never create a self-loop edge",
    stats_self["dependency_edges_recorded"] == 0,
    f"{stats_self}",
)

# ── 5. Multiple dependents: A contradicts B and A contradicts C -> both flagged ──

g5 = _fresh_graph()
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

g6 = _fresh_graph()
stats_unrelated = apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "unrelated", a, b, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g6,
)
check(
    "unrelated relation creates no edge",
    stats_unrelated["dependency_edges_recorded"] == 0,
    f"{stats_unrelated}",
)
stats_uncertain = apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "uncertain", a, b, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g6,
)
check(
    "uncertain relation creates no edge",
    stats_uncertain["dependency_edges_recorded"] == 0,
    f"{stats_uncertain}",
)

# ── 7. Same semantic family, multiple occurrences within one request: last status wins, no crash ──

g7 = _fresh_graph()
occ1 = _claim("cl_o1", "occ1", "fam_O", status="supported")
occ2 = _claim("cl_o2", "occ2", "fam_O", status="contradicted")
apply_family_dependency_shadow([occ1, occ2], None, log=lambda m: None, verbose=False, graph=g7)
check(
    "same family, multiple occurrences: baseline recorded without crash",
    g7.observe_family_status("fam_O", "contradicted")["previous_status"] == "contradicted",
)

# ── 8. No duplicate candidates: a family reachable via two paths appears once ──

g8 = _fresh_graph()
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

g9 = _fresh_graph()
stats_fallback = apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "contradicts", a, b, "batch_fallback")]), log=lambda m: None, verbose=False, graph=g9,
)
check(
    "non-llm_nli_batch method (fallback) never creates an edge, even if relation says contradicts",
    stats_fallback["dependency_edges_recorded"] == 0,
    f"{stats_fallback}",
)

# ── 10. Persistence: a SECOND FamilyDependencyGraph() sharing the SAME connection sees identical state ──

g10a = _fresh_graph()
apply_family_dependency_shadow(
    [a, b], _disagreement([("0:1", "contradicts", a, b, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g10a,
)
g10b = FamilyDependencyGraph()  # still wired to the same fake connection via fdg_mod.get_connection
check(
    "a brand-new FamilyDependencyGraph() instance, same connection, sees the SAME edges immediately",
    len(g10b.dependents_of("fam_A")) == len(g10a.dependents_of("fam_A")) and len(g10a.dependents_of("fam_A")) > 0,
)
apply_family_dependency_shadow(
    [a, c], _disagreement([("0:1", "contradicts", a, c, "llm_nli_batch")]), log=lambda m: None, verbose=False, graph=g10b,
)
g10c = FamilyDependencyGraph()
check(
    "additive update on top of a shared graph preserves prior edges and adds new ones",
    any(e["from_family"] == "fam_B" for e in g10c.dependents_of("fam_A"))
    and any(e["from_family"] == "fam_C" for e in g10c.dependents_of("fam_A")),
    f"{g10c.dependents_of('fam_A')}",
)

# ── 11. Scope containment: structural inertness w.r.t. THIS request ──
#
# Epistemic Core v1 Phase 12 (agent/dependency_recheck.py) legitimately
# reads apply_family_dependency_shadow()'s return value now (to decide
# what to recheck), so "return value never captured at the call site" is
# no longer the right invariant to test. What must still hold — and is
# checked here structurally, not by trusting the call site's style — is
# that this function itself never mutates claims_data and never even
# HAS a parameter through which it could reach synthesis_result/Trust/
# evidence_data. That is what actually makes it impossible for THIS
# function to influence the current request's own answer/Trust/coverage,
# regardless of who reads its return value afterward.

_sig = inspect.signature(fdg_mod.apply_family_dependency_shadow)
check(
    "apply_family_dependency_shadow has no synthesis_result/trust/evidence_data/"
    "belief_manager parameter — structurally cannot reach those subsystems",
    not any(
        p in _sig.parameters
        for p in ("synthesis_result", "trust", "evidence_data", "belief_manager")
    ),
    f"{list(_sig.parameters)}",
)

fdg_src = inspect.getsource(fdg_mod.apply_family_dependency_shadow)
check(
    "apply_family_dependency_shadow never assigns into a claim/pair dict "
    "(only reads claims_data via .get) — cannot mutate verification_status "
    "or any other claim field as a side effect",
    not any(
        pattern in fdg_src
        for pattern in ("claim[", "c1[", "c2[", "c[")
    ),
    "",
)

import agent.orchestrator_v2 as orch_v2_mod
orch_src = inspect.getsource(orch_v2_mod)
check(
    "orchestrator_v2.py calls apply_dependency_recheck (Phase 12) as a bare "
    "statement — its return value is never captured, so it is structurally "
    "impossible for it to influence THIS request's own answer/Trust/coverage",
    "apply_dependency_recheck(" in orch_src
    and "= apply_dependency_recheck(" not in orch_src,
    "",
)

check(
    "family_dependency_graph.py makes no NLI/embedding calls of its own "
    "(no requests/http imports) — reuses only already-computed disagreement_result",
    "import requests" not in inspect.getsource(fdg_mod)
    and "infer_claim_relations_batch" not in inspect.getsource(fdg_mod),
    "",
)

# ── 12. Fail LOUD, not fail-open: SQL genuinely unreachable raises SqlUnavailable
#      (replaces the retired "corrupt JSON file" scenario, which has no SQL
#      equivalent — "точка ноль": there is no more file-based fallback). ──

from agent.db.sql.connection import SqlUnavailable

g12 = FamilyDependencyGraph()  # still wired to the last fake connection


def _raise_unavailable(autocommit=False):
    raise SqlUnavailable("forced unreachable for this test")


from unittest.mock import patch as _patch
with _patch.object(fdg_mod, "get_connection", _raise_unavailable):
    raised = False
    try:
        g12.observe_family_status("fam_Z", "supported")
    except SqlUnavailable:
        raised = True
    check(
        "with SQL genuinely unreachable, observe_family_status() raises SqlUnavailable — the "
        "deliberate opposite of the retired JSON fail-safe (\"corrupt file -> start empty, never crash\")",
        raised,
    )

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
