"""
agent/db_sql_recheck_event_shadow_regression_test.py — recheck_event
regression (mandate §16 DoD item "recheck history append-only"), wired
into agent/family_dependency_graph.py::FamilyDependencyGraph.
record_recheck() — the single method all 3 of agent/dependency_
recheck.py's call sites (no_belief / success / error outcomes) go
through.

WHAT THIS FIXES (schema.py's own comment, mandate §16): the OLD
registry/claim_family_graph.json's recheck_log[family_id] OVERWROTE on
every recheck — a family rechecked 3 times only ever showed the LAST
outcome, the other 2 were gone. recheck_event is append-only: one row
per actual attempt, history never lost. Check E below proves this
directly (two rechecks of the SAME family produce TWO rows).

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): recheck_event was reached via a
JSON-primary/SQL-shadow write (shadow_record_recheck_event, agent/db/
sql/shadow_write.py) before this rewrite — registry/claim_family_
graph.json is now retired entirely, and FamilyDependencyGraph.
record_recheck() calls agent.db.sql.repositories.record_recheck_event()
directly through agent.family_dependency_graph.get_connection(), not
through shadow_write.py. This is now the PRIMARY write, fail LOUD (see
check F below — SqlUnavailable propagates, replacing the retired
fail-open contract that made sense only when a JSON file was the real
source of truth).

Covers:
    A. structural: all 3 of dependency_recheck.py's graph.record_recheck(
       calls pass run_id/trigger_reason/started_at/domain/canonical_text;
       family_dependency_graph.py's record_recheck() calls
       repo.record_recheck_event directly (no shadow_write indirection).
    B. functional (FakeConnection, real apply_dependency_recheck() +
       real FamilyDependencyGraph + real BeliefManager, only retrieval/
       NLI mocked): a 'supported' outcome writes a defensive
       get_or_create_claim_family() (FK safety) THEN a recheck_event row
       with the right family_id/run_id/trigger_reason/outcome.
    C. the no_belief early-exit path also writes (outcome='no_belief'),
       not just the retrieval-attempted path.
    D. an exception during retrieval writes outcome='error' with a
       short (not stack-trace-shaped) reason.
    E. APPEND-ONLY proof: two separate rechecks of the SAME family (two
       separate apply_dependency_recheck() calls) produce TWO distinct
       recheck_event rows — the bug this closes, made concrete.
    F. fail LOUD: SQL genuinely unreachable -> SqlUnavailable propagates
       out of apply_dependency_recheck (the deliberate opposite of the
       retired JSON-era fail-open contract).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_recheck_event_shadow_regression_test
"""
from __future__ import annotations

import contextlib
import inspect
from datetime import timedelta
from unittest.mock import patch

import agent.dependency_recheck as dr_mod
from agent.dependency_recheck import apply_dependency_recheck
from agent.family_dependency_graph import FamilyDependencyGraph
import agent.family_dependency_graph as fdg_mod
from agent.belief_manager import BeliefManager
import agent.belief_manager as bm_mod
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


class FakeRegistry:
    def __init__(self, families):
        self.families = families

    def get_family(self, family_id):
        for fam in self.families:
            if fam.get("family_id") == family_id:
                return fam
        return None


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
        norm = " ".join(sql.split())
        upper = norm.upper()
        self.conn.calls.append((norm, params))
        self._result = None
        self._results = None
        conn = self.conn

        if upper.startswith("INSERT IGNORE INTO CLAIM_FAMILY"):
            family_id, domain, canonical_text, created_at, updated_at = params
            conn.families.setdefault(family_id, {
                "family_id": family_id, "domain": domain, "canonical_text": canonical_text,
            })
        elif upper.startswith("SELECT * FROM RECHECK_EVENT WHERE FAMILY_ID=%S ORDER BY STARTED_AT DESC LIMIT 1"):
            (family_id,) = params
            rows = [r for r in conn.rechecks if r["family_id"] == family_id]
            rows.sort(key=lambda r: r["started_at"])
            self._result = dict(rows[-1]) if rows else None
        elif upper.startswith("INSERT INTO RECHECK_EVENT"):
            family_id, run_id, trigger_reason, started_at, outcome, reason = params
            conn.rechecks.append({
                "family_id": family_id, "run_id": run_id, "trigger_reason": trigger_reason,
                "started_at": started_at, "outcome": outcome, "reason": reason,
            })
        elif upper.startswith("INSERT INTO BELIEF "):
            belief_id = params[0]
            conn.beliefs[belief_id] = {
                "belief_id": params[0], "topic": params[1], "statement": params[2],
                "confidence": params[3], "status": params[4],
                "evidence_for": params[5], "evidence_against": params[6], "claim_ids": params[7],
                "prior": params[8], "likelihood": params[9], "contradiction_score": params[10],
                "decay_factor": params[11], "superseded_by": params[12],
                "created_at": params[13], "updated_at": params[14],
            }
        elif upper.startswith("SELECT * FROM BELIEF WHERE BELIEF_ID=%S"):
            (belief_id,) = params
            self._result = dict(conn.beliefs[belief_id]) if belief_id in conn.beliefs else None
        elif upper.startswith("SELECT * FROM BELIEF"):
            self._results = [dict(r) for r in conn.beliefs.values()]

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results or []


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.families = {}
        self.rechecks = []
        self.beliefs = {}

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass


def _fresh_graph(conn):
    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    fdg_mod.get_connection = _fake_get_connection
    return FamilyDependencyGraph()


def _tmp_belief_manager():
    """"Точка ноль" (owner mandate): BeliefManager is SQL-backed
    unconditionally (agent/belief_manager_sql_regression_test.py tests
    THAT directly). A dedicated, always-on fake connection (patched at
    module scope right after the fake classes below are defined) keeps
    every BeliefManager used here isolated from the real live SQL
    instance and from this file's own per-case fake connections."""
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
    "A: FamilyDependencyGraph.record_recheck() calls repo.record_recheck_event directly "
    "(PRIMARY write, no shadow_write indirection — \"точка ноль\" retired the JSON side)",
    "repo.record_recheck_event(" in _fdg_src and "shadow_record_recheck_event" not in _fdg_src,
)

# Belief-manager connection: patched ONCE, for this whole file's duration.
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
conn_b = FakeConnection()
graph_b = _fresh_graph(conn_b)
bm_b = _tmp_belief_manager()
bm_b.add_belief(topic="t", statement="B statement", confidence=0.5, claim_ids=["cl_b1"])
registry_b = FakeRegistry([_family("fam_B", "B statement", "cl_b1", domain="factual")])

dr_mod.retrieve_for_claims = lambda claims, fetch_cache=None: [{"evidence_id": "ev_1", "content_excerpt": "text"}]
dr_mod.classify_relation = lambda main, source: "supports"
stats_b = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_B")]},
    bm_b, cost={}, log=lambda m: None, verbose=False, graph=graph_b, registry=registry_b,
    run_id="run_test_1",
)
_restore()

check("B: recheck succeeded (sanity)", stats_b["rechecks_performed"] == 1, f"{stats_b}")
check(
    "B: defensive get_or_create_claim_family() runs BEFORE the recheck_event insert "
    "(FK safety — same pattern as the retired shadow_record_claim_family)",
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
conn_c = FakeConnection()
graph_c = _fresh_graph(conn_c)
registry_c = FakeRegistry([_family("fam_C", "C statement", "cl_c1")])
stats_c = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_C")]},
    belief_manager=None, cost={}, log=lambda m: None, verbose=False, graph=graph_c, registry=registry_c,
    run_id="run_test_2",
)
check(
    "C: no_belief early-exit path ALSO writes a recheck_event (outcome='no_belief')",
    any(p[0] == "fam_C" and p[4] == "no_belief" for p in _recheck_inserts(conn_c)),
    f"{_recheck_inserts(conn_c)}",
)

# --- D: error path ---
conn_d = FakeConnection()
graph_d = _fresh_graph(conn_d)
bm_d = _tmp_belief_manager()
bm_d.add_belief(topic="t", statement="D statement", confidence=0.5, claim_ids=["cl_d1"])
registry_d = FakeRegistry([_family("fam_D", "D statement", "cl_d1")])

dr_mod.retrieve_for_claims = lambda claims, fetch_cache=None: (_ for _ in ()).throw(RuntimeError("network exploded"))
stats_d = apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_D")]},
    bm_d, cost={}, log=lambda m: None, verbose=False, graph=graph_d, registry=registry_d,
    run_id="run_test_3",
)
_restore()

check(
    "D: an exception during retrieval writes outcome='error' with a short, "
    "non-stack-trace reason (the exception's own message, truncated)",
    any(
        p[0] == "fam_D" and p[4] == "error" and p[5] == "network exploded"
        for p in _recheck_inserts(conn_d)
    ),
    f"{_recheck_inserts(conn_d)}",
)

# --- E: APPEND-ONLY proof — two separate rechecks of the SAME family
# produce TWO rows, not one overwritten (the bug this closes).
conn_e = FakeConnection()
graph_e = _fresh_graph(conn_e)
bm_e = _tmp_belief_manager()
bm_e.add_belief(topic="t", statement="E statement", confidence=0.5, claim_ids=["cl_e1"])
registry_e = FakeRegistry([_family("fam_E", "E statement", "cl_e1")])

dr_mod.retrieve_for_claims = lambda claims, fetch_cache=None: [{"evidence_id": "ev_1", "content_excerpt": "text"}]
dr_mod.classify_relation = lambda main, source: "supports"
apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_E")]},
    bm_e, cost={}, log=lambda m: None, verbose=False, graph=graph_e, registry=registry_e, run_id="run_e1",
)

dr_mod.classify_relation = lambda main, source: "contradicts"
# Bypass the (real, correct) per-family cooldown so this test can exercise
# a SECOND recheck attempt within the same test run — RECHECK_COOLDOWN_
# SECONDS (3600s) would otherwise legitimately skip it. Directly rewind
# the fake connection's own stored recheck_event row rather than
# poking a (now nonexistent) `graph.recheck_log` dict.
conn_e.rechecks[-1]["started_at"] -= timedelta(seconds=dr_mod.RECHECK_COOLDOWN_SECONDS + 1)
apply_dependency_recheck(
    {"recheck_candidate_details": [_candidate("fam_A", "fam_E")]},
    bm_e, cost={}, log=lambda m: None, verbose=False, graph=graph_e, registry=registry_e, run_id="run_e2",
)
_restore()

check(
    "E: first recheck of fam_E wrote its own recheck_event row (outcome=supported)",
    any(p[0] == "fam_E" and p[4] == "supported" and p[1] == "run_e1" for p in _recheck_inserts(conn_e)),
)
check(
    "E APPEND-ONLY: the SECOND recheck of the SAME family_id issues its OWN new "
    "INSERT (outcome=contradicted, run_e2) — NOT an UPDATE/overwrite of the first row "
    "(this is exactly the confirmed bug the retired JSON recheck_log had, fixed here)",
    any(p[0] == "fam_E" and p[4] == "contradicted" and p[1] == "run_e2" for p in _recheck_inserts(conn_e)),
)
check(
    "E: both rows are genuinely present in recheck_event — two distinct INSERTs, "
    "not one row mutated in place",
    len([r for r in conn_e.rechecks if r["family_id"] == "fam_E"]) == 2,
    f"{conn_e.rechecks}",
)


# ============================================================
# F. Fail LOUD, not fail-open: SQL genuinely unreachable raises
# SqlUnavailable out of apply_dependency_recheck — the deliberate
# opposite of the retired JSON-era fail-open contract (there is no more
# file-based fallback for this module to quietly succeed against).
# ============================================================

conn_f = FakeConnection()
graph_f = _fresh_graph(conn_f)
bm_f = _tmp_belief_manager()
bm_f.add_belief(topic="t", statement="F statement", confidence=0.5, claim_ids=["cl_f1"])
registry_f = FakeRegistry([_family("fam_F", "F statement", "cl_f1")])

dr_mod.retrieve_for_claims = lambda claims, fetch_cache=None: [{"evidence_id": "ev_1", "content_excerpt": "text"}]
dr_mod.classify_relation = lambda main, source: "supports"


def _raise_unavailable(autocommit=False):
    raise SqlUnavailable("forced unreachable for this test")


with patch.object(fdg_mod, "get_connection", _raise_unavailable):
    raised = False
    try:
        apply_dependency_recheck(
            {"recheck_candidate_details": [_candidate("fam_A", "fam_F")]},
            bm_f, cost={}, log=lambda m: None, verbose=False, graph=graph_f, registry=registry_f, run_id="run_f",
        )
    except SqlUnavailable:
        raised = True
_restore()

check(
    "F: with SQL genuinely unreachable, apply_dependency_recheck() raises SqlUnavailable — "
    "no more JSON-side fallback to quietly succeed against",
    raised,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
