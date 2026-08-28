"""
agent/db_sql_belief_shadow_regression_test.py — Этап 5 (SQL persistence
migration) regression: belief/belief_assessment_history shadow write
(mandate §17 DoD item "belief history не уничтожается"), wired at all
5 of agent/belief_manager.py's existing Belief.history.append(...)
call sites (add_belief's create path, _apply_decay, _update_existing,
challenge_belief, supersede_belief).

PREVIOUS AUDIT CORRECTION proven here: schema.py's original 5A design
guessed belief_assessment_history.change_type as ENUM('decay','update',
'challenge','supersede') — none of which match the real values
agent/belief_manager.py ever writes ('created'/'decayed'/'updated'/
'revised'/'superseded'). Corrected in schema.py before this table was
ever written to; this suite proves the corrected ENUM actually matches
what the wiring sends, for all 5 change types.

Covers:
    A. structural: shadow_record_belief_assessment( appears in all 5
       real production call sites, each passing that method's OWN
       change_type ('created'/'decayed'/'updated'/'revised'/'superseded').
    B. functional (FakeConnection): add_belief (create), add_belief
       again with new evidence (-> _update_existing), challenge_belief,
       supersede_belief each produce a belief upsert + a
       belief_assessment_history insert with the correct change_type
       and old/new confidence.
    C. fail-open: SQL genuinely unconfigured — BeliefManager's normal
       (JSON) behavior is completely unaffected (real env, not mocked).
    D. ENUM/code consistency: every change_type string the code passes
       to shadow_record_belief_assessment is one of the 5 values
       schema.py's corrected ENUM actually declares — no 6th value
       invented, none of the 4 stale guessed labels used.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_belief_shadow_regression_test
"""
from __future__ import annotations

import contextlib
import inspect
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.belief_manager as bm_mod
import agent.db.sql.shadow_write as sw
import agent.db.sql.schema as schema_mod
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


_src = inspect.getsource(bm_mod)

# ============================================================
# A. Structural: all 5 call sites present, each with its own change_type.
# ============================================================

_expected_sites = [
    ('change_type="created"', "add_belief (creation path)"),
    ('change_type="decayed"', "_apply_decay"),
    ('change_type="updated"', "_update_existing"),
    ('change_type="revised"', "challenge_belief"),
    ('change_type="superseded"', "supersede_belief"),
]
for needle, label in _expected_sites:
    check(f"A: shadow_record_belief_assessment( call with {needle} present ({label})", needle in _src)

check(
    "A: shadow_record_belief_assessment is imported from agent.db.sql.shadow_write "
    "(not a second, duplicate persistence path)",
    "from agent.db.sql.shadow_write import shadow_record_belief_assessment" in _src,
)


# ============================================================
# D. ENUM/code consistency.
# ============================================================

_schema_src = inspect.getsource(schema_mod)
_enum_match = re.search(r"change_type\s+ENUM\(([^)]+)\)", _schema_src)
_enum_values = {v.strip().strip("'") for v in _enum_match.group(1).split(",")} if _enum_match else set()
_code_values = {m for m in re.findall(r'change_type="(\w+)"', _src)}

check(
    "D: every change_type string belief_manager.py actually sends is declared in "
    "schema.py's ENUM (no undeclared 6th value)",
    _code_values.issubset(_enum_values),
    f"code={_code_values} schema_enum={_enum_values}",
)
check(
    "D: schema.py's ENUM declares EXACTLY the 5 real values (no leftover stale "
    "guessed labels like the original 'decay'/'update'/'challenge'/'supersede')",
    _enum_values == {"created", "decayed", "updated", "revised", "superseded"},
    f"{_enum_values}",
)


# ============================================================
# B. Functional — FakeConnection harness, real BeliefManager, isolated storage.
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


def _run_with_fake_sql(fn):
    conn = FakeConnection()

    @contextlib.contextmanager
    def _fake_get_connection(autocommit=False):
        yield conn

    with patch.object(sw, "get_connection", _fake_get_connection):
        result = fn()
    return conn, result


def _belief_upserts(conn):
    return [p for s, p in conn.calls if s.startswith("INSERT INTO belief ")]


def _history_inserts(conn):
    return [p for s, p in conn.calls if s.startswith("INSERT INTO belief_assessment_history")]


storage = Path(tempfile.mkdtemp(prefix="p5_beliefshadow_")) / "beliefs.json"
bm = bm_mod.BeliefManager(storage_file=storage)

# --- create ---
conn_create, belief = _run_with_fake_sql(lambda: bm.add_belief(
    topic="astronomy", statement="У Юпитера известно 95 спутников.", confidence=0.6,
))
check("B (create): belief upsert written", len(_belief_upserts(conn_create)) == 1, f"{conn_create.calls}")
check("B (create): history insert has change_type='created', old_confidence=0.0", any(
    p[5] == "created" and p[2] == 0.0 and p[3] == 0.6 for p in _history_inserts(conn_create)
), f"{_history_inserts(conn_create)}")

# --- update (same topic+statement -> _find_similar exact match -> _update_existing) ---
conn_update, belief2 = _run_with_fake_sql(lambda: bm.add_belief(
    topic="astronomy", statement="У Юпитера известно 95 спутников.", confidence=0.7,
    evidence_for=["ev_new_1"],
))
check(
    "B (update): SAME belief_id reused (exact-match found the existing belief, not a new one)",
    belief2.id == belief.id, f"{belief.id} vs {belief2.id}",
)
check("B (update): history insert has change_type='updated'", any(
    p[5] == "updated" for p in _history_inserts(conn_update)
), f"{_history_inserts(conn_update)}")

# --- challenge ---
conn_challenge, challenged = _run_with_fake_sql(lambda: bm.challenge_belief(
    belief_id=belief.id, counter_evidence="ev_counter_1", new_confidence=0.4, reason="new study",
))
check("B (challenge): challenge_belief found and returned the belief", challenged is not None)
check("B (challenge): history insert has change_type='revised'", any(
    p[5] == "revised" for p in _history_inserts(conn_challenge)
), f"{_history_inserts(conn_challenge)}")

# --- supersede ---
new_belief = bm.add_belief(topic="astronomy", statement="Совершенно другое отдельное утверждение.", confidence=0.5)
conn_supersede, ok = _run_with_fake_sql(lambda: bm.supersede_belief(belief.id, new_belief.id))
check("B (supersede): supersede_belief succeeded", ok is True)
check(
    "B (supersede): history insert has change_type='superseded', "
    "old/new confidence both None (matches JSON: no confidence recorded for this change)",
    any(p[5] == "superseded" and p[2] is None and p[3] is None for p in _history_inserts(conn_supersede)),
    f"{_history_inserts(conn_supersede)}",
)

# --- decay ---
old_belief = bm_mod.Belief(
    id="bel_decaytest", topic="test", statement="убеждение для проверки decay",
    confidence=0.8, evidence_for=[], evidence_against=[], claim_ids=[],
    created_at=0.0, updated_at=0.0,  # far in the past -> age_days > 1
    history=[], status="active",
)
bm.beliefs.append(old_belief)
conn_decay, _ = _run_with_fake_sql(lambda: bm._apply_decay())
check("B (decay): history insert has change_type='decayed' for the aged belief", any(
    p[5] == "decayed" and p[0] == "bel_decaytest" for p in _history_inserts(conn_decay)
), f"{_history_inserts(conn_decay)}")


# ============================================================
# C. Fail-open — SQL genuinely unconfigured (real environment state).
# ============================================================

check("C precondition: SQL layer genuinely unconfigured", sqlconn.is_configured() is False)

storage_c = Path(tempfile.mkdtemp(prefix="p5_beliefshadow_c_")) / "beliefs.json"
bm_c = bm_mod.BeliefManager(storage_file=storage_c)
try:
    b_c = bm_c.add_belief(topic="t", statement="s", confidence=0.5)
    bm_c.challenge_belief(belief_id=b_c.id, counter_evidence="e", new_confidence=0.3, reason="r")
    no_raise = True
except Exception as e:
    no_raise = False
check("C: BeliefManager's normal JSON behavior is unaffected with no SQL configured", no_raise)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
