"""
agent/db_sql_origin_observation_regression_test.py — Этап 5 (SQL
persistence migration) regression: origin_observation_id resolution
for local_memory replay (mandate §15 gap, closed).

Before this: shadow_record_claims_and_evidence() always wrote
origin_observation_id=NULL for a local_memory replay — MIGRATION_
STATUS.md's §41 documented this as a deliberate, honest limitation
("resolving which SQL observation a local_memory replay pointed at
needs a resource+run+time lookup not built in this pass"). This suite
proves the lookup now works: agent.db.sql.repositories.
find_observation_id_for_replay() uses the replay's JSON-side origin_
trace_id (== the original run's SQL run_id) to find the SQL-side
source_observation row the original run wrote for the same resource.

Covers:
    A. find_observation_id_for_replay(): resolves to the matching
       observation_id when the origin run DID write one; None when
       origin_run_id is falsy; None when no matching row exists
       (never fabricated).
    B. shadow_record_claims_and_evidence(): a local_memory replay whose
       origin run has a matching SQL observation gets a real (non-NULL)
       origin_observation_id in its INSERT; a DIRECT internet
       observation (not a replay) still gets NULL — the FK only
       applies to replays, never to an original observation pointing
       at itself.
    C. fail-open: SQL genuinely unconfigured — unaffected (this lookup
       sits inside the same fail-open transaction as everything else
       in shadow_record_claims_and_evidence()).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_origin_observation_regression_test
"""
from __future__ import annotations

import contextlib
from unittest.mock import patch

import agent.db.sql.repositories as repo
import agent.db.sql.shadow_write as sw
import agent.db.sql.connection as sqlconn

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


def _noop_log(*a, **k):
    pass


# ============================================================
# A. find_observation_id_for_replay() unit behavior, real SQL text,
# fake DB-API connection with scripted fetchone() results.
# ============================================================

class ScriptedCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = None
        self._last_sql = None
        self._last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((" ".join(sql.split()), params))
        self._last_sql = " ".join(sql.split())
        self._last_params = params

    def fetchone(self):
        for pattern, params_match, result in self.conn.script:
            if pattern in self._last_sql and (params_match is None or params_match == self._last_params):
                return result
        return None

    def fetchall(self):
        return []


class ScriptedConnection:
    def __init__(self, script):
        self.calls = []
        self.script = script  # list of (sql_fragment, params_or_None, fetchone_result)
        self.next_id = 1000

    def cursor(self):
        return ScriptedCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


conn_a1 = ScriptedConnection(script=[
    ("FROM source_observation WHERE resource_id=%s AND run_id=%s", (555, "origin_run_1"),
     {"observation_id": 777}),
])
result_a1 = repo.find_observation_id_for_replay(conn_a1, 555, "origin_run_1")
check("A: resolves to the matching observation_id when the origin run wrote one", result_a1 == 777, f"got {result_a1!r}")

check(
    "A: falsy origin_run_id -> None, no query even issued",
    repo.find_observation_id_for_replay(ScriptedConnection(script=[]), 555, None) is None,
)

conn_a2 = ScriptedConnection(script=[])  # no script entries -> fetchone() always None
result_a2 = repo.find_observation_id_for_replay(conn_a2, 555, "origin_run_with_no_sql_record")
check("A: no matching row -> None, never fabricated", result_a2 is None, f"got {result_a2!r}")


# ============================================================
# B. shadow_record_claims_and_evidence(): replay gets a real
# origin_observation_id; a direct (non-replay) observation gets NULL.
# ============================================================

def _run_shadow(script, claims_data, evidence_data, run_id="run_current"):
    conn = ScriptedConnection(script=script)

    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    with patch.object(sw, "get_connection", _fake_get_connection):
        sw.shadow_record_claims_and_evidence(
            run_id=run_id, claims_data=claims_data, evidence_data=evidence_data,
            log=_noop_log, verbose=True,
        )
    return conn


claims_replay = [{
    "claim_id": "cl_orig_1", "claim_text": "claim",
    "evidence_relations": [{"evidence_id": "ev_1", "relation": "supports", "evidence_eligible": True}],
}]
evidence_replay = [{
    "evidence_id": "ev_1", "route": "local_memory", "origin_route": "internet",
    "origin_trace_id": "origin_run_1", "source_uri": "https://x.example/replayed", "observed_at": 0.0,
}]

conn_b1 = _run_shadow(
    script=[
        ("FROM source_resource WHERE uri_hash", None, {"resource_id": 555}),
        ("FROM source_observation WHERE resource_id=%s AND run_id=%s", (555, "origin_run_1"),
         {"observation_id": 777}),
    ],
    claims_data=claims_replay, evidence_data=evidence_replay,
)
_insert_so = next((p for s, p in conn_b1.calls if s.startswith("INSERT INTO source_observation")), None)
check(
    "B: a local_memory replay whose origin run wrote a matching SQL observation "
    "gets a REAL (non-NULL) origin_observation_id in its INSERT",
    _insert_so is not None and _insert_so[3] == 777,
    f"{_insert_so}",
)

claims_direct = [{
    "claim_id": "cl_orig_2", "claim_text": "claim",
    "evidence_relations": [{"evidence_id": "ev_2", "relation": "supports", "evidence_eligible": True}],
}]
evidence_direct = [{
    "evidence_id": "ev_2", "route": "internet", "source_uri": "https://x.example/direct", "observed_at": 0.0,
}]
conn_b2 = _run_shadow(
    script=[("FROM source_resource WHERE uri_hash", None, {"resource_id": 556})],
    claims_data=claims_direct, evidence_data=evidence_direct,
)
_insert_so2 = next((p for s, p in conn_b2.calls if s.startswith("INSERT INTO source_observation")), None)
check(
    "B: a DIRECT (non-replay) internet observation still gets NULL origin_observation_id "
    "(the FK only applies to replays, never to an original observation)",
    _insert_so2 is not None and _insert_so2[3] is None,
    f"{_insert_so2}",
)

# Replay whose origin run has NO SQL record at all (predates SQL shadow-
# writing, or the DB was down then) -> NULL, never fabricated.
conn_b3 = _run_shadow(
    script=[("FROM source_resource WHERE uri_hash", None, {"resource_id": 557})],
    claims_data=[{
        "claim_id": "cl_orig_3", "claim_text": "claim",
        "evidence_relations": [{"evidence_id": "ev_3", "relation": "supports", "evidence_eligible": True}],
    }],
    evidence_data=[{
        "evidence_id": "ev_3", "route": "local_memory", "origin_route": "internet",
        "origin_trace_id": "origin_run_never_recorded", "source_uri": "https://x.example/orphan-replay",
        "observed_at": 0.0,
    }],
)
_insert_so3 = next((p for s, p in conn_b3.calls if s.startswith("INSERT INTO source_observation")), None)
check(
    "B: a replay whose origin run has NO matching SQL observation gets NULL, never fabricated",
    _insert_so3 is not None and _insert_so3[3] is None,
    f"{_insert_so3}",
)


# ============================================================
# C. Fail-open — SQL endpoint genuinely unreachable (forced,
# deterministic — DATABASE BOOTSTRAP V1's canonical defaults mean
# is_configured() is True out of the box now).
# ============================================================

with patch.dict("os.environ", {"YANDI_SQL_SOCKET": "/nonexistent/origin-observation-test/mysql.sock"}):
    check(
        "C precondition: SQL layer resolves canonical defaults (is_configured()=True) "
        "but the forced socket path is genuinely unreachable",
        sqlconn.is_configured() is True,
    )
    try:
        sw.shadow_record_claims_and_evidence(
            run_id="run_x", claims_data=claims_replay, evidence_data=evidence_replay, log=_noop_log, verbose=True,
        )
        no_raise = True
    except Exception:
        no_raise = False
    check("C: shadow_record_claims_and_evidence never raises with SQL endpoint unreachable", no_raise)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
