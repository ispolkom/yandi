"""
agent/verification_memory_regression_test.py — Этап 3 (P5) regression:
verification memory SAVE (agent/verification_memory.py::
persist_verification_evidence, agent/orch_tracer.py::save_trace/
add_claim_raw/to_dict), LOAD (lookup_historical_evidence,
_process_one_claim's MEMORY PASS in agent/orchestrator/claims/
async_pipeline.py), and the "MEMORY != TRUTH" invariants (P4 §9/§10:
historical relations are never copied as truth, and alone can never
skip PASS2).

CRITICAL: registry/index.db and registry/dataset/orch_traces/*.jsonl
are REAL, accumulated state — every test here patches agent.
verification_memory.INDEX_DB/TRACES_DIR and agent.orch_tracer.TRACES_DIR
to isolated temp paths (same discipline as orch_stoplist_regression_test.py
after its real-file leak incident earlier this session).

Run: /home/iam/venv/bin/python3 -m agent.verification_memory_regression_test
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import agent.orch_tracer as ot
import agent.verification_memory as vm
import agent.orchestrator.claims.async_pipeline as pipeline_mod
from agent.orch_schemas import EvidenceRecord, ClaimRecord
from agent.claim_identity import compute_claim_content_hash
from agent.source_clustering import assign_source_clusters

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


def _isolated_paths():
    traces = Path(tempfile.mkdtemp(prefix="yandi_vm_test_traces_"))
    index = Path(tempfile.mkdtemp(prefix="yandi_vm_test_index_")) / "index.db"
    return traces, index


def _patches(traces_dir, index_db):
    return (
        patch.object(ot, "TRACES_DIR", traces_dir),
        patch.object(vm, "TRACES_DIR", traces_dir),
        patch.object(vm, "INDEX_DB", index_db),
    )


# ============================================================
# A. FULL SAVE: retrieval_claim_id / source_cluster_id / evidence /
#    relation all present in the persisted Trace JSON.
# ============================================================

traces_a, index_a = _isolated_paths()
p1, p2, p3 = _patches(traces_a, index_a)

with p1, p2, p3:
    claim_a = {
        "claim_id": "cl_a1",
        "claim_text": "Луна вращается вокруг Земли.",
        "content_hash": compute_claim_content_hash("Луна вращается вокруг Земли."),
        "derived_from_evidence_ids": ["ev_a1"],
        "verification_status": "supported",
        "evidence_relations": [
            {"evidence_id": "ev_a1", "relation": "supports", "method": "nli",
             "source_claim": "Луна — естественный спутник Земли.",
             "source_class": "reference", "evidence_eligible": True,
             "evidence_role": "direct", "directness": 0.9, "retrieval_origin": "claim_specific"},
        ],
    }
    evidence_data_a = [{
        "evidence_id": "ev_a1", "source_type": "web", "source_uri": "https://moon.example/facts",
        "source_title": "Moon facts", "content_excerpt": "Луна — естественный спутник Земли.",
        "quality_score": 0.85, "source_class": "reference", "evidence_eligible": True,
        "evidence_role": "direct", "authority": 0.8, "traceability": 0.8, "primaryness": 0.7,
        "retrieval_origin": "claim_specific", "retrieval_claim_id": "cl_a1",
        "source_cluster_id": "sc_ev_a1",
    }]

    trace_a = ot.Trace(trace_id="t_a", timestamp=time.time(), query="Вращается ли Луна вокруг Земли?")
    trace_a.add_claim_raw(claim_a)
    vm.persist_verification_evidence(trace_a, [claim_a], evidence_data_a)

    saved = trace_a.to_dict()
    saved_ev = saved["evidence"][0] if saved["evidence"] else {}
    saved_claim = saved["claims"][0]

    check(
        "A: persisted EvidenceRecord carries retrieval_claim_id",
        saved_ev.get("retrieval_claim_id") == "cl_a1",
        f"{saved_ev}",
    )
    check(
        "A: persisted EvidenceRecord carries source_cluster_id AS-IS (not recomputed)",
        saved_ev.get("source_cluster_id") == "sc_ev_a1",
        f"{saved_ev}",
    )
    check(
        "A: persisted claim carries its evidence_relations (relation=supports)",
        saved_claim.get("evidence_relations", [{}])[0].get("relation") == "supports",
        f"{saved_claim}",
    )
    check(
        "A: persisted evidence is present at all (not dropped)",
        len(saved["evidence"]) == 1,
        f"{saved['evidence']}",
    )

# ============================================================
# B. PASS2 EVIDENCE PERSISTENCE: not just stage-6's first 3 snippets —
#    evidence used by claim-specific PASS2 reaches the Trace too.
# ============================================================

traces_b, index_b = _isolated_paths()
p1, p2, p3 = _patches(traces_b, index_b)

with p1, p2, p3:
    # Simulate: 1 stage-6 evidence item (route=internet, global,
    # unrelated to any claim) + 5 PASS2 claim-owned items actually used
    # by claims, + 3 more PASS2-discovered items that were fetched but
    # got NO evidence_relation anywhere (rejected/irrelevant noise) —
    # "FETCHED SOURCE != VERIFICATION EVIDENCE" (P4 §2).
    claims_b = [{
        "claim_id": "cl_b1",
        "claim_text": "Тестовый claim B",
        "content_hash": compute_claim_content_hash("Тестовый claim B"),
        "evidence_relations": [
            {"evidence_id": f"ev_b{i}", "relation": "supports", "method": "nli"}
            for i in range(1, 6)
        ],
    }]
    evidence_data_b = (
        [{"evidence_id": "ev_stage6_unused", "source_uri": "https://x.example/unused",
          "content_excerpt": "irrelevant noise", "retrieval_origin": "initial_web"}]
        + [{"evidence_id": f"ev_b{i}", "source_uri": f"https://x.example/{i}",
            "content_excerpt": f"evidence {i}", "retrieval_origin": "claim_specific",
            "retrieval_claim_id": "cl_b1"} for i in range(1, 6)]
        + [{"evidence_id": f"ev_noise{i}", "source_uri": f"https://noise.example/{i}",
            "content_excerpt": "rejected as irrelevant", "retrieval_origin": "claim_specific",
            "retrieval_claim_id": "cl_b1"} for i in range(1, 4)]
    )

    trace_b = ot.Trace(trace_id="t_b", timestamp=time.time(), query="test B")
    trace_b.add_claim_raw(claims_b[0])
    added = vm.persist_verification_evidence(trace_b, claims_b, evidence_data_b)

    saved_b = trace_b.to_dict()

    check(
        "B: exactly the 5 evidence items with a real relation are persisted (not 3, not all 9)",
        added == 5 and len(saved_b["evidence"]) == 5,
        f"added={added} persisted={len(saved_b['evidence'])}",
    )
    check(
        "B: stage-6 unused item (no relation to any claim) is NOT persisted",
        all(e["evidence_id"] != "ev_stage6_unused" for e in saved_b["evidence"]),
    )
    check(
        "B: fetched-but-unrelated noise items are NOT persisted",
        all(not e["evidence_id"].startswith("ev_noise") for e in saved_b["evidence"]),
    )

# ============================================================
# C. RESTART: save (process/storage instance 1) -> fresh lookup
#    (simulating a new process) -> content_hash exact match -> found.
# ============================================================

traces_c, index_c = _isolated_paths()
p1, p2, p3 = _patches(traces_c, index_c)

with p1, p2, p3:
    claim_c = {
        "claim_id": "cl_c1",
        "claim_text": "Вода кипит при 100 градусах Цельсия на уровне моря.",
        "content_hash": compute_claim_content_hash("Вода кипит при 100 градусах Цельсия на уровне моря."),
        "evidence_relations": [{"evidence_id": "ev_c1", "relation": "supports", "method": "nli"}],
    }
    evidence_data_c = [{
        "evidence_id": "ev_c1", "source_uri": "https://physics.example/boiling",
        "content_excerpt": "Вода кипит при 100°C при нормальном атмосферном давлении.",
        "source_class": "reference", "evidence_eligible": True, "evidence_role": "direct",
        "quality_score": 0.9,
    }]

    tracer_c = ot.DecisionTracer()
    trace_c = ot.Trace(trace_id="t_c", timestamp=time.time(), query="При какой температуре кипит вода?")
    trace_c.add_claim_raw(claim_c)
    vm.persist_verification_evidence(trace_c, [claim_c], evidence_data_c)
    tracer_c.save_trace(trace_c)  # this is what does the REAL append + index write

    # Simulate a brand new process/request: a NEW claim occurrence,
    # different claim_id, only content_hash matching.
    new_claim_c = {
        "claim_id": "cl_c2_NEW_OCCURRENCE",
        "claim_text": "Вода кипит при 100 градусах Цельсия на уровне моря.",
        "content_hash": compute_claim_content_hash("Вода кипит при 100 градусах Цельсия на уровне моря."),
    }
    hits_c = vm.lookup_historical_evidence(new_claim_c)

    check(
        "C: after save_trace() (JSONL append + index write), a fresh lookup finds the evidence",
        len(hits_c) == 1 and hits_c[0]["source_uri"] == "https://physics.example/boiling",
        f"{hits_c}",
    )
    check(
        "C: the JSONL file physically contains the trace (source of truth, not just the index)",
        (traces_c / f"{time.strftime('%Y%m%d')}.jsonl").exists(),
    )

# ============================================================
# D. EXACT CLAIM LOOKUP: same content_hash -> hit; different claim
#    (different content_hash) -> no exact hit.
# ============================================================

traces_d, index_d = _isolated_paths()
p1, p2, p3 = _patches(traces_d, index_d)

with p1, p2, p3:
    claim_d = {
        "claim_id": "cl_d1",
        "claim_text": "Скорость света в вакууме — 299792458 м/с.",
        "content_hash": compute_claim_content_hash("Скорость света в вакууме — 299792458 м/с."),
        "evidence_relations": [{"evidence_id": "ev_d1", "relation": "supports", "method": "nli"}],
    }
    evidence_data_d = [{"evidence_id": "ev_d1", "source_uri": "https://physics.example/c",
                         "content_excerpt": "c = 299792458 м/с."}]

    trace_d = ot.Trace(trace_id="t_d", timestamp=time.time(), query="test D")
    trace_d.add_claim_raw(claim_d)
    vm.persist_verification_evidence(trace_d, [claim_d], evidence_data_d)
    ot.DecisionTracer().save_trace(trace_d)

    same_hash_claim = {"claim_id": "cl_d_other_occurrence", "claim_text": claim_d["claim_text"],
                        "content_hash": claim_d["content_hash"]}
    different_claim = {"claim_id": "cl_d_unrelated", "claim_text": "Совершенно другое утверждение о биологии.",
                        "content_hash": compute_claim_content_hash("Совершенно другое утверждение о биологии.")}

    check(
        "D: same content_hash (different occurrence) -> memory hit",
        len(vm.lookup_historical_evidence(same_hash_claim)) == 1,
    )
    check(
        "D: different claim text/content_hash -> no exact hit (memory miss, not a fabricated match)",
        len(vm.lookup_historical_evidence(different_claim)) == 0,
    )

# ============================================================
# E. FAMILY FALLBACK (index-level only — no new classifier):
#    the semantic_family_id column is populated at SAVE time and
#    queryable via _query_index() directly, even though the LIVE
#    lookup_historical_evidence() path deliberately does not call it
#    yet (see that function's docstring — avoiding a new embedding
#    lookup for v1). This proves the storage layer is wired correctly
#    for a future stage to flip on, without building a new classifier
#    here.
# ============================================================

traces_e, index_e = _isolated_paths()
p1, p2, p3 = _patches(traces_e, index_e)

with p1, p2, p3:
    claim_e = ClaimRecord(
        claim_id="cl_e1", claim_text="Family-linked claim",
        content_hash="hash_e_does_not_matter_for_this_test",
        semantic_family_id="fam_test123",
    )
    trace_e = ot.Trace(trace_id="t_e", timestamp=time.time(), query="test E")
    trace_e.claims.append(claim_e)

    vm.index_trace(trace_e, "20260101.jsonl", 0)

    rows_by_family = vm._query_index(None, "fam_test123")
    rows_by_wrong_family = vm._query_index(None, "fam_nonexistent")

    check(
        "E: semantic_family_id is persisted to the index at SAVE time",
        len(rows_by_family) == 1 and rows_by_family[0]["claim_id"] == "cl_e1",
        f"{[dict(r) for r in rows_by_family]}",
    )
    check(
        "E: a non-matching family_id yields no rows (no fabricated match)",
        len(rows_by_wrong_family) == 0,
    )

# ============================================================
# F. REASSESSMENT: historical relation was 'supports'; current
#    (mocked) NLI says 'contradicts' -> the CURRENT relation must be
#    'contradicts', never the old 'supports' copied through.
# ============================================================

traces_f, index_f = _isolated_paths()
p1, p2, p3 = _patches(traces_f, index_f)

with p1, p2, p3:
    # Save a historical claim where the evidence SUPPORTS it.
    hist_claim_f = {
        "claim_id": "cl_f_hist",
        "claim_text": "Растение X ядовито для человека.",
        "content_hash": compute_claim_content_hash("Растение X ядовито для человека."),
        "evidence_relations": [{"evidence_id": "ev_f1", "relation": "supports", "method": "nli"}],
    }
    evidence_data_f = [{
        "evidence_id": "ev_f1", "source_uri": "https://old-source.example/plant-x",
        "content_excerpt": "Растение X содержит токсичные алкалоиды.",
        "source_class": "reference", "evidence_eligible": True, "evidence_role": "direct",
        "quality_score": 0.8,
    }]
    trace_f = ot.Trace(trace_id="t_f_hist", timestamp=time.time(), query="test F hist")
    trace_f.add_claim_raw(hist_claim_f)
    vm.persist_verification_evidence(trace_f, [hist_claim_f], evidence_data_f)
    ot.DecisionTracer().save_trace(trace_f)

    # Reconstruction itself must carry NO 'relation' field at all —
    # structurally impossible to copy the old verdict through, not
    # just "we chose not to".
    reconstructed_f = vm.lookup_historical_evidence({
        "claim_id": "cl_f_new", "claim_text": hist_claim_f["claim_text"],
        "content_hash": hist_claim_f["content_hash"],
    })
    check(
        "F: reconstructed memory evidence carries NO 'relation' key (structurally cannot leak the old verdict)",
        len(reconstructed_f) == 1 and "relation" not in reconstructed_f[0],
        f"{reconstructed_f}",
    )

    # Now run it through the REAL async pipeline's MEMORY PASS, with a
    # fake NLI that says the CURRENT claim text is CONTRADICTED by this
    # same evidence excerpt (simulating: the claim's wording changed
    # since 2026-08-20, or the model's re-read of the evidence differs).
    def _fake_map_f(claims, evidence_records, embedding_cache=None):
        out = []
        for c in claims:
            ids = [e["evidence_id"] for e in evidence_records if e.get("evidence_id")]
            out.append(ClaimRecord(claim_id=c["claim_id"], claim_text=c["claim_text"],
                                    derived_from_evidence_ids=ids, verification_status="candidate"))
        return out

    def _fake_nli_contradicts(claims, evidence, batch_label, log, verbose):
        count = 0
        for c in claims:
            relations = []
            ev_by_id = {e["evidence_id"]: e for e in evidence}
            for ev_id in c.get("derived_from_evidence_ids", []) or []:
                ev = ev_by_id.get(ev_id)
                if not ev:
                    continue
                relations.append({
                    "evidence_id": ev_id, "evidence_role": "direct", "evidence_eligible": True,
                    "relation": "contradicts", "method": "fake_nli_reassessment",
                    "from_memory": ev.get("from_memory", False),
                })
                count += 1
            c["evidence_relations"] = relations
        return count

    import asyncio

    async def _run_one_claim_f():
        claim_new = {
            "claim_id": "cl_f_new", "claim_text": hist_claim_f["claim_text"],
            "content_hash": hist_claim_f["content_hash"],
            "verification_status": "candidate", "derived_from_evidence_ids": [],
            "evidence_relations": [],
        }
        evidence_data_live = []
        evidence_lock = asyncio.Lock()
        embedding_cache = object()
        nli_batcher = pipeline_mod._NLIBatcher(evidence_data_live, print, False, coalesce_wait_s=0.0)
        semaphore = asyncio.Semaphore(1)
        stop_event = asyncio.Event()
        active_counter = {"active": 0, "max_active": 0}
        profile = {}

        consumer_task = asyncio.create_task(nli_batcher.run_until(stop_event))
        try:
            await pipeline_mod._process_one_claim(
                claim_new, evidence_data_live, evidence_lock, embedding_cache,
                nli_batcher, None, True, False, False, semaphore, active_counter,
                profile, print, False,
            )
        finally:
            stop_event.set()
            await consumer_task
        return claim_new

    with patch.object(pipeline_mod, "map_claims_to_evidence", _fake_map_f), \
         patch.object(pipeline_mod, "run_claim_evidence_batch", _fake_nli_contradicts), \
         patch.object(pipeline_mod, "retrieve_claim_evidence", lambda *a, **k: []):
        result_claim_f = asyncio.run(_run_one_claim_f())

    check(
        "F: current relation is 'contradicts' (reassessed), NOT the historical 'supports'",
        any(r.get("relation") == "contradicts" for r in result_claim_f.get("evidence_relations", []))
        and not any(r.get("relation") == "supports" for r in result_claim_f.get("evidence_relations", [])),
        f"{result_claim_f.get('evidence_relations')}",
    )
    check(
        "F: the reassessed relation is correctly tagged from_memory=True (it came from a memory-loaded item)",
        any(r.get("from_memory") is True for r in result_claim_f.get("evidence_relations", [])),
        f"{result_claim_f.get('evidence_relations')}",
    )

# ============================================================
# G. MEMORY != TRUTH: a from_memory relation alone must NOT make
#    _claim_has_effective_evidence() report the claim resolved (P4
#    §10) — PASS2 must still be reachable; the memory relation still
#    participates in the final relation set (not deleted, just not a
#    gate-passing shortcut on its own).
# ============================================================

from agent.orchestrator.claims.retrieval import _claim_has_effective_evidence

claim_g_memory_only = {
    "evidence_relations": [
        {"evidence_id": "ev_g1", "evidence_role": "direct", "evidence_eligible": True,
         "relation": "supports", "from_memory": True},
    ],
}
claim_g_fresh = {
    "evidence_relations": [
        {"evidence_id": "ev_g2", "evidence_role": "direct", "evidence_eligible": True,
         "relation": "supports", "from_memory": False},
    ],
}
claim_g_mixed = {
    "evidence_relations": [
        {"evidence_id": "ev_g1", "evidence_role": "direct", "evidence_eligible": True,
         "relation": "supports", "from_memory": True},
        {"evidence_id": "ev_g3", "evidence_role": "direct", "evidence_eligible": True,
         "relation": "contradicts", "from_memory": False},
    ],
}

check(
    "G: a from_memory=True relation ALONE does not resolve the claim (memory is not truth, PASS2 stays reachable)",
    _claim_has_effective_evidence(claim_g_memory_only) is False,
)
check(
    "G: an ordinary fresh (non-memory) direct+eligible relation still resolves the claim as before (unchanged behavior)",
    _claim_has_effective_evidence(claim_g_fresh) is True,
)
check(
    "G: mixed case — a fresh relation present alongside a memory one still resolves normally",
    _claim_has_effective_evidence(claim_g_mixed) is True,
)
check(
    "G: the memory relation is NOT deleted from evidence_relations just because it doesn't gate PASS2 — it still reaches Trust/synthesis",
    any(r.get("from_memory") for r in claim_g_mixed["evidence_relations"]),
)

# ============================================================
# H. CLAIM OWNERSHIP: evidence loaded for claim A is not mixed into
#    claim B's evidence pool (reuses the EXISTING ownership-gate in
#    claim_evidence_mapper.py via retrieval_claim_id/retrieval_origin,
#    not a new mechanism).
# ============================================================

from agent.claim_evidence_mapper import map_claims_to_evidence

evidence_owned_by_a = {
    "evidence_id": "ev_h_a", "content_excerpt": "Evidence specifically about claim A's subject matter here.",
    "retrieval_origin": "claim_specific", "retrieval_claim_id": "cl_h_a",
    "source_uri": "https://a.example/x",
}
claims_h = [
    {"claim_id": "cl_h_a", "claim_text": "Claim A's subject matter here.", "derived_from_evidence_ids": []},
    {"claim_id": "cl_h_b", "claim_text": "Completely unrelated claim B about something else entirely.",
     "derived_from_evidence_ids": []},
]

mapped_h = map_claims_to_evidence(claims_h, [evidence_owned_by_a], None)
mapped_by_id_h = {m.claim_id: m for m in mapped_h}

check(
    "H: claim A gets its own owned evidence linked",
    "ev_h_a" in mapped_by_id_h["cl_h_a"].derived_from_evidence_ids,
    f"{mapped_by_id_h['cl_h_a'].derived_from_evidence_ids}",
)
check(
    "H: claim B (different owner) does NOT get claim A's claim-owned evidence, "
    "regardless of any textual similarity",
    "ev_h_a" not in mapped_by_id_h["cl_h_b"].derived_from_evidence_ids,
    f"{mapped_by_id_h['cl_h_b'].derived_from_evidence_ids}",
)

# ============================================================
# I. SOURCE CLUSTER: persist/reload preserves the SAME source_cluster_id
#    (tracer never recomputes it, only propagates what was already
#    computed — P4 §4).
# ============================================================

traces_i, index_i = _isolated_paths()
p1, p2, p3 = _patches(traces_i, index_i)

with p1, p2, p3:
    claim_i = {
        "claim_id": "cl_i1", "claim_text": "Test claim I",
        "content_hash": compute_claim_content_hash("Test claim I"),
        "evidence_relations": [{"evidence_id": "ev_i1", "relation": "supports", "method": "nli"}],
    }
    evidence_data_i = [{
        "evidence_id": "ev_i1", "source_uri": "https://cluster.example/x",
        "content_excerpt": "clustered content", "source_cluster_id": "sc_ev_i1_ROOT",
    }]

    trace_i = ot.Trace(trace_id="t_i", timestamp=time.time(), query="test I")
    trace_i.add_claim_raw(claim_i)
    vm.persist_verification_evidence(trace_i, [claim_i], evidence_data_i)
    saved_i = trace_i.to_dict()

    check(
        "I: source_cluster_id survives the round trip unchanged (never recomputed by the tracer)",
        saved_i["evidence"][0]["source_cluster_id"] == "sc_ev_i1_ROOT",
        f"{saved_i['evidence'][0]}",
    )

# ============================================================
# J. MEMORY ROUTE: a loaded record has route="local_memory" but
#    preserves the ORIGINAL provenance chain (origin_route/
#    origin_trace_id/origin_observed_at) — reuse is a new ROUTE, not a
#    new SOURCE (P4 §12).
# ============================================================

traces_j, index_j = _isolated_paths()
p1, p2, p3 = _patches(traces_j, index_j)

with p1, p2, p3:
    claim_j = {
        "claim_id": "cl_j1", "claim_text": "Тест J маршрута памяти.",
        "content_hash": compute_claim_content_hash("Тест J маршрута памяти."),
        "evidence_relations": [{"evidence_id": "ev_j1", "relation": "supports", "method": "nli"}],
    }
    evidence_data_j = [{"evidence_id": "ev_j1", "source_uri": "https://original.example/j",
                         "content_excerpt": "original internet content", "route": "internet"}]

    trace_j = ot.Trace(trace_id="t_j_ORIGINAL", timestamp=12345.0, query="test J")
    trace_j.add_claim_raw(claim_j)
    vm.persist_verification_evidence(trace_j, [claim_j], evidence_data_j)
    ot.DecisionTracer().save_trace(trace_j)

    loaded_j = vm.lookup_historical_evidence({
        "claim_id": "cl_j2", "claim_text": claim_j["claim_text"], "content_hash": claim_j["content_hash"],
    })

    check(
        "J: loaded evidence has CURRENT route=local_memory",
        len(loaded_j) == 1 and loaded_j[0]["route"] == "local_memory",
        f"{loaded_j}",
    )
    check(
        "J: source_uri is UNCHANGED (same original URL, not a new source)",
        loaded_j[0]["source_uri"] == "https://original.example/j",
    )
    check(
        "J: origin_route preserves what the channel ORIGINALLY was (internet)",
        loaded_j[0]["origin_route"] == "internet",
    )
    check(
        "J: origin_trace_id points back to the ORIGINAL trace",
        loaded_j[0]["origin_trace_id"] == "t_j_ORIGINAL",
    )
    check(
        "J: origin_observed_at preserves the ORIGINAL observation time (12345.0), not now",
        loaded_j[0]["origin_observed_at"] == 12345.0,
    )

# ============================================================
# K. NO DOUBLE INDEPENDENCE: evidence A saved yesterday, loaded today
#    from memory, does NOT become a second independent source root —
#    it clusters with a fresh copy of the SAME content, exactly like
#    two ordinary independent-vs-syndicated fetches would.
# ============================================================

evidence_fresh_k = {
    "evidence_id": "ev_k_fresh", "source_uri": "https://k.example/article",
    "source_title": "K Article Title", "content_excerpt": "This is the K article content, fetched fresh today. " * 3,
    "route": "internet", "from_memory": False,
}
evidence_memory_k = {
    "evidence_id": "ev_k_memory", "source_uri": "https://k.example/article",
    "source_title": "K Article Title", "content_excerpt": "This is the K article content, fetched fresh today. " * 3,
    "route": "local_memory", "from_memory": True, "origin_route": "internet",
    "origin_trace_id": "t_yesterday",
}

pool_k = [evidence_fresh_k, evidence_memory_k]
assign_source_clusters(pool_k, log=print, verbose=False)

check(
    "K: memory-reused evidence and its fresh-fetched twin share the SAME source_cluster_id "
    "(memory reuse does not fabricate a second independent root)",
    evidence_fresh_k.get("source_cluster_id") is not None
    and evidence_fresh_k.get("source_cluster_id") == evidence_memory_k.get("source_cluster_id"),
    f"fresh={evidence_fresh_k.get('source_cluster_id')} memory={evidence_memory_k.get('source_cluster_id')}",
)

# ============================================================
# L. CURRENT REGRESSIONS: constants/invariants this patch must not
#    have touched (full 44/45-suite enforcement happens outside this
#    file; these are the same load-bearing constants re-checked here
#    for a single, self-contained confirmation).
# ============================================================

from agent.orchestrator.claims.async_pipeline import MAX_CLAIM_WORKERS

check("L: MAX_CLAIM_WORKERS unchanged (<=3)", MAX_CLAIM_WORKERS <= 3, f"{MAX_CLAIM_WORKERS}")

import inspect
_src_l = inspect.getsource(pipeline_mod)
check(
    "L: NLI concurrency still == 1 (single consumer task, unchanged by this patch)",
    _src_l.count("asyncio.create_task(nli_batcher.run_until(") == 1,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
