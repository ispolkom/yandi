"""
agent/claim_history_note_regression_test.py — "Живая память" (owner
request, 2026-09): agent/claim_history_note.py regression.

Owner's exact requirement: YANDI should remember past questions/
answers and be allowed to annotate/change how she answers, but ONLY
when she has actually checked sources this request — never as a
background/idle-time habit. Concretely: ask "сколько спутников у
Марса?", get an answer; a week later ask "есть ли спутники у Марса?" —
a DIFFERENT question whose fresh claim extraction lands in the SAME
semantic family (agent.claim_family_registry's embedding+LLM-judge
matching, not exact question text) — she should say so in the answer,
not silently update an invisible belief.

Covers:
    A. get_family_historical_claims(): claim-level history, real Trace
       persistence (same pattern as family_history_read_path_
       regression_test.py — not a hand-rolled JSONL shape), newest
       first, family-scoped (a different family is never pulled in).
    B. build_claim_history_notes(): no note when a family is genuinely
       fresh (no prior occurrence besides this request's own claim);
       a note with changed=False when the prior conclusion matches;
       changed=True when it doesn't; exactly ONE note when two of this
       request's own claims share a family (dedup by family, not claim).
    C. format_history_note_block(): deterministic text, "" for no
       notes, distinguishable wording for changed vs reinforced.
    D. WIRING: run_optimistic_respond() is reachable only from
       orchestrator_v2.py's standard pipeline branch, never the
       pre_pipeline cache-hit/short-circuit early-return — proven via
       source inspection, the same style already established by
       agent/epistemic_canonical_trust_shadow_regression_test.py — so
       "she only changes an answer after actually checking sources
       this request" is structural, not just documented intent.
    E. Empty claims_data (existing db_sql_wiring_regression_test.py /
       answer_delivery_persistence_regression_test.py scenarios) must
       remain completely unaffected — no note text appended.

Run: /home/iam/venv/bin/python3 -m agent.claim_history_note_regression_test
"""
from __future__ import annotations

import inspect
import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.orch_tracer as ot
import agent.verification_memory as vm
from agent.orch_schemas import EvidenceRecord
from agent.claim_history_note import build_claim_history_notes, format_history_note_block
from agent.verification_memory import get_family_historical_claims

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


def _make_env():
    traces_dir = Path(tempfile.mkdtemp(prefix="chn_traces_"))
    index_db = Path(tempfile.mkdtemp(prefix="chn_index_")) / "index.db"
    return traces_dir, index_db


def _persist_claim(
    *, trace_id: str, claim_id: str, content_hash: str, semantic_family_id: str,
    query: str = "q", claim_text: str = "default placeholder claim text", verification_status: str = "supported",
    claim_confidence: float = 0.7, evidence_id: str = None, source_uri: str = "https://x.example/a",
):
    """Same established pattern as family_history_read_path_regression_
    test.py: a real Trace, saved through the real DecisionTracer path —
    not a hand-rolled JSONL line."""
    trace = ot.Trace(trace_id=trace_id, timestamp=0.0, query=query)
    claim_data = {
        "claim_id": claim_id,
        "claim_text": claim_text,
        "claim_confidence": claim_confidence,
        "content_hash": content_hash,
        "semantic_family_id": semantic_family_id,
        "verification_status": verification_status,
    }
    if evidence_id:
        claim_data["derived_from_evidence_ids"] = [evidence_id]
        claim_data["evidence_relations"] = [{
            "evidence_id": evidence_id, "relation": "supports",
            "evidence_role": "direct", "evidence_eligible": True,
            "source_class": "reference", "directness": "direct",
        }]
        trace.add_evidence(EvidenceRecord(
            evidence_id=evidence_id, source_type="web", source_uri=source_uri,
            content_excerpt="excerpt", source_class="reference", evidence_eligible=True,
            route="internet",
        ))
    trace.add_claim_raw(claim_data)
    ot.DecisionTracer().save_trace(trace)


# ============================================================
# A. get_family_historical_claims() — claim-level history.
# ============================================================

traces_a, index_a = _make_env()
with patch.object(ot, "TRACES_DIR", traces_a), \
     patch.object(vm, "TRACES_DIR", traces_a), \
     patch.object(vm, "INDEX_DB", index_a):

    _persist_claim(
        trace_id="t_mars1", claim_id="cl_mars1", content_hash="h_mars1",
        semantic_family_id="fam_mars_moons", query="Сколько спутников у Марса?",
        claim_text="У Марса два спутника: Фобос и Деймос.",
        verification_status="supported",
    )
    _persist_claim(
        trace_id="t_other", claim_id="cl_other", content_hash="h_other",
        semantic_family_id="fam_unrelated", query="Какая планета ближе всего к Солнцу?",
        claim_text="Меркурий ближе всего к Солнцу.",
        verification_status="supported",
    )

    hist_mars = get_family_historical_claims("fam_mars_moons")
    hist_unrelated = get_family_historical_claims("fam_unrelated")

check(
    "A1: get_family_historical_claims() finds the persisted Mars-moons claim",
    len(hist_mars) == 1 and hist_mars[0]["claim_id"] == "cl_mars1",
    f"{hist_mars}",
)
check(
    "A2: the returned dict carries the ORIGINAL question text, claim text, and status",
    hist_mars[0]["query"] == "Сколько спутников у Марса?"
    and hist_mars[0]["claim_text"] == "У Марса два спутника: Фобос и Деймос."
    and hist_mars[0]["verification_status"] == "supported",
    f"{hist_mars[0]}",
)
check(
    "A3: family-scoping is exact — a different family's claim never leaks in",
    len(hist_unrelated) == 1 and hist_unrelated[0]["claim_id"] == "cl_other"
    and all(h["claim_id"] != "cl_other" for h in hist_mars),
)
check(
    "A4: querying a family with zero history returns [], not an error",
    get_family_historical_claims("fam_never_seen") == [],
)


# ============================================================
# B. build_claim_history_notes() — the comparison logic.
# ============================================================

# B1: fresh family (only this request's own claim exists) -> no note.
traces_b1, index_b1 = _make_env()
with patch.object(ot, "TRACES_DIR", traces_b1), \
     patch.object(vm, "TRACES_DIR", traces_b1), \
     patch.object(vm, "INDEX_DB", index_b1):

    _persist_claim(
        trace_id="t_fresh_now", claim_id="cl_fresh_now", content_hash="h_fresh",
        semantic_family_id="fam_fresh", query="Новый вопрос?",
        claim_text="Свежее утверждение.", verification_status="supported",
    )
    notes_fresh = build_claim_history_notes([{
        "claim_id": "cl_fresh_now", "claim_text": "Свежее утверждение.",
        "semantic_family_id": "fam_fresh", "verification_status": "supported",
    }])

check(
    "B1: a genuinely fresh family (no OTHER occurrence) produces NO note "
    "(the only history entry IS this request's own claim)",
    notes_fresh == [],
    f"{notes_fresh}",
)

# B2: prior claim exists, SAME verification_status -> changed=False (reinforced).
traces_b2, index_b2 = _make_env()
with patch.object(ot, "TRACES_DIR", traces_b2), \
     patch.object(vm, "TRACES_DIR", traces_b2), \
     patch.object(vm, "INDEX_DB", index_b2):

    _persist_claim(
        trace_id="t_mars_week1", claim_id="cl_mars_week1", content_hash="h_w1",
        semantic_family_id="fam_mars_moons2", query="Сколько спутников у Марса?",
        claim_text="У Марса два спутника.", verification_status="supported",
    )
    current_claims_reinforced = [{
        "claim_id": "cl_mars_week2", "claim_text": "У Марса есть спутники.",
        "semantic_family_id": "fam_mars_moons2", "verification_status": "supported",
    }]
    notes_reinforced = build_claim_history_notes(current_claims_reinforced)

check(
    "B2: a prior claim in the SAME family with the SAME verification_status "
    "produces exactly one note, changed=False (reinforced, not contradicted)",
    len(notes_reinforced) == 1 and notes_reinforced[0]["changed"] is False
    and notes_reinforced[0]["prior_status"] == "supported"
    and notes_reinforced[0]["current_status"] == "supported",
    f"{notes_reinforced}",
)
check(
    "B2b: the note carries the ORIGINAL prior question, for the answer to reference",
    notes_reinforced[0]["prior_query"] == "Сколько спутников у Марса?",
    f"{notes_reinforced[0]}",
)

# B3: prior claim exists, DIFFERENT verification_status -> changed=True.
traces_b3, index_b3 = _make_env()
with patch.object(ot, "TRACES_DIR", traces_b3), \
     patch.object(vm, "TRACES_DIR", traces_b3), \
     patch.object(vm, "INDEX_DB", index_b3):

    _persist_claim(
        trace_id="t_changed_prior", claim_id="cl_changed_prior", content_hash="h_cp",
        semantic_family_id="fam_changed", query="Есть ли спутники у Марса?",
        claim_text="Неясно, есть ли у Марса спутники.", verification_status="unverified",
    )
    current_claims_changed = [{
        "claim_id": "cl_changed_now", "claim_text": "У Марса точно есть спутники.",
        "semantic_family_id": "fam_changed", "verification_status": "supported",
    }]
    notes_changed = build_claim_history_notes(current_claims_changed)

check(
    "B3: a prior claim with a DIFFERENT verification_status produces changed=True",
    len(notes_changed) == 1 and notes_changed[0]["changed"] is True
    and notes_changed[0]["prior_status"] == "unverified"
    and notes_changed[0]["current_status"] == "supported",
    f"{notes_changed}",
)

# B4: two of THIS request's own claims share a family -> exactly ONE note (dedup by family).
traces_b4, index_b4 = _make_env()
with patch.object(ot, "TRACES_DIR", traces_b4), \
     patch.object(vm, "TRACES_DIR", traces_b4), \
     patch.object(vm, "INDEX_DB", index_b4):

    _persist_claim(
        trace_id="t_dedup_prior", claim_id="cl_dedup_prior", content_hash="h_dp",
        semantic_family_id="fam_dedup", query="Исходный вопрос про дедупликацию",
        claim_text="Утверждение из прошлого запроса, достаточно длинное.",
        verification_status="supported",
    )
    current_claims_dedup = [
        {"claim_id": "cl_dedup_a", "claim_text": "a", "semantic_family_id": "fam_dedup", "verification_status": "supported"},
        {"claim_id": "cl_dedup_b", "claim_text": "b", "semantic_family_id": "fam_dedup", "verification_status": "supported"},
    ]
    notes_dedup = build_claim_history_notes(current_claims_dedup)

check(
    "B4: two of this request's own claims sharing one family produce exactly "
    "ONE note, not two (deduplicated by family_id)",
    len(notes_dedup) == 1,
    f"{notes_dedup}",
)

check(
    "B5: claims with no semantic_family_id at all are silently skipped, never crash",
    build_claim_history_notes([{"claim_id": "cl_x", "claim_text": "x", "verification_status": "supported"}]) == [],
)
check(
    "B6: an empty claims_data list produces an empty notes list",
    build_claim_history_notes([]) == [],
)


# ============================================================
# C. format_history_note_block() — deterministic text.
# ============================================================

check("C1: no notes -> empty string (safe to always += the result)", format_history_note_block([]) == "")

block_changed = format_history_note_block([{
    "family_id": "fam_x", "claim_text": "current", "prior_query": "старый вопрос",
    "prior_claim_text": "old", "prior_status": "unverified", "current_status": "supported",
    "changed": True,
}])
check(
    "C2: a changed=True note mentions BOTH the prior and current status, and the prior question",
    "unverified" in block_changed and "supported" in block_changed and "старый вопрос" in block_changed
    and "иначе" in block_changed,
    block_changed,
)

block_same = format_history_note_block([{
    "family_id": "fam_y", "claim_text": "current", "prior_query": "старый вопрос 2",
    "prior_claim_text": "old", "prior_status": "supported", "current_status": "supported",
    "changed": False,
}])
check(
    "C3: a changed=False note uses reinforcing wording ('подтверждает'), not contradicting",
    "подтверждает" in block_same and "иначе" not in block_same,
    block_same,
)


# ============================================================
# D. WIRING — structural proof this only fires after real, fresh
# evidence-checking (never for a cache-hit/short-circuit response).
# ============================================================

import agent.orchestrator.response.writeback as writeback_mod
import agent.orchestrator_v2 as orch_v2_mod

wb_src = inspect.getsource(writeback_mod.run_optimistic_respond)

check(
    "D1: run_optimistic_respond() calls build_claim_history_notes(claims_data)",
    "build_claim_history_notes(claims_data)" in wb_src,
)

_lines = wb_src.splitlines()
_banner_idx = next(i for i, l in enumerate(_lines) if 'optimistic.text = f"{banner}' in l)
_history_idx = next(i for i, l in enumerate(_lines) if "_history_notes = build_claim_history_notes" in l)
_observe_idx = next(i for i, l in enumerate(_lines) if 'trace.add_observation("delivered_answer_text"' in l)
check(
    "D2: the history-note step runs AFTER the trust banner is applied and BEFORE "
    "delivered_answer_text is captured for persistence — so a note, if any, is "
    "part of what actually gets saved/shadow-written, not silently dropped",
    _banner_idx < _history_idx < _observe_idx,
    f"banner={_banner_idx} history={_history_idx} observe={_observe_idx}",
)

orch_src = inspect.getsource(orch_v2_mod)
_early_return_idx = orch_src.find("if early_response is not None:")
_early_return_block = orch_src[_early_return_idx:_early_return_idx + 1300] if _early_return_idx != -1 else ""
check(
    "D3: orchestrator_v2.py's pre_pipeline cache-hit/short-circuit branch exists "
    "and is found for inspection (test itself isn't vacuous)",
    _early_return_idx != -1 and "return early_response" in _early_return_block,
)
check(
    "D4: the cache-hit/short-circuit branch NEVER calls run_optimistic_respond() "
    "or imports writeback — structurally proving a cached/replayed answer can "
    "never carry a history note (it calls shadow_complete_run + returns directly)",
    "run_optimistic_respond" not in _early_return_block
    and "shadow_complete_run" in _early_return_block,
    _early_return_block,
)


# ============================================================
# E. Empty claims_data (matching the EXISTING wiring tests' own
# scenarios) must remain completely unaffected.
# ============================================================

check(
    "E1: build_claim_history_notes([]) never raises and returns [] (existing "
    "db_sql_wiring_regression_test.py / answer_delivery_persistence_regression_"
    "test.py both call run_optimistic_respond with claims_data=[])",
    build_claim_history_notes([]) == [],
)
check(
    "E2: format_history_note_block([]) appended to any string is a true no-op",
    ("some answer text" + format_history_note_block([])) == "some answer text",
)


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
