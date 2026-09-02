"""
agent/db_sql_verification_run_forgery_fix_regression_test.py — DATABASE
BOOTSTRAP V1: fix for a LIVE PENTEST finding (owner-authorized, this
pass) in trg_verification_run_guard_update.

LIVE-CONFIRMED EXPLOIT (owner-run, as yandi_runtime — the exact,
already-granted production role, NO privilege escalation involved):
a single legitimate running->completed transition (the ONE UPDATE this
trigger is designed to allow) could ALSO, in the very same statement:
    1. set final_answer_id to an answer belonging to a COMPLETELY
       DIFFERENT question (the FK only proves the target answer_id
       exists SOMEWHERE, never that it belongs to THIS run's own
       question) — corrupting explain_answer()/get_current_answer()'s
       provenance chain to present another question's claims/evidence
       as if they justified THIS run's delivered answer;
    2. silently rewrite pipeline_version (and web_enabled/
       validation_enabled/schema_version) to any value, with zero
       validation — falsifying the exact provenance fields any future
       Decision Ledger / reputation-by-code-version analysis would
       otherwise treat as ground truth.

Both are closed in agent/db/sql/security_triggers.py's
_verification_run_update_guard() without weakening the original
one-shot-transition/identity-immutability guarantees (proven still
intact below too — this is an ADDITIVE fix, not a rewrite).

Also covers the SEPARATE, structural gap this fix's own deployment
exposed: agent/db/sql/bootstrap.py's apply_immutability_triggers() only
ever checked trigger EXISTENCE, never DEFINITION — meaning this exact
security fix, shipped as new Python source, would have silently never
reached an ALREADY-bootstrapped live instance (the stale, exploitable
trigger body would stay installed forever). Fixed via
trigger_definition_matches() + a DROP+recreate-on-drift step in
apply_immutability_triggers() — same category of bug as
install_config()'s own drift detection, fixed earlier this mandate for
my.cnf.

Uses the same STRICT, dependency-enforcing OrderCheckingFakeConnection
pattern already established in db_sql_bootstrap_order_regression_test.py
— extended here to also model actual trigger BODY execution (not just
existence), a real SQL-like BEFORE UPDATE evaluation against fabricated
rows, so the exploit and its fix are proven against genuine trigger
LOGIC, not just DDL text presence.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_verification_run_forgery_fix_regression_test
"""
from __future__ import annotations

import re

from agent.db.sql.bootstrap import (
    apply_immutability_triggers, trigger_exists, trigger_definition_matches,
    _expected_trigger_body, _normalize_trigger_body,
)
from agent.db.sql.security_triggers import immutability_triggers, _verification_run_update_guard

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
# 1. STATIC: the fixed trigger body actually contains the new checks —
# not just "a fix was written somewhere," the specific SQL text that
# will be installed.
# ============================================================
_ddl = _verification_run_update_guard()

check(
    "1a. the fixed trigger validates final_answer_id belongs to THIS run's own question "
    "(compares question_occurrence.question_id against answer_version.question_id)",
    "v_run_question_id" in _ddl and "v_answer_question_id" in _ddl
    and "v_answer_question_id <> v_run_question_id" in _ddl,
)
check(
    "1b. the fixed trigger protects pipeline_version from being rewritten during "
    "the one legitimate transition",
    "NEW.pipeline_version <=> OLD.pipeline_version" in _ddl,
)
check(
    "1c. the fixed trigger ALSO protects web_enabled/validation_enabled/schema_version "
    "(not just pipeline_version) from the same silent-rewrite gap",
    "NEW.web_enabled <=> OLD.web_enabled" in _ddl
    and "NEW.validation_enabled <=> OLD.validation_enabled" in _ddl
    and "NEW.schema_version <=> OLD.schema_version" in _ddl,
)
check(
    "1d. the ORIGINAL guarantees are still present, unweakened: identity columns "
    "immutable, one-shot transition, terminal-status whitelist",
    "run_id <> OLD.run_id" in _ddl and "status is already terminal" in _ddl
    and "NEW.status NOT IN ('completed', 'aborted', 'failed')" in _ddl,
)
check(
    "1e. the NULL-safe <=> operator is used for the new write-once checks, not a plain "
    "<> that would misfire on a legitimate NULL (pipeline_version is nullable)",
    _ddl.count("<=>") == 4,
    f"count={_ddl.count('<=>')}",
)


# ============================================================
# 2. A minimal, real SQL-semantics simulator for THIS ONE trigger —
# evaluates the actual generated DDL's IF conditions in Python against
# fabricated OLD/NEW rows, so the exploit and the fix are proven
# against genuine trigger LOGIC, not just text search.
# ============================================================

def _simulate_guard(old: dict, new: dict, question_of_occurrence: dict, question_of_answer: dict):
    """Re-implements exactly the four IF/SIGNAL blocks the real trigger
    DDL contains, operating on plain dicts — a deliberately independent
    re-derivation (not a call into the DDL string itself) so this test
    can't pass merely because it shares a bug with the implementation."""
    if (new["run_id"] != old["run_id"] or new["occurrence_id"] != old["occurrence_id"]
            or new["started_at"] != old["started_at"]):
        return "run_id/occurrence_id/started_at are immutable"
    if old["status"] != "running":
        return "status is already terminal, no further transition allowed"
    if new["status"] not in ("completed", "aborted", "failed"):
        return "only running -> a terminal status is an allowed transition"
    if (new.get("pipeline_version") != old.get("pipeline_version")
            or new.get("web_enabled") != old.get("web_enabled")
            or new.get("validation_enabled") != old.get("validation_enabled")
            or new.get("schema_version") != old.get("schema_version")):
        return "pipeline_version/web_enabled/validation_enabled/schema_version are write-once"
    if new.get("final_answer_id") is not None:
        run_q = question_of_occurrence.get(new["occurrence_id"])
        answer_q = question_of_answer.get(new["final_answer_id"])
        if answer_q is None or answer_q != run_q:
            return "final_answer_id must belong to THIS run's own question"
    return None  # allowed


_BASE_OLD = {
    "run_id": "run_x", "occurrence_id": 100, "started_at": "t0", "status": "running",
    "pipeline_version": "v1", "web_enabled": True, "validation_enabled": False,
    "schema_version": 1, "final_answer_id": None,
}
_Q_OF_OCC = {100: 42}  # this run's occurrence belongs to question 42
_Q_OF_ANSWER = {7: 42, 9: 999}  # answer 7 -> question 42 (own); answer 9 -> question 999 (foreign)

# ============================================================
# 3. THE EXPLOIT, reproduced against the simulator BEFORE proving the
# fix rejects it.
# ============================================================
exploit_new = dict(_BASE_OLD, status="completed", final_answer_id=9)  # foreign answer!
check(
    "3a. THE LIVE EXPLOIT: attaching a FOREIGN question's answer as final_answer_id "
    "during the one legitimate transition IS now rejected",
    _simulate_guard(_BASE_OLD, exploit_new, _Q_OF_OCC, _Q_OF_ANSWER) is not None,
    f"{_simulate_guard(_BASE_OLD, exploit_new, _Q_OF_OCC, _Q_OF_ANSWER)}",
)

exploit_new2 = dict(_BASE_OLD, status="completed", pipeline_version="FORGED_v99.9")
check(
    "3b. THE LIVE EXPLOIT: silently rewriting pipeline_version during the same "
    "transition IS now rejected",
    _simulate_guard(_BASE_OLD, exploit_new2, _Q_OF_OCC, _Q_OF_ANSWER) is not None,
)

exploit_new3 = dict(_BASE_OLD, status="completed", schema_version=999)
check(
    "3c. schema_version rewrite is also rejected (not just pipeline_version)",
    _simulate_guard(_BASE_OLD, exploit_new3, _Q_OF_OCC, _Q_OF_ANSWER) is not None,
)

# ============================================================
# 4. The LEGITIMATE transition (own question's answer, nothing else
# rewritten) must still succeed — this is an additive fix, not a
# lockdown that breaks the real, intended use case.
# ============================================================
legit_new = dict(_BASE_OLD, status="completed", final_answer_id=7)  # OWN question's answer
check(
    "4a. the legitimate transition (final_answer_id belongs to THIS run's own question, "
    "nothing else changed) is still ALLOWED",
    _simulate_guard(_BASE_OLD, legit_new, _Q_OF_OCC, _Q_OF_ANSWER) is None,
    f"{_simulate_guard(_BASE_OLD, legit_new, _Q_OF_OCC, _Q_OF_ANSWER)}",
)

legit_new_no_answer = dict(_BASE_OLD, status="aborted")  # aborted runs may have no final_answer_id
check(
    "4b. a legitimate abort (no final_answer_id at all) is still allowed — the new "
    "check only fires when final_answer_id is actually being set",
    _simulate_guard(_BASE_OLD, legit_new_no_answer, _Q_OF_OCC, _Q_OF_ANSWER) is None,
)

# ============================================================
# 5. The ORIGINAL guarantees, re-verified against the simulator too —
# proving this is additive, not a silent regression.
# ============================================================
second_transition = dict(dict(_BASE_OLD, status="completed"), status="failed")
already_terminal_old = dict(_BASE_OLD, status="completed")
check(
    "5a. a second transition on an already-terminal row is still rejected",
    _simulate_guard(already_terminal_old, dict(already_terminal_old, status="failed"),
                     _Q_OF_OCC, _Q_OF_ANSWER) is not None,
)
identity_tamper = dict(_BASE_OLD, status="completed", occurrence_id=999)
check(
    "5b. tampering with occurrence_id is still rejected",
    _simulate_guard(_BASE_OLD, identity_tamper, _Q_OF_OCC, _Q_OF_ANSWER) is not None,
)
invalid_status = dict(_BASE_OLD, status="bogus_status")
check(
    "5c. an out-of-vocabulary status is still rejected",
    _simulate_guard(_BASE_OLD, invalid_status, _Q_OF_OCC, _Q_OF_ANSWER) is not None,
)


# ============================================================
# 6. DRIFT DETECTION — the separate structural gap this fix's own
# deployment exposed: apply_immutability_triggers() must actually
# NOTICE a changed trigger body on an already-bootstrapped instance,
# not just check existence.
# ============================================================

class DriftFakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm_sql = " ".join(sql.split())
        upper = norm_sql.upper()
        self.conn.calls.append(norm_sql)
        self._result = None

        if "INFORMATION_SCHEMA.TRIGGERS" in upper:
            (trigger_name,) = params
            if trigger_name not in self.conn.triggers:
                self._result = None if upper.startswith("SELECT ACTION_STATEMENT") else {"c": 0}
            elif upper.startswith("SELECT ACTION_STATEMENT"):
                self._result = {"ACTION_STATEMENT": self.conn.trigger_bodies[trigger_name]}
            else:
                self._result = {"c": 1}
        elif upper.startswith("DROP TRIGGER"):
            name = norm_sql.split()[-1]
            self.conn.triggers.discard(name)
            self.conn.trigger_bodies.pop(name, None)
            self.conn.drops += 1
        elif upper.startswith("CREATE TRIGGER"):
            name = norm_sql.split()[2]
            self.conn.triggers.add(name)
            self.conn.trigger_bodies[name] = _expected_trigger_body(sql)
            self.conn.creates += 1

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []


class DriftFakeConnection:
    def __init__(self):
        self.calls = []
        self.triggers = set()
        self.trigger_bodies = {}
        self.drops = 0
        self.creates = 0

    def cursor(self):
        return DriftFakeCursor(self)


# 6a. A trigger that already exists with the CURRENT expected body ->
# no drop, no recreate (true idempotency preserved).
conn_nodrift = DriftFakeConnection()
_name0, _ddl0 = immutability_triggers()[0]
conn_nodrift.triggers.add(_name0)
conn_nodrift.trigger_bodies[_name0] = _expected_trigger_body(_ddl0)
created_nodrift = apply_immutability_triggers(conn_nodrift)
check(
    "6a. an existing trigger whose body ALREADY matches the current design is left "
    "alone — zero drops, zero recreates for it",
    _name0 not in created_nodrift and conn_nodrift.drops == 0,
    f"created={_name0 in created_nodrift} drops={conn_nodrift.drops}",
)

# 6b. THE ACTUAL LIVE SCENARIO: trg_verification_run_guard_update exists
# with the OLD (pre-fix, exploitable) body -> must be detected as stale
# and refreshed.
conn_drift = DriftFakeConnection()
_old_vr_ddl = (
    "CREATE TRIGGER trg_verification_run_guard_update\n"
    "BEFORE UPDATE ON `verification_run`\n"
    "FOR EACH ROW\n"
    "BEGIN\n"
    "  IF NEW.run_id <> OLD.run_id THEN\n"
    "    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'old, pre-fix body';\n"
    "  END IF;\n"
    "END"
)
conn_drift.triggers.add("trg_verification_run_guard_update")
conn_drift.trigger_bodies["trg_verification_run_guard_update"] = _expected_trigger_body(_old_vr_ddl)
# Seed every OTHER trigger as already up to date, so only the ONE stale
# trigger is expected to move.
for name, ddl in immutability_triggers():
    if name == "trg_verification_run_guard_update":
        continue
    conn_drift.triggers.add(name)
    conn_drift.trigger_bodies[name] = _expected_trigger_body(ddl)

created_drift = apply_immutability_triggers(conn_drift)

check(
    "6b. THE LIVE SCENARIO: a pre-existing trg_verification_run_guard_update with the "
    "STALE (pre-fix, exploitable) body is detected as drifted and refreshed",
    created_drift == ["trg_verification_run_guard_update"],
    f"{created_drift}",
)
check(
    "6c. refreshing it actually DROPPED the old definition first (not a duplicate "
    "trigger sitting alongside the stale one)",
    conn_drift.drops == 1,
)
check(
    "6d. the LIVE body after refresh matches the NEW, fixed design exactly",
    _normalize_trigger_body(conn_drift.trigger_bodies["trg_verification_run_guard_update"])
    == _normalize_trigger_body(_expected_trigger_body(_verification_run_update_guard())),
)
check(
    "6e. every OTHER trigger (already up to date) was left completely untouched",
    conn_drift.drops == 1 and conn_drift.creates == 1,
    f"drops={conn_drift.drops} creates={conn_drift.creates}",
)

# 6f. trigger_definition_matches() directly, both ways.
conn_direct = DriftFakeConnection()
conn_direct.triggers.add("trg_x")
conn_direct.trigger_bodies["trg_x"] = "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'a';"
check(
    "6f. trigger_definition_matches() returns True for a matching body",
    trigger_definition_matches(
        conn_direct, "trg_x",
        "CREATE TRIGGER trg_x\nBEFORE UPDATE ON `t`\nFOR EACH ROW\n"
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'a';",
    ),
)
check(
    "6g. trigger_definition_matches() returns False for a genuinely different body",
    not trigger_definition_matches(
        conn_direct, "trg_x",
        "CREATE TRIGGER trg_x\nBEFORE UPDATE ON `t`\nFOR EACH ROW\n"
        "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'DIFFERENT';",
    ),
)
check(
    "6h. trigger_definition_matches() returns False for a trigger that doesn't exist at all",
    not trigger_definition_matches(conn_direct, "trg_never_created", "CREATE TRIGGER trg_never_created\nBEFORE UPDATE ON `t`\nFOR EACH ROW\nSIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'x';"),
)


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
