"""
agent/shared_fetch_regression_test.py — P0 regression (cross-claim
document fetch dedup, performance architecture pass).

FUNDAMENTAL INVARIANT under test: COMPUTATION MAY BE SHARED. EPISTEMIC
OWNERSHIP MUST NOT BE SHARED IMPLICITLY. Two claims requesting the same
URL must trigger exactly ONE physical HTTP fetch, but each claim must
still receive its OWN evidence record (retrieval_claim_id preserved per
claim) — the cache must never collapse "claim A found document X
relevant" into "X is therefore evidence for claim B too".

Run: /home/iam/venv/bin/python3 -m agent.shared_fetch_regression_test
"""

import threading
import time

from agent.orch_web_scraper import SharedFetchCache

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


# ── 1. Same URL, two callers -> exactly ONE network fetch ──

call_count = {"n": 0}
call_lock = threading.Lock()


def fake_fetch(url):
    with call_lock:
        call_count["n"] += 1
    time.sleep(0.05)
    return {"url": url, "title": "T", "text": "hello world " * 10, "content": "x"}, ""


cache = SharedFetchCache()
result_a = cache.get_or_fetch("https://example.com/page", "direct", fake_fetch)
result_b = cache.get_or_fetch("https://example.com/page", "direct", fake_fetch)

check(
    "same claim/URL fetched twice -> HTTP called exactly once (basic cache hit)",
    call_count["n"] == 1,
    f"got {call_count['n']} calls",
)
check("second call returns identical cached result", result_a == result_b)

# ── 2. Concurrent same URL from two threads -> exactly ONE fetch (race safety) ──

call_count2 = {"n": 0}
call_lock2 = threading.Lock()
started_event = threading.Event()


def fake_fetch_slow(url):
    with call_lock2:
        call_count2["n"] += 1
    started_event.set()
    time.sleep(0.2)  # long enough that a second thread's request would race in
    return {"url": url, "title": "T", "text": "concurrent content", "content": "x"}, ""


cache2 = SharedFetchCache()
results = []


def worker(claim_id):
    r = cache2.get_or_fetch("https://example.com/shared", "direct", fake_fetch_slow)
    results.append((claim_id, r))


threads = [threading.Thread(target=worker, args=(cid,)) for cid in ("A", "B", "C")]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=5)

check(
    "3 concurrent threads, same URL -> HTTP called exactly once (thread-safe in-flight dedup)",
    call_count2["n"] == 1,
    f"got {call_count2['n']} calls",
)
check(
    "all 3 concurrent callers receive the SAME fetched result",
    len(results) == 3 and all(r[1] == results[0][1] for r in results),
)
stats2 = cache2.summary()
check(
    "inflight_waits recorded for the 2 threads that waited on the first fetch",
    stats2["inflight_waits"] == 2,
    f"got inflight_waits={stats2['inflight_waits']}",
)

# ── 3. Different URLs -> independent fetches, no false sharing ──

cache3 = SharedFetchCache()
call_log3 = []


def fake_fetch_track(url):
    call_log3.append(url)
    return {"url": url, "title": "T", "text": f"content for {url}", "content": "x"}, ""


r1 = cache3.get_or_fetch("https://a.com/1", "direct", fake_fetch_track)
r2 = cache3.get_or_fetch("https://a.com/2", "direct", fake_fetch_track)

check("different URLs -> two separate fetches", len(call_log3) == 2)
check("different URLs -> different results, not conflated", r1 != r2)

# ── 4. Fetch error is cached too (avoid hammering a server that's already down) ──

cache4 = SharedFetchCache()
error_call_count = {"n": 0}


def fake_fetch_error(url):
    error_call_count["n"] += 1
    return None, "timeout"


r_err1 = cache4.get_or_fetch("https://broken.example/page", "direct", fake_fetch_error)
r_err2 = cache4.get_or_fetch("https://broken.example/page", "direct", fake_fetch_error)

check(
    "fetch error result cached -> second call does NOT re-fetch",
    error_call_count["n"] == 1,
    f"got {error_call_count['n']} calls",
)
check("cached error result preserves the reason", r_err1 == (None, "timeout") and r_err2 == (None, "timeout"))

# ── 5. Same URL, different transport (direct vs proxy) -> independent cache entries ──

cache5 = SharedFetchCache()
transport_calls = []


def fake_fetch_by_transport(url):
    transport_calls.append(url)
    return {"url": url, "title": "T", "text": "content", "content": "x"}, ""


cache5.get_or_fetch("https://x.com/page", "direct", fake_fetch_by_transport)
cache5.get_or_fetch("https://x.com/page", "proxy", fake_fetch_by_transport)

check(
    "same URL via direct AND proxy transport -> TWO fetches (different transport = different outcome possible)",
    len(transport_calls) == 2,
    f"got {len(transport_calls)} calls",
)

# ── 6. URL canonicalization: fragment stripped, query string preserved ──

check(
    "canonicalize strips fragment",
    SharedFetchCache.canonicalize("https://a.com/page#section") == SharedFetchCache.canonicalize("https://a.com/page"),
)
check(
    "canonicalize lowercases host",
    SharedFetchCache.canonicalize("https://EXAMPLE.com/page") == SharedFetchCache.canonicalize("https://example.com/page"),
)
check(
    "canonicalize does NOT strip query params (task explicitly forbids blind stripping)",
    SharedFetchCache.canonicalize("https://a.com/page?x=1") != SharedFetchCache.canonicalize("https://a.com/page?x=2"),
)

# ── 7. Malformed/empty content still cached correctly (no crash) ──

cache7 = SharedFetchCache()


def fake_fetch_empty(url):
    return None, "no_content"


r_empty1 = cache7.get_or_fetch("https://empty.example/page", "direct", fake_fetch_empty)
r_empty2 = cache7.get_or_fetch("https://empty.example/page", "direct", fake_fetch_empty)
check("empty/malformed content result handled without crash", r_empty1 == r_empty2 == (None, "no_content"))

# ── 8. summary() stats sanity ──

cache8 = SharedFetchCache()


def fake_fetch_8(url):
    return {"url": url, "title": "T", "text": "x", "content": "x"}, ""


cache8.get_or_fetch("https://s.com/1", "direct", fake_fetch_8)
cache8.get_or_fetch("https://s.com/1", "direct", fake_fetch_8)  # hit
cache8.get_or_fetch("https://s.com/2", "direct", fake_fetch_8)  # miss

stats8 = cache8.summary()
check("summary: requests counted correctly", stats8["requests"] == 3, f"{stats8}")
check("summary: network_fetches counted correctly (2 unique URLs)", stats8["network_fetches"] == 2, f"{stats8}")
check("summary: hits counted correctly (1 cache hit)", stats8["hits"] == 1, f"{stats8}")
check("summary: hit_ratio computed correctly", abs(stats8["hit_ratio"] - (1 / 3)) < 1e-6, f"{stats8}")

# ============================================================
# 9. END-TO-END: retrieve_for_claims-shaped scenario — two claims,
#    overlapping URL discovery, via retrieve_claim_evidence() with a
#    shared cache, mocking scrape()'s network layer.
# ============================================================

from unittest.mock import patch, MagicMock
import agent.claim_evidence_retriever as cer

shared_cache = SharedFetchCache()
fetch_calls_e2e = []


class FakeSnippet:
    def __init__(self, url, text):
        self.url = url
        self.title = "Jupiter facts"
        self.text = text
        self.content = text


def fake_scrape(direct_query, counter_query, fetch_cache=None, claim_id=""):
    # Simulate: this claim's search discovered a URL that ANOTHER
    # claim will also discover, and go through the SAME shared cache
    # that retrieve_for_claims would have created.
    url = "https://shared.example/jupiter"

    def real_fetch(u):
        fetch_calls_e2e.append(u)
        return {"url": u, "title": "Jupiter facts", "text": "Юпитер является газовым гигантом. " * 5, "content": "x"}, ""

    result, reason = fetch_cache.get_or_fetch(url, "direct", real_fetch)
    snippets = [FakeSnippet(result["url"], result["text"])] if result else []
    fake_result = MagicMock()
    fake_result.snippets = snippets
    return fake_result


with patch.object(cer, "scrape_budgeted", fake_scrape):
    with patch.object(cer, "formulate_claim_evidence_queries", return_value=MagicMock(queries=["jupiter test query"])):
        with patch.object(cer, "extract_claim_from_source", return_value="Юпитер является газовым гигантом."):
            with patch.object(cer, "_subject_anchor_matches", return_value=(True, ["title"])):
                with patch.object(cer, "is_relevant", return_value=True):
                    with patch.object(cer, "evaluate_source_quality") as mock_q:
                        mock_q.return_value = MagicMock(
                            quality_score=0.9, source_class="reference", evidence_eligible=True,
                            evidence_role="direct", authority=0.8, traceability=0.8, primaryness=0.8, reasons=[],
                        )
                        claim_a = {"claim_id": "cl_A", "claim_text": "claim A about Jupiter"}
                        claim_b = {"claim_id": "cl_B", "claim_text": "claim B about Jupiter"}

                        records_a = cer.retrieve_claim_evidence(claim_a, fetch_cache=shared_cache)
                        records_b = cer.retrieve_claim_evidence(claim_b, fetch_cache=shared_cache)

check(
    "end-to-end: two claims discovering the SAME URL -> exactly 1 physical fetch",
    len(fetch_calls_e2e) == 1,
    f"got {len(fetch_calls_e2e)} fetches: {fetch_calls_e2e}",
)
check(
    "end-to-end: claim A still gets its OWN evidence record",
    len(records_a) == 1 and records_a[0]["retrieval_claim_id"] == "cl_A",
    f"records_a={records_a}",
)
check(
    "end-to-end: claim B still gets its OWN evidence record (not skipped just because A already 'used' this URL)",
    len(records_b) == 1 and records_b[0]["retrieval_claim_id"] == "cl_B",
    f"records_b={records_b}",
)
check(
    "end-to-end: both evidence records have distinct evidence_id (not literally the same object/ownership)",
    records_a[0]["evidence_id"] != records_b[0]["evidence_id"],
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
