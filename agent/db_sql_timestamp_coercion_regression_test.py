"""
agent/db_sql_timestamp_coercion_regression_test.py — Этап 5 (SQL
persistence migration) regression: P0 timestamp-type fix found while
investigating mandate §15 (origin_observation_id for local_memory
replay).

Two real bugs, found by code inspection (never caught before because
no live DB has ever existed to fail against):

  BUG 1: agent.orch_tracer.Trace.timestamp is a Unix-epoch float
  (`time.time()`), forwarded UNCHANGED as started_at/asked_at from
  orchestrator_v2.py's shadow_record_question_and_run() call, all the
  way to repositories.py's resolve_question()/start_run(), which bind
  it directly as a query parameter for a DATETIME column. A bare float
  is not a valid MySQL datetime literal — every single question/
  verification_run row would have failed to insert on a real live DB,
  silently swallowed by the shadow layer's fail-open contract. Fixed by
  agent.db.sql.repositories._coerce_datetime(), applied at every
  `X = X or _now()` call site in that module.

  BUG 2: agent/orchestrator/response/writeback.py's shadow_complete_run
  call used `datetime.now()` (naive LOCAL time) for completed_at, while
  every other repositories.py timestamp defaults to `datetime.utcnow()`
  (_now()). On a server not in UTC, a run's completed_at could sort
  BEFORE its own started_at — corrupting run-duration and history
  ordering the moment a live DB exists. Fixed: datetime.utcnow().

Covers:
    A. _coerce_datetime(): float -> UTC datetime, None -> None,
       datetime -> unchanged (identity, not a copy).
    B. resolve_question()/start_run() bind a real datetime object (not
       a float) to their DATETIME columns when given Trace.timestamp's
       actual float shape — FakeConnection param inspection.
    C. structural: writeback.py's shadow_complete_run call uses
       datetime.utcnow(), not datetime.now().
    D. every `X or _now()` timestamp default in repositories.py is
       preceded by `_coerce_datetime(X)` — no call site was missed.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_timestamp_coercion_regression_test
"""
from __future__ import annotations

import contextlib
import inspect
import re
import time
from datetime import datetime
from unittest.mock import patch

import agent.db.sql.repositories as repo
import agent.orchestrator.response.writeback as writeback_mod

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


# ============================================================
# A. _coerce_datetime() unit behavior.
# ============================================================

_epoch_float = 1735500000.123456
_coerced = repo._coerce_datetime(_epoch_float)
check(
    "A: float epoch -> datetime, matching datetime.utcfromtimestamp (UTC, not local)",
    _coerced == datetime.utcfromtimestamp(_epoch_float),
    f"got {_coerced!r}",
)
check("A: None stays None", repo._coerce_datetime(None) is None)
_dt = datetime(2026, 1, 1, 12, 0, 0)
check("A: an already-correct datetime passes through unchanged", repo._coerce_datetime(_dt) is _dt)
check("A: int epoch also coerced", isinstance(repo._coerce_datetime(1735500000), datetime))


# ============================================================
# B. resolve_question()/start_run() bind a real datetime, not a float,
# when given Trace.timestamp's actual shape (a float).
# ============================================================

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((" ".join(sql.split()), params))
        if sql.strip().upper().startswith("SELECT"):
            self._result = None
        if sql.strip().upper().startswith("INSERT"):
            self.conn.next_id += 1
            self.lastrowid = self.conn.next_id

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.next_id = 1000

    def cursor(self):
        return FakeCursor(self)


conn_b = FakeConnection()
_trace_timestamp = time.time()  # the exact real shape: a float
ids = repo.resolve_question(conn_b, "Сколько спутников у Юпитера?", None, asked_at=_trace_timestamp)

_question_insert = next(
    (p for s, p in conn_b.calls if s.startswith("INSERT INTO question ")), None
)
_occurrence_insert = next(
    (p for s, p in conn_b.calls if s.startswith("INSERT INTO question_occurrence")), None
)
check(
    "B: resolve_question() binds a datetime object (not the raw float) to "
    "question.first_asked_at",
    _question_insert is not None and isinstance(_question_insert[1], datetime),
    f"{_question_insert}",
)
check(
    "B: resolve_question() binds a datetime object (not the raw float) to "
    "question_occurrence.asked_at",
    _occurrence_insert is not None and isinstance(_occurrence_insert[3], datetime),
    f"{_occurrence_insert}",
)

conn_b2 = FakeConnection()
repo.start_run(conn_b2, "run_x", ids["occurrence_id"], started_at=_trace_timestamp)
_run_insert = next((p for s, p in conn_b2.calls if s.startswith("INSERT INTO verification_run")), None)
check(
    "B: start_run() binds a datetime object (not the raw float) to verification_run.started_at",
    _run_insert is not None and isinstance(_run_insert[2], datetime),
    f"{_run_insert}",
)


# ============================================================
# C. Structural: writeback.py uses datetime.utcnow(), not datetime.now(),
# for shadow_complete_run's completed_at (UTC-consistency with _now()).
# ============================================================

_src = inspect.getsource(writeback_mod)
_call_start = _src.find("shadow_complete_run(")
_call_end = _src.find(")", _src.find("log=log, verbose=verbose", _call_start))
_call_block = _src[_call_start:_call_end]

check(
    "C: shadow_complete_run's completed_at uses datetime.utcnow() "
    "(matches repositories.py's _now() = datetime.utcnow(), not local time)",
    "completed_at=datetime.utcnow()" in _call_block,
    f"{_call_block}",
)
check(
    "C: datetime.now() (naive LOCAL time, the bug) no longer appears in that call",
    "completed_at=datetime.now()" not in _call_block,
)


# ============================================================
# D. Every `X or _now()` default in repositories.py is coerced first —
# no call site missed by the mechanical fix.
# ============================================================

_repo_src = inspect.getsource(repo)
_bare_or_now = re.findall(r"^\s*\w+ = \w+ or _now\(\)$", _repo_src, re.MULTILINE)
check(
    "D: no remaining bare `X = X or _now()` call site (all coerced via _coerce_datetime first)",
    len(_bare_or_now) == 0,
    f"found: {_bare_or_now}",
)
_coerced_sites = re.findall(r"^\s*(\w+) = _coerce_datetime\(\1\) or _now\(\)$", _repo_src, re.MULTILINE)
check(
    "D: at least the known timestamp params (asked_at/started_at/completed_at/"
    "created_at/linked_at/observed_at) are all coerced",
    len(_coerced_sites) >= 12,
    f"found {len(_coerced_sites)}: {_coerced_sites}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
