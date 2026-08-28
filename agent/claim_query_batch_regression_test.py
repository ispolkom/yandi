"""
agent/claim_query_batch_regression_test.py — P1 regression (batch
claim-specific query generation, performance architecture pass).

Live offline experiment (7 claims, 3 domains: Jupiter temperature/life/
magnetic-field, Mars, Russian history, math, biology) at batch sizes
2/4/8 already proved on real Ollama output: query ownership 100%, ZERO
cross-claim subject-anchor leakage, epistemic modality (negation)
preserved in every case. This suite covers the MECHANISM deterministically
with mocked LLM responses: ownership guarantee, bounded per-claim
fallback (not a second unbounded per-claim round for the WHOLE batch),
whole-batch call-error fallback, and the precomputed_query_result wiring
into retrieve_claim_evidence()/retrieve_for_claims().

Run: /home/iam/venv/bin/python3 -m agent.claim_query_batch_regression_test
"""

import json
from unittest.mock import patch, MagicMock

import agent.claim_evidence_retriever as cer
from agent.claim_evidence_retriever import (
    formulate_claim_evidence_queries_batch,
    _build_contextual_claim_text,
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


# ── 1. Full ownership: every claim_id present in a valid batch response ──

claims = [
    {"claim_id": "cl_A", "claim_text": "Юпитер является газовым гигантом."},
    {"claim_id": "cl_B", "claim_text": "Марс имеет тонкую атмосферу."},
    {"claim_id": "cl_C", "claim_text": "Бозон Хиггса был обнаружен в 2012 году."},
]


def _mock_ok_response(*args, **kwargs):
    return json.dumps({
        "cl_A": {"direct": "Jupiter gas giant composition data", "counter": "Jupiter not gas giant alternative theory"},
        "cl_B": {"direct": "Mars thin atmosphere measurements", "counter": "Mars thick atmosphere counter evidence"},
        "cl_C": {"direct": "Higgs boson discovered 2012 LHC evidence", "counter": "Higgs boson not detected LHC null result"},
    }, ensure_ascii=False)


with patch.object(cer, "_call_ollama_for_query_batch", _mock_ok_response):
    result_map = formulate_claim_evidence_queries_batch(claims)

check(
    "full ownership: every input claim_id present in output",
    all(c["claim_id"] in result_map for c in claims),
    f"got keys={list(result_map.keys())}",
)
check(
    "each claim got exactly 2 queries (direct + counter)",
    all(len(result_map[c["claim_id"]].queries) == 2 for c in claims),
)
check(
    "no cross-claim leakage: cl_A's queries mention Jupiter, not Mars/Higgs",
    "jupiter" in " ".join(result_map["cl_A"].queries).lower()
    and "mars" not in " ".join(result_map["cl_A"].queries).lower()
    and "higgs" not in " ".join(result_map["cl_A"].queries).lower(),
)

# ── 2. Partial batch response: one claim_id missing -> bounded fallback ──

def _mock_partial_response(*args, **kwargs):
    return json.dumps({
        "cl_A": {"direct": "Jupiter gas giant composition data", "counter": "Jupiter not gas giant alternative theory"},
        # cl_B missing entirely
        "cl_C": {"direct": "Higgs boson discovered 2012 LHC evidence", "counter": "Higgs boson not detected LHC null result"},
    }, ensure_ascii=False)


fallback_calls = []


def _mock_fallback_single(claim_text):
    fallback_calls.append(claim_text)
    from agent.orch_schemas import WebQueryResult
    return WebQueryResult(queries=["fallback direct query", "fallback counter query"], raw="")


with patch.object(cer, "_call_ollama_for_query_batch", _mock_partial_response):
    with patch.object(cer, "formulate_claim_evidence_queries", _mock_fallback_single):
        result_map2 = formulate_claim_evidence_queries_batch(claims)

check(
    "partial batch: ALL claims still present in output (missing one triggers bounded fallback)",
    all(c["claim_id"] in result_map2 for c in claims),
    f"got keys={list(result_map2.keys())}",
)
check(
    "partial batch: only the MISSING claim (cl_B) triggered a fallback call, not all 3",
    len(fallback_calls) == 1,
    f"got {len(fallback_calls)} fallback calls: {fallback_calls}",
)
check(
    "partial batch: cl_A and cl_C used the batch result, not fallback",
    "gas giant" in " ".join(result_map2["cl_A"].queries).lower()
    and "higgs" in " ".join(result_map2["cl_C"].queries).lower(),
)

# ── 3. Whole-batch call error -> ALL claims fall back (bounded, diagnosed) ──

def _mock_error(*args, **kwargs):
    raise ConnectionError("simulated Ollama outage")


fallback_calls.clear()

with patch.object(cer, "_call_ollama_for_query_batch", _mock_error):
    with patch.object(cer, "formulate_claim_evidence_queries", _mock_fallback_single):
        result_map3 = formulate_claim_evidence_queries_batch(claims)

check(
    "whole-batch call error: all 3 claims still get queries via fallback",
    len(result_map3) == 3 and all(len(r.queries) == 2 for r in result_map3.values()),
)
check(
    "whole-batch call error: fallback triggered for all 3 (not a crash, not silently empty)",
    len(fallback_calls) == 3,
    f"got {len(fallback_calls)}",
)

# ── 4. Malformed JSON response -> treated like missing data, bounded fallback ──

fallback_calls.clear()


def _mock_garbage(*args, **kwargs):
    return "this is not json at all {{{"


with patch.object(cer, "_call_ollama_for_query_batch", _mock_garbage):
    with patch.object(cer, "formulate_claim_evidence_queries", _mock_fallback_single):
        result_map4 = formulate_claim_evidence_queries_batch(claims)

check(
    "malformed JSON response: all claims still resolved via fallback, no crash",
    len(result_map4) == 3,
)
check(
    "malformed JSON response: fallback used for all 3 (batch contributed nothing usable)",
    len(fallback_calls) == 3,
)

# ── 5. Empty/invalid input claims are skipped gracefully ──

empty_result = formulate_claim_evidence_queries_batch([])
check("empty input list -> empty result, no crash", empty_result == {})

garbage_claims = [{"claim_id": "cl_X", "claim_text": "   "}, {"claim_id": "", "claim_text": "real text"}]
garbage_result = formulate_claim_evidence_queries_batch(garbage_claims)
check("claims with empty text or empty claim_id are filtered out, no crash", garbage_result == {})

# ── 6. _build_contextual_claim_text: shared helper produces identical
#      output to what retrieve_claim_evidence() used to build inline ──

claim_with_context = {"claim_text": "Температура -145C.", "query_context": "Есть ли жизнь на Юпитере?"}
claim_without_context = {"claim_text": "Температура -145C."}

check(
    "_build_contextual_claim_text: includes query_context when present",
    "Есть ли жизнь на Юпитере?" in _build_contextual_claim_text(claim_with_context, "Температура -145C."),
)
check(
    "_build_contextual_claim_text: falls back to bare claim_text when no context",
    _build_contextual_claim_text(claim_without_context, "Температура -145C.") == "Температура -145C.",
)

# ============================================================
# 7. WIRING: retrieve_claim_evidence() actually SKIPS its own
#    generation call when precomputed_query_result is provided.
# ============================================================

generation_call_count = {"n": 0}


def _mock_formulate_single(text):
    generation_call_count["n"] += 1
    from agent.orch_schemas import WebQueryResult
    return WebQueryResult(queries=["should not be used"], raw="")


class FakeSnippet:
    url = "https://example.com/x"
    title = "T"
    text = "Юпитер является газовым гигантом. " * 5
    content = text


def fake_scrape_stub(direct_query, counter_query, fetch_cache=None, claim_id=""):
    result = MagicMock()
    result.snippets = [FakeSnippet()]
    return result


from agent.orch_schemas import WebQueryResult as _WQR

with patch.object(cer, "formulate_claim_evidence_queries", _mock_formulate_single):
    with patch.object(cer, "scrape_budgeted", fake_scrape_stub):
        with patch.object(cer, "extract_claim_from_source", return_value="Юпитер является газовым гигантом."):
            with patch.object(cer, "_subject_anchor_matches", return_value=(True, ["title"])):
                with patch.object(cer, "is_relevant", return_value=True):
                    with patch.object(cer, "evaluate_source_quality") as mock_q:
                        mock_q.return_value = MagicMock(
                            quality_score=0.9, source_class="reference", evidence_eligible=True,
                            evidence_role="direct", authority=0.8, traceability=0.8, primaryness=0.8, reasons=[],
                        )
                        precomputed = _WQR(queries=["precomputed direct query", "precomputed counter query"], raw="")
                        records = cer.retrieve_claim_evidence(
                            {"claim_id": "cl_test", "claim_text": "Юпитер тест."},
                            precomputed_query_result=precomputed,
                        )

check(
    "retrieve_claim_evidence with precomputed_query_result -> does NOT call formulate_claim_evidence_queries()",
    generation_call_count["n"] == 0,
    f"got {generation_call_count['n']} calls",
)
check("retrieve_claim_evidence with precomputed queries still returns evidence", len(records) == 1)

# Non-regression: WITHOUT precomputed_query_result, the per-claim path still works.
generation_call_count["n"] = 0
with patch.object(cer, "formulate_claim_evidence_queries", _mock_formulate_single):
    with patch.object(cer, "scrape_budgeted", fake_scrape_stub):
        with patch.object(cer, "extract_claim_from_source", return_value="Юпитер является газовым гигантом."):
            with patch.object(cer, "_subject_anchor_matches", return_value=(True, ["title"])):
                with patch.object(cer, "is_relevant", return_value=True):
                    with patch.object(cer, "evaluate_source_quality") as mock_q:
                        mock_q.return_value = MagicMock(
                            quality_score=0.9, source_class="reference", evidence_eligible=True,
                            evidence_role="direct", authority=0.8, traceability=0.8, primaryness=0.8, reasons=[],
                        )
                        cer.retrieve_claim_evidence({"claim_id": "cl_test2", "claim_text": "Юпитер тест 2."})

check(
    "non-regression: WITHOUT precomputed_query_result, own generation call still happens (backward compatible)",
    generation_call_count["n"] == 1,
    f"got {generation_call_count['n']} calls",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
