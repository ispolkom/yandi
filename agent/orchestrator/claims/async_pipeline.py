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

MAX_CLAIM_WORKERS = 3

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
    a lock that could be bypassed.

    Micro-batching (P2 Part 6): never sleeps to accumulate a batch.
    Blocks (briefly, _QUEUE_POLL_S) only when the queue is empty;
    once one item is available, immediately drains whatever else is
    ALREADY queued (non-blocking get_nowait loop) before dispatching.
    A solitary ready claim is never delayed waiting for a sibling that
    may or may not arrive soon.
    """

    def __init__(self, evidence_data: List[Dict[str, Any]], log, verbose: bool):
        self._queue: "asyncio.Queue" = asyncio.Queue()
        self._evidence_data = evidence_data
        self._log = log
        self._verbose = verbose
        self.total_calls = 0
        self.max_observed_batch = 0
        self.batch_sizes: List[int] = []

    async def submit(self, claim: Dict[str, Any], label: str) -> None:
        fut: "asyncio.Future" = asyncio.get_event_loop().create_future()
        await self._queue.put((claim, label, fut))
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

            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            claims_in_batch = [item[0] for item in batch]
            label = batch[0][1]

            self.total_calls += 1
            self.max_observed_batch = max(self.max_observed_batch, len(claims_in_batch))
            self.batch_sizes.append(len(claims_in_batch))

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
                for _, _, fut in batch:
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
) -> List[Dict[str, Any]]:
    embedding_cache = EvidenceEmbeddingCache()
    evidence_lock = asyncio.Lock()
    nli_batcher = _NLIBatcher(evidence_data, log, verbose)
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
            f"nli_calls={profile.get('nli_total_calls', 0)} "
            f"nli_batch_sizes={profile.get('nli_batch_sizes', [])} "
            f"wall={cost['claim_async_pipeline_ms']/1000:.2f}s"
        )

    return result
