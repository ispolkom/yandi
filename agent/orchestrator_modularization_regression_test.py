"""
agent/orchestrator_modularization_regression_test.py — OLD-vs-NEW
equivalence net for the agent/orchestrator/ strangler-fig migration.

Structural extraction from agent/orchestrator_v2.py moves code, it does not
duplicate it — there is a single implementation, owned by
agent/orchestrator/*, that orchestrator_v2.py now calls into. So this suite
is not a branch-comparison (there is no separate "old" implementation left
to diff against); it is a deterministic behavioral pin on each extracted
unit, built from the exact logic that was inline in orchestrator_v2.py
before each move (verified line-for-line against the pre-extraction source
at extraction time — see YANDI_ORCHESTRATOR_MODULARIZATION_MAP.md and the
git history of each `refactor: extract ...` commit).

Run via: /home/iam/venv/bin/python3 -m agent.orchestrator_modularization_regression_test
"""

import time
from types import SimpleNamespace

from agent.orchestrator.epistemic.existence_contract import apply_existence_query_contract
from agent.orchestrator.epistemic.trust_gate import (
    _apply_trust_cap,
    _calculate_delta_factors,
    apply_epistemic_trust_adjustment,
)
from agent.orchestrator.epistemic.final_coverage import evaluate_and_record_final_coverage
from agent.orchestrator.runtime.profiling import report_pipeline_profile
from agent.orchestrator.response.assembly import (
    build_self_answer,
    _generate_character_response,
    _generate_apology_response,
    _adapt_answer_to_style,
)
from agent.orchestrator.claims.status import classify_claim_epistemic_status
from agent.orchestrator.claims.validation import apply_structural_claim_validation
from agent.orchestrator.claims.lifecycle import (
    setup_claim_and_evidence_lifecycle,
    update_beliefs_link_answer_and_personality_cycle,
)
import agent.orchestrator.claims.mapping as mapping_mod
from agent.orchestrator.claims.mapping import run_claim_evidence_batch
import agent.orchestrator.claims.retrieval as retrieval_mod
from agent.orchestrator.claims.retrieval import apply_claim_resolution_and_second_retrieval
import agent.orchestrator.claims.disagreement as disagreement_mod
from agent.orchestrator.claims.disagreement import apply_claim_claim_disagreement
from agent.orchestrator_v2 import LocalSynthesisResult
from agent.orch_tracer import Trace
from agent.epistemic_router import EpistemicClassification

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


def make_trace(query="test query"):
    return Trace(trace_id="t_test", timestamp=time.time(), query=query)


def make_synthesis_result(answer="Юпитер — газовый гигант.", trust_level="SUPPORTED", confidence=0.8):
    return LocalSynthesisResult(answer=answer, trust_level=trust_level, confidence=confidence)


# ============================================================
# 1. epistemic/existence_contract.py
# ============================================================

# Non-existence question: no-op, nothing mutated, returns None.
sr = make_synthesis_result()
result = apply_existence_query_contract(
    "какая масса у Юпитера", [{"claim_id": "c1", "supports_query_aspect": ["CORE"]}], 1, sr, log=lambda m: None,
)
check("existence_contract: non-existence query is a no-op", result is None)
check("existence_contract: non-existence query does not touch synthesis_result", sr.answer == "Юпитер — газовый гигант." and sr.confidence == 0.8)

# Existence question, no CORE claim among the checked claims -> FAILED,
# trust capped, notice prepended.
sr = make_synthesis_result(trust_level="STRONGLY_SUPPORTED", confidence=0.9)
claims = [
    {"claim_id": "c1", "supports_query_aspect": ["BACKGROUND"]},
    {"claim_id": "c2", "supports_query_aspect": ["BACKGROUND"]},
]
result = apply_existence_query_contract("есть ли разумная жизнь на юпитере", claims, 2, sr, log=lambda m: None)
check("existence_contract: no CORE claim -> FAILED", result == "FAILED")
check("existence_contract: FAILED caps trust_level to WEAKLY_SUPPORTED", sr.trust_level == "WEAKLY_SUPPORTED")
check("existence_contract: FAILED caps confidence to <= 0.35", sr.confidence <= 0.35)
check("existence_contract: FAILED prepends warning notice", sr.answer.startswith("⚠️ ВАЖНО:"))

# Existence question, a CORE claim present -> OK, nothing mutated.
sr = make_synthesis_result(trust_level="STRONGLY_SUPPORTED", confidence=0.9)
claims = [{"claim_id": "c1", "supports_query_aspect": ["CORE"]}]
result = apply_existence_query_contract("есть ли разумная жизнь на юпитере", claims, 1, sr, log=lambda m: None)
check("existence_contract: CORE claim present -> OK", result == "OK")
check("existence_contract: OK does not touch trust_level", sr.trust_level == "STRONGLY_SUPPORTED")
check("existence_contract: OK does not touch confidence", sr.confidence == 0.9)


# ============================================================
# 2. epistemic/trust_gate.py — pure helpers
# ============================================================

check("_apply_trust_cap: caps a higher label down", _apply_trust_cap("STRONGLY_SUPPORTED", "PARTIALLY_SUPPORTED") == "PARTIALLY_SUPPORTED")
check("_apply_trust_cap: leaves a lower label untouched", _apply_trust_cap("UNVERIFIED", "SUPPORTED") == "UNVERIFIED")
check("_apply_trust_cap: equal order leaves label untouched", _apply_trust_cap("SUPPORTED", "VERIFIED") == "SUPPORTED")

delta = _calculate_delta_factors("VERIFIED", confidence=1.0, has_sources=True, consensus_agreement=5, total_nodes=5)
check("_calculate_delta_factors: full VERIFIED consensus -> total==0.5 (max clamp)", delta["total"] == 0.5, str(delta))

delta2 = _calculate_delta_factors("REJECTED", confidence=1.0, has_sources=True, consensus_agreement=5, total_nodes=5)
check("_calculate_delta_factors: full REJECTED consensus -> total==-0.5 (min clamp)", delta2["total"] == -0.5, str(delta2))

delta3 = _calculate_delta_factors("TIMEOUT", confidence=0.0, has_sources=False, total_nodes=0)
check("_calculate_delta_factors: zero confidence -> total==0.0", delta3["total"] == 0.0, str(delta3))


# ============================================================
# 3. epistemic/trust_gate.py — apply_epistemic_trust_adjustment
# ============================================================

def make_epistemic_result(**overrides):
    base = dict(
        domain="scientific",
        testability="fully_testable",
        answer_mode="factual",
        trust_score=0.8,
        max_trust_cap="STRONGLY_SUPPORTED",
        need_clarification=False,
        needs_frame_split=False,
        is_science_as_model=False,
    )
    base.update(overrides)
    return EpistemicClassification(**base)


# Scenario A: high epistemic trust, no coverage/grounding penalty -> label
# stays at the epistemic classification's own trust label.
trace = make_trace()
label = apply_epistemic_trust_adjustment(
    is_subjective_answer=False,
    epistemic_trust_label="STRONGLY_SUPPORTED",
    epistemic_result=make_epistemic_result(),
    entity=None,
    final_claim_coverage_score=0.95,
    support_grounding_score=0.9,
    belief_manager=None,
    trace=trace,
    web_used=True,
    claims_data=[{"a": 1}, {"a": 2}],
    search_result=SimpleNamespace(confidence=0.9),
    epistemic_grounding_score=0.9,
    clarification_answered=False,
    is_media_query=False,
    supporting_ids=["c1"],
    coverage_report_data=None,
    intent_result=SimpleNamespace(intent="science"),
)
check("trust_adjustment: high trust + high coverage/grounding -> STRONGLY_SUPPORTED", label == "STRONGLY_SUPPORTED", label)
check("trust_adjustment: trace.trust mirrors returned label", trace.trust == label)

# Scenario B: low final_claim_coverage_score (< 0.50) forces UNVERIFIED.
trace = make_trace()
label = apply_epistemic_trust_adjustment(
    is_subjective_answer=False,
    epistemic_trust_label="STRONGLY_SUPPORTED",
    epistemic_result=make_epistemic_result(),
    entity=None,
    final_claim_coverage_score=0.2,
    support_grounding_score=0.9,
    belief_manager=None,
    trace=trace,
    web_used=False,
    claims_data=[],
    search_result=SimpleNamespace(confidence=0.9),
    epistemic_grounding_score=0.9,
    clarification_answered=False,
    is_media_query=False,
    supporting_ids=[],
    coverage_report_data=None,
    intent_result=SimpleNamespace(intent="science"),
)
check("trust_adjustment: coverage < 0.50 forces UNVERIFIED", label == "UNVERIFIED", label)

# Scenario C: low support_grounding_score (< 0.3) also forces UNVERIFIED.
trace = make_trace()
label = apply_epistemic_trust_adjustment(
    is_subjective_answer=False,
    epistemic_trust_label="STRONGLY_SUPPORTED",
    epistemic_result=make_epistemic_result(),
    entity=None,
    final_claim_coverage_score=0.95,
    support_grounding_score=0.1,
    belief_manager=None,
    trace=trace,
    web_used=False,
    claims_data=[],
    search_result=SimpleNamespace(confidence=0.9),
    epistemic_grounding_score=0.9,
    clarification_answered=False,
    is_media_query=False,
    supporting_ids=[],
    coverage_report_data=None,
    intent_result=SimpleNamespace(intent="science"),
)
check("trust_adjustment: support_grounding < 0.3 forces UNVERIFIED", label == "UNVERIFIED", label)

# Scenario D: subjective answer path skips all epistemic-domain gates and
# stays UNVERIFIED (label init default), regardless of coverage/grounding.
trace = make_trace()
label = apply_epistemic_trust_adjustment(
    is_subjective_answer=True,
    epistemic_trust_label="STRONGLY_SUPPORTED",
    epistemic_result=make_epistemic_result(),
    entity=None,
    final_claim_coverage_score=0.95,
    support_grounding_score=0.9,
    belief_manager=None,
    trace=trace,
    web_used=False,
    claims_data=[],
    search_result=SimpleNamespace(confidence=0.9),
    epistemic_grounding_score=0.9,
    clarification_answered=False,
    is_media_query=False,
    supporting_ids=[],
    coverage_report_data=None,
    intent_result=SimpleNamespace(intent="science"),
)
check("trust_adjustment: subjective answer -> UNVERIFIED (epistemic gates skipped)", label == "UNVERIFIED", label)

# Scenario E: belief-manager average confidence gate downgrades
# STRONGLY_SUPPORTED/SUPPORTED to PARTIALLY_SUPPORTED.
trace = make_trace()
fake_belief_manager = SimpleNamespace(
    get_all_active=lambda: [SimpleNamespace(confidence=0.2), SimpleNamespace(confidence=0.3)]
)
label = apply_epistemic_trust_adjustment(
    is_subjective_answer=False,
    epistemic_trust_label="STRONGLY_SUPPORTED",
    epistemic_result=make_epistemic_result(),
    entity=None,
    final_claim_coverage_score=0.95,
    support_grounding_score=0.9,
    belief_manager=fake_belief_manager,
    trace=trace,
    web_used=False,
    claims_data=[{"a": 1}],
    search_result=SimpleNamespace(confidence=0.9),
    epistemic_grounding_score=0.9,
    clarification_answered=False,
    is_media_query=False,
    supporting_ids=[],
    coverage_report_data=None,
    intent_result=SimpleNamespace(intent="science"),
)
check("trust_adjustment: low avg belief confidence downgrades to PARTIALLY_SUPPORTED", label == "PARTIALLY_SUPPORTED", label)


# ============================================================
# 4. epistemic/final_coverage.py — exception fallback path
#    (deterministic; the success path calls a real LLM extractor and is
#    covered indirectly by the live final_epistemic_regression_test suite)
# ============================================================

import agent.orchestrator.epistemic.final_coverage as final_coverage_mod

_orig_evaluate = final_coverage_mod.evaluate_final_claim_coverage


def _raise(*a, **kw):
    raise RuntimeError("simulated extractor failure")


final_coverage_mod.evaluate_final_claim_coverage = _raise
try:
    cost = {}
    trace = make_trace()
    out = evaluate_and_record_final_coverage(
        make_synthesis_result(), [{"claim_id": "c1"}], "query", cost, trace, log=lambda m: None, verbose=False,
    )
finally:
    final_coverage_mod.evaluate_final_claim_coverage = _orig_evaluate

check("final_coverage: exception fallback returns (1.0, 0, 0, [])", out == (1.0, 0, 0, []), str(out))
check("final_coverage: exception fallback does not write cost[]", "final_coverage_ms" not in cost)


# ============================================================
# 5. runtime/profiling.py
# ============================================================

logged = []
cost = {
    "total_ms": 1000.0,
    "cache_ms": 100.0,
    "synthesize_ms": 500.0,
}
fake_fetch_cache = SimpleNamespace(summary=lambda: {"requests": 3, "unique": 2, "network_fetches": 1, "saved": 1, "hit_ratio": 0.5})
report_pipeline_profile(cost, 1.0, fake_fetch_cache, log=logged.append, verbose=True)
joined = "\n".join(logged)
check("profiling: verbose=True emits [PROFILE] lines", "[PROFILE]" in joined)
check("profiling: verbose=True emits [PROFILE BOTTLENECK]", "[PROFILE BOTTLENECK]" in joined)
check("profiling: bottleneck is the largest cost[] bucket (synthesize)", "synthesize" in [l for l in logged if "[PROFILE BOTTLENECK]" in l][0])
check("profiling: verbose=True emits [Search Work Audit]", "[Search Work Audit]" in joined)

logged2 = []
report_pipeline_profile(cost, 1.0, fake_fetch_cache, log=logged2.append, verbose=False)
check("profiling: verbose=False logs nothing", logged2 == [])


# ============================================================
# 6. response/assembly.py
# ============================================================

manifest = {"name": "YANDI", "role": "помощница", "personality": ["честность"], "epistemology": {"core_belief": "проверяй всё"}}
check("assembly: build_self_answer includes manifest name", "YANDI" in build_self_answer(manifest, "кто ты", {"trust": 50, "irritation": 10}))
check("assembly: build_self_answer low trust branch", "не очень доверяю" in build_self_answer(manifest, "кто ты", {"trust": 10, "irritation": 10}))
check("assembly: build_self_answer high irritation branch", "не в настроении" in build_self_answer(manifest, "кто ты", {"trust": 50, "irritation": 80}))

blocked_char = SimpleNamespace(should_block=lambda: (True, "нарушены границы"))
check("assembly: _generate_character_response honors should_block", "нарушены границы" in _generate_character_response(blocked_char, {}))

open_char = SimpleNamespace(should_block=lambda: (False, ""))
check("assembly: _generate_character_response irritation>80", _generate_character_response(open_char, {"irritation": 90}) == "Мне очень неприятен этот разговор. Я не обязана терпеть такое отношение.")
check("assembly: _generate_character_response low trust + low forgiveness", _generate_character_response(open_char, {"trust": 10, "forgiveness": 10}) == "Я помню, что ты меня обижал. Я ещё не простила.")

check("assembly: _generate_apology_response sincere, low trust", "доверие восстанавливается" in _generate_apology_response(True, {"trust": 10, "forgiveness": 50}))
check("assembly: _generate_apology_response insincere", "формальность" in _generate_apology_response(False, {}))

check("assembly: _adapt_answer_to_style brief truncates long answers", _adapt_answer_to_style("x" * 400, {"style": {"verbosity": "brief"}}).endswith("..."))
check("assembly: _adapt_answer_to_style cold tone prefixes marker", _adapt_answer_to_style("Спасибо большое", {"tone": "cold"}).startswith("[СДЕРЖАННО]"))
check("assembly: _adapt_answer_to_style warm tone prefixes emoji", _adapt_answer_to_style("Хороший вопрос, отвечаю подробно", {"tone": "warm"}).startswith("💭"))


# ============================================================
# 7. claims/status.py
# ============================================================

claims_data = [
    {  # authority-path supported
        "claim_id": "c_supported",
        "verification_status": "candidate",
        "evidence_relations": [
            {"evidence_id": "e1", "relation": "supports", "evidence_role": "direct", "evidence_eligible": True},
        ],
    },
    {  # directness-path contradicted (not local_registry, not hard-blocked, directness>=0.60)
        "claim_id": "c_contradicted",
        "verification_status": "candidate",
        "evidence_relations": [
            {"evidence_id": "e2", "relation": "contradicts", "evidence_role": "secondary", "evidence_eligible": False,
             "source_class": "academic", "retrieval_origin": "web", "directness": 0.75},
        ],
    },
    {  # both supports and contradicts -> disputed
        "claim_id": "c_disputed",
        "verification_status": "candidate",
        "evidence_relations": [
            {"evidence_id": "e3", "relation": "supports", "evidence_role": "direct", "evidence_eligible": True},
            {"evidence_id": "e4", "relation": "contradicts", "evidence_role": "direct", "evidence_eligible": True},
        ],
    },
    {  # no evidence -> unverified
        "claim_id": "c_unverified",
        "verification_status": "candidate",
        "evidence_relations": [],
    },
    {  # structurally rejected -> stays rejected, short-circuits
        "claim_id": "c_rejected",
        "verification_status": "rejected",
        "evidence_relations": [
            {"evidence_id": "e5", "relation": "supports", "evidence_role": "direct", "evidence_eligible": True},
        ],
    },
    {  # local_registry directness path must NOT count (P0-E carve-out)
        "claim_id": "c_registry_directness_excluded",
        "verification_status": "candidate",
        "evidence_relations": [
            {"evidence_id": "e6", "relation": "supports", "evidence_role": "secondary", "evidence_eligible": False,
             "source_class": "academic", "retrieval_origin": "local_registry", "directness": 0.99},
        ],
    },
    {  # hard-blocked source class must NOT count even at high directness
        "claim_id": "c_hard_blocked_excluded",
        "verification_status": "candidate",
        "evidence_relations": [
            {"evidence_id": "e7", "relation": "supports", "evidence_role": "secondary", "evidence_eligible": False,
             "source_class": "forum", "retrieval_origin": "web", "directness": 0.99},
        ],
    },
]

counts = classify_claim_epistemic_status(claims_data, log=lambda m: None, verbose=False)
by_id = {c["claim_id"]: c for c in claims_data}

check("claims/status: authority-path supported claim -> supported", by_id["c_supported"]["verification_status"] == "supported")
check("claims/status: directness-path contradicted claim -> contradicted", by_id["c_contradicted"]["verification_status"] == "contradicted")
check("claims/status: directness relation tagged counted_via=directness", by_id["c_contradicted"]["evidence_relations"][0]["counted_via"] == "directness")
check("claims/status: supports+contradicts -> disputed", by_id["c_disputed"]["verification_status"] == "disputed")
check("claims/status: no evidence -> unverified", by_id["c_unverified"]["verification_status"] == "unverified")
check("claims/status: structurally rejected claim stays rejected", by_id["c_rejected"]["verification_status"] == "rejected")
check("claims/status: local_registry directness path excluded -> unverified, not supported", by_id["c_registry_directness_excluded"]["verification_status"] == "unverified")
check("claims/status: hard-blocked source class excluded -> unverified, not supported", by_id["c_hard_blocked_excluded"]["verification_status"] == "unverified")

check("claims/status: counts tally matches per-claim statuses", counts == {
    "supported": 1, "disputed": 1, "contradicted": 1, "unverified": 3, "rejected": 1,
}, str(counts))


# ============================================================
# 8. claims/validation.py
# ============================================================

fake_validator = SimpleNamespace(
    filter_claims=lambda claims: [c for c in claims if not c.get("_rejected")],
    rejection_reasons={"low_confidence": 1},
)
claims_in = [
    {"claim_id": "v1", "claim_text": "ok claim"},
    {"claim_id": "v2", "claim_text": "bad claim", "_rejected": True, "_rejected_reason": "too_vague", "structural_validation": "rejected"},
]
trace = make_trace()
claims_out, rejected = apply_structural_claim_validation(list(claims_in), fake_validator, {"claims": [1, 2]}, trace, log=lambda m: None, verbose=False)
check("validation: accepted claim passes through", claims_out == [claims_in[0]], str(claims_out))
check("validation: rejected claim is filtered out of claims_data", claims_in[1] not in claims_out)
check("validation: rejected claim is returned separately for diagnostics", rejected == [claims_in[1]])
check("validation: rejected claim recorded on trace.rejected_claims", len(trace.rejected_claims) == 1 and trace.rejected_claims[0]["claim_id"] == "v2" and trace.rejected_claims[0]["rejection_reason"] == "too_vague")

# No validator -> pass-through, no rejections.
claims_out2, rejected2 = apply_structural_claim_validation(list(claims_in), None, {}, make_trace(), log=lambda m: None, verbose=False)
check("validation: no claim_validator -> claims_data unchanged", claims_out2 == claims_in)
check("validation: no claim_validator -> no rejected claims", rejected2 == [])

# filter_claims raises -> claims_data left as the pre-call input, no rejections
# recorded (matches the original inline try/except: only the reassignment and
# rejected-claims computation are inside the try, so an exception before either
# completes leaves both untouched — not "fixed" here, preserved as-is).
def _raise_validator(claims):
    raise RuntimeError("validator boom")


raising_validator = SimpleNamespace(filter_claims=_raise_validator, rejection_reasons={})
claims_out3, rejected3 = apply_structural_claim_validation(list(claims_in), raising_validator, {}, make_trace(), log=lambda m: None, verbose=False)
check("validation: filter_claims exception -> claims_data left unchanged", claims_out3 == claims_in)
check("validation: filter_claims exception -> rejected_structural_claims stays []", rejected3 == [])


# ============================================================
# 9. claims/lifecycle.py
# ============================================================

reasoning_info = {
    "claims": ["простая строка claim", {"claim_text": "уже словарь", "claim_id": "pre_id"}, 123],
    "evidence_records": [{"evidence_id": "ev1", "content_excerpt": "какой-то текст evidence", "evidence_role": "direct", "evidence_eligible": True, "retrieval_origin": "web"}],
    "trust_report": {"foo": "bar"},
    "coverage_report": {"cov": 1},
    "technical_errors": ["timeout on x"],
}
logged9 = []
(
    trust_report_data, trust_reasons, coverage_report_data, claims_data9, evidence_data9, technical_errors9,
) = setup_claim_and_evidence_lifecycle(
    reasoning_info, search_result=None, web_result=None, refutation_snippets=[],
    query_to_use="тестовый запрос", log=logged9.append, verbose=True,
)

check("lifecycle: trust_report_data passed through", trust_report_data == {"foo": "bar"})
check("lifecycle: trust_reasons starts empty", trust_reasons == [])
check("lifecycle: coverage_report_data passed through", coverage_report_data == {"cov": 1})
check("lifecycle: technical_errors passed through", technical_errors9 == ["timeout on x"])
check("lifecycle: synthesizer evidence_records survive into evidence_data", any(ev.get("evidence_id") == "ev1" for ev in evidence_data9))
check("lifecycle: non-dict/non-str claim (123) dropped during normalization", len(claims_data9) == 2, str(claims_data9))
check("lifecycle: every claim gets a claim_id", all(c.get("claim_id") for c in claims_data9))
check("lifecycle: pre-existing claim_id preserved, not overwritten", any(c.get("claim_id") == "pre_id" for c in claims_data9))
check("lifecycle: string claim gets a generated cl_ claim_id", any(c.get("claim_id", "").startswith("cl_") for c in claims_data9))
check("lifecycle: claim_type defaults to factual", all(c.get("claim_type") == "factual" for c in claims_data9))
check("lifecycle: claim_confidence defaults to 0.5", all(c.get("claim_confidence") == 0.5 for c in claims_data9))
check("lifecycle: query_context filled from query_to_use", all(c.get("query_context") == "тестовый запрос" for c in claims_data9))
check("lifecycle: verbose logs [Evidence Pool]", any("[Evidence Pool]" in l for l in logged9))
check("lifecycle: verbose logs dropped-claim diagnostic", any("Пропущен claim неизвестного типа" in l for l in logged9))


# ============================================================
# 10. claims/mapping.py — run_claim_evidence_batch (de-closured
#     _run_claim_evidence_batch; see the extraction commit for the full
#     free-variable audit: only `log`/`verbose` were true closure captures,
#     everything else was already a module-level import).
# ============================================================

_orig_directness = mapping_mod.evaluate_evidence_directness
_orig_classify = mapping_mod.classify_claim_evidence_batch

mapping_mod.evaluate_evidence_directness = lambda claim_text, ev_text: 0.75

captured_classify_calls = []


def _fake_classify(jobs, batch_size=8):
    captured_classify_calls.append({"jobs": jobs, "batch_size": batch_size})
    return {
        "c1": {
            "supports": [{
                "evidence_id": "e1",
                "relation_method": "nli",
                "source_claim": "evidence one text",
                "source_class": "academic",
                "quality_score": 0.9,
                "evidence_eligible": True,
                "evidence_role": "direct",
                "retrieval_origin": "web",
                "directness": 0.75,
            }],
        },
        # c3 intentionally absent from the classifier's output -> must fall
        # back to an empty evidence_relations list, not crash.
    }


mapping_mod.classify_claim_evidence_batch = _fake_classify

claims_m = [
    {"claim_id": "c1", "claim_text": "claim one text", "derived_from_evidence_ids": ["e1", "e2"]},
    {"claim_id": "c2", "claim_text": "", "derived_from_evidence_ids": ["e1"]},
    {"claim_id": "c3", "claim_text": "claim three", "derived_from_evidence_ids": ["e_missing"]},
]
evidence_m = [
    {"evidence_id": "e1", "content_excerpt": "evidence one text", "source_type": "web", "source_uri": "http://x", "source_class": "academic", "quality_score": 0.9, "evidence_eligible": True, "evidence_role": "direct", "retrieval_origin": "web"},
    {"evidence_id": "e2", "content_excerpt": "", "source_type": "web"},  # empty excerpt -> must be skipped
]

logged10 = []
try:
    relation_count = run_claim_evidence_batch(claims_m, evidence_m, "PASS1", logged10.append, True)

    by_id_m = {c["claim_id"]: c for c in claims_m}

    check("mapping: empty claim_text -> evidence_relations=[] and excluded from jobs", by_id_m["c2"]["evidence_relations"] == [])
    check("mapping: empty claim_text claim excluded from classifier jobs", all(j["claim_id"] != "c2" for j in captured_classify_calls[0]["jobs"]))
    check("mapping: linked evidence with empty content_excerpt (e2) skipped as a candidate source", len(captured_classify_calls[0]["jobs"][0]["sources"]) == 1 and captured_classify_calls[0]["jobs"][0]["sources"][0]["evidence_id"] == "e1")
    check("mapping: missing linked evidence id (e_missing) yields zero candidate sources, claim still gets a job", any(j["claim_id"] == "c3" and j["sources"] == [] for j in captured_classify_calls[0]["jobs"]))
    check("mapping: classify called with batch_size=8 (unchanged)", captured_classify_calls[0]["batch_size"] == 8)
    check("mapping: c1 evidence_relations populated from classifier output, field-mapped correctly", by_id_m["c1"]["evidence_relations"] == [{
        "evidence_id": "e1", "relation": "supports", "method": "nli", "source_claim": "evidence one text",
        "error": None, "source_class": "academic", "quality_score": 0.9, "evidence_eligible": True,
        "evidence_role": "direct", "retrieval_origin": "web", "directness": 0.75,
    }], str(by_id_m["c1"]["evidence_relations"]))
    check("mapping: c3 absent from classifier output -> evidence_relations=[] (no crash)", by_id_m["c3"]["evidence_relations"] == [])
    check("mapping: relation_count return value == total relations written", relation_count == 1, str(relation_count))
    check("mapping: verbose=True logs [Evidence Eligibility] per candidate pair", any("[Evidence Eligibility]" in l for l in logged10))
    check("mapping: verbose=True logs [Claim Evidence Batch PASS1] summary with batch_label", any("[Claim Evidence Batch PASS1]" in l for l in logged10))

    # verbose=False -> no log calls at all.
    logged10b = []
    claims_m2 = [{"claim_id": "c1", "claim_text": "x", "derived_from_evidence_ids": []}]
    run_claim_evidence_batch(claims_m2, [], "PASS2", logged10b.append, False)
    check("mapping: verbose=False emits no log lines", logged10b == [])

    # Same call shape works for PASS2's batch_label (mechanical param, not a
    # different code path) -> just confirm the label is threaded through.
    logged10c = []
    claims_m3 = [{"claim_id": "c1", "claim_text": "claim one text", "derived_from_evidence_ids": ["e1"]}]
    run_claim_evidence_batch(claims_m3, evidence_m, "PASS2", logged10c.append, True)
    check("mapping: PASS2 batch_label threaded through unchanged", any("[Claim Evidence Batch PASS2]" in l for l in logged10c))

    # Exception/fallback behavior: the original closure had no try/except
    # around the classifier call, so an exception must propagate unchanged
    # (no silent fallback introduced by the extraction).
    def _raise_classify(jobs, batch_size=8):
        raise RuntimeError("classifier boom")

    mapping_mod.classify_claim_evidence_batch = _raise_classify
    threw = False
    try:
        run_claim_evidence_batch([{"claim_id": "c1", "claim_text": "x", "derived_from_evidence_ids": []}], [], "PASS1", lambda m: None, False)
    except RuntimeError:
        threw = True
    check("mapping: classifier exception propagates unchanged (no swallowed fallback)", threw)

finally:
    mapping_mod.evaluate_evidence_directness = _orig_directness
    mapping_mod.classify_claim_evidence_batch = _orig_classify


# ============================================================
# 11. claims/retrieval.py — apply_claim_resolution_and_second_retrieval
# ============================================================

_orig_retrieve = retrieval_mod.retrieve_for_claims
_orig_merge_r = retrieval_mod.merge_evidence
_orig_map_r = retrieval_mod.map_claims_to_evidence
_orig_batch_r = retrieval_mod.run_claim_evidence_batch

captured_retrieve_calls = []


def _fake_retrieve(claims, fetch_cache=None):
    captured_retrieve_calls.append({"claims": [c["claim_id"] for c in claims], "fetch_cache": fetch_cache})
    return [{"evidence_id": "new_ev1", "content_excerpt": "new evidence text"}]


def _fake_merge_growth(base, extra):
    return list(base) + list(extra)


map_calls = []


def _fake_map(claims, evidence):
    map_calls.append(len(claims))
    return [
        SimpleNamespace(claim_id=c["claim_id"], derived_from_evidence_ids=["new_ev1"] if c["claim_id"] == "need1" else [])
        for c in claims
    ]


batch_calls = []


def _fake_batch(claims, evidence, batch_label, log, verbose):
    batch_calls.append(batch_label)
    return 1


retrieval_mod.retrieve_for_claims = _fake_retrieve
retrieval_mod.merge_evidence = _fake_merge_growth
retrieval_mod.map_claims_to_evidence = _fake_map
retrieval_mod.run_claim_evidence_batch = _fake_batch

try:
    claims_r = [
        {"claim_id": "resolved1", "verification_status": "supported", "evidence_relations": [{"evidence_role": "direct", "evidence_eligible": True, "relation": "supports", "evidence_id": "e0"}]},
        {"claim_id": "rejected1", "verification_status": "rejected", "evidence_relations": []},
        {"claim_id": "need1", "verification_status": "unverified", "evidence_relations": []},
    ]
    evidence_r = [{"evidence_id": "e0", "content_excerpt": "x"}]
    cost_r = {}
    logged_r = []

    out_evidence = apply_claim_resolution_and_second_retrieval(
        claims_r, evidence_r, True, False, False, "FAKE_CACHE", cost_r, logged_r.append, True,
    )

    check("retrieval: resolved claim (effective evidence) excluded from retrieval set", captured_retrieve_calls[0]["claims"] == ["need1"], str(captured_retrieve_calls))
    check("retrieval: rejected claim excluded from retrieval set", "rejected1" not in captured_retrieve_calls[0]["claims"])
    check("retrieval: fetch_cache threaded through unchanged", captured_retrieve_calls[0]["fetch_cache"] == "FAKE_CACHE")
    check("retrieval: cost[claim_retrieval_ms] recorded", "claim_retrieval_ms" in cost_r)
    check("retrieval: evidence_data merged/grew by the retrieved evidence", len(out_evidence) == 2, str(out_evidence))
    check("retrieval: mapper PASS2 triggered (added_count>0), called over ALL claims_data (not just retrieval_claims)", map_calls == [3], str(map_calls))
    check("retrieval: PASS2 batch NLI triggered with label PASS2", batch_calls == ["PASS2"])
    check("retrieval: cost[claim_pass2_mapping_nli_ms] recorded", "claim_pass2_mapping_nli_ms" in cost_r)
    check("retrieval: derived_from_evidence_ids written for the retrieved claim", claims_r[2]["derived_from_evidence_ids"] == ["new_ev1"])
    check("retrieval: [Claim Resolution Gate] log emitted", any("[Claim Resolution Gate]" in l for l in logged_r))
    check("retrieval: [Claim Retrieval Pass 2] log emitted", any("[Claim Retrieval Pass 2]" in l for l in logged_r))
    check("retrieval: [Claim Evidence NLI Pass 2] log emitted", any("[Claim Evidence NLI Pass 2]" in l for l in logged_r))

    # enable_web=False -> gate still runs, but retrieval never fires.
    captured_retrieve_calls.clear()
    evidence_r2 = [{"evidence_id": "e0"}]
    out2 = apply_claim_resolution_and_second_retrieval(claims_r, evidence_r2, False, False, False, None, {}, lambda m: None, False)
    check("retrieval: enable_web=False -> retrieve_for_claims not called", captured_retrieve_calls == [])
    check("retrieval: enable_web=False -> evidence_data returned unchanged (same object)", out2 is evidence_r2)

    # skip_rag=True -> no retrieval.
    out3 = apply_claim_resolution_and_second_retrieval(claims_r, evidence_r2, True, False, True, None, {}, lambda m: None, False)
    check("retrieval: skip_rag=True -> retrieve_for_claims not called", captured_retrieve_calls == [])

    # is_subjective_answer=True -> no retrieval.
    out4 = apply_claim_resolution_and_second_retrieval(claims_r, evidence_r2, True, True, False, None, {}, lambda m: None, False)
    check("retrieval: is_subjective_answer=True -> retrieve_for_claims not called", captured_retrieve_calls == [])

    # No claims need retrieval (all resolved/rejected) -> no retrieval call.
    claims_all_resolved = [claims_r[0], claims_r[1]]
    out5 = apply_claim_resolution_and_second_retrieval(claims_all_resolved, evidence_r2, True, False, False, None, {}, lambda m: None, False)
    check("retrieval: no claims need retrieval -> retrieve_for_claims not called", captured_retrieve_calls == [])

    # added_count == 0 (merge doesn't grow the pool) -> mapper/PASS2 NOT triggered.
    map_calls.clear()
    batch_calls.clear()
    retrieval_mod.merge_evidence = lambda base, extra: list(base)
    claims_need = [{"claim_id": "need2", "verification_status": "unverified", "evidence_relations": []}]
    cost6 = {}
    apply_claim_resolution_and_second_retrieval(claims_need, [{"evidence_id": "e0"}], True, False, False, None, cost6, lambda m: None, False)
    check("retrieval: added_count==0 -> mapper PASS2 NOT triggered", map_calls == [])
    check("retrieval: added_count==0 -> claim_pass2_mapping_nli_ms NOT set", "claim_pass2_mapping_nli_ms" not in cost6)

    # Exception in retrieve_for_claims is caught (the original inline
    # try/except logs and swallows it — unlike run_claim_evidence_batch,
    # which has no try/except at all and propagates). evidence_data must
    # come back unchanged since the reassignment never runs.
    def _raise_retrieve(claims, fetch_cache=None):
        raise RuntimeError("retrieval boom")

    retrieval_mod.retrieve_for_claims = _raise_retrieve
    evidence_before_exc = [{"evidence_id": "e0"}]
    logged_exc = []
    out_exc = apply_claim_resolution_and_second_retrieval(claims_need, evidence_before_exc, True, False, False, None, {}, logged_exc.append, True)
    check("retrieval: retrieve_for_claims exception is caught (swallowed), does not propagate", out_exc is evidence_before_exc)
    check("retrieval: on exception, [Claim Retrieval Pass 2] error logged", any("[Claim Retrieval Pass 2] error=" in l for l in logged_exc))

finally:
    retrieval_mod.retrieve_for_claims = _orig_retrieve
    retrieval_mod.merge_evidence = _orig_merge_r
    retrieval_mod.map_claims_to_evidence = _orig_map_r
    retrieval_mod.run_claim_evidence_batch = _orig_batch_r


# ============================================================
# 12. claims/lifecycle.py — update_beliefs_link_answer_and_personality_cycle
# ============================================================

captured_add_belief_calls = []
fake_belief_manager_bl = SimpleNamespace(
    add_belief=lambda **kw: captured_add_belief_calls.append(kw),
    get_stats=lambda: {"total": 5},
)
fake_linker_bl = SimpleNamespace(
    link_answer_to_claims=lambda answer, claims: (None, ["b1", "b3"])
)
captured_personality_calls = []
fake_personality_bl = SimpleNamespace(
    increment_cycles=lambda: captured_personality_calls.append("cycles"),
    increment_decisions=lambda: captured_personality_calls.append("decisions"),
    get_summary=lambda: {"name": "YANDI", "cycles": 5},
)

claims_bl = [
    {"claim_id": "b1", "claim_text": "a" * 25, "claim_confidence": 0.9, "evidence_relations": [{"evidence_role": "direct", "evidence_eligible": True, "relation": "supports", "evidence_id": "e1"}]},
    {"claim_id": "b2", "claim_text": "short", "claim_confidence": 0.9, "evidence_relations": []},
    {"claim_id": "b3", "claim_text": "c" * 25, "claim_confidence": 0.9, "evidence_relations": [{"evidence_role": "direct", "evidence_eligible": True, "relation": "contradicts", "evidence_id": "e2"}]},
    {"claim_id": "b4", "claim_text": "d" * 25, "claim_confidence": 0.9, "evidence_relations": []},
]
cost_bl = {}
logged_bl = []
supporting_ids_bl = update_beliefs_link_answer_and_personality_cycle(
    claims_bl, make_synthesis_result(), make_epistemic_result(), False,
    fake_belief_manager_bl, fake_linker_bl, fake_personality_bl,
    cost_bl, logged_bl.append, True,
)

check("beliefs: add_belief called for exactly the 2 eligible claims (b1, b3)", len(captured_add_belief_calls) == 2, str(captured_add_belief_calls))
check("beliefs: short claim_text (<=20 chars) skipped", all(c["claim_ids"] != ["b2"] for c in captured_add_belief_calls))
check("beliefs: only claims_data[:3] considered, 4th claim (b4) never reached", all(c["claim_ids"] != ["b4"] for c in captured_add_belief_calls))
check("beliefs: evidence_for/against correctly split by relation", captured_add_belief_calls[0]["evidence_for"] == ["e1"] and captured_add_belief_calls[0]["evidence_against"] == [])
check("beliefs: confidence capped at 0.5 when support present", captured_add_belief_calls[0]["confidence"] == 0.5)
check("beliefs: confidence further capped to 0.35 when only contradicts (no supports)", captured_add_belief_calls[1]["confidence"] == 0.35)
check("beliefs: topic uses epistemic_result.domain when not subjective", captured_add_belief_calls[0]["topic"] == "scientific")
check("beliefs: cost[belief_update_ms] recorded", "belief_update_ms" in cost_bl)
check("linker: supporting_ids returned from claim_answer_linker", supporting_ids_bl == ["b1", "b3"])
check("personality: increment_cycles and increment_decisions both called once", captured_personality_calls == ["cycles", "decisions"])
check("beliefs: [Belief] logged per candidate", sum("[Belief] candidate=" in l for l in logged_bl) == 2)
check("beliefs: [V6] Beliefs обработано summary logged", any("[V6] Beliefs обработано" in l for l in logged_bl))
check("beliefs: [Belief Update Timing] logged", any("[Belief Update Timing]" in l for l in logged_bl))
check("linker: [V6] Связано claims logged", any("[V6] Связано claims" in l for l in logged_bl))
check("personality: [V6] Личность logged", any("[V6] Личность" in l for l in logged_bl))

# subjective_answer=True -> topic becomes "subjective", not epistemic_result.domain.
captured_add_belief_calls.clear()
update_beliefs_link_answer_and_personality_cycle(
    [claims_bl[0]], make_synthesis_result(), make_epistemic_result(), True,
    fake_belief_manager_bl, None, None, {}, lambda m: None, False,
)
check("beliefs: subjective answer -> topic='subjective'", captured_add_belief_calls[0]["topic"] == "subjective")

# All three collaborators None -> no crash, supporting_ids stays [], cost still recorded.
cost_none = {}
supporting_ids_none = update_beliefs_link_answer_and_personality_cycle(
    claims_bl, make_synthesis_result(), make_epistemic_result(), False,
    None, None, None, cost_none, lambda m: None, False,
)
check("beliefs: all collaborators None -> supporting_ids stays []", supporting_ids_none == [])
check("beliefs: all collaborators None -> cost[belief_update_ms] still recorded", "belief_update_ms" in cost_none)

# Exceptions in each collaborator are individually caught and swallowed —
# none crash the call, none affect the others' results.
def _raise_add_belief(**kw):
    raise RuntimeError("belief boom")


def _raise_link(answer, claims):
    raise RuntimeError("linker boom")


def _raise_cycles():
    raise RuntimeError("personality boom")


raising_belief_manager = SimpleNamespace(add_belief=_raise_add_belief, get_stats=lambda: {"total": 0})
raising_linker = SimpleNamespace(link_answer_to_claims=_raise_link)
raising_personality = SimpleNamespace(increment_cycles=_raise_cycles, increment_decisions=lambda: None, get_summary=lambda: {"name": "", "cycles": 0})

cost_exc = {}
supporting_ids_exc = update_beliefs_link_answer_and_personality_cycle(
    claims_bl, make_synthesis_result(), make_epistemic_result(), False,
    raising_belief_manager, raising_linker, raising_personality,
    cost_exc, lambda m: None, True,
)
check("beliefs: add_belief exception caught, no crash", True)
check("beliefs: add_belief exception -> cost[belief_update_ms] still recorded", "belief_update_ms" in cost_exc)
check("linker: exception caught -> supporting_ids left at [] (assignment never completed)", supporting_ids_exc == [])
check("personality: exception caught -> does not propagate or affect return value", True)


# ============================================================
# 13. claims/disagreement.py — apply_claim_claim_disagreement
# ============================================================
#
# The embedding call uses a raw `requests.Session().post(...)` created
# *inside* the function body (matches the original inline code exactly —
# not something this migration should "fix"), so it can't be monkeypatched
# via a module-level attribute the way the other extractions' dependencies
# are. Forcing requests.Session.post to raise exercises the "FAIL-OPEN FOR
# CORRECTNESS" branch deterministically (semantic_available=False -> full
# candidate pair set, still routed through batch NLI) without any real
# network call — this is itself a real, load-bearing code path (embedding
# service down), not just a test convenience.

import requests as _requests_mod

_orig_session_post = _requests_mod.Session.post
_orig_infer_batch = disagreement_mod.infer_claim_relations_batch


def _raise_post(self, *a, **kw):
    raise RuntimeError("embed endpoint unreachable (test)")


captured_infer_calls = []


def _fake_infer_batch(claim_pairs, batch_size=16):
    captured_infer_calls.append({"pairs": list(claim_pairs), "batch_size": batch_size})
    results = []
    for i, pair in enumerate(claim_pairs):
        if i == 0:
            results.append({"pair_id": pair["pair_id"], "relation": "contradicts", "method": "llm_nli_batch"})
        else:
            results.append({"pair_id": pair["pair_id"], "relation": "unrelated", "method": "llm_nli_batch"})
    return results


captured_challenge_calls = []


class _FakeDisagreementEngine:
    def challenge(self, **kw):
        captured_challenge_calls.append(kw)


_requests_mod.Session.post = _raise_post
disagreement_mod.infer_claim_relations_batch = _fake_infer_batch

try:
    claims_dis = [
        {"claim_id": "d1", "claim_text": "Земля вращается вокруг Солнца", "claim_confidence": 0.3},
        {"claim_id": "d2", "claim_text": "Солнце вращается вокруг Земли", "claim_confidence": 0.9},
        {"claim_id": "d3", "claim_text": "Луна — спутник Земли", "claim_confidence": 0.5},
    ]
    cost_dis = {}
    logged_dis = []
    engine = _FakeDisagreementEngine()

    apply_claim_claim_disagreement(claims_dis, engine, make_epistemic_result(), False, cost_dis, logged_dis.append, True)

    check("disagreement: embedding failure -> fail-open, full pair set sent to batch NLI", len(captured_infer_calls[0]["pairs"]) == 3, str(captured_infer_calls))
    check("disagreement: batch_size unchanged (16)", captured_infer_calls[0]["batch_size"] == 16)
    check("disagreement: [Claim↔Claim Prefilter] logs semantic=fallback on embedding failure", any("semantic=fallback" in l for l in logged_dis))
    check("disagreement: [Claim↔Claim Prefilter] logs the embedding_error", any("embedding_error=" in l for l in logged_dis))
    check("disagreement: contradicts+llm_nli_batch triggers exactly one challenge() call", len(captured_challenge_calls) == 1, str(captured_challenge_calls))
    check("disagreement: challenge() topic uses epistemic_result.domain (not subjective)", captured_challenge_calls[0]["topic"] == "scientific")
    check("disagreement: challenge() old_position is claim d1's text (first of the pair)", captured_challenge_calls[0]["old_position"] == "Земля вращается вокруг Солнца")
    check("disagreement: challenge() new_position picks the higher-confidence claim's text (d2, conf=0.9)", captured_challenge_calls[0]["new_position"] == "Солнце вращается вокруг Земли")
    check("disagreement: challenge() confidence_before/after come from claim_confidence", captured_challenge_calls[0]["confidence_before"] == 0.3 and captured_challenge_calls[0]["confidence_after"] == 0.9)
    check("disagreement: cost[claim_claim_nli_ms] recorded", "claim_claim_nli_ms" in cost_dis)
    check("disagreement: [Claim↔Claim Batch Summary] logged with contradicts=1", any("[Claim↔Claim Batch Summary]" in l and "contradicts=1" in l for l in logged_dis))
    check("disagreement: [Claim↔Claim Timing] logged", any("[Claim↔Claim Timing]" in l for l in logged_dis))
    check("disagreement: [V6] Зафиксирован спор logged", any("Зафиксирован спор" in l for l in logged_dis))

    # disagreement_engine=None -> no-op, no NLI call, cost untouched.
    captured_infer_calls.clear()
    captured_challenge_calls.clear()
    cost_none_dis = {}
    apply_claim_claim_disagreement(claims_dis, None, make_epistemic_result(), False, cost_none_dis, lambda m: None, False)
    check("disagreement: disagreement_engine=None -> no-op, batch NLI never called", captured_infer_calls == [])
    check("disagreement: disagreement_engine=None -> cost untouched", cost_none_dis == {})

    # len(claims_data) <= 1 -> no-op.
    apply_claim_claim_disagreement([claims_dis[0]], engine, make_epistemic_result(), False, {}, lambda m: None, False)
    check("disagreement: single claim -> no-op, batch NLI never called", captured_infer_calls == [])

    # Non-"llm_nli_batch" method never triggers a challenge, even if relation=="contradicts".
    captured_challenge_calls.clear()

    def _fake_infer_fallback_method(claim_pairs, batch_size=16):
        return [{"pair_id": p["pair_id"], "relation": "contradicts", "method": "batch_fallback"} for p in claim_pairs]

    disagreement_mod.infer_claim_relations_batch = _fake_infer_fallback_method
    apply_claim_claim_disagreement(claims_dis, engine, make_epistemic_result(), False, {}, lambda m: None, False)
    check("disagreement: contradicts via batch_fallback (not llm_nli_batch) does NOT trigger challenge", captured_challenge_calls == [])

    # Exception inside the block (batch NLI raises) is caught and logged,
    # never propagates.
    def _raise_infer(claim_pairs, batch_size=16):
        raise RuntimeError("nli boom")

    disagreement_mod.infer_claim_relations_batch = _raise_infer
    logged_exc_dis = []
    apply_claim_claim_disagreement(claims_dis, engine, make_epistemic_result(), False, {}, logged_exc_dis.append, True)
    check("disagreement: batch NLI exception is caught, does not propagate", True)
    check("disagreement: batch NLI exception logs [V6] Ошибка batch спора", any("Ошибка batch спора" in l for l in logged_exc_dis))

finally:
    _requests_mod.Session.post = _orig_session_post
    disagreement_mod.infer_claim_relations_batch = _orig_infer_batch


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
