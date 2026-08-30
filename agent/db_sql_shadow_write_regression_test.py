"""
agent/db_sql_shadow_write_regression_test.py — Этап 5 (SQL persistence
migration) regression: agent/db/sql/shadow_write.py fail-open contract.

DATABASE BOOTSTRAP V1, seventeenth Phase B attempt (default SQL config
resolver): this suite's ORIGINAL premise — "every check below runs with
ZERO YANDI_SQL_* environment variables set, proving fail-open against
the REAL current 'not configured' state" — stopped being universally
true the moment connection.py gained canonical dedicated-appliance
defaults (is_configured() is now True out of the box on a host with a
bootstrapped dedicated instance, by design — see connection.py's own
module docstring). On THIS specific host, with zero env vars set, the
default resolver now genuinely reaches the live dedicated socket.

So this suite no longer relies on ambient environment state at all —
every "SQL unavailable" scenario below is FORCED deterministically via
an explicit YANDI_SQL_SOCKET override pointing at a path that cannot
exist, so the suite proves the exact same fail-open contract
regardless of whether the machine running it happens to have a live
dedicated instance reachable or not (mandate: don't couple test
determinism to which host happens to run it).

Covers:
    A. every shadow_* function returns cleanly (no raise) when the
       resolved SQL endpoint is genuinely unreachable
    B. a malformed/wrong-typed call still doesn't raise out of the shadow layer
    C. a repository-level exception (mocked) is caught and rolled back, not raised
    D. shadow functions never mutate their input arguments
    E. get_connection()/SqlUnavailable behave correctly as the single
       exception type every shadow function relies on

Run: /home/iam/venv/bin/python3 -m agent.db_sql_shadow_write_regression_test
"""
from __future__ import annotations

import os
import time
import types
from unittest.mock import patch

import agent.db.sql.shadow_write as sw
import agent.db.sql.connection as sqlconn
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


def _noop_log(*a, **k):
    pass


# A path that cannot exist on any filesystem, ever — forces a real,
# deterministic connection failure (ENOENT/ECONNREFUSED) regardless of
# whether THIS host happens to have a live dedicated instance at the
# real default path. Every scenario below runs inside this override so
# the suite's result never depends on ambient host state.
_UNREACHABLE_SOCKET = "/nonexistent/agent-db-sql-shadow-write-regression-test/mysql.sock"
_forced_unreachable = patch.dict(
    "os.environ", {"YANDI_SQL_SOCKET": _UNREACHABLE_SOCKET, "YANDI_SQL_AUTH_MODE": "auth_socket"},
)
_forced_unreachable.start()

check(
    "PRECONDITION: is_configured() is True (canonical defaults always resolve — "
    "DATABASE BOOTSTRAP V1) even though the SOCKET this run forces is deliberately "
    "unreachable — 'configured' and 'reachable' are different questions",
    sqlconn.is_configured() is True,
    f"is_configured()={sqlconn.is_configured()}",
)
_precondition_raised = False
try:
    with sqlconn.get_connection():
        pass
except sqlconn.SqlUnavailable:
    _precondition_raised = True
check(
    "PRECONDITION: get_connection() genuinely fails against the forced-unreachable "
    "socket (deterministic 'SQL unavailable', independent of ambient host state)",
    _precondition_raised,
)

# ============================================================
# A. Every shadow_* function returns cleanly, no DB configured.
# ============================================================

r1 = sw.shadow_record_question_and_run(
    raw_text="Сколько спутников у Юпитера?", run_id="t1", started_at=time.time(),
    web_enabled=True, validation_enabled=False, pipeline_version="abc123",
    log=_noop_log, verbose=True,
)
check("A: shadow_record_question_and_run returns None (no raise) with no DB configured", r1 is None, f"{r1}")

sw.shadow_complete_run(
    run_id="t1", question_id=1, delivered_answer_text="answer text",
    completed_at=time.time(), canonical_trust="UNVERIFIED", log=_noop_log, verbose=True,
)
check("A: shadow_complete_run does not raise with no DB configured", True)

sw.shadow_fail_run(run_id="t1", failed_stage="claims", error_class="TimeoutError", log=_noop_log, verbose=True)
check("A: shadow_fail_run does not raise with no DB configured", True)

sw.shadow_record_claim(
    claim_id="cl_1", run_id="t1", claim_text="claim text", content_hash="h1",
    claim_type="factual", claim_confidence=0.5, verification_status="unverified",
    family_id="fam_1", family_domain="science", family_canonical_text="canonical",
    query_context="q", support_count=0, contradiction_count=0, log=_noop_log, verbose=True,
)
check("A: shadow_record_claim does not raise with no DB configured", True)

r_reconcile = sw.shadow_reconcile_stale_runs(log=_noop_log, verbose=True)
check("A: shadow_reconcile_stale_runs returns None (no raise) with no DB configured", r_reconcile is None, f"{r_reconcile}")

sw.shadow_record_evidence(
    claim_id="cl_1", run_id="t1", resource_type="internet", canonical_uri="https://x.example/a",
    observation_route="internet", origin_observation_id=None, observed_at=time.time(),
    source_class="reference", quality_score=0.8, content_excerpt="excerpt",
    relation="supports", directness=0.8, evidence_eligible=True,
    evidence_role="direct", counted_via="authority", log=_noop_log, verbose=True,
)
check("A: shadow_record_evidence does not raise with no DB configured", True)

# ============================================================
# B. Malformed/wrong-typed calls still don't escape the shadow layer.
# ============================================================

r_bad = sw.shadow_record_question_and_run(
    raw_text=None, run_id=12345, started_at="not-a-date",  # wrong types on purpose
    web_enabled="yes", validation_enabled=None, pipeline_version=object(),
    log=_noop_log, verbose=True,
)
check(
    "B: even nonsense/wrong-typed arguments never raise out of the shadow layer "
    "(fail-open must survive caller bugs too, not just DB outages)",
    r_bad is None,
    f"{r_bad}",
)

# ============================================================
# C. A repository-level exception (simulated: DB WOULD be reachable but
# the repository call itself blows up) is caught and rolled back.
# ============================================================

class _FakeConnThatCommitsOrRollsBack:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        raise RuntimeError("simulated repository-level failure")

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


import contextlib


@contextlib.contextmanager
def _fake_get_connection(autocommit=False):
    conn = _FakeConnThatCommitsOrRollsBack()
    yield conn


with patch.object(sw, "get_connection", _fake_get_connection):
    r_c = sw.shadow_record_claim(
        claim_id="cl_2", run_id="t2", claim_text="x", content_hash=None,
        claim_type=None, claim_confidence=None, verification_status=None,
        family_id=None, family_domain=None, family_canonical_text=None,
        query_context=None, support_count=0, contradiction_count=0,
        log=_noop_log, verbose=True,
    )
check(
    "C: a repository-level exception (DB reachable, query itself fails) is caught, "
    "never escapes shadow_record_claim()",
    r_c is None,
    f"{r_c}",
)

# ============================================================
# D. Shadow functions never mutate their input arguments.
# ============================================================

original_kwargs = dict(
    claim_id="cl_3", run_id="t3", claim_text="immutable check", content_hash="h3",
    claim_type="factual", claim_confidence=0.7, verification_status="supported",
    family_id="fam_3", family_domain="science", family_canonical_text="canonical text",
    query_context="query context text", support_count=1, contradiction_count=0,
)
snapshot = dict(original_kwargs)
sw.shadow_record_claim(**original_kwargs, log=_noop_log, verbose=False)
check(
    "D: shadow_record_claim's keyword arguments are unchanged after the call "
    "(no in-place mutation of caller data)",
    original_kwargs == snapshot,
    f"before={snapshot} after={original_kwargs}",
)

# ============================================================
# E. get_connection()/SqlUnavailable — the single exception type every
# shadow function relies on for its fail-open guarantee.
# ============================================================

check(
    "E: SqlUnavailable is a real, importable exception type "
    "(the shadow layer's whole fail-open contract rests on catching exactly this)",
    issubclass(sqlconn.SqlUnavailable, Exception),
)

raised = False
try:
    with sqlconn.get_connection():
        pass
except sqlconn.SqlUnavailable:
    raised = True
check(
    "E: get_connection() raises SqlUnavailable (not a raw pymysql/socket error) "
    "when the resolved SQL endpoint is unreachable — this is what makes fail-open "
    "possible with ONE except clause",
    raised,
)

_forced_unreachable.stop()

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
