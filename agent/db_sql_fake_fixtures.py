"""
agent/db_sql_fake_fixtures.py — "ТОЧКА НОЛЬ" v13 shared test fixture.

NOT a regression test itself (no PASS/FAIL, nothing runs on import) —
a small fake claim_occurrence/source_resource/source_observation/
evidence_relation/trace_record backing store, standing in for the real
bastion-protected SQL tables, shared by every regression test that
exercises agent.verification_memory.py's LOAD path and/or agent.
orch_tracer.DecisionTracer.save_trace() now that both are SQL-backed
(registry/index.db sqlite locator + registry/dataset/orch_traces/
*.jsonl are retired, not migrated — see agent/verification_memory.py's
own module docstring).

Use _fresh_fake() to get an isolated fake connection wired into
agent.db.sql.shadow_write / agent.verification_memory / agent.orch_tracer
(orch_tracer resolves get_connection lazily at call time from agent.db.
sql.connection, so patching that module's attribute is what it actually
picks up), and _record(...) to mirror the real production sequence
(agent/orchestrator/claims/status.py's record_claims_and_evidence(),
called separately from — and before — agent.orch_tracer.DecisionTracer.
save_trace()).
"""
from __future__ import annotations

import contextlib
from datetime import datetime

import agent.db.sql.shadow_write as sw
import agent.db.sql.connection as sqlconn
import agent.verification_memory as vm


class _FakeCursor:
    lastrowid = 1

    def __init__(self, conn):
        self.conn = conn
        self._result = None
        self._results = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        upper = " ".join(sql.split()).upper()
        self._result = None
        self._results = None
        c = self.conn

        if upper.startswith("INSERT INTO CLAIM_OCCURRENCE"):
            (claim_id, run_id, claim_text, content_hash, claim_type, claim_confidence,
             verification_status, family_id, query_context, support_count, contradiction_count) = params
            c.claims[claim_id] = dict(
                claim_id=claim_id, run_id=run_id, claim_text=claim_text, content_hash=content_hash,
                claim_type=claim_type, claim_confidence=claim_confidence,
                verification_status=verification_status, family_id=family_id,
                query_context=query_context, support_count=support_count,
                contradiction_count=contradiction_count,
            )
            c.runs.setdefault(run_id, {"run_id": run_id, "started_at": c.next_started_at})
        elif upper.startswith("SELECT RESOURCE_ID FROM SOURCE_RESOURCE WHERE URI_HASH=%S"):
            (uri_hash,) = params
            rid = c.resource_by_hash.get(uri_hash)
            self._result = {"resource_id": rid} if rid else None
        elif upper.startswith("INSERT INTO SOURCE_RESOURCE"):
            resource_type, canonical_uri, uri_hash = params[0], params[1], params[2]
            c.next_resource_id += 1
            rid = c.next_resource_id
            c.resources[rid] = dict(resource_id=rid, resource_type=resource_type,
                                     canonical_uri=canonical_uri, node_id=None, validator_id=None, model_id=None)
            c.resource_by_hash[uri_hash] = rid
            self.lastrowid = rid
        elif upper.startswith("SELECT * FROM SOURCE_RESOURCE WHERE RESOURCE_ID=%S"):
            (rid,) = params
            self._result = dict(c.resources[rid]) if rid in c.resources else None
        elif upper.startswith("SELECT OBSERVATION_ID FROM SOURCE_OBSERVATION WHERE RESOURCE_ID=%S AND RUN_ID=%S"):
            resource_id, origin_run_id = params
            match = next(
                (o for o in c.observations.values() if o["resource_id"] == resource_id and o["run_id"] == origin_run_id),
                None,
            )
            self._result = {"observation_id": match["observation_id"]} if match else None
        elif upper.startswith("INSERT INTO SOURCE_OBSERVATION"):
            c.next_observation_id += 1
            oid = c.next_observation_id
            cols = [
                "resource_id", "run_id", "observation_route", "origin_observation_id", "observed_at",
                "source_class", "quality_score", "content_excerpt", "rejection_reason", "evidence_id",
                "source_title", "retrieval_query", "retrieval_rank", "relevance_to_query", "authority",
                "traceability", "primaryness", "is_meta_pipeline_output", "is_subject_matter_evidence",
                "source_cluster_id", "origin_source_cluster_id", "retrieval_claim_id", "route_side",
                "subject_entities", "fact_candidates", "supports_query_aspect",
            ]
            row = dict(zip(cols, params))
            row["observation_id"] = oid
            c.observations[oid] = row
            self.lastrowid = oid
        elif "FROM SOURCE_OBSERVATION WHERE OBSERVATION_ID=%S" in upper:
            (oid,) = params
            self._result = dict(c.observations[oid]) if oid in c.observations else None
        elif upper.startswith("INSERT INTO EVIDENCE_RELATION"):
            claim_id, observation_id, relation, directness, evidence_eligible, evidence_role, counted_via, created_at = params
            c.next_relation_id += 1
            c.relations[c.next_relation_id] = dict(
                relation_id=c.next_relation_id, claim_id=claim_id, observation_id=observation_id,
                relation=relation, directness=directness, evidence_eligible=evidence_eligible,
                evidence_role=evidence_role, counted_via=counted_via, created_at=created_at,
            )
        elif upper.startswith("SELECT ER.RELATION, ER.DIRECTNESS, ER.EVIDENCE_ELIGIBLE, ER.EVIDENCE_ROLE"):
            (claim_id,) = params
            rows = []
            for rel in c.relations.values():
                if rel["claim_id"] != claim_id:
                    continue
                obs = dict(c.observations[rel["observation_id"]])
                obs.update({
                    "relation": rel["relation"], "directness": rel["directness"],
                    "evidence_eligible": rel["evidence_eligible"], "evidence_role": rel["evidence_role"],
                })
                rows.append(obs)
            self._results = rows
        elif upper.startswith("SELECT CO.*, VR.STARTED_AT AS OCCURRENCE_OBSERVED_AT FROM CLAIM_OCCURRENCE CO"):
            if "CO.CONTENT_HASH=%S" in upper:
                if "AND CO.RUN_ID !=" in upper:
                    content_hash, exclude_run_id = params[:2]
                    limit = params[2] if len(params) > 2 else None
                else:
                    content_hash = params[0]
                    exclude_run_id = None
                    limit = params[1] if len(params) > 1 else None
                matches = [
                    dict(co, occurrence_observed_at=c.runs.get(co["run_id"], {}).get("started_at"))
                    for co in c.claims.values()
                    if co["content_hash"] == content_hash and co["run_id"] != exclude_run_id
                ]
            else:  # family_id lookup
                (family_id,) = params
                matches = [
                    dict(co, occurrence_observed_at=c.runs.get(co["run_id"], {}).get("started_at"))
                    for co in c.claims.values() if co["family_id"] == family_id
                ]
                limit = None
            matches.sort(key=lambda r: r["occurrence_observed_at"] or 0, reverse=True)
            self._results = matches[:limit] if limit else matches
        elif upper.startswith("SELECT QO.RAW_TEXT FROM VERIFICATION_RUN"):
            (run_id,) = params
            q = c.run_query.get(run_id)
            self._result = {"raw_text": q} if q else None
        elif upper.startswith("INSERT INTO TRACE_RECORD"):
            run_id = params[0]
            c.trace_records[run_id] = params

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results or []


class _FakeConnection:
    def __init__(self):
        self.claims = {}
        self.resources = {}
        self.resource_by_hash = {}
        self.observations = {}
        self.relations = {}
        self.trace_records = {}
        self.runs = {}
        self.run_query = {}
        self.next_resource_id = 0
        self.next_observation_id = 0
        self.next_relation_id = 0
        self.next_started_at = datetime(2026, 1, 1)

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass


def fresh_fake():
    """A brand-new fake claim_occurrence/source_resource/source_observation/
    evidence_relation/trace_record store, wired into BOTH agent.db.sql.
    shadow_write (record_claims_and_evidence) and agent.verification_memory/
    agent.orch_tracer (the LOAD path + save_trace's trace_record write)."""
    conn = _FakeConnection()

    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    sw.get_connection = _fake_get_connection
    vm.get_connection = _fake_get_connection
    # agent.orch_tracer imports get_connection lazily (inside save_trace),
    # resolving agent.db.sql.connection.get_connection at CALL time — so
    # patching the module attribute here is what it actually picks up.
    sqlconn.get_connection = _fake_get_connection
    return conn


def record(conn, run_id, claims_data, evidence_data, query_text=None, started_at=None):
    """Mirrors the real production sequence: agent/orchestrator/claims/
    status.py's record_claims_and_evidence() call, done separately from
    (and before) agent.orch_tracer.DecisionTracer.save_trace()."""
    if started_at is not None:
        conn.next_started_at = started_at
    if query_text is not None:
        conn.run_query[run_id] = query_text
    sw.record_claims_and_evidence(run_id=run_id, claims_data=claims_data, evidence_data=evidence_data)
