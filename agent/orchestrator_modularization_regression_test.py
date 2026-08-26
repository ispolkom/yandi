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


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
