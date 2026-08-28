"""
agent/db_sql_repositories_regression_test.py — Этап 5 (SQL persistence
migration) regression: agent/db/sql/repositories.py.

IMPORTANT SCOPE NOTE (mandate §27): no MySQL/Percona credentials exist
in this environment, so this suite CANNOT and does NOT claim to
validate live MySQL semantics (constraint enforcement, actual FK
behavior, real AUTO_INCREMENT, etc.) — that would require a real
server. What it DOES prove, against a minimal fake DB-API connection
that records every executed statement+params and returns programmed
results: SQL construction correctness (right table/columns/param
count), idempotency control flow (resolve_question/get_or_create_
resource/record_answer_version reuse existing rows instead of always
inserting), controlled-vocabulary validation, and read-query join/
result-assembly logic. See MIGRATION_STATUS.md for what remains
BLOCKED BY CREDENTIALS.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_repositories_regression_test
"""
from __future__ import annotations

from datetime import datetime

import agent.db.sql.repositories as repo

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._last_result = None
        self.lastrowid = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        # Very small "SQL engine": the test wires up conn.responses as a
        # list of (predicate(sql) -> bool, result) pairs, consumed FIFO
        # for SELECTs; INSERT/UPDATE just bump lastrowid/rowcount from
        # conn's own counters, driven explicitly by the test.
        for i, (predicate, result) in enumerate(self.conn.responses):
            if predicate(sql):
                self._last_result = result
                del self.conn.responses[i]
                break
        else:
            self._last_result = None

        if sql.strip().upper().startswith("INSERT"):
            self.conn.next_id += 1
            self.lastrowid = self.conn.next_id
        if sql.strip().upper().startswith("UPDATE"):
            self.rowcount = self.conn.next_update_rowcount

    def fetchone(self):
        if isinstance(self._last_result, list):
            return self._last_result[0] if self._last_result else None
        return self._last_result

    def fetchall(self):
        if isinstance(self._last_result, list):
            return self._last_result
        return [] if self._last_result is None else [self._last_result]


class FakeConnection:
    def __init__(self):
        self.executed = []
        self.responses = []  # list of (predicate, result)
        self.next_id = 100
        self.next_update_rowcount = 1

    def cursor(self):
        return FakeCursor(self)

    def when(self, predicate, result):
        self.responses.append((predicate, result))


def _sql_contains(conn, *fragments) -> bool:
    for sql, _params in conn.executed:
        if all(f in sql for f in fragments):
            return True
    return False


# ============================================================
# resolve_question: idempotent by canonical_hash; occurrence ALWAYS new.
# ============================================================

conn = FakeConnection()
conn.when(lambda s: "SELECT question_id FROM question" in s, None)  # no existing question
result1 = repo.resolve_question(conn, "Сколько спутников у Юпитера?", None, session_id="s1")
check(
    "resolve_question: creates a new QUESTION when no hash match exists",
    _sql_contains(conn, "INSERT INTO question", "canonical_hash"),
    f"{conn.executed}",
)
check(
    "resolve_question: always inserts a QUESTION_OCCURRENCE",
    _sql_contains(conn, "INSERT INTO question_occurrence"),
    f"{conn.executed}",
)

conn2 = FakeConnection()
conn2.when(lambda s: "SELECT question_id FROM question" in s, {"question_id": 42})
result2 = repo.resolve_question(conn2, "Сколько спутников у Юпитера?", None)
check(
    "resolve_question: REUSES existing question_id when canonical_hash already matches "
    "(idempotent identity, mandate §30)",
    result2["question_id"] == 42
    and not _sql_contains(conn2, "INSERT INTO question ", "canonical_hash"),
    f"result={result2} executed={conn2.executed}",
)
check(
    "resolve_question: still inserts a NEW occurrence even on a reused question "
    "(raw text is never deduplicated, mandate §7)",
    _sql_contains(conn2, "INSERT INTO question_occurrence"),
    f"{conn2.executed}",
)

# ============================================================
# record_answer_version: exact-hash reuse vs new version + supersedes_id
# ============================================================

conn3 = FakeConnection()
conn3.when(
    lambda s: "SELECT answer_id, answer_hash, version_number" in s,
    {"answer_id": 7, "answer_hash": repo._text_hash("A"), "version_number": 1},
)
aid = repo.record_answer_version(conn3, question_id=1, answer_text="A", run_id="r1")
check(
    "record_answer_version: byte-identical text REUSES the existing answer_id, no new INSERT",
    aid == 7 and not _sql_contains(conn3, "INSERT INTO answer_version"),
    f"aid={aid} executed={conn3.executed}",
)

conn4 = FakeConnection()
conn4.when(
    lambda s: "SELECT answer_id, answer_hash, version_number" in s,
    {"answer_id": 7, "answer_hash": repo._text_hash("A"), "version_number": 1},
)
aid2 = repo.record_answer_version(conn4, question_id=1, answer_text="B (different text)", run_id="r2")
check(
    "record_answer_version: DIFFERENT text creates a new version with supersedes_id pointing "
    "at the prior answer_id",
    aid2 != 7 and any(
        "INSERT INTO answer_version" in sql and params and params[-2] == 7
        for sql, params in conn4.executed
    ),
    f"{conn4.executed}",
)

conn5 = FakeConnection()
conn5.when(lambda s: "SELECT answer_id, answer_hash, version_number" in s, None)
aid3 = repo.record_answer_version(conn5, question_id=1, answer_text="first ever", run_id="r1")
check(
    "record_answer_version: first version for a question starts at version_number=1, supersedes_id=NULL",
    any(
        "INSERT INTO answer_version" in sql and params[1] == 1 and params[-2] is None
        for sql, params in conn5.executed
    ),
    f"{conn5.executed}",
)

# ============================================================
# get_or_create_resource: uri_hash reuse (stable cross-run root identity)
# ============================================================

conn6 = FakeConnection()
conn6.when(lambda s: "SELECT resource_id FROM source_resource" in s, {"resource_id": 55})
rid = repo.get_or_create_resource(conn6, "internet", canonical_uri="https://example.com/a")
check(
    "get_or_create_resource: reuses existing resource_id by uri_hash (stable root, no duplicate row)",
    rid == 55 and not _sql_contains(conn6, "INSERT INTO source_resource"),
    f"rid={rid} executed={conn6.executed}",
)

conn7 = FakeConnection()
conn7.when(lambda s: "SELECT resource_id FROM source_resource" in s, None)
rid2 = repo.get_or_create_resource(conn7, "internet", canonical_uri="https://example.com/new")
check(
    "get_or_create_resource: creates a new resource when uri_hash has never been seen",
    rid2 is not None and _sql_contains(conn7, "INSERT INTO source_resource"),
    f"{conn7.executed}",
)

conn8 = FakeConnection()
r_a = repo.get_or_create_resource(conn8, "network_node", node_id="node_1")  # no canonical_uri
check(
    "get_or_create_resource: non-internet resource (no canonical_uri) skips the uri_hash lookup "
    "entirely and always inserts (schema-ready, not yet deduplicated for node/ai_chat/local_model)",
    _sql_contains(conn8, "INSERT INTO source_resource")
    and not _sql_contains(conn8, "SELECT resource_id FROM source_resource"),
    f"{conn8.executed}",
)

# ============================================================
# record_source_observation: controlled rejection_reason vocabulary
# ============================================================

conn9 = FakeConnection()
try:
    repo.record_source_observation(
        conn9, resource_id=1, run_id="r1", observation_route="internet",
        rejection_reason="cache_hit_debug_noise",
    )
    bad_reason_raised = False
except ValueError:
    bad_reason_raised = True

check(
    "record_source_observation: rejects an out-of-vocabulary rejection_reason "
    "(controlled vocabulary, mandate §13 — no debug-noise reasons)",
    bad_reason_raised,
    "no ValueError raised for an invalid reason",
)

conn10 = FakeConnection()
repo.record_source_observation(
    conn10, resource_id=1, run_id="r1", observation_route="internet", rejection_reason="unrelated",
)
check(
    "record_source_observation: accepts a valid controlled-vocabulary rejection_reason",
    _sql_contains(conn10, "INSERT INTO source_observation"),
    f"{conn10.executed}",
)

# ============================================================
# local_memory replay provenance: origin_observation_id round-trips
# ============================================================

conn11 = FakeConnection()
repo.record_source_observation(
    conn11, resource_id=1, run_id="r2", observation_route="local_memory", origin_observation_id=999,
)
check(
    "record_source_observation: origin_observation_id (replay provenance) is passed through to the INSERT",
    any(999 in (params or ()) for _sql, params in conn11.executed),
    f"{conn11.executed}",
)

# ============================================================
# run lifecycle: start/complete/fail — correct status transitions
# ============================================================

conn12 = FakeConnection()
repo.start_run(conn12, "run_1", occurrence_id=1, web_enabled=True)
check(
    "start_run: inserts status='running'",
    _sql_contains(conn12, "INSERT INTO verification_run", "'running'"),
    f"{conn12.executed}",
)

conn13 = FakeConnection()
repo.complete_run(conn13, "run_1", final_answer_id=7)
check(
    "complete_run: UPDATE status='completed' guarded by AND status='running' "
    "(never completes an already-aborted/failed run)",
    _sql_contains(conn13, "UPDATE verification_run", "status='completed'", "AND status='running'"),
    f"{conn13.executed}",
)

conn14 = FakeConnection()
repo.fail_run(conn14, "run_1", failed_stage="claims", error_class="TimeoutError", outcome="aborted")
check(
    "fail_run: UPDATE status accepts 'aborted' outcome for crash reconciliation",
    _sql_contains(conn14, "UPDATE verification_run", "status=%s")
    and any("aborted" in (p or ()) for _s, p in conn14.executed),
    f"{conn14.executed}",
)

conn15 = FakeConnection()
try:
    repo.fail_run(conn15, "run_1", "claims", "X", outcome="completed")
    bad_outcome_raised = False
except AssertionError:
    bad_outcome_raised = True
check(
    "fail_run: refuses an invalid outcome value (only 'failed'/'aborted' are legal here — "
    "'completed' must only ever come from complete_run())",
    bad_outcome_raised,
    "no AssertionError raised",
)

conn16 = FakeConnection()
repo.reconcile_stale_running_runs(conn16, older_than_seconds=1800)
check(
    "reconcile_stale_running_runs: marks stale running runs as 'aborted', never 'completed'",
    _sql_contains(conn16, "UPDATE verification_run", "status='aborted'", "WHERE status='running'"),
    f"{conn16.executed}",
)

# ============================================================
# claim_family: write-once canonical_text via INSERT IGNORE
# ============================================================

conn17 = FakeConnection()
repo.get_or_create_claim_family(conn17, "fam_x", "science", "canonical text")
check(
    "get_or_create_claim_family: uses INSERT IGNORE (write-once, never UPDATEs canonical_text — "
    "matches confirmed current Python behavior)",
    _sql_contains(conn17, "INSERT IGNORE INTO claim_family"),
    f"{conn17.executed}",
)

# ============================================================
# explain_answer: joins answer -> assessment -> run -> claims -> evidence
# ============================================================

conn18 = FakeConnection()
conn18.when(lambda s: s.strip().startswith("SELECT * FROM answer_version"), {"answer_id": 1, "created_by_run_id": "r1"})
conn18.when(lambda s: "FROM answer_assessment" in s, {"canonical_trust": "VERIFIED"})
conn18.when(lambda s: s.strip().startswith("SELECT * FROM verification_run"), {"run_id": "r1"})
conn18.when(lambda s: s.strip().startswith("SELECT * FROM claim_occurrence"), [{"claim_id": "cl_1"}])
conn18.when(lambda s: "FROM evidence_relation er" in s, [{"relation": "supports"}])

explanation = repo.explain_answer(conn18, answer_id=1)
check(
    "explain_answer: walks the full chain (answer/assessment/run/claims/evidence) in one call",
    explanation["answer"]["answer_id"] == 1
    and explanation["run"]["run_id"] == "r1"
    and explanation["claims"][0]["claim_id"] == "cl_1"
    and explanation["claims"][0]["evidence"][0]["relation"] == "supports",
    f"{explanation}",
)

# ============================================================
# compare_runs: added/lost/changed by resource_id, not observation_id
# ============================================================

conn19 = FakeConnection()

def _sources_for(run_id):
    if run_id == "run_a":
        return [
            {"resource_id": 1, "canonical_uri": "https://x.example/a"},
            {"resource_id": 2, "canonical_uri": "https://x.example/b"},
        ]
    return [
        {"resource_id": 2, "canonical_uri": "https://x.example/b"},
        {"resource_id": 3, "canonical_uri": "https://x.example/c"},
    ]

import unittest.mock as _mock
with _mock.patch.object(repo, "get_sources_for_run", side_effect=lambda c, rid: _sources_for(rid)):
    conn19.when(lambda s: "so.run_id=%s AND so.resource_id=%s" in s, [{"relation": "supports"}])
    diff = repo.compare_runs(conn19, "run_a", "run_b")

check(
    "compare_runs: resource present only in run_b is 'added'",
    any(r["resource_id"] == 3 for r in diff["added"]),
    f"{diff}",
)
check(
    "compare_runs: resource present only in run_a is 'lost'",
    any(r["resource_id"] == 1 for r in diff["lost"]),
    f"{diff}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
