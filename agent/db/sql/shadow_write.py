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
import uuid
from typing import Any, Callable, Optional

from agent.db.sql.connection import get_connection, SqlUnavailable
import agent.db.sql.repositories as repo
import agent.relationship_memory as relationship_memory

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


def shadow_record_decision_event(
    event_type: str, trace_id: str, entity_type: str, entity_id: str,
    verdict: str = "", reason: str = "", domain: str = "general",
    confidence: float = 0.0, delta: float = 0.0,
    delta_factors: Optional[dict] = None, meta: Optional[dict] = None,
    parent_event_id: Optional[str] = None, duration_ms: Optional[int] = None,
    policy_snapshot: Optional[dict] = None, policy_version: Optional[str] = None,
    orchestrator_version: Optional[str] = None, log=None, verbose: bool = False,
) -> None:
    """The production entry point for the "живая память" decision
    ledger (owner request: "почему YANDI так решила... ни я, ни ты, ни
    следующие поколения не имеют прав для изменения").

    Signature-compatible with agent.orch_reputation.add_decision_event()'s
    dead stub — SAME parameter names, SAME positional/keyword shape,
    confirmed against every real call site in orchestrator_v2.py/
    pipeline.py/writeback.py (grepped: only event_type, trace_id,
    entity_type, entity_id, verdict, reason, domain, meta, confidence,
    delta, delta_factors are ever actually passed) — so reconnecting
    production call sites only requires changing their IMPORT line
    (`from agent.orch_reputation import add_decision_event` ->
    `from agent.db.sql.shadow_write import shadow_record_decision_event
    as add_decision_event`), never the call sites themselves.

    event_id is generated HERE (uuid4 hex, same shape agent/orch_ledger.
    py's own DecisionEvent already used) — no existing caller ever
    supplied one, since the stub never accepted or needed it.

    Requires trace_id to already exist as a verification_run.run_id row
    — callers MUST record the question/run (shadow_record_question_and_
    run) before their FIRST decision event of a request, never after
    (an FK violation here fails open exactly like every other shadow
    write, but silently losing the FIRST event of every run — the one
    literally named "DecisionStarted" — would defeat the whole point;
    see orchestrator_v2.py's own reordering fix for this)."""
    event_id = uuid.uuid4().hex

    def _do(conn):
        repo.record_decision_event(
            conn, event_id, trace_id, event_type, entity_type, entity_id, verdict, domain,
            confidence, delta, delta_factors, reason, meta, parent_event_id, duration_ms,
            policy_snapshot, policy_version, orchestrator_version,
        )

    _shadow(log, verbose, "record_decision_event", _do)
    return event_id


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

    origin_observation_id (the SQL-side replay-chain FK): resolved via
    repo.find_observation_id_for_replay(), using the JSON-side origin_
    trace_id a local_memory replay already carries (== the original
    run's SQL run_id). Resolves to None whenever the origin run has no
    matching SQL observation (predates SQL shadow-writing, DB was down
    then, etc.) — never fabricated, same as before this was wired.

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
                origin_observation_id = (
                    repo.find_observation_id_for_replay(conn, resource_id, ev.get("origin_trace_id"))
                    if route == "local_memory" else None
                )
                observation_id = repo.record_source_observation(
                    conn, resource_id, run_id, route,
                    origin_observation_id=origin_observation_id,
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


def shadow_record_claim_family(
    *, family_id: str, domain: str, canonical_text: str, claim_id: str,
    log=None, verbose: bool = False,
) -> None:
    """
    Wired into agent/orchestrator/claims/lifecycle.py::
    assign_claim_family_identity() — the exact point agent.
    claim_family_registry.ClaimFamilyRegistry.find_or_link_claim()
    already runs, BEFORE finalize_claim_trace_and_grounding() and its
    bulk shadow_record_claims_and_evidence() call.

    This ordering matters beyond mirroring the JSON write: claim_
    occurrence.family_id carries a FOREIGN KEY to claim_family(family_
    id) (schema.py). shadow_record_claims_and_evidence() inserts claim_
    occurrence rows with family_id set whenever a claim already has
    semantic_family_id — on a real live DB, without this function
    running first in the same run, every one of those inserts would hit
    an FK violation and the whole claim_occurrence row would silently
    fail to shadow-write (fail-open swallows the exception, per design,
    but that would mean NO claim occurrences persist for any claim that
    got a family — a correctness gap, not a cosmetic one). Calling this
    here closes it: get_or_create_claim_family() commits in its own
    transaction before the bulk path ever runs.

    canonical_text is write-once at the SQL layer (repo.get_or_create_
    claim_family uses INSERT IGNORE) — matches the confirmed current
    Python behavior (ClaimFamilyRegistry never rewrites an existing
    family's canonical_text either).
    """
    def _do(conn):
        repo.get_or_create_claim_family(conn, family_id, domain, canonical_text)
        repo.link_family_member(conn, family_id, claim_id)

    _shadow(log, verbose, "record_claim_family", _do)


def shadow_record_belief_assessment(
    *, belief_id: str, topic: str, statement: str, confidence: float,
    status: str, change_type: str, old_confidence: Optional[float] = None,
    new_confidence: Optional[float] = None, reason: Optional[str] = None,
    run_id: Optional[str] = None, log=None, verbose: bool = False,
) -> None:
    """
    Wired into agent/belief_manager.py at each of its 5 existing
    Belief.history.append(...) call sites (add_belief's create path,
    _apply_decay, _update_existing, challenge_belief, supersede_belief)
    — mirrors the JSON history[] entry being appended, in the same
    transaction as the belief's current-state upsert (mandate §17:
    BELIEF is mutable current state; BELIEF_ASSESSMENT_HISTORY is
    append-only, never overwritten — matches confirmed current
    beliefs.json semantics exactly, doesn't invent a new one).

    change_type must be one of the 5 REAL values agent/belief_manager.py
    ever writes to Belief.history[]["change"] — 'created', 'decayed',
    'updated', 'revised', 'superseded' (schema.py's ENUM was corrected
    to match these exactly; the original 5A design had guessed at
    different labels, see schema.py's own PREVIOUS AUDIT CORRECTION
    comment).

    run_id is None at every current call site (see repo.record_belief_
    assessment()'s docstring for why threading it through Belief-
    Manager's public API is out of scope for this pass) — honest NULL,
    not fabricated.
    """
    def _do(conn):
        repo.upsert_belief(conn, belief_id, topic, statement, confidence, status)
        repo.record_belief_assessment(
            conn, belief_id, change_type, old_confidence, new_confidence, reason, run_id,
        )

    _shadow(log, verbose, "record_belief_assessment", _do)


def shadow_record_recheck_event(
    *, family_id: str, outcome: str, run_id: Optional[str] = None,
    trigger_reason: Optional[str] = None, started_at=None, reason: Optional[str] = None,
    domain: Optional[str] = None, canonical_text: Optional[str] = None,
    log=None, verbose: bool = False,
) -> None:
    """
    Wired into agent/family_dependency_graph.py::FamilyDependencyGraph.
    record_recheck() — all 3 of agent/dependency_recheck.py's call sites
    (no_belief / success / error outcomes) go through that one method.

    domain/canonical_text (when the caller has them — dependency_
    recheck.py always does, from the family it already looked up)
    trigger a defensive get_or_create_claim_family() first, in the SAME
    transaction: recheck_event.family_id carries an FK to claim_family
    (family_id), and a recheck can legitimately target a family this
    SQL layer never saw created (e.g. it was created while SQL was
    unconfigured, or before this migration existed) — without this,
    that FK violation would silently drop the whole recheck_event, the
    same class of gap shadow_record_claim_family() already closes for
    claim_occurrence. INSERT IGNORE means this is a safe no-op when the
    family already exists.
    """
    def _do(conn):
        if domain and canonical_text:
            repo.get_or_create_claim_family(conn, family_id, domain, canonical_text)
        repo.record_recheck_event(conn, family_id, outcome, run_id, trigger_reason, started_at, reason)

    _shadow(log, verbose, "record_recheck_event", _do)


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


def shadow_record_ai_observation(
    *, run_id: Optional[str], provider: str, model_id: str,
    prompt_identity: Optional[str], answer_excerpt: Optional[str],
    reported_sources: Optional[list] = None,
    observed_at=None,
    provenance_mode_reported: str = "UNKNOWN",
    live_search_used_reported: str = "UNKNOWN",
    provenance_parse_status: str = "missing",
    log=None, verbose: bool = False,
) -> Optional[int]:
    """Persist a raw external-AI utterance as reported provenance only.

    This writes AI_OBSERVATION / AI_REPORTED_SOURCE, never
    SOURCE_RESOURCE / EVIDENCE_RELATION; reported URLs are not verified
    provenance roots until the agent resolves them later.
    """
    def _do(conn):
        obs_id = repo.record_ai_observation(
            conn,
            provider=provider,
            model_id=model_id,
            run_id=run_id,
            prompt_identity=prompt_identity,
            answer_excerpt=answer_excerpt,
            provenance_mode_reported=provenance_mode_reported,
            live_search_used_reported=live_search_used_reported,
            provenance_parse_status=provenance_parse_status,
            observed_at=observed_at,
        )
        for idx, source in enumerate(reported_sources or [], 1):
            if isinstance(source, dict):
                reported_name = source.get("title") or source.get("name")
                reported_uri = source.get("url") or source.get("uri")
            else:
                reported_name = None
                reported_uri = str(source)
            repo.record_ai_reported_source(
                conn,
                obs_id,
                idx,
                reported_name,
                reported_uri,
            )
        return obs_id

    return _shadow(log, verbose, "record_ai_observation", _do)


# ============================================================
# Relationship/character state — owner mandate: "мне нужен у неё
# характер, она обидчива". Same fail-open discipline as every other
# shadow write here: pet/chat_local.py's reply must never break because
# this graph is unreachable — a chat with no memory of being offended
# is a degraded feature, not a crashed one.
# ============================================================

def shadow_add_grievance(
    *, user_id: str, event_type: str, description: str, severity: float,
    context: Optional[dict] = None, log=None, verbose: bool = False,
) -> Optional[str]:
    def _do(conn):
        return relationship_memory.add_grievance(conn, user_id, event_type, description, severity, context)

    return _shadow(log, verbose, "add_grievance", _do)


def shadow_acknowledge_apology(
    *, grievance_id: str, sincerity: float, log=None, verbose: bool = False,
) -> Optional[bool]:
    def _do(conn):
        return relationship_memory.acknowledge_apology(conn, grievance_id, sincerity)

    return _shadow(log, verbose, "acknowledge_apology", _do)


def shadow_progress_healing(*, grievance_id: str, log=None, verbose: bool = False) -> Optional[bool]:
    def _do(conn):
        return relationship_memory.progress_healing(conn, grievance_id)

    return _shadow(log, verbose, "progress_healing", _do)


def shadow_get_relationship_context(*, user_id: str, log=None, verbose: bool = False) -> Optional[dict]:
    """Read-only: the RAW FACTS of the most severe active grievance (if
    any) — description/severity/status, nothing interpreted — for
    pet/chat_local.py to state plainly in the system prompt. Returns
    None (not an empty dict) both when SQL is unreachable AND when
    there is genuinely no active grievance — the caller treats both
    identically (nothing to mention), so this distinction doesn't need
    to survive the shadow-write boundary."""
    def _do(conn):
        grievance = relationship_memory.most_severe_active_grievance(conn, user_id)
        if not grievance:
            return None
        return {"grievance_id": grievance["id"], **relationship_memory.memory_facts(grievance)}

    return _shadow(log, verbose, "get_relationship_context", _do)
