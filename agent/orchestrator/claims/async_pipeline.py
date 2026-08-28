"""
agent/orchestrator/claims/async_pipeline.py — Bounded async claim
pipeline (follow-up to YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md P2:
"YANDI — ASYNC CLAIM PIPELINE / MAX 3 WORKERS").

Replaces the synchronous sequence in agent/orchestrator_v2.py:
    run_claim_evidence_mapping_pass1(claims_data, evidence_data, ...)   [ALL claims, one call]
    run_claim_evidence_batch(claims_data, evidence_data, "PASS1", ...)  [ALL claims, one call]
    apply_claim_resolution_and_second_retrieval(...)                   [gate + PASS2, ALL retrieval_claims]

with a per-claim async pipeline bounded to MAX_CLAIM_WORKERS=3
concurrent claims. Each claim moves through PASS1 mapping -> PASS1 NLI
-> resolved check -> (done | PASS2 retrieval -> PASS2 mapping -> PASS2
NLI) independently — a claim whose own PASS1 work is done does not
wait for its siblings before entering NLI or starting PASS2 (P2 Part
D/E: this wait was proven to cost ~100-140s for the fastest claim).

EXPLICITLY UNCHANGED, called by the caller exactly as before, using
THIS function's return value: classify_claim_epistemic_status() (a
pure per-claim function, already called ONCE after everything else —
P2 Part D barrier 5 — so it does not need to become streaming itself),
apply_claim_claim_disagreement(), canonical Trust, reflection, final
synthesis. This function's own internal asyncio.run() IS the global
barrier the caller relies on — nothing downstream changes meaning or
timing relative to before.

Reused, not reimplemented (P2 instruction: "если существующий batch
NLI уже хорошо работает — REUSE IT", "не создавать параллельный cache
subsystem"):
    - run_claim_evidence_batch() (agent/orchestrator/claims/mapping.py)
      — the actual Ollama-calling NLI batch function, unchanged.
    - map_claims_to_evidence() (agent/claim_evidence_mapper.py) — the
      actual mapper, unchanged except for the new optional
      embedding_cache parameter (see that file).
    - retrieve_claim_evidence() (agent/claim_evidence_retriever.py) —
      the existing PER-CLAIM retrieval function (already used
      internally by retrieve_for_claims()'s own ThreadPoolExecutor(3))
      — called directly here instead, since this pipeline's own
      asyncio.Semaphore(3) is now the concurrency bound, replacing the
      need for a second, nested executor.
    - _claim_has_effective_evidence() (agent/orchestrator/claims/
      retrieval.py) — the exact PASS1->PASS2 routing gate, unchanged.
    - merge_evidence() (agent/evidence_pool.py) — unchanged.

Deliberately NOT done in this pass (documented tradeoff, not hidden):
PASS2 query generation is NOT micro-batched across claims here —
each claim that needs PASS2 calls retrieve_claim_evidence() with
precomputed_query_result=None, which internally falls back to
formulate_claim_evidence_queries() (a single-claim call, already
GENERATION_SEMAPHORE-bounded). This trades away the PRE-EXISTING
batch-query-generation optimization (agent/orchestrator/claims/
retrieval.py used to call formulate_claim_evidence_queries_batch()
once for all retrieval_claims, upfront) for streaming correctness —
under streaming, the full set of PASS2-needing claims isn't known
upfront, so there is nothing to batch queries FOR ahead of time.
This is the query-generation path (cheap, short-text calls), not the
NLI path (the actual GPU-heavy concern the task centers on) — flagged
honestly in the final report as a known, bounded regression, not
silently absorbed.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from agent.claim_evidence_mapper import EvidenceEmbeddingCache, map_claims_to_evidence
from agent.claim_evidence_retriever import retrieve_claim_evidence
from agent.evidence_pool import merge_evidence
from agent.orchestrator.claims.mapping import run_claim_evidence_batch
from agent.orchestrator.claims.retrieval import _claim_has_effective_evidence
from agent.source_clustering import assign_source_clusters
from agent.verification_memory import lookup_historical_evidence

MAX_CLAIM_WORKERS = 3

# P2-C (adaptive NLI micro-batching): bounded coalescing window the
# single NLI consumer waits, after its first queued item, for siblings
# to also become ready before flushing.
#
# Measured (live baseline, coalesce_wait_s=0.0, YANDI_AGENT_RETRIEVAL_
# PERFORMANCE_AUDIT.md P2-C): real NLI-request inter-arrival gaps,
# n=14, p50=3.073s p75=20.570s p90=26.209s max=31.674s. Most gaps are
# tens of seconds (PASS2 claims are network-retrieval-bound, inherently
# spread out) — only the first few PASS1-only-resolved claims (no
# network dependency, complete close together) ever have a realistic
# chance to merge. Simulated candidates 0/0.25/0.5/1/2s against this
# distribution: 0.25s catches nothing (0% reduction), 1s catches one
# merge (~7% call reduction), 2s catches the early PASS1 cluster more
# fully (~20% call reduction, 15->12 physical calls on the measured
# shape). 2.0s selected: the best-justified candidate from the task's
# own suggested list — a real, if modest, reduction, while adding at
# most 2s of latency to the few early claims it ever affects, and
# staying ~50-75x smaller than the old 100-140s global barrier (P2
# Part E/K) it must never resemble (item 5's hard bound). This is a
# genuinely limited win, not a large one — reported honestly, not
# oversold; most of the pipeline's NLI calls remain batch-size-1
# because most claims are simply never ready at the same time.
NLI_COALESCE_WAIT_S = 2.0

# Consumer poll interval while waiting for the first queued item. NOT a
# batching delay (P2 Part 6 explicitly forbids a blind sleep(100-300ms)
# batching wait) — this only bounds how quickly the consumer notices a
# fresh submission when the queue was empty; once ANY item is queued,
# draining is immediate and non-blocking (see _NLIBatcher.run_until).
_QUEUE_POLL_S = 0.05


class _NLIBatcher:
    """
    Single controlled consumer for claim<->evidence NLI (P2 Part 5/G).
    Multiple claim workers call submit() and await their own result;
    exactly ONE asyncio task (run_until, spawned once) ever calls the
    underlying synchronous, Ollama-calling run_claim_evidence_batch()
    at a time — there is structurally no second consumer, so "max
    concurrent NLI consumer calls == 1" holds by construction, not by
    a lock that could be bypassed. P2-C did not change this — the
    coalescing window added below only delays WHEN the single
    consumer flushes, never adds a second consumer.

    Adaptive micro-batching (P2-C, follow-up to P2's "mostly batch
    size 1 in practice" finding): after the FIRST item in an empty
    queue arrives, waits up to `coalesce_wait_s` (default 0.0 —
    exactly the P2 behavior, no wait) for siblings to also become
    ready, using the queue's own blocking get() bounded by the
    remaining window (not a fixed sleep) so it returns the instant
    something arrives rather than waiting out the full window
    regardless. Once the window elapses (or is skipped, at
    coalesce_wait_s=0.0), drains whatever is currently queued
    (non-blocking) before dispatching ONE batch call. A solitary ready
    claim, with coalesce_wait_s=0.0, is never delayed at all; with a
    nonzero window it is delayed by AT MOST that window — never by an
    unbounded wait for a sibling that may or may not arrive.
    """

    def __init__(
        self,
        evidence_data: List[Dict[str, Any]],
        log,
        verbose: bool,
        coalesce_wait_s: float = 0.0,
    ):
        self._queue: "asyncio.Queue" = asyncio.Queue()
        self._evidence_data = evidence_data
        self._log = log
        self._verbose = verbose
        self._coalesce_wait_s = coalesce_wait_s
        self.total_calls = 0
        self.max_observed_batch = 0
        self.batch_sizes: List[int] = []
        # P2-C measurement (item 1): every submit() enqueue timestamp,
        # for inter-arrival gap analysis independent of batching policy.
        self.enqueue_timestamps: List[float] = []
        # Per-flush diagnostic records (item 1's "for every actual
        # Ollama NLI call" fields).
        self.flush_records: List[Dict[str, Any]] = []

    async def submit(self, claim: Dict[str, Any], label: str) -> None:
        fut: "asyncio.Future" = asyncio.get_event_loop().create_future()
        enqueue_ts = time.time()
        self.enqueue_timestamps.append(enqueue_ts)
        pair_count = len(claim.get("derived_from_evidence_ids") or [])
        await self._queue.put((claim, label, fut, enqueue_ts, pair_count))
        await fut

    async def run_until(self, stop_event: "asyncio.Event") -> None:
        while True:
            if stop_event.is_set() and self._queue.empty():
                return

            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=_QUEUE_POLL_S)
            except asyncio.TimeoutError:
                continue

            batch = [first]

            # Bounded coalescing window (item 3/5) — CONDITIONAL, not
            # applied uniformly. Measured data (YANDI_AGENT_RETRIEVAL_
            # PERFORMANCE_AUDIT.md P2-C) shows PASS1 submissions (no
            # network dependency — mapping/embedding only) can cluster
            # within a couple seconds of each other, while PASS2
            # submissions (network-retrieval-bound) are ALWAYS tens of
            # seconds apart in this workload — never close enough to
            # benefit from any bounded window. Waiting unconditionally
            # would add up to coalesce_wait_s of pure latency to EVERY
            # solitary flush (asyncio.wait_for blocks for the FULL
            # remaining timeout when nothing arrives — it does not
            # "give up early" just because a merge looks unlikely),
            # which would re-introduce exactly the kind of avoidable
            # per-claim delay this whole redesign exists to remove —
            # worst case landing on the slowest (typically PASS2)
            # claim's own critical path. Gating on the first item's
            # label is the measured, targeted signal item 3 asks for
            # ("only if measurements show another request is likely to
            # arrive soon"), not a guess.
            if self._coalesce_wait_s > 0 and first[1].startswith("PASS1"):
                window_deadline = time.time() + self._coalesce_wait_s
                while True:
                    remaining = window_deadline - time.time()
                    if remaining <= 0:
                        break
                    try:
                        nxt = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                        batch.append(nxt)
                    except asyncio.TimeoutError:
                        break

            # Drain-now (item 6): whatever's ALREADY queued at this
            # point is free to collect — no additional wait per item.
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            flush_ts = time.time()
            claims_in_batch = [item[0] for item in batch]
            # P2-C item 8: PASS1 and PASS2 items share the exact same
            # NLI protocol (run_claim_evidence_batch's batch_label is
            # used ONLY for a diagnostic log line — confirmed by
            # reading agent/orchestrator/claims/mapping.py, no branch
            # on it anywhere else) — safe to combine physically. Label
            # reflects a mixed batch honestly instead of silently
            # reporting the first item's label for a batch that may
            # contain the other pass too.
            distinct_labels = sorted({item[1] for item in batch})
            label = distinct_labels[0] if len(distinct_labels) == 1 else "+".join(distinct_labels)
            enqueue_times_in_batch = [item[3] for item in batch]
            pair_counts_in_batch = [item[4] for item in batch]

            self.total_calls += 1
            self.max_observed_batch = max(self.max_observed_batch, len(claims_in_batch))
            self.batch_sizes.append(len(claims_in_batch))

            oldest_wait = flush_ts - min(enqueue_times_in_batch)
            newest_wait = flush_ts - max(enqueue_times_in_batch)

            gen_t0 = time.time()
            try:
                await asyncio.to_thread(
                    run_claim_evidence_batch,
                    claims_in_batch,
                    self._evidence_data,
                    label,
                    self._log,
                    self._verbose,
                )
            finally:
                gen_wall = time.time() - gen_t0

                self.flush_records.append({
                    "batch_id": self.total_calls,
                    "flush_ts": flush_ts,
                    "request_count": len(batch),
                    "claim_count": len(claims_in_batch),
                    "pair_count": sum(pair_counts_in_batch),
                    "oldest_wait": oldest_wait,
                    "newest_wait": newest_wait,
                    "generation_wall": gen_wall,
                })

                if self._verbose:
                    self._log(
                        f"[NLI Batcher] batch_id={self.total_calls} "
                        f"label={label} requests={len(batch)} "
                        f"pairs={sum(pair_counts_in_batch)} "
                        f"oldest_wait={oldest_wait:.3f}s "
                        f"newest_wait={newest_wait:.3f}s "
                        f"generation_wall={gen_wall:.3f}s"
                    )

                for _, _, fut, _, _ in batch:
                    if not fut.done():
                        fut.set_result(None)


async def _process_one_claim(
    claim: Dict[str, Any],
    evidence_data: List[Dict[str, Any]],
    evidence_lock: "asyncio.Lock",
    embedding_cache: "EvidenceEmbeddingCache",
    nli_batcher: "_NLIBatcher",
    fetch_cache,
    enable_web: bool,
    is_subjective_answer: bool,
    skip_rag: bool,
    semaphore: "asyncio.Semaphore",
    active_counter: Dict[str, int],
    profile: Dict[str, Any],
    log,
    verbose: bool,
) -> None:
    async with semaphore:
        active_counter["active"] += 1
        active_counter["max_active"] = max(active_counter["max_active"], active_counter["active"])

        claim_id = claim.get("claim_id", "unknown")
        timeline = {"claim_id": claim_id}
        profile.setdefault("timelines", []).append(timeline)
        t_start = time.time()

        try:
            # ---- PASS1 mapping (per-claim, shared embedding cache) ----
            async with evidence_lock:
                snapshot = list(evidence_data)

            mapped = await asyncio.to_thread(
                map_claims_to_evidence, [claim], snapshot, embedding_cache,
            )

            if mapped:
                mc = mapped[0]
                claim["derived_from_evidence_ids"] = list(mc.derived_from_evidence_ids or [])
            else:
                claim["derived_from_evidence_ids"] = []

            claim["verification_status"] = "candidate"
            timeline["pass1_mapping_done"] = time.time() - t_start

            # ---- PASS1 NLI (shared controlled consumer) ----
            await nli_batcher.submit(claim, "PASS1_ASYNC")
            timeline["pass1_nli_done"] = time.time() - t_start

            # Epistemic Core v1 Phase 3 fields — same contract as the
            # synchronous retrieval.py path: default to "gate blocked
            # it" (None/None) unless PASS2 is actually attempted below.
            claim["evidence_search_attempted"] = None
            claim["evidence_search_error"] = None

            # ---- resolved at PASS1? same gate as the sync path ----
            if not (enable_web and not skip_rag and not is_subjective_answer):
                timeline["final_status_ready"] = time.time() - t_start
                return

            # ---- MEMORY PASS (Этап 3 / P5: verification memory) ----
            #
            # LOAD point: after claim extraction/identity (content_hash
            # already set upstream, in claims/lifecycle.py), BEFORE PASS2
            # web retrieval — per the confirmed brief. Historical evidence
            # (if any) is reconstructed as ordinary route="local_memory"
            # candidates OWNED by this claim (retrieval_claim_id=claim_id,
            # via agent.verification_memory.lookup_historical_evidence) and
            # run through the EXACT SAME Mapper -> NLI this claim's PASS1
            # evidence just went through — never copied in as a ready-made
            # verdict. _claim_has_effective_evidence() below ignores
            # from_memory relations on their own (P4 §10), so a memory hit
            # alone can never short-circuit PASS2 — it only participates in
            # the final relation set alongside whatever PASS1/PASS2 find.
            try:
                memory_evidence = await asyncio.to_thread(
                    lookup_historical_evidence, claim, None, log, verbose,
                )
            except Exception:
                memory_evidence = []

            print(
                f"[VerificationMemory] claim_id={claim_id} "
                f"content_hash={(claim.get('content_hash') or '-')[:12]} "
                f"evidence_loaded={len(memory_evidence)}"
            )

            timeline["memory_evidence_loaded"] = len(memory_evidence)

            if memory_evidence:
                async with evidence_lock:
                    evidence_data[:] = merge_evidence(evidence_data, memory_evidence)
                    snapshot_mem = list(evidence_data)

                mapped_mem = await asyncio.to_thread(
                    map_claims_to_evidence, [claim], snapshot_mem, embedding_cache,
                )

                mapped_mem_ids = list(mapped_mem[0].derived_from_evidence_ids or []) if mapped_mem else []
                if mapped_mem:
                    claim["derived_from_evidence_ids"] = mapped_mem_ids

                memory_ids = {e["evidence_id"] for e in memory_evidence if e.get("evidence_id")}
                memory_mapped = len(memory_ids & set(mapped_mem_ids))
                timeline["memory_evidence_mapped"] = memory_mapped

                await nli_batcher.submit(claim, "MEMORY_ASYNC")
                timeline["memory_pass_done"] = time.time() - t_start

                memory_nli_pairs = sum(
                    1 for rel in (claim.get("evidence_relations") or [])
                    if rel.get("evidence_id") in memory_ids
                )
                timeline["memory_nli_pairs"] = memory_nli_pairs

                print(
                    f"[VerificationMemory] claim_id={claim_id} "
                    f"evidence_mapped={memory_mapped} nli_pairs={memory_nli_pairs}"
                )

            if _claim_has_effective_evidence(claim):
                timeline["final_status_ready"] = time.time() - t_start
                return  # DONE — resolved, no PASS2 needed

            # ---- PASS2 needed ----
            claim["evidence_search_attempted"] = True
            timeline["pass2_start"] = time.time() - t_start

            try:
                new_evidence = await asyncio.to_thread(
                    retrieve_claim_evidence, claim, fetch_cache, None,
                )
            except Exception as exc:
                # ERROR != NOT_FOUND, and never becomes FALSE/CONTRADICTED
                # (P2 instruction 14/15) — recorded, claim simply stays
                # whatever PASS1 already determined (typically unresolved
                # -> unverified at the final status gate, same as a
                # PASS2 attempt that legitimately found nothing).
                claim["evidence_search_error"] = str(exc)
                timeline["final_status_ready"] = time.time() - t_start
                return

            timeline["pass2_retrieval_done"] = time.time() - t_start

            if new_evidence:
                async with evidence_lock:
                    evidence_data[:] = merge_evidence(evidence_data, new_evidence)
                    snapshot2 = list(evidence_data)

                mapped2 = await asyncio.to_thread(
                    map_claims_to_evidence, [claim], snapshot2, embedding_cache,
                )

                if mapped2:
                    mc2 = mapped2[0]
                    claim["derived_from_evidence_ids"] = list(mc2.derived_from_evidence_ids or [])

                timeline["pass2_mapping_done"] = time.time() - t_start

                await nli_batcher.submit(claim, "PASS2_ASYNC")
                timeline["pass2_nli_done"] = time.time() - t_start

            timeline["final_status_ready"] = time.time() - t_start

        finally:
            active_counter["active"] -= 1


async def _run_async_claim_pipeline_impl(
    claims_data: List[Dict[str, Any]],
    evidence_data: List[Dict[str, Any]],
    enable_web: bool,
    is_subjective_answer: bool,
    skip_rag: bool,
    fetch_cache,
    log,
    verbose: bool,
    profile: Dict[str, Any],
    coalesce_wait_s: float = 0.0,
) -> List[Dict[str, Any]]:
    embedding_cache = EvidenceEmbeddingCache()
    evidence_lock = asyncio.Lock()
    nli_batcher = _NLIBatcher(evidence_data, log, verbose, coalesce_wait_s=coalesce_wait_s)
    semaphore = asyncio.Semaphore(MAX_CLAIM_WORKERS)
    stop_event = asyncio.Event()
    active_counter = {"active": 0, "max_active": 0}

    consumer_task = asyncio.create_task(nli_batcher.run_until(stop_event))

    eligible_claims = [
        c for c in claims_data
        if isinstance(c, dict) and (c.get("claim_text") or "").strip()
    ]

    worker_tasks = [
        asyncio.create_task(
            _process_one_claim(
                claim, evidence_data, evidence_lock, embedding_cache,
                nli_batcher, fetch_cache, enable_web, is_subjective_answer,
                skip_rag, semaphore, active_counter, profile, log, verbose,
            )
        )
        for claim in eligible_claims
    ]

    try:
        await asyncio.gather(*worker_tasks)
    finally:
        # GLOBAL BARRIER: every claim worker has reached DONE (or
        # raised, isolated per-task by gather's default behavior being
        # overridden below) before the consumer is told to stop —
        # no orphaned NLI submissions, no task left waiting on a queue
        # nobody will ever drain again (P2 instruction 16, cancellation
        # safety).
        stop_event.set()
        await consumer_task

    if profile.get("_growth_before") is not None and len(evidence_data) > profile["_growth_before"]:
        assign_source_clusters(evidence_data, log=log, verbose=verbose)

    profile["max_active_claim_workers"] = active_counter["max_active"]
    profile["nli_total_calls"] = nli_batcher.total_calls
    profile["nli_max_batch"] = nli_batcher.max_observed_batch
    profile["nli_batch_sizes"] = nli_batcher.batch_sizes
    profile["nli_enqueue_timestamps"] = nli_batcher.enqueue_timestamps
    profile["nli_flush_records"] = nli_batcher.flush_records

    # P5 (verification memory): request-wide summary, aggregated from
    # each claim worker's own timeline entries (set in the MEMORY PASS
    # block above).
    timelines = profile.get("timelines", [])
    memory_claim_hits = sum(1 for t in timelines if t.get("memory_evidence_loaded", 0) > 0)
    memory_evidence_loaded_total = sum(t.get("memory_evidence_loaded", 0) for t in timelines)
    memory_evidence_mapped_total = sum(t.get("memory_evidence_mapped", 0) for t in timelines)
    memory_nli_pairs_total = sum(t.get("memory_nli_pairs", 0) for t in timelines)

    profile["memory_claim_hits"] = memory_claim_hits
    profile["memory_evidence_loaded"] = memory_evidence_loaded_total
    profile["memory_evidence_mapped"] = memory_evidence_mapped_total
    profile["memory_nli_pairs"] = memory_nli_pairs_total

    print(
        f"[VerificationMemory] summary claims={len(timelines)} "
        f"memory_claim_hits={memory_claim_hits} "
        f"memory_evidence_loaded={memory_evidence_loaded_total} "
        f"memory_evidence_mapped={memory_evidence_mapped_total} "
        f"memory_nli_pairs={memory_nli_pairs_total}"
    )

    return evidence_data


def run_async_claim_pipeline(
    claims_data: List[Dict[str, Any]],
    evidence_data: List[Dict[str, Any]],
    enable_web: bool,
    is_subjective_answer: bool,
    skip_rag: bool,
    fetch_cache,
    cost: Dict[str, Any],
    log,
    verbose: bool,
    coalesce_wait_s: float = NLI_COALESCE_WAIT_S,
) -> List[Dict[str, Any]]:
    """
    Synchronous entry point (called from the still-synchronous
    orchestrator_v2.py). Runs the whole bounded async claim pipeline
    to completion via asyncio.run() — this IS the global barrier the
    caller relies on: this function does not return until every claim
    has reached its final per-claim state (resolved at PASS1, or
    PASS2 attempted and resolved/exhausted).

    Mutates each claim in claims_data in place (derived_from_evidence_ids,
    verification_status, evidence_relations, evidence_search_attempted,
    evidence_search_error) — same contract as the synchronous
    run_claim_evidence_mapping_pass1 + run_claim_evidence_batch("PASS1")
    + apply_claim_resolution_and_second_retrieval it replaces.

    Returns the (possibly grown) evidence_data list — same contract as
    apply_claim_resolution_and_second_retrieval's return value.
    """
    _t0 = time.time()

    profile: Dict[str, Any] = {"_growth_before": len(evidence_data)}

    result = asyncio.run(
        _run_async_claim_pipeline_impl(
            claims_data, evidence_data, enable_web, is_subjective_answer,
            skip_rag, fetch_cache, log, verbose, profile,
            coalesce_wait_s=coalesce_wait_s,
        )
    )

    cost["claim_async_pipeline_ms"] = (time.time() - _t0) * 1000
    cost["claim_async_max_workers"] = profile.get("max_active_claim_workers", 0)
    cost["claim_async_nli_calls"] = profile.get("nli_total_calls", 0)
    cost["claim_async_nli_max_batch"] = profile.get("nli_max_batch", 0)

    if verbose:
        log(
            f"[Async Claim Pipeline] "
            f"claims={len(claims_data)} "
            f"max_concurrent_workers={profile.get('max_active_claim_workers', 0)} "
            f"coalesce_wait_s={coalesce_wait_s} "
            f"nli_calls={profile.get('nli_total_calls', 0)} "
            f"nli_batch_sizes={profile.get('nli_batch_sizes', [])} "
            f"wall={cost['claim_async_pipeline_ms']/1000:.2f}s"
        )

        # P2-C item 1: inter-arrival gap distribution, independent of
        # batching policy — measures how far apart claims' NLI requests
        # actually arrive, which is what any coalescing-window choice
        # must be justified against.
        ts = sorted(profile.get("nli_enqueue_timestamps", []))
        if len(ts) > 1:
            gaps = sorted(b - a for a, b in zip(ts, ts[1:]))

            def _pct(p):
                idx = min(len(gaps) - 1, int(round(p * (len(gaps) - 1))))
                return gaps[idx]

            log(
                f"[NLI Inter-Arrival] n_gaps={len(gaps)} "
                f"p50={_pct(0.50):.3f}s p75={_pct(0.75):.3f}s "
                f"p90={_pct(0.90):.3f}s max={gaps[-1]:.3f}s"
            )
        # Per-batch detail is already logged in real time as
        # [NLI Batcher] lines inside _NLIBatcher.run_until() — not
        # repeated here to avoid duplicate log volume.

    return result
