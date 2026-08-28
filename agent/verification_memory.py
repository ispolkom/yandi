"""
agent/verification_memory.py — Этап 3 (P5): LOCAL MEMORY as one of
YANDI's five epistemic channels (local_model / local_memory /
network_node / internet / ai_chat).

Source of truth stays registry/dataset/orch_traces/*.jsonl (agent.
orch_tracer.Trace, unchanged format, just extended with new additive
fields — see agent/orch_schemas.py::EvidenceRecord). This module adds
exactly two things on top of that, per the explicit decision:

    SAVE — collect_verification_evidence_ids() / persist_verification_
           evidence(): find which evidence in the runtime pool actually
           participated in claim verification (has an evidence_relation
           on some claim — "FETCHED SOURCE != VERIFICATION EVIDENCE",
           not the whole discovery pool) and persist a full EvidenceRecord
           for each, wired at the EXISTING trace.add_claim_raw() save
           point (agent/orchestrator/claims/status.py).

    LOAD — lookup_historical_evidence(): given the CURRENT claim's
           content_hash (the only lookup key this v1 actually uses at
           runtime — see that function's docstring for why the
           semantic_family_id fallback column exists in the index but
           isn't live-queried yet), find a prior verification of the
           "same" claim and reconstruct its evidence as ordinary runtime
           evidence dicts, tagged route="local_memory"/from_memory=True
           with the original provenance chain preserved (origin_route/
           origin_trace_id/origin_observed_at — P4 §12: reuse is a new
           ROUTE, never a new SOURCE). These are fed into the EXISTING
           Mapper -> NLI the same way fresh PASS2 evidence is — the
           historical relation/verdict is NEVER copied as-is. See
           agent/orchestrator/claims/async_pipeline.py's MEMORY PASS
           block for the caller.

registry/index.db's claim_verification_index table (agent/db/schema.py)
is a pure locator accelerator — (content_hash | semantic_family_id) ->
(jsonl_file, byte_offset) — never a second copy of Trust/relations/
evidence content. If it's ever lost/corrupted, nothing is lost that
isn't recoverable by re-scanning the JSONL trace files; it is not
itself a store of record.

Explicitly NOT this module's job (later stage, per the Этап 3 brief):
excluding already-processed URLs from NEW web retrieval (WebBudget
candidate selection is untouched); node/AI-chat channels (node_id/
validator_id/model_id stay None — schema-prepared, not activated).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.claim_identity import compute_claim_content_hash
from agent.db.schema import INDEX_SCHEMA
from agent.orch_schemas import EvidenceRecord
from agent.orch_tracer import TRACES_DIR

BASE = Path(__file__).parent.parent
INDEX_DB = BASE / "registry" / "index.db"

# How many prior verifications of "the same" claim to reconstruct
# evidence from. 1 = only the single most recent occurrence — matches
# the P4 "bounded verification per cycle, accumulating knowledge across
# cycles" philosophy (a later cycle's SAVE will itself become the new
# most-recent occurrence for the cycle after that), not an attempt to
# gather every historical mention at once.
MAX_HISTORICAL_OCCURRENCES = 1


def _db_connect() -> sqlite3.Connection:
    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(INDEX_DB))
    con.executescript(INDEX_SCHEMA)
    con.commit()
    return con


# ============================================================
# SAVE
# ============================================================

def index_trace(
    trace: Any,
    jsonl_file: str,
    byte_offset: int,
    log=None,
    verbose: bool = False,
) -> int:
    """
    Locator-only indexing (P4 §7): one row per claim that has a
    content_hash, pointing at WHERE this trace's line lives, not at
    what it contains. Called from orch_tracer.py::DecisionTracer.
    save_trace() right after the JSONL append succeeds.

    Safe to call multiple times for the same (trace_id, claim_id) —
    PRIMARY KEY makes it idempotent (INSERT OR REPLACE).
    """
    rows = [
        (
            trace.trace_id,
            c.claim_id,
            c.content_hash,
            c.semantic_family_id,
            jsonl_file,
            byte_offset,
            trace.timestamp,
        )
        for c in getattr(trace, "claims", [])
        if getattr(c, "content_hash", None)
    ]

    if not rows:
        return 0

    con = _db_connect()
    try:
        con.executemany(
            """
            INSERT OR REPLACE INTO claim_verification_index
                (trace_id, claim_id, content_hash, semantic_family_id,
                 jsonl_file, byte_offset, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        con.commit()
    finally:
        con.close()

    if verbose and log:
        log(f"[VerificationMemory] indexed {len(rows)} claim(s) from trace={trace.trace_id}")

    return len(rows)


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
    persistence path. Fixes the "27 runtime evidence -> 3 persisted"
    gap (P4 §2): the old code only ever called trace.add_source() for
    the first 3 stage-6 refutation snippets; PASS2/memory evidence
    never reached the trace at all.

    Mutates `trace` in place (trace.evidence). Returns the count added.
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

def _query_index(content_hash: Optional[str], semantic_family_id: Optional[str]) -> List[sqlite3.Row]:
    con = _db_connect()
    con.row_factory = sqlite3.Row
    try:
        if content_hash:
            rows = con.execute(
                """
                SELECT * FROM claim_verification_index
                WHERE content_hash = ?
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (content_hash, MAX_HISTORICAL_OCCURRENCES),
            ).fetchall()
            if rows:
                return rows

        if semantic_family_id:
            rows = con.execute(
                """
                SELECT * FROM claim_verification_index
                WHERE semantic_family_id = ?
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (semantic_family_id, MAX_HISTORICAL_OCCURRENCES),
            ).fetchall()
            return rows

        return []
    finally:
        con.close()


def _read_trace_line(jsonl_file: str, byte_offset: int) -> Optional[Dict[str, Any]]:
    path = TRACES_DIR / jsonl_file
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            f.seek(byte_offset)
            line = f.readline()
        if not line.strip():
            return None
        return json.loads(line)
    except Exception:
        return None


def _reconstruct_evidence(
    trace_dict: Dict[str, Any],
    historical_claim_id: str,
    current_claim_id: str,
    current_claim_text: str,
) -> List[Dict[str, Any]]:
    """
    Runtime-shape evidence dicts (same keys claim_evidence_retriever.py's
    PASS2 evidence uses) reconstructed from a historical trace's
    ClaimRecord/EvidenceRecord — tagged route="local_memory"/
    from_memory=True, owned by the CURRENT claim (retrieval_claim_id=
    current_claim_id, retrieval_origin="claim_specific" — reuses the
    EXISTING ownership-gate in claim_evidence_mapper.py, P4 §3), with
    the original provenance chain preserved (origin_* — P4 §12).

    Deliberately does NOT copy `relation` — the historical relation is
    audit-only, read directly off trace_dict by the caller if needed;
    reconstructed evidence here carries no relation field at all, so it
    is structurally impossible for it to be mistaken for an already-
    computed verdict downstream (P4 §9/§10).
    """
    historical_claim = next(
        (c for c in trace_dict.get("claims", []) if c.get("claim_id") == historical_claim_id),
        None,
    )
    if not historical_claim:
        return []

    evidence_by_id = {
        e.get("evidence_id"): e
        for e in trace_dict.get("evidence", [])
        if e.get("evidence_id")
    }

    linked_ids = {
        rel.get("evidence_id")
        for rel in historical_claim.get("evidence_relations", []) or []
        if rel.get("evidence_id")
    }

    reconstructed = []
    origin_trace_id = trace_dict.get("trace_id")
    origin_observed_at = trace_dict.get("timestamp")

    for ev_id in linked_ids:
        ev = evidence_by_id.get(ev_id)
        if not ev or not ev.get("content_excerpt"):
            continue

        reconstructed.append({
            "evidence_id": ev.get("evidence_id", ev_id),
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

            "route": "local_memory",
            "from_memory": True,
            "origin_route": ev.get("route", "internet") or "internet",
            "origin_trace_id": origin_trace_id,
            "origin_observed_at": origin_observed_at,
            "origin_source_cluster_id": ev.get("source_cluster_id"),
            # source_cluster_id intentionally left unset here — the
            # CURRENT cycle's assign_source_clusters() recomputes it
            # fresh over whatever's actually present this cycle (P4
            # §4's "tracer only saves, never reclusters" is about SAVE;
            # this is a normal LOAD-time evidence candidate, clustered
            # like any other this cycle, so reuse never fabricates a
            # new independent root — P4 §12).
        })

    return reconstructed


def lookup_historical_evidence(
    claim: Dict[str, Any],
    exclude_trace_id: Optional[str] = None,
    log=None,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    P5 LOAD entry point. content_hash EXACT match is the ONLY lookup
    path this v1 actually performs at runtime (P4 §5's PRIMARY).

    semantic_family_id is deliberately NOT used as a live fallback here,
    despite the index column existing (populated at SAVE time from
    claim.semantic_family_id when present) and _query_index() being able
    to search by it: the CURRENT claim does not have a semantic_family_id
    yet at this point in the pipeline (still only assigned later, in
    orchestrator/claims/lifecycle.py's belief-update block — well after
    PASS1/PASS2/this memory pass complete), and computing one HERE would
    mean calling ClaimFamilyRegistry-style matching against every
    persisted family (each comparison itself an embedding + LLM-judge
    call, agent/claim_semantic_identity_prototype.py::classify_claim_pair)
    — exactly the "не вводить новый embedding lookup" this v1 was told
    not to do. A cheap, non-network way to know the CURRENT claim's
    family before this point would need a real design change, deferred
    to a later stage, not silently worked around here.

    Returns [] on a memory miss — never fabricates a match.
    """
    content_hash = claim.get("content_hash") or compute_claim_content_hash(
        claim.get("claim_text", "")
    )
    claim_text = claim.get("claim_text", "")
    claim_id = claim.get("claim_id", "")

    rows = _query_index(content_hash, None)

    results: List[Dict[str, Any]] = []
    for row in rows:
        if exclude_trace_id and row["trace_id"] == exclude_trace_id:
            continue

        trace_dict = _read_trace_line(row["jsonl_file"], row["byte_offset"])
        if not trace_dict:
            continue

        results.extend(
            _reconstruct_evidence(trace_dict, row["claim_id"], claim_id, claim_text)
        )

    if verbose and log:
        log(
            f"[VerificationMemory] lookup claim_id={claim_id} "
            f"content_hash={(content_hash or '-')[:12]} "
            f"hits={len(rows)} evidence_reconstructed={len(results)}"
        )

    return results
