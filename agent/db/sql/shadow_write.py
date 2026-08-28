"""
agent/db/sql/shadow_write.py — the ONLY way production code should call
into the SQL layer during the shadow-dual-write phase (mandate §39).

Every function here is FAIL-OPEN by construction: it never raises, it
never blocks the JSON canonical write path, and it never mutates
anything the caller passed in. A DB outage, a missing credential, a
schema mismatch, or any other SQL-layer failure degrades silently to
"the shadow write didn't happen this time" — logged if a logger was
given, never more than that (mandate §44: "Shadow SQL failure на этапе
dual-write НЕ должен ломать production JSON answer path").

Wired into agent/orchestrator_v2.py (question+run start) and agent/
orchestrator/response/writeback.py (answer+assessment+run completion)
— safe to wire even with NO live database, because the fail-open
contract above is itself proven (agent/db_sql_shadow_write_regression_
test.py) against the REAL current "unconfigured" state, not a
simulation: every call below costs one is_configured() env-var check
(microseconds) and then returns None. Claim/evidence-level wiring
(shadow_record_claim/shadow_record_evidence) is NOT wired into the
production call graph yet — deliberately staged separately, see the
final report's "READY / NOT READY" section.
"""
from __future__ import annotations

import subprocess
from typing import Any, Callable, Optional

from agent.db.sql.connection import get_connection, SqlUnavailable
import agent.db.sql.repositories as repo

_PIPELINE_VERSION_CACHE: Optional[str] = None


def pipeline_version() -> Optional[str]:
    """Short git commit hash, computed once and cached — NOT a per-
    request subprocess call. None outside a git checkout (never raises)."""
    global _PIPELINE_VERSION_CACHE
    if _PIPELINE_VERSION_CACHE is None:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=2, cwd=__file__.rsplit("/agent/", 1)[0],
            )
            _PIPELINE_VERSION_CACHE = out.stdout.strip() or "unknown"
        except Exception:
            _PIPELINE_VERSION_CACHE = "unknown"
    return _PIPELINE_VERSION_CACHE


def _shadow(log, verbose: bool, label: str, fn: Callable[[Any], Any]) -> Optional[Any]:
    """Runs fn(conn) inside one transaction; commits on success, rolls
    back and swallows on ANY exception (SqlUnavailable or otherwise —
    a repository bug must not be able to break the JSON path either).
    Returns fn's result, or None if the shadow write didn't happen."""
    try:
        with get_connection(autocommit=False) as conn:
            try:
                result = fn(conn)
                conn.commit()
                if verbose and log:
                    log(f"[SqlShadow] {label} OK")
                return result
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if verbose and log:
                    log(f"[SqlShadow] {label} FAILED (rolled back): {e}")
                return None
    except SqlUnavailable as e:
        if verbose and log:
            log(f"[SqlShadow] {label} SKIPPED (SQL unavailable): {e}")
        return None
    except Exception as e:
        # Defensive: get_connection() itself is documented to only ever
        # raise SqlUnavailable, but a shadow layer must survive being
        # wrong about that too.
        if verbose and log:
            log(f"[SqlShadow] {label} SKIPPED (unexpected: {e})")
        return None


def shadow_record_question_and_run(
    *, raw_text: str, run_id: str, started_at, web_enabled: bool,
    validation_enabled: bool, pipeline_version: Optional[str],
    anonymized_text: Optional[str] = None, session_id: Optional[str] = None,
    log=None, verbose: bool = False,
) -> Optional[dict]:
    def _do(conn):
        ids = repo.resolve_question(conn, raw_text, anonymized_text, started_at, session_id)
        repo.start_run(
            conn, run_id, ids["occurrence_id"], started_at,
            web_enabled, validation_enabled, pipeline_version,
        )
        return ids

    return _shadow(log, verbose, "record_question_and_run", _do)


def shadow_complete_run(
    *, run_id: str, question_id: Optional[int], delivered_answer_text: str,
    completed_at, canonical_trust: str, synthesizer_strand: Optional[str] = None,
    trust_gate_strand: Optional[str] = None, diverged: bool = False,
    stricter_strand: Optional[str] = None, reason: Optional[str] = None,
    log=None, verbose: bool = False,
) -> None:
    def _do(conn):
        if question_id is None:
            return
        answer_id = repo.record_answer_version(conn, question_id, delivered_answer_text, run_id, completed_at)
        repo.record_answer_assessment(
            conn, answer_id, run_id, canonical_trust,
            synthesizer_strand, trust_gate_strand, diverged, stricter_strand, reason, completed_at,
        )
        repo.complete_run(conn, run_id, completed_at, final_answer_id=answer_id)

    _shadow(log, verbose, "complete_run", _do)


def shadow_fail_run(*, run_id: str, failed_stage: str, error_class: str, log=None, verbose: bool = False) -> None:
    def _do(conn):
        repo.fail_run(conn, run_id, failed_stage, error_class, outcome="failed")
        repo.record_run_error(conn, run_id, failed_stage, error_class)

    _shadow(log, verbose, "fail_run", _do)


def shadow_record_claim(
    *, claim_id: str, run_id: str, claim_text: str, content_hash: Optional[str],
    claim_type: Optional[str], claim_confidence: Optional[float], verification_status: Optional[str],
    family_id: Optional[str], family_domain: Optional[str], family_canonical_text: Optional[str],
    query_context: Optional[str], support_count: int, contradiction_count: int,
    log=None, verbose: bool = False,
) -> None:
    def _do(conn):
        if family_id and family_canonical_text:
            repo.get_or_create_claim_family(conn, family_id, family_domain or "general", family_canonical_text)
            repo.link_family_member(conn, family_id, claim_id)
        repo.record_claim_occurrence(
            conn, claim_id, run_id, claim_text, content_hash, claim_type, claim_confidence,
            verification_status, family_id, query_context, support_count, contradiction_count,
        )

    _shadow(log, verbose, "record_claim", _do)


def shadow_record_claims_and_evidence(
    *, run_id: str, claims_data: list, evidence_data: list, log=None, verbose: bool = False,
) -> None:
    """
    Bulk shadow write for a run's FINALIZED claims + evidence, meant to
    be called from the exact point agent/orchestrator/claims/status.py::
    finalize_claim_trace_and_grounding() calls agent.verification_
    memory.persist_verification_evidence() — claims_data/evidence_data
    are already in their final JSON-persisted shape there. ONE
    transaction for the whole batch (mandate §45: never a connection/
    commit per row).

    Resource identity mapping (the §6 correction, applied to REAL
    runtime evidence dict shape):
        observation_route = ev["route"]            -- all 5 channels valid
        resource_type      = ev["origin_route"] if ev["route"] ==
                              "local_memory" else ev["route"]
                              -- a replay's resource is what it ORIGINALLY
                              was, never "local_memory" itself.

    Only evidence with resource_type == "internet" and a real source_uri
    is shadow-written in this pass — network_node/ai_chat/local_model
    resources have no canonical identity source yet (V1 scope, same as
    agent.verification_memory.compute_stable_root()).

    KNOWN LIMITATION (documented, not silently papered over):
    origin_observation_id is NOT populated here — reconstructing which
    SQL-side observation a local_memory replay pointed at would need a
    resource+run+time lookup this pass didn't build. The JSON-side
    provenance chain (origin_route/origin_trace_id/...) is unaffected;
    only the SQL replay-chain FK stays NULL for now. See
    agent/db/sql/MIGRATION_STATUS.md.

    family_id linkage (claim_family/family_member) is deliberately NOT
    written from this bulk path either — claims_data doesn't carry
    canonical_text, and get_or_create_claim_family() needs it. That
    belongs at claim_family_registry.py's own find_or_link_claim() call
    site, a separate, deliberate wiring decision (not done here).
    """
    def _do(conn):
        evidence_by_id = {
            ev.get("evidence_id"): ev for ev in (evidence_data or []) if ev.get("evidence_id")
        }

        for claim in claims_data or []:
            claim_id = claim.get("claim_id")
            if not claim_id:
                continue

            repo.record_claim_occurrence(
                conn, claim_id, run_id, claim.get("claim_text", ""),
                claim.get("content_hash"), claim.get("claim_type"),
                claim.get("claim_confidence"), claim.get("verification_status"),
                claim.get("semantic_family_id"), claim.get("query_context"),
                claim.get("support_count", 0), claim.get("contradiction_count", 0),
            )

            for rel in claim.get("evidence_relations", []) or []:
                ev = evidence_by_id.get(rel.get("evidence_id"))
                if not ev:
                    continue

                route = ev.get("route") or "internet"
                resource_type = ev.get("origin_route") if route == "local_memory" else route
                if resource_type != "internet":
                    continue  # V1 scope: only internet resources have canonical identity
                canonical_uri = ev.get("source_uri")
                if not canonical_uri:
                    continue

                resource_id = repo.get_or_create_resource(
                    conn, "internet", canonical_uri=canonical_uri, observed_at=ev.get("observed_at"),
                )
                observation_id = repo.record_source_observation(
                    conn, resource_id, run_id, route,
                    origin_observation_id=None,  # see docstring: known limitation
                    observed_at=ev.get("observed_at"),
                    source_class=ev.get("source_class"),
                    quality_score=ev.get("quality_score"),
                    content_excerpt=(ev.get("content_excerpt") or "")[:2000],
                )
                relation = rel.get("relation")
                if relation not in ("supports", "contradicts", "uncertain", "unrelated"):
                    continue
                repo.record_evidence_relation(
                    conn, claim_id, observation_id, relation,
                    directness=rel.get("directness"),
                    evidence_eligible=bool(rel.get("evidence_eligible", False)),
                    evidence_role=rel.get("evidence_role"),
                    counted_via=rel.get("counted_via"),
                )

    _shadow(log, verbose, "record_claims_and_evidence", _do)


def shadow_reconcile_stale_runs(*, older_than_seconds: int = 3600, log=None, verbose: bool = False) -> Optional[int]:
    """
    Should be called once at process/daemon startup (NOT wired into any
    startup path yet — this codebase's actual daemon entrypoint wasn't
    touched in this pass, see the final report's READY/NOT READY
    section). Returns the number of runs reconciled, or None if the SQL
    layer isn't reachable.
    """
    return _shadow(log, verbose, "reconcile_stale_runs", lambda conn: repo.reconcile_stale_running_runs(conn, older_than_seconds))


def shadow_record_evidence(
    *, claim_id: str, run_id: str, resource_type: str, canonical_uri: Optional[str],
    observation_route: str, origin_observation_id: Optional[int], observed_at,
    source_class: Optional[str], quality_score: Optional[float], content_excerpt: Optional[str],
    relation: str, directness: Optional[float], evidence_eligible: bool,
    evidence_role: Optional[str], counted_via: Optional[str],
    log=None, verbose: bool = False,
) -> None:
    def _do(conn):
        resource_id = repo.get_or_create_resource(
            conn, resource_type, canonical_uri=canonical_uri, observed_at=observed_at,
        )
        observation_id = repo.record_source_observation(
            conn, resource_id, run_id, observation_route, origin_observation_id,
            observed_at, source_class, quality_score, content_excerpt,
        )
        repo.record_evidence_relation(
            conn, claim_id, observation_id, relation, directness,
            evidence_eligible, evidence_role, counted_via,
        )

    _shadow(log, verbose, "record_evidence", _do)
