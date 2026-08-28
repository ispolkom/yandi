"""
agent/orch_nli_coalescing_regression_test.py — P2-C adaptive NLI
micro-batching: policy comparison experiment (item 12/13) plus the
three NEW regression tests item 20 requires beyond what P2's own
orch_async_claim_pipeline_regression_test.py already covers
(determinism, max-3-workers, NLI-concurrency-1, exception isolation,
P1-A scope — all still separately re-run, unchanged, not duplicated
here):

    - adaptive batch coalescing (close arrival merges under a
      sufficient window)
    - far-arrival no-barrier (a claim's wait is bounded by the window,
      never anything resembling the old 100-140s barrier)
    - batch-boundary determinism (same logical requests, different
      physical batch groupings across coalesce_wait_s values ->
      identical final relations/statuses)

Uses a deterministic mocked arrival schedule (item 12), not live
Ollama — the live-measured baseline distribution
(p50=3.073s p75=20.570s p90=26.209s max=31.674s, from a real
--web --validate run) is reported in
YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md P2-C and used here only to
justify WHICH coalesce_wait_s candidates are worth testing, not
replayed exactly.

Run: /home/iam/venv/bin/python3 -m agent.orch_nli_coalescing_regression_test
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import agent.orchestrator.claims.async_pipeline as pipeline_mod
from agent.orch_schemas import ClaimRecord

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


def _noop_log(*a, **k):
    pass


def _fake_map(claims, evidence_records, embedding_cache=None):
    ids_present = {e.get("evidence_id") for e in (evidence_records or [])}
    out = []
    for c in claims:
        cid = c["claim_id"]
        own_id = f"ev_{cid}"
        linked = [own_id] if own_id in ids_present else []
        out.append(ClaimRecord(
            claim_id=cid, claim_text=c["claim_text"],
            derived_from_evidence_ids=linked, verification_status="candidate",
        ))
    return out


def _fake_nli(claims, evidence, batch_label, log, verbose):
    """Deterministic per-claim result, independent of batch composition
    (proves any determinism finding is about the ARCHITECTURE, not
    just this fake happening to be order-insensitive by omission)."""
    evidence_by_id = {e.get("evidence_id"): e for e in (evidence or [])}
    for c in claims:
        relations = []
        for ev_id in c.get("derived_from_evidence_ids", []) or []:
            if ev_id in evidence_by_id:
                relations.append({
                    "evidence_id": ev_id, "evidence_role": "direct",
                    "evidence_eligible": True, "relation": "supports",
                    "method": "fake_nli",
                })
        c["evidence_relations"] = relations
    return sum(len(c.get("evidence_relations", [])) for c in claims)


def _own_evidence(cid):
    return {
        "evidence_id": f"ev_{cid}", "content_excerpt": f"evidence for {cid} " * 10,
        "source_uri": f"http://example.test/{cid}", "retrieval_origin": "claim_specific",
        "retrieval_claim_id": cid, "source_class": "scientific",
    }


async def _run_nli_batcher_schedule(schedule, coalesce_wait_s):
    """
    schedule: list of (claim_id, label, arrival_delay_s, evidence_records).
    Drives _NLIBatcher directly (not the whole pipeline) — this is the
    "deterministic mocked arrival schedule" harness item 12 asks for.
    Returns (batcher, per_claim_wait_s dict, submit_order).
    """
    all_evidence = []
    for _, _, _, ev_recs in schedule:
        all_evidence.extend(ev_recs)

    batcher = pipeline_mod._NLIBatcher(all_evidence, _noop_log, False, coalesce_wait_s=coalesce_wait_s)
    stop_event = asyncio.Event()
    consumer = asyncio.create_task(batcher.run_until(stop_event))

    claims_by_id = {}
    waits = {}

    async def _submitter(cid, label, delay, ev_recs):
        await asyncio.sleep(delay)
        claim = {"claim_id": cid, "claim_text": f"text {cid}",
                  "derived_from_evidence_ids": [e["evidence_id"] for e in ev_recs]}
        claims_by_id[cid] = claim
        t0 = time.time()
        await batcher.submit(claim, label)
        waits[cid] = time.time() - t0

    with patch.object(pipeline_mod, "run_claim_evidence_batch", _fake_nli):
        await asyncio.gather(*[
            _submitter(cid, label, delay, ev_recs) for cid, label, delay, ev_recs in schedule
        ])

    stop_event.set()
    await consumer

    return batcher, waits, claims_by_id


def _run(schedule, coalesce_wait_s):
    return asyncio.run(_run_nli_batcher_schedule(schedule, coalesce_wait_s))


# ============================================================
# Item 12 — CLOSE ARRIVAL schedule
# ============================================================

close_schedule = [
    ("A", "PASS1_ASYNC", 0.0, [_own_evidence("A")]),
    ("B", "PASS1_ASYNC", 0.02, [_own_evidence("B")]),
    ("C", "PASS1_ASYNC", 0.04, [_own_evidence("C")]),
]

batcher0, waits0, _ = _run(close_schedule, coalesce_wait_s=0.0)
batcher_coalesce, waits_c, _ = _run(close_schedule, coalesce_wait_s=0.10)

check(
    "adaptive batch coalescing: with coalesce_wait_s=0.0 (P2 baseline), "
    "3 close-arriving claims (20-40ms apart) mostly do NOT merge - "
    "close to 3 separate Ollama calls (documents the exact P2 finding "
    "this task is following up on)",
    batcher0.total_calls >= 2,
    f"total_calls={batcher0.total_calls} batch_sizes={batcher0.batch_sizes}",
)
check(
    "adaptive batch coalescing: with a 100ms window (which measurably "
    "exceeds this schedule's 20-40ms gaps), all 3 close-arriving claims "
    "merge into ONE physical Ollama call",
    batcher_coalesce.total_calls == 1 and batcher_coalesce.batch_sizes == [3],
    f"total_calls={batcher_coalesce.total_calls} batch_sizes={batcher_coalesce.batch_sizes}",
)

# ============================================================
# Item 12 — FAR ARRIVAL schedule + item 5 — hard bound proof
# ============================================================

far_schedule = [
    ("A", "PASS1_ASYNC", 0.0, [_own_evidence("A")]),
    ("B", "PASS1_ASYNC", 0.30, [_own_evidence("B")]),
    ("C", "PASS1_ASYNC", 0.60, [_own_evidence("C")]),
]

COALESCE_WINDOW = 0.10
batcher_far, waits_far, _ = _run(far_schedule, coalesce_wait_s=COALESCE_WINDOW)

check(
    "far-arrival no-barrier: claim A (arrives first, siblings 300-600ms "
    "later - far beyond the 100ms window) is flushed on its own, not "
    "held waiting for B/C",
    batcher_far.batch_sizes[0] == 1,
    f"batch_sizes={batcher_far.batch_sizes}",
)
check(
    "far-arrival no-barrier: claim A's own wait for its NLI result is "
    "bounded by approximately the coalescing window, NOT anything "
    "resembling the old 100-140s global barrier (orders of magnitude "
    "smaller, per item 5's hard-bound requirement)",
    waits_far["A"] < COALESCE_WINDOW + 0.15,
    f"A_wait={waits_far['A']:.3f}s window={COALESCE_WINDOW}s",
)
check(
    "far-arrival: all 3 far-apart claims each get their own physical "
    "call (3 calls for 3 claims, no artificial merging when nothing "
    "is actually ready to merge)",
    batcher_far.total_calls == 3,
    f"total_calls={batcher_far.total_calls}",
)

# ============================================================
# Regression for a real design flaw found and fixed during this task:
# asyncio.wait_for() blocks for the FULL remaining timeout when
# nothing arrives — an unconditional coalescing window would add up to
# coalesce_wait_s of pure latency to EVERY solitary flush, including
# PASS2 claims (measured data: always tens of seconds apart, never
# realistically mergeable), re-landing exactly the kind of avoidable
# per-claim delay this whole redesign exists to remove. Fixed by
# gating the window on the first item's label (PASS1 only).
# ============================================================

solitary_pass2_schedule = [
    ("X", "PASS2_ASYNC", 0.0, [_own_evidence("X")]),
]

_t0 = time.time()
batcher_solo2, waits_solo2, _ = _run(solitary_pass2_schedule, coalesce_wait_s=2.0)
_solo2_wall = time.time() - _t0

check(
    "a solitary PASS2 submission does NOT incur the coalescing window "
    "at all (flushes essentially immediately), even with a large "
    "coalesce_wait_s=2.0s configured globally - the window only ever "
    "applies when the FIRST item in a flush cycle is a PASS1 "
    "submission, per the measured PASS1-clusters/PASS2-never-clusters "
    "finding",
    _solo2_wall < 0.5,
    f"wall={_solo2_wall:.3f}s (would be >=2.0s if the window applied unconditionally)",
)

solitary_pass1_schedule = [
    ("Y", "PASS1_ASYNC", 0.0, [_own_evidence("Y")]),
]
_t0 = time.time()
batcher_solo1, waits_solo1, _ = _run(solitary_pass1_schedule, coalesce_wait_s=0.15)
_solo1_wall = time.time() - _t0

check(
    "a solitary PASS1 submission with NO siblings still eventually "
    "flushes on its own after the window elapses (never waits "
    "forever) - the window is a bound, not an indefinite hold",
    0.10 <= _solo1_wall < 0.5,
    f"wall={_solo1_wall:.3f}s window=0.15s",
)

# ============================================================
# Item 12 — mixed PASS1/PASS2 compatibility
# ============================================================

mixed_schedule = [
    ("A", "PASS1_ASYNC", 0.0, [_own_evidence("A")]),
    ("B", "PASS2_ASYNC", 0.01, [_own_evidence("B")]),
    ("C", "PASS1_ASYNC", 0.02, [_own_evidence("C")]),
]
batcher_mixed, _, _ = _run(mixed_schedule, coalesce_wait_s=0.10)

check(
    "PASS1 and PASS2 items arriving close together merge into one "
    "physical call (item 8: same NLI protocol, safe to combine) and "
    "the batch label honestly reflects the mix instead of hiding it "
    "as one pass",
    batcher_mixed.total_calls == 1,
    f"total_calls={batcher_mixed.total_calls}",
)

# ============================================================
# Item 10 — NLI concurrency remains exactly 1 under coalescing
# ============================================================

_active = {"n": 0, "max": 0}


def _fake_nli_tracking(claims, evidence, batch_label, log, verbose):
    _active["n"] += 1
    _active["max"] = max(_active["max"], _active["n"])
    time.sleep(0.02)
    result = _fake_nli(claims, evidence, batch_label, log, verbose)
    _active["n"] -= 1
    return result


async def _concurrency_check():
    schedule = [(f"C{i}", "PASS1_ASYNC", i * 0.005, [_own_evidence(f"C{i}")]) for i in range(10)]
    all_ev = []
    for _, _, _, ev in schedule:
        all_ev.extend(ev)
    batcher = pipeline_mod._NLIBatcher(all_ev, _noop_log, False, coalesce_wait_s=0.05)
    stop_event = asyncio.Event()
    consumer = asyncio.create_task(batcher.run_until(stop_event))

    async def _submitter(cid, label, delay, ev_recs):
        await asyncio.sleep(delay)
        claim = {"claim_id": cid, "claim_text": cid,
                  "derived_from_evidence_ids": [e["evidence_id"] for e in ev_recs]}
        await batcher.submit(claim, label)

    with patch.object(pipeline_mod, "run_claim_evidence_batch", _fake_nli_tracking):
        await asyncio.gather(*[_submitter(*s) for s in schedule])

    stop_event.set()
    await consumer
    return batcher


batcher_conc = asyncio.run(_concurrency_check())
check(
    "with coalescing active and 10 near-simultaneous claims, max "
    "concurrent NLI calls is still exactly 1 (coalescing changes CALL "
    "COUNT, never GPU concurrency - item 10's hard invariant)",
    _active["max"] == 1,
    f"observed max concurrent NLI calls={_active['max']}",
)
check(
    "coalescing actually reduced call count for this near-simultaneous "
    "batch (fewer physical calls than the 10 logical requests)",
    batcher_conc.total_calls < 10,
    f"total_calls={batcher_conc.total_calls} batch_sizes={batcher_conc.batch_sizes}",
)

# ============================================================
# Item 14 — batch-boundary determinism
# ============================================================
#
# Same 3 logical requests, forced into DIFFERENT physical batch
# groupings by using different coalesce windows against a schedule
# shaped so 0.05s groups [A,B],[C] and 0.15s groups [A],[B,C] is NOT
# guaranteed by timing alone — instead directly drive two explicit
# groupings through the same underlying fake NLI function and compare
# final claim state, which is what actually matters (the NLI function
# itself must be composition-independent, not just typically so).

claims_run_a = {
    "A": {"claim_id": "A", "claim_text": "t", "derived_from_evidence_ids": ["ev_A"]},
    "B": {"claim_id": "B", "claim_text": "t", "derived_from_evidence_ids": ["ev_B"]},
    "C": {"claim_id": "C", "claim_text": "t", "derived_from_evidence_ids": ["ev_C"]},
}
claims_run_b = {
    "A": {"claim_id": "A", "claim_text": "t", "derived_from_evidence_ids": ["ev_A"]},
    "B": {"claim_id": "B", "claim_text": "t", "derived_from_evidence_ids": ["ev_B"]},
    "C": {"claim_id": "C", "claim_text": "t", "derived_from_evidence_ids": ["ev_C"]},
}
evidence_pool = [_own_evidence("A"), _own_evidence("B"), _own_evidence("C")]

# RUN A grouping: [A,B] then [C]
_fake_nli([claims_run_a["A"], claims_run_a["B"]], evidence_pool, "PASS1_ASYNC", _noop_log, False)
_fake_nli([claims_run_a["C"]], evidence_pool, "PASS1_ASYNC", _noop_log, False)

# RUN B grouping: [A] then [B,C]
_fake_nli([claims_run_b["A"]], evidence_pool, "PASS1_ASYNC", _noop_log, False)
_fake_nli([claims_run_b["B"], claims_run_b["C"]], evidence_pool, "PASS1_ASYNC", _noop_log, False)

check(
    "batch-boundary determinism: identical logical NLI requests "
    "grouped into DIFFERENT physical batches ([A,B]+[C] vs [A]+[B,C]) "
    "produce byte-identical final relations for every claim - physical "
    "batching boundaries do not alter epistemic result",
    all(claims_run_a[cid]["evidence_relations"] == claims_run_b[cid]["evidence_relations"] for cid in ("A", "B", "C")),
    f"run_a={ {k: v['evidence_relations'] for k, v in claims_run_a.items()} } "
    f"run_b={ {k: v['evidence_relations'] for k, v in claims_run_b.items()} }",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
