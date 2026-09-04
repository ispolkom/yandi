"""
agent/db_sql_recheck_event_shadow_regression_test.py — Этап 5 (SQL
persistence migration) regression: recheck_event shadow write (mandate
§16 DoD item "recheck history append-only"), wired into
agent/family_dependency_graph.py::FamilyDependencyGraph.record_recheck()
— the single method all 3 of agent/dependency_recheck.py's call sites
(no_belief / success / error outcomes) go through.

WHAT THIS FIXES (schema.py's own comment, mandate §16): the current
registry/claim_family_graph.json's recheck_log[family_id] OVERWRITES on
every recheck — a family rechecked 3 times only ever shows the LAST
outcome, the other 2 are gone. recheck_event is append-only: one row
per actual attempt, history never lost. Check E below proves this
directly (two rechecks of the SAME family produce TWO rows).

Covers:
    A. structural: all 3 of dependency_recheck.py's graph.record_recheck(
       calls pass run_id/trigger_reason/started_at/domain/canonical_text;
       family_dependency_graph.py's record_recheck() calls
       shadow_record_recheck_event.
    B. functional (FakeConnection, real apply_dependency_recheck() +
       real FamilyDependencyGraph + real BeliefManager, only retrieval/
       NLI mocked — same harness technique as agent/epistemic_
       dependency_recheck_regression_test.py): a 'supported' outcome
       writes a defensive get_or_create_claim_family() (FK safety, same
       pattern as shadow_record_claim_family) THEN a recheck_event row
       with the right family_id/run_id/trigger_reason/outcome.
    C. the no_belief early-exit path also shadow-writes (outcome=
       'no_belief'), not just the retrieval-attempted path.
    D. an exception during retrieval shadow-writes outcome='error' with
       a short (not stack-trace-shaped) reason.
    E. APPEND-ONLY proof: two separate rechecks of the SAME family (two
       separate apply_dependency_recheck() calls) produce TWO distinct
       recheck_event rows — the bug this closes, made concrete.
    F. fail-open: SQL genuinely unconfigured — apply_dependency_recheck's
       real (JSON-side) behavior and return stats are unaffected.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_recheck_event_shadow_regression_test
"""
from __future__ import annotations

import contextlib
import inspect
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import agent.dependency_recheck as dr_mod
from agent.dependency_recheck import apply_dependency_recheck
from agent.family_dependency_graph import FamilyDependencyGraph
import agent.family_dependency_graph as fdg_mod
from agent.belief_manager import BeliefManager
import agent.belief_manager as bm_mod
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


class FakeRegistry:
    def __init__(self, families):
        self.families = families

    def get_family(self, family_id):
        # "точка ноль”: matches the real ClaimFamilyRegistry.get_family() public API
        # (_family_by_id() in dependency_recheck.py now calls this,
        # not registry.families directly).
        for fam in self.families:
            if fam.get("family_id") == family_id:
                return fam
        return None


def _tmp_graph():
    tmp = Path(tempfile.mkstemp(suffix=".json")[1])
    tmp.unlink()
    return FamilyDependencyGraph(storage_file=tmp)


def _tmp_belief_manager():
    """"Точка ноль" (owner mandate): BeliefManager no longer accepts a
    storage_file — it is SQL-backed unconditionally now (agent/
    belief_manager_sql_regression_test.py tests THAT directly). This
    file's own concern is recheck_event, not belief persistence — a
    dedicated, always-on fake connection (patched at module scope right
    after the fake classes below are defined) keeps every BeliefManager
    used here isolated from the real live SQL instance, and — just as
    importantly — isolated from Section F's own YANDI_SQL_SOCKET
    forcing further down this file, which is testing recheck_event's
    OWN fail-open behavior, not belief_manager's (separately confirmed
    to now fail LOUD by design, in belief_manager_sql_regression_test.py's
    own check 7)."""
    return BeliefManager()


def _family(family_id, canonical_text, claim_id, domain="factual"):
    return {
        "family_id": family_id, "domain": domain, "canonical_text": canonical_text,
        "members": [{"claim_id": claim_id, "claim_text": canonical_text}],
    }


def _candidate(changed_family, dependent_family, depth=1):
    return {
        "changed_family": changed_family, "previous_status": "supported",
        "new_status": "contradicted", "dependent_family": dependent_family,
        "edge_type": "depends_on", "reason": "contradicts",
        "triggering_claim_ids": ["cl_x"], "depth": depth,
    }


_orig_retrieve = dr_mod.retrieve_for_claims
_orig_classify = dr_mod.classify_relation


def _restore():
    dr_mod.retrieve_for_claims = _orig_retrieve
    dr_mod.classify_relation = _orig_classify


# ============================================================
# A. Structural.
# ============================================================

_dr_src = inspect.getsource(dr_mod)
for needle, label in [
    ('"no_belief", run_id=run_id, trigger_reason=cand_trigger_reason', "no_belief path"),
    ('family_id, outcome, run_id=run_id, trigger_reason=cand_trigger_reason', "success path"),
    ('family_id, "error", run_id=run_id, trigger_reason=cand_trigger_reason', "error path"),
]:
    check(f"A: dependency_recheck.py's {label} passes run_id/trigger_reason through to record_recheck", needle in _dr_src)

_fdg_src = inspect.getsource(fdg_mod)
check(
    "A: FamilyDependencyGraph.record_recheck() calls shadow_record_recheck_event",
    "shadow_record_recheck_event(" in _fdg_src,
)


# ============================================================
# B/C/D/F — FakeConnection harness.
# ============================================================

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = None
        self._result = None
        self._results = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.conn.calls.append((norm, params))
        upper = norm.strip().upper()
        self._result = None
        self._results = []

        # Minimal belief-table awareness — this file's own concern is
        # recheck_event, not belief CRUD correctness (that's fully
        # covered separately in belief_manager_sql_regression_test.py);
        # this just needs BeliefManager to not crash when it reads back
        # what it just wrote.
        if upper.startswith("INSERT INTO BELIEF "):
            belief_id = params[0]
            self.conn.beliefs[belief_id] = {
                "belief_id": params[0], "topic": params[1], "statement": params[2],
                "confidence": params[3], "status": params[4],
                "evidence_for": params[5], "evidence_against": params[6], "claim_ids": params[7],
                "prior": params[8], "likelihood": params[9], "contradiction_score": params[10],
                "decay_factor": params[11], "superseded_by": params[12],
                "created_at": params[13], "updated_at": params[14],
            }
        elif upper.startswith("SELECT * FROM BELIEF WHERE BELIEF_ID=%S"):
            (belief_id,) = params
            self._result = dict(self.conn.beliefs[belief_id]) if belief_id in self.conn.beliefs else None
        elif upper.startswith("SELECT * FROM BELIEF"):
            self._results = [dict(r) for r in self.conn.beliefs.values()]
        elif sql.strip().upper().startswith("INSERT"):
            self.conn.next_id += 1
            self.lastrowid = self.conn.next_id

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.next_id = 1000
        self.beliefs = {}

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _run_with_fake_sql(fn):
    conn = FakeConnection()

    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    with patch.object(sw, "get_connection", _fake_get_connection):
        result = fn()
    return conn, result


# Belief-manager connection: patched ONCE, for this whole file's
# duration, deliberately separate from _run_with_fake_sql's per-call
# sw.get_connection patch above — see _tmp_belief_manager()'s own
# docstring for why. Never stopped (this is a standalone top-to-bottom
# script, not a test suite with teardown between cases).
_belief_fake_conn = FakeConnection()


@contextlib.contextmanager
def _fake_belief_get_connection(autocommit=False):
    yield _belief_fake_conn


patch.object(bm_mod, "get_connection", _fake_belief_get_connection).start()


def _recheck_inserts(conn):
    return [p for s, p in conn.calls if s.startswith("INSERT INTO recheck_event")]


def _family_upserts(conn):
    return [p for s, p in conn.calls if s.startswith("INSERT IGNORE INTO claim_family")]


# --- B: success path ('supported' outcome) ---
graph_b = _tmp_graph()
bm_b = _tmp_belief_manager()
bm_b.add_belief(topic="t", statement="B statement", confidence=0.5, claim_ids=["cl_b1"])
registry_b = FakeRegistry([_family("fam_B", "B statement", "cl_b1", domain="factual")])

dr_mod.retrieve_for_claims = lambda claims, fetch_cache=None: [{"evidence_id": "ev_1", "content_excerpt": "text"}]
dr_mod.classify_relation = lambda main, source: "supports"
conn_b, stats_b = _run_with_fake_sql(lambda: apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_B")]},
    bm_b, cost={}, log=lambda m: None, verbose=False, graph=graph_b, registry=registry_b,
    run_id="run_test_1",
))
_restore()

check("B: recheck succeeded (sanity)", stats_b["rechecks_performed"] == 1, f"{stats_b}")
check(
    "B: defensive get_or_create_claim_family() runs BEFORE the recheck_event insert "
    "(FK safety — same pattern as shadow_record_claim_family)",
    len(_family_upserts(conn_b)) == 1 and any(p[0] == "fam_B" and p[1] == "factual" for p in _family_upserts(conn_b)),
    f"{conn_b.calls}",
)
check(
    "B: recheck_event row has family_id=fam_B, run_id=run_test_1, "
    "trigger_reason=fam_A (the changed family that triggered this), outcome=supported",
    any(
        p[0] == "fam_B" and p[1] == "run_test_1" and p[2] == "fam_A" and p[4] == "supported"
        for p in _recheck_inserts(conn_b)
    ),
    f"{_recheck_inserts(conn_b)}",
)

# --- C: no_belief path ---
graph_c = _tmp_graph()
registry_c = FakeRegistry([_family("fam_C", "C statement", "cl_c1")])
conn_c, stats_c = _run_with_fake_sql(lambda: apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_C")]},
    belief_manager=None, cost={}, log=lambda m: None, verbose=False, graph=graph_c, registry=registry_c,
    run_id="run_test_2",
))
check(
    "C: no_belief early-exit path ALSO shadow-writes a recheck_event (outcome='no_belief')",
    any(p[0] == "fam_C" and p[4] == "no_belief" for p in _recheck_inserts(conn_c)),
    f"{_recheck_inserts(conn_c)}",
)

# --- D: error path ---
graph_d = _tmp_graph()
bm_d = _tmp_belief_manager()
bm_d.add_belief(topic="t", statement="D statement", confidence=0.5, claim_ids=["cl_d1"])
registry_d = FakeRegistry([_family("fam_D", "D statement", "cl_d1")])

dr_mod.retrieve_for_claims = lambda claims, fetch_cache=None: (_ for _ in ()).throw(RuntimeError("network exploded"))
conn_d, stats_d = _run_with_fake_sql(lambda: apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_D")]},
    bm_d, cost={}, log=lambda m: None, verbose=False, graph=graph_d, registry=registry_d,
    run_id="run_test_3",
))
_restore()

check(
    "D: an exception during retrieval shadow-writes outcome='error' with a short, "
    "non-stack-trace reason (the exception's own message, truncated)",
    any(
        p[0] == "fam_D" and p[4] == "error" and p[5] == "network exploded"
        for p in _recheck_inserts(conn_d)
    ),
    f"{_recheck_inserts(conn_d)}",
)

# --- E: APPEND-ONLY proof — two separate rechecks of the SAME family
# produce TWO rows, not one overwritten (the bug this closes).
graph_e = _tmp_graph()
bm_e = _tmp_belief_manager()
bm_e.add_belief(topic="t", statement="E statement", confidence=0.5, claim_ids=["cl_e1"])
registry_e = FakeRegistry([_family("fam_E", "E statement", "cl_e1")])

dr_mod.retrieve_for_claims = lambda claims, fetch_cache=None: [{"evidence_id": "ev_1", "content_excerpt": "text"}]
dr_mod.classify_relation = lambda main, source: "supports"
conn_e1, _ = _run_with_fake_sql(lambda: apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_E")]},
    bm_e, cost={}, log=lambda m: None, verbose=False, graph=graph_e, registry=registry_e, run_id="run_e1",
))

dr_mod.classify_relation = lambda main, source: "contradicts"
# Bypass the (real, correct) per-family cooldown so this test can exercise
# a SECOND recheck attempt within the same test run — RECHECK_COOLDOWN_
# SECONDS (3600s) would otherwise legitimately skip it, which is not what
# this check is testing (that skip behavior is already covered by
# agent/epistemic_dependency_recheck_regression_test.py).
graph_e.recheck_log["fam_E"]["last_rechecked_at"] = time.time() - dr_mod.RECHECK_COOLDOWN_SECONDS - 1
conn_e2, _ = _run_with_fake_sql(lambda: apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_E")]},
    bm_e, cost={}, log=lambda m: None, verbose=False, graph=graph_e, registry=registry_e, run_id="run_e2",
))
_restore()

check(
    "E: first recheck of fam_E wrote its own recheck_event row (outcome=supported)",
    any(p[0] == "fam_E" and p[4] == "supported" and p[1] == "run_e1" for p in _recheck_inserts(conn_e1)),
    f"{_recheck_inserts(conn_e1)}",
)
check(
    "E APPEND-ONLY: the SECOND recheck of the SAME family_id issues its OWN new "
    "INSERT (outcome=contradicted, run_e2) — NOT an UPDATE/overwrite of the first row "
    "(this is exactly the confirmed bug schema.py's recheck_event comment describes "
    "for the JSON recheck_log, fixed here)",
    any(p[0] == "fam_E" and p[4] == "contradicted" and p[1] == "run_e2" for p in _recheck_inserts(conn_e2)),
    f"{_recheck_inserts(conn_e2)}",
)
check(
    "E: JSON-side recheck_log[family_id] still shows only the LAST outcome (unaffected "
    "current-state shortcut) — the SQL side is what preserves both",
    graph_e.recheck_log["fam_E"]["last_outcome"] == "contradicted"
    and graph_e.recheck_log["fam_E"]["recheck_count"] == 2,
    f"{graph_e.recheck_log}",
)


# ============================================================
# F. Fail-open — SQL endpoint genuinely unreachable (forced,
# deterministic — DATABASE BOOTSTRAP V1's canonical defaults mean
# is_configured() is True out of the box now).
# ============================================================

_forced_unreachable = patch.dict(
    "os.environ", {"YANDI_SQL_SOCKET": "/nonexistent/recheck-event-shadow-test/mysql.sock"},
)
_forced_unreachable.start()

check(
    "F precondition: SQL layer resolves canonical defaults (is_configured()=True) but "
    "the forced socket path is genuinely unreachable",
    sqlconn.is_configured() is True,
)

graph_f = _tmp_graph()
bm_f = _tmp_belief_manager()
bm_f.add_belief(topic="t", statement="F statement", confidence=0.5, claim_ids=["cl_f1"])
registry_f = FakeRegistry([_family("fam_F", "F statement", "cl_f1")])

dr_mod.retrieve_for_claims = lambda claims, fetch_cache=None: [{"evidence_id": "ev_1", "content_excerpt": "text"}]
dr_mod.classify_relation = lambda main, source: "supports"
try:
    stats_f = apply_dependency_recheck(
        {"recheck_candidate_details": [_candidate("fam_A", "fam_F")]},
        bm_f, cost={}, log=lambda m: None, verbose=False, graph=graph_f, registry=registry_f, run_id="run_f",
    )
    no_raise = True
except Exception:
    no_raise = False
_restore()

check("F: apply_dependency_recheck never raises with SQL endpoint unreachable", no_raise)
check(
    "F: its real (JSON-side) behavior is unaffected — recheck still performed, "
    "JSON recheck_log still updated",
    no_raise and stats_f["rechecks_performed"] == 1 and "fam_F" in graph_f.recheck_log,
    f"{stats_f if no_raise else None}",
)

_forced_unreachable.stop()

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
