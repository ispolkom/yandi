"""
agent/orch_web_budget_regression_test.py — regression for P4 (web budget
3+3): hard, structurally-independent per-side NETWORK FETCH budgets for
both claim-specific (PASS2) and question-scope (stage 6) web retrieval
(agent/orch_web_scraper.py: scrape_budgeted, scrape_budgeted_side,
_budgeted_side_candidates, _fetch_budgeted_tagged_urls).

Product decision (explicit, from the session): "3+3" is a hard FETCH
CEILING per cycle, never "search until 3 independent sources are found."
MAIN/DIRECT and COUNTER budgets are structurally independent — exhausting
one side's budget can never block or affect the other. Exact-URL dedup and
permanent-stoplist exclusion happen BEFORE the budget is spent, so neither
consumes a slot. No "reserve"/top-up search is ever issued to compensate
for later-discovered duplicates or transport failures within one cycle.

CRITICAL: registry/transport_memory.json is REAL, accumulated runtime
state — every test here that touches the stoplist patches
transport_memory.REGISTRY_DIR/MEMORY_FILE to an isolated temp path so
nothing here ever touches the real file (same pattern as
orch_stoplist_regression_test.py, learned from a real leak incident in
this session).

Run: /home/iam/venv/bin/python3 -m agent.orch_web_budget_regression_test
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.transport_memory as tm
import agent.orch_web_scraper as ows
from agent.orch_web_scraper import (
    SharedFetchCache,
    scrape_budgeted,
    scrape_budgeted_side,
    _budgeted_side_candidates,
    PASS2_DIRECT_BUDGET,
    PASS2_COUNTER_BUDGET,
    STAGE6_MAIN_BUDGET,
    STAGE6_COUNTER_BUDGET,
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


def _isolated_memory():
    d = tempfile.mkdtemp(prefix="yandi_web_budget_test_")
    return Path(d), Path(d) / "transport_memory.json"


def _fake_fetch_ok(url, query=""):
    return {"url": url, "title": "t", "text": f"content for {url} " * 5, "content": f"content for {url}"}, ""


# ============================================================
# 1/2/3. PASS2 (scrape_budgeted): direct<=3, counter<=3, total<=6
# ============================================================

tmp_dir1, tmp_file1 = _isolated_memory()

_ddgs_calls_1 = []


def _fake_ddgs_1(query, max_results=10, fetch_cache=None):
    _ddgs_calls_1.append(query)
    if "direct" in query:
        return [f"https://direct{i}.example/x" for i in range(5)], []
    return [f"https://counter{i}.example/x" for i in range(5)], []


with patch.object(tm, "REGISTRY_DIR", tmp_dir1), patch.object(tm, "MEMORY_FILE", tmp_file1), \
     patch.object(ows, "_search_with_ddgs", _fake_ddgs_1), \
     patch.object(ows, "_fetch_url", _fake_fetch_ok), \
     patch.object(ows, "_load_proxy_url", lambda: None):

    result_pass2 = scrape_budgeted(
        "direct query about jupiter", "counter query about jupiter",
        fetch_cache=SharedFetchCache(), claim_id="cl_budget_test",
    )

    direct_snips = [s for s in result_pass2.snippets if s.origin == "direct"]
    counter_snips = [s for s in result_pass2.snippets if s.origin == "counter"]

    check(
        "PASS2: direct fetched <= PASS2_DIRECT_BUDGET (3), even though "
        "5 candidates were discovered",
        len(direct_snips) <= PASS2_DIRECT_BUDGET,
        f"got {len(direct_snips)} direct snippets",
    )
    check(
        "PASS2: counter fetched <= PASS2_COUNTER_BUDGET (3), even though "
        "5 candidates were discovered",
        len(counter_snips) <= PASS2_COUNTER_BUDGET,
        f"got {len(counter_snips)} counter snippets",
    )
    check(
        "PASS2: total fetched (direct+counter) <= 6",
        len(result_pass2.snippets) <= 6,
        f"got {len(result_pass2.snippets)} total snippets",
    )

# ============================================================
# 4. Stage 6 (scrape_budgeted_side): main<=3, counter<=3
# ============================================================

tmp_dir2, tmp_file2 = _isolated_memory()


def _fake_ddgs_many(query, max_results=10, fetch_cache=None):
    return [f"https://s6-{query.replace(' ', '_')}-{i}.example/x" for i in range(6)], []


with patch.object(tm, "REGISTRY_DIR", tmp_dir2), patch.object(tm, "MEMORY_FILE", tmp_file2), \
     patch.object(ows, "_search_with_ddgs", _fake_ddgs_many), \
     patch.object(ows, "_fetch_url", _fake_fetch_ok), \
     patch.object(ows, "_load_proxy_url", lambda: None):

    result_main = scrape_budgeted_side(
        ["main sub-query one", "main sub-query two", "main sub-query three"],
        STAGE6_MAIN_BUDGET, fetch_cache=SharedFetchCache(), side="main", scope="initial",
    )
    result_counter = scrape_budgeted_side(
        ["refutation query one", "refutation query two"],
        STAGE6_COUNTER_BUDGET, fetch_cache=SharedFetchCache(), side="counter", scope="initial",
    )

    check(
        "Stage 6: main side fetched <= STAGE6_MAIN_BUDGET (3), even with "
        "3 sub-queries each discovering 6 URLs",
        len(result_main.snippets) <= STAGE6_MAIN_BUDGET,
        f"got {len(result_main.snippets)}",
    )
    check(
        "Stage 6: counter/refutation side fetched <= STAGE6_COUNTER_BUDGET (3)",
        len(result_counter.snippets) <= STAGE6_COUNTER_BUDGET,
        f"got {len(result_counter.snippets)}",
    )

# ============================================================
# 5. Exact duplicate URL does not consume a second budget slot
# ============================================================

tmp_dir3, tmp_file3 = _isolated_memory()


def _fake_ddgs_dupe(query, max_results=10, fetch_cache=None):
    # Same URL twice (fragment-only difference — canonicalize() drops
    # fragments), plus one genuinely different URL.
    return [
        "https://dup.example/page#section1",
        "https://dup.example/page#section2",
        "https://distinct.example/other",
    ], []


with patch.object(tm, "REGISTRY_DIR", tmp_dir3), patch.object(tm, "MEMORY_FILE", tmp_file3), \
     patch.object(ows, "_search_with_ddgs", _fake_ddgs_dupe):

    candidates, discovered, stoplist_excluded, _processed_excluded = _budgeted_side_candidates(
        ["dupe test query"], 3, SharedFetchCache(), "direct",
    )

    check(
        "exact duplicate URL (fragment-only variant) collapses to ONE "
        "candidate, not two — dedup happens before budget is spent",
        len(candidates) == 2 and discovered == 3,
        f"candidates={candidates} discovered={discovered}",
    )

# ============================================================
# 6. Stoplisted URL does not consume a budget slot
# ============================================================

tmp_dir4, tmp_file4 = _isolated_memory()

with patch.object(tm, "REGISTRY_DIR", tmp_dir4), patch.object(tm, "MEMORY_FILE", tmp_file4):
    tm.stoplist_url("https://banned.example/x", "http_403", "proxy_http_403")

    def _fake_ddgs_stoplisted(query, max_results=10, fetch_cache=None):
        return [
            "https://banned.example/x",
            "https://ok1.example/x",
            "https://ok2.example/x",
        ], []

    with patch.object(ows, "_search_with_ddgs", _fake_ddgs_stoplisted):
        candidates2, discovered2, stoplist_excluded2, _processed_excluded2 = _budgeted_side_candidates(
            ["stoplist test query"], 3, SharedFetchCache(), "direct",
        )

    check(
        "stoplisted URL is excluded before budget selection — never "
        "occupies one of the 3 slots",
        "https://banned.example/x" not in candidates2
        and len(candidates2) == 2
        and stoplist_excluded2 == 1,
        f"candidates={candidates2} stoplist_excluded={stoplist_excluded2}",
    )

# ============================================================
# 7/8. Main/direct saturation never blocks counter, and vice versa
# ============================================================

tmp_dir5, tmp_file5 = _isolated_memory()

_ddgs_calls_78 = {"direct": 0, "counter": 0}


def _fake_ddgs_78(query, max_results=10, fetch_cache=None):
    if "direct" in query:
        _ddgs_calls_78["direct"] += 1
        # Direct side discovers WAY more than budget — old scrape()'s
        # shared discovery loop would break here and potentially never
        # search the counter query at all.
        return [f"https://direct-heavy-{i}.example/x" for i in range(20)], []
    _ddgs_calls_78["counter"] += 1
    return [f"https://counter-normal-{i}.example/x" for i in range(2)], []


with patch.object(tm, "REGISTRY_DIR", tmp_dir5), patch.object(tm, "MEMORY_FILE", tmp_file5), \
     patch.object(ows, "_search_with_ddgs", _fake_ddgs_78), \
     patch.object(ows, "_fetch_url", _fake_fetch_ok), \
     patch.object(ows, "_load_proxy_url", lambda: None):

    result_78 = scrape_budgeted(
        "direct heavy query", "counter normal query",
        fetch_cache=SharedFetchCache(), claim_id="cl_78",
    )

    counter_snips_78 = [s for s in result_78.snippets if s.origin == "counter"]
    direct_snips_78 = [s for s in result_78.snippets if s.origin == "direct"]

    check(
        "main/direct saturation (20 candidates) does not block counter "
        "retrieval — counter query still ran and produced its own evidence",
        _ddgs_calls_78["counter"] == 1 and len(counter_snips_78) >= 1,
        f"counter_calls={_ddgs_calls_78['counter']} counter_snips={len(counter_snips_78)}",
    )
    check(
        "counter saturation (structurally, symmetric guarantee) does not "
        "affect direct — direct side still capped at its own budget "
        "independent of counter's candidate count",
        len(direct_snips_78) <= PASS2_DIRECT_BUDGET,
        f"got {len(direct_snips_78)}",
    )

# ============================================================
# 9. No "reserve"/top-up search: exactly ONE discovery call per side,
#    regardless of how many candidates later fail/duplicate.
# ============================================================

tmp_dir6, tmp_file6 = _isolated_memory()

_ddgs_call_count_9 = {"n": 0}


def _fake_ddgs_9(query, max_results=10, fetch_cache=None):
    _ddgs_call_count_9["n"] += 1
    return [
        "https://reprint-a.example/x",
        "https://reprint-b.example/x",
        "https://reprint-c.example/x",
    ], []


with patch.object(tm, "REGISTRY_DIR", tmp_dir6), patch.object(tm, "MEMORY_FILE", tmp_file6), \
     patch.object(ows, "_search_with_ddgs", _fake_ddgs_9), \
     patch.object(ows, "_fetch_url", _fake_fetch_ok), \
     patch.object(ows, "_load_proxy_url", lambda: None):

    result_9 = scrape_budgeted(
        "reprint scenario query", "",
        fetch_cache=SharedFetchCache(), claim_id="cl_reprint",
    )

    check(
        "reprint case: exactly 3 URLs fetched (budget), no 4th URL "
        "fetched as compensation, and no second/top-up discovery call "
        "was ever issued for this side",
        len(result_9.snippets) == 3 and _ddgs_call_count_9["n"] == 1,
        f"snippets={len(result_9.snippets)} ddgs_calls={_ddgs_call_count_9['n']}",
    )

# ============================================================
# 10. direct fail -> proxy success: exactly ONE source-budget slot
# ============================================================

tmp_dir7, tmp_file7 = _isolated_memory()


def _fake_ddgs_10(query, max_results=10, fetch_cache=None):
    return ["https://recovered-via-proxy.example/x"], []


def _fake_direct_timeout(url, query=""):
    return None, "timeout"


def _fake_proxy_ok(url, query=""):
    text = "recovered via proxy content " * 10
    return {"url": url, "title": "t", "text": text, "content": text}, ""


with patch.object(tm, "REGISTRY_DIR", tmp_dir7), patch.object(tm, "MEMORY_FILE", tmp_file7), \
     patch.object(ows, "_search_with_ddgs", _fake_ddgs_10), \
     patch.object(ows, "_fetch_url", _fake_direct_timeout), \
     patch.object(ows, "_fetch_url_proxy", _fake_proxy_ok), \
     patch.object(ows, "_load_proxy_url", lambda: "http://fake-proxy:8080"):

    result_10 = scrape_budgeted(
        "recovered content query", "",
        fetch_cache=SharedFetchCache(), claim_id="cl_proxy_recover",
    )

    check(
        "direct fail + proxy success: the URL consumes exactly ONE "
        "source-budget slot (one snippet), not two — direct+proxy "
        "together are ONE retrieval attempt against the budget",
        len(result_10.snippets) == 1
        and "recovered via proxy" in result_10.snippets[0].text,
        f"snippets={len(result_10.snippets)}",
    )
    check(
        "direct fail + proxy success: URL is NOT stoplisted (it IS "
        "reachable, just via a different transport)",
        not tm.is_stoplisted("https://recovered-via-proxy.example/x"),
    )

# ============================================================
# 11. direct+proxy fail -> stoplisted; no replacement search in cycle
# ============================================================

tmp_dir8, tmp_file8 = _isolated_memory()

_ddgs_call_count_11 = {"n": 0}


def _fake_ddgs_11(query, max_results=10, fetch_cache=None):
    _ddgs_call_count_11["n"] += 1
    return ["https://both-fail.example/x", "https://ok-sibling.example/x"], []


def _fake_direct_fail_11(url, query=""):
    if "both-fail" in url:
        return None, "http_403"
    return {"url": url, "title": "t", "text": "ok content " * 10, "content": "ok content"}, ""


def _fake_proxy_fail_11(url, query=""):
    return None, "proxy_http_403"


with patch.object(tm, "REGISTRY_DIR", tmp_dir8), patch.object(tm, "MEMORY_FILE", tmp_file8), \
     patch.object(ows, "_search_with_ddgs", _fake_ddgs_11), \
     patch.object(ows, "_fetch_url", _fake_direct_fail_11), \
     patch.object(ows, "_fetch_url_proxy", _fake_proxy_fail_11), \
     patch.object(ows, "_load_proxy_url", lambda: "http://fake-proxy:8080"):

    result_11 = scrape_budgeted(
        "both fail query", "",
        fetch_cache=SharedFetchCache(), claim_id="cl_both_fail",
    )

    check(
        "direct+proxy both fail: URL is stoplisted",
        tm.is_stoplisted("https://both-fail.example/x"),
    )
    check(
        "direct+proxy both fail: no replacement/top-up discovery call "
        "was made this cycle to compensate for the failed slot — "
        "exactly ONE _search_with_ddgs call for this side",
        _ddgs_call_count_11["n"] == 1,
        f"got {_ddgs_call_count_11['n']} calls",
    )
    check(
        "direct+proxy both fail: the sibling URL (independent candidate) "
        "was still fetched normally — one URL's failure doesn't sink "
        "the other candidates in the same budget",
        len(result_11.snippets) == 1
        and result_11.snippets[0].url == "https://ok-sibling.example/x",
        f"snippets={[s.url for s in result_11.snippets]}",
    )

# ============================================================
# 12. P1-A: a resolved claim does not get PASS2 retrieval
# ============================================================

import agent.orchestrator.claims.retrieval as claims_retrieval

_retrieve_for_claims_calls = []


def _fake_retrieve_for_claims(claims, fetch_cache=None):
    _retrieve_for_claims_calls.extend(c.get("claim_id") for c in claims)
    return []


resolved_claim = {
    "claim_id": "cl_resolved",
    "verification_status": "candidate",
    "evidence_relations": [
        {"evidence_role": "direct", "evidence_eligible": True, "relation": "supports"},
    ],
}
unresolved_claim = {
    "claim_id": "cl_unresolved",
    "verification_status": "candidate",
    "evidence_relations": [],
}

with patch.object(claims_retrieval, "retrieve_for_claims", _fake_retrieve_for_claims):
    claims_data_12 = [dict(resolved_claim), dict(unresolved_claim)]
    claims_retrieval.apply_claim_resolution_and_second_retrieval(
        claims_data_12,
        [],
        True,   # enable_web
        False,  # is_subjective_answer
        False,  # skip_rag
        None,   # request_fetch_cache
        {},     # cost
        lambda *a, **kw: None,  # log
        False,  # verbose
    )

check(
    "P1-A: a claim with effective direct evidence (resolved) is NOT "
    "included in the PASS2 retrieve_for_claims() call",
    "cl_resolved" not in _retrieve_for_claims_calls,
    f"calls={_retrieve_for_claims_calls}",
)
check(
    "P1-A: an unresolved claim IS included in the PASS2 "
    "retrieve_for_claims() call",
    "cl_unresolved" in _retrieve_for_claims_calls,
    f"calls={_retrieve_for_claims_calls}",
)

# ============================================================
# 13. MAX_CLAIM_WORKERS <= 3 (untouched invariant)
# ============================================================

from agent.orchestrator.claims.async_pipeline import MAX_CLAIM_WORKERS

check(
    "MAX_CLAIM_WORKERS <= 3 (unchanged by this patch)",
    MAX_CLAIM_WORKERS <= 3,
    f"got {MAX_CLAIM_WORKERS}",
)

# ============================================================
# 14. NLI concurrency == 1 (structural: exactly one consumer task
#     spawned per run_claims_async invocation — unchanged by this patch)
# ============================================================

import inspect
import agent.orchestrator.claims.async_pipeline as async_pipeline_mod

_src_14 = inspect.getsource(async_pipeline_mod)
_consumer_task_spawns = _src_14.count("asyncio.create_task(nli_batcher.run_until(")

check(
    "NLI concurrency == 1: exactly one asyncio.create_task(nli_batcher."
    "run_until(...)) call site — a single controlled NLI consumer, "
    "unchanged by this patch",
    _consumer_task_spawns == 1,
    f"got {_consumer_task_spawns} call sites",
)

# ============================================================
# 15. canonical Trust semantics unchanged (fixed-input smoke test)
# ============================================================

from agent.orchestrator.epistemic.canonical_trust import compute_canonical_trust

_ct_same = compute_canonical_trust("VERIFIED", "VERIFIED", lambda *a, **kw: None, False)
_ct_diverge = compute_canonical_trust("VERIFIED", "UNVERIFIED", lambda *a, **kw: None, False)
_ct_neither = compute_canonical_trust(None, None, lambda *a, **kw: None, False)

check(
    "canonical Trust: both strands agree -> canonical == that value, "
    "diverged=False (unchanged semantics)",
    _ct_same["canonical_trust"] == "VERIFIED" and _ct_same["diverged"] is False,
    f"{_ct_same}",
)
check(
    "canonical Trust: strands disagree -> diverged=True, canonical "
    "capped to the stricter/weaker strand, not the looser one",
    _ct_diverge["diverged"] is True and _ct_diverge["canonical_trust"] != "VERIFIED",
    f"{_ct_diverge}",
)
check(
    "canonical Trust: neither strand available -> UNVERIFIED fail-safe "
    "default (unchanged)",
    _ct_neither["canonical_trust"] == "UNVERIFIED" and _ct_neither["diverged"] is False,
    f"{_ct_neither}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
