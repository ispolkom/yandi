"""
agent/verification_memory.py — Этап 3 (P5): LOCAL MEMORY as one of
YANDI's five epistemic channels (local_model / local_memory /
network_node / internet / ai_chat).

"ТОЧКА НОЛЬ" v13 (owner mandate, 2026-09): registry/dataset/orch_traces/
*.jsonl (agent.orch_tracer.Trace) and registry/index.db's
claim_verification_index sqlite locator table are BOTH retired, not
migrated. The locator-plus-byte-offset-seek-plus-JSONL-line-parse
mechanism this module used to depend on is gone entirely — claim_
occurrence/source_observation/evidence_relation (agent.db.sql.
shadow_write.record_claims_and_evidence(), now PRIMARY and lossless as
of this same v13 pass) are queried directly instead, via agent.db.sql.
repositories.find_claim_occurrences_by_content_hash()/
find_claim_occurrences_by_family()/list_evidence_for_claim(). This is a
genuine simplification, not just a format swap: SQL can index/query by
content_hash and semantic_family_id directly, so the whole "where is
this trace's line in which day-file" locator problem this module
existed partly to solve no longer exists.

    SAVE — collect_verification_evidence_ids() / persist_verification_
           evidence(): find which evidence in the runtime pool actually
           participated in claim verification (has an evidence_relation
           on some claim — "FETCHED SOURCE != VERIFICATION EVIDENCE",
           not the whole discovery pool) and add a full EvidenceRecord
           for each onto `trace` in memory, wired at the EXISTING
           trace.add_claim_raw() save point (agent/orchestrator/claims/
           status.py). UNCHANGED by v13 — trace.evidence is no longer
           itself persisted anywhere (agent.orch_tracer.DecisionTracer.
           save_trace() only persists the envelope now), but keeping
           this in-memory build step costs nothing and nothing else in
           this module depends on removing it.

    LOAD — lookup_historical_evidence(): given the CURRENT claim's
           content_hash (the only lookup key this v1 actually uses at
           runtime — see that function's docstring for why the
           semantic_family_id fallback isn't live-queried yet), find a
           prior verification of the "same" claim and reconstruct its
           evidence as ordinary runtime evidence dicts, tagged
           route="local_memory"/from_memory=True with the original
           provenance chain preserved (origin_route/origin_trace_id/
           origin_observed_at — P4 §12: reuse is a new ROUTE, never a
           new SOURCE). These are fed into the EXISTING Mapper -> NLI
           the same way fresh PASS2 evidence is — the historical
           relation/verdict is NEVER copied as-is. See agent/
           orchestrator/claims/async_pipeline.py's MEMORY PASS block for
           the caller.

Explicitly NOT this module's job (later stage, per the Этап 3 brief):
excluding already-processed URLs from NEW web retrieval (WebBudget
candidate selection is untouched); node/AI-chat channels (node_id/
validator_id/model_id stay None — schema-prepared, not activated).

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every
function here. There is no JSONL/sqlite fallback left to quietly
succeed against.
"""
from __future__ import annotations

import time
from datetime import timezone
from typing import Any, Dict, List, Optional

from agent.claim_identity import compute_claim_content_hash
from agent.orch_schemas import EvidenceRecord
from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo
# P10 (Этап 4G-2): reuses the SAME URL normalization already used for
# exact-URL-dedup / processed-source-reuse (Этап 2/4) — not a second
# canonicalization. Safe to import at module level: orch_web_scraper.py
# only imports THIS module back inside a function body (scrape_budgeted),
# never at its own module top, so there is no import cycle.
from agent.orch_web_scraper import SharedFetchCache

# How many prior verifications of "the same" claim to reconstruct
# evidence from. 1 = only the single most recent occurrence — matches
# the P4 "bounded verification per cycle, accumulating knowledge across
# cycles" philosophy (a later cycle's SAVE will itself become the new
# most-recent occurrence for the cycle after that), not an attempt to
# gather every historical mention at once.
MAX_HISTORICAL_OCCURRENCES = 1


def _dt_to_unix(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return value.replace(tzinfo=timezone.utc).timestamp()


# ============================================================
# SAVE
# ============================================================

def collect_verification_evidence_ids(claims_data: List[Dict[str, Any]]) -> set:
    """
    FETCHED SOURCE != VERIFICATION EVIDENCE (P4 §2): only evidence that
    actually got an evidence_relation attached to some claim counts —
    not the whole discovery/rejected pool. Union across all claims (one
    evidence item can legitimately relate to several claims when it's
    shared/global, not claim-owned).
    """
    used = set()
    for claim in claims_data or []:
        for rel in claim.get("evidence_relations", []) or []:
            ev_id = rel.get("evidence_id")
            if ev_id:
                used.add(ev_id)
    return used


def persist_verification_evidence(
    trace: Any,
    claims_data: List[Dict[str, Any]],
    evidence_data: List[Dict[str, Any]],
    log=None,
    verbose: bool = False,
) -> int:
    """
    Builds a full EvidenceRecord (agent/orch_schemas.py) for every
    evidence item that participated in verification (per
    collect_verification_evidence_ids) and adds it to `trace` via the
    EXISTING trace.add_evidence() — no new tracer method, no new
    persistence path. Mutates `trace` in place (trace.evidence).
    Returns the count added.
    """
    used_ids = collect_verification_evidence_ids(claims_data)
    if not used_ids:
        return 0

    evidence_by_id = {
        ev.get("evidence_id"): ev
        for ev in (evidence_data or [])
        if ev.get("evidence_id")
    }

    added = 0
    for ev_id in used_ids:
        ev = evidence_by_id.get(ev_id)
        if not ev:
            continue

        record = EvidenceRecord(
            evidence_id=ev.get("evidence_id", ""),
            source_type=ev.get("source_type", "web"),
            source_uri=ev.get("source_uri", ""),
            source_title=ev.get("source_title", ""),
            retrieval_query=", ".join(ev.get("retrieval_queries", []) or [])[:200],
            content_excerpt=(ev.get("content_excerpt") or "")[:700],
            relevance_to_query=float(ev.get("relevance_to_query", 0.0) or 0.0),
            quality_score=float(ev.get("quality_score", 0.0) or 0.0),
            source_class=ev.get("source_class", "unknown"),
            evidence_eligible=bool(ev.get("evidence_eligible", False)),
            evidence_role=ev.get("evidence_role", "context"),
            authority=float(ev.get("authority", 0.0) or 0.0),
            traceability=float(ev.get("traceability", 0.0) or 0.0),
            primaryness=float(ev.get("primaryness", 0.0) or 0.0),
            is_meta_pipeline_output=bool(ev.get("is_meta_pipeline_output", False)),
            is_subject_matter_evidence=bool(ev.get("is_subject_matter_evidence", True)),
            rejection_reason=ev.get("rejection_reason"),
            # P4 §4: propagate the ALREADY-COMPUTED source_cluster_id
            # as-is — never recompute inside the tracer.
            source_cluster_id=ev.get("source_cluster_id"),
            retrieval_claim_id=ev.get("retrieval_claim_id", "") or "",
            route=ev.get("route", "internet") or "internet",
            observed_at=ev.get("observed_at") or time.time(),
            from_memory=bool(ev.get("from_memory", False)),
            origin_route=ev.get("origin_route"),
            origin_trace_id=ev.get("origin_trace_id"),
            origin_observed_at=ev.get("origin_observed_at"),
            origin_source_cluster_id=ev.get("origin_source_cluster_id"),
            node_id=ev.get("node_id"),
            validator_id=ev.get("validator_id"),
            model_id=ev.get("model_id"),
            route_side=ev.get("route_side", "") or "",
        )
        trace.add_evidence(record)
        added += 1

    if verbose and log:
        log(
            f"[VerificationMemory] persisted {added} evidence record(s) "
            f"(runtime pool={len(evidence_data or [])}, used-in-verification={len(used_ids)})"
        )

    return added


# ============================================================
# LOAD
# ============================================================

def _reconstruct_evidence(
    historical_evidence: List[Dict[str, Any]],
    current_claim_id: str,
    current_claim_text: str,
) -> List[Dict[str, Any]]:
    """
    Runtime-shape evidence dicts (same keys claim_evidence_retriever.py's
    PASS2 evidence uses) reconstructed from a historical claim's SQL-
    backed evidence (agent.db.sql.repositories.list_evidence_for_claim())
    — tagged route="local_memory"/from_memory=True, owned by the CURRENT
    claim (retrieval_claim_id=current_claim_id, retrieval_origin=
    "claim_specific" — reuses the EXISTING ownership-gate in claim_
    evidence_mapper.py, P4 §3), with the original provenance chain
    preserved (origin_* — P4 §12, already resolved to the TRUE root by
    repositories.list_evidence_for_claim()'s own origin-chain walk, so
    no "is this itself already a replay" branching is needed here the
    way the old JSON-reading code needed — the root is the root
    regardless of how many reuse hops preceded this one).

    Deliberately does NOT copy `relation` — the historical relation is
    audit-only; reconstructed evidence here carries no relation field at
    all, so it is structurally impossible for it to be mistaken for an
    already-computed verdict downstream (P4 §9/§10).
    """
    reconstructed = []
    for ev in historical_evidence:
        if not ev.get("content_excerpt"):
            continue

        reconstructed.append({
            "evidence_id": ev.get("evidence_id") or f"ev_{ev.get('source_uri', '')[:8]}",
            "source_type": ev.get("source_type", "web"),
            "source_uri": ev.get("source_uri", ""),
            "source_title": ev.get("source_title", ""),
            "content_excerpt": ev.get("content_excerpt", ""),
            "relevance_to_query": ev.get("relevance_to_query", 0.5),
            "quality_score": ev.get("quality_score", 0.0),
            "source_class": ev.get("source_class", "unknown"),
            "evidence_eligible": ev.get("evidence_eligible", False),
            "evidence_role": ev.get("evidence_role", "context"),
            "authority": ev.get("authority", 0.0),
            "traceability": ev.get("traceability", 0.0),
            "primaryness": ev.get("primaryness", 0.0),
            "is_meta_pipeline_output": False,
            "is_subject_matter_evidence": True,
            "rejection_reason": None,

            "retrieval_origin": "claim_specific",
            "retrieval_claim_id": current_claim_id,
            "retrieval_claim_text": current_claim_text[:300],
            "route_side": ev.get("route_side", "") or "",

            "route": "local_memory",
            "from_memory": True,
            "origin_route": ev.get("origin_route") or "internet",
            "origin_trace_id": ev.get("origin_trace_id"),
            "origin_observed_at": _dt_to_unix(ev.get("origin_observed_at")),
            "origin_source_cluster_id": ev.get("origin_source_cluster_id") or ev.get("source_cluster_id"),
            # source_cluster_id intentionally left unset here — the
            # CURRENT cycle's assign_source_clusters() recomputes it
            # fresh over whatever's actually present this cycle (P4
            # §4's "tracer only saves, never reclusters" is about SAVE;
            # this is a normal LOAD-time evidence candidate, clustered
            # like any other this cycle, so reuse never fabricates a
            # new independent root — P4 §12).
        })

    return reconstructed


def get_historical_web_urls(
    content_hash: str,
    exclude_trace_id: Optional[str] = None,
    log=None,
    verbose: bool = False,
) -> tuple:
    """
    P6 (Этап 4): "processed" URL set for a claim — the UNION of
    source_uri values across EVERY historical occurrence of this
    content_hash (§3: no LIMIT, unlike lookup_historical_evidence's
    conservative single-most-recent-occurrence LOAD, which is a
    DIFFERENT job and stays untouched — §15).

    "Processed" = a persisted evidence observation genuinely linked to
    this claim via an evidence_relation (§4) — exactly what
    record_claims_and_evidence() already restricts SAVE to, so no new
    filtering rule is invented here; relation type (supports/
    contradicts/uncertain/unrelated) does NOT matter (§4/§10 of the
    Этап 3 brief already established unrelated counts too).

    Returns (url_set, historical_occurrences) — occurrence count is the
    number of distinct historical claim_occurrence rows found (for the
    [ProcessedSources] observability line, §17), not the URL count.
    """
    with get_connection() as conn:
        occurrences = repo.find_claim_occurrences_by_content_hash(conn, content_hash, limit=None)
        if exclude_trace_id:
            occurrences = [o for o in occurrences if o["run_id"] != exclude_trace_id]

        urls: set = set()
        seen_traces: set = set()
        for occ in occurrences:
            evidence = repo.list_evidence_for_claim(conn, occ["claim_id"])
            if not evidence:
                continue
            seen_traces.add(occ["run_id"])
            for ev in evidence:
                uri = ev.get("source_uri")
                if uri:
                    urls.add(uri)

    if verbose and log:
        log(
            f"[ProcessedSources] content_hash={(content_hash or '-')[:12]} "
            f"historical_occurrences={len(seen_traces)} processed_urls={len(urls)}"
        )

    return urls, len(seen_traces)


def lookup_historical_evidence(
    claim: Dict[str, Any],
    exclude_trace_id: Optional[str] = None,
    log=None,
    verbose: bool = False,
    domain: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    P5 LOAD entry point. content_hash EXACT match is the PRIMARY lookup
    path (P4 §5) — cheap (a hash comparison, no model call), so it
    always runs first and, on a hit, is never second-guessed by the
    fallback below.

    2026-09 (owner mandate: reuse by MEANING, not only by near-identical
    wording — "если смысл не изменился, оставляем", still always
    re-verified, never a blind copy): when the exact match finds
    NOTHING and a `domain` is given, this now ALSO tries the semantic
    family the current claim belongs to — the SAME matching this
    codebase already uses to decide whether two differently-worded
    claims mean the same thing (agent.claim_family_registry.
    ClaimFamilyRegistry.find_or_link_claim: embedding prefilter, then an
    LLM judge on the close candidates only — never every claim against
    every claim, the O(n²) cost this codebase has already paid for
    elsewhere and moved away from). `find_or_link_claim` is idempotent
    (family_member's own (family_id, claim_id) primary key + INSERT
    IGNORE make a re-link of the same claim a safe no-op), so calling it
    here — earlier than orchestrator/claims/lifecycle.py's own official
    assign_claim_family_identity() call later in the same request — is
    safe; the known, accepted cost is one extra embedding+LLM-judge
    round trip per claim in the common no-exact-hit case, paid once
    here and once more (as a cheap no-op re-link) later. No `domain` —
    the fallback is skipped, exactly like before this change.

    Whichever path found it, the reused evidence is fed into the SAME
    Mapper -> NLI this claim's fresh evidence goes through — never a
    verdict copied in as-is (see _reconstruct_evidence's own docstring:
    no `relation` field is ever carried over).

    Returns [] on a memory miss — never fabricates a match.
    """
    content_hash = claim.get("content_hash") or compute_claim_content_hash(
        claim.get("claim_text", "")
    )
    claim_text = claim.get("claim_text", "")
    claim_id = claim.get("claim_id", "")

    with get_connection() as conn:
        occurrences = repo.find_claim_occurrences_by_content_hash(
            conn, content_hash, limit=MAX_HISTORICAL_OCCURRENCES, exclude_run_id=exclude_trace_id,
        )

        results: List[Dict[str, Any]] = []
        for occ in occurrences:
            historical_evidence = repo.list_evidence_for_claim(conn, occ["claim_id"])
            results.extend(_reconstruct_evidence(historical_evidence, claim_id, claim_text))

    match_kind = "exact" if results else "none"
    family_occurrences: List[Dict[str, Any]] = []

    if not results and domain:
        from agent.claim_family_registry import get_claim_family_registry
        try:
            family_id = get_claim_family_registry().find_or_link_claim(
                claim_text, claim_id, domain, log=log, verbose=verbose,
            )
        except Exception as exc:  # noqa: BLE001 — a memory-fallback failure must never break verification itself
            family_id = None
            if verbose and log:
                log(f"[VerificationMemory] family lookup failed for claim_id={claim_id}: {type(exc).__name__}: {exc}")

        if family_id:
            with get_connection() as conn:
                family_occurrences = repo.find_claim_occurrences_by_family(conn, family_id)
                family_occurrences = [
                    occ for occ in family_occurrences
                    if occ["claim_id"] != claim_id and occ["run_id"] != exclude_trace_id
                ][:MAX_HISTORICAL_OCCURRENCES]
                for occ in family_occurrences:
                    historical_evidence = repo.list_evidence_for_claim(conn, occ["claim_id"])
                    results.extend(_reconstruct_evidence(historical_evidence, claim_id, claim_text))
            if results:
                match_kind = "family"

    if verbose and log:
        log(
            f"[VerificationMemory] lookup claim_id={claim_id} "
            f"content_hash={(content_hash or '-')[:12]} "
            f"match={match_kind} "
            f"hits={len(occurrences) + len(family_occurrences)} evidence_reconstructed={len(results)}"
        )

    return results


# ============================================================
# P10 (Этап 4G-2): FAMILY-SCOPED HISTORICAL EVIDENCE READ PATH
# ============================================================
#
# READ-ONLY. Nothing below is wired into the live retrieval/verification
# pipeline yet (no production caller exists — см. Этап 4G brief §16/17:
# "gate Phase12" и любая activation остаются отдельным, будущим
# решением). This is deliberately just a read capability over data that
# ALREADY exists (claim_occurrence.family_id, populated since Этап 4C's
# ordering fix).

def compute_stable_root(observation: Dict[str, Any]) -> Optional[tuple]:
    """
    P10 (Этап 4G-2): stable, CROSS-RUN root identity for one evidence
    observation.

    Deliberately NOT source_cluster_id — Этап 4F's Finding Y: cluster
    ids are `f"sc_{root_evidence_id}"`, recomputed fresh every cycle
    from a RANDOM per-fetch evidence_id (agent/source_clustering.py),
    so the same URL fetched in two different requests gets a DIFFERENT
    source_cluster_id. Uses canonicalized source_uri instead
    (SharedFetchCache.canonicalize — the SAME normalization already
    used for exact-URL-dedup / processed-source-reuse, not a second
    one) — source_uri never changes across reuse, only `route` does.

    A route="local_memory" observation is a REPLAY of an earlier
    internet observation of the SAME source_uri, not a new one — so it
    must resolve to the SAME root as the original. origin_route (which
    the origin-chain walk keeps correct across multiple reuse hops)
    tells us what the TRUE original channel was.

    Returns None — "not countable as an independent root in V1" — for
    any observation whose ultimate channel isn't "internet".
    network_node/ai_chat have no reliable stable identity yet
    (node_id/validator_id/model_id are still unpopulated placeholders,
    Этап 3 §13) and are deliberately not guessed at here.
    """
    route = observation.get("route")
    origin_route = observation.get("origin_route")

    effective_channel = origin_route if (route == "local_memory" and origin_route) else route

    if effective_channel != "internet":
        return None

    uri = observation.get("source_uri")
    if not uri:
        return None

    return ("internet", SharedFetchCache.canonicalize(uri))


def get_family_historical_evidence(
    family_id: str,
    log=None,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    P10 (Этап 4G-2): RAW historical evidence observations for every
    claim occurrence ever linked into this semantic_family_id (Level 2
    identity), across EVERY historical run (no LIMIT, same "union
    across all past runs" shape as get_historical_web_urls() — a
    DIFFERENT job, content_hash-scoped, left untouched).

    Returns raw observations, NEVER an aggregated verdict — the caller
    decides what (if anything) to do with them. Each dict carries only
    fields that already genuinely exist on a persisted evidence_
    relation/source_observation:

        semantic_family_id, claim_id, trace_id, evidence_id, relation,
        source_uri, route, origin_route, observed_at,
        origin_observed_at, source_cluster_id, origin_source_cluster_id,
        directness, evidence_eligible, evidence_role, source_class,
        retrieval_origin, origin_trace_id, stable_root.
    """
    with get_connection() as conn:
        occurrences = repo.find_claim_occurrences_by_family(conn, family_id)

        results: List[Dict[str, Any]] = []
        for occ in occurrences:
            for ev in repo.list_evidence_for_claim(conn, occ["claim_id"]):
                observation = {
                    "semantic_family_id": family_id,
                    "claim_id": occ["claim_id"],
                    "trace_id": occ["run_id"],
                    "evidence_id": ev.get("evidence_id"),
                    "relation": ev.get("relation"),
                    "source_uri": ev.get("source_uri"),
                    "route": ev.get("route"),
                    "origin_route": ev.get("origin_route"),
                    "observed_at": _dt_to_unix(ev.get("observed_at")),
                    "origin_observed_at": _dt_to_unix(ev.get("origin_observed_at")),
                    "source_cluster_id": ev.get("source_cluster_id"),
                    "origin_source_cluster_id": ev.get("origin_source_cluster_id"),
                    "directness": ev.get("directness"),
                    "evidence_eligible": ev.get("evidence_eligible"),
                    "evidence_role": ev.get("evidence_role"),
                    "source_class": ev.get("source_class"),
                    "retrieval_origin": ev.get("retrieval_claim_id") and "claim_specific" or None,
                    "origin_trace_id": ev.get("origin_trace_id"),
                }
                observation["stable_root"] = compute_stable_root(observation)
                results.append(observation)

    if verbose and log:
        log(
            f"[VerificationMemory] family_history family_id={family_id} "
            f"occurrences={len(occurrences)} observations={len(results)}"
        )

    return results


def get_family_historical_claims(
    family_id: str,
    log=None,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    "Живая память" (owner request): CLAIM-LEVEL historical summary for
    this semantic family — one row per distinct historical claim
    occurrence ever linked into it, newest first. Sibling to
    get_family_historical_evidence() above, which returns EVIDENCE-
    relation-level detail instead — this one exists because a caller
    that wants to say "here's what I concluded last time" needs the
    historical claim's own text/status/confidence and the ORIGINAL
    question that produced it.

    Returns raw historical facts only, never a verdict about whether
    anything "changed" — that comparison is the caller's job (see
    agent/claim_history_note.py::build_claim_history_notes(), the one
    current consumer). Each dict:
        semantic_family_id, claim_id, trace_id, query (the ORIGINAL
        question text that produced this historical claim),
        claim_text, verification_status, claim_confidence, observed_at.
    """
    with get_connection() as conn:
        occurrences = repo.find_claim_occurrences_by_family(conn, family_id)

        results: List[Dict[str, Any]] = []
        for occ in occurrences:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT qo.raw_text FROM verification_run vr "
                    "JOIN question_occurrence qo ON qo.occurrence_id = vr.occurrence_id "
                    "WHERE vr.run_id=%s",
                    (occ["run_id"],),
                )
                q_row = cur.fetchone()

            results.append({
                "semantic_family_id": family_id,
                "claim_id": occ["claim_id"],
                "trace_id": occ["run_id"],
                "query": (q_row["raw_text"] if q_row else None),
                "claim_text": occ.get("claim_text"),
                "verification_status": occ.get("verification_status"),
                "claim_confidence": occ.get("claim_confidence"),
                "observed_at": _dt_to_unix(occ.get("occurrence_observed_at")),
            })

    if verbose and log:
        log(
            f"[VerificationMemory] family_historical_claims family_id={family_id} "
            f"occurrences={len(occurrences)} claims={len(results)}"
        )

    return results
