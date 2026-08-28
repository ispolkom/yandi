"""
agent/db_sql_claim_family_shadow_regression_test.py — Этап 5 (SQL
persistence migration) regression: claim_family/family_member shadow
write (agent.db.sql.shadow_write.shadow_record_claim_family), wired
into agent/orchestrator/claims/lifecycle.py::
assign_claim_family_identity(), the exact point
agent.claim_family_registry.ClaimFamilyRegistry.find_or_link_claim()
already runs — deliberately NOT part of the bulk
shadow_record_claims_and_evidence() path (MIGRATION_STATUS.md's §41
gap: that bulk path has no canonical_text to write claim_family with).

Why this matters beyond "another table gets written": claim_occurrence.
family_id (schema.py) carries a FOREIGN KEY to claim_family(family_id).
shadow_record_claims_and_evidence() inserts claim_occurrence rows with
family_id set whenever a claim has one. On a real live DB, without
THIS function running first in the same run, every such insert would
hit an FK violation — fail-open would swallow it silently, meaning NO
claim occurrence would persist for any claim that got a family. This
suite proves the fix, not just that "a new table gets written".

Covers:
    A. structural: shadow_record_claim_family( is called inside
       assign_claim_family_identity() (not elsewhere), after
       claim["semantic_family_id"] = family_id is set.
    B. functional (FakeConnection): claim_family + family_member both
       written, INSERT IGNORE semantics, correct params.
    C. canonical_text is the FAMILY's canonical text (first member's
       wording), not the new claim's own text, when a second claim
       joins an existing family.
    D. fail-open: SQL genuinely unconfigured in this environment —
       assign_claim_family_identity() does not raise, semantic_family_id
       assignment still works normally (JSON path unaffected).
    E. no unexpected mutation of claims_data beyond semantic_family_id.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_claim_family_shadow_regression_test
"""
from __future__ import annotations

import contextlib
import inspect
import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.orchestrator.claims.lifecycle as lifecycle_mod
import agent.db.sql.shadow_write as sw
import agent.db.sql.connection as sqlconn
from agent.claim_family_registry import ClaimFamilyRegistry

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


class _FakeEpistemicResult:
    domain = "factual"


def _isolated_registry():
    path = Path(tempfile.mkdtemp(prefix="p5_famshadow_")) / "families.json"
    return ClaimFamilyRegistry(storage_file=path)


# ============================================================
# A. STRUCTURAL: call site lives inside assign_claim_family_identity(),
# after the semantic_family_id assignment — not in the bulk path.
# ============================================================

_src = inspect.getsource(lifecycle_mod)
_fn_start = _src.find("def assign_claim_family_identity(")
_fn_end = _src.find("def update_beliefs_link_answer_and_personality_cycle(")
_fn_body = _src[_fn_start:_fn_end]

_assign_pos = _fn_body.find('claim["semantic_family_id"] = family_id')
_shadow_pos = _fn_body.find("shadow_record_claim_family(")

check(
    "A: shadow_record_claim_family( is called inside assign_claim_family_identity(), "
    "AFTER claim['semantic_family_id'] = family_id is set",
    -1 < _assign_pos < _shadow_pos,
    f"assign_pos={_assign_pos} shadow_pos={_shadow_pos}",
)


# ============================================================
# B. Functional — FakeConnection harness (same technique as
# db_sql_claims_evidence_shadow_regression_test.py).
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

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _sql_contains(conn, *fragments):
    return any(all(f in sql for f in fragments) for sql, _p in conn.calls)


def _run_with_fake_sql(fn):
    conn = FakeConnection()

    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    with patch.object(sw, "get_connection", _fake_get_connection):
        fn()
    return conn


registry_b = _isolated_registry()
claims_b = [{
    "claim_id": "cl_fs_1",
    "claim_text": "У Юпитера известно 95 спутников на данный момент.",
}]

with patch.object(lifecycle_mod, "get_claim_family_registry", lambda: registry_b):
    conn_b = _run_with_fake_sql(lambda: lifecycle_mod.assign_claim_family_identity(
        claims_b, _FakeEpistemicResult(), False, {}, _noop_log, False,
    ))

check(
    "B: claim_family row written (INSERT IGNORE, write-once semantics)",
    _sql_contains(conn_b, "INSERT IGNORE INTO claim_family"),
    f"{conn_b.calls}",
)
check(
    "B: family_member row written (INSERT IGNORE, idempotent link)",
    _sql_contains(conn_b, "INSERT IGNORE INTO family_member"),
    f"{conn_b.calls}",
)
_family_id_b = claims_b[0]["semantic_family_id"]
check(
    "B: claim_family params carry this run's family_id + domain + claim's own text "
    "(first member -> canonical_text == claim_text)",
    any(
        "INSERT IGNORE INTO claim_family" in s and p[0] == _family_id_b
        and p[1] == "factual" and p[2] == claims_b[0]["claim_text"]
        for s, p in conn_b.calls
    ),
    f"{conn_b.calls}",
)
check(
    "B: family_member params carry (family_id, claim_id)",
    any(
        "INSERT IGNORE INTO family_member" in s and p[0] == _family_id_b and p[1] == "cl_fs_1"
        for s, p in conn_b.calls
    ),
    f"{conn_b.calls}",
)


# ============================================================
# C. canonical_text is the FAMILY's text, not the new joining claim's
# own (different-but-equivalent) text.
# ============================================================

registry_c = _isolated_registry()
claim_c1 = [{"claim_id": "cl_fs_c1", "claim_text": "У Юпитера известно 95 спутников."}]
with patch.object(lifecycle_mod, "get_claim_family_registry", lambda: registry_c):
    lifecycle_mod.assign_claim_family_identity(
        claim_c1, _FakeEpistemicResult(), False, {}, _noop_log, False,
    )
family_id_c = claim_c1[0]["semantic_family_id"]

# Second claim: byte-different but normalizes identically (whitespace/case
# only) -> hits the EXACT fast path, no embedding/LLM call needed, joins
# the SAME family without creating a new one.
claim_c2 = [{"claim_id": "cl_fs_c2", "claim_text": "  У юпитера известно 95 спутников.  "}]
with patch.object(lifecycle_mod, "get_claim_family_registry", lambda: registry_c):
    conn_c2 = _run_with_fake_sql(lambda: lifecycle_mod.assign_claim_family_identity(
        claim_c2, _FakeEpistemicResult(), False, {}, _noop_log, False,
    ))

check(
    "C precondition: second claim joined the SAME existing family (exact fast path), "
    "not a new one",
    claim_c2[0]["semantic_family_id"] == family_id_c,
    f"family_c1={family_id_c} family_c2={claim_c2[0]['semantic_family_id']}",
)
check(
    "C: shadow write for the SECOND claim uses the FAMILY's canonical_text "
    "(first member's wording), not the second claim's own differently-formatted text",
    any(
        "INSERT IGNORE INTO claim_family" in s and p[2] == claim_c1[0]["claim_text"]
        for s, p in conn_c2.calls
    ),
    f"{conn_c2.calls}",
)
check(
    "C: family_member link for the SECOND claim points at the pre-existing family_id",
    any(
        "INSERT IGNORE INTO family_member" in s and p[0] == family_id_c and p[1] == "cl_fs_c2"
        for s, p in conn_c2.calls
    ),
    f"{conn_c2.calls}",
)


# ============================================================
# D. Fail-open — SQL genuinely unconfigured (real environment state,
# not simulated).
# ============================================================

check("D precondition: SQL layer genuinely unconfigured", sqlconn.is_configured() is False)

registry_d = _isolated_registry()
claims_d = [{"claim_id": "cl_fs_d1", "claim_text": "Совершенно другое отдельное утверждение про тесты."}]
with patch.object(lifecycle_mod, "get_claim_family_registry", lambda: registry_d):
    try:
        lifecycle_mod.assign_claim_family_identity(
            claims_d, _FakeEpistemicResult(), False, {}, _noop_log, False,
        )
        no_raise = True
    except Exception as e:
        no_raise = False

check("D: assign_claim_family_identity() never raises with no DB configured", no_raise)
check(
    "D: semantic_family_id assignment (JSON path) still works with SQL unconfigured",
    claims_d[0].get("semantic_family_id") is not None,
    f"{claims_d[0]}",
)


# ============================================================
# E. No unexpected mutation beyond semantic_family_id.
# ============================================================

check(
    "E: claim dict gains ONLY semantic_family_id, no other keys, from the shadow wiring",
    set(claims_d[0].keys()) == {"claim_id", "claim_text", "semantic_family_id"},
    f"{sorted(claims_d[0].keys())}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
