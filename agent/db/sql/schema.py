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
    node_id               VARCHAR(64) NULL,     -- network_node resources (inactive).
                                                 -- STRUCTURAL FIX (future NETWORK_NODE
                                                 -- provenance hook INSPECT): was
                                                 -- VARCHAR(40) — too narrow for this
                                                 -- codebase's REAL node identity
                                                 -- (node/src/core/identity.rs::
                                                 -- NodeIdentity::node_id(), a 32-byte
                                                 -- Ed25519-derived HashId, hex-encoded
                                                 -- to exactly 64 chars). Widened before
                                                 -- any writer exists, zero migration
                                                 -- risk. Do NOT confuse this with
                                                 -- agent/orch_federation.py's/agent/
                                                 -- orch_reputation.py's OWN unrelated
                                                 -- "node_id" — short human-readable
                                                 -- labels for LOCAL background-
                                                 -- validation council members
                                                 -- ("local-qwen14b-a", "council-claude"),
                                                 -- not P2P peer identity at all. See
                                                 -- the NETWORK_NODE PROVENANCE
                                                 -- extension-contract note below.
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

# ── AI_OBSERVATION / AI_REPORTED_SOURCE ──────────────────────────────────
# Architectural stub for a FUTURE capability — no AI-chat transport, no
# Claude/Gemini/ChatGPT/DeepSeek/Qwen API calls, no browser automation,
# no AI_CHAT route activation, and no independence counting exist
# anywhere in this codebase yet. This is schema only: a place for a
# future AI_OBSERVATION collector to write into without a second,
# incompatible persistence design being invented later. APPEND-ONLY,
# both tables — an AI's self-report is a historical utterance, never
# corrected in place.
#
# CONCEPT: AI SELF-REPORTED PROVENANCE.
#
# SELF-REPORTED PROVENANCE IS AN OBSERVATION, NOT VERIFIED PROVENANCE.
#
# When an external AI model is asked (a future standard YANDI prompt,
# not built here) whether it used live search/external sources for a
# specific answer, its own answer to THAT question is itself just
# another self-report — not evidence YANDI has independently verified.
# "AI model says: I used NASA" means reported_source = NASA, never
# verified_provenance_root = NASA. Only a LATER, independent YANDI
# check (a future PROVENANCE_RESOLUTION step, deliberately NOT built
# here) could ever connect a reported_uri to a real SOURCE_RESOURCE —
# which is exactly why AI_REPORTED_SOURCE carries NO foreign key to
# source_resource: that FK would assert an identity nothing has proven.
#
# WHY THIS IS TWO NEW TABLES, NOT AN EXTENSION OF SOURCE_RESOURCE/
# SOURCE_OBSERVATION (the INSPECT conclusion this pass reached): those
# two model "a real observation of a real resource" — SOURCE_RESOURCE's
# identity is a canonical_uri/uri_hash; SOURCE_OBSERVATION is exactly
# ONE observation of exactly ONE resource per row. An AI self-report is
# structurally different on both counts: (a) the "resource" being
# identified is a provider+model, which has no URI at all and no
# working identity/dedup path today (source_resource.uri_hash is the
# only UNIQUE key, and it is NULL for any non-internet resource_type —
# MySQL does not enforce uniqueness across NULLs, so get_or_create_
# resource() would silently create a fresh, undeduplicated row every
# single call for a "resource_type='ai_chat'" observation with no URI);
# (b) ONE AI answer can report MANY external sources at once (1-to-N),
# which SOURCE_OBSERVATION's one-observation-one-resource shape cannot
# express without fabricating N separate "resources" for strings the
# model merely claimed, exactly the "AI_REPORTED_SOURCE == SOURCE_
# RESOURCE" conflation the concept above forbids. Reusing the existing
# tables would require either lying about resource identity or
# smuggling self-reported strings in as if they were independently
# observed — both worse than two small, honestly-named new tables.
#
# Do NOT treat multiple AI observations reporting the same source name
# as independent provenance roots: Gemini, Claude, and ChatGPT all
# reporting "NASA" are three AI_OBSERVATION rows that may all point at
# ONE real provenance root, or none — never three independent roots by
# virtue of being three different providers. That judgment belongs to
# the future PROVENANCE_RESOLUTION step (UNRESOLVED/MATCHED/AMBIGUOUS/
# REJECTED — not implemented here), never to a count of AI_OBSERVATION
# rows.

AI_OBSERVATION = """
CREATE TABLE IF NOT EXISTS ai_observation (
    ai_observation_id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    provider                      VARCHAR(40) NOT NULL,   -- e.g. 'anthropic','google','openai' — free text, no enum: providers are not YANDI's to enumerate closed
    model_id                      VARCHAR(80) NOT NULL,   -- e.g. 'claude-opus-5', 'gemini-3-pro'
    run_id                        VARCHAR(40) NULL,       -- the YANDI run that triggered this observation, if any (nullable: e.g. a council-chat exchange with no owning verification run)
    prompt_identity                CHAR(64) NULL,          -- sha256 of the prompt/request text — identity/dedup key, not the raw prompt itself
    answer_excerpt                  TEXT NULL,              -- excerpt only, matches source_observation.content_excerpt's own no-raw-dump discipline
    provenance_mode_reported        ENUM('MODEL_KNOWLEDGE','LIVE_SOURCES','MIXED','UNKNOWN') NOT NULL,
    live_search_used_reported       ENUM('YES','NO','UNKNOWN') NOT NULL,
    provenance_parse_status          VARCHAR(30) NOT NULL,   -- e.g. 'parsed'/'malformed'/'missing' — small controlled vocabulary, left open (no parser exists yet to enumerate it against)
    observed_at                     DATETIME NOT NULL,
    CONSTRAINT fk_aiobs_run FOREIGN KEY (run_id)
        REFERENCES verification_run(run_id),
    KEY idx_aiobs_provider_model (provider, model_id),
    KEY idx_aiobs_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

AI_REPORTED_SOURCE = """
CREATE TABLE IF NOT EXISTS ai_reported_source (
    ai_reported_source_id  BIGINT AUTO_INCREMENT PRIMARY KEY,
    ai_observation_id       BIGINT NOT NULL,
    ordinal                  INT NULL,
    reported_name             VARCHAR(255) NULL,
    reported_uri              VARCHAR(2048) NULL,   -- self-reported by the AI, NEVER canonicalized/deduped against source_resource here — see module note above
    CONSTRAINT fk_airs_observation FOREIGN KEY (ai_observation_id)
        REFERENCES ai_observation(ai_observation_id),
    KEY idx_airs_observation (ai_observation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# ── NETWORK_NODE PROVENANCE — extension contract (documentation only) ───
#
# Architectural stub for a FUTURE P2P capability. NOT built here: any
# network protocol, node discovery, DHT, onion routing, reputation
# protocol, consensus, voting, distributed Trust, remote revalidation,
# cryptographic signing beyond what node/src/core/identity.rs already
# provides, or a NETWORK_NODE route activation. This is documentation
# and one structural fix (source_resource.node_id widened above), not
# new tables — the INSPECT conclusion below is "extensible enough for
# now", matching mandate §19's "если схема уже расширяема — не создавать
# сейчас десять таблиц".
#
# CONCEPT (shared with AI_OBSERVATION above — one fundamental rule):
#
#     SELF-REPORT != VERIFIED PROVENANCE.
#
#     REMOTE NODE PROVENANCE IS A CLAIM ABOUT PROVENANCE.
#     IT IS NOT PROVENANCE UNTIL LOCALLY RESOLVED.
#
#     DO NOT COUNT NODES. COUNT DISTINGUISHABLE PROVENANCE ROOTS.
#
# NODE IDENTITY — REUSE, do not invent a second scheme: node/src/core/
# identity.rs::NodeIdentity::node_id() (== .address, a real Ed25519/
# X25519-derived 32-byte HashId, hex-encoded to exactly 64 chars,
# persisted encrypted at rest) is the one real cryptographic node
# identity this codebase has. NOT an IP address, hostname, or temporary
# connection ID — those are transport endpoints, not epistemic identity,
# and can change without the node's identity changing.
#
# DO NOT CONFUSE with a DIFFERENT, ALREADY-ACTIVE "node_id" concept:
# agent/orch_federation.py and agent/orch_reputation.py already use
# "node_id" for short human-readable labels identifying LOCAL
# background-validation council members ("local-qwen14b-a",
# "council-claude", "yandi-council" — see the live "[BG] Ноды: [...]"
# log line). Those are not P2P peers and have nothing to do with remote
# node provenance — a future NETWORK_NODE implementation must not
# silently conflate the two "node_id" namespaces.
#
# SQL_ARCHITECTURE_CHECK — 10 questions answered (mandate's own §18):
#
#  1. Represent a remote node observation without breaking SOURCE_
#     RESOURCE? NO, for the same structural reason AI_OBSERVATION
#     needed its own tables: a node's self-report is 1-to-N (many
#     claims, many sources, an upstream node it says IT used) and
#     hierarchical (nested upstream provenance), which SOURCE_
#     OBSERVATION's one-observation-of-one-resource shape cannot
#     express without fabricating resources for things only claimed.
#     A future NODE_OBSERVATION would be structurally analogous to
#     AI_OBSERVATION, not a SOURCE_OBSERVATION extension.
#  2. Where does node identity live? source_resource.node_id (widened
#     to VARCHAR(64) above) once resource_type='network_node' is ever
#     activated — reusing the Rust HashId, per "REUSE EXISTING IDENTITY"
#     above, not a second identifier.
#  3. Where does network observation live? A future NODE_OBSERVATION
#     table (not built here): remote_node_id, run_id (nullable — an
#     exchange need not belong to one YANDI run), remote_answer,
#     remote_canonical_trust_reported, remote_confidence_reported,
#     protocol_version, payload_schema_version, payload_hash (mandate
#     §16: hash for integrity audit, never the raw packet),
#     observed_at. Claims would need their own child table (a node's
#     answer can carry several), same 1-to-N shape as AI_REPORTED_SOURCE.
#  4. Represent reported provenance? A future NODE_REPORTED_SOURCE,
#     structurally identical to AI_REPORTED_SOURCE — reported route
#     (one of LOCAL_MODEL/LOCAL_MEMORY/INTERNET/NETWORK_NODE/AI_CHAT),
#     reported name/uri, NO foreign key to source_resource (same
#     critical invariant AI_REPORTED_SOURCE already enforces).
#  5. Represent a nested upstream node? A self-referencing FK on the
#     future NODE_OBSERVATION (upstream_observation_id), exactly the
#     same pattern SOURCE_OBSERVATION.origin_observation_id already
#     proves out for local_memory replay chains — reused, not invented.
#  6. Preserve lineage? exchange_id + parent_exchange_id + hop_depth
#     columns on the future NODE_OBSERVATION (or a small dedicated
#     EXCHANGE table) — enough for a FUTURE loop-detection pass to
#     walk without this pass implementing any traversal itself.
#  7. Avoid false independence? Never count NODE_OBSERVATION rows (or
#     distinct node_ids) as independent roots. Independence is decided
#     at the level of REAL, LOCALLY RESOLVED provenance roots (the same
#     job agent/epistemic_source_independence.py already does for
#     internet sources) — never by node/provider count. NODE_A,
#     NODE_C, and a Gemini observation all reporting "NASA" collapse to
#     candidates for ONE root, never three.
#  8. Connect a remote reported source to a later locally resolved
#     SOURCE_RESOURCE? A future PROVENANCE_RESOLUTION step (not built
#     here): reported_uri -> canonicalize (reuse SharedFetchCache's
#     canonicalizer, not a second one) -> local fetch -> a REAL
#     source_observation -> a resolution status (UNRESOLVED / MATCHED /
#     AMBIGUOUS / REJECTED). Never a direct FK asserting the identity
#     before that work happens.
#  9. Represent a node's answer changing over time? Append-only
#     NODE_OBSERVATION, one row per exchange — same ANSWER_VERSION-style
#     discipline this schema already uses everywhere else; never an
#     UPDATE of a "current" node answer.
# 10. Represent node-reported Trust separately from local canonical
#     Trust? remote_canonical_trust_reported as a plain stored field on
#     NODE_OBSERVATION — an observation of what the OTHER node claims,
#     never merged into or compared against answer_assessment.
#     canonical_trust by anything in this pass.
#
# FUTURE INVARIANTS (mandate §21 — kept as a checkable list so future
# code/tests can assert against the canonical wording instead of each
# re-deriving it):

NETWORK_NODE_PROVENANCE_INVARIANTS = (
    "NODE_ID != INDEPENDENT_ROOT",
    "NODE_RELAY != NEW_ROOT",
    "MEMORY_REPLAY != NEW_ROOT",
    "REMOTE_REPORTED_SOURCE != LOCALLY_VERIFIED_SOURCE",
    "REMOTE_REPORTED_TRUST != LOCAL_CANONICAL_TRUST",
    "REMOTE_AI_REPORT != LOCAL_AI_OBSERVATION",
    "SELF_REPORTED_PROVENANCE != VERIFIED_PROVENANCE",
    "SAME URL THROUGH MULTIPLE NODES != MULTIPLE ROOTS",
    "HISTORICAL OBSERVATIONS ARE APPEND-ONLY",
)

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
    ("ai_observation", AI_OBSERVATION),
    ("ai_reported_source", AI_REPORTED_SOURCE),
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
