"""
agent/epistemic_source_cluster_regression_test.py — Epistemic Core v1
Phase 6 regression: source_cluster_id metadata
(agent/source_clustering.py::assign_source_clusters()), wired into
claims/lifecycle.py and claims/retrieval.py, persisted via
EvidenceRecord/Trace.

Proves: a syndicated family gets a shared cluster_id, independent
sources stay in separate (singleton) clusters, a comparison FAILURE
never causes a merge (fails open), the computation is deterministic
across repeated calls on an unchanged evidence list, backward
compatibility with EvidenceRecord objects that predate this field, round
trip through Trace, and — critically — that nothing in
claims/status.py reads source_cluster_id yet (metadata-only this phase,
per the plan; Phase 7 is the separate, deliberate step that would change
that).

Run: /home/iam/venv/bin/python3 -m agent.epistemic_source_cluster_regression_test
"""

import inspect
import json
import time
from unittest.mock import patch

from agent.source_clustering import assign_source_clusters
import agent.source_clustering as clustering_mod
from agent.orch_schemas import EvidenceRecord
from agent.orch_tracer import Trace

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


def _ev(eid, url, title, text):
    return {
        "evidence_id": eid,
        "source_uri": url,
        "source_title": title,
        "content_excerpt": text,
    }


# ── 1. A syndicated family (cross-domain, near-identical content) shares one cluster ──

syndicated_pool = [
    _ev("ev_s1", "https://outlet-a.example.com/story",
        "Central bank holds rates steady, cites inflation concerns",
        "The central bank left its benchmark interest rate unchanged on Wednesday, citing persistent inflation concerns and signaling that further hikes remain possible."),
    _ev("ev_s2", "https://outlet-b.example.com/news",
        "Rates unchanged as inflation worries persist",
        "The central bank left its benchmark interest rate unchanged on Wednesday, citing persistent inflation concerns, and signaled further hikes remain possible."),
    _ev("ev_s3", "https://unrelated-outlet.example.org/other",
        "Local team wins championship after decade-long drought",
        "The city's baseball team won its first championship in over a decade last night after a dramatic extra-innings victory."),
]

assign_source_clusters(syndicated_pool)
ids_by_ev = {e["evidence_id"]: e["source_cluster_id"] for e in syndicated_pool}

check(
    "syndicated cross-domain pair shares one cluster_id",
    ids_by_ev["ev_s1"] == ids_by_ev["ev_s2"] and ids_by_ev["ev_s1"] is not None,
    f"{ids_by_ev}",
)
check(
    "unrelated third item gets its own distinct cluster_id",
    ids_by_ev["ev_s3"] != ids_by_ev["ev_s1"],
    f"{ids_by_ev}",
)

# ── 2. Independent sources (different domains, different content) stay in separate singleton clusters ──

independent_pool = [
    _ev("ev_i1", "https://site-x.example.com/a", "Tachyon particles explained simply",
        "A tachyon is a hypothetical particle that always travels faster than light and has never been experimentally observed."),
    _ev("ev_i2", "https://site-y.example.net/b", "Classic borscht recipe",
        "For a classic borscht you will need beets, cabbage, potatoes, carrots and a splash of vinegar to keep the color bright."),
]
assign_source_clusters(independent_pool)
check(
    "two genuinely independent sources get different cluster_ids",
    independent_pool[0]["source_cluster_id"] != independent_pool[1]["source_cluster_id"],
    f"{[e['source_cluster_id'] for e in independent_pool]}",
)

# ── 3. FAILS OPEN: a comparison error never causes a merge ──

error_pool = [
    _ev("ev_e1", "https://a.example.com/x", "Some title", "Some content excerpt here for testing purposes only."),
    _ev("ev_e2", "https://b.example.com/y", "Some title", "Some content excerpt here for testing purposes only."),
]
with patch.object(clustering_mod, "title_similarity", side_effect=RuntimeError("simulated failure")):
    assign_source_clusters(error_pool)
check(
    "comparison error -> NOT merged (fails open, never a confident-but-wrong merge)",
    error_pool[0]["source_cluster_id"] != error_pool[1]["source_cluster_id"],
    f"{[e['source_cluster_id'] for e in error_pool]}",
)

# ── 4. Deterministic across repeated calls on an unchanged list ──

pool_a = [dict(e) for e in syndicated_pool]
for e in pool_a:
    e.pop("source_cluster_id", None)
assign_source_clusters(pool_a)
result_1 = {e["evidence_id"]: e["source_cluster_id"] for e in pool_a}
assign_source_clusters(pool_a)
result_2 = {e["evidence_id"]: e["source_cluster_id"] for e in pool_a}
check(
    "assign_source_clusters is deterministic across repeated calls on an unchanged pool",
    result_1 == result_2,
    f"{result_1} != {result_2}",
)

# ── 5. Evidence items missing evidence_id are skipped, not crashed on ──

pool_missing_id = [
    {"source_uri": "https://x.example.com/a", "source_title": "t", "content_excerpt": "c"},
    _ev("ev_ok", "https://y.example.com/b", "t2", "c2"),
]
try:
    assign_source_clusters(pool_missing_id)
    check(
        "evidence item without evidence_id is skipped, no crash, valid item still gets a cluster_id",
        "source_cluster_id" not in pool_missing_id[0] and pool_missing_id[1].get("source_cluster_id") is not None,
        f"{pool_missing_id}",
    )
except Exception as e:
    check("evidence item without evidence_id is skipped, no crash", False, repr(e))

# ── 6. Backward compatibility: EvidenceRecord without source_cluster_id kwarg ──

try:
    old_style = EvidenceRecord(evidence_id="ev_old", source_type="web")
    check(
        "EvidenceRecord constructed without source_cluster_id kwarg defaults to None",
        old_style.source_cluster_id is None,
    )
except Exception as e:
    check("EvidenceRecord constructed without source_cluster_id kwarg defaults to None", False, repr(e))

# ── 7. Round trip through Trace: add_evidence() -> to_dict() -> json ──

trace = Trace(trace_id="t_test", timestamp=time.time(), query="test")
trace.add_evidence(EvidenceRecord(
    evidence_id="ev_rt1",
    source_type="web",
    source_uri="https://a.example.com/x",
    source_cluster_id="sc_ev_rt1",
))
trace.add_evidence(EvidenceRecord(
    evidence_id="ev_rt2",
    source_type="web",
    source_uri="https://b.example.com/y",
    # no source_cluster_id passed -> None
))
rt = json.loads(json.dumps(trace.to_dict(), ensure_ascii=False))
by_id = {e["evidence_id"]: e for e in rt["evidence"]}
check(
    "round trip: source_cluster_id survives serialization when set",
    by_id["ev_rt1"]["source_cluster_id"] == "sc_ev_rt1",
    f"{by_id.get('ev_rt1')}",
)
check(
    "round trip: source_cluster_id survives as null when not set (backward compat)",
    by_id["ev_rt2"]["source_cluster_id"] is None,
    f"{by_id.get('ev_rt2')}",
)

# ── 8. Scope containment: claims/status.py does NOT read source_cluster_id this phase ──

import agent.orchestrator.claims.status as status_mod
status_src = inspect.getsource(status_mod)
check(
    "claims/status.py does not reference source_cluster_id — metadata-only this phase, "
    "per the plan ('NOT change support_count in this commit'); Phase 7 is the separate step",
    "source_cluster_id" not in status_src,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
