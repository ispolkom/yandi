"""
agent/verification_memory_regression_test.py — Этап 3 (P5) regression:
verification memory SAVE (agent/verification_memory.py::
persist_verification_evidence, agent/orch_tracer.py::save_trace/
add_claim_raw/to_dict), LOAD (lookup_historical_evidence,
_process_one_claim's MEMORY PASS in agent/orchestrator/claims/
async_pipeline.py), and the "MEMORY != TRUTH" invariants (P4 §9/§10:
historical relations are never copied as truth, and alone can never
skip PASS2).

"ТОЧКА НОЛЬ" v13 (owner mandate, 2026-09): registry/index.db (sqlite
locator) and registry/dataset/orch_traces/*.jsonl are BOTH retired, not
migrated. claim_occurrence/source_observation/evidence_relation
(agent.db.sql.shadow_write.record_claims_and_evidence(), now PRIMARY)
are the real source of truth agent.verification_memory.py's LOAD path
queries. A small fake connection stands in for the real bastion-
protected tables. persist_verification_evidence() itself is UNCHANGED
(still a pure in-memory trace.evidence build, no SQL/file I/O) — tests
that only call it (A, B, I) need no SQL fixture at all; tests that also
call tracer.save_trace() and/or lookup_historical_evidence() (C, D, F,
J) now ALSO call record_claims_and_evidence() explicitly, matching the
real production call sequence (agent/orchestrator/claims/status.py
calls it separately from agent.orch_tracer.DecisionTracer.save_trace()).

Run: /home/iam/venv/bin/python3 -m agent.verification_memory_regression_test
"""
from __future__ import annotations

import time
from unittest.mock import patch

import agent.orch_tracer as ot
import agent.verification_memory as vm
import agent.db.sql.repositories as repo
import agent.orchestrator.claims.async_pipeline as pipeline_mod
from agent.orch_schemas import EvidenceRecord, ClaimRecord
from agent.claim_identity import compute_claim_content_hash
from agent.source_clustering import assign_source_clusters
from agent.db_sql_fake_fixtures import fresh_fake as _fresh_fake, record as _record

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


# ============================================================
# A. FULL SAVE: retrieval_claim_id / source_cluster_id / evidence /
#    relation all present in the persisted Trace JSON.
#    persist_verification_evidence() is a pure in-memory trace.evidence
#    build (no SQL/file I/O) — no fake connection needed here.
# ============================================================

if True:
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
#    Also pure in-memory — no fake connection needed.
# ============================================================

if True:
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
# C. RESTART: record_claims_and_evidence (real production sequence) ->
#    fresh lookup (simulating a new process) -> content_hash exact
#    match -> found.
# ============================================================

conn_c = _fresh_fake()

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
_record(conn_c, "t_c", [claim_c], evidence_data_c)  # real prod: status.py's record_claims_and_evidence()
tracer_c.save_trace(trace_c)  # real prod: writeback.py's tracer.save_trace() (trace_record envelope only)

# Simulate a brand new process/request: a NEW claim occurrence,
# different claim_id, only content_hash matching.
new_claim_c = {
    "claim_id": "cl_c2_NEW_OCCURRENCE",
    "claim_text": "Вода кипит при 100 градусах Цельсия на уровне моря.",
    "content_hash": compute_claim_content_hash("Вода кипит при 100 градусах Цельсия на уровне моря."),
}
hits_c = vm.lookup_historical_evidence(new_claim_c)

check(
    "C: after record_claims_and_evidence(), a fresh lookup finds the evidence",
    len(hits_c) == 1 and hits_c[0]["source_uri"] == "https://physics.example/boiling",
    f"{hits_c}",
)
check(
    "C: the claim_occurrence row physically exists in SQL (source of truth, not just returned)",
    conn_c.claims.get("cl_c1", {}).get("content_hash") == claim_c["content_hash"],
    f"{conn_c.claims}",
)
check(
    "C: save_trace() persisted the trace_record envelope row too",
    "t_c" in conn_c.trace_records,
)

# ============================================================
# D. EXACT CLAIM LOOKUP: same content_hash -> hit; different claim
#    (different content_hash) -> no exact hit.
# ============================================================

conn_d = _fresh_fake()

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
_record(conn_d, "t_d", [claim_d], evidence_data_d)
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
# E. FAMILY FALLBACK, storage layer: family_id is a real column on
#    claim_occurrence (agent.db.sql.schema.py), populated by
#    record_claims_and_evidence() and queryable via
#    repo.find_claim_occurrences_by_family(). Section E2 below proves
#    lookup_historical_evidence() itself now uses this (2026-09, owner
#    mandate: reuse by meaning, not only near-identical wording).
# ============================================================

conn_e = _fresh_fake()

claim_e = {
    "claim_id": "cl_e1", "claim_text": "Family-linked claim",
    "content_hash": "hash_e_does_not_matter_for_this_test",
    "semantic_family_id": "fam_test123",
}
_record(conn_e, "t_e", [claim_e], [])

rows_by_family = repo.find_claim_occurrences_by_family(conn_e, "fam_test123")
rows_by_wrong_family = repo.find_claim_occurrences_by_family(conn_e, "fam_nonexistent")

check(
    "E: family_id is persisted to claim_occurrence at SAVE time",
    len(rows_by_family) == 1 and rows_by_family[0]["claim_id"] == "cl_e1",
    f"{rows_by_family}",
)
check(
    "E: a non-matching family_id yields no rows (no fabricated match)",
    len(rows_by_wrong_family) == 0,
)

# ============================================================
# E2. FAMILY FALLBACK, live in lookup_historical_evidence(): reuse by
#     MEANING, not only near-identical wording (2026-09 owner mandate).
#     find_or_link_claim() itself is scripted (embedding/LLM cost is
#     not this test's concern) — everything downstream of the family_id
#     it returns is the REAL storage-layer code.
# ============================================================

import agent.claim_family_registry as cfr_mod


class _ScriptedRegistry:
    """find_or_link_claim() returns whatever this test queued for the given claim_text, and records every call
    it received (so a test can assert domain/claim_text/claim_id were passed through correctly)."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls = []

    def find_or_link_claim(self, claim_text, claim_id, domain, log=None, verbose=False, stats=None):
        self.calls.append({"claim_text": claim_text, "claim_id": claim_id, "domain": domain})
        return self.answers.get(claim_text)


def _with_registry(answers: dict):
    return _ScriptedRegistry(answers)


conn_e2 = _fresh_fake()

# a PRIOR occurrence, worded differently from what will be asked next, already linked into fam_e2
prior_e2 = {"claim_id": "cl_e2_prior", "claim_text": "Сруб из бревна ручной рубки стоит от 470000 рублей.",
            "content_hash": compute_claim_content_hash("Сруб из бревна ручной рубки стоит от 470000 рублей."),
            "evidence_relations": [{"evidence_id": "ev_e2", "relation": "supports", "method": "nli"}],
            "semantic_family_id": "fam_e2"}
evidence_e2 = [{"evidence_id": "ev_e2", "source_uri": "https://logcabin.example/price",
               "content_excerpt": "Цена сруба ручной рубки — от 470 000 руб."}]
_record(conn_e2, "t_e2_prior", [prior_e2], evidence_e2)

# a DIFFERENTLY-WORDED new claim, same meaning: content_hash misses, family_id (scripted) hits.
new_claim_e2 = {"claim_id": "cl_e2_new", "claim_text": "Стоимость ручной рубки сруба начинается от 470 тысяч.",
                "content_hash": compute_claim_content_hash("Стоимость ручной рубки сруба начинается от 470 тысяч.")}

registry_e2 = _with_registry({new_claim_e2["claim_text"]: "fam_e2"})
with patch.object(cfr_mod, "get_claim_family_registry", lambda: registry_e2):
    check("E2: content_hash alone finds nothing for a rephrased claim (proves the fallback is doing the work below)",
          vm.lookup_historical_evidence(dict(new_claim_e2)) == [])
    hits_no_domain = vm.lookup_historical_evidence(dict(new_claim_e2))
    check("E2: with NO domain given, the family fallback does not fire (explicit opt-in, unchanged old behaviour)",
          hits_no_domain == [] and registry_e2.calls == [])
    hits = vm.lookup_historical_evidence(dict(new_claim_e2), domain="строительство")
    check("E2: WITH a domain, a rephrased claim now finds the prior, differently-worded occurrence by meaning",
          len(hits) == 1 and hits[0]["content_excerpt"] == "Цена сруба ручной рубки — от 470 000 руб.")
    check("E2: the reused evidence is tagged local_memory/from_memory, owned by the NEW claim, same as exact-match reuse",
          hits[0]["route"] == "local_memory" and hits[0]["from_memory"] is True and hits[0]["retrieval_claim_id"] == "cl_e2_new")
    check("E2: no `relation` field — the historical verdict is never copied in as a ready-made answer",
          "relation" not in hits[0])

    # the registry was asked with the CURRENT claim's own text/id/domain, not the prior occurrence's
    check("E2: find_or_link_claim was called with the new claim's own text, id and the given domain",
          registry_e2.calls[-1] == {"claim_text": new_claim_e2["claim_text"], "claim_id": "cl_e2_new", "domain": "строительство"})

# no family match at all (scripted None) -> still a clean miss, never an error
with patch.object(cfr_mod, "get_claim_family_registry", lambda: _with_registry({})):
    check("E2: no family match either -> [] (a genuine memory miss, not a fabricated one)",
          vm.lookup_historical_evidence(dict(new_claim_e2), domain="строительство") == [])

# the CURRENT claim's own occurrence (if it happens to already be in the family) is never "reused into itself" —
# seeded WITH real evidence, so an empty result really does mean "excluded", not just "nothing was ever attached"
conn_e2b = _fresh_fake()
self_claim = {"claim_id": "cl_e2_self", "claim_text": "Само себя.", "content_hash": "h_self", "semantic_family_id": "fam_e2_self",
              "evidence_relations": [{"evidence_id": "ev_self", "relation": "supports", "method": "nli"}]}
_record(conn_e2b, "t_e2_self", [self_claim], [{"evidence_id": "ev_self", "source_uri": "https://x.example/self", "content_excerpt": "Self excerpt"}])
with patch.object(cfr_mod, "get_claim_family_registry", lambda: _with_registry({"Само себя.": "fam_e2_self"})):
    check("E2: a claim never reuses its OWN occurrence as if it were history",
          vm.lookup_historical_evidence({"claim_id": "cl_e2_self", "claim_text": "Само себя.",
                                          "content_hash": "h_self_different"}, domain="d") == [])

# exclude_trace_id is respected on the family path too (same THIS-run exclusion as the exact-hash path); again
# seeded WITH real evidence so the check is meaningful.
conn_e2c = _fresh_fake()
same_run = {"claim_id": "cl_e2_samerun", "claim_text": "В этом же запуске.", "content_hash": "h_samerun",
            "semantic_family_id": "fam_e2_run", "evidence_relations": [{"evidence_id": "ev_samerun", "relation": "supports", "method": "nli"}]}
_record(conn_e2c, "t_e2_thisrun", [same_run], [{"evidence_id": "ev_samerun", "source_uri": "https://x.example/samerun", "content_excerpt": "Same-run excerpt"}])
with patch.object(cfr_mod, "get_claim_family_registry", lambda: _with_registry({"Другой текст, тот же запуск.": "fam_e2_run"})):
    check("E2: an occurrence from the run being excluded (exclude_trace_id) is not reused even via the family path",
          vm.lookup_historical_evidence({"claim_id": "cl_e2_x", "claim_text": "Другой текст, тот же запуск.",
                                          "content_hash": "h_x"}, exclude_trace_id="t_e2_thisrun", domain="d") == [])

# a failure inside the family lookup (e.g. registry itself raises) is caught, never breaks the memory pass — and,
# whatever family_id the except branch ends up with, find_claim_occurrences_by_family must never even be CALLED.
conn_e2d = _fresh_fake()


class _RaisingRegistry:
    def find_or_link_claim(self, *a, **k):
        raise RuntimeError("embedding service down")


with patch.object(cfr_mod, "get_claim_family_registry", lambda: _RaisingRegistry()), \
     patch.object(repo, "find_claim_occurrences_by_family") as _spy_family_query:
    check("E2: a family-lookup failure fails OPEN — [] instead of raising, verification is not broken by memory",
          vm.lookup_historical_evidence({"claim_id": "cl_e2_fail", "claim_text": "Что угодно.",
                                          "content_hash": "h_fail"}, domain="d") == [])
    check("E2: …and the caught exception really did stop it — the family table is never even queried afterwards",
          _spy_family_query.call_count == 0)

# MAX_HISTORICAL_OCCURRENCES cap applies to the family path too
conn_e2e = _fresh_fake()
many = [{"claim_id": f"cl_e2_many{i}", "claim_text": f"Вариант формулировки {i}.", "content_hash": f"h_many{i}",
         "semantic_family_id": "fam_e2_many", "evidence_relations": [{"evidence_id": f"ev_many{i}", "relation": "supports", "method": "nli"}]}
        for i in range(3)]
for i, c in enumerate(many):
    _record(conn_e2e, f"t_e2_many{i}", [c], [{"evidence_id": f"ev_many{i}", "source_uri": f"https://x.example/{i}",
                                              "content_excerpt": f"Excerpt {i}"}])
with patch.object(cfr_mod, "get_claim_family_registry", lambda: _with_registry({"Новая формулировка того же самого.": "fam_e2_many"})):
    hits_many = vm.lookup_historical_evidence({"claim_id": "cl_e2_newmany", "claim_text": "Новая формулировка того же самого.",
                                                "content_hash": "h_newmany"}, domain="d")
    check(f"E2: the family path is bounded by MAX_HISTORICAL_OCCURRENCES ({vm.MAX_HISTORICAL_OCCURRENCES}) out of 3 candidates, not every past occurrence",
          0 < len(hits_many) <= vm.MAX_HISTORICAL_OCCURRENCES < 3)

# ============================================================
# F. REASSESSMENT: historical relation was 'supports'; current
#    (mocked) NLI says 'contradicts' -> the CURRENT relation must be
#    'contradicts', never the old 'supports' copied through.
# ============================================================

conn_f = _fresh_fake()

if True:
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
    _record(conn_f, "t_f_hist", [hist_claim_f], evidence_data_f)
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
#    computed — P4 §4). Pure in-memory — no fake connection needed.
# ============================================================

if True:
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

conn_j = _fresh_fake()

claim_j = {
    "claim_id": "cl_j1", "claim_text": "Тест J маршрута памяти.",
    "content_hash": compute_claim_content_hash("Тест J маршрута памяти."),
    "evidence_relations": [{"evidence_id": "ev_j1", "relation": "supports", "method": "nli"}],
}
evidence_data_j = [{"evidence_id": "ev_j1", "source_uri": "https://original.example/j",
                     "content_excerpt": "original internet content", "route": "internet",
                     "observed_at": 12345.0}]

trace_j = ot.Trace(trace_id="t_j_ORIGINAL", timestamp=12345.0, query="test J")
trace_j.add_claim_raw(claim_j)
vm.persist_verification_evidence(trace_j, [claim_j], evidence_data_j)
_record(conn_j, "t_j_ORIGINAL", [claim_j], evidence_data_j, started_at=12345.0)
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
    f"{loaded_j[0]['origin_observed_at']}",
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
