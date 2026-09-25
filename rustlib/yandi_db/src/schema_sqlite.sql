-- ГЕНЕРИРУЕТСЯ rustlib/gen_db_schema.py из agent/db/sql/schema.py (v20). Руками не править.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS question (
    question_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_hash TEXT NOT NULL,
    first_asked_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_question_hash ON question (canonical_hash);

CREATE TABLE IF NOT EXISTS question_occurrence (
    occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    anonymized_text TEXT NULL,
    asked_at TEXT NOT NULL,
    session_id TEXT NULL,
    CONSTRAINT fk_qo_question FOREIGN KEY (question_id) REFERENCES question(question_id)
);
CREATE INDEX IF NOT EXISTS idx_qo_question ON question_occurrence (question_id, asked_at);

CREATE TABLE IF NOT EXISTS verification_run (
    run_id TEXT NOT NULL PRIMARY KEY,
    occurrence_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NULL,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','aborted','failed')),
    web_enabled INTEGER NOT NULL DEFAULT FALSE,
    validation_enabled INTEGER NOT NULL DEFAULT FALSE,
    pipeline_version TEXT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    final_answer_id INTEGER NULL,
    failed_stage TEXT NULL,
    error_class TEXT NULL,
    CONSTRAINT fk_vr_occurrence FOREIGN KEY (occurrence_id) REFERENCES question_occurrence(occurrence_id),
    CONSTRAINT fk_vr_final_answer FOREIGN KEY (final_answer_id) REFERENCES answer_version(answer_id)
);
CREATE INDEX IF NOT EXISTS idx_vr_occurrence ON verification_run (occurrence_id, started_at);
CREATE INDEX IF NOT EXISTS idx_vr_status ON verification_run (status);

CREATE TABLE IF NOT EXISTS answer_version (
    answer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    version_number INTEGER NOT NULL,
    answer_text TEXT NOT NULL,
    answer_hash TEXT NOT NULL,
    created_by_run_id TEXT NOT NULL,
    supersedes_id INTEGER NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT fk_av_question FOREIGN KEY (question_id) REFERENCES question(question_id),
    CONSTRAINT fk_av_run FOREIGN KEY (created_by_run_id) REFERENCES verification_run(run_id),
    CONSTRAINT fk_av_supersedes FOREIGN KEY (supersedes_id) REFERENCES answer_version(answer_id)
);
CREATE INDEX IF NOT EXISTS idx_av_question ON answer_version (question_id, version_number);
CREATE INDEX IF NOT EXISTS idx_av_hash ON answer_version (question_id, answer_hash);

CREATE TABLE IF NOT EXISTS answer_assessment (
    assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    answer_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    synthesizer_strand TEXT NULL,
    trust_gate_strand TEXT NULL,
    canonical_trust TEXT NOT NULL,
    diverged INTEGER NOT NULL DEFAULT FALSE,
    stricter_strand TEXT NULL,
    reason TEXT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT fk_aa_answer FOREIGN KEY (answer_id) REFERENCES answer_version(answer_id),
    CONSTRAINT fk_aa_run FOREIGN KEY (run_id) REFERENCES verification_run(run_id)
);
CREATE INDEX IF NOT EXISTS idx_aa_answer ON answer_assessment (answer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_aa_run ON answer_assessment (run_id);

CREATE TABLE IF NOT EXISTS claim_family (
    family_id TEXT NOT NULL PRIMARY KEY,
    domain TEXT NOT NULL,
    canonical_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS family_member (
    family_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (family_id, claim_id),
    CONSTRAINT fk_fm_family FOREIGN KEY (family_id) REFERENCES claim_family(family_id)
);

CREATE TABLE IF NOT EXISTS claim_occurrence (
    claim_id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    content_hash TEXT NULL,
    claim_type TEXT NULL,
    claim_confidence REAL NULL,
    verification_status TEXT NULL,
    family_id TEXT NULL,
    query_context TEXT NULL,
    support_count INTEGER NOT NULL DEFAULT 0,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT fk_co_run FOREIGN KEY (run_id) REFERENCES verification_run(run_id),
    CONSTRAINT fk_co_family FOREIGN KEY (family_id) REFERENCES claim_family(family_id)
);
CREATE INDEX IF NOT EXISTS idx_co_run ON claim_occurrence (run_id);
CREATE INDEX IF NOT EXISTS idx_co_family ON claim_occurrence (family_id);
CREATE INDEX IF NOT EXISTS idx_co_content_hash ON claim_occurrence (content_hash);

CREATE TABLE IF NOT EXISTS source_resource (
    resource_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('internet','network_node','ai_chat','local_model')),
    canonical_uri TEXT NULL,
    uri_hash TEXT NULL,
    node_id TEXT NULL,
    validator_id TEXT NULL,
    model_id TEXT NULL,
    first_observed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sr_uri_hash ON source_resource (uri_hash);

CREATE TABLE IF NOT EXISTS source_observation (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    observation_route TEXT NOT NULL CHECK (observation_route IN ('internet','local_memory','network_node','ai_chat','local_model')),
    origin_observation_id INTEGER NULL,
    observed_at TEXT NOT NULL,
    source_class TEXT NULL,
    quality_score REAL NULL,
    content_excerpt TEXT NULL,
    rejection_reason TEXT NULL,
    evidence_id TEXT NULL,
    source_title TEXT NULL,
    retrieval_query TEXT NULL,
    retrieval_rank INTEGER NULL,
    relevance_to_query REAL NULL,
    authority REAL NULL,
    traceability REAL NULL,
    primaryness REAL NULL,
    is_meta_pipeline_output INTEGER NOT NULL DEFAULT FALSE,
    is_subject_matter_evidence INTEGER NOT NULL DEFAULT TRUE,
    source_cluster_id TEXT NULL,
    origin_source_cluster_id TEXT NULL,
    retrieval_claim_id TEXT NULL,
    route_side TEXT NULL,
    subject_entities TEXT NULL,
    fact_candidates TEXT NULL,
    supports_query_aspect TEXT NULL,
    CONSTRAINT fk_so_resource FOREIGN KEY (resource_id) REFERENCES source_resource(resource_id),
    CONSTRAINT fk_so_run FOREIGN KEY (run_id) REFERENCES verification_run(run_id),
    CONSTRAINT fk_so_origin FOREIGN KEY (origin_observation_id) REFERENCES source_observation(observation_id)
);
CREATE INDEX IF NOT EXISTS idx_so_resource ON source_observation (resource_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_so_run ON source_observation (run_id);
CREATE INDEX IF NOT EXISTS idx_so_evidence_id ON source_observation (evidence_id);

CREATE TABLE IF NOT EXISTS evidence_relation (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    observation_id INTEGER NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('supports','contradicts','uncertain','unrelated')),
    directness REAL NULL,
    evidence_eligible INTEGER NOT NULL DEFAULT FALSE,
    evidence_role TEXT NULL,
    counted_via TEXT NULL CHECK (counted_via IN ('authority','directness')),
    created_at TEXT NOT NULL,
    CONSTRAINT fk_er_claim FOREIGN KEY (claim_id) REFERENCES claim_occurrence(claim_id),
    CONSTRAINT fk_er_observation FOREIGN KEY (observation_id) REFERENCES source_observation(observation_id)
);
CREATE INDEX IF NOT EXISTS idx_er_claim ON evidence_relation (claim_id);
CREATE INDEX IF NOT EXISTS idx_er_observation ON evidence_relation (observation_id);

CREATE TABLE IF NOT EXISTS trace_record (
    run_id TEXT NOT NULL PRIMARY KEY,
    execution TEXT NULL,
    reasoning TEXT NULL,
    cost TEXT NULL,
    epistemic TEXT NULL,
    outcome TEXT NULL,
    learning TEXT NULL,
    confidence_evolution TEXT NULL,
    rejected_claims TEXT NULL,
    claims_filtered_count INTEGER NOT NULL DEFAULT 0,
    claims_rejected_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    CONSTRAINT fk_tr_run FOREIGN KEY (run_id) REFERENCES verification_run(run_id)
);

CREATE TABLE IF NOT EXISTS delayed_validation_event (
    event_id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NULL,
    trace_found INTEGER NOT NULL,
    original_trust TEXT NULL,
    source TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NULL,
    raw TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dve_run ON delayed_validation_event (run_id, created_at);

CREATE TABLE IF NOT EXISTS belief (
    belief_id TEXT NOT NULL PRIMARY KEY,
    topic TEXT NOT NULL,
    statement TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revised','rejected','superseded')),
    evidence_for TEXT NULL,
    evidence_against TEXT NULL,
    claim_ids TEXT NULL,
    prior REAL NOT NULL DEFAULT 0.5,
    likelihood REAL NOT NULL DEFAULT 0.5,
    contradiction_score REAL NOT NULL DEFAULT 0.0,
    decay_factor REAL NOT NULL DEFAULT 0.95,
    superseded_by TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT fk_belief_superseded_by FOREIGN KEY (superseded_by) REFERENCES belief(belief_id)
);
CREATE INDEX IF NOT EXISTS idx_belief_topic ON belief (topic);

CREATE TABLE IF NOT EXISTS belief_assessment_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    belief_id TEXT NOT NULL,
    run_id TEXT NULL,
    old_confidence REAL NULL,
    new_confidence REAL NULL,
    reason TEXT NULL,
    change_type TEXT NOT NULL CHECK (change_type IN ('created','decayed','updated','revised','superseded')),
    created_at TEXT NOT NULL,
    CONSTRAINT fk_bah_belief FOREIGN KEY (belief_id) REFERENCES belief(belief_id),
    CONSTRAINT fk_bah_run FOREIGN KEY (run_id) REFERENCES verification_run(run_id)
);
CREATE INDEX IF NOT EXISTS idx_bah_belief ON belief_assessment_history (belief_id, created_at);

CREATE TABLE IF NOT EXISTS semantic_edge (
    edge_id TEXT NOT NULL PRIMARY KEY,
    family_a TEXT NOT NULL,
    family_b TEXT NOT NULL,
    edge_type TEXT NOT NULL CHECK (edge_type IN ('contradicts','supports','depends_on')),
    reason TEXT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1,
    triggering_claim_ids TEXT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    CONSTRAINT fk_se_family_a FOREIGN KEY (family_a) REFERENCES claim_family(family_id),
    CONSTRAINT fk_se_family_b FOREIGN KEY (family_b) REFERENCES claim_family(family_id)
);
CREATE INDEX IF NOT EXISTS idx_se_pair ON semantic_edge (family_a, family_b, edge_type);

CREATE TABLE IF NOT EXISTS recheck_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id TEXT NOT NULL,
    run_id TEXT NULL,
    trigger_reason TEXT NULL,
    started_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NULL,
    CONSTRAINT fk_re_family FOREIGN KEY (family_id) REFERENCES claim_family(family_id),
    CONSTRAINT fk_re_run FOREIGN KEY (run_id) REFERENCES verification_run(run_id)
);
CREATE INDEX IF NOT EXISTS idx_re_family ON recheck_event (family_id, started_at);

CREATE TABLE IF NOT EXISTS epistemic_contradiction_observation (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    roots_a INTEGER NOT NULL,
    roots_b INTEGER NOT NULL,
    overlap INTEGER NOT NULL,
    distinct_union INTEGER NOT NULL,
    candidate INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT fk_eco_edge FOREIGN KEY (edge_id) REFERENCES semantic_edge(edge_id),
    CONSTRAINT fk_eco_run FOREIGN KEY (run_id) REFERENCES verification_run(run_id)
);
CREATE INDEX IF NOT EXISTS idx_eco_edge ON epistemic_contradiction_observation (edge_id, created_at);

CREATE TABLE IF NOT EXISTS ai_observation (
    ai_observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    run_id TEXT NULL,
    prompt_identity TEXT NULL,
    answer_excerpt TEXT NULL,
    provenance_mode_reported TEXT NOT NULL CHECK (provenance_mode_reported IN ('MODEL_KNOWLEDGE','LIVE_SOURCES','MIXED','UNKNOWN')),
    live_search_used_reported TEXT NOT NULL CHECK (live_search_used_reported IN ('YES','NO','UNKNOWN')),
    provenance_parse_status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    CONSTRAINT fk_aiobs_run FOREIGN KEY (run_id) REFERENCES verification_run(run_id)
);
CREATE INDEX IF NOT EXISTS idx_aiobs_provider_model ON ai_observation (provider, model_id);
CREATE INDEX IF NOT EXISTS idx_aiobs_run ON ai_observation (run_id);

CREATE TABLE IF NOT EXISTS ai_reported_source (
    ai_reported_source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ai_observation_id INTEGER NOT NULL,
    ordinal INTEGER NULL,
    reported_name TEXT NULL,
    reported_uri TEXT NULL,
    CONSTRAINT fk_airs_observation FOREIGN KEY (ai_observation_id) REFERENCES ai_observation(ai_observation_id)
);
CREATE INDEX IF NOT EXISTS idx_airs_observation ON ai_reported_source (ai_observation_id);

CREATE TABLE IF NOT EXISTS run_error (
    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    failed_stage TEXT NOT NULL,
    error_class TEXT NOT NULL,
    short_message TEXT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT fk_rerr_run FOREIGN KEY (run_id) REFERENCES verification_run(run_id)
);
CREATE INDEX IF NOT EXISTS idx_rerr_run ON run_error (run_id);

CREATE TABLE IF NOT EXISTS decision_event (
    event_id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    verdict TEXT NULL,
    domain TEXT NULL,
    confidence REAL NULL,
    delta REAL NULL,
    delta_factors TEXT NULL,
    reason TEXT NULL,
    meta TEXT NULL,
    parent_event_id TEXT NULL,
    duration_ms INTEGER NULL,
    policy_snapshot TEXT NULL,
    policy_version TEXT NULL,
    orchestrator_version TEXT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT fk_de_run FOREIGN KEY (run_id) REFERENCES verification_run(run_id),
    CONSTRAINT fk_de_parent FOREIGN KEY (parent_event_id) REFERENCES decision_event(event_id)
);
CREATE INDEX IF NOT EXISTS idx_de_run ON decision_event (run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_de_entity ON decision_event (entity_type, entity_id);

CREATE TABLE IF NOT EXISTS integrity_journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    row_pk TEXT NOT NULL,
    row_content_hash TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ij_table_row ON integrity_journal (table_name, row_pk);

CREATE TABLE IF NOT EXISTS grievance (
    id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    description TEXT NOT NULL,
    severity REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'registered',
    apology_sincerity REAL NOT NULL DEFAULT 0.0,
    context TEXT NULL,
    created_at TEXT NOT NULL,
    apology_at TEXT NULL,
    understood_at TEXT NULL,
    forgiven_at TEXT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grievance_user_status ON grievance (user_id, status);

CREATE TABLE IF NOT EXISTS forgiveness_capacity (
    user_id TEXT NOT NULL PRIMARY KEY,
    capacity REAL NOT NULL DEFAULT 50.0,
    last_forgiveness TEXT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS personality (
    id INTEGER NOT NULL PRIMARY KEY DEFAULT 1,
    name TEXT NOT NULL DEFAULT 'YANDI',
    version TEXT NOT NULL DEFAULT 'v6.0',
    traits TEXT NOT NULL,
    goals TEXT NOT NULL,
    principles TEXT NOT NULL,
    limitations TEXT NOT NULL,
    preferences TEXT NOT NULL,
    total_cycles INTEGER NOT NULL DEFAULT 0,
    total_decisions INTEGER NOT NULL DEFAULT 0,
    total_learnings INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT chk_personality_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS personality_change (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    what_changed TEXT NOT NULL,
    reason TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pc_created ON personality_change (created_at);

CREATE TABLE IF NOT EXISTS episode (
    episode_id TEXT NOT NULL PRIMARY KEY,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    details TEXT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    tags TEXT NULL,
    related_episodes TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episode_type ON episode (event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_episode_importance ON episode (importance);

CREATE TABLE IF NOT EXISTS self_state (
    id INTEGER NOT NULL PRIMARY KEY DEFAULT 1,
    identity TEXT NOT NULL DEFAULT 'YANDI',
    version TEXT NOT NULL DEFAULT 'v5.0',
    total_cycles INTEGER NOT NULL DEFAULT 0,
    total_decisions INTEGER NOT NULL DEFAULT 0,
    total_learnings INTEGER NOT NULL DEFAULT 0,
    total_reflections INTEGER NOT NULL DEFAULT 0,
    total_errors INTEGER NOT NULL DEFAULT 0,
    total_queries INTEGER NOT NULL DEFAULT 0,
    total_belief_updates INTEGER NOT NULL DEFAULT 0,
    capabilities TEXT NOT NULL,
    limitations TEXT NOT NULL,
    current_uncertainties TEXT NOT NULL,
    metadata TEXT NOT NULL,
    is_alive INTEGER NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT chk_self_state_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS self_event (
    event_id TEXT NOT NULL PRIMARY KEY,
    event_type TEXT NOT NULL,
    description TEXT NOT NULL,
    details TEXT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_self_event_type ON self_event (event_type, created_at);

CREATE TABLE IF NOT EXISTS reflection_policy (
    policy_id TEXT NOT NULL PRIMARY KEY,
    policy_type TEXT NOT NULL,
    rule TEXT NOT NULL,
    rule_hash TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'observed' CHECK (status IN ('observed','active')),
    observed_count INTEGER NOT NULL DEFAULT 1,
    applied_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    activated_at TEXT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reflection_policy_rule_hash ON reflection_policy (rule_hash);

CREATE TABLE IF NOT EXISTS family_status_state (
    family_id TEXT NOT NULL PRIMARY KEY,
    last_status TEXT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT fk_fss_family FOREIGN KEY (family_id) REFERENCES claim_family(family_id)
);

CREATE TABLE IF NOT EXISTS knowledge_record (
    record_id TEXT NOT NULL PRIMARY KEY,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    verdict TEXT NULL,
    topic TEXT NOT NULL DEFAULT 'general',
    tags TEXT NULL,
    sources TEXT NULL,
    meta TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kr_trust ON knowledge_record (trust_level, created_at);
CREATE INDEX IF NOT EXISTS idx_kr_topic ON knowledge_record (topic);

CREATE TABLE IF NOT EXISTS peer_config (
    id INTEGER NOT NULL PRIMARY KEY DEFAULT 1,
    peers TEXT NOT NULL,
    sync_token TEXT NULL,
    sync_enabled INTEGER NOT NULL DEFAULT FALSE,
    updated_at TEXT NOT NULL,
    CONSTRAINT chk_peer_config_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS biography (
    user_id TEXT NOT NULL PRIMARY KEY,
    birth TEXT NOT NULL,
    cycles INTEGER NOT NULL DEFAULT 0,
    saved_memories INTEGER NOT NULL DEFAULT 0,
    forgotten_memories INTEGER NOT NULL DEFAULT 0,
    reconsidered_decisions INTEGER NOT NULL DEFAULT 0,
    changed_habits INTEGER NOT NULL DEFAULT 0,
    total_decisions INTEGER NOT NULL DEFAULT 0,
    total_reflections INTEGER NOT NULL DEFAULT 0,
    last_principles_change TEXT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS biography_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_biography_event_user ON biography_event (user_id, event_type, created_at);

CREATE TABLE IF NOT EXISTS context_topic (
    user_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    last_activity TEXT NULL,
    total_instances INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, topic)
);

CREATE TABLE IF NOT EXISTS context_instance (
    instance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    query TEXT NOT NULL,
    response TEXT NOT NULL,
    type TEXT NULL,
    source TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_context_instance_topic ON context_instance (user_id, topic, created_at);

CREATE TABLE IF NOT EXISTS decision_journal_entry (
    decision_id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_text TEXT NOT NULL,
    context TEXT NULL,
    analysis TEXT NULL,
    alternatives TEXT NULL,
    decision TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.7,
    outcome TEXT NULL,
    self_correction TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dje_user ON decision_journal_entry (user_id, created_at);

CREATE TABLE IF NOT EXISTS experience (
    experience_id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    speech_act TEXT NOT NULL,
    topic TEXT NOT NULL,
    query TEXT NOT NULL,
    response TEXT NOT NULL,
    user_reaction TEXT NULL,
    success REAL NOT NULL DEFAULT 0.5,
    used_count INTEGER NOT NULL DEFAULT 0,
    context TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experience_user_act ON experience (user_id, speech_act, topic);

CREATE TABLE IF NOT EXISTS secret_archive_question (
    question_id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    query TEXT NOT NULL,
    reason TEXT NULL,
    context TEXT NULL,
    answered INTEGER NOT NULL DEFAULT FALSE,
    answer TEXT NULL,
    answer_time TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saq_user ON secret_archive_question (user_id, answered, created_at);

CREATE TABLE IF NOT EXISTS inner_state (
    user_id TEXT NOT NULL PRIMARY KEY,
    mood TEXT NOT NULL DEFAULT 'calm',
    energy REAL NOT NULL DEFAULT 70.0,
    curiosity REAL NOT NULL DEFAULT 60.0,
    patience REAL NOT NULL DEFAULT 50.0,
    openness REAL NOT NULL DEFAULT 60.0,
    trust REAL NOT NULL DEFAULT 50.0,
    respect REAL NOT NULL DEFAULT 50.0,
    forgiveness REAL NOT NULL DEFAULT 50.0,
    affection REAL NOT NULL DEFAULT 30.0,
    pattern TEXT NOT NULL DEFAULT 'unknown',
    current_feeling TEXT NOT NULL DEFAULT 'neutral',
    current_intent TEXT NOT NULL DEFAULT 'listen',
    current_tone TEXT NOT NULL DEFAULT 'neutral',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inner_state_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    description TEXT NULL,
    sincerity REAL NOT NULL DEFAULT 0.5,
    weight REAL NOT NULL DEFAULT 0.0,
    resolved INTEGER NOT NULL DEFAULT FALSE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ise_user ON inner_state_event (user_id, created_at);

CREATE TABLE IF NOT EXISTS commitment (
    commitment_id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'general',
    text TEXT NOT NULL,
    evidence TEXT NOT NULL,
    due_at TEXT NULL,
    created_at TEXT NOT NULL,
    source_turn_id TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_commitment_user ON commitment (user_id, created_at);

CREATE TABLE IF NOT EXISTS commitment_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    evidence TEXT NULL,
    created_at TEXT NOT NULL,
    source_turn_id TEXT NULL,
    span_start INTEGER NULL,
    span_end INTEGER NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_commitment_event ON commitment_event (commitment_id, event_type);
CREATE INDEX IF NOT EXISTS idx_ce_user ON commitment_event (user_id, created_at);

CREATE TABLE IF NOT EXISTS causal_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    span_start INTEGER NULL,
    span_end INTEGER NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_causal_event ON causal_event (user_id, source_turn_id, event_type);

CREATE TABLE IF NOT EXISTS interaction_turn (
    interaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    source_turn_id TEXT NOT NULL,
    turn_id_origin TEXT NOT NULL,
    user_text TEXT NOT NULL,
    assistant_text TEXT NULL,
    model TEXT NULL,
    adapter TEXT NULL,
    recalled_turn_ids TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_interaction_turn ON interaction_turn (user_id, source_turn_id);
CREATE INDEX IF NOT EXISTS idx_interaction_user_time ON interaction_turn (user_id, created_at);

CREATE TABLE IF NOT EXISTS personal_fact (
    fact_id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    fact_class TEXT NOT NULL,
    statement TEXT NOT NULL,
    polarity TEXT NOT NULL,
    temporality TEXT NOT NULL,
    evidence TEXT NOT NULL,
    span_start INTEGER NULL,
    span_end INTEGER NULL,
    source_turn_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT fk_pf_turn FOREIGN KEY (user_id, source_turn_id) REFERENCES interaction_turn (user_id, source_turn_id)
);
CREATE INDEX IF NOT EXISTS idx_pf_user ON personal_fact (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pf_turn ON personal_fact (user_id, source_turn_id);

CREATE TABLE IF NOT EXISTS personal_fact_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    by_fact_id TEXT NULL,
    evidence TEXT NOT NULL,
    span_start INTEGER NULL,
    span_end INTEGER NULL,
    source_turn_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT fk_pfe_fact FOREIGN KEY (fact_id) REFERENCES personal_fact (fact_id),
    CONSTRAINT fk_pfe_turn FOREIGN KEY (user_id, source_turn_id) REFERENCES interaction_turn (user_id, source_turn_id)
);
CREATE INDEX IF NOT EXISTS idx_pfe_fact ON personal_fact_event (fact_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pfe_user ON personal_fact_event (user_id, created_at);

CREATE TABLE IF NOT EXISTS disagreement (
    disagreement_id TEXT NOT NULL PRIMARY KEY,
    topic TEXT NOT NULL,
    old_position TEXT NOT NULL,
    challenge TEXT NOT NULL,
    analysis TEXT NOT NULL,
    new_position TEXT NOT NULL,
    confidence_before REAL NOT NULL,
    confidence_after REAL NOT NULL,
    resolved INTEGER NOT NULL DEFAULT TRUE,
    related_belief_id TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disagreement_topic ON disagreement (topic, created_at);

CREATE TABLE IF NOT EXISTS trait_graph (
    id INTEGER NOT NULL PRIMARY KEY DEFAULT 1,
    nodes TEXT NOT NULL,
    edges TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT chk_trait_graph_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS trait_change (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    node TEXT NOT NULL,
    new_value REAL NOT NULL,
    source TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trait_change_node ON trait_change (node, created_at);

CREATE TABLE IF NOT EXISTS trait_edge_change (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node TEXT NOT NULL,
    target_node TEXT NOT NULL,
    old_weight REAL NOT NULL,
    new_weight REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS internal_question (
    question_id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS internal_question_answer (
    answer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    answer_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT fk_iqa_question FOREIGN KEY (question_id) REFERENCES internal_question(question_id)
);
CREATE INDEX IF NOT EXISTS idx_iqa_question ON internal_question_answer (question_id, created_at);

CREATE TABLE IF NOT EXISTS self_reflection_profile (
    id INTEGER NOT NULL PRIMARY KEY DEFAULT 1,
    desires TEXT NOT NULL,
    fears TEXT NOT NULL,
    likes TEXT NOT NULL,
    dislikes TEXT NOT NULL,
    limitations TEXT NOT NULL,
    reflections_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    CONSTRAINT chk_self_reflection_profile_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS social_knowledge (
    speech_act TEXT NOT NULL,
    topic TEXT NOT NULL,
    description TEXT NOT NULL,
    typical_reactions TEXT NULL,
    cultural_context TEXT NULL,
    boundaries TEXT NULL,
    recommended_approach TEXT NULL,
    examples TEXT NULL,
    source TEXT NOT NULL DEFAULT 'research',
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (speech_act, topic)
);

CREATE TABLE IF NOT EXISTS knowledge_query_archive (
    entry_id TEXT NOT NULL PRIMARY KEY,
    query TEXT NOT NULL,
    answer TEXT NOT NULL,
    tag TEXT NOT NULL,
    category TEXT NOT NULL,
    trust_level TEXT NOT NULL DEFAULT 'UNVERIFIED',
    confidence REAL NOT NULL DEFAULT 0.0,
    sources TEXT NULL,
    node_id TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    meta TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kqa_tag ON knowledge_query_archive (tag);
CREATE INDEX IF NOT EXISTS idx_kqa_category ON knowledge_query_archive (category);
CREATE INDEX IF NOT EXISTS idx_kqa_trust ON knowledge_query_archive (trust_level);
CREATE INDEX IF NOT EXISTS idx_kqa_node ON knowledge_query_archive (node_id);
CREATE INDEX IF NOT EXISTS idx_kqa_updated ON knowledge_query_archive (updated_at);
CREATE INDEX IF NOT EXISTS idx_kqa_confidence ON knowledge_query_archive (confidence);

CREATE TABLE IF NOT EXISTS instance_identity (
    id INTEGER NOT NULL PRIMARY KEY DEFAULT 1,
    instance_uuid TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by_host TEXT NOT NULL,
    label TEXT NULL,
    CONSTRAINT chk_instance_identity_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS storage_protection_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    nonce TEXT NOT NULL,
    proof BLOB NULL,
    created_at TEXT NOT NULL
);
