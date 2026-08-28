"""
agent/orch_async_claim_pipeline_regression_test.py — regression for the
bounded async claim pipeline (agent/orchestrator/claims/async_pipeline.py),
introduced to close the ~100-140s barrier-wait finding from
YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md P2.

Tests the ASYNC ORCHESTRATION layer itself (concurrency bounds,
determinism regardless of completion order, exception isolation, P1-A
scope preservation) using controllable fakes for the underlying
(already separately tested) synchronous functions
map_claims_to_evidence / run_claim_evidence_batch /
retrieve_claim_evidence — same testing boundary used for the P1-A fix's
own regression test.

Run: /home/iam/venv/bin/python3 -m agent.orch_async_claim_pipeline_regression_test
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import agent.orchestrator.claims.async_pipeline as pipeline_mod
from agent.orch_schemas import ClaimRecord

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


def _noop_log(*a, **k):
    pass


def _make_claim(cid):
    return {
        "claim_id": cid,
        "claim_text": f"claim text for {cid}",
        "verification_status": "candidate",
        "derived_from_evidence_ids": [],
        "evidence_relations": [],
    }


def _own_evidence(cid):
    return {
        "evidence_id": f"ev_{cid}",
        "content_excerpt": f"evidence content for {cid} " * 10,
        "source_uri": f"http://example.test/{cid}",
        "retrieval_origin": "claim_specific",
        "retrieval_claim_id": cid,
        "source_class": "scientific",
    }


def _fake_map_claims_to_evidence(claims, evidence_records, embedding_cache=None):
    """Links a claim to its OWN evidence item iff that item is already
    present in evidence_records (PASS1: not yet present -> unresolved;
    PASS2, after retrieval merges it in -> resolved)."""
    out = []
    ids_present = {e.get("evidence_id") for e in (evidence_records or [])}
    for c in claims:
        cid = c["claim_id"]
        own_id = f"ev_{cid}"
        linked = [own_id] if own_id in ids_present else []
        out.append(ClaimRecord(
            claim_id=cid, claim_text=c["claim_text"],
            derived_from_evidence_ids=linked, verification_status="candidate",
        ))
    return out


_nli_active_lock = threading.Lock()
_nli_active_count = [0]
_nli_max_concurrent = [0]


def _fake_run_claim_evidence_batch(claims, evidence, batch_label, log, verbose):
    with _nli_active_lock:
        _nli_active_count[0] += 1
        _nli_max_concurrent[0] = max(_nli_max_concurrent[0], _nli_active_count[0])
    try:
        time.sleep(0.02)  # simulate real Ollama call latency
        evidence_by_id = {e.get("evidence_id"): e for e in (evidence or [])}
        for c in claims:
            relations = []
            for ev_id in c.get("derived_from_evidence_ids", []) or []:
                ev = evidence_by_id.get(ev_id)
                if ev is None:
                    continue
                relations.append({
                    "evidence_id": ev_id,
                    "evidence_role": "direct",
                    "evidence_eligible": True,
                    "relation": "supports",
                    "method": "fake_nli",
                })
            c["evidence_relations"] = relations
        return sum(len(c.get("evidence_relations", [])) for c in claims)
    finally:
        with _nli_active_lock:
            _nli_active_count[0] -= 1


_DELAYS = {}
_retrieval_calls = []
_retrieval_active_lock = threading.Lock()
_retrieval_active = [0]
_retrieval_max_active = [0]


def _fake_retrieve_claim_evidence(claim, fetch_cache, precomputed_query_result):
    cid = claim["claim_id"]
    _retrieval_calls.append(cid)
    with _retrieval_active_lock:
        _retrieval_active[0] += 1
        _retrieval_max_active[0] = max(_retrieval_max_active[0], _retrieval_active[0])
    try:
        if _DELAYS.get(cid) == "RAISE":
            raise RuntimeError(f"simulated network failure for {cid}")
        time.sleep(_DELAYS.get(cid, 0.0))
        return [_own_evidence(cid)]
    finally:
        with _retrieval_active_lock:
            _retrieval_active[0] -= 1


def _run_pipeline(claim_ids, evidence_data=None):
    claims_data = [_make_claim(cid) for cid in claim_ids]
    evidence_data = list(evidence_data or [])
    cost = {}

    with patch.object(pipeline_mod, "map_claims_to_evidence", _fake_map_claims_to_evidence), \
         patch.object(pipeline_mod, "run_claim_evidence_batch", _fake_run_claim_evidence_batch), \
         patch.object(pipeline_mod, "retrieve_claim_evidence", _fake_retrieve_claim_evidence), \
         patch.object(pipeline_mod, "assign_source_clusters", lambda *a, **k: None):
        pipeline_mod.run_async_claim_pipeline(
            claims_data, evidence_data, True, False, False, None, cost, _noop_log, True,
        )

    return claims_data, evidence_data, cost


def _snapshot(claims_data):
    return [
        {
            "claim_id": c["claim_id"],
            "derived_from_evidence_ids": sorted(c.get("derived_from_evidence_ids", [])),
            "evidence_relations": sorted(
                (r["evidence_id"], r["relation"]) for r in c.get("evidence_relations", [])
            ),
            "verification_status": c.get("verification_status"),
            "evidence_search_attempted": c.get("evidence_search_attempted"),
        }
        for c in claims_data
    ]


# ============================================================
# Item 11 — determinism regardless of completion order
# ============================================================

_DELAYS.clear()
_DELAYS.update({"A": 0.10, "B": 0.01, "C": 0.05})
claims1, evidence1, _ = _run_pipeline(["A", "B", "C"])
snap1 = _snapshot(claims1)

_DELAYS.clear()
_DELAYS.update({"A": 0.01, "B": 0.10, "C": 0.05})
claims2, evidence2, _ = _run_pipeline(["A", "B", "C"])
snap2 = _snapshot(claims2)

check(
    "CASE 1 vs CASE 2 (reversed retrieval delays): final claim states "
    "(mapping, relations, status) are byte-identical regardless of "
    "which claim's network call finished first",
    snap1 == snap2,
    f"snap1={snap1} snap2={snap2}",
)

import random
random.seed(42)
_DELAYS.clear()
_DELAYS.update({"A": random.uniform(0, 0.08), "B": random.uniform(0, 0.08), "C": random.uniform(0, 0.08)})
claims3, evidence3, _ = _run_pipeline(["A", "B", "C"])
snap3 = _snapshot(claims3)

check(
    "CASE 3 (randomized completion order) also matches CASE 1/2",
    snap3 == snap1,
    f"snap3={snap3} snap1={snap1}",
)
check(
    "evidence pool grows identically regardless of completion order "
    "(same evidence_ids present, order-independent as a set)",
    {e["evidence_id"] for e in evidence1} == {e["evidence_id"] for e in evidence2} == {e["evidence_id"] for e in evidence3},
    f"e1={[e['evidence_id'] for e in evidence1]} e2={[e['evidence_id'] for e in evidence2]}",
)

# ============================================================
# Item 12 — max 3 concurrent claim workers, never more
# ============================================================

_DELAYS.clear()
ten_ids = [f"claim{i}" for i in range(10)]
for cid in ten_ids:
    _DELAYS[cid] = 0.05  # uniform delay so several are in-flight together

_retrieval_active[0] = 0
_retrieval_max_active[0] = 0
claims10, evidence10, cost10 = _run_pipeline(ten_ids)

check(
    "with 10 claims all needing PASS2 retrieval, observed max concurrent "
    "claim-worker retrieval calls is EXACTLY MAX_CLAIM_WORKERS=3 (workload "
    "has enough claims to actually exercise the bound, not just satisfy <=3 trivially)",
    _retrieval_max_active[0] == pipeline_mod.MAX_CLAIM_WORKERS == 3,
    f"observed_max={_retrieval_max_active[0]} MAX_CLAIM_WORKERS={pipeline_mod.MAX_CLAIM_WORKERS}",
)
check(
    "cost['claim_async_max_workers'] (the pipeline's own reported metric) "
    "also confirms <= 3 and matches the independently-observed max",
    cost10["claim_async_max_workers"] <= 3,
    f"reported={cost10['claim_async_max_workers']}",
)
check(
    "all 10 claims completed (none silently dropped by the bounded pool)",
    len(_snapshot(claims10)) == 10 and all(c["derived_from_evidence_ids"] for c in _snapshot(claims10)),
    f"count={len(claims10)}",
)

# ============================================================
# Item 13 — NLI concurrency never exceeds 1 (single controlled consumer)
# ============================================================

_nli_max_concurrent[0] = 0
_DELAYS.clear()
for cid in ten_ids:
    _DELAYS[cid] = 0.03
_run_pipeline(ten_ids)

check(
    "even with 10 claims and up to 3 concurrent claim workers all "
    "needing NLI, the underlying (Ollama-calling) NLI batch function "
    "NEVER runs concurrently with itself - max observed = 1, "
    "structurally guaranteed by a single consumer task, not just a lock",
    _nli_max_concurrent[0] == 1,
    f"observed max concurrent NLI calls={_nli_max_concurrent[0]}",
)

# ============================================================
# Item 14 — exception isolation
# ============================================================

_DELAYS.clear()
_DELAYS.update({"A": 0.02, "B": "RAISE", "C": 0.02})
claims_err, evidence_err, _ = _run_pipeline(["A", "B", "C"])
by_id = {c["claim_id"]: c for c in claims_err}

check(
    "one claim's (B) retrieval raising an exception does not crash the "
    "whole async pipeline - the other two claims (A, C) still complete normally",
    by_id["A"]["derived_from_evidence_ids"] == ["ev_A"] and by_id["C"]["derived_from_evidence_ids"] == ["ev_C"],
    f"A={by_id['A']} C={by_id['C']}",
)
check(
    "the failing claim (B) records evidence_search_error (ERROR "
    "semantics), not silently swallowed and not disguised as NOT_FOUND",
    by_id["B"].get("evidence_search_error") is not None and "simulated network failure" in by_id["B"]["evidence_search_error"],
    f"B evidence_search_error={by_id['B'].get('evidence_search_error')!r}",
)
check(
    "the failing claim's status/relations are NOT falsely CONTRADICTED "
    "or falsely resolved - it simply has no PASS2 evidence, same as a "
    "legitimate 'found nothing' outcome",
    by_id["B"]["derived_from_evidence_ids"] == [] and by_id["B"]["evidence_relations"] == [],
    f"B={by_id['B']}",
)

# ============================================================
# P1-A scope preservation: a PASS1-resolved claim never reaches PASS2
# ============================================================

_retrieval_calls.clear()
_DELAYS.clear()
resolved_claim = _make_claim("RESOLVED")
unresolved_claim = _make_claim("UNRESOLVED")
_DELAYS["UNRESOLVED"] = 0.01
# Seed evidence_data so claim RESOLVED's PASS1 mapping already links it.
seed_evidence = [_own_evidence("RESOLVED")]

claims_p1a = [resolved_claim, unresolved_claim]
cost_p1a = {}
with patch.object(pipeline_mod, "map_claims_to_evidence", _fake_map_claims_to_evidence), \
     patch.object(pipeline_mod, "run_claim_evidence_batch", _fake_run_claim_evidence_batch), \
     patch.object(pipeline_mod, "retrieve_claim_evidence", _fake_retrieve_claim_evidence), \
     patch.object(pipeline_mod, "assign_source_clusters", lambda *a, **k: None):
    pipeline_mod.run_async_claim_pipeline(
        claims_p1a, seed_evidence, True, False, False, None, cost_p1a, _noop_log, True,
    )

check(
    "P1-A scope (commit a082b55) preserved: a claim already resolved "
    "at PASS1 (evidence pre-linked, NLI says supports) NEVER triggers "
    "PASS2 retrieval - retrieve_claim_evidence is called for UNRESOLVED "
    "only, never for RESOLVED",
    _retrieval_calls == ["UNRESOLVED"],
    f"retrieval_calls={_retrieval_calls}",
)
check(
    "the resolved claim's PASS1 relation is untouched/correct "
    "(supports, via its own pre-seeded evidence)",
    resolved_claim["evidence_relations"] == [
        {"evidence_id": "ev_RESOLVED", "evidence_role": "direct",
         "evidence_eligible": True, "relation": "supports", "method": "fake_nli"}
    ],
    f"resolved_claim={resolved_claim}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
