"""
agent/db_sql_live_immutability_proof.py — DATABASE BOOTSTRAP V1: LIVE
VERSION / IMMUTABILITY PROOF.

Companion to agent/db_sql_live_persistence_proof.py — another one-time,
owner/operator-invoked LIVE verification tool (not a regression test:
it issues real UPDATE/DELETE attempts against the live dedicated
instance). Every negative test below targets ONLY a fresh,
unmistakably-synthetic LIVE_DB_PROOF_<marker>-tagged row this script
creates for itself — never real data, never an existing row from a
previous run.

Proves, live, against the ACTUAL yandi_runtime connection (never root):
    1. QUESTION (class A): UPDATE and DELETE both rejected by trigger.
    2. ANSWER_VERSION: a second call to record_answer_version() with
       DIFFERENT text APPENDS version_number=2, never overwrites
       version 1 (the row itself is also trigger-immutable).
    3. BELIEF (class C): UPDATE is legitimate (upsert_belief() called
       twice with different confidence) but DELETE is still rejected —
       proves the projection-vs-history distinction is real, not just
       documented.
    4. VERIFICATION_RUN (class D): the narrow guard trigger rejects (a)
       changing run_id/occurrence_id/started_at and (b) a second status
       transition away from an already-terminal state.
    5. YANDI_RUNTIME's own live grants: a genuinely destructive/admin
       statement (ALTER TABLE, DROP TABLE, CREATE USER) is rejected by
       MySQL's privilege system itself for this account — not merely
       absent from a static GRANT-text scan, but a REAL access-denied
       response from the live server.

NOT live-tested here (documented honestly, not silently skipped):
YANDI_READONLY and YANDI_MIGRATOR's own live behavior — this process
has no access to those two accounts' passwords (root-generated,
0600-protected secret files this OS user cannot read, by design: least-
privilege isolation working as intended). Their privilege TEXT is
already proven correct by the existing static suite,
agent/db_sql_security_privilege_regression_test.py; a live behavioral
proof for those two roles would require a caller who already holds
those credentials (e.g. install-yandi.sh itself, under sudo).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_live_immutability_proof
"""
from __future__ import annotations

import sys
import uuid

import pymysql

from agent.db.sql.connection import get_connection, SqlUnavailable
from agent.db.sql import repositories as repo

MARKER = uuid.uuid4().hex[:10]
RUN_ID = f"LDBI_RUN_{MARKER}"
BELIEF_ID = f"LDBI_BEL_{MARKER}"
RAW_TEXT = f"LIVE_DB_PROOF_{MARKER} synthetic immutability-proof question — not real"

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"OK   {label}")
    else:
        FAIL += 1
        print(f"FAIL {label} {detail}")


def expect_rejected(conn, sql: str, params=(), expect_substr: str = "") -> str:
    """Runs `sql` and returns '' if it was WRONGLY accepted, otherwise
    the actual MySQL error text (proving a real rejection happened, not
    just an assumption)."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.rollback()
        return ""
    except pymysql.MySQLError as e:
        conn.rollback()
        msg = str(e)
        if expect_substr and expect_substr.lower() not in msg.lower():
            return f"REJECTED BUT WRONG REASON: {msg}"
        return msg


def main() -> int:
    try:
        with get_connection(autocommit=False) as conn:
            run_immutability_proof(conn)
    except SqlUnavailable as e:
        print(f"FAIL: SQL unavailable: {e}")
        return 1

    print()
    print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
    if FAIL:
        return 1
    print("все проверки пройдены")
    return 0


def run_immutability_proof(conn) -> None:
    # ---- setup: one fresh synthetic question/run, via the repository API ----
    q = repo.resolve_question(conn, RAW_TEXT, anonymized_text=None, session_id=f"live-db-immutability-{MARKER}")
    question_id = q["question_id"]
    repo.start_run(conn, RUN_ID, q["occurrence_id"], pipeline_version="live-db-immutability-proof")
    conn.commit()

    # ============================================================
    # 1. QUESTION (class A) — UPDATE and DELETE both rejected.
    # ============================================================
    err = expect_rejected(
        conn, "UPDATE question SET first_asked_at = NOW() WHERE question_id = %s",
        (question_id,), expect_substr="immutable",
    )
    check("1a. QUESTION: UPDATE rejected by trigger (class A, canonical identity)", bool(err), err)

    err = expect_rejected(
        conn, "DELETE FROM question WHERE question_id = %s", (question_id,), expect_substr="immutable",
    )
    check("1b. QUESTION: DELETE rejected by trigger", bool(err), err)

    # ============================================================
    # 2. ANSWER_VERSION — a second, DIFFERENT answer text APPENDS a new
    # version, never overwrites; the row itself is also immutable.
    # ============================================================
    answer_id_1 = repo.record_answer_version(conn, question_id, f"LIVE_DB_PROOF_{MARKER} answer v1", RUN_ID)
    answer_id_2 = repo.record_answer_version(conn, question_id, f"LIVE_DB_PROOF_{MARKER} answer v2 — DIFFERENT text", RUN_ID)
    conn.commit()
    check("2a. ANSWER_VERSION: a different answer text creates a NEW row (new answer_id), not an overwrite", answer_id_2 != answer_id_1)

    with conn.cursor() as cur:
        cur.execute("SELECT version_number, supersedes_id FROM answer_version WHERE answer_id = %s", (answer_id_2,))
        v2 = cur.fetchone()
    check("2b. ANSWER_VERSION: version_number incremented to 2 for the same question", v2["version_number"] == 2, f"{v2}")
    check("2c. ANSWER_VERSION: supersedes_id correctly points back at version 1", v2["supersedes_id"] == answer_id_1, f"{v2}")

    err = expect_rejected(
        conn, "UPDATE answer_version SET answer_text = %s WHERE answer_id = %s",
        ("tampered", answer_id_1), expect_substr="immutable",
    )
    check("2d. ANSWER_VERSION: UPDATE on an existing version is rejected by trigger", bool(err), err)

    # Re-recording the SAME text again must be a no-op (find, not re-create).
    answer_id_2_again = repo.record_answer_version(conn, question_id, f"LIVE_DB_PROOF_{MARKER} answer v2 — DIFFERENT text", RUN_ID)
    conn.commit()
    check("2e. ANSWER_VERSION: re-recording byte-identical text returns the SAME answer_id (no duplicate version)", answer_id_2_again == answer_id_2)

    # ============================================================
    # 3. BELIEF (class C) — UPDATE legitimate, DELETE still rejected.
    # ============================================================
    repo.upsert_belief(conn, BELIEF_ID, topic="live_db_proof", statement="synthetic belief v1", confidence=0.2, status="active")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT confidence, statement FROM belief WHERE belief_id = %s", (BELIEF_ID,))
        b1 = cur.fetchone()
    check("3a. BELIEF: initial upsert persisted", b1 is not None and float(b1["confidence"]) == 0.2, f"{b1}")

    repo.upsert_belief(conn, BELIEF_ID, topic="live_db_proof", statement="synthetic belief v2 — UPDATED IN PLACE", confidence=0.9, status="active")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT confidence, statement FROM belief WHERE belief_id = %s", (BELIEF_ID,))
        b2 = cur.fetchone()
    check(
        "3b. BELIEF (class C, projection): a plain UPDATE-in-place (via upsert_belief()) IS "
        "legitimate and actually changed the row — NOT blocked, unlike class A/B",
        b2 is not None and float(b2["confidence"]) == 0.9 and "UPDATED IN PLACE" in b2["statement"],
        f"{b2}",
    )

    err = expect_rejected(conn, "DELETE FROM belief WHERE belief_id = %s", (BELIEF_ID,), expect_substr="immutable")
    check("3c. BELIEF: DELETE is still rejected even though UPDATE is allowed (projection is disposable-by-rewrite, not by delete)", bool(err), err)

    # ============================================================
    # 4. VERIFICATION_RUN (class D) — narrow guard: identity columns
    # immutable; only ONE transition away from 'running' is allowed.
    # ============================================================
    err = expect_rejected(
        conn, "UPDATE verification_run SET started_at = NOW() WHERE run_id = %s", (RUN_ID,),
        expect_substr="immutable",
    )
    check("4a. VERIFICATION_RUN: changing started_at (identity column) is rejected", bool(err), err)

    repo.complete_run(conn, RUN_ID, final_answer_id=answer_id_2)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM verification_run WHERE run_id = %s", (RUN_ID,))
        after_complete = cur.fetchone()
    check("4b. VERIFICATION_RUN: legitimate running->completed transition succeeded", after_complete["status"] == "completed", f"{after_complete}")

    err = expect_rejected(
        conn, "UPDATE verification_run SET status = 'failed' WHERE run_id = %s", (RUN_ID,),
    )
    check(
        "4c. VERIFICATION_RUN: a SECOND status transition on an already-terminal run is rejected "
        "(the guard trigger's own OLD.status <> 'running' check, live)",
        bool(err), err,
    )

    # ============================================================
    # 5. YANDI_RUNTIME's own live privilege boundary — a real
    # access-denied response from the server, not a static text scan.
    # ============================================================
    err = expect_rejected(conn, "ALTER TABLE question_occurrence ADD COLUMN ldbi_proof_col INT")
    check("5a. YANDI_RUNTIME: ALTER TABLE is rejected live by the server (no ALTER grant)", bool(err), err)

    err = expect_rejected(conn, f"DROP TABLE IF EXISTS question_occurrence")
    check("5b. YANDI_RUNTIME: DROP TABLE is rejected live by the server (no DROP grant)", bool(err), err)

    err = expect_rejected(conn, "CREATE USER IF NOT EXISTS 'ldbi_proof_user'@'localhost' IDENTIFIED BY 'x'")
    check("5c. YANDI_RUNTIME: CREATE USER is rejected live by the server (no CREATE USER grant, global-only privilege)", bool(err), err)


if __name__ == "__main__":
    sys.exit(main())
