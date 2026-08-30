"""
agent/db_sql_claims_evidence_shadow_regression_test.py — Этап 5 (SQL
persistence migration) regression: claim/evidence bulk shadow write
(agent.db.sql.shadow_write.shadow_record_claims_and_evidence), wired
into agent/orchestrator/claims/status.py::
finalize_claim_trace_and_grounding() at the same SAVE point as the
existing JSON persist_verification_evidence() call.

Covers:
    A. structural: the shadow call appears in status.py's real
       production source, inside the same `if evidence_data is not
       None:` block, after the JSON persist call.
    B. resource_type/observation_route mapping (the §6 correction)
       against REAL runtime evidence-dict shapes: plain internet
       evidence; a local_memory replay of an ORIGINALLY-internet
       observation (resource_type must resolve to 'internet', not
       'local_memory'); a local_memory replay of a non-internet origin
       (must be SKIPPED — V1 has no canonical identity for network_
       node/ai_chat/local_model resources yet); evidence with no
       source_uri (skipped, never a fabricated identity); an
       out-of-vocabulary relation value (skipped, never crashes).
    C. fail-open with no DB configured (this environment's real state).
    D. no mutation of claims_data/evidence_data.
    E. functional: finalize_claim_trace_and_grounding()'s own return
       value and trace mutations are unaffected by the SQL wiring sitting
       next to the existing JSON persist call (same technique as the
       4G-4 Phase-11/Phase-12 non-interference proof this session
       already established).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_claims_evidence_shadow_regression_test
"""
from __future__ import annotations

import copy
import inspect
import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.orchestrator.claims.status as status_mod
import agent.db.sql.shadow_write as sw
import agent.db.sql.connection as sqlconn
import agent.orch_tracer as ot

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
# A. STRUCTURAL: real call-graph position in status.py.
# ============================================================

_src = inspect.getsource(status_mod)
_pos_json_persist = _src.find("persist_verification_evidence(trace, claims_data, evidence_data")
_pos_shadow = _src.find("shadow_record_claims_and_evidence(")
_pos_if_block = _src.find("if evidence_data is not None:")

check(
    "A: shadow_record_claims_and_evidence is called AFTER the JSON persist_verification_evidence "
    "call, inside the same 'if evidence_data is not None' block",
    -1 < _pos_if_block < _pos_json_persist < _pos_shadow,
    f"if_block={_pos_if_block} json={_pos_json_persist} shadow={_pos_shadow}",
)

# ============================================================
# B. resource_type/observation_route mapping — FakeConnection harness.
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
            self._result = None  # nothing pre-exists -> always "create new"
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
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


import contextlib


def _run_shadow(claims_data, evidence_data):
    conn = FakeConnection()

    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    with patch.object(sw, "get_connection", _fake_get_connection):
        sw.shadow_record_claims_and_evidence(
            run_id="run_1", claims_data=claims_data, evidence_data=evidence_data,
            log=_noop_log, verbose=True,
        )
    return conn


def _sql_contains(conn, *fragments):
    return any(all(f in sql for f in fragments) for sql, _p in conn.calls)


claims_b1 = [{
    "claim_id": "cl_1", "claim_text": "claim text", "content_hash": "h1",
    "claim_type": "factual", "claim_confidence": 0.6, "verification_status": "supported",
    "semantic_family_id": "fam_1", "query_context": "q", "support_count": 1, "contradiction_count": 0,
    "evidence_relations": [{"evidence_id": "ev_1", "relation": "supports", "directness": 0.8,
                             "evidence_eligible": True, "evidence_role": "direct", "counted_via": "authority"}],
}]
evidence_b1 = [{"evidence_id": "ev_1", "route": "internet", "source_uri": "https://x.example/a",
                "source_class": "reference", "quality_score": 0.8, "content_excerpt": "excerpt", "observed_at": 0.0}]
conn_b1 = _run_shadow(claims_b1, evidence_b1)
check(
    "B: plain internet evidence -> resource_type='internet', observation_route='internet'",
    any("INSERT INTO source_resource" in s and "internet" in (p or ()) for s, p in conn_b1.calls)
    and any("INSERT INTO source_observation" in s and "internet" in (p or ()) for s, p in conn_b1.calls),
    f"{conn_b1.calls}",
)
check("B: claim_occurrence inserted", _sql_contains(conn_b1, "INSERT INTO claim_occurrence"), f"{conn_b1.calls}")
check("B: evidence_relation inserted", _sql_contains(conn_b1, "INSERT INTO evidence_relation"), f"{conn_b1.calls}")

claims_b2 = [{"claim_id": "cl_2", "claim_text": "x", "evidence_relations": [
    {"evidence_id": "ev_2", "relation": "supports", "evidence_eligible": True},
]}]
evidence_b2 = [{"evidence_id": "ev_2", "route": "local_memory", "origin_route": "internet",
                "source_uri": "https://x.example/replayed", "observed_at": 0.0}]
conn_b2 = _run_shadow(claims_b2, evidence_b2)
check(
    "B: local_memory replay of an ORIGINALLY-internet observation resolves resource_type="
    "'internet' (from origin_route), NOT 'local_memory' — the §6 correction, applied to real data",
    any("INSERT INTO source_resource" in s and "internet" in (p or ()) for s, p in conn_b2.calls),
    f"{conn_b2.calls}",
)
check(
    "B: the SAME replay observation's observation_route is 'local_memory' (the retrieval "
    "channel THIS time), distinct from resource_type",
    any("INSERT INTO source_observation" in s and "local_memory" in (p or ()) for s, p in conn_b2.calls),
    f"{conn_b2.calls}",
)

claims_b3 = [{"claim_id": "cl_3", "claim_text": "x", "evidence_relations": [
    {"evidence_id": "ev_3", "relation": "supports", "evidence_eligible": True},
]}]
evidence_b3 = [{"evidence_id": "ev_3", "route": "local_memory", "origin_route": "network_node",
                "source_uri": None, "observed_at": 0.0}]
conn_b3 = _run_shadow(claims_b3, evidence_b3)
check(
    "B: a replay of a NON-internet origin (network_node) is SKIPPED entirely — V1 has no "
    "canonical identity for network_node/ai_chat/local_model resources yet, never fabricated",
    not _sql_contains(conn_b3, "INSERT INTO source_resource")
    and not _sql_contains(conn_b3, "INSERT INTO source_observation"),
    f"{conn_b3.calls}",
)
check(
    "B: the claim itself is still recorded even when its only evidence is skipped "
    "(claim_occurrence is not gated by evidence)",
    _sql_contains(conn_b3, "INSERT INTO claim_occurrence"),
    f"{conn_b3.calls}",
)

claims_b4 = [{"claim_id": "cl_4", "claim_text": "x", "evidence_relations": [
    {"evidence_id": "ev_4", "relation": "supports", "evidence_eligible": True},
]}]
evidence_b4 = [{"evidence_id": "ev_4", "route": "internet", "source_uri": None, "observed_at": 0.0}]
conn_b4 = _run_shadow(claims_b4, evidence_b4)
check(
    "B: internet-route evidence with NO source_uri is skipped, never a fabricated identity",
    not _sql_contains(conn_b4, "INSERT INTO source_resource"),
    f"{conn_b4.calls}",
)

claims_b5 = [{"claim_id": "cl_5", "claim_text": "x", "evidence_relations": [
    {"evidence_id": "ev_5", "relation": "some_garbage_value", "evidence_eligible": True},
]}]
evidence_b5 = [{"evidence_id": "ev_5", "route": "internet", "source_uri": "https://x.example/z", "observed_at": 0.0}]
conn_b5 = _run_shadow(claims_b5, evidence_b5)
check(
    "B: an out-of-vocabulary relation value is skipped (source_observation may still be "
    "created, but no evidence_relation row for garbage input)",
    not _sql_contains(conn_b5, "INSERT INTO evidence_relation"),
    f"{conn_b5.calls}",
)

# ============================================================
# C. Fail-open with SQL endpoint genuinely unreachable (forced,
# deterministic — DATABASE BOOTSTRAP V1's canonical defaults mean
# is_configured() is True out of the box now; forcing unreachability
# also prevents this suite from ever writing test rows into a real
# live dedicated DB on a host where one happens to be reachable).
# Stopped at the very end of the file (also covers D/E below, which
# would otherwise attempt real shadow writes too).
# ============================================================

_forced_unreachable = patch.dict(
    "os.environ", {"YANDI_SQL_SOCKET": "/nonexistent/claims-evidence-shadow-test/mysql.sock"},
)
_forced_unreachable.start()

check(
    "C precondition: SQL layer resolves canonical defaults (is_configured()=True) but "
    "the forced socket path is genuinely unreachable",
    sqlconn.is_configured() is True,
)
try:
    sw.shadow_record_claims_and_evidence(
        run_id="run_x", claims_data=claims_b1, evidence_data=evidence_b1, log=_noop_log, verbose=True,
    )
    no_raise = True
except Exception as e:
    no_raise = False
check("C: shadow_record_claims_and_evidence never raises with SQL endpoint unreachable", no_raise)

# ============================================================
# D. No mutation of caller data.
# ============================================================

claims_before = copy.deepcopy(claims_b1)
evidence_before = copy.deepcopy(evidence_b1)
_run_shadow(claims_b1, evidence_b1)
check(
    "D: claims_data/evidence_data are unchanged after the shadow call (no in-place mutation)",
    claims_b1 == claims_before and evidence_b1 == evidence_before,
    f"claims before={claims_before} after={claims_b1}",
)

# ============================================================
# E. finalize_claim_trace_and_grounding()'s own behavior is unaffected
# by the SQL wiring sitting next to its existing JSON persist call
# (real function, real Trace, SQL genuinely unconfigured).
# ============================================================

traces_dir = Path(tempfile.mkdtemp(prefix="p10_claimshadow_"))
with patch.object(ot, "TRACES_DIR", traces_dir):
    trace = ot.Trace(trace_id="t_finalize", timestamp=0.0, query="q")
    claims_e = [{
        "claim_id": "cl_e1", "claim_text": "У Юпитера известно 95 спутников на данный момент.",
        "content_hash": "he1", "claim_type": "factual", "claim_confidence": 0.6,
        "verification_status": "supported", "evidence_relations": [
            {"evidence_id": "ev_e1", "relation": "supports", "evidence_eligible": True, "evidence_role": "direct"},
        ],
    }]
    evidence_e = [{"evidence_id": "ev_e1", "route": "internet", "source_uri": "https://x.example/e1",
                    "content_excerpt": "excerpt", "observed_at": 0.0}]

    result = status_mod.finalize_claim_trace_and_grounding(
        claims_e, trace, [], 0.5, _noop_log, False, evidence_data=evidence_e,
    )

check(
    "E: finalize_claim_trace_and_grounding() still returns its normal "
    "(epistemic_grounding, support_grounding) tuple with the SQL wiring present",
    isinstance(result, tuple) and len(result) == 2,
    f"{result}",
)
check(
    "E: the claim is still added to the trace (JSON path unaffected by the SQL wiring)",
    len(trace.claims) == 1 and trace.claims[0].claim_id == "cl_e1",
    f"{[c.claim_id for c in trace.claims]}",
)

_forced_unreachable.stop()

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
