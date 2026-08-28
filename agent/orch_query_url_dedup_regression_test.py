"""
agent/orch_query_url_dedup_regression_test.py — regression for P1-B
(YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md, PHASE 3/4/5 follow-up):

Phase 3 (NEW): request-scoped search-QUERY dedup, added to
SharedFetchCache.get_or_search() (agent/orch_web_scraper.py) — two
different claims (or the initial/refutation/claim-specific search
stages) generating the EXACT SAME normalized query text now only pay
for the DDGS network call once. Exact-text match only, never fuzzy/
semantic — must never conflate a support-intent query with a
contradiction-intent query.

Phase 4/5 (PROVING pre-existing behavior, not new code): SharedFetchCache
already deduped URL fetches and coalesced in-flight requests before this
task (confirmed by reading the code and by
agent/refutation_performance_regression_test.py's existing "two
concurrent phases" test at the raw cache level). This file adds the
INTEGRATION-level proof the task explicitly asked for: when claim A and
claim B (two different claims, via retrieve_claim_evidence()) both
independently discover the same URL, there is exactly ONE real network
fetch, AND both claims still receive their own attributed
evidence/provenance record (retrieval_claim_id correctly set per claim,
not lost or merged).

Run: /home/iam/venv/bin/python3 -m agent.orch_query_url_dedup_regression_test
"""
from __future__ import annotations

from unittest.mock import patch

import agent.orch_web_scraper as ows
import agent.claim_evidence_retriever as cer
from agent.orch_schemas import WebQueryResult
from agent.orch_web_scraper import SharedFetchCache, scrape

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
# PHASE 3 — query dedup
# ============================================================

# ── exact duplicate (after whitespace/case normalization) -> ONE search ──
search_calls = []


def _fake_ddgs(query, max_results=10, fetch_cache=None):
    if fetch_cache is not None:
        return ows.SharedFetchCache.get_or_search(
            fetch_cache, query,
            lambda q: _fake_ddgs(q, max_results=max_results, fetch_cache=None),
        )
    search_calls.append(query)
    return ([f"https://example.test/{len(search_calls)}"], [])


with patch.object(ows, "_search_with_ddgs", _fake_ddgs):
    cache = SharedFetchCache()
    wq1 = WebQueryResult(queries=["IARC coffee cancer classification"])
    wq2 = WebQueryResult(queries=["  iarc   COFFEE cancer classification  "])  # same, different case/whitespace

    with patch.object(ows, "_fetch_url", lambda u, q="": (None, "no_content")):
        scrape(wq1, fetch_cache=cache)
        scrape(wq2, fetch_cache=cache)

check(
    "exact-duplicate query (case/whitespace-normalized) across two "
    "separate scrape() calls -> the search engine is hit only ONCE",
    len(search_calls) == 1,
    f"search_calls={search_calls}",
)
check(
    "SharedFetchCache reports the query cache hit",
    cache.query_hits == 1,
    f"query_hits={cache.query_hits} query_requests={cache.query_requests}",
)

# ── different query text -> NOT deduped, even if similar topic ──
search_calls.clear()
with patch.object(ows, "_search_with_ddgs", _fake_ddgs):
    cache2 = SharedFetchCache()
    support_q = WebQueryResult(queries=["IARC coffee classified as carcinogen evidence"])
    counter_q = WebQueryResult(queries=["IARC coffee NOT classified as carcinogen evidence"])

    with patch.object(ows, "_fetch_url", lambda u, q="": (None, "no_content")):
        scrape(support_q, fetch_cache=cache2)
        scrape(counter_q, fetch_cache=cache2)

check(
    "a support-intent query and a differently-worded counter-intent "
    "query are NOT merged by dedup, even though topically similar - "
    "only byte-identical (post-normalization) text collapses",
    len(search_calls) == 2 and cache2.query_hits == 0,
    f"search_calls={search_calls} query_hits={cache2.query_hits}",
)

# ── normalize_query: whitespace/case collapse, but real content differs ──
check(
    "normalize_query collapses whitespace runs and case",
    SharedFetchCache.normalize_query("  Coffee   Cancer  ") == SharedFetchCache.normalize_query("coffee cancer"),
)
check(
    "normalize_query does NOT collapse genuinely different query text",
    SharedFetchCache.normalize_query("coffee causes cancer")
    != SharedFetchCache.normalize_query("coffee does not cause cancer"),
)


# ============================================================
# PHASE 4/5 — URL fetch dedup + provenance, across two DIFFERENT claims
# ============================================================

fetch_calls = []


def _fake_fetch_url(url, query=""):
    fetch_calls.append(url)
    return (
        {
            "url": url,
            "title": "IARC Coffee Classification Report",
            "content": "IARC classified very hot beverages as a possible carcinogen. " * 5,
            "text": "IARC classified very hot beverages as a possible carcinogen. " * 5,
        },
        "",
    )


def _fake_ddgs_shared_url(query, max_results=10, fetch_cache=None):
    if fetch_cache is not None:
        return ows.SharedFetchCache.get_or_search(
            fetch_cache, query,
            lambda q: _fake_ddgs_shared_url(q, max_results=max_results, fetch_cache=None),
        )
    # Two DIFFERENT claims' queries independently "discover" the SAME URL.
    return (["https://iarc.example/report"], [])


claim_a = {"claim_id": "claim_A", "claim_text": "IARC classified very hot beverages as a possible carcinogen."}
claim_b = {"claim_id": "claim_B", "claim_text": "IARC classified very hot beverages as a possible carcinogen too."}

shared_cache = SharedFetchCache()

with patch.object(ows, "_search_with_ddgs", _fake_ddgs_shared_url), \
     patch.object(ows, "_fetch_url", _fake_fetch_url), \
     patch.object(cer, "is_relevant", lambda passage, claim, threshold=0.4: True), \
     patch.object(cer, "_subject_anchor_matches", lambda *a, **k: (True, ["passage"])):

    evidence_a = cer.retrieve_claim_evidence(
        claim_a,
        fetch_cache=shared_cache,
        precomputed_query_result=WebQueryResult(queries=["IARC very hot beverages carcinogen classification"]),
    )
    evidence_b = cer.retrieve_claim_evidence(
        claim_b,
        fetch_cache=shared_cache,
        # Different query TEXT from claim A's (so query-dedup, tested
        # above, is not what's making these two share a fetch) but
        # still keyword-overlapping with the fetched content, matching
        # how a real query for a real claim about the same topic would
        # look — scrape() has its own internal query<->content keyword
        # relevance filter (separate from the claim-level gates mocked
        # above), and an unrelated random string would legitimately be
        # filtered there, which is not what this test is exercising.
        precomputed_query_result=WebQueryResult(queries=["IARC very hot beverages carcinogen report findings"]),
    )

check(
    "claim A and claim B independently discover the SAME URL -> exactly "
    "ONE real network fetch happens (SharedFetchCache dedup), not two",
    len(fetch_calls) == 1,
    f"fetch_calls={fetch_calls}",
)
check(
    "claim A still receives its own evidence record for that URL "
    "(fetch dedup does not mean evidence is lost for either claim)",
    len(evidence_a) == 1 and evidence_a[0]["source_uri"] == "https://iarc.example/report",
    f"evidence_a={evidence_a}",
)
check(
    "claim B ALSO still receives its own evidence record for the same URL",
    len(evidence_b) == 1 and evidence_b[0]["source_uri"] == "https://iarc.example/report",
    f"evidence_b={evidence_b}",
)
check(
    "provenance preserved per-claim: claim A's evidence record is "
    "attributed to claim A, not claim B or unattributed",
    evidence_a[0]["retrieval_claim_id"] == "claim_A",
    f"retrieval_claim_id={evidence_a[0].get('retrieval_claim_id')!r}",
)
check(
    "provenance preserved per-claim: claim B's evidence record is "
    "attributed to claim B independently",
    evidence_b[0]["retrieval_claim_id"] == "claim_B",
    f"retrieval_claim_id={evidence_b[0].get('retrieval_claim_id')!r}",
)
check(
    "both claims' evidence records are distinct objects with distinct "
    "evidence_ids (not the same dict silently shared/aliased)",
    evidence_a[0]["evidence_id"] != evidence_b[0]["evidence_id"],
    f"a={evidence_a[0]['evidence_id']} b={evidence_b[0]['evidence_id']}",
)
check(
    "the shared fetch cache's own counters confirm one owner fetch and "
    "one hit (2 requests, 1 network_fetch, 1 saved)",
    shared_cache.requests == 2 and shared_cache.network_fetches == 1 and shared_cache.hits == 1,
    f"requests={shared_cache.requests} network_fetches={shared_cache.network_fetches} hits={shared_cache.hits}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
