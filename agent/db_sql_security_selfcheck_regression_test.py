"""
agent/db_sql_security_selfcheck_regression_test.py — Этап 5E-S S8:
startup/CI security self-check (mandate §39, §44 section N).

STATIC/MOCK PROOF ONLY (mandate §55) — no live server to run SHOW
GRANTS against. What IS proven: agent/db/sql/security_selfcheck.py's
LOGIC correctly interprets realistic information_schema/SHOW GRANTS
result shapes, via a scripted fake connection that returns exactly the
kind of rows a real MySQL server would.

Covers:
    - clean state (correct schema version, all tables present, all
      triggers present, grants match the allow-list) -> ok=True.
    - missing table -> detected, named specifically.
    - missing trigger -> detected, named specifically.
    - a forbidden privilege actually present in SHOW GRANTS output
      (e.g. DROP somehow granted to the runtime account) -> detected —
      the LIVE counterpart of the static privilege regression's proof.
    - parse_show_grants() correctly extracts privilege tokens from
      realistic multi-privilege GRANT lines, including an 'ALL
      PRIVILEGES' line (must be treated as maximally forbidden, not
      silently ignored because "ALL PRIVILEGES" isn't a literal string
      in FORBIDDEN_FOR_RUNTIME's short list).
    - no silent repair: run_selfcheck() issues ZERO write statements
      (no CREATE/GRANT/INSERT/UPDATE/DELETE in any call it makes) —
      mandate §39: "не делать silent repair от runtime account."

DATABASE BOOTSTRAP V1, sixteenth Phase B attempt — LIVE BUG in the
grants half of this check. Owner-run live output (auth_socket bootstrap
+ schema fully applied, tables_ok=True, triggers_ok=True,
identity_ok=True MATCH):

    grants_ok=False
    grant_violations=[CREATE, DROP, RELOAD, SHUTDOWN, PROCESS, FILE,
                       ALTER, SUPER, EXECUTE, CREATE USER]

ROOT CAUSE: live_bootstrap.py's run() calls run_selfcheck(conn,
role="runtime", ...) using the SAME `conn` it just used to BOOTSTRAP
everything — root@localhost via auth_socket (necessarily privileged;
that connection is how yandi_runtime/readonly/migrator get created at
all). check_current_grants_against_allowlist() runs `SHOW GRANTS FOR
CURRENT_USER()`, so this was checking ROOT's own legitimate admin
grants against FORBIDDEN_FOR_RUNTIME — an account holding CREATE
USER/SUPER/etc. is not a runtime-role misconfiguration, it is exactly
what a bootstrap-capable account is supposed to hold. The violations
list is a perfect literal match for a fresh MySQL root account's
default global grants, not a hint of anything wrong with
yandi_runtime.

FIX: check_named_principal_grants(conn, username, host, forbidden) —
SHOW GRANTS FOR the EXPLICITLY NAMED account, never CURRENT_USER().
run_selfcheck() gained an optional role_principals={"runtime": (user,
host), "readonly": (...), "migrator": (...)} parameter — when given,
each named account is checked against ITS OWN policy via
FORBIDDEN_BY_ROLE (runtime/readonly/migrator each have a distinct
list; bootstrap/root is deliberately absent from FORBIDDEN_BY_ROLE
entirely — it is never evaluated against any deny-list). Omitting
role_principals (every pre-existing test above, and any future
genuine "a runtime process self-checks its OWN connection" caller)
preserves the exact original CURRENT_USER()-based behavior — zero
regression risk for that use case. live_bootstrap.py's run() now
passes role_principals=result["role_principals"] (bootstrap.
run_bootstrap()'s own return value — the exact accounts/hosts it just
created, not re-derived/hardcoded a second time).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_security_selfcheck_regression_test
"""
from __future__ import annotations

from agent.db.sql.security_selfcheck import (
    check_schema_version, check_required_tables, check_required_triggers,
    check_current_grants_against_allowlist, check_named_principal_grants,
    parse_show_grants, run_selfcheck, FORBIDDEN_BY_ROLE,
)
from agent.db.sql.schema import ALL_TABLES_IN_ORDER, SCHEMA_VERSION
from agent.db.sql.security_triggers import immutability_triggers

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


_ALL_TABLE_NAMES = [n for n, _ in ALL_TABLES_IN_ORDER]
_ALL_TRIGGER_NAMES = [n for n, _ in immutability_triggers()]


class ScriptedCursor:
    def __init__(self, conn):
        self.conn = conn
        self._last_sql = None
        self._last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.conn.calls.append((norm, params))
        self._last_sql = norm
        self._last_params = params

    def fetchone(self):
        upper = self._last_sql.upper()
        if "MAX(VERSION)" in upper:
            return {"v": self.conn.schema_version}
        if "INFORMATION_SCHEMA.TABLES" in upper:
            (name,) = self._last_params
            return {"c": 1 if name in self.conn.existing_tables else 0}
        if "INFORMATION_SCHEMA.TRIGGERS" in upper:
            (name,) = self._last_params
            return {"c": 1 if name in self.conn.existing_triggers else 0}
        return None

    def fetchall(self):
        if self._last_sql and self._last_sql.upper().startswith("SHOW GRANTS"):
            if self.conn.principal_grants is not None and self._last_params:
                username = self._last_params[0]
                rows = self.conn.principal_grants.get(username, [])
            else:
                rows = self.conn.show_grants_rows
            return [{"Grants for ...": row} for row in rows]
        return []


class ScriptedConnection:
    def __init__(self, schema_version, existing_tables, existing_triggers, show_grants_rows,
                 principal_grants=None):
        self.calls = []
        self.schema_version = schema_version
        self.existing_tables = set(existing_tables)
        self.existing_triggers = set(existing_triggers)
        self.show_grants_rows = show_grants_rows
        # {username: [raw SHOW GRANTS lines]} — used ONLY by the
        # role_principals= path (SHOW GRANTS FOR %s@%s, a NAMED account,
        # never CURRENT_USER()). None means "not exercised by this
        # scenario" — the CURRENT_USER()-based show_grants_rows above
        # stays the single fallback, matching every pre-existing test
        # in this file exactly.
        self.principal_grants = principal_grants

    def cursor(self):
        return ScriptedCursor(self)


# ============================================================
# parse_show_grants() correctness against realistic MySQL output.
# ============================================================

realistic_rows = [
    "GRANT USAGE ON *.* TO `yandi_runtime`@`%`",
    "GRANT SELECT, INSERT ON `yandi_epistemic`.* TO `yandi_runtime`@`%`",
    "GRANT UPDATE ON `yandi_epistemic`.`belief` TO `yandi_runtime`@`%`",
]
parsed = parse_show_grants(realistic_rows)
check(
    "parse_show_grants(): extracts SELECT/INSERT/UPDATE from realistic multi-line "
    "GRANT output, ignoring the harmless USAGE line",
    set(parsed) == {"USAGE", "SELECT", "INSERT", "UPDATE"},
    f"{parsed}",
)

all_privileges_row = ["GRANT ALL PRIVILEGES ON `yandi_epistemic`.* TO `yandi_bootstrap`@`localhost`"]
parsed_all = parse_show_grants(all_privileges_row)
check(
    "parse_show_grants(): 'ALL PRIVILEGES' parses as a single token (not split "
    "on its internal space into two meaningless fragments)",
    "ALL PRIVILEGES" in parsed_all,
    f"{parsed_all}",
)


# ============================================================
# Clean state -> ok=True.
# ============================================================

clean_conn = ScriptedConnection(
    schema_version=SCHEMA_VERSION,
    existing_tables=_ALL_TABLE_NAMES,
    existing_triggers=_ALL_TRIGGER_NAMES,
    show_grants_rows=[
        "GRANT USAGE ON *.* TO `yandi_runtime`@`%`",
        "GRANT SELECT, INSERT ON `yandi_epistemic`.* TO `yandi_runtime`@`%`",
        "GRANT UPDATE ON `yandi_epistemic`.`belief` TO `yandi_runtime`@`%`",
        "GRANT UPDATE ON `yandi_epistemic`.`semantic_edge` TO `yandi_runtime`@`%`",
        "GRANT UPDATE ON `yandi_epistemic`.`verification_run` TO `yandi_runtime`@`%`",
    ],
)
result_clean = run_selfcheck(clean_conn, role="runtime")
check("clean state: run_selfcheck() reports ok=True", result_clean["ok"] is True, f"{result_clean}")
check("clean state: no missing tables", result_clean["missing_tables"] == [])
check("clean state: no missing triggers", result_clean["missing_triggers"] == [])
check("clean state: no grant violations", result_clean["grant_violations"] == [])


# ============================================================
# Missing table -> detected specifically.
# ============================================================

missing_table_conn = ScriptedConnection(
    schema_version=SCHEMA_VERSION,
    existing_tables=[t for t in _ALL_TABLE_NAMES if t != "recheck_event"],
    existing_triggers=_ALL_TRIGGER_NAMES,
    show_grants_rows=clean_conn.show_grants_rows,
)
result_missing_table = run_selfcheck(missing_table_conn, role="runtime")
check("missing table: run_selfcheck() reports ok=False", result_missing_table["ok"] is False)
check(
    "missing table: 'recheck_event' is named specifically in missing_tables",
    result_missing_table["missing_tables"] == ["recheck_event"],
    f"{result_missing_table['missing_tables']}",
)


# ============================================================
# Missing trigger -> detected specifically.
# ============================================================

missing_trigger_conn = ScriptedConnection(
    schema_version=SCHEMA_VERSION,
    existing_tables=_ALL_TABLE_NAMES,
    existing_triggers=[t for t in _ALL_TRIGGER_NAMES if t != "trg_question_no_update"],
    show_grants_rows=clean_conn.show_grants_rows,
)
result_missing_trigger = run_selfcheck(missing_trigger_conn, role="runtime")
check("missing trigger: run_selfcheck() reports ok=False", result_missing_trigger["ok"] is False)
check(
    "missing trigger: 'trg_question_no_update' is named specifically",
    result_missing_trigger["missing_triggers"] == ["trg_question_no_update"],
    f"{result_missing_trigger['missing_triggers']}",
)


# ============================================================
# Forbidden privilege actually granted -> detected (LIVE counterpart
# of the static privilege regression's proof).
# ============================================================

bad_grants_conn = ScriptedConnection(
    schema_version=SCHEMA_VERSION,
    existing_tables=_ALL_TABLE_NAMES,
    existing_triggers=_ALL_TRIGGER_NAMES,
    show_grants_rows=clean_conn.show_grants_rows + [
        "GRANT DROP ON `yandi_epistemic`.* TO `yandi_runtime`@`%`",  # simulated misconfiguration
    ],
)
result_bad_grants = run_selfcheck(bad_grants_conn, role="runtime")
check(
    "CRITICAL: a forbidden privilege (DROP) actually present in SHOW GRANTS for the "
    "runtime role IS DETECTED, not silently accepted",
    result_bad_grants["ok"] is False and "DROP" in result_bad_grants["grant_violations"],
    f"{result_bad_grants}",
)

all_priv_conn = ScriptedConnection(
    schema_version=SCHEMA_VERSION, existing_tables=_ALL_TABLE_NAMES, existing_triggers=_ALL_TRIGGER_NAMES,
    show_grants_rows=["GRANT ALL PRIVILEGES ON `yandi_epistemic`.* TO `yandi_runtime`@`%`"],
)
result_all_priv = run_selfcheck(all_priv_conn, role="runtime")
check(
    "CRITICAL: 'ALL PRIVILEGES' granted to the runtime role is caught even though "
    "it's not a literal entry in FORBIDDEN_FOR_RUNTIME's short list (treated as "
    "maximally forbidden, not silently ignored on a technicality)",
    result_all_priv["ok"] is False and "ALL PRIVILEGES" in result_all_priv["grant_violations"],
    f"{result_all_priv}",
)


# ============================================================
# Wrong schema version -> detected.
# ============================================================

wrong_version_conn = ScriptedConnection(
    schema_version=SCHEMA_VERSION + 1, existing_tables=_ALL_TABLE_NAMES,
    existing_triggers=_ALL_TRIGGER_NAMES, show_grants_rows=clean_conn.show_grants_rows,
)
result_wrong_version = run_selfcheck(wrong_version_conn, role="runtime")
check(
    "schema version mismatch: run_selfcheck() reports ok=False with a specific detail",
    result_wrong_version["ok"] is False and str(SCHEMA_VERSION) in result_wrong_version["schema_version_detail"],
    f"{result_wrong_version}",
)


# ============================================================
# No silent repair — zero write statements issued anywhere.
# ============================================================

for conn, label in (
    (clean_conn, "clean"), (missing_table_conn, "missing_table"),
    (missing_trigger_conn, "missing_trigger"), (bad_grants_conn, "bad_grants"),
):
    write_calls = [
        sql for sql, _p in conn.calls
        if sql.strip().upper().startswith(("CREATE", "GRANT ", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "REVOKE"))
    ]
    check(
        f"NO SILENT REPAIR (mandate §39): run_selfcheck() against the {label!r} scenario "
        f"issues ZERO write statements — read-only diagnostics only",
        write_calls == [],
        f"{write_calls}",
    )


# ============================================================
# role='readonly' uses the stricter forbidden list.
# ============================================================

readonly_with_insert_conn = ScriptedConnection(
    schema_version=SCHEMA_VERSION, existing_tables=_ALL_TABLE_NAMES, existing_triggers=_ALL_TRIGGER_NAMES,
    show_grants_rows=["GRANT SELECT, INSERT ON `yandi_epistemic`.* TO `yandi_readonly`@`localhost`"],
)
result_readonly = run_selfcheck(readonly_with_insert_conn, role="readonly")
check(
    "role='readonly': INSERT is forbidden for this role (unlike 'runtime', where "
    "INSERT is expected) — detected",
    result_readonly["ok"] is False and "INSERT" in result_readonly["grant_violations"],
    f"{result_readonly}",
)

try:
    run_selfcheck(clean_conn, role="not_a_real_role")
    bad_role_raised = False
except ValueError:
    bad_role_raised = True
check("run_selfcheck() rejects an unrecognized role argument rather than silently defaulting", bad_role_raised)


# ============================================================
# THE LIVE BUG: role_principals= checks NAMED accounts, never
# CURRENT_USER() — root/bootstrap's own grants must never be evaluated
# against a lesser role's deny-list.
# ============================================================

_ROOT_LIKE_GRANTS = [
    "GRANT USAGE ON *.* TO `root`@`localhost`",
    "GRANT CREATE, DROP, RELOAD, SHUTDOWN, PROCESS, FILE, ALTER, SUPER, "
    "EXECUTE, CREATE USER ON *.* TO `root`@`localhost`",
    "GRANT ALL PRIVILEGES ON `yandi_epistemic`.* TO `root`@`localhost`",
]

# 5+9: THE LIVE BUG, reproduced directly — checking root's OWN
# (necessarily privileged) grants against FORBIDDEN_FOR_RUNTIME must
# fail (this is exactly the wrong comparison the live bug made); the
# FIX is that run_selfcheck() must never be ASKED to do this in bootstrap
# mode — role_principals= checks the NAMED yandi_runtime account
# instead, which is what check 6 below proves.
root_forbidden = FORBIDDEN_BY_ROLE["runtime"]
root_conn_for_direct_check = ScriptedConnection(
    schema_version=SCHEMA_VERSION, existing_tables=_ALL_TABLE_NAMES, existing_triggers=_ALL_TRIGGER_NAMES,
    show_grants_rows=_ROOT_LIKE_GRANTS,
)
root_ok_if_misused, root_violations_if_misused = check_current_grants_against_allowlist(
    root_conn_for_direct_check, root_forbidden,
)
check(
    "5/9. THE LIVE BUG, reproduced directly: checking a root-like "
    "CURRENT_USER() connection's grants against FORBIDDEN_FOR_RUNTIME "
    "DOES report violations (proving this was a real, reproducible "
    "false-positive mechanism, not a one-off fluke) — this is exactly "
    "why run_selfcheck() must be given role_principals= at bootstrap "
    "time instead of relying on CURRENT_USER()",
    root_ok_if_misused is False and set(root_violations_if_misused) >= {"CREATE", "SUPER", "CREATE USER"},
    f"{root_violations_if_misused}",
)

# 6/7/8: THE FIX — role_principals= checks each NAMED account against
# its OWN policy, all in ONE run_selfcheck() call, never against
# root's grants.
principal_grants = {
    "yandi_runtime": ["GRANT SELECT, INSERT ON `yandi_epistemic`.* TO `yandi_runtime`@`localhost`"],
    "yandi_readonly": ["GRANT SELECT ON `yandi_epistemic`.* TO `yandi_readonly`@`localhost`"],
    "yandi_migrator": [
        "GRANT CREATE, ALTER, INDEX, REFERENCES, CREATE VIEW, TRIGGER, DROP "
        "ON `yandi_epistemic`.* TO `yandi_migrator`@`localhost`",
    ],
}
bootstrap_conn = ScriptedConnection(
    schema_version=SCHEMA_VERSION, existing_tables=_ALL_TABLE_NAMES, existing_triggers=_ALL_TRIGGER_NAMES,
    show_grants_rows=_ROOT_LIKE_GRANTS,  # what CURRENT_USER() would see if wrongly used
    principal_grants=principal_grants,
)
result_bootstrap = run_selfcheck(
    bootstrap_conn,
    role_principals={
        "runtime": ("yandi_runtime", "localhost"),
        "readonly": ("yandi_readonly", "localhost"),
        "migrator": ("yandi_migrator", "localhost"),
    },
)
check(
    "6. runtime grants are checked EXPLICITLY for the named yandi_runtime "
    "account (SELECT/INSERT only) — clean, not a false positive from "
    "root's grants leaking in",
    result_bootstrap["grants_detail"]["runtime"]["ok"] is True
    and result_bootstrap["grants_detail"]["runtime"]["principal"] == "yandi_runtime@localhost",
    f"{result_bootstrap['grants_detail'].get('runtime')}",
)
check(
    "7. readonly grants are checked SEPARATELY for yandi_readonly "
    "(SELECT only) — its own entry, not merged with runtime's",
    result_bootstrap["grants_detail"]["readonly"]["ok"] is True
    and result_bootstrap["grants_detail"]["readonly"]["principal"] == "yandi_readonly@localhost",
    f"{result_bootstrap['grants_detail'].get('readonly')}",
)
check(
    "8/11. migrator grants are checked SEPARATELY against its OWN policy "
    "(FORBIDDEN_FOR_MIGRATOR) — its legitimate CREATE/ALTER/DROP DDL is "
    "NOT a false-positive 'runtime role violation' (FORBIDDEN_FOR_RUNTIME "
    "would have flagged all three)",
    result_bootstrap["grants_detail"]["migrator"]["ok"] is True
    and result_bootstrap["grants_detail"]["migrator"]["violations"] == [],
    f"{result_bootstrap['grants_detail'].get('migrator')}",
)
check(
    "6b/7b/8b: the overall bootstrap-mode selfcheck is CLEAN "
    "(grants_ok=True, ok=True) once each account is checked against its "
    "own, correctly-scoped policy — this is the live fix end-to-end",
    result_bootstrap["grants_ok"] is True and result_bootstrap["ok"] is True,
    f"{result_bootstrap}",
)
check(
    "role_principals= mode never issues SHOW GRANTS FOR CURRENT_USER() "
    "at all (only the three named-account queries) — root is consulted "
    "only as whatever runs the query, never as a checked principal",
    not any(
        "CURRENT_USER" in sql.upper()
        for sql, _p in bootstrap_conn.calls if sql.upper().startswith("SHOW GRANTS")
    ),
)

# 9: runtime forbidden privilege actually present -> selfcheck FAILS
# even in role_principals= mode (the new path is not accidentally
# more lenient than the CURRENT_USER() path already proven above).
bad_runtime_conn = ScriptedConnection(
    schema_version=SCHEMA_VERSION, existing_tables=_ALL_TABLE_NAMES, existing_triggers=_ALL_TRIGGER_NAMES,
    show_grants_rows=[], principal_grants={
        **principal_grants,
        "yandi_runtime": ["GRANT SELECT, INSERT, DROP ON `yandi_epistemic`.* TO `yandi_runtime`@`localhost`"],
    },
)
result_bad_runtime = run_selfcheck(
    bad_runtime_conn,
    role_principals={"runtime": ("yandi_runtime", "localhost"), "readonly": ("yandi_readonly", "localhost"),
                      "migrator": ("yandi_migrator", "localhost")},
)
check(
    "9. a forbidden privilege (DROP) actually granted to yandi_runtime "
    "IS detected and fails the overall selfcheck, even via the "
    "role_principals= path",
    result_bad_runtime["ok"] is False
    and result_bad_runtime["grants_detail"]["runtime"]["ok"] is False
    and "DROP" in result_bad_runtime["grants_detail"]["runtime"]["violations"],
    f"{result_bad_runtime['grants_detail'].get('runtime')}",
)

# 10: readonly write privilege -> FAIL, same guarantee via role_principals=.
bad_readonly_conn = ScriptedConnection(
    schema_version=SCHEMA_VERSION, existing_tables=_ALL_TABLE_NAMES, existing_triggers=_ALL_TRIGGER_NAMES,
    show_grants_rows=[], principal_grants={
        **principal_grants,
        "yandi_readonly": ["GRANT SELECT, INSERT ON `yandi_epistemic`.* TO `yandi_readonly`@`localhost`"],
    },
)
result_bad_readonly = run_selfcheck(
    bad_readonly_conn,
    role_principals={"runtime": ("yandi_runtime", "localhost"), "readonly": ("yandi_readonly", "localhost"),
                      "migrator": ("yandi_migrator", "localhost")},
)
check(
    "10. a write privilege (INSERT) actually granted to yandi_readonly "
    "IS detected and fails the overall selfcheck via role_principals=",
    result_bad_readonly["ok"] is False
    and result_bad_readonly["grants_detail"]["readonly"]["ok"] is False
    and "INSERT" in result_bad_readonly["grants_detail"]["readonly"]["violations"],
    f"{result_bad_readonly['grants_detail'].get('readonly')}",
)

# Unknown role name inside role_principals= -> explicit error, never a
# silently-skipped check.
try:
    run_selfcheck(bootstrap_conn, role_principals={"nonexistent_role": ("someone", "localhost")})
    bad_role_principal_raised = False
except ValueError:
    bad_role_principal_raised = True
check(
    "run_selfcheck() rejects an unrecognized role name inside "
    "role_principals= rather than silently skipping that account's check",
    bad_role_principal_raised,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
