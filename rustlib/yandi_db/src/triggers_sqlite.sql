-- ГЕНЕРИРУЕТСЯ rustlib/gen_db_schema.py из agent/db/sql/security_triggers.py. Руками не править.

CREATE TRIGGER IF NOT EXISTS trg_schema_migrations_no_update
BEFORE UPDATE ON schema_migrations
BEGIN
  SELECT RAISE(ABORT, 'schema_migrations is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_schema_migrations_no_delete
BEFORE DELETE ON schema_migrations
BEGIN
  SELECT RAISE(ABORT, 'schema_migrations is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_question_no_update
BEFORE UPDATE ON question
BEGIN
  SELECT RAISE(ABORT, 'question is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_question_no_delete
BEFORE DELETE ON question
BEGIN
  SELECT RAISE(ABORT, 'question is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_question_occurrence_no_update
BEFORE UPDATE ON question_occurrence
BEGIN
  SELECT RAISE(ABORT, 'question_occurrence is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_question_occurrence_no_delete
BEFORE DELETE ON question_occurrence
BEGIN
  SELECT RAISE(ABORT, 'question_occurrence is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_verification_run_guard_update
BEFORE UPDATE ON verification_run
BEGIN
  SELECT RAISE(ABORT, 'verification_run: run_id/occurrence_id/started_at are immutable')
    WHERE NEW.run_id <> OLD.run_id OR NEW.occurrence_id <> OLD.occurrence_id OR NEW.started_at <> OLD.started_at;
  SELECT RAISE(ABORT, 'verification_run: status is already terminal, no further transition allowed')
    WHERE OLD.status <> 'running';
  SELECT RAISE(ABORT, 'verification_run: only running -> a terminal status is an allowed transition')
    WHERE NEW.status NOT IN ('completed', 'aborted', 'failed');
  SELECT RAISE(ABORT, 'verification_run: pipeline_version/web_enabled/validation_enabled/schema_version are write-once, set only at start_run() time')
    WHERE NEW.pipeline_version IS NOT OLD.pipeline_version OR NEW.web_enabled IS NOT OLD.web_enabled
       OR NEW.validation_enabled IS NOT OLD.validation_enabled OR NEW.schema_version IS NOT OLD.schema_version;
  SELECT RAISE(ABORT, 'verification_run: final_answer_id must belong to THIS run''s own question')
    WHERE NEW.final_answer_id IS NOT NULL AND (
      (SELECT question_id FROM answer_version WHERE answer_id = NEW.final_answer_id) IS NULL
      OR (SELECT question_id FROM answer_version WHERE answer_id = NEW.final_answer_id)
         <> (SELECT question_id FROM question_occurrence WHERE occurrence_id = NEW.occurrence_id));
END;
CREATE TRIGGER IF NOT EXISTS trg_verification_run_no_delete
BEFORE DELETE ON verification_run
BEGIN
  SELECT RAISE(ABORT, 'verification_run is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_answer_version_no_update
BEFORE UPDATE ON answer_version
BEGIN
  SELECT RAISE(ABORT, 'answer_version is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_answer_version_no_delete
BEFORE DELETE ON answer_version
BEGIN
  SELECT RAISE(ABORT, 'answer_version is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_answer_assessment_no_update
BEFORE UPDATE ON answer_assessment
BEGIN
  SELECT RAISE(ABORT, 'answer_assessment is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_answer_assessment_no_delete
BEFORE DELETE ON answer_assessment
BEGIN
  SELECT RAISE(ABORT, 'answer_assessment is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_claim_family_no_update
BEFORE UPDATE ON claim_family
BEGIN
  SELECT RAISE(ABORT, 'claim_family is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_claim_family_no_delete
BEFORE DELETE ON claim_family
BEGIN
  SELECT RAISE(ABORT, 'claim_family is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_family_member_no_update
BEFORE UPDATE ON family_member
BEGIN
  SELECT RAISE(ABORT, 'family_member is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_family_member_no_delete
BEFORE DELETE ON family_member
BEGIN
  SELECT RAISE(ABORT, 'family_member is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_claim_occurrence_no_update
BEFORE UPDATE ON claim_occurrence
BEGIN
  SELECT RAISE(ABORT, 'claim_occurrence is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_claim_occurrence_no_delete
BEFORE DELETE ON claim_occurrence
BEGIN
  SELECT RAISE(ABORT, 'claim_occurrence is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_resource_no_update
BEFORE UPDATE ON source_resource
BEGIN
  SELECT RAISE(ABORT, 'source_resource is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_resource_no_delete
BEFORE DELETE ON source_resource
BEGIN
  SELECT RAISE(ABORT, 'source_resource is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_observation_no_update
BEFORE UPDATE ON source_observation
BEGIN
  SELECT RAISE(ABORT, 'source_observation is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_source_observation_no_delete
BEFORE DELETE ON source_observation
BEGIN
  SELECT RAISE(ABORT, 'source_observation is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_evidence_relation_no_update
BEFORE UPDATE ON evidence_relation
BEGIN
  SELECT RAISE(ABORT, 'evidence_relation is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_evidence_relation_no_delete
BEFORE DELETE ON evidence_relation
BEGIN
  SELECT RAISE(ABORT, 'evidence_relation is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_trace_record_no_update
BEFORE UPDATE ON trace_record
BEGIN
  SELECT RAISE(ABORT, 'trace_record is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_trace_record_no_delete
BEFORE DELETE ON trace_record
BEGIN
  SELECT RAISE(ABORT, 'trace_record is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_delayed_validation_event_no_update
BEFORE UPDATE ON delayed_validation_event
BEGIN
  SELECT RAISE(ABORT, 'delayed_validation_event is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_delayed_validation_event_no_delete
BEFORE DELETE ON delayed_validation_event
BEGIN
  SELECT RAISE(ABORT, 'delayed_validation_event is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_belief_no_delete
BEFORE DELETE ON belief
BEGIN
  SELECT RAISE(ABORT, 'belief is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_belief_assessment_history_no_update
BEFORE UPDATE ON belief_assessment_history
BEGIN
  SELECT RAISE(ABORT, 'belief_assessment_history is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_belief_assessment_history_no_delete
BEFORE DELETE ON belief_assessment_history
BEGIN
  SELECT RAISE(ABORT, 'belief_assessment_history is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_semantic_edge_no_delete
BEFORE DELETE ON semantic_edge
BEGIN
  SELECT RAISE(ABORT, 'semantic_edge is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_recheck_event_no_update
BEFORE UPDATE ON recheck_event
BEGIN
  SELECT RAISE(ABORT, 'recheck_event is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_recheck_event_no_delete
BEFORE DELETE ON recheck_event
BEGIN
  SELECT RAISE(ABORT, 'recheck_event is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_epistemic_contradiction_observation_no_update
BEFORE UPDATE ON epistemic_contradiction_observation
BEGIN
  SELECT RAISE(ABORT, 'epistemic_contradiction_observation is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_epistemic_contradiction_observation_no_delete
BEFORE DELETE ON epistemic_contradiction_observation
BEGIN
  SELECT RAISE(ABORT, 'epistemic_contradiction_observation is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_ai_observation_no_update
BEFORE UPDATE ON ai_observation
BEGIN
  SELECT RAISE(ABORT, 'ai_observation is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_ai_observation_no_delete
BEFORE DELETE ON ai_observation
BEGIN
  SELECT RAISE(ABORT, 'ai_observation is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_ai_reported_source_no_update
BEFORE UPDATE ON ai_reported_source
BEGIN
  SELECT RAISE(ABORT, 'ai_reported_source is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_ai_reported_source_no_delete
BEFORE DELETE ON ai_reported_source
BEGIN
  SELECT RAISE(ABORT, 'ai_reported_source is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_run_error_no_update
BEFORE UPDATE ON run_error
BEGIN
  SELECT RAISE(ABORT, 'run_error is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_run_error_no_delete
BEFORE DELETE ON run_error
BEGIN
  SELECT RAISE(ABORT, 'run_error is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_decision_event_no_update
BEFORE UPDATE ON decision_event
BEGIN
  SELECT RAISE(ABORT, 'decision_event is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_decision_event_no_delete
BEFORE DELETE ON decision_event
BEGIN
  SELECT RAISE(ABORT, 'decision_event is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_integrity_journal_no_update
BEFORE UPDATE ON integrity_journal
BEGIN
  SELECT RAISE(ABORT, 'integrity_journal is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_integrity_journal_no_delete
BEFORE DELETE ON integrity_journal
BEGIN
  SELECT RAISE(ABORT, 'integrity_journal is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_grievance_no_delete
BEFORE DELETE ON grievance
BEGIN
  SELECT RAISE(ABORT, 'grievance is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_forgiveness_capacity_no_delete
BEFORE DELETE ON forgiveness_capacity
BEGIN
  SELECT RAISE(ABORT, 'forgiveness_capacity is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_personality_no_delete
BEFORE DELETE ON personality
BEGIN
  SELECT RAISE(ABORT, 'personality is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_personality_change_no_update
BEFORE UPDATE ON personality_change
BEGIN
  SELECT RAISE(ABORT, 'personality_change is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_personality_change_no_delete
BEFORE DELETE ON personality_change
BEGIN
  SELECT RAISE(ABORT, 'personality_change is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_episode_no_update
BEFORE UPDATE ON episode
BEGIN
  SELECT RAISE(ABORT, 'episode is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_episode_no_delete
BEFORE DELETE ON episode
BEGIN
  SELECT RAISE(ABORT, 'episode is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_self_state_no_delete
BEFORE DELETE ON self_state
BEGIN
  SELECT RAISE(ABORT, 'self_state is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_self_event_no_update
BEFORE UPDATE ON self_event
BEGIN
  SELECT RAISE(ABORT, 'self_event is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_self_event_no_delete
BEFORE DELETE ON self_event
BEGIN
  SELECT RAISE(ABORT, 'self_event is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_reflection_policy_no_delete
BEFORE DELETE ON reflection_policy
BEGIN
  SELECT RAISE(ABORT, 'reflection_policy is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_family_status_state_no_delete
BEFORE DELETE ON family_status_state
BEGIN
  SELECT RAISE(ABORT, 'family_status_state is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_knowledge_record_no_delete
BEFORE DELETE ON knowledge_record
BEGIN
  SELECT RAISE(ABORT, 'knowledge_record is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_peer_config_no_delete
BEFORE DELETE ON peer_config
BEGIN
  SELECT RAISE(ABORT, 'peer_config is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_biography_no_delete
BEFORE DELETE ON biography
BEGIN
  SELECT RAISE(ABORT, 'biography is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_biography_event_no_update
BEFORE UPDATE ON biography_event
BEGIN
  SELECT RAISE(ABORT, 'biography_event is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_biography_event_no_delete
BEFORE DELETE ON biography_event
BEGIN
  SELECT RAISE(ABORT, 'biography_event is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_context_topic_no_delete
BEFORE DELETE ON context_topic
BEGIN
  SELECT RAISE(ABORT, 'context_topic is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_context_instance_no_update
BEFORE UPDATE ON context_instance
BEGIN
  SELECT RAISE(ABORT, 'context_instance is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_context_instance_no_delete
BEFORE DELETE ON context_instance
BEGIN
  SELECT RAISE(ABORT, 'context_instance is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_decision_journal_entry_no_delete
BEFORE DELETE ON decision_journal_entry
BEGIN
  SELECT RAISE(ABORT, 'decision_journal_entry is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_experience_no_delete
BEFORE DELETE ON experience
BEGIN
  SELECT RAISE(ABORT, 'experience is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_secret_archive_question_no_delete
BEFORE DELETE ON secret_archive_question
BEGIN
  SELECT RAISE(ABORT, 'secret_archive_question is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_inner_state_no_delete
BEFORE DELETE ON inner_state
BEGIN
  SELECT RAISE(ABORT, 'inner_state is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_inner_state_event_no_update
BEFORE UPDATE ON inner_state_event
BEGIN
  SELECT RAISE(ABORT, 'inner_state_event is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_inner_state_event_no_delete
BEFORE DELETE ON inner_state_event
BEGIN
  SELECT RAISE(ABORT, 'inner_state_event is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_commitment_no_update
BEFORE UPDATE ON commitment
BEGIN
  SELECT RAISE(ABORT, 'commitment is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_commitment_no_delete
BEFORE DELETE ON commitment
BEGIN
  SELECT RAISE(ABORT, 'commitment is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_commitment_event_no_update
BEFORE UPDATE ON commitment_event
BEGIN
  SELECT RAISE(ABORT, 'commitment_event is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_commitment_event_no_delete
BEFORE DELETE ON commitment_event
BEGIN
  SELECT RAISE(ABORT, 'commitment_event is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_causal_event_no_update
BEFORE UPDATE ON causal_event
BEGIN
  SELECT RAISE(ABORT, 'causal_event is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_causal_event_no_delete
BEFORE DELETE ON causal_event
BEGIN
  SELECT RAISE(ABORT, 'causal_event is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_interaction_turn_no_update
BEFORE UPDATE ON interaction_turn
BEGIN
  SELECT RAISE(ABORT, 'interaction_turn is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_interaction_turn_no_delete
BEFORE DELETE ON interaction_turn
BEGIN
  SELECT RAISE(ABORT, 'interaction_turn is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_personal_fact_no_update
BEFORE UPDATE ON personal_fact
BEGIN
  SELECT RAISE(ABORT, 'personal_fact is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_personal_fact_no_delete
BEFORE DELETE ON personal_fact
BEGIN
  SELECT RAISE(ABORT, 'personal_fact is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_personal_fact_event_no_update
BEFORE UPDATE ON personal_fact_event
BEGIN
  SELECT RAISE(ABORT, 'personal_fact_event is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_personal_fact_event_no_delete
BEFORE DELETE ON personal_fact_event
BEGIN
  SELECT RAISE(ABORT, 'personal_fact_event is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_disagreement_no_update
BEFORE UPDATE ON disagreement
BEGIN
  SELECT RAISE(ABORT, 'disagreement is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_disagreement_no_delete
BEFORE DELETE ON disagreement
BEGIN
  SELECT RAISE(ABORT, 'disagreement is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_trait_graph_no_delete
BEFORE DELETE ON trait_graph
BEGIN
  SELECT RAISE(ABORT, 'trait_graph is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_trait_change_no_update
BEFORE UPDATE ON trait_change
BEGIN
  SELECT RAISE(ABORT, 'trait_change is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_trait_change_no_delete
BEFORE DELETE ON trait_change
BEGIN
  SELECT RAISE(ABORT, 'trait_change is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_trait_edge_change_no_update
BEFORE UPDATE ON trait_edge_change
BEGIN
  SELECT RAISE(ABORT, 'trait_edge_change is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_trait_edge_change_no_delete
BEFORE DELETE ON trait_edge_change
BEGIN
  SELECT RAISE(ABORT, 'trait_edge_change is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_internal_question_no_update
BEFORE UPDATE ON internal_question
BEGIN
  SELECT RAISE(ABORT, 'internal_question is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_internal_question_no_delete
BEFORE DELETE ON internal_question
BEGIN
  SELECT RAISE(ABORT, 'internal_question is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_internal_question_answer_no_update
BEFORE UPDATE ON internal_question_answer
BEGIN
  SELECT RAISE(ABORT, 'internal_question_answer is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_internal_question_answer_no_delete
BEFORE DELETE ON internal_question_answer
BEGIN
  SELECT RAISE(ABORT, 'internal_question_answer is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_self_reflection_profile_no_delete
BEFORE DELETE ON self_reflection_profile
BEGIN
  SELECT RAISE(ABORT, 'self_reflection_profile is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_social_knowledge_no_delete
BEFORE DELETE ON social_knowledge
BEGIN
  SELECT RAISE(ABORT, 'social_knowledge is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_knowledge_query_archive_no_delete
BEFORE DELETE ON knowledge_query_archive
BEGIN
  SELECT RAISE(ABORT, 'knowledge_query_archive is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_instance_identity_no_update
BEFORE UPDATE ON instance_identity
BEGIN
  SELECT RAISE(ABORT, 'instance_identity is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_instance_identity_no_delete
BEFORE DELETE ON instance_identity
BEGIN
  SELECT RAISE(ABORT, 'instance_identity is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_storage_protection_event_no_update
BEFORE UPDATE ON storage_protection_event
BEGIN
  SELECT RAISE(ABORT, 'storage_protection_event is immutable (YANDI SQL BASTION): UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_storage_protection_event_no_delete
BEFORE DELETE ON storage_protection_event
BEGIN
  SELECT RAISE(ABORT, 'storage_protection_event is immutable (YANDI SQL BASTION): DELETE forbidden');
END;
