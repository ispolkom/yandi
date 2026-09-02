"""
agent/db_sql_decision_event_regression_test.py — DATABASE BOOTSTRAP V1:
"живая память" (owner request) — the decision/reasoning ledger moves
from agent/orch_ledger.py's unprotected SQLite file into the hardened
dedicated SQL instance.

LIVE-CONFIRMED BUG this closes: every production call site
(orchestrator_v2.py, orchestrator/pipeline.py, orchestrator/response/
writeback.py — ~15 call sites total) imported add_decision_event from
agent.orch_reputation, which is a dead stub ("Заглушка для
совместимости с Decision Ledger" — literally `pass`). The REAL,
feature-complete implementation lives in agent/orch_ledger.py
("Decision Ledger V4"), but nothing in production ever imported it —
confirmed via both DB files' own last-event timestamps (registry/
ledger/decision_ledger.db: 2026-07-01, registry/reputation/decision_
ledger.db: 2026-06-30) sitting silent for ~2 months while the pipeline
kept calling the stub on every single request.

Owner's explicit requirement for the replacement: "ни я, ни ты, ни
следующие поколения не имеют прав для изменения" — a bare SQLite file
has NO access-control model at all (any process with filesystem access
can open and edit/delete any row). The dedicated MySQL instance's
GRANT model + immutability triggers (already live-pentested this
mandate) is a REAL enforcement boundary instead — this is why the
Decision Ledger's replacement lives in decision_event (schema.py),
not a reconnection to orch_ledger.py's own SQLite file.

Covers:
    A. Schema: decision_event is correctly positioned (after
       verification_run/answer_version, its own FK targets), classified
       "B" (gets the standard no-update/no-delete immutability triggers
       automatically, same as every other history table), no banned
       truth-tokens, SCHEMA_VERSION bumped.
    B. repositories.record_decision_event()/get_decision_trace(): a
       real INSERT+SELECT round-trip against a stateful fake connection,
       including JSON encode/decode of delta_factors/meta/policy_snapshot.
    C. shadow_write.shadow_record_decision_event(): TRUE drop-in
       signature compatibility with every real production call site
       (not just similar — the EXACT keyword shape grepped from
       orchestrator_v2.py/pipeline.py/writeback.py); fail-open when SQL
       is unreachable; event_id auto-generated.
    D. THE ORDERING FIX: orchestrator_v2.py's shadow_record_question_
       and_run() (which creates the verification_run row
       decision_event.run_id's FK depends on) now runs BEFORE the first
       add_decision_event() call — live-confirmed ordering trap, the
       same class of bug already fixed twice this mandate for the SQL
       bootstrap's own GRANT/USE sequencing.
    E. All three production files now import add_decision_event from
       the hardened SQL path, not the dead stub.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_decision_event_regression_test
"""
from __future__ import annotations

import inspect
import json

from agent.db.sql.schema import (
    ALL_TABLES_IN_ORDER, TABLE_CLASSIFICATION, SCHEMA_VERSION, DECISION_EVENT,
)
from agent.db.sql.security_triggers import immutability_triggers
from agent.db.sql import repositories as repo
from agent.db.sql import shadow_write as sw
from agent.db.sql.connection import SqlUnavailable

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
# A. Schema.
# ============================================================
_table_names = [n for n, _ in ALL_TABLES_IN_ORDER]
check("A1. decision_event is a real table in ALL_TABLES_IN_ORDER", "decision_event" in _table_names)
check(
    "A2. decision_event is positioned AFTER verification_run AND answer_version "
    "(both are its FK targets — MySQL needs them to already exist)",
    _table_names.index("decision_event") > _table_names.index("verification_run")
    and _table_names.index("decision_event") > _table_names.index("answer_version"),
)
check(
    "A3. decision_event is classified 'B' (append-only history) — automatically "
    "gets the standard immutability triggers, no bespoke guard needed",
    TABLE_CLASSIFICATION.get("decision_event") == "B",
)
_trigger_names = [name for name, _ in immutability_triggers()]
check(
    "A4. trg_decision_event_no_update and trg_decision_event_no_delete both exist "
    "in the generated trigger set (classification 'B' wiring actually took effect)",
    "trg_decision_event_no_update" in _trigger_names and "trg_decision_event_no_delete" in _trigger_names,
)
check(
    "A5. no banned truth-claiming vocabulary in the DDL (mandate §1/§14, same "
    "discipline as every other table)",
    all(tok not in DECISION_EVENT for tok in ("is_true", "verified_truth", "absolute_truth", "truth_certificate")),
)
check("A6. SCHEMA_VERSION was bumped for this addition (not silently reusing v1)", SCHEMA_VERSION >= 2)
check(
    "A7. decision_event carries a self-FK for parent_event_id (the decision GRAPH, "
    "not a flat log — matches agent/orch_ledger.py's own DecisionEvent design)",
    "fk_de_parent" in DECISION_EVENT and "REFERENCES decision_event(event_id)" in DECISION_EVENT,
)


# ============================================================
# B. repositories.record_decision_event() / get_decision_trace() —
# real INSERT+SELECT round-trip against a stateful fake connection.
# ============================================================

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        if norm.upper().startswith("INSERT INTO DECISION_EVENT"):
            cols = [
                "event_id", "run_id", "event_type", "entity_type", "entity_id", "verdict",
                "domain", "confidence", "delta", "delta_factors", "reason", "meta",
                "parent_event_id", "duration_ms", "policy_snapshot", "policy_version",
                "orchestrator_version", "created_at",
            ]
            self.conn.rows.append(dict(zip(cols, params)))
            self._rows = None
        elif norm.upper().startswith("SELECT * FROM DECISION_EVENT WHERE RUN_ID"):
            (run_id,) = params
            self._rows = [r for r in self.conn.rows if r["run_id"] == run_id]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows or []

    def fetchone(self):
        return (self._rows or [None])[0]


class _FakeConn:
    def __init__(self):
        self.rows = []

    def cursor(self):
        return _FakeCursor(self)


conn_b = _FakeConn()
repo.record_decision_event(
    conn_b, event_id="evt_1", run_id="run_b1", event_type="DecisionStarted",
    entity_type="decision", entity_id="dec_1", verdict="STARTED", domain="general",
    confidence=0.5, delta=0.0, delta_factors={"support": 0.3, "speed": 0.7},
    reason="Query: test", meta={"query": "test question"},
)
repo.record_decision_event(
    conn_b, event_id="evt_2", run_id="run_b1", event_type="ReputationUpdated",
    entity_type="route", entity_id="general_web", verdict="VERIFIED", domain="general",
    confidence=0.8, delta=0.1, delta_factors=None, reason="worked well",
    meta=None, parent_event_id="evt_1", duration_ms=1234,
    policy_snapshot={"threshold": 0.6}, policy_version="RegistryFirstPolicy v1",
    orchestrator_version="v2.0",
)
# a different run's event must never leak into run_b1's trace
repo.record_decision_event(
    conn_b, event_id="evt_x", run_id="run_OTHER", event_type="DecisionStarted",
    entity_type="decision", entity_id="dec_x", verdict="STARTED",
)

trace_b1 = repo.get_decision_trace(conn_b, "run_b1")

check("B1. record_decision_event() actually inserts a row", len(conn_b.rows) == 3)
check(
    "B2. get_decision_trace() returns ONLY this run's events, in insertion order",
    [r["event_id"] for r in trace_b1] == ["evt_1", "evt_2"],
    f"{[r['event_id'] for r in trace_b1]}",
)
check(
    "B3. delta_factors round-trips through JSON encode (write) / decode (read) intact",
    trace_b1[0]["delta_factors"] == {"support": 0.3, "speed": 0.7},
    f"{trace_b1[0]['delta_factors']!r}",
)
check(
    "B4. meta round-trips through JSON encode/decode intact",
    trace_b1[0]["meta"] == {"query": "test question"},
    f"{trace_b1[0]['meta']!r}",
)
check(
    "B5. policy_snapshot round-trips through JSON encode/decode intact",
    trace_b1[1]["policy_snapshot"] == {"threshold": 0.6},
    f"{trace_b1[1]['policy_snapshot']!r}",
)
check(
    "B6. parent_event_id correctly links the second event to the first "
    "(the decision GRAPH, not a flat log)",
    trace_b1[1]["parent_event_id"] == "evt_1",
)
check(
    "B7. a NULL delta_factors/meta stays None after the round-trip, never "
    "coerced into an empty dict or the literal string 'null'",
    trace_b1[1]["delta_factors"] is None and trace_b1[1]["meta"] is None,
    f"{trace_b1[1]['delta_factors']!r} {trace_b1[1]['meta']!r}",
)


# ============================================================
# C. shadow_write.shadow_record_decision_event() — TRUE drop-in
# compatibility with every REAL production call site's exact keyword
# shape (not a redesigned signature).
# ============================================================

_orch_src = inspect.getsource(__import__("agent.orchestrator_v2", fromlist=["x"]))
_pipeline_src = inspect.getsource(__import__("agent.orchestrator.pipeline", fromlist=["x"]))
_writeback_src = inspect.getsource(__import__("agent.orchestrator.response.writeback", fromlist=["x"]))

import ast


def _extract_kwarg_names(src: str, call_name: str) -> set:
    """AST-based (not regex) extraction of every ACTUAL keyword argument
    NAME passed to `call_name(...)` — regex over raw source text was
    tried first and produced false positives from f-string
    interpolations INSIDE a reason=f"..." value (e.g. reason=f"risk=
    {risk_level}, nodes={n}" regex-matched a bogus 'nodes' keyword that
    isn't a keyword argument to add_decision_event() at all — it's text
    inside a string literal). Parsing real Python syntax is the only
    reliable way to tell "keyword argument name" apart from "text that
    happens to look like one\""."""
    tree = ast.parse(src)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == call_name:
            names |= {kw.arg for kw in node.keywords if kw.arg}
    return names


_used_kwargs = set()
for _src in (_orch_src, _pipeline_src, _writeback_src):
    _used_kwargs |= _extract_kwarg_names(_src, "add_decision_event")

check(
    "C1. every keyword actually used at a real call site is genuinely accepted by "
    "shadow_record_decision_event() (TRUE drop-in, not a redesigned signature "
    "that happens to look similar)",
    _used_kwargs and _used_kwargs <= set(inspect.signature(sw.shadow_record_decision_event).parameters),
    f"used={_used_kwargs} missing="
    f"{_used_kwargs - set(inspect.signature(sw.shadow_record_decision_event).parameters)}",
)

# Fail-open: SQL unreachable -> no raise, event_id still returned.
import os
from unittest.mock import patch

with patch.dict(os.environ, {"YANDI_SQL_SOCKET": "/nonexistent/decision-event-test/mysql.sock"}):
    try:
        returned = sw.shadow_record_decision_event(
            event_type="DecisionStarted", trace_id="run_fail", entity_type="decision",
            entity_id="dec_1", verdict="STARTED", reason="x", domain="general",
        )
        no_raise = True
    except Exception as e:
        no_raise = False
        returned = None

check("C2. shadow_record_decision_event() never raises when SQL is unreachable (fail-open)", no_raise)
check(
    "C3. event_id is still generated and returned even when the write itself failed "
    "(a caller wiring parent_event_id doesn't need to know the write succeeded)",
    isinstance(returned, str) and len(returned) == 32,
    f"{returned!r}",
)

# Spy proof: it actually calls repo.record_decision_event via the same
# get_connection() resolver every other shadow function uses.
_calls = []


def _spy_get_connection(autocommit=False):
    _calls.append(autocommit)
    raise SqlUnavailable("spy: refusing to actually connect")


with patch.object(sw, "get_connection", _spy_get_connection):
    sw.shadow_record_decision_event(
        event_type="DecisionStarted", trace_id="run_spy", entity_type="decision",
        entity_id="dec_1", verdict="STARTED",
    )
check(
    "C4. shadow_record_decision_event() routes through the SAME connection.get_connection() "
    "resolver every other shadow_write function uses — no separate/hardcoded config",
    len(_calls) == 1,
)


# ============================================================
# D. THE ORDERING FIX — static proof shadow_record_question_and_run()
# runs BEFORE the first add_decision_event() call.
# ============================================================
_lines = _orch_src.splitlines()
_run_created_idx = next(i for i, l in enumerate(_lines) if "_sql_question = shadow_record_question_and_run(" in l)
_first_decision_event_idx = next(i for i, l in enumerate(_lines) if l.strip() == "add_decision_event(")
check(
    "D1. THE ORDERING FIX: shadow_record_question_and_run() (creates the "
    "verification_run row decision_event.run_id's FK depends on) now runs BEFORE "
    "the first add_decision_event() call in orchestrator_v2.py",
    _run_created_idx < _first_decision_event_idx,
    f"run_created={_run_created_idx} first_decision_event={_first_decision_event_idx}",
)


# ============================================================
# E. All three production files import from the hardened SQL path, not
# the dead orch_reputation stub.
# ============================================================
check(
    "E1. orchestrator_v2.py imports add_decision_event from the hardened SQL shadow "
    "writer, not agent.orch_reputation's dead stub",
    "from agent.db.sql.shadow_write import shadow_record_decision_event as add_decision_event" in _orch_src
    and "from agent.orch_reputation import add_decision_event" not in _orch_src,
)
check(
    "E2. orchestrator/pipeline.py imports from the hardened SQL shadow writer too",
    "from agent.db.sql.shadow_write import shadow_record_decision_event as add_decision_event" in _pipeline_src
    and "from agent.orch_reputation import add_decision_event" not in _pipeline_src,
)
check(
    "E3. orchestrator/response/writeback.py imports from the hardened SQL shadow "
    "writer too",
    "from agent.db.sql.shadow_write import shadow_record_decision_event as add_decision_event" in _writeback_src
    and "from agent.orch_reputation import add_decision_event" not in _writeback_src,
)


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
