"""
agent/epistemic_relation_persistence_regression_test.py — Epistemic Core v1
Phase 1 regression: claim<->evidence relation verdicts must survive the
runtime -> Trace.add_claim_raw() -> to_dict() -> json.dumps -> json.loads
round trip.

Before this phase, the persisted trace only carried a bare evidence_id list
(derived_from_evidence_ids) per claim — the actual NLI verdict
(supports/contradicts/unrelated/uncertain) computed by
claim_relation.py::classify_claim_evidence_batch() and attached to
claim["evidence_relations"] by claims/mapping.py::run_claim_evidence_batch()
was silently dropped in Trace.add_claim_raw() (see
YANDI_EPISTEMIC_ARCHITECTURE_AUDIT.md §3.1). This suite proves the fix:
the round trip now reconstructs relation, relation_method and source_claim
for all four relation types, respects the existing [:3] truncation cap
(same reasoning as derived_from_evidence_ids), and stays backward
compatible with claim dicts that have no evidence_relations key at all.

Run: /home/iam/venv/bin/python3 -m agent.epistemic_relation_persistence_regression_test
"""

import json
import time

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


def _fresh_trace():
    return Trace(trace_id="t_test", timestamp=time.time(), query="test query")


# ── 1. All four relation types survive a full round trip ──

claim_all_types = {
    "claim_id": "cl_aaa11111",
    "claim_text": "Юпитер — крупнейшая планета Солнечной системы по объёму и массе.",
    "derived_from_evidence_ids": ["ev_s1", "ev_c1", "ev_u1"],
    "claim_type": "factual",
    "claim_confidence": 0.7,
    "verification_status": "disputed",
    "evidence_relations": [
        {"evidence_id": "ev_s1", "relation": "supports", "method": "nli_llm",
         "source_claim": "Юпитер имеет наибольший объём среди планет Солнечной системы."},
        {"evidence_id": "ev_c1", "relation": "contradicts", "method": "nli_llm",
         "source_claim": "Сатурн является крупнейшей планетой по некоторым устаревшим данным."},
        {"evidence_id": "ev_u1", "relation": "uncertain", "method": "embedding_fallback",
         "source_claim": "Газовые гиганты Солнечной системы значительно крупнее землеподобных планет."},
    ],
}

trace = _fresh_trace()
trace.add_claim_raw(claim_all_types)
round_tripped = json.loads(json.dumps(trace.to_dict(), ensure_ascii=False))
rec = round_tripped["claims"][0]
rels_by_ev = {r["evidence_id"]: r for r in rec["evidence_relations"]}

check(
    "round trip: exactly 3 relations survive (all within the [:3] cap)",
    len(rec["evidence_relations"]) == 3,
    f"{rec['evidence_relations']}",
)
check(
    "round trip: SUPPORTS relation + method + source_claim reconstructed",
    rels_by_ev.get("ev_s1", {}).get("relation") == "supports"
    and rels_by_ev.get("ev_s1", {}).get("relation_method") == "nli_llm"
    and "наибольший объём" in rels_by_ev.get("ev_s1", {}).get("source_claim", ""),
    f"{rels_by_ev.get('ev_s1')}",
)
check(
    "round trip: CONTRADICTS relation reconstructed distinctly from supports",
    rels_by_ev.get("ev_c1", {}).get("relation") == "contradicts",
    f"{rels_by_ev.get('ev_c1')}",
)
check(
    "round trip: UNCERTAIN relation reconstructed with its own method",
    rels_by_ev.get("ev_u1", {}).get("relation") == "uncertain"
    and rels_by_ev.get("ev_u1", {}).get("relation_method") == "embedding_fallback",
    f"{rels_by_ev.get('ev_u1')}",
)

# ── 2. UNRELATED relation type also survives (separate claim, keeps test 1 clean) ──

claim_unrelated = {
    "claim_id": "cl_bbb22222",
    "claim_text": "Аспартам метаболизируется в организме на фенилаланин и метанол.",
    "derived_from_evidence_ids": ["ev_x1"],
    "claim_type": "factual",
    "claim_confidence": 0.6,
    "verification_status": "unverified",
    "evidence_relations": [
        {"evidence_id": "ev_x1", "relation": "unrelated", "method": "nli_llm",
         "source_claim": "Погода в регионе была солнечной в день публикации статьи."},
    ],
}

trace2 = _fresh_trace()
trace2.add_claim_raw(claim_unrelated)
rt2 = json.loads(json.dumps(trace2.to_dict(), ensure_ascii=False))
rec2 = rt2["claims"][0]
check(
    "round trip: UNRELATED relation reconstructed",
    len(rec2["evidence_relations"]) == 1
    and rec2["evidence_relations"][0]["relation"] == "unrelated",
    f"{rec2['evidence_relations']}",
)

# ── 3. Cap: more than 3 relations -> only first 3 persisted (same as derived_from_evidence_ids) ──

claim_many = {
    "claim_id": "cl_ccc33333",
    "claim_text": "Пять источников независимо друг от друга подтверждают этот факт о планетах.",
    "derived_from_evidence_ids": ["ev1", "ev2", "ev3", "ev4", "ev5"],
    "claim_type": "factual",
    "claim_confidence": 0.5,
    "verification_status": "supported",
    "evidence_relations": [
        {"evidence_id": f"ev{i}", "relation": "supports", "method": "nli_llm", "source_claim": f"source {i}"}
        for i in range(1, 6)
    ],
}

trace3 = _fresh_trace()
trace3.add_claim_raw(claim_many)
rt3 = json.loads(json.dumps(trace3.to_dict(), ensure_ascii=False))
rec3 = rt3["claims"][0]
check(
    "cap: 5 relations in runtime -> exactly 3 survive to_dict() (matches derived_from_evidence_ids[:3])",
    len(rec3["evidence_relations"]) == 3 and len(rec3["derived_from_evidence_ids"]) == 3,
    f"relations={len(rec3['evidence_relations'])} ids={len(rec3['derived_from_evidence_ids'])}",
)

# ── 4. Backward compatibility: claim dict with NO evidence_relations key at all ──

claim_no_relations_key = {
    "claim_id": "cl_ddd44444",
    "claim_text": "Это утверждение пришло из кода, который ещё не знает о evidence_relations.",
    "derived_from_evidence_ids": [],
    "claim_type": "factual",
    "claim_confidence": 0.4,
    "verification_status": "unverified",
    # deliberately no "evidence_relations" key — simulates old caller / old data
}

trace4 = _fresh_trace()
try:
    trace4.add_claim_raw(claim_no_relations_key)
    rt4 = json.loads(json.dumps(trace4.to_dict(), ensure_ascii=False))
    rec4 = rt4["claims"][0]
    check(
        "backward compat: missing evidence_relations key -> empty list, no crash",
        rec4["evidence_relations"] == [],
        f"{rec4}",
    )
except Exception as e:
    check("backward compat: missing evidence_relations key -> empty list, no crash", False, repr(e))

# ── 5. Malformed relation entries (no evidence_id) are dropped, not persisted as garbage ──

claim_malformed = {
    "claim_id": "cl_eee55555",
    "claim_text": "Claim с одной валидной и одной битой relation-записью для проверки фильтрации.",
    "derived_from_evidence_ids": ["ev_ok"],
    "claim_type": "factual",
    "claim_confidence": 0.5,
    "verification_status": "supported",
    "evidence_relations": [
        {"evidence_id": "ev_ok", "relation": "supports", "method": "nli_llm", "source_claim": "valid"},
        {"relation": "supports", "method": "nli_llm", "source_claim": "no evidence_id here"},
    ],
}

trace5 = _fresh_trace()
trace5.add_claim_raw(claim_malformed)
rt5 = json.loads(json.dumps(trace5.to_dict(), ensure_ascii=False))
rec5 = rt5["claims"][0]
check(
    "malformed entry without evidence_id is dropped, valid one survives",
    len(rec5["evidence_relations"]) == 1 and rec5["evidence_relations"][0]["evidence_id"] == "ev_ok",
    f"{rec5['evidence_relations']}",
)

# ── 6. Old-style ClaimRecord construction (no evidence_relations kwarg) still works ──

from agent.orch_schemas import ClaimRecord

try:
    old_style = ClaimRecord(
        claim_id="cl_old",
        claim_text="constructed the old way, without evidence_relations kwarg",
        verification_status="unverified",
    )
    check(
        "ClaimRecord constructed without evidence_relations kwarg defaults to []",
        old_style.evidence_relations == [],
        f"{old_style.evidence_relations}",
    )
except Exception as e:
    check("ClaimRecord constructed without evidence_relations kwarg defaults to []", False, repr(e))

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
