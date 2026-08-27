"""
agent/query_context_retrieval_regression_test.py — regression for the
query_context NameError root-cause fix in
agent/claim_evidence_retriever.py.

Root cause (see YANDI_EPISTEMIC_DEPENDENCY_REEVALUATION_REPORT.md §14
item 1, and agent.claim_evidence_retriever._resolve_query_context's own
docstring): commit 61279fe extracted the query_context resolution
precedence chain out of retrieve_claim_evidence()'s own scope into the
new _build_contextual_claim_text() helper, but retrieve_claim_evidence()
had a SECOND, independent use of the same raw value further down (the
SUBJECT ANCHOR VIEW block's `elif query_context:`), which the extraction
left referencing a name no longer bound in that scope — a NameError for
any claim whose text has no extractable subject anchor (e.g. its only
capitalized/entity-like token is the first word, which anchor extraction
deliberately skips).

Fix: a single _resolve_query_context(claim) helper, used by all three
call sites that need this value (_build_contextual_claim_text,
_claim_retrieval_priority, and the restored call in
retrieve_claim_evidence) — no duplicated logic, no masking via
try/except/getattr/default substitution, no globals/locals hack.

Only the network/embedding boundary is mocked
(extract_claim_from_source, is_relevant, scrape) — matching this
project's established pattern (e.g. epistemic_dependency_recheck_
regression_test.py). Everything else, including the real
_subject_anchor_matches() gate, runs for real.

Run: /home/iam/venv/bin/python3 -m agent.query_context_retrieval_regression_test
"""

import agent.claim_evidence_retriever as cer
from agent.claim_evidence_retriever import (
    _resolve_query_context,
    retrieve_claim_evidence,
)
from agent.orch_schemas import WebQueryResult, WebScrapeResult, WebSnippet

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


# ── 1. _resolve_query_context: precedence + stripping, single source of truth ──

check(
    "query_context takes precedence over source_query/original_query",
    _resolve_query_context({"query_context": "  A  ", "source_query": "B", "original_query": "C"}) == "A",
)
check(
    "source_query used when query_context absent",
    _resolve_query_context({"source_query": "B", "original_query": "C"}) == "B",
)
check(
    "original_query used when both query_context and source_query absent",
    _resolve_query_context({"original_query": "C"}) == "C",
)
check(
    "empty dict -> empty string, never None, never a crash",
    _resolve_query_context({}) == "",
)
check(
    "all fields blank/whitespace-only -> empty string (falsy, not a stray-whitespace context)",
    _resolve_query_context({"query_context": "   ", "source_query": "", "original_query": None}) == "",
)

# ── 2. Reproduces the OLD NameError's exact trigger condition, now fixed ──
#
# claim_text below has NO extractable subject anchor: "Форма" is the
# first word (anchor extraction deliberately skips index 0), nothing
# else is capitalized, no planetary alias matches. This is exactly the
# condition that used to hit `elif query_context:` with query_context
# unbound in retrieve_claim_evidence()'s scope.

_no_anchor_claim = {
    "claim_id": "cl_test_no_anchor",
    "claim_text": "Форма планеты является предметом обсуждения.",
    "claim_type": "factual",
    "query_context": "Известно, что Земля имеет форму.",
}

try:
    result = retrieve_claim_evidence(
        _no_anchor_claim,
        precomputed_query_result=WebQueryResult(queries=[]),  # empty -> early return, no network
    )
    check(
        "claim with no subject anchor + query_context set: no NameError, clean empty-queries return",
        result == [],
        f"{result}",
    )
except NameError as e:
    check("claim with no subject anchor + query_context set: no NameError", False, f"raised: {e}")

# Same claim, but with NO query_context at all either — must still not
# crash (the `else: subject_anchor_text = claim_text` branch).
_no_anchor_no_context_claim = {
    "claim_id": "cl_test_no_anchor_no_ctx",
    "claim_text": "Форма планеты является предметом обсуждения.",
    "claim_type": "factual",
}
try:
    result2 = retrieve_claim_evidence(
        _no_anchor_no_context_claim,
        precomputed_query_result=WebQueryResult(queries=[]),
    )
    check(
        "claim with no subject anchor AND no query_context: no NameError, falls back to claim_text",
        result2 == [],
        f"{result2}",
    )
except NameError as e:
    check("claim with no subject anchor AND no query_context: no NameError", False, f"raised: {e}")


# ── 3. query_context reaches the REAL Subject Gate consumer and actually discriminates ──

_orig_extract = cer.extract_claim_from_source
_orig_is_relevant = cer.is_relevant
_orig_scrape = cer.scrape


def _restore():
    cer.extract_claim_from_source = _orig_extract
    cer.is_relevant = _orig_is_relevant
    cer.scrape = _orig_scrape


earth_snippet = WebSnippet(
    url="https://ru.wikipedia.org/wiki/Земля",
    title="Земля — Википедия",
    content=(
        "Земля имеет форму, близкую к сфере, а не плоскую. "
        "Это подтверждено множеством независимых наблюдений, "
        "включая фотографии из космоса и измерения гравитации."
    ),
    text=(
        "Земля имеет форму, близкую к сфере, а не плоскую. "
        "Это подтверждено множеством независимых наблюдений, "
        "включая фотографии из космоса и измерения гравитации."
    ),
    relevance=0.8,
)
unrelated_snippet = WebSnippet(
    url="https://example.com/tomatoes",
    title="Выращивание помидоров на подоконнике",
    content=(
        "Помидоры любят солнечный свет и регулярный полив. "
        "Для хорошего урожая важно поддерживать тёплую температуру "
        "и вовремя подкармливать растения удобрениями."
    ),
    text=(
        "Помидоры любят солнечный свет и регулярный полив. "
        "Для хорошего урожая важно поддерживать тёплую температуру "
        "и вовремя подкармливать растения удобрениями."
    ),
    relevance=0.8,
)

cer.scrape = lambda *a, **kw: WebScrapeResult(
    snippets=[earth_snippet, unrelated_snippet],
    total_chars=100,
    urls=[earth_snippet.url, unrelated_snippet.url],
)
cer.extract_claim_from_source = lambda text, main_claim="": text[:200]
cer.is_relevant = lambda text, main_claim, threshold=0.4: True

try:
    records = retrieve_claim_evidence(
        _no_anchor_claim,
        precomputed_query_result=WebQueryResult(queries=["земля форма плоская"]),
    )
finally:
    _restore()

check(
    "query_context-derived anchor ('земля') reaches the real Subject Gate: "
    "the Earth snippet (matches via title/url) is kept, the unrelated tomato "
    "snippet is rejected — proves the correct value, not claim_text (which has "
    "no anchor of its own), drove a real discrimination decision",
    len(records) == 1 and records[0]["source_uri"] == earth_snippet.url,
    f"{[r.get('source_uri') for r in records]}",
)

# ── 4. Exception/fallback path: a genuine scrape failure still degrades gracefully ──

cer.scrape = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("network down"))
try:
    records_err = retrieve_claim_evidence(
        _no_anchor_claim,
        precomputed_query_result=WebQueryResult(queries=["земля форма плоская"]),
    )
    check(
        "a genuine scrape() exception still degrades to an empty list, not a crash "
        "(pre-existing fallback path, unaffected by this fix)",
        records_err == [],
        f"{records_err}",
    )
except Exception as e:
    check("a genuine scrape() exception still degrades to an empty list", False, f"raised: {e}")
finally:
    _restore()

# ── 5. Consolidation didn't change behavior of the two call sites that already worked ──

from agent.claim_evidence_retriever import _build_contextual_claim_text, _claim_retrieval_priority

check(
    "_build_contextual_claim_text still builds the same contextual text via the shared helper",
    _build_contextual_claim_text({"query_context": "Земля"}, "имеет форму сферы")
    == "Земля\nПроверяемое утверждение: имеет форму сферы",
)
check(
    "_build_contextual_claim_text with no context falls back to bare claim_text (unchanged)",
    _build_contextual_claim_text({}, "имеет форму сферы") == "имеет форму сферы",
)
check(
    "_claim_retrieval_priority still runs end-to-end via the shared helper, no crash",
    isinstance(_claim_retrieval_priority({"claim_text": "Юпитер имеет 95 спутников.", "query_context": "Сколько спутников у Юпитера?"}), float),
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
