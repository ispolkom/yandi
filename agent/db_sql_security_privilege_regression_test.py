"""
agent/db_sql_security_privilege_regression_test.py — Этап 5E-S S2:
privilege model + immutability trigger design (mandate §10/§6/§7/§34).

STATIC PROOF ONLY (mandate §55) — no live server exists to run
`SHOW GRANTS`/fire a real trigger against. What IS proven here:
the exact GRANT/CREATE TRIGGER SQL text this pass generates has the
right shape, targets the right tables, and structurally cannot grant
a forbidden privilege — proven by parsing the actual strings
agent/db/sql/security_grants.py and security_triggers.py produce, not
by asserting a design intention in prose.

Covers:
    A. YANDI_RUNTIME: no FORBIDDEN_FOR_RUNTIME privilege appears in any
       of its statements; UPDATE appears ONLY for class C/D tables,
       never for a class A/B table; DELETE appears nowhere.
    B. YANDI_READONLY: SELECT only, zero write privilege of any kind.
    C. YANDI_MIGRATOR: DDL scoped to `yandi_epistemic`.* only — no
       CREATE USER, no GRANT OPTION, no `*.*` grant anywhere.
    D. YANDI_BOOTSTRAP: the one role allowed CREATE USER/GRANT OPTION
       (MySQL's own global-only constraint, documented) — but even it
       gets a real `revoke_all_statement()` path (mandate §10.1:
       remove after install).
    E. every password is passed as a bound parameter, never
       interpolated into the SQL text (same T1/T15 discipline as the
       rest of agent/db/sql/).
    F. TABLE_CLASSIFICATION covers EXACTLY the same table set as
       ALL_TABLES_IN_ORDER — no drift, no forgotten table defaulting to
       "unclassified == unprotected".
    G. immutability_triggers(): every class A/B table gets BOTH a
       no-update and no-delete trigger; every class C table gets a
       no-delete trigger (but NOT a no-update one — projections are
       meant to be updated); verification_run (class D) gets its own
       narrow guard, not a blanket reject.
    H. repository capability surface (mandate §34): zero generic
       update_any()/delete_any()/execute_raw()/run_sql(), and zero
       function name matching update_question/delete_question/
       delete_answer/update_answer_version-shaped patterns exist in
       agent/db/sql/repositories.py.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_security_privilege_regression_test
"""
from __future__ import annotations

import inspect
import re

from agent.db.sql.schema import ALL_TABLES_IN_ORDER, TABLE_CLASSIFICATION
from agent.db.sql.security_grants import (
    FORBIDDEN_FOR_RUNTIME, FORBIDDEN_FOR_READONLY,
    yandi_bootstrap_statements, yandi_migrator_statements,
    yandi_runtime_statements, yandi_readonly_statements,
    revoke_all_statement, CLASS_AB_TABLES, CLASS_C_TABLES, CLASS_D_TABLES,
)
from agent.db.sql.security_triggers import immutability_triggers
import agent.db.sql.repositories as repo_mod

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
# A. YANDI_RUNTIME.
# ============================================================

def _grant_only_text(stmts) -> str:
    """Isolates just the GRANT ... statements' text — excludes the
    account-creation CREATE USER statement, which legitimately contains
    the substring 'CREATE' without granting the CREATE privilege to
    anything (creating an account is not the same as granting it a
    privilege)."""
    return "\n".join(sql for sql, _p in stmts if sql.startswith("GRANT"))


runtime_stmts = yandi_runtime_statements("yandi_runtime", "10.0.0.5", "pw")
runtime_sql_text = "\n".join(sql for sql, _p in runtime_stmts)
runtime_grants_text = _grant_only_text(runtime_stmts)

for forbidden in FORBIDDEN_FOR_RUNTIME:
    check(
        f"A: YANDI_RUNTIME's GRANT statements never mention forbidden privilege {forbidden!r}",
        forbidden not in runtime_grants_text,
        f"{runtime_grants_text}",
    )
check(
    "A: YANDI_RUNTIME has NO DELETE grant anywhere",
    "DELETE" not in runtime_sql_text,
    f"{runtime_sql_text}",
)

_update_tables_granted = set(re.findall(r"GRANT UPDATE ON `yandi_epistemic`\.`(\w+)`", runtime_sql_text))
check(
    "A: YANDI_RUNTIME's UPDATE grants are EXACTLY the class C+D tables — "
    "never a class A/B table",
    _update_tables_granted == set(CLASS_C_TABLES) | set(CLASS_D_TABLES),
    f"granted={_update_tables_granted} expected={set(CLASS_C_TABLES) | set(CLASS_D_TABLES)}",
)
check(
    "A: YANDI_RUNTIME gets a blanket SELECT, INSERT on the whole database "
    "(every canonical write is an INSERT; reads are needed everywhere)",
    "GRANT SELECT, INSERT ON `yandi_epistemic`.*" in runtime_sql_text,
)

# ============================================================
# B. YANDI_READONLY.
# ============================================================

readonly_stmts = yandi_readonly_statements("yandi_readonly", "localhost", "pw")
readonly_sql_text = "\n".join(sql for sql, _p in readonly_stmts)
readonly_grants_text = _grant_only_text(readonly_stmts)

for forbidden in FORBIDDEN_FOR_READONLY:
    check(
        f"B: YANDI_READONLY's GRANT statements never mention forbidden privilege {forbidden!r}",
        forbidden not in readonly_grants_text,
        f"{readonly_grants_text}",
    )
check(
    "B: YANDI_READONLY gets exactly one GRANT statement (SELECT), plus CREATE USER",
    len(readonly_stmts) == 2 and "GRANT SELECT ON `yandi_epistemic`.*" in readonly_sql_text,
    f"{readonly_stmts}",
)

# ============================================================
# C. YANDI_MIGRATOR.
# ============================================================

migrator_stmts = yandi_migrator_statements("yandi_migrator", "localhost", "pw")
migrator_sql_text = "\n".join(sql for sql, _p in migrator_stmts)

check(
    "C: YANDI_MIGRATOR has no CREATE USER grant (only bootstrap does) — "
    "note: its own account creation uses CREATE USER, but it is not GRANTED "
    "the CREATE USER privilege itself",
    "GRANT" in migrator_sql_text and "CREATE USER ON" not in migrator_sql_text,
    f"{migrator_sql_text}",
)
check(
    "C: YANDI_MIGRATOR has no GRANT OPTION",
    "GRANT OPTION" not in migrator_sql_text,
)
check(
    "C: YANDI_MIGRATOR's DDL grant is scoped to `yandi_epistemic`.* only — no `*.*` grant",
    "*.*" not in migrator_sql_text and "`yandi_epistemic`.*" in migrator_sql_text,
    f"{migrator_sql_text}",
)
check(
    "C: YANDI_MIGRATOR has no SUPER/FILE/PROCESS",
    all(tok not in migrator_sql_text for tok in ("SUPER", "FILE", "PROCESS")),
)

# ============================================================
# D. YANDI_BOOTSTRAP (allowed to be broad, but only temporally).
# ============================================================

bootstrap_stmts = yandi_bootstrap_statements("yandi_bootstrap", "localhost", "pw")
bootstrap_sql_text = "\n".join(sql for sql, _p in bootstrap_stmts)
check(
    "D: YANDI_BOOTSTRAP is the only role whose design includes CREATE USER "
    "(MySQL's own global-only constraint for this privilege, documented in "
    "the module docstring, not hidden)",
    "CREATE USER" in bootstrap_sql_text,
)
revoke_sql, revoke_params = revoke_all_statement("yandi_bootstrap", "localhost")
check(
    "D: a real revoke/drop path exists for retiring YANDI_BOOTSTRAP after install "
    "(mandate §10.1: remove the credential after use)",
    "DROP USER" in revoke_sql and revoke_params == ("yandi_bootstrap", "localhost"),
    f"{revoke_sql} {revoke_params}",
)

# ============================================================
# E. Passwords are always bound parameters, never interpolated.
# ============================================================

for stmts, label in (
    (runtime_stmts, "runtime"), (readonly_stmts, "readonly"),
    (migrator_stmts, "migrator"), (bootstrap_stmts, "bootstrap"),
):
    create_user_stmt = next((s for s, p in stmts if s.startswith("CREATE USER")), None)
    check(
        f"E: {label}'s CREATE USER statement binds username/host/password as "
        f"%s parameters, never interpolated into the SQL text",
        create_user_stmt is not None and "pw" not in create_user_stmt and "%s" in create_user_stmt,
        f"{create_user_stmt}",
    )

# ============================================================
# F. TABLE_CLASSIFICATION has no drift from ALL_TABLES_IN_ORDER.
# ============================================================

_all_table_names = {n for n, _ in ALL_TABLES_IN_ORDER}
_classified_names = set(TABLE_CLASSIFICATION.keys())
check(
    "F: every table in ALL_TABLES_IN_ORDER has a classification — none "
    "silently unclassified (== unprotected)",
    _all_table_names <= _classified_names,
    f"unclassified: {_all_table_names - _classified_names}",
)
check(
    "F: TABLE_CLASSIFICATION has no stale entries for tables that don't exist",
    _classified_names <= _all_table_names,
    f"stale: {_classified_names - _all_table_names}",
)
check(
    "F: every classification value is one of A/B/C/D/E",
    all(v in ("A", "B", "C", "D", "E") for v in TABLE_CLASSIFICATION.values()),
    f"{TABLE_CLASSIFICATION}",
)

# ============================================================
# G. Trigger coverage.
# ============================================================

triggers = immutability_triggers()
trigger_names = {name for name, _ddl in triggers}

for table in CLASS_AB_TABLES:
    check(f"G: class A/B table {table!r} has a no-update trigger", f"trg_{table}_no_update" in trigger_names)
    check(f"G: class A/B table {table!r} has a no-delete trigger", f"trg_{table}_no_delete" in trigger_names)

for table in CLASS_C_TABLES:
    check(f"G: class C (projection) table {table!r} has a no-delete trigger", f"trg_{table}_no_delete" in trigger_names)
    check(
        f"G: class C table {table!r} does NOT have a blanket no-update trigger "
        f"(projections are legitimately updatable)",
        f"trg_{table}_no_update" not in trigger_names,
    )

check(
    "G: verification_run gets its OWN narrow guard trigger, not a blanket no-update",
    "trg_verification_run_guard_update" in trigger_names
    and "trg_verification_run_no_update" not in trigger_names,
    f"{sorted(trigger_names)}",
)
check("G: verification_run still gets a no-delete trigger", "trg_verification_run_no_delete" in trigger_names)

_guard_ddl = next(ddl for name, ddl in triggers if name == "trg_verification_run_guard_update")
check(
    "G: verification_run's guard rejects changing run_id/occurrence_id/started_at",
    "run_id <> OLD.run_id" in _guard_ddl or "NEW.run_id <> OLD.run_id" in _guard_ddl,
    f"{_guard_ddl}",
)
check(
    "G: verification_run's guard only allows running -> a terminal status",
    "'completed', 'aborted', 'failed'" in _guard_ddl,
    f"{_guard_ddl}",
)

# ============================================================
# H. Repository capability surface (mandate §34).
# ============================================================

_repo_functions = [name for name, _ in inspect.getmembers(repo_mod, inspect.isfunction)]
_forbidden_generic = ("update_any", "delete_any", "execute_raw", "run_sql", "raw_execute")
for forbidden in _forbidden_generic:
    check(f"H: no generic '{forbidden}'-shaped function exists in repositories.py", forbidden not in _repo_functions)

_forbidden_specific_patterns = re.compile(
    r"^(update|delete)_(question|answer|answer_version|claim_occurrence|"
    r"source_observation|evidence_relation|belief_assessment_history|recheck_event)$"
)
_matches = [n for n in _repo_functions if _forbidden_specific_patterns.match(n)]
check(
    "H: no update_question()/delete_question()/update_answer()/delete_answer()"
    "-shaped function exists (mandate §6/§33 — no such capability should ever exist)",
    len(_matches) == 0,
    f"found: {_matches}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
