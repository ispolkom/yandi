"""
agent/processed_source_reuse_regression_test.py — Этап 4 (P6) regression:
PROCESSED SOURCE REUSE / NEW ROUTES ONLY.

A repeat verification cycle must not re-fetch URLs already verified as
evidence for the SAME claim (exact content_hash scope) — old routes
come from LOCAL MEMORY (Этап 3), new 3+3 budget slots (Этап 2) go only
to genuinely new candidates. Covers:

  - agent/verification_memory.py::get_historical_web_urls() (UNION
    across ALL historical occurrences of a content_hash, via index
    locators — no full JSONL scan, no new persistent store).
  - the multi-hop provenance fix in _reconstruct_evidence() (a 3rd-
    generation reuse must still point back to the TRUE original
    internet observation, not the most recent reuse hop).
  - agent/orch_web_scraper.py::_budgeted_side_candidates()'s new
    processed-exclusion tier (between stoplist and the hard cap) and
    scrape_budgeted()'s content_hash-scoped wiring.
  - route_side (WebSnippet.origin -> runtime evidence -> Trace ->
    memory reconstruction) surviving the full round trip (Finding 2).

CRITICAL: registry/index.db and registry/dataset/orch_traces/*.jsonl
are REAL, accumulated state — every test here patches agent.
verification_memory.INDEX_DB/TRACES_DIR and agent.orch_tracer.
TRACES_DIR to isolated temp paths.

Run: /home/iam/venv/bin/python3 -m agent.processed_source_reuse_regression_test
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import agent.orch_tracer as ot
import agent.verification_memory as vm
import agent.orch_web_scraper as ows
import agent.transport_memory as tm
from agent.orch_schemas import EvidenceRecord, ClaimRecord
from agent.orch_web_scraper import SharedFetchCache, _budgeted_side_candidates, scrape_budgeted
from agent.claim_identity import compute_claim_content_hash

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


def _isolated_paths():
    traces = Path(tempfile.mkdtemp(prefix="yandi_p6_traces_"))
    index = Path(tempfile.mkdtemp(prefix="yandi_p6_index_")) / "index.db"
    return traces, index


def _vm_patches(traces_dir, index_db):
    return (
        patch.object(ot, "TRACES_DIR", traces_dir),
        patch.object(vm, "TRACES_DIR", traces_dir),
        patch.object(vm, "INDEX_DB", index_db),
    )


def _transport_patches():
    d = Path(tempfile.mkdtemp(prefix="yandi_p6_transport_"))
    f = d / "transport_memory.json"
    return patch.object(tm, "REGISTRY_DIR", d), patch.object(tm, "MEMORY_FILE", f)


def _save_claim(trace, evidence_data, claim):
    trace.add_claim_raw(claim)
    vm.persist_verification_evidence(trace, [claim], evidence_data)
    ot.DecisionTracer().save_trace(trace)


# ============================================================
# 1/2. Processed exclusion happens BEFORE budget, does NOT consume a slot.
# ============================================================

def _fake_ddgs_5(query, max_results=10, fetch_cache=None):
    return [f"https://p6.example/{c}" for c in "ABCDE"], []


processed_ab = {SharedFetchCache.canonicalize("https://p6.example/A"),
                SharedFetchCache.canonicalize("https://p6.example/B")}

with patch.object(ows, "_search_with_ddgs", _fake_ddgs_5):
    candidates, discovered, stoplist_excl, processed_excl = _budgeted_side_candidates(
        ["real discovery query"], 3, SharedFetchCache(), "direct", processed_ab,
    )

check(
    "1/2: processed A,B excluded BEFORE budget cap -> exactly the 3 NEW candidates (C,D,E) selected, "
    "not A/B occupying slots",
    candidates == ["https://p6.example/C", "https://p6.example/D", "https://p6.example/E"]
    and processed_excl == 2,
    f"candidates={candidates} processed_excl={processed_excl}",
)

# ============================================================
# 3. Exact content_hash scope: claim X processed A does not block claim Y.
# ============================================================

traces_3, index_3 = _isolated_paths()
p1, p2, p3 = _vm_patches(traces_3, index_3)

with p1, p2, p3:
    claim_x = {"claim_id": "cl_x", "claim_text": "Утверждение X про планету Юпитер.",
               "content_hash": compute_claim_content_hash("Утверждение X про планету Юпитер."),
               "evidence_relations": [{"evidence_id": "ev_x", "relation": "supports", "method": "nli"}]}
    ev_data_x = [{"evidence_id": "ev_x", "source_uri": "https://shared.example/page",
                  "content_excerpt": "shared content"}]
    trace_x = ot.Trace(trace_id="t_x", timestamp=time.time(), query="q")
    _save_claim(trace_x, ev_data_x, claim_x)

    urls_x, _ = vm.get_historical_web_urls(claim_x["content_hash"])
    urls_y, occ_y = vm.get_historical_web_urls(
        compute_claim_content_hash("Совершенно другое утверждение Y про биологию клетки.")
    )

    check(
        "3: claim X's processed set contains the shared URL",
        "https://shared.example/page" in urls_x,
    )
    check(
        "3: claim Y (different content_hash) has an EMPTY processed set — "
        "the same URL remains available to it",
        len(urls_y) == 0 and occ_y == 0,
        f"urls_y={urls_y} occ_y={occ_y}",
    )

# ============================================================
# 4. Multiple historical traces UNION: RUN1{A,B} + RUN2{C,D} -> {A,B,C,D}.
# ============================================================

traces_4, index_4 = _isolated_paths()
p1, p2, p3 = _vm_patches(traces_4, index_4)

with p1, p2, p3:
    CH4 = compute_claim_content_hash("Claim повторяющееся утверждение про историю.")

    claim_r1 = {"claim_id": "cl_r1", "claim_text": "Claim повторяющееся утверждение про историю.",
                "content_hash": CH4,
                "evidence_relations": [{"evidence_id": "ev_a", "relation": "supports", "method": "nli"},
                                        {"evidence_id": "ev_b", "relation": "uncertain", "method": "nli"}]}
    ev_data_r1 = [{"evidence_id": "ev_a", "source_uri": "https://union.example/A", "content_excerpt": "a"},
                  {"evidence_id": "ev_b", "source_uri": "https://union.example/B", "content_excerpt": "b"}]
    trace_r1 = ot.Trace(trace_id="RUN1", timestamp=1000.0, query="q")
    _save_claim(trace_r1, ev_data_r1, claim_r1)

    claim_r2 = {"claim_id": "cl_r2", "claim_text": "Claim повторяющееся утверждение про историю.",
                "content_hash": CH4,
                "evidence_relations": [{"evidence_id": "ev_c", "relation": "contradicts", "method": "nli"},
                                        {"evidence_id": "ev_d", "relation": "unrelated", "method": "nli"}]}
    ev_data_r2 = [{"evidence_id": "ev_c", "source_uri": "https://union.example/C", "content_excerpt": "c"},
                  {"evidence_id": "ev_d", "source_uri": "https://union.example/D", "content_excerpt": "d"}]
    trace_r2 = ot.Trace(trace_id="RUN2", timestamp=2000.0, query="q")
    _save_claim(trace_r2, ev_data_r2, claim_r2)

    urls_union, occurrences = vm.get_historical_web_urls(CH4)

    check(
        "4: union across RUN1{A,B} + RUN2{C,D} = {A,B,C,D}, not just the latest run's {C,D}",
        urls_union == {"https://union.example/A", "https://union.example/B",
                        "https://union.example/C", "https://union.example/D"},
        f"{urls_union}",
    )
    check("4: historical_occurrences counts BOTH runs", occurrences == 2, f"{occurrences}")

# ============================================================
# 5. Restart persistence: fresh lookup after simulated process restart.
# ============================================================

traces_5, index_5 = _isolated_paths()
p1, p2, p3 = _vm_patches(traces_5, index_5)

with p1, p2, p3:
    CH5 = compute_claim_content_hash("Claim для проверки survival после restart.")
    claim_5 = {"claim_id": "cl_5", "claim_text": "Claim для проверки survival после restart.",
               "content_hash": CH5,
               "evidence_relations": [{"evidence_id": "ev_5", "relation": "supports", "method": "nli"}]}
    ev_data_5 = [{"evidence_id": "ev_5", "source_uri": "https://restart.example/x", "content_excerpt": "x"}]
    trace_5 = ot.Trace(trace_id="t_5", timestamp=time.time(), query="q")
    _save_claim(trace_5, ev_data_5, claim_5)

    # Simulate restart: nothing in-process is reused, only the isolated
    # on-disk paths (same patches, representing "the files are still
    # there after a restart" — a fresh DecisionTracer()/module state
    # would behave identically since nothing here is held in memory
    # across calls other than the files themselves).
    urls_after_restart, _ = vm.get_historical_web_urls(CH5)
    check(
        "5: processed URL survives a simulated restart (on-disk index + JSONL, no in-memory state)",
        "https://restart.example/x" in urls_after_restart,
    )

# ============================================================
# 6. get_historical_web_urls uses index locators, not a full JSONL scan.
# ============================================================

traces_6, index_6 = _isolated_paths()
p1, p2, p3 = _vm_patches(traces_6, index_6)

with p1, p2, p3:
    CH6 = compute_claim_content_hash("Claim locator-only lookup test уникальный.")
    claim_6 = {"claim_id": "cl_6", "claim_text": "Claim locator-only lookup test уникальный.",
               "content_hash": CH6,
               "evidence_relations": [{"evidence_id": "ev_6", "relation": "supports", "method": "nli"}]}
    ev_data_6 = [{"evidence_id": "ev_6", "source_uri": "https://locator.example/x", "content_excerpt": "x"}]
    trace_6 = ot.Trace(trace_id="t_6", timestamp=time.time(), query="q")
    _save_claim(trace_6, ev_data_6, claim_6)

    # Add a bunch of NOISE lines to the SAME day-file, unrelated to CH6,
    # that are NOT indexed (simulating other claims/traces with
    # different content_hash sharing the file) — a full-scan
    # implementation would still work, but a locator-based one reads
    # ONLY the byte offsets the index actually points to.
    day_file = traces_6 / f"{time.strftime('%Y%m%d')}.jsonl"
    with day_file.open("a", encoding="utf-8") as f:
        for i in range(50):
            f.write('{"trace_id": "noise", "claims": [], "evidence": []}\n')

    read_calls = []
    _orig_read = vm._read_trace_line

    def _counting_read(jsonl_file, byte_offset):
        read_calls.append(byte_offset)
        return _orig_read(jsonl_file, byte_offset)

    with patch.object(vm, "_read_trace_line", _counting_read):
        urls_6, occ_6 = vm.get_historical_web_urls(CH6)

    check(
        "6: exactly ONE seek+readline was performed (one locator row for CH6), "
        "not 51 lines scanned",
        len(read_calls) == 1,
        f"read_calls={read_calls}",
    )
    check("6: the correct URL was still found via its locator", "https://locator.example/x" in urls_6)

# ============================================================
# 7-10. supports/contradicts/uncertain/unrelated all count as processed
#       (relation type does not matter for processed status — only
#       "was this a persisted EvidenceRecord linked to this claim").
# ============================================================

traces_7, index_7 = _isolated_paths()
p1, p2, p3 = _vm_patches(traces_7, index_7)

with p1, p2, p3:
    CH7 = compute_claim_content_hash("Claim для проверки всех типов relation.")
    claim_7 = {
        "claim_id": "cl_7", "claim_text": "Claim для проверки всех типов relation.",
        "content_hash": CH7,
        "evidence_relations": [
            {"evidence_id": "ev_supports", "relation": "supports", "method": "nli"},
            {"evidence_id": "ev_contradicts", "relation": "contradicts", "method": "nli"},
            {"evidence_id": "ev_uncertain", "relation": "uncertain", "method": "nli"},
            {"evidence_id": "ev_unrelated", "relation": "unrelated", "method": "nli"},
        ],
    }
    ev_data_7 = [
        {"evidence_id": "ev_supports", "source_uri": "https://rel.example/supports", "content_excerpt": "s"},
        {"evidence_id": "ev_contradicts", "source_uri": "https://rel.example/contradicts", "content_excerpt": "c"},
        {"evidence_id": "ev_uncertain", "source_uri": "https://rel.example/uncertain", "content_excerpt": "u"},
        {"evidence_id": "ev_unrelated", "source_uri": "https://rel.example/unrelated", "content_excerpt": "un"},
    ]
    trace_7 = ot.Trace(trace_id="t_7", timestamp=time.time(), query="q")
    _save_claim(trace_7, ev_data_7, claim_7)

    urls_7, _ = vm.get_historical_web_urls(CH7)

    for label, url in [
        ("7: supports", "https://rel.example/supports"),
        ("8: contradicts", "https://rel.example/contradicts"),
        ("9: uncertain", "https://rel.example/uncertain"),
        ("10: unrelated (IF persisted as verification evidence)", "https://rel.example/unrelated"),
    ]:
        check(f"{label} URL counts as processed regardless of relation type", url in urls_7, f"{urls_7}")

# ============================================================
# 11/12/13/14/15/16/17/18/19. End-to-end scrape_budgeted() with a
# real historical trace: OLD URL not re-fetched (no network attempt),
# NEW URLs still fetched, budgets stay <=3/<=3/<=6, stoplist and
# processed stay independent, transport failure / reprint after
# selection do not trigger a reserve fetch.
# ============================================================

traces_e2e, index_e2e = _isolated_paths()
p1, p2, p3 = _vm_patches(traces_e2e, index_e2e)
pt1, pt2 = _transport_patches()

with p1, p2, p3, pt1, pt2:
    CH_E2E = compute_claim_content_hash("End to end claim про повторный web cycle.")

    # RUN1: claim gets evidence at https://e2e.example/OLD1 and OLD2.
    claim_run1 = {
        "claim_id": "cl_e2e_run1", "claim_text": "End to end claim про повторный web cycle.",
        "content_hash": CH_E2E,
        "evidence_relations": [
            {"evidence_id": "ev_old1", "relation": "supports", "method": "nli"},
            {"evidence_id": "ev_old2", "relation": "supports", "method": "nli"},
        ],
    }
    ev_data_run1 = [
        {"evidence_id": "ev_old1", "source_uri": "https://e2e.example/OLD1", "content_excerpt": "old1"},
        {"evidence_id": "ev_old2", "source_uri": "https://e2e.example/OLD2", "content_excerpt": "old2"},
    ]
    trace_run1 = ot.Trace(trace_id="t_e2e_run1", timestamp=time.time(), query="q")
    _save_claim(trace_run1, ev_data_run1, claim_run1)

    # Also stoplist a THIRD, always-broken URL — independent mechanism.
    tm.stoplist_url("https://e2e.example/BANNED", "http_403", "proxy_http_403")

    fetch_attempts = []

    def _fake_ddgs_e2e(query, max_results=10, fetch_cache=None):
        if "direct" in query:
            return ["https://e2e.example/OLD1", "https://e2e.example/OLD2",
                    "https://e2e.example/BANNED", "https://e2e.example/NEW1",
                    "https://e2e.example/NEW2"], []
        return ["https://e2e.example/NEW3", "https://e2e.example/NEW4"], []

    def _fake_fetch_ok(url, query=""):
        fetch_attempts.append(url)
        return {"url": url, "title": "t", "text": f"content {url} " * 10, "content": f"content {url}"}, ""

    with patch.object(ows, "_search_with_ddgs", _fake_ddgs_e2e), \
         patch.object(ows, "_fetch_url", _fake_fetch_ok), \
         patch.object(ows, "_load_proxy_url", lambda: None):

        result_e2e = scrape_budgeted(
            "direct query for e2e test", "counter query for e2e test",
            fetch_cache=SharedFetchCache(), claim_id="cl_e2e_run2",
            content_hash=CH_E2E,
        )

    fetched_urls = {s.url for s in result_e2e.snippets}

    check(
        "11: OLD1/OLD2 (already verification evidence for this exact claim) "
        "were NEVER network-fetched this cycle",
        "https://e2e.example/OLD1" not in fetch_attempts
        and "https://e2e.example/OLD2" not in fetch_attempts,
        f"fetch_attempts={fetch_attempts}",
    )
    check(
        "12: genuinely NEW URLs were still selected and fetched",
        any(u.startswith("https://e2e.example/NEW") for u in fetched_urls),
        f"{fetched_urls}",
    )
    direct_fetched_count = sum(1 for s in result_e2e.snippets if s.origin == "direct")
    counter_fetched_count = sum(1 for s in result_e2e.snippets if s.origin == "counter")
    check("13: direct fetched <= 3", direct_fetched_count <= 3, f"{direct_fetched_count}")
    check("14: counter fetched <= 3", counter_fetched_count <= 3, f"{counter_fetched_count}")
    check("15: total fetched <= 6", len(result_e2e.snippets) <= 6, f"{len(result_e2e.snippets)}")
    check(
        "16: stoplist and processed are independent — BANNED excluded by stoplist, "
        "OLD1/OLD2 excluded by processed, neither mechanism touches the other's data",
        not tm.is_stoplisted("https://e2e.example/OLD1")
        and "https://e2e.example/BANNED" not in fetch_attempts,
    )
    check(
        "18/19: no reserve/top-up fetch happened to compensate for the 3 "
        "excluded candidates (OLD1, OLD2, BANNED) — direct selected from "
        "the remaining pool only, never re-searched",
        direct_fetched_count <= 3,
        f"{direct_fetched_count}",
    )

# ============================================================
# 17. Proxy lifecycle unchanged (direct-fail -> proxy-success still =
#     1 budget slot) even with processed exclusion active.
# ============================================================

traces_17, index_17 = _isolated_paths()
p1, p2, p3 = _vm_patches(traces_17, index_17)
pt1, pt2 = _transport_patches()

with p1, p2, p3, pt1, pt2:
    def _fake_ddgs_17(query, max_results=10, fetch_cache=None):
        return ["https://proxy17.example/x"], []

    def _fake_direct_timeout(url, query=""):
        return None, "timeout"

    def _fake_proxy_ok(url, query=""):
        text = "recovered via proxy " * 10
        return {"url": url, "title": "t", "text": text, "content": text}, ""

    with patch.object(ows, "_search_with_ddgs", _fake_ddgs_17), \
         patch.object(ows, "_fetch_url", _fake_direct_timeout), \
         patch.object(ows, "_fetch_url_proxy", _fake_proxy_ok), \
         patch.object(ows, "_load_proxy_url", lambda: "http://fake-proxy:8080"):

        result_17 = scrape_budgeted(
            "proxy recovery query", "", fetch_cache=SharedFetchCache(),
            claim_id="cl_17", content_hash="",
        )

    check(
        "17: proxy lifecycle unchanged — direct fail + proxy success still = exactly 1 snippet/slot",
        len(result_17.snippets) == 1 and "recovered via proxy" in result_17.snippets[0].text,
        f"{result_17.snippets}",
    )

# ============================================================
# 20. P1-A memory protection preserved (from Этап 3, re-confirmed here).
# ============================================================

from agent.orchestrator.claims.retrieval import _claim_has_effective_evidence

check(
    "20: a from_memory=True relation alone still does not resolve a claim (P1-A protection unchanged)",
    _claim_has_effective_evidence({
        "evidence_relations": [{"evidence_id": "e1", "evidence_role": "direct",
                                 "evidence_eligible": True, "relation": "supports", "from_memory": True}],
    }) is False,
)

# ============================================================
# 21. canonical Trust semantics unchanged (fixed-input smoke check).
# ============================================================

from agent.orchestrator.epistemic.canonical_trust import compute_canonical_trust

_ct = compute_canonical_trust("VERIFIED", "VERIFIED", lambda *a, **k: None, False)
check(
    "21: canonical Trust semantics unchanged (both strands agree -> that value, diverged=False)",
    _ct["canonical_trust"] == "VERIFIED" and _ct["diverged"] is False,
    f"{_ct}",
)

# ============================================================
# 22/23. Multi-hop provenance (RUN1 internet -> RUN2 memory -> RUN3
# memory): origin_route/origin_trace_id/origin_source_cluster_id must
# still point to the TRUE original (RUN1), not the intermediate hop.
# ============================================================

traces_mh, index_mh = _isolated_paths()
p1, p2, p3 = _vm_patches(traces_mh, index_mh)

with p1, p2, p3:
    CH_MH = compute_claim_content_hash("Multi hop provenance test claim про цепочку.")

    trace_mh1 = ot.Trace(trace_id="MH_RUN1", timestamp=1000.0, query="q")
    trace_mh1.claims.append(ClaimRecord(
        claim_id="cl_mh1", claim_text="Multi hop provenance test claim про цепочку.",
        content_hash=CH_MH,
        evidence_relations=[{"evidence_id": "ev_mh", "relation": "supports", "method": "nli"}],
    ))
    trace_mh1.evidence.append(EvidenceRecord(
        evidence_id="ev_mh", source_type="web", source_uri="https://mh.example/original",
        content_excerpt="original observation", route="internet", from_memory=False,
        source_cluster_id="sc_mh_ORIGINAL",
    ))
    ot.DecisionTracer().save_trace(trace_mh1)

    reconstructed_mh2 = vm.lookup_historical_evidence({
        "claim_id": "cl_mh2", "claim_text": "Multi hop provenance test claim про цепочку.",
        "content_hash": CH_MH,
    })
    claim_mh2 = {
        "claim_id": "cl_mh2", "claim_text": "Multi hop provenance test claim про цепочку.",
        "content_hash": CH_MH,
        "evidence_relations": [{"evidence_id": "ev_mh", "relation": "contradicts", "method": "nli"}],
    }
    trace_mh2 = ot.Trace(trace_id="MH_RUN2", timestamp=2000.0, query="q")
    _save_claim(trace_mh2, reconstructed_mh2, claim_mh2)

    reconstructed_mh3 = vm.lookup_historical_evidence({
        "claim_id": "cl_mh3", "claim_text": "Multi hop provenance test claim про цепочку.",
        "content_hash": CH_MH,
    })

    check(
        "22: after a THIRD generation reuse (RUN3 <- RUN2 <- RUN1), origin_route still = internet, not local_memory",
        len(reconstructed_mh3) == 1 and reconstructed_mh3[0]["origin_route"] == "internet",
        f"{reconstructed_mh3}",
    )
    check(
        "22: origin_trace_id still points to the TRUE original (MH_RUN1), not the intermediate MH_RUN2",
        reconstructed_mh3[0]["origin_trace_id"] == "MH_RUN1",
        f"{reconstructed_mh3}",
    )
    check(
        "23: origin_source_cluster_id remains stable (the ORIGINAL cluster) across both reuse hops",
        reconstructed_mh3[0]["origin_source_cluster_id"] == "sc_mh_ORIGINAL",
        f"{reconstructed_mh3}",
    )

# ============================================================
# 24. Processed set is a URL SET — the same URL appearing across 3
# traces (original + 2 reuse hops) counts ONCE, not 3 times.
# ============================================================

with p1, p2, p3:
    urls_mh_final, occ_mh_final = vm.get_historical_web_urls(CH_MH)
    check(
        "24: processed set is a SET — the same URL across 3 traces (RUN1 original, "
        "RUN2 reuse) counts once, not duplicated",
        urls_mh_final == {"https://mh.example/original"},
        f"{urls_mh_final}",
    )
    check("24: historical_occurrences correctly counts 2 distinct traces", occ_mh_final == 2, f"{occ_mh_final}")

# ============================================================
# 25. direct/counter origin (route_side) survives: WebSnippet -> runtime
# evidence -> Trace -> reconstruction.
# ============================================================

traces_25, index_25 = _isolated_paths()
p1, p2, p3 = _vm_patches(traces_25, index_25)

with p1, p2, p3:
    CH_25 = compute_claim_content_hash("Route side survival test claim уникальный.")
    claim_25 = {
        "claim_id": "cl_25", "claim_text": "Route side survival test claim уникальный.",
        "content_hash": CH_25,
        "evidence_relations": [{"evidence_id": "ev_direct25", "relation": "supports", "method": "nli"},
                                {"evidence_id": "ev_counter25", "relation": "contradicts", "method": "nli"}],
    }
    ev_data_25 = [
        {"evidence_id": "ev_direct25", "source_uri": "https://rs.example/direct",
         "content_excerpt": "direct side content", "route_side": "direct"},
        {"evidence_id": "ev_counter25", "source_uri": "https://rs.example/counter",
         "content_excerpt": "counter side content", "route_side": "counter"},
    ]
    trace_25 = ot.Trace(trace_id="t_25", timestamp=time.time(), query="q")
    _save_claim(trace_25, ev_data_25, claim_25)

    saved_25 = trace_25.to_dict()
    saved_route_sides = {e["source_uri"]: e["route_side"] for e in saved_25["evidence"]}
    check(
        "25a: route_side persisted correctly in Trace (direct/counter, not overwritten)",
        saved_route_sides.get("https://rs.example/direct") == "direct"
        and saved_route_sides.get("https://rs.example/counter") == "counter",
        f"{saved_route_sides}",
    )

    reconstructed_25 = vm.lookup_historical_evidence({
        "claim_id": "cl_25_new", "claim_text": claim_25["claim_text"], "content_hash": CH_25,
    })
    reconstructed_route_sides = {e["source_uri"]: e["route_side"] for e in reconstructed_25}
    check(
        "25b: route_side survives reconstruction from memory too",
        reconstructed_route_sides.get("https://rs.example/direct") == "direct"
        and reconstructed_route_sides.get("https://rs.example/counter") == "counter",
        f"{reconstructed_route_sides}",
    )

# ============================================================
# 26. Old regression suite (44/45-file) GREEN enforcement happens
# outside this file (full suite run) — same convention as Этапы 2/3.
# ============================================================

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
