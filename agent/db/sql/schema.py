"""
agent/db/sql/schema.py — canonical epistemic memory DDL (MySQL 8.0 /
Percona-compatible). Static SQL only — nothing in this module connects
to a database or executes anything; see agent/db/sql/connection.py and
agent/db/sql/migrate.py for that.

DESIGN NOTES (read before changing a table):

1. MEMORY != TRUTH. No column, table, or enum value anywhere in this
   schema may spell "is_true"/"verified_truth"/"absolute_truth" or an
   equivalent — grepped for this explicitly, see the regression suite.
   Outcomes are always `candidate`/`reason`/`verification_status`, never
   a truth predicate.

2. Route vs resource (storage-audit correction, Этап 5 mandate §6): the
   FIRST version of this schema put route_type on SOURCE_RESOURCE alone.
   That conflates "what this resource fundamentally is" with "how THIS
   observation of it was retrieved" — a local_memory replay of an
   internet URL is not a new resource, it is a new OBSERVATION of the
   SAME resource via a different route. Fixed: SOURCE_RESOURCE.
   resource_type never includes "local_memory" (a resource is always
   internet/network_node/ai_chat/local_model at its origin);
   SOURCE_OBSERVATION.observation_route can be any of the five channels,
   and origin_observation_id (self-FK) carries the replay chain when
   observation_route="local_memory" — matching agent/verification_
   memory.py's existing compute_stable_root() semantics, just backed by
   a real FK instead of a recomputed tuple.

3. Append-only vs mutable — every table below is one or the other, never
   ambiguous. See the "APPEND-ONLY" / "MUTABLE" marker in each table's
   comment block. A row in an append-only table is NEVER UPDATEd by any
   repository function in agent/db/sql/repositories.py except for the
   one explicitly-mutable table per entity (e.g. VERIFICATION_RUN.status
   transitions running->completed/aborted/failed exactly once).

4. Nothing here activates network_node/ai_chat as independent epistemic
   roots — node_id/validator_id/model_id columns exist (nullable) so the
   schema doesn't need another migration when that work happens, but no
   repository function in this package currently writes non-NULL values
   for them, and no counting logic treats them as independent (that
   logic lives in Python, agent/epistemic_contradiction_shadow.py,
   unchanged by this migration).

5. Debug/telemetry does NOT live here (мандат §38/§20 of the storage
   audit): no stack traces, no prompt dumps, no wall-clock breakdowns,
   no HTTP retry chatter. RUN_ERROR is 5 columns, not a log warehouse.
"""

SCHEMA_VERSION = 1

SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version      INT PRIMARY KEY,
    applied_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    description  VARCHAR(255) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ── QUESTION / QUESTION_OCCURRENCE ──────────────────────────────────────
# MUTABLE identity row (QUESTION) + APPEND-ONLY occurrences.
# V1 identity = exact-normalized text only (reuses agent/claim_identity.
# py::canonicalize_claim_text — NOT a new, incompatible normalizer — see
# repositories.py::resolve_question()). No topic_id, no semantic
# cross-question clustering — explicitly deferred (mandate §7).

QUESTION = """
CREATE TABLE IF NOT EXISTS question (
    question_id     BIGINT AUTO_INCREMENT PRIMARY KEY,
    canonical_hash  CHAR(64) NOT NULL,   -- sha256(canonicalize_claim_text(raw))
    first_asked_at  DATETIME NOT NULL,
    UNIQUE KEY uq_question_hash (canonical_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

QUESTION_OCCURRENCE = """
CREATE TABLE IF NOT EXISTS question_occurrence (
    occurrence_id   BIGINT AUTO_INCREMENT PRIMARY KEY,
    question_id     BIGINT NOT NULL,
    raw_text        TEXT NOT NULL,        -- verbatim, NEVER rewritten
    anonymized_text TEXT NULL,            -- output of orch_query_archive.py's
                                           -- existing anonymize() step, ported
                                           -- here (privacy protection must not
                                           -- be lost in migration, mandate §21)
    asked_at        DATETIME NOT NULL,
    session_id      VARCHAR(64) NULL,
    CONSTRAINT fk_qo_question FOREIGN KEY (question_id)
        REFERENCES question(question_id),
    KEY idx_qo_question (question_id, asked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ── VERIFICATION_RUN ─────────────────────────────────────────────────────
# MUTABLE only on .status, exactly once (running -> completed|aborted|
# failed). Crash-safe lifecycle: INSERT status=running + COMMIT first,
# append-only children written as they happen, final UPDATE status=
# completed LAST. A reader must never treat status=running as a finished
# verification (mandate §29).

VERIFICATION_RUN = """
CREATE TABLE IF NOT EXISTS verification_run (
    run_id            VARCHAR(40) PRIMARY KEY,  -- reuses existing trace_id, not a new id
    occurrence_id     BIGINT NOT NULL,
    started_at        DATETIME NOT NULL,
    completed_at      DATETIME NULL,
    status            ENUM('running','completed','aborted','failed') NOT NULL DEFAULT 'running',
    web_enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    validation_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    pipeline_version  VARCHAR(40) NULL,   -- short git commit
    schema_version    INT NOT NULL DEFAULT 1,
    final_answer_id   BIGINT NULL,        -- FK added after answer_version exists (below)
    failed_stage      VARCHAR(80) NULL,
    error_class       VARCHAR(120) NULL,
    CONSTRAINT fk_vr_occurrence FOREIGN KEY (occurrence_id)
        REFERENCES question_occurrence(occurrence_id),
    KEY idx_vr_occurrence (occurrence_id, started_at),
    KEY idx_vr_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ── ANSWER_VERSION / ANSWER_ASSESSMENT ──────────────────────────────────
# ANSWER_VERSION: APPEND-ONLY. answer_text is the LITERAL delivered
# string (agent/orchestrator/response/writeback.py's new "delivered_
# answer_text" observation — see agent/answer_delivery_persistence_
# regression_test.py) — not synthesis_result.answer pre-decoration.
# New row only when answer_hash changes from the question's current
# latest version (exact-hash v1, semantic-equivalence explicitly
# deferred — mandate §9: do not silently invent semantic-change
# detection that doesn't exist).
#
# ANSWER_ASSESSMENT: APPEND-ONLY, one row PER RUN regardless of whether
# the answer text changed — this is what makes CONFIDENCE_CHANGED
# queryable independently of ANSWER_CHANGED (mandate §2/§10).

ANSWER_VERSION = """
CREATE TABLE IF NOT EXISTS answer_version (
    answer_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    question_id        BIGINT NOT NULL,
    version_number     INT NOT NULL,
    answer_text        MEDIUMTEXT NOT NULL,
    answer_hash        CHAR(64) NOT NULL,
    created_by_run_id  VARCHAR(40) NOT NULL,
    supersedes_id      BIGINT NULL,
    created_at         DATETIME NOT NULL,
    CONSTRAINT fk_av_question FOREIGN KEY (question_id)
        REFERENCES question(question_id),
    CONSTRAINT fk_av_run FOREIGN KEY (created_by_run_id)
        REFERENCES verification_run(run_id),
    CONSTRAINT fk_av_supersedes FOREIGN KEY (supersedes_id)
        REFERENCES answer_version(answer_id),
    KEY idx_av_question (question_id, version_number),
    KEY idx_av_hash (question_id, answer_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

ANSWER_ASSESSMENT = """
CREATE TABLE IF NOT EXISTS answer_assessment (
    assessment_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    answer_id          BIGINT NOT NULL,
    run_id             VARCHAR(40) NOT NULL,
    synthesizer_strand VARCHAR(30) NULL,
    trust_gate_strand  VARCHAR(30) NULL,
    canonical_trust    VARCHAR(30) NOT NULL,
    diverged           BOOLEAN NOT NULL DEFAULT FALSE,
    stricter_strand    VARCHAR(20) NULL,
    reason             TEXT NULL,
    created_at         DATETIME NOT NULL,
    CONSTRAINT fk_aa_answer FOREIGN KEY (answer_id)
        REFERENCES answer_version(answer_id),
    CONSTRAINT fk_aa_run FOREIGN KEY (run_id)
        REFERENCES verification_run(run_id),
    KEY idx_aa_answer (answer_id, created_at),
    KEY idx_aa_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# verification_run.final_answer_id's FK is added separately (below) since
# answer_version didn't exist yet when verification_run was created above
# — MySQL requires the referenced table to already exist for an inline FK.
VERIFICATION_RUN_FINAL_ANSWER_FK = """
ALTER TABLE verification_run
    ADD CONSTRAINT fk_vr_final_answer FOREIGN KEY (final_answer_id)
        REFERENCES answer_version(answer_id);
"""

# ── CLAIM_FAMILY / FAMILY_MEMBER / CLAIM_OCCURRENCE ─────────────────────
# CLAIM_FAMILY.canonical_text is write-once (confirmed immutable in the
# current Python implementation, agent/claim_family_registry.py — no
# writer changes it after creation). FAMILY_MEMBER replaces the current
# flat members[] JSON array with a real join table. CLAIM_OCCURRENCE is
# APPEND-ONLY, run-scoped.

CLAIM_FAMILY = """
CREATE TABLE IF NOT EXISTS claim_family (
    family_id       VARCHAR(20) PRIMARY KEY,  -- reuses existing fam_ ids
    domain          VARCHAR(40) NOT NULL,
    canonical_text  TEXT NOT NULL,            -- write-once
    created_at      DATETIME NOT NULL,
    updated_at      DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

FAMILY_MEMBER = """
CREATE TABLE IF NOT EXISTS family_member (
    family_id   VARCHAR(20) NOT NULL,
    claim_id    VARCHAR(20) NOT NULL,
    linked_at   DATETIME NOT NULL,
    PRIMARY KEY (family_id, claim_id),
    CONSTRAINT fk_fm_family FOREIGN KEY (family_id)
        REFERENCES claim_family(family_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

CLAIM_OCCURRENCE = """
CREATE TABLE IF NOT EXISTS claim_occurrence (
    claim_id             VARCHAR(20) PRIMARY KEY,  -- reuses existing cl_ ids
    run_id               VARCHAR(40) NOT NULL,
    claim_text           TEXT NOT NULL,
    content_hash         CHAR(64) NULL,
    claim_type           VARCHAR(20) NULL,
    claim_confidence     FLOAT NULL,
    verification_status  VARCHAR(20) NULL,
    family_id            VARCHAR(20) NULL,
    query_context        TEXT NULL,   -- FIX (mandate §11): currently computed
                                       -- upstream (claims/lifecycle.py) but
                                       -- never persisted anywhere — real gap
    support_count        INT NOT NULL DEFAULT 0,
    contradiction_count  INT NOT NULL DEFAULT 0,
    CONSTRAINT fk_co_run FOREIGN KEY (run_id)
        REFERENCES verification_run(run_id),
    CONSTRAINT fk_co_family FOREIGN KEY (family_id)
        REFERENCES claim_family(family_id),
    KEY idx_co_run (run_id),
    KEY idx_co_family (family_id),
    KEY idx_co_content_hash (content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ── SOURCE_RESOURCE / SOURCE_OBSERVATION / EVIDENCE_RELATION ────────────
# See module docstring point 2 for the resource-vs-route correction.
# SOURCE_RESOURCE is a MUTABLE identity row (only first_observed_at is
# ever set, never changed). SOURCE_OBSERVATION and EVIDENCE_RELATION are
# both APPEND-ONLY — a re-observation or a re-NLI of the same pair is
# always a NEW row, never an UPDATE (mandate §3/§4).

SOURCE_RESOURCE = """
CREATE TABLE IF NOT EXISTS source_resource (
    resource_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    resource_type       ENUM('internet','network_node','ai_chat','local_model') NOT NULL,
    canonical_uri        VARCHAR(2048) NULL,   -- internet resources; reuses
                                                -- agent.orch_web_scraper.
                                                -- SharedFetchCache.canonicalize
                                                -- — NOT a second canonicalizer
    uri_hash              CHAR(64) NULL,        -- sha256(canonical_uri) — the
                                                 -- REAL uniqueness key; VARCHAR
                                                 -- (2048) itself exceeds
                                                 -- InnoDB's key-prefix limit
                                                 -- under utf8mb4 for a direct
                                                 -- UNIQUE index, so identity is
                                                 -- enforced on the fixed-width
                                                 -- hash instead (standard
                                                 -- pattern, not app-only trust)
    node_id               VARCHAR(40) NULL,     -- network_node resources (inactive)
    validator_id          VARCHAR(40) NULL,     -- ai_chat resources (inactive)
    model_id               VARCHAR(40) NULL,     -- local_model resources (inactive)
    first_observed_at     DATETIME NOT NULL,
    UNIQUE KEY uq_sr_uri_hash (uri_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

SOURCE_OBSERVATION = """
CREATE TABLE IF NOT EXISTS source_observation (
    observation_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    resource_id           BIGINT NOT NULL,
    run_id                 VARCHAR(40) NOT NULL,
    observation_route     ENUM('internet','local_memory','network_node','ai_chat','local_model') NOT NULL,
    origin_observation_id BIGINT NULL,   -- self-FK, replay provenance chain
    observed_at            DATETIME NOT NULL,
    source_class           VARCHAR(30) NULL,
    quality_score           FLOAT NULL,
    content_excerpt         TEXT NULL,     -- excerpt only, NEVER full raw HTML
    rejection_reason         VARCHAR(60) NULL,  -- controlled vocabulary, see
                                                 -- repositories.py::REJECTION_REASONS
    CONSTRAINT fk_so_resource FOREIGN KEY (resource_id)
        REFERENCES source_resource(resource_id),
    CONSTRAINT fk_so_run FOREIGN KEY (run_id)
        REFERENCES verification_run(run_id),
    CONSTRAINT fk_so_origin FOREIGN KEY (origin_observation_id)
        REFERENCES source_observation(observation_id),
    KEY idx_so_resource (resource_id, observed_at),
    KEY idx_so_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

EVIDENCE_RELATION = """
CREATE TABLE IF NOT EXISTS evidence_relation (
    relation_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    claim_id          VARCHAR(20) NOT NULL,
    observation_id    BIGINT NOT NULL,
    relation          ENUM('supports','contradicts','uncertain','unrelated') NOT NULL,
    directness        FLOAT NULL,
    evidence_eligible  BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_role      VARCHAR(20) NULL,
    counted_via        ENUM('authority','directness') NULL,
    created_at        DATETIME NOT NULL,
    CONSTRAINT fk_er_claim FOREIGN KEY (claim_id)
        REFERENCES claim_occurrence(claim_id),
    CONSTRAINT fk_er_observation FOREIGN KEY (observation_id)
        REFERENCES source_observation(observation_id),
    KEY idx_er_claim (claim_id),
    KEY idx_er_observation (observation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ── BELIEF / BELIEF_ASSESSMENT_HISTORY ──────────────────────────────────
# BELIEF is MUTABLE (current derived state — matches confirmed current
# beliefs.json semantics exactly). BELIEF_ASSESSMENT_HISTORY is
# APPEND-ONLY, extracted from the currently-embedded history[] array.

BELIEF = """
CREATE TABLE IF NOT EXISTS belief (
    belief_id          VARCHAR(20) PRIMARY KEY,  -- reuses existing belief id
    topic               VARCHAR(120) NOT NULL,
    statement            TEXT NOT NULL,
    confidence            FLOAT NOT NULL,
    status                ENUM('active','revised','rejected','superseded') NOT NULL DEFAULT 'active',
    created_at           DATETIME NOT NULL,
    updated_at           DATETIME NOT NULL,
    KEY idx_belief_topic (topic)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

BELIEF_ASSESSMENT_HISTORY = """
CREATE TABLE IF NOT EXISTS belief_assessment_history (
    history_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
    belief_id        VARCHAR(20) NOT NULL,
    run_id           VARCHAR(40) NULL,
    old_confidence   FLOAT NULL,
    new_confidence   FLOAT NULL,
    reason           VARCHAR(255) NULL,
    -- PREVIOUS AUDIT CORRECTION (mandate §50): the original 5A design
    -- guessed at these labels ('decay','update','challenge','supersede')
    -- before checking agent/belief_manager.py's real history[].change
    -- values. The actual, only-ever-written values are 'created'
    -- (add_belief), 'decayed' (_apply_decay), 'updated'
    -- (_update_existing), 'revised' (challenge_belief — NOT 'challenge'),
    -- 'superseded' (supersede_belief). Corrected to match before this
    -- table is ever written to (schema never deployed yet, so no
    -- migration-of-existing-rows risk).
    change_type      ENUM('created','decayed','updated','revised','superseded') NOT NULL,
    created_at       DATETIME NOT NULL,
    CONSTRAINT fk_bah_belief FOREIGN KEY (belief_id)
        REFERENCES belief(belief_id),
    CONSTRAINT fk_bah_run FOREIGN KEY (run_id)
        REFERENCES verification_run(run_id),
    KEY idx_bah_belief (belief_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ── SEMANTIC_EDGE / RECHECK_EVENT / EPISTEMIC_CONTRADICTION_OBSERVATION ──
# SEMANTIC_EDGE is MUTABLE-upsert (observation_count++), matching the
# CURRENT, correct behavior of agent/family_dependency_graph.py — only
# the storage shape changes, not the semantics.
#
# RECHECK_EVENT is APPEND-ONLY — this FIXES a real, confirmed bug: the
# current registry/claim_family_graph.json's recheck_log[family_id]
# OVERWRITES on every recheck (fam_c370ccfa had recheck_count=2 but only
# the LAST outcome was ever visible). Never do that here.
#
# EPISTEMIC_CONTRADICTION_OBSERVATION mirrors agent/epistemic_
# contradiction_shadow.py's output exactly: candidate (bool) + reason,
# never a truth verdict. This table is NOT read by anything that gates
# Phase 12 — mandate §19: "SQL migration не является поводом активировать
# shadow logic." Writing to it is purely additive telemetry of an
# already-shadow-only classifier.

SEMANTIC_EDGE = """
CREATE TABLE IF NOT EXISTS semantic_edge (
    edge_id             VARCHAR(20) PRIMARY KEY,  -- reuses existing edg_ ids
    family_a            VARCHAR(20) NOT NULL,
    family_b            VARCHAR(20) NOT NULL,
    edge_type            ENUM('contradicts','supports','depends_on') NOT NULL,
    reason                VARCHAR(120) NULL,
    observation_count    INT NOT NULL DEFAULT 1,
    created_at           DATETIME NOT NULL,
    last_seen_at         DATETIME NOT NULL,
    CONSTRAINT fk_se_family_a FOREIGN KEY (family_a)
        REFERENCES claim_family(family_id),
    CONSTRAINT fk_se_family_b FOREIGN KEY (family_b)
        REFERENCES claim_family(family_id),
    KEY idx_se_pair (family_a, family_b, edge_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

RECHECK_EVENT = """
CREATE TABLE IF NOT EXISTS recheck_event (
    event_id     BIGINT AUTO_INCREMENT PRIMARY KEY,
    family_id    VARCHAR(20) NOT NULL,
    run_id       VARCHAR(40) NULL,
    trigger_reason VARCHAR(60) NULL,
    started_at   DATETIME NOT NULL,
    outcome      VARCHAR(30) NOT NULL,
    reason       VARCHAR(120) NULL,
    CONSTRAINT fk_re_family FOREIGN KEY (family_id)
        REFERENCES claim_family(family_id),
    CONSTRAINT fk_re_run FOREIGN KEY (run_id)
        REFERENCES verification_run(run_id),
    KEY idx_re_family (family_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

EPISTEMIC_CONTRADICTION_OBSERVATION = """
CREATE TABLE IF NOT EXISTS epistemic_contradiction_observation (
    observation_id   BIGINT AUTO_INCREMENT PRIMARY KEY,
    edge_id          VARCHAR(20) NOT NULL,
    run_id           VARCHAR(40) NOT NULL,
    roots_a          INT NOT NULL,
    roots_b          INT NOT NULL,
    overlap          INT NOT NULL,
    distinct_union   INT NOT NULL,
    candidate        BOOLEAN NOT NULL,   -- "recheck-worthy" flag, never a truth verdict
    reason           VARCHAR(60) NOT NULL,
    created_at       DATETIME NOT NULL,
    CONSTRAINT fk_eco_edge FOREIGN KEY (edge_id)
        REFERENCES semantic_edge(edge_id),
    CONSTRAINT fk_eco_run FOREIGN KEY (run_id)
        REFERENCES verification_run(run_id),
    KEY idx_eco_edge (edge_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ── RUN_ERROR ────────────────────────────────────────────────────────────
# APPEND-ONLY, minimal, per mandate §20: NOT a debug warehouse. No stack
# traces, no prompt dumps, no stdout.

RUN_ERROR = """
CREATE TABLE IF NOT EXISTS run_error (
    error_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id        VARCHAR(40) NOT NULL,
    failed_stage  VARCHAR(80) NOT NULL,
    error_class   VARCHAR(120) NOT NULL,
    short_message VARCHAR(500) NULL,
    created_at    DATETIME NOT NULL,
    CONSTRAINT fk_rerr_run FOREIGN KEY (run_id)
        REFERENCES verification_run(run_id),
    KEY idx_rerr_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# Ordered: each CREATE TABLE only references tables already created
# above it (FK ordering matters in MySQL without deferred constraints).
ALL_TABLES_IN_ORDER = [
    ("schema_migrations", SCHEMA_MIGRATIONS),
    ("question", QUESTION),
    ("question_occurrence", QUESTION_OCCURRENCE),
    ("verification_run", VERIFICATION_RUN),
    ("answer_version", ANSWER_VERSION),
    ("answer_assessment", ANSWER_ASSESSMENT),
    ("claim_family", CLAIM_FAMILY),
    ("family_member", FAMILY_MEMBER),
    ("claim_occurrence", CLAIM_OCCURRENCE),
    ("source_resource", SOURCE_RESOURCE),
    ("source_observation", SOURCE_OBSERVATION),
    ("evidence_relation", EVIDENCE_RELATION),
    ("belief", BELIEF),
    ("belief_assessment_history", BELIEF_ASSESSMENT_HISTORY),
    ("semantic_edge", SEMANTIC_EDGE),
    ("recheck_event", RECHECK_EVENT),
    ("epistemic_contradiction_observation", EPISTEMIC_CONTRADICTION_OBSERVATION),
    ("run_error", RUN_ERROR),
]

# Deferred ALTER (needs answer_version to already exist).
ALTER_STATEMENTS_IN_ORDER = [
    ("verification_run.final_answer_id FK", VERIFICATION_RUN_FINAL_ANSWER_FK),
]

# Truth-claiming vocabulary is explicitly BANNED from this schema
# (mandate §1/§14) — a regression test greps every DDL string above for
# these tokens and must find zero matches.
_BANNED_TOKENS = ("is_true", "verified_truth", "absolute_truth", "truth_certificate")
