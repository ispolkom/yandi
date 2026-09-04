"""
agent/db/sql/repositories.py — write + read functions over the schema
in agent/db/sql/schema.py. Every function takes an already-open pymysql
connection (from agent.db.sql.connection.get_connection) — callers own
the connection/transaction lifecycle, this module never opens one
itself, so a caller can group several repository calls into one
transaction (mandate §29/§30).

WRITE functions are idempotent where the mandate requires it (§30):
resolve_question() finds-or-creates by canonical_hash; get_or_create_
resource() finds-or-creates by uri_hash; record_answer_version() only
inserts a new row when the hash actually changed. They are NOT
idempotent where the mandate explicitly says they must not be (§30:
"same source, same claim, different run — должны иметь право
существовать как разные historical observations") — record_claim_
occurrence()/record_source_observation()/record_evidence_relation()
always insert a new row, every call, on purpose.

READ functions implement the Local Memory read API (mandate §14/§33).
Names match the mandate's own suggested names where given.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

# Reuses the EXISTING claim-text normalizer for question identity too —
# NOT a second, incompatible normalizer (mandate §7). It happens to live
# in claim_identity.py but its behavior (NFC, casefold, whitespace
# collapse, trailing-punctuation strip) is generic text normalization,
# not claim-specific.
from agent.claim_identity import canonicalize_claim_text

# Controlled vocabulary for source_observation.rejection_reason — small
# and deliberate (mandate §13): epistemically meaningful reasons only,
# never raw scraper/transport diagnostics.
REJECTION_REASONS = {
    "unrelated", "no_content", "low_quality", "stoplisted",
    "transport_failed", "duplicate", "below_eligibility_threshold",
}


def _now() -> datetime:
    return datetime.utcnow()


def _coerce_datetime(value):
    """P0 FIX (found while investigating origin_observation_id, mandate
    §15): every timestamp this module accepts from a caller has to reach
    a DATETIME column. Two real production call sites pass a raw Unix-
    epoch float instead (agent.orch_tracer.Trace.timestamp, forwarded as
    started_at/asked_at from orchestrator_v2.py's shadow_record_question_
    and_run() call) — a bare float bound to a DATETIME column is not a
    valid MySQL datetime literal (pymysql just embeds the numeric value;
    the server rejects it), which the shadow layer's fail-open contract
    would have swallowed silently: EVERY question/run row would have
    failed to write on a real live DB, never caught because no live DB
    has existed yet to fail against. None passes through unchanged (the
    `X = _coerce_datetime(X) or _now()` idiom below still falls back to
    _now() for a genuinely absent timestamp); an already-correct datetime
    passes through unchanged too."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value)
    return value


def _question_hash(raw_text: str) -> str:
    return hashlib.sha256(canonicalize_claim_text(raw_text).encode("utf-8")).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ============================================================
# WRITE
# ============================================================

def resolve_question(
    conn, raw_text: str, anonymized_text: Optional[str], asked_at=None, session_id: Optional[str] = None,
) -> Dict[str, int]:
    """Find-or-create QUESTION by canonical_hash; ALWAYS insert a new
    QUESTION_OCCURRENCE (the raw text is never deduplicated — that's
    the whole point of splitting the two entities, mandate §7)."""
    asked_at = _coerce_datetime(asked_at) or _now()
    h = _question_hash(raw_text)

    with conn.cursor() as cur:
        cur.execute("SELECT question_id FROM question WHERE canonical_hash = %s", (h,))
        row = cur.fetchone()
        if row:
            question_id = row["question_id"]
        else:
            cur.execute(
                "INSERT INTO question (canonical_hash, first_asked_at) VALUES (%s, %s)",
                (h, asked_at),
            )
            question_id = cur.lastrowid

        cur.execute(
            "INSERT INTO question_occurrence "
            "(question_id, raw_text, anonymized_text, asked_at, session_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (question_id, raw_text, anonymized_text, asked_at, session_id),
        )
        occurrence_id = cur.lastrowid

    return {"question_id": question_id, "occurrence_id": occurrence_id}


def start_run(
    conn, run_id: str, occurrence_id: int, started_at=None,
    web_enabled: bool = False, validation_enabled: bool = False,
    pipeline_version: Optional[str] = None, schema_version: int = 1,
) -> None:
    started_at = _coerce_datetime(started_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO verification_run "
            "(run_id, occurrence_id, started_at, status, web_enabled, "
            " validation_enabled, pipeline_version, schema_version) "
            "VALUES (%s, %s, %s, 'running', %s, %s, %s, %s)",
            (run_id, occurrence_id, started_at, web_enabled, validation_enabled,
             pipeline_version, schema_version),
        )


def complete_run(conn, run_id: str, completed_at=None, final_answer_id: Optional[int] = None) -> None:
    completed_at = _coerce_datetime(completed_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE verification_run SET status='completed', completed_at=%s, "
            "final_answer_id=%s WHERE run_id=%s AND status='running'",
            (completed_at, final_answer_id, run_id),
        )


def fail_run(
    conn, run_id: str, failed_stage: str, error_class: str,
    outcome: str = "failed", completed_at=None,
) -> None:
    """outcome: 'failed' (unhandled exception) or 'aborted' (process died
    mid-run — see reconcile_stale_running_runs() below for how that
    second case is detected after the fact)."""
    assert outcome in ("failed", "aborted")
    completed_at = _coerce_datetime(completed_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE verification_run SET status=%s, completed_at=%s, "
            "failed_stage=%s, error_class=%s WHERE run_id=%s",
            (outcome, completed_at, failed_stage, error_class, run_id),
        )


def reconcile_stale_running_runs(conn, older_than_seconds: int = 3600) -> int:
    """Startup reconciliation (mandate §29): a run stuck in status=
    'running' for longer than a live request could plausibly take means
    the process died mid-run. Marks it aborted — NEVER completed, so no
    reader can mistake it for a finished verification. Returns the
    number of rows reconciled."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE verification_run SET status='aborted', completed_at=NOW() "
            "WHERE status='running' AND started_at < (NOW() - INTERVAL %s SECOND)",
            (older_than_seconds,),
        )
        return cur.rowcount


def record_answer_version(conn, question_id: int, answer_text: str, run_id: str, created_at=None) -> int:
    """Inserts a NEW version only if answer_hash differs from the
    question's current latest version — exact-hash v1 (mandate §9: no
    silent semantic-equivalence invention). Reuses the existing
    answer_id when the text is byte-identical to the latest version."""
    created_at = _coerce_datetime(created_at) or _now()
    h = _text_hash(answer_text)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT answer_id, answer_hash, version_number FROM answer_version "
            "WHERE question_id=%s ORDER BY version_number DESC LIMIT 1",
            (question_id,),
        )
        latest = cur.fetchone()

        if latest and latest["answer_hash"] == h:
            return latest["answer_id"]

        version_number = (latest["version_number"] + 1) if latest else 1
        supersedes_id = latest["answer_id"] if latest else None

        cur.execute(
            "INSERT INTO answer_version "
            "(question_id, version_number, answer_text, answer_hash, "
            " created_by_run_id, supersedes_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (question_id, version_number, answer_text, h, run_id, supersedes_id, created_at),
        )
        return cur.lastrowid


def record_answer_assessment(
    conn, answer_id: int, run_id: str, canonical_trust: str,
    synthesizer_strand: Optional[str] = None, trust_gate_strand: Optional[str] = None,
    diverged: bool = False, stricter_strand: Optional[str] = None,
    reason: Optional[str] = None, created_at=None,
) -> int:
    created_at = _coerce_datetime(created_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO answer_assessment "
            "(answer_id, run_id, synthesizer_strand, trust_gate_strand, "
            " canonical_trust, diverged, stricter_strand, reason, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (answer_id, run_id, synthesizer_strand, trust_gate_strand,
             canonical_trust, diverged, stricter_strand, reason, created_at),
        )
        return cur.lastrowid


def get_or_create_claim_family(conn, family_id: str, domain: str, canonical_text: str, created_at=None) -> None:
    """canonical_text is write-once (matches confirmed current Python
    behavior, agent/claim_family_registry.py) — INSERT IGNORE, never
    UPDATE, so a second call with different text for the same family_id
    is silently a no-op, not a rewrite."""
    created_at = _coerce_datetime(created_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO claim_family (family_id, domain, canonical_text, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (family_id, domain, canonical_text, created_at, created_at),
        )


def link_family_member(conn, family_id: str, claim_id: str, linked_at=None) -> None:
    linked_at = _coerce_datetime(linked_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO family_member (family_id, claim_id, linked_at) VALUES (%s, %s, %s)",
            (family_id, claim_id, linked_at),
        )


def list_claim_families_by_domain(conn, domain: str) -> List[Dict[str, Any]]:
    """Candidates for agent.claim_family_registry.ClaimFamilyRegistry.
    find_or_link_claim()'s own matching loop — same domain-scoped
    candidate set the retired JSON registry used
    (`[f for f in self.families if f.get("domain") == domain]`), same
    registry order (oldest first) so first-candidate-wins stays
    deterministic."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT family_id, canonical_text FROM claim_family WHERE domain=%s ORDER BY created_at ASC",
            (domain,),
        )
        return cur.fetchall()


def get_claim_family(conn, family_id: str) -> Optional[Dict[str, Any]]:
    """One family plus its members — mirrors the retired JSON record's
    own shape ({"family_id", "domain", "canonical_text", "members": [...],
    "created_at", "updated_at"}), except a member here is {"claim_id",
    "linked_at"} — family_member never stored claim_text redundantly
    the way the old JSON member dict did, and the one real caller
    (agent.dependency_recheck._belief_for_family()) only ever reads
    claim_id."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM claim_family WHERE family_id=%s", (family_id,))
        family = cur.fetchone()
    if not family:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT claim_id, linked_at FROM family_member WHERE family_id=%s ORDER BY linked_at ASC",
            (family_id,),
        )
        members = cur.fetchall()
    family["members"] = members
    return family


def record_claim_occurrence(
    conn, claim_id: str, run_id: str, claim_text: str, content_hash: Optional[str],
    claim_type: Optional[str], claim_confidence: Optional[float], verification_status: Optional[str],
    family_id: Optional[str], query_context: Optional[str],
    support_count: int = 0, contradiction_count: int = 0,
) -> None:
    """ALWAYS inserts — a claim occurrence belongs to exactly one run,
    never updated after the run (mandate §11)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO claim_occurrence "
            "(claim_id, run_id, claim_text, content_hash, claim_type, claim_confidence, "
            " verification_status, family_id, query_context, support_count, contradiction_count) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (claim_id, run_id, claim_text, content_hash, claim_type, claim_confidence,
             verification_status, family_id, query_context, support_count, contradiction_count),
        )


def get_or_create_resource(
    conn, resource_type: str, canonical_uri: Optional[str] = None,
    node_id: Optional[str] = None, validator_id: Optional[str] = None,
    model_id: Optional[str] = None, observed_at=None,
) -> int:
    """V1 activates only resource_type='internet' with canonical_uri
    set — node/validator/model identity columns are schema-ready but no
    caller in this codebase passes them yet (mandate §4)."""
    observed_at = _coerce_datetime(observed_at) or _now()
    uri_hash = _text_hash(canonical_uri) if canonical_uri else None

    with conn.cursor() as cur:
        if uri_hash:
            cur.execute("SELECT resource_id FROM source_resource WHERE uri_hash=%s", (uri_hash,))
            row = cur.fetchone()
            if row:
                return row["resource_id"]

        cur.execute(
            "INSERT INTO source_resource "
            "(resource_type, canonical_uri, uri_hash, node_id, validator_id, model_id, first_observed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (resource_type, canonical_uri, uri_hash, node_id, validator_id, model_id, observed_at),
        )
        return cur.lastrowid


def find_observation_id_for_replay(conn, resource_id: int, origin_run_id: Optional[str]) -> Optional[int]:
    """Resolves which SQL-side source_observation row a local_memory
    replay pointed at (mandate §15's origin_observation_id FK), using
    the JSON-side origin_trace_id the replay already carries
    (agent.verification_memory's reconstructed evidence dicts —
    origin_trace_id IS the original run's SQL run_id, same string).

    Returns None whenever the origin run never wrote a SQL observation
    for this resource — it predates SQL shadow-writing, the DB was
    unreachable at the time, or (rare) a resource+run pairing that
    genuinely never happened — same NULL result as before this
    function existed, never fabricated. When more than one observation
    of the same resource happened within the same origin run (e.g. a
    recheck), the earliest one is treated as the replay's origin —
    ambiguity here is a genuine V1 scope limit (mandate §14: not solving
    NLP-grade provenance disambiguation inside a persistence migration),
    not a correctness claim about which exact observation was replayed."""
    if not origin_run_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT observation_id FROM source_observation "
            "WHERE resource_id=%s AND run_id=%s ORDER BY observation_id ASC LIMIT 1",
            (resource_id, origin_run_id),
        )
        row = cur.fetchone()
        return row["observation_id"] if row else None


def record_source_observation(
    conn, resource_id: int, run_id: str, observation_route: str,
    origin_observation_id: Optional[int] = None, observed_at=None,
    source_class: Optional[str] = None, quality_score: Optional[float] = None,
    content_excerpt: Optional[str] = None, rejection_reason: Optional[str] = None,
) -> int:
    if rejection_reason is not None and rejection_reason not in REJECTION_REASONS:
        raise ValueError(f"rejection_reason {rejection_reason!r} not in controlled vocabulary {REJECTION_REASONS}")
    observed_at = _coerce_datetime(observed_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_observation "
            "(resource_id, run_id, observation_route, origin_observation_id, observed_at, "
            " source_class, quality_score, content_excerpt, rejection_reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (resource_id, run_id, observation_route, origin_observation_id, observed_at,
             source_class, quality_score, content_excerpt, rejection_reason),
        )
        return cur.lastrowid


def record_evidence_relation(
    conn, claim_id: str, observation_id: int, relation: str,
    directness: Optional[float] = None, evidence_eligible: bool = False,
    evidence_role: Optional[str] = None, counted_via: Optional[str] = None, created_at=None,
) -> int:
    created_at = _coerce_datetime(created_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO evidence_relation "
            "(claim_id, observation_id, relation, directness, evidence_eligible, "
            " evidence_role, counted_via, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (claim_id, observation_id, relation, directness, evidence_eligible,
             evidence_role, counted_via, created_at),
        )
        return cur.lastrowid


def record_run_error(conn, run_id: str, failed_stage: str, error_class: str, short_message: Optional[str] = None, created_at=None) -> None:
    created_at = _coerce_datetime(created_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO run_error (run_id, failed_stage, error_class, short_message, created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (run_id, failed_stage, error_class, (short_message or "")[:500], created_at),
        )


def record_decision_event(
    conn, event_id: str, run_id: str, event_type: str, entity_type: str, entity_id: str,
    verdict: Optional[str] = None, domain: Optional[str] = None,
    confidence: Optional[float] = None, delta: Optional[float] = None,
    delta_factors: Optional[Dict[str, Any]] = None, reason: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None, parent_event_id: Optional[str] = None,
    duration_ms: Optional[int] = None, policy_snapshot: Optional[Dict[str, Any]] = None,
    policy_version: Optional[str] = None, orchestrator_version: Optional[str] = None,
    created_at=None,
) -> None:
    """Owner request ("живая память... почему YANDI так решила"):
    APPEND-ONLY decision/reasoning ledger, in the same dedicated
    instance as everything else — never updated after the fact (mandate
    §16/§3: history is not disposable), and protected by the SAME
    GRANT+trigger wall as every other history table here (yandi_runtime
    has INSERT only, no UPDATE at all — see security_grants.py), unlike
    the pre-existing SQLite-based agent/orch_ledger.py, which has no
    access-control model beyond OS file permissions.

    `event_id` is the caller's own uuid4().hex (agent/orch_ledger.py's
    DecisionEvent already generates one per event) — passed in, never
    generated here, so a caller that wires parent_event_id can do so
    with a value it already knows, no round-trip needed.

    delta_factors/meta/policy_snapshot are JSON-serialized here (plain
    dict in, JSON text out) — this is the first JSON column in this
    schema; pymysql does not auto-serialize a dict for one, so this
    function owns that conversion rather than pushing it onto every
    caller."""
    created_at = _coerce_datetime(created_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO decision_event "
            "(event_id, run_id, event_type, entity_type, entity_id, verdict, domain, confidence, "
            " delta, delta_factors, reason, meta, parent_event_id, duration_ms, policy_snapshot, "
            " policy_version, orchestrator_version, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                event_id, run_id, event_type, entity_type, entity_id, verdict, domain, confidence, delta,
                json.dumps(delta_factors) if delta_factors is not None else None,
                reason,
                json.dumps(meta) if meta is not None else None,
                parent_event_id, duration_ms,
                json.dumps(policy_snapshot) if policy_snapshot is not None else None,
                policy_version, orchestrator_version, created_at,
            ),
        )


def get_decision_trace(conn, run_id: str) -> List[Dict[str, Any]]:
    """Read path for get_decision_trace()/show_trace()'s SQL-backed
    replacement — every decision_event for one run, oldest first,
    JSON columns decoded back into plain dicts."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM decision_event WHERE run_id=%s ORDER BY created_at ASC, event_id ASC",
            (run_id,),
        )
        rows = cur.fetchall()
    for row in rows:
        for _json_col in ("delta_factors", "meta", "policy_snapshot"):
            if row.get(_json_col) is not None and isinstance(row[_json_col], str):
                row[_json_col] = json.loads(row[_json_col])
    return rows


def record_ai_observation(
    conn, provider: str, model_id: str, run_id: Optional[str],
    prompt_identity: Optional[str], answer_excerpt: Optional[str],
    provenance_mode_reported: str = "UNKNOWN",
    live_search_used_reported: str = "UNKNOWN",
    provenance_parse_status: str = "missing",
    observed_at=None,
) -> int:
    observed_at = _coerce_datetime(observed_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ai_observation "
            "(provider, model_id, run_id, prompt_identity, answer_excerpt, "
            " provenance_mode_reported, live_search_used_reported, provenance_parse_status, observed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                provider, model_id, run_id, prompt_identity, answer_excerpt,
                provenance_mode_reported, live_search_used_reported,
                provenance_parse_status, observed_at,
            ),
        )
        return cur.lastrowid


def record_ai_reported_source(
    conn, ai_observation_id: int, ordinal: Optional[int],
    reported_name: Optional[str], reported_uri: Optional[str],
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ai_reported_source "
            "(ai_observation_id, ordinal, reported_name, reported_uri) "
            "VALUES (%s,%s,%s,%s)",
            (ai_observation_id, ordinal, reported_name, reported_uri),
        )
        return cur.lastrowid


def get_ai_observations_for_run(conn, run_id: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ai_observation_id, provider, model_id, run_id, prompt_identity, "
            "answer_excerpt, provenance_mode_reported, live_search_used_reported, "
            "provenance_parse_status, observed_at "
            "FROM ai_observation WHERE run_id=%s ORDER BY ai_observation_id",
            (run_id,),
        )
        observations = list(cur.fetchall())
        for obs in observations:
            cur.execute(
                "SELECT ordinal, reported_name, reported_uri "
                "FROM ai_reported_source WHERE ai_observation_id=%s ORDER BY ordinal, ai_reported_source_id",
                (obs["ai_observation_id"],),
            )
            obs["reported_sources"] = list(cur.fetchall())
        return observations


def upsert_belief(
    conn, belief_id: str, topic: str, statement: str, confidence: float,
    status: str = "active", evidence_for: Optional[List[str]] = None,
    evidence_against: Optional[List[str]] = None, claim_ids: Optional[List[str]] = None,
    prior: float = 0.5, likelihood: float = 0.5, contradiction_score: float = 0.0,
    decay_factor: float = 0.95, superseded_by: Optional[str] = None,
    created_at=None, updated_at=None,
) -> None:
    """belief is MUTABLE current-derived-state (mandate §17: BELIEF !=
    truth table, matches agent.belief_manager.Belief's own semantics
    exactly — every column overwritten in place on every call, same as
    the retired JSON beliefs.json record they used to mirror before
    "точка ноль" (owner mandate: no JSON file holds durable state that
    belongs under the bastion).
    created_at is write-once (first INSERT only); every subsequent call
    for the same belief_id only updates the mutable columns."""
    created_at = _coerce_datetime(created_at) or _now()
    updated_at = _coerce_datetime(updated_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO belief (belief_id, topic, statement, confidence, status, "
            "evidence_for, evidence_against, claim_ids, prior, likelihood, "
            "contradiction_score, decay_factor, superseded_by, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE statement=VALUES(statement), confidence=VALUES(confidence), "
            "status=VALUES(status), evidence_for=VALUES(evidence_for), "
            "evidence_against=VALUES(evidence_against), claim_ids=VALUES(claim_ids), "
            "prior=VALUES(prior), likelihood=VALUES(likelihood), "
            "contradiction_score=VALUES(contradiction_score), decay_factor=VALUES(decay_factor), "
            "superseded_by=VALUES(superseded_by), updated_at=VALUES(updated_at)",
            (
                belief_id, topic, statement, confidence, status,
                json.dumps(evidence_for) if evidence_for is not None else None,
                json.dumps(evidence_against) if evidence_against is not None else None,
                json.dumps(claim_ids) if claim_ids is not None else None,
                prior, likelihood, contradiction_score, decay_factor, superseded_by,
                created_at, updated_at,
            ),
        )


def get_belief(conn, belief_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM belief WHERE belief_id=%s", (belief_id,))
        row = cur.fetchone()
    return _decode_belief_json(row) if row else None


def list_beliefs_by_topic(conn, topic: str, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """statuses defaults to ('active','revised') — matches agent.belief_
    manager.BeliefManager._find_similar()'s own candidate filter exactly
    (superseded/rejected beliefs are never merge candidates)."""
    statuses = statuses or ["active", "revised"]
    # Built as a separate variable, never inline in .execute() — same
    # discipline already applied to update_grievance_status() earlier
    # this pass (mandate §15/T1: no f-string/concat/format passed
    # directly as an execute() argument, statically greppable). The
    # placeholder count is bounded by len(statuses), never externally
    # controlled text.
    sql = "SELECT * FROM belief WHERE topic=%s AND status IN (" + ", ".join(["%s"] * len(statuses)) + ") ORDER BY created_at ASC"
    with conn.cursor() as cur:
        cur.execute(sql, (topic, *statuses))
        rows = cur.fetchall()
    return [_decode_belief_json(r) for r in rows]


def list_belief_history(conn, belief_id: str) -> List[Dict[str, Any]]:
    """The REAL append-only history for one belief — belief_assessment_
    history, ordered oldest-first (matches the old JSON Belief.history[]
    list's own append order exactly)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM belief_assessment_history WHERE belief_id=%s "
            "ORDER BY created_at ASC, history_id ASC",
            (belief_id,),
        )
        return cur.fetchall()


def list_all_beliefs(conn) -> List[Dict[str, Any]]:
    """Every belief regardless of status — for the rare, genuinely
    cross-status lookup (agent.dependency_recheck._belief_for_family()
    matches on claim_ids across ANY status, not just active ones; a
    family can legitimately point at a belief that was later revised or
    superseded)."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM belief")
        rows = cur.fetchall()
    return [_decode_belief_json(r) for r in rows]


def list_active_beliefs(conn) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM belief WHERE status='active' ORDER BY created_at ASC")
        rows = cur.fetchall()
    return [_decode_belief_json(r) for r in rows]


def list_contradictory_beliefs(conn, min_score: float = 0.5) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM belief WHERE contradiction_score >= %s ORDER BY contradiction_score DESC", (min_score,))
        rows = cur.fetchall()
    return [_decode_belief_json(r) for r in rows]


def get_belief_stats(conn) -> Dict[str, Any]:
    """Single aggregate query — same shape as agent.belief_manager.
    BeliefManager.get_stats(), computed in SQL instead of iterating an
    in-memory list loaded from JSON."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT "
            "COUNT(*) AS total, "
            "SUM(status='active') AS active, "
            "SUM(status='revised') AS revised, "
            "SUM(status='superseded') AS superseded, "
            "AVG(confidence) AS avg_confidence, "
            "AVG(contradiction_score) AS avg_contradiction "
            "FROM belief"
        )
        totals = cur.fetchone()
        cur.execute("SELECT topic, COUNT(*) AS c FROM belief GROUP BY topic")
        topic_rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS c FROM belief WHERE contradiction_score >= 0.5")
        contradictory = cur.fetchone()
    return {
        "total": totals["total"] or 0,
        "active": totals["active"] or 0,
        "revised": totals["revised"] or 0,
        "superseded": totals["superseded"] or 0,
        "contradictory": contradictory["c"] or 0,
        "topics": {r["topic"]: r["c"] for r in topic_rows},
        "avg_confidence": round(float(totals["avg_confidence"] or 0), 2),
        "avg_contradiction": round(float(totals["avg_contradiction"] or 0), 2),
    }


def _decode_belief_json(row: Dict[str, Any]) -> Dict[str, Any]:
    for col in ("evidence_for", "evidence_against", "claim_ids"):
        if row.get(col) is not None and isinstance(row[col], str):
            row[col] = json.loads(row[col])
    return row


def record_belief_assessment(
    conn, belief_id: str, change_type: str, old_confidence: Optional[float] = None,
    new_confidence: Optional[float] = None, reason: Optional[str] = None,
    run_id: Optional[str] = None, created_at=None,
) -> int:
    """belief_assessment_history is APPEND-ONLY (mandate §17) — mirrors
    the entries already appended to Belief.history[] in agent.belief_
    manager.py, never replacing or overwriting a previous assessment.
    run_id is genuinely NULL-able and often NULL by design: a decay
    sweep (_apply_decay()) is not tied to any single verification run,
    and correlating every BeliefManager.add_belief()/challenge_belief()
    call site (agent/dependency_recheck.py, agent/curiosity.py x2,
    agent/disagreement_engine.py, agent/orchestrator/claims/lifecycle.py
    — agent/biography_stats.py's own unrelated BiographyStats.add_belief()
    is a different class entirely, not this one) with a run_id would
    mean changing each of their public signatures — out of proportion
    for this pass; documented as a deliberate V1 scope limit, not
    silently omitted."""
    created_at = _coerce_datetime(created_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO belief_assessment_history "
            "(belief_id, run_id, old_confidence, new_confidence, reason, change_type, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (belief_id, run_id, old_confidence, new_confidence, (reason or "")[:255] or None, change_type, created_at),
        )
        return cur.lastrowid


def record_recheck_event(
    conn, family_id: str, outcome: str, run_id: Optional[str] = None,
    trigger_reason: Optional[str] = None, started_at=None, reason: Optional[str] = None,
) -> int:
    """APPEND-ONLY (mandate §16) — FIXES a real, confirmed bug: the
    current registry/claim_family_graph.json's recheck_log[family_id]
    OVERWRITES on every recheck (schema.py's own comment: fam_c370ccfa
    had recheck_count=2 but only the LAST outcome was ever visible).
    One row per actual recheck attempt here, never overwritten — the
    JSON side's own last-outcome/count fields are untouched by this,
    kept as the cheap "current state" shortcut they already are."""
    started_at = _coerce_datetime(started_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recheck_event "
            "(family_id, run_id, trigger_reason, started_at, outcome, reason) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (family_id, run_id, trigger_reason, started_at, outcome, reason),
        )
        return cur.lastrowid


# ============================================================
# READ — Local Memory read API (mandate §14/§33)
# ============================================================

def get_current_answer(conn, question_id: int) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT av.*, aa.canonical_trust, aa.diverged, aa.created_at AS assessed_at "
            "FROM answer_version av "
            "LEFT JOIN answer_assessment aa ON aa.answer_id = av.answer_id "
            "WHERE av.question_id = %s "
            "ORDER BY av.version_number DESC, aa.created_at DESC LIMIT 1",
            (question_id,),
        )
        return cur.fetchone()


def get_answer_history(conn, question_id: int) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT answer_id, version_number, answer_text, created_at, created_by_run_id "
            "FROM answer_version WHERE question_id=%s ORDER BY version_number",
            (question_id,),
        )
        versions = cur.fetchall()
        for v in versions:
            cur.execute(
                "SELECT assessment_id, run_id, canonical_trust, diverged, created_at "
                "FROM answer_assessment WHERE answer_id=%s ORDER BY created_at",
                (v["answer_id"],),
            )
            v["assessments"] = cur.fetchall()
        return versions


def explain_answer(conn, answer_id: int) -> Dict[str, Any]:
    """Walks answer -> run -> claims -> evidence_relations ->
    source_observations -> resources (mandate §16 scenario 1/§41)."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM answer_version WHERE answer_id=%s", (answer_id,))
        answer = cur.fetchone()
        if not answer:
            return {}

        cur.execute(
            "SELECT * FROM answer_assessment WHERE answer_id=%s ORDER BY created_at DESC LIMIT 1",
            (answer_id,),
        )
        assessment = cur.fetchone()

        cur.execute("SELECT * FROM verification_run WHERE run_id=%s", (answer["created_by_run_id"],))
        run = cur.fetchone()

        cur.execute("SELECT * FROM claim_occurrence WHERE run_id=%s", (answer["created_by_run_id"],))
        claims = cur.fetchall()

        for claim in claims:
            cur.execute(
                "SELECT er.*, so.resource_id, so.observation_route, so.observed_at, "
                "       so.content_excerpt, sr.canonical_uri, sr.resource_type "
                "FROM evidence_relation er "
                "JOIN source_observation so ON so.observation_id = er.observation_id "
                "JOIN source_resource sr ON sr.resource_id = so.resource_id "
                "WHERE er.claim_id = %s",
                (claim["claim_id"],),
            )
            claim["evidence"] = cur.fetchall()

    return {"answer": answer, "assessment": assessment, "run": run, "claims": claims}


def get_verification_runs(conn, question_id: int) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT vr.* FROM verification_run vr "
            "JOIN question_occurrence qo ON qo.occurrence_id = vr.occurrence_id "
            "WHERE qo.question_id = %s ORDER BY vr.started_at",
            (question_id,),
        )
        return cur.fetchall()


def get_sources_for_run(conn, run_id: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT so.*, sr.canonical_uri, sr.resource_type FROM source_observation so "
            "JOIN source_resource sr ON sr.resource_id = so.resource_id "
            "WHERE so.run_id = %s ORDER BY so.observed_at",
            (run_id,),
        )
        return cur.fetchall()


def get_claim_history(conn, family_id: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT co.* FROM claim_occurrence co "
            "JOIN family_member fm ON fm.claim_id = co.claim_id "
            "WHERE fm.family_id = %s ORDER BY co.run_id",
            (family_id,),
        )
        return cur.fetchall()


def get_route_history(conn, resource_id: int) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM source_observation WHERE resource_id=%s ORDER BY observed_at",
            (resource_id,),
        )
        return cur.fetchall()


def get_last_checked(conn, question_id: int) -> Optional[datetime]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(vr.started_at) AS last_checked FROM verification_run vr "
            "JOIN question_occurrence qo ON qo.occurrence_id = vr.occurrence_id "
            "WHERE qo.question_id = %s",
            (question_id,),
        )
        row = cur.fetchone()
        return row["last_checked"] if row else None


def compare_runs(conn, run_id_a: str, run_id_b: str) -> Dict[str, Any]:
    """Evidence diff between two runs of the same question, keyed by
    resource_id (not observation_id — the same resource re-observed is
    a different row but the same root, mandate §13/§43)."""
    sources_a = {s["resource_id"]: s for s in get_sources_for_run(conn, run_id_a)}
    sources_b = {s["resource_id"]: s for s in get_sources_for_run(conn, run_id_b)}

    added = [sources_b[r] for r in sources_b if r not in sources_a]
    lost = [sources_a[r] for r in sources_a if r not in sources_b]

    changed = []
    with conn.cursor() as cur:
        for resource_id in set(sources_a) & set(sources_b):
            cur.execute(
                "SELECT er.relation FROM evidence_relation er "
                "JOIN source_observation so ON so.observation_id = er.observation_id "
                "WHERE so.run_id=%s AND so.resource_id=%s", (run_id_a, resource_id),
            )
            rel_a = {r["relation"] for r in cur.fetchall()}
            cur.execute(
                "SELECT er.relation FROM evidence_relation er "
                "JOIN source_observation so ON so.observation_id = er.observation_id "
                "WHERE so.run_id=%s AND so.resource_id=%s", (run_id_b, resource_id),
            )
            rel_b = {r["relation"] for r in cur.fetchall()}
            if rel_a != rel_b:
                changed.append({"resource_id": resource_id, "before": sorted(rel_a), "after": sorted(rel_b)})

    return {"added": added, "lost": lost, "changed": changed}


# ============================================================
# GRIEVANCE / FORGIVENESS_CAPACITY — SQL-backed character/relationship
# state (see schema.py's own docstring for these two tables). Low-level
# CRUD only; the actual state-machine logic (when a grievance advances
# from "acknowledged" to "understood", the minimum healing time, etc.)
# lives in agent/relationship_memory.py, the one intended caller of
# these functions — kept separate the same way every other repository
# function here is a thin SQL layer under agent/orchestrator*'s own
# business logic.
# ============================================================

def record_grievance(
    conn, grievance_id: str, user_id: str, event_type: str, description: str,
    severity: float, context: Optional[Dict[str, Any]] = None, created_at=None,
) -> None:
    created_at = _coerce_datetime(created_at) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO grievance "
            "(id, user_id, event_type, description, severity, status, context, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, 'registered', %s, %s, %s)",
            (grievance_id, user_id, event_type, description, severity,
             json.dumps(context) if context is not None else None, created_at, created_at),
        )


def get_grievance(conn, grievance_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM grievance WHERE id=%s", (grievance_id,))
        row = cur.fetchone()
    if row and row.get("context") is not None and isinstance(row["context"], str):
        row["context"] = json.loads(row["context"])
    return row


def find_similar_open_grievance(conn, user_id: str, description: str) -> Optional[Dict[str, Any]]:
    """Mirrors the old forgiveness_model.py's own `_find_similar()`
    exactly: same-first-20-characters match, excluding grievances
    already fully "forgiven" (a repeat of a long-forgiven offense is a
    NEW grievance, not a reopening of the old one) — "unforgiven" ones
    ARE still matched, same as the original."""
    prefix = description[:20]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM grievance WHERE user_id=%s AND status != 'forgiven' "
            "AND LEFT(description, 20) = %s ORDER BY created_at DESC LIMIT 1",
            (user_id, prefix),
        )
        return cur.fetchone()


def bump_grievance(conn, grievance_id: str, new_severity: float, timestamp=None) -> None:
    """Existing-grievance-recurred path: severity rises, status resets
    to 'registered' (a fresh instance of the same old grievance is not
    automatically still 'healing')."""
    timestamp = _coerce_datetime(timestamp) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE grievance SET severity=%s, status='registered', updated_at=%s WHERE id=%s",
            (new_severity, timestamp, grievance_id),
        )


def update_grievance_status(
    conn, grievance_id: str, status: str, *, apology_sincerity: Optional[float] = None,
    apology_at=None, understood_at=None, forgiven_at=None, timestamp=None,
) -> None:
    """Generic status/timestamp update — agent/relationship_memory.py
    decides WHICH fields to set for a given transition; this function
    just writes whatever it's given. A NULL parameter for any of the
    COALESCE'd columns below leaves that column's existing value alone
    (a transition that doesn't reach "understood" this call must never
    accidentally clear an already-set understood_at from an earlier
    call) — a fixed, static SQL string with COALESCE achieves the same
    "only set what was given" behavior as building the SET clause
    dynamically, without ever interpolating anything into the query
    text itself (mandate §15 / T1: no f-string/concat/format passed
    directly as an execute() argument anywhere in this package,
    statically greppable — see agent/db_sql_security_injection_
    regression_test.py)."""
    timestamp = _coerce_datetime(timestamp) or _now()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE grievance SET "
            "status=%s, updated_at=%s, "
            "apology_sincerity=COALESCE(%s, apology_sincerity), "
            "apology_at=COALESCE(%s, apology_at), "
            "understood_at=COALESCE(%s, understood_at), "
            "forgiven_at=COALESCE(%s, forgiven_at) "
            "WHERE id=%s",
            (
                status, timestamp, apology_sincerity,
                _coerce_datetime(apology_at), _coerce_datetime(understood_at), _coerce_datetime(forgiven_at),
                grievance_id,
            ),
        )


def list_active_grievances(conn, user_id: str) -> List[Dict[str, Any]]:
    """Active = not yet resolved either way ('forgiven'/'unforgiven' are
    the two terminal states) — same definition as the original
    ForgivenessModel.get_active_grievances()."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM grievance WHERE user_id=%s AND status NOT IN ('forgiven', 'unforgiven') "
            "ORDER BY created_at ASC",
            (user_id,),
        )
        rows = cur.fetchall()
    for row in rows:
        if row.get("context") is not None and isinstance(row["context"], str):
            row["context"] = json.loads(row["context"])
    return rows


def count_grievances_by_status(conn, user_id: str, status: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM grievance WHERE user_id=%s AND status=%s",
            (user_id, status),
        )
        row = cur.fetchone()
    return int(row["c"]) if row else 0


def get_forgiveness_capacity(conn, user_id: str) -> Dict[str, Any]:
    """Find-or-default (NOT find-or-create — see set_forgiveness_capacity()
    for why the actual INSERT is deferred to the first real write)."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM forgiveness_capacity WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
    return row or {"user_id": user_id, "capacity": 50.0, "last_forgiveness": None, "updated_at": None}


def set_forgiveness_capacity(conn, user_id: str, capacity: float, last_forgiveness=None, timestamp=None) -> None:
    """INSERT ... ON DUPLICATE KEY UPDATE — this table has no
    find-or-create helper of its own because the very first grievance
    a user ever registers should be able to lazily create their capacity
    row at 50.0 (schema's own DEFAULT) without a separate round-trip;
    this single statement handles both the never-existed and the
    already-exists case identically."""
    timestamp = _coerce_datetime(timestamp) or _now()
    last_forgiveness = _coerce_datetime(last_forgiveness) if last_forgiveness is not None else None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO forgiveness_capacity (user_id, capacity, last_forgiveness, updated_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE capacity=VALUES(capacity), "
            "last_forgiveness=COALESCE(VALUES(last_forgiveness), last_forgiveness), updated_at=VALUES(updated_at)",
            (user_id, capacity, last_forgiveness, timestamp),
        )
