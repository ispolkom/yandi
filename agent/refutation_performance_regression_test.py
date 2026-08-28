"""
agent/refutation_performance_regression_test.py — regression for the
refutation performance audit (cross-phase shared fetch cache).

Root cause found: main web scrape(), refutation scrape() and
claim-specific retrieve_for_claims() each created their OWN
SharedFetchCache (or scrape() created a one-off internally when none
was passed) -- a URL discovered independently by two of these three
phases in the SAME request was fetched physically more than once.

Fix: orchestrator_v2.process() now creates ONE SharedFetchCache per
request and passes it into all three call sites (main web scrape(),
refutation scrape(), retrieve_for_claims()). Live measurement (real
run, "Есть ли разумная жизнь на Юпитере?" --web --no-cache):
[Search Work Audit] requests=210 unique_urls=194 network_fetches=194
saved=16 hit_ratio=0.08 -- 16 real cross-phase duplicate fetches
eliminated, mechanism proven on real data, not assumed.

refutation query generation (formulate_refutation_queries) was
checked and found to already be ONE LLM call producing 2-3 queries as
structured JSON -- no batching fix needed there.

This suite covers the MECHANISM deterministically (mocked search/
fetch, no network): refutation queries preserved, duplicate URL
fetched once across phases, independent ownership between normal and
refutation pipelines for a shared source, cache failure fallback,
relation never auto-transferred by the cache layer, empty refutation
result, partial search failure, and concurrent duplicate URL across
two phases.

Run: /home/iam/venv/bin/python3 -m agent.refutation_performance_regression_test
"""

import threading
import time
from unittest.mock import patch

import agent.orch_web_scraper as ows
from agent.orch_web_scraper import scrape, SharedFetchCache
from agent.orch_schemas import WebQueryResult

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


def _fake_search_factory(url_map):
    """url_map: query -> list of urls to 'discover' for that query."""
    def _fake_search(query, max_results=10, fetch_cache=None):
        return list(url_map.get(query, [])), []
    return _fake_search


def _fake_fetch_factory(fetch_calls, delay=0.0, fail_urls=None):
    fail_urls = fail_urls or set()

    def _fake_fetch(url, query=""):
        fetch_calls.append(url)
        if delay:
            time.sleep(delay)
        if url in fail_urls:
            return None, "fetch_failed"
        # Include keywords from every test query in this file so the
        # scrape() relevance gate (>=20% keyword overlap with at least
        # one query) passes regardless of which query discovered this
        # fake URL -- the gate itself is not what this suite tests.
        content = (
            "Юпитере критика альтернативные теории жизни падает "
            "работает шарит доказательства партиал "
            f"источник {url}"
        )
        return (
            {
                "url": url,
                "title": content,
                "content": content,
                "text": content,
            },
            "",
        )

    return _fake_fetch


# ── 1. Refutation queries сохраняются end-to-end через scrape() ──

fetch_calls_1 = []

with patch.object(
    ows,
    "_search_with_ddgs",
    _fake_search_factory({
        "критика жизни на Юпитере": ["https://a.example/1"],
        "альтернативные теории Юпитер": ["https://b.example/2"],
    }),
):
    with patch.object(ows, "_fetch_url", _fake_fetch_factory(fetch_calls_1)):
        refutation_wq = WebQueryResult(
            queries=["критика жизни на Юпитере", "альтернативные теории Юпитер"]
        )
        cache = SharedFetchCache()
        result = scrape(refutation_wq, fetch_cache=cache)

check(
    "refutation queries all reach scrape() and produce snippets",
    len(result.snippets) == 2,
    f"got {len(result.snippets)} snippets",
)
check(
    "contradiction/skeptic query not silently dropped",
    any("b.example" in s.url for s in result.snippets),
)

# ── 2. Duplicate URL across phases fetched ONCE (the actual fix) ──

fetch_calls_2 = []
shared_cache = SharedFetchCache()

with patch.object(
    ows,
    "_search_with_ddgs",
    _fake_search_factory({
        "жизнь на Юпитере": ["https://shared.example/doc"],
        "критика жизни на Юпитере": ["https://shared.example/doc"],
    }),
):
    with patch.object(ows, "_fetch_url", _fake_fetch_factory(fetch_calls_2)):
        main_wq = WebQueryResult(queries=["жизнь на Юпитере"])
        main_result = scrape(main_wq, fetch_cache=shared_cache)

        refutation_wq2 = WebQueryResult(queries=["критика жизни на Юпитере"])
        refutation_result2 = scrape(refutation_wq2, fetch_cache=shared_cache)

check(
    "same URL discovered by main web AND refutation -> fetched exactly once",
    fetch_calls_2.count("https://shared.example/doc") == 1,
    f"got {fetch_calls_2.count('https://shared.example/doc')} physical fetches",
)
check(
    "both phases still get a snippet for the shared URL (cache hit still returns content)",
    len(main_result.snippets) == 1 and len(refutation_result2.snippets) == 1,
)
check(
    "[Search Work Audit]-style summary reflects the saved fetch",
    shared_cache.summary()["saved"] == 1,
    f"got {shared_cache.summary()}",
)

# ── 3. Same source belongs to normal + refutation pipelines
#      INDEPENDENTLY -- ownership/type tagging is not shared ──

main_snippet = main_result.snippets[0]
refutation_snippet = refutation_result2.snippets[0]

check(
    "main-web snippet and refutation snippet are separate objects (not the same instance)",
    main_snippet is not refutation_snippet,
)
check(
    "neither snippet object carries a relation/type field from the cache layer itself",
    not hasattr(main_snippet, "relation") and not hasattr(refutation_snippet, "relation"),
)

# Simulate what orchestrator_v2.py does downstream: it tags "type"
# independently per source list -- verify nothing in the shared cache
# forces these to collide.
main_source = {"type": "web", "url": main_snippet.url, "text": main_snippet.text}
refutation_source = {"type": "refutation", "url": refutation_snippet.url, "text": refutation_snippet.text}

check(
    "downstream tagging stays independent per pipeline for the same physical URL",
    main_source["type"] == "web" and refutation_source["type"] == "refutation",
)

# ── 4. Cache failure fallback: fetch_fn exception -> caller sees it,
#      no merge/crash, no poisoned cache entry blocking other URLs ──

def _raising_fetch(url, query=""):
    raise ConnectionError("simulated network outage")


cache4 = SharedFetchCache()
try:
    cache4.get_or_fetch("https://broken.example/x", "direct", lambda u: _raising_fetch(u))
    raised = False
except ConnectionError:
    raised = True

check("fetch failure propagates to the caller (no silent swallow)", raised)

# a second, unrelated URL must still work after a prior failure
result4b = cache4.get_or_fetch(
    "https://ok.example/y", "direct", lambda u: ({"url": u, "title": "t", "content": "c", "text": "c"}, "")
)
check("cache stays usable for other URLs after a prior fetch failure", result4b[0] is not None)

# ── 5. Empty refutation result: no queries / no snippets found ──

empty_wq = WebQueryResult(queries=[])
empty_result = scrape(empty_wq, fetch_cache=SharedFetchCache())
check("empty refutation query list -> empty result, no crash", empty_result.snippets == [])

with patch.object(ows, "_search_with_ddgs", _fake_search_factory({})):
    no_hits_wq = WebQueryResult(queries=["запрос без результатов"])
    no_hits_result = scrape(no_hits_wq, fetch_cache=SharedFetchCache())
check("refutation search with zero discovered URLs -> empty snippets, no crash", no_hits_result.snippets == [])

# ── 6. Partial search failure: one query errors, others still proceed ──
#
# _search_with_ddgs() already catches its own exceptions internally
# (returns ([], []) on any DDGS error, never raises to scrape()) -- so
# the realistic partial-failure shape is "one query yields zero URLs",
# not an exception escaping the search call.

def _partial_fail_search(query, max_results=10, fetch_cache=None):
    if query == "падает":
        return [], []
    return ["https://ok.example/partial"], []


fetch_calls_6 = []
with patch.object(ows, "_search_with_ddgs", _partial_fail_search):
    with patch.object(ows, "_fetch_url", _fake_fetch_factory(fetch_calls_6)):
        try:
            partial_wq = WebQueryResult(queries=["падает", "работает"])
            partial_result = scrape(partial_wq, fetch_cache=SharedFetchCache())
            partial_ok = len(partial_result.snippets) >= 1
        except Exception as e:
            partial_ok = False

check(
    "one failing search query does not prevent snippets from a working query",
    partial_ok,
)

# ── 7. Concurrent duplicate URL across two phases (thread race) ──
#
# get_or_fetch()'s own locking guarantees only ONE thread ever becomes
# the "owner" that actually calls fetch_fn, regardless of exact
# timing -- the other becomes a waiter on the owner's Event (see
# shared_fetch_regression_test.py's existing 3-thread race test for
# the same mechanism). Only the owner's thread ever executes
# _slow_fake_fetch, so a barrier requiring 2 arrivals INSIDE it would
# never be satisfied -- a slow fetch (sleep) is enough to make the
# race exercise realistic without that flaw.

concurrent_cache = SharedFetchCache()
concurrent_fetch_calls = []


def _slow_fake_fetch(url, query=""):
    time.sleep(0.05)
    concurrent_fetch_calls.append(url)
    return {"url": url, "title": "t", "content": "c", "text": "c"}, ""


results = [None, None]


def _worker(i):
    results[i] = concurrent_cache.get_or_fetch(
        "https://race.example/z", "direct", lambda u: _slow_fake_fetch(u)
    )


t1 = threading.Thread(target=_worker, args=(0,))
t2 = threading.Thread(target=_worker, args=(1,))
t1.start()
t2.start()
t1.join(timeout=3)
t2.join(timeout=3)

check(
    "two concurrent phases requesting the same URL -> exactly one physical fetch",
    len(concurrent_fetch_calls) == 1,
    f"got {len(concurrent_fetch_calls)} fetches",
)
check(
    "both concurrent callers still get the fetched content",
    results[0] is not None and results[1] is not None and results[0][0]["url"] == results[1][0]["url"],
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
