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
