"""
agent/db_sql_live_persistence_proof.py — DATABASE BOOTSTRAP V1: LIVE SQL
PERSISTENCE PROOF.

This is a one-time, owner/operator-invoked LIVE verification tool, NOT
a regression test — unlike everything else in this package's
*_test.py/*_regression_test.py files (which run against fakes/mocks),
this script writes REAL rows into whatever database YANDI_SQL_* points
it at. It is meant to be run against the dedicated yandi-db instance,
using the actual production connection path (agent.db.sql.connection),
as the actual yandi_runtime role — never as root/bootstrap.

Every row this script writes carries an unmistakable, unique
LIVE_DB_PROOF_<marker> tag (a fresh random marker each run) so it can
never be confused with real epistemic content, and can be identified
later if a cleanup is ever wanted. Nothing here makes or evaluates any
epistemic claim: PERSISTED != TRUE. Every enum/status value used is
either structurally required or an explicit "not_evaluated"/
"synthetic_proof" placeholder.

Exercises ONLY the EXISTING repository API (agent/db/sql/repositories.py)
for every write — never a manual INSERT that bypasses it. The one
exception is read-only diagnostic SELECTs, and there are none needed
here: every readback also goes through repositories.py's own read
functions, proving those work end-to-end too, not just the writes.

Chain proven, in order:
    resolve_question() -> QUESTION + QUESTION_OCCURRENCE
    start_run()         -> VERIFICATION_RUN (status=running)
    record_answer_version() -> ANSWER_VERSION
    record_answer_assessment() -> ANSWER_ASSESSMENT
    get_or_create_claim_family() -> CLAIM_FAMILY
    record_claim_occurrence()    -> CLAIM_OCCURRENCE
    link_family_member()         -> FAMILY_MEMBER
    get_or_create_resource()     -> SOURCE_RESOURCE
    record_source_observation()  -> SOURCE_OBSERVATION
    record_evidence_relation()   -> EVIDENCE_RELATION
    complete_run()       -> VERIFICATION_RUN (status=completed)

then reads every one of those back through repositories.py's read API
(get_current_answer, get_answer_history, explain_answer,
get_verification_runs, get_sources_for_run, get_claim_history,
get_route_history, get_last_checked) and asserts every linkage survived
exactly as written — including that source_resource and
source_observation are genuinely distinct rows (module docstring point
2 of schema.py: "a resource is not the same thing as an observation of
it").

Requires the environment already configured for the intended target
instance, e.g. for the LIVE dedicated yandi-db instance:
    YANDI_SQL_USER=yandi_runtime
    YANDI_SQL_AUTH_MODE=auth_socket
    YANDI_SQL_SOCKET=/run/yandi/mysql/mysql.sock
    YANDI_SQL_DATABASE=yandi_epistemic

Run: /home/iam/venv/bin/python3 -m agent.db_sql_live_persistence_proof
"""
from __future__ import annotations

import sys
import uuid

from agent.db.sql.connection import get_connection, SqlUnavailable
from agent.db.sql import repositories as repo

MARKER = uuid.uuid4().hex[:10]
RUN_ID = f"LDBP_RUN_{MARKER}"
CLAIM_ID = f"cl_LDBP_{MARKER}"
FAMILY_ID = f"fam_LDBP_{MARKER}"
RAW_TEXT = f"LIVE_DB_PROOF_{MARKER} synthetic verification question — not a real query"
ANSWER_TEXT = f"LIVE_DB_PROOF_{MARKER} synthetic answer text — not real content"
CANONICAL_URI = f"https://live-db-proof.invalid/{MARKER}"


def check(label: str, cond: bool) -> None:
    print(f"{'OK  ' if cond else 'FAIL'} {label}")
    if not cond:
        raise SystemExit(1)


def write_chain(conn) -> dict:
    q = repo.resolve_question(conn, RAW_TEXT, anonymized_text=None, session_id=f"live-db-proof-{MARKER}")
    question_id, occurrence_id = q["question_id"], q["occurrence_id"]

    repo.start_run(
        conn, RUN_ID, occurrence_id, web_enabled=False, validation_enabled=False,
        pipeline_version="live-db-proof", schema_version=1,
    )

    answer_id = repo.record_answer_version(conn, question_id, ANSWER_TEXT, RUN_ID)

    assessment_id = repo.record_answer_assessment(
        conn, answer_id, RUN_ID, canonical_trust="not_evaluated",
        synthesizer_strand="live_db_proof", trust_gate_strand="live_db_proof",
        diverged=False, reason="synthetic live persistence proof row — not a real assessment",
    )

    repo.get_or_create_claim_family(
        conn, FAMILY_ID, domain="live_db_proof",
        canonical_text=f"LIVE_DB_PROOF_{MARKER} synthetic claim family — not a real claim",
    )
    repo.record_claim_occurrence(
        conn, CLAIM_ID, RUN_ID, claim_text=f"LIVE_DB_PROOF_{MARKER} synthetic claim text",
        content_hash=None, claim_type="synthetic_proof", claim_confidence=0.5,
        verification_status="not_evaluated", family_id=FAMILY_ID, query_context=None,
    )
    repo.link_family_member(conn, FAMILY_ID, CLAIM_ID)

    resource_id = repo.get_or_create_resource(conn, resource_type="internet", canonical_uri=CANONICAL_URI)
    observation_id = repo.record_source_observation(
        conn, resource_id, RUN_ID, observation_route="internet",
        source_class="live_db_proof", quality_score=None,
        content_excerpt=f"LIVE_DB_PROOF_{MARKER} synthetic excerpt", rejection_reason=None,
    )
    evidence_relation_id = repo.record_evidence_relation(
        conn, CLAIM_ID, observation_id, relation="uncertain", directness=0.0,
        evidence_eligible=False, evidence_role="synthetic_proof", counted_via="directness",
    )

    repo.complete_run(conn, RUN_ID, final_answer_id=answer_id)

    return dict(
        question_id=question_id, occurrence_id=occurrence_id, answer_id=answer_id,
        assessment_id=assessment_id, resource_id=resource_id, observation_id=observation_id,
        evidence_relation_id=evidence_relation_id,
    )


def verify_readback(conn, ids: dict) -> None:
    current = repo.get_current_answer(conn, ids["question_id"])
    check("get_current_answer(): returns our synthetic answer text", bool(current) and current["answer_text"] == ANSWER_TEXT)
    check("get_current_answer(): assessment joined (canonical_trust)", current.get("canonical_trust") == "not_evaluated")

    history = repo.get_answer_history(conn, ids["question_id"])
    check(
        "get_answer_history(): exactly one version for this fresh synthetic question",
        len(history) == 1 and history[0]["answer_id"] == ids["answer_id"],
    )
    check(
        "get_answer_history(): assessment nested under the version, run_id preserved",
        len(history[0]["assessments"]) == 1 and history[0]["assessments"][0]["run_id"] == RUN_ID,
    )

    explained = repo.explain_answer(conn, ids["answer_id"])
    check("explain_answer(): run linked to our answer", explained["run"]["run_id"] == RUN_ID)
    check("explain_answer(): run status is completed (not left running)", explained["run"]["status"] == "completed")
    check(
        "explain_answer(): run.final_answer_id points back to our answer_version",
        explained["run"]["final_answer_id"] == ids["answer_id"],
    )
    check(
        "explain_answer(): exactly our claim is attached to the run",
        len(explained["claims"]) == 1 and explained["claims"][0]["claim_id"] == CLAIM_ID,
    )
    claim = explained["claims"][0]
    check("explain_answer(): claim.family_id preserved (family/member relation survived)", claim["family_id"] == FAMILY_ID)
    check("explain_answer(): evidence walked through claim -> observation -> resource", len(claim["evidence"]) == 1)
    ev = claim["evidence"][0]
    check("explain_answer(): evidence.observation_id matches our observation", ev["observation_id"] == ids["observation_id"])
    check("explain_answer(): evidence -> source_resource.canonical_uri survived the roundtrip", ev["canonical_uri"] == CANONICAL_URI)
    check(
        "explain_answer(): the joined evidence row carries BOTH observation-level fields "
        "(observation_route) AND resource-level fields (canonical_uri, resource_type) — "
        "source_resource and source_observation are genuinely separate tables joined "
        "together, not one row wearing two names (schema.py's own resource-vs-route "
        "correction, proven live). NOTE: resource_id/observation_id are independent "
        "AUTO_INCREMENT sequences in separate tables — on a virgin database both "
        "legitimately start at 1, so equal numeric values here do NOT indicate the same "
        "row; table identity, not numeric coincidence, is what's being proven.",
        ev["resource_id"] == ids["resource_id"] and ev.get("observation_route") == "internet"
        and ev.get("resource_type") == "internet",
    )

    runs = repo.get_verification_runs(conn, ids["question_id"])
    check("get_verification_runs(): our run is listed for this question", any(r["run_id"] == RUN_ID for r in runs))

    sources = repo.get_sources_for_run(conn, RUN_ID)
    check(
        "get_sources_for_run(): our observation is listed, joined to the correct resource",
        len(sources) == 1 and sources[0]["observation_id"] == ids["observation_id"]
        and sources[0]["resource_id"] == ids["resource_id"],
    )

    claim_history = repo.get_claim_history(conn, FAMILY_ID)
    check("get_claim_history(): our claim is listed under its family", any(c["claim_id"] == CLAIM_ID for c in claim_history))

    route_history = repo.get_route_history(conn, ids["resource_id"])
    check(
        "get_route_history(): our observation is listed for its resource",
        any(o["observation_id"] == ids["observation_id"] for o in route_history),
    )

    last_checked = repo.get_last_checked(conn, ids["question_id"])
    check("get_last_checked(): a non-null timestamp is returned for this question", last_checked is not None)


def main() -> int:
    print(f"=== LIVE DB PERSISTENCE PROOF — marker LIVE_DB_PROOF_{MARKER} ===")
    try:
        with get_connection(autocommit=False) as conn:
            ids = write_chain(conn)
            conn.commit()
    except SqlUnavailable as e:
        print(f"FAIL: SQL unavailable during write phase: {e}")
        return 1
    print(
        f"WRITE COMMITTED: question_id={ids['question_id']} occurrence_id={ids['occurrence_id']} "
        f"run_id={RUN_ID} answer_id={ids['answer_id']} assessment_id={ids['assessment_id']} "
        f"claim_id={CLAIM_ID} family_id={FAMILY_ID} resource_id={ids['resource_id']} "
        f"observation_id={ids['observation_id']} evidence_relation_id={ids['evidence_relation_id']}"
    )

    try:
        with get_connection(autocommit=True) as conn:
            verify_readback(conn, ids)
    except SqlUnavailable as e:
        print(f"FAIL: SQL unavailable during readback phase: {e}")
        return 1

    print()
    print(f"ALL ROUNDTRIP CHECKS PASSED — marker LIVE_DB_PROOF_{MARKER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
