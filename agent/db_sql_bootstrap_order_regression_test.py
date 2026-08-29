"""
agent/db_sql_bootstrap_order_regression_test.py — DATABASE BOOTSTRAP V1,
thirteenth Phase B attempt: run_bootstrap()'s internal statement ORDER.

EXACT LIVE FAILURE (owner-run, --reinitialize-empty-instance, against a
genuinely fresh, empty datadir — auth_socket bootstrap already proven
working: TEMP_SOURCE_FP == MARKER_FP == PYTHON_FP,
MYSQL_AUTH=TEMP_PASSWORD_AUTH_OK_BUT_PASSWORD_EXPIRED,
AUTH_SOCKET_PLUGIN=AUTH_SOCKET_PLUGIN_INSTALLED, AUTH_SOCKET_READY):

    File "agent/db/sql/bootstrap.py", line 208, in run_bootstrap
      ensure_role(conn, [yandi_runtime_auth_socket_statement(...)]
                  + yandi_runtime_grant_statements("yandi_runtime", ...))
    File "agent/db/sql/bootstrap.py", line 96, in ensure_role
      cur.execute(sql, params)
    pymysql.err.ProgrammingError: (1146, "Table 'yandi_epistemic.belief'
    doesn't exist")

ROOT CAUSE (confirmed by reading the actual source, not guessed):
run_bootstrap()'s OLD order was ensure_database() -> ensure_role() x3
-> apply_schema() -> apply_immutability_triggers(). YANDI_RUNTIME's own
grant statements (security_grants.yandi_runtime_grant_statements())
include PER-TABLE `GRANT UPDATE ON db.<table>` for every class-C/D
table (`belief`, `semantic_edge`, `verification_run` —
security_grants.CLASS_C_TABLES + CLASS_D_TABLES). Unlike a `db.*`
wildcard GRANT, a table-scoped GRANT requires MySQL to resolve the
named table, which does not exist yet the very first time this runs
against an empty database — apply_schema() (the only thing that
creates tables) ran AFTER all three ensure_role() calls. `belief` is
the FIRST class-C table in schema.ALL_TABLES_IN_ORDER, which is why it
is the one named in the live error.

NOT a stale-table-name bug: `belief` is confirmed live/current — it is
schema.py's own BELIEF DDL (line ~339), present in
schema.ALL_TABLES_IN_ORDER, and classified "C" in
schema.TABLE_CLASSIFICATION. Check 5 below proves this directly against
the actual schema.py source so this can never silently regress into a
"just rename the grant target" style non-fix.

FIX: apply_schema() now runs immediately after ensure_database(), and
before any ensure_role() call — see bootstrap.py's own inline comment
at that call site. Nothing else in the sequence changed: triggers
(which also need tables to exist) and record_instance_identity() (which
needs the instance_identity table, itself one of ALL_TABLES_IN_ORDER)
were already positioned after schema creation and stay there.

This file's central tool is a STRICTER stateful fake connection than
agent/db_sql_security_bootstrap_regression_test.py's — that one already
existed and tracks *idempotency* (has this been created before?) but
NEVER checked *dependency* (does the referenced table exist yet?), which
is exactly why it did not catch this live bug. OrderCheckingFakeCursor
below raises a RuntimeError, shaped like MySQL's own 1146, the instant
any per-table GRANT or CREATE TRIGGER references a table not yet
created — turning a silent ordering assumption into an immediate,
loud test failure if this ever regresses.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_bootstrap_order_regression_test
"""
from __future__ import annotations

import re

from agent.db.sql.bootstrap import run_bootstrap
from agent.db.sql.schema import ALL_TABLES_IN_ORDER, SCHEMA_VERSION, TABLE_CLASSIFICATION
from agent.db.sql.security_grants import DATABASE_NAME, CLASS_C_TABLES, CLASS_D_TABLES
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


_TABLE_SCOPED_GRANT_RE = re.compile(r"ON `[^`]+`\.`([^`]+)`")
_TABLE_SCOPED_TRIGGER_RE = re.compile(r"(?:BEFORE|AFTER)\s+\w+\s+ON\s+`([^`]+)`")


class OrderViolation(RuntimeError):
    """Raised in place of pymysql's real (1146, "Table ... doesn't
    exist") — same shape of failure, no live server required."""


class OrderCheckingFakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm_sql = " ".join(sql.split())
        self.conn.calls.append(norm_sql)
        upper = norm_sql.strip().upper()

        if upper.startswith("CREATE DATABASE"):
            self.conn.database_created = True
        elif upper.startswith("CREATE TABLE"):
            # "CREATE TABLE IF NOT EXISTS `name` (" / "CREATE TABLE IF
            # NOT EXISTS name (" — schema.py's DDL uses bare identifiers.
            m = re.search(r"CREATE TABLE IF NOT EXISTS\s+`?(\w+)`?", norm_sql, re.IGNORECASE)
            if m:
                self.conn.tables.add(m.group(1))
        elif upper.startswith("GRANT"):
            m = _TABLE_SCOPED_GRANT_RE.search(norm_sql)
            if m:
                table = m.group(1)
                if table not in self.conn.tables:
                    raise OrderViolation(
                        f"(1146, \"Table '{DATABASE_NAME}.{table}' doesn't exist\") — "
                        f"GRANT issued before CREATE TABLE for {table!r}: {norm_sql!r}"
                    )
        elif upper.startswith("CREATE TRIGGER"):
            m = _TABLE_SCOPED_TRIGGER_RE.search(norm_sql)
            if m:
                table = m.group(1)
                if table not in self.conn.tables:
                    raise OrderViolation(
                        f"(1146, \"Table '{DATABASE_NAME}.{table}' doesn't exist\") — "
                        f"CREATE TRIGGER issued before CREATE TABLE for {table!r}: {norm_sql!r}"
                    )
            self.conn.triggers.add(norm_sql.split()[2])
        elif "INFORMATION_SCHEMA.TRIGGERS" in upper:
            (trigger_name,) = params
            self._result = {"c": 1 if trigger_name in self.conn.triggers else 0}
            return
        elif upper.startswith("SELECT COUNT(*) AS C FROM MYSQL.USER"):
            self._result = {"c": 0}
            return
        elif upper.startswith("INSERT") and "INSTANCE_IDENTITY" in upper:
            if "instance_identity" not in self.conn.tables:
                raise OrderViolation(
                    "INSERT into instance_identity issued before its CREATE TABLE"
                )
            self.conn.instance_identity_rows.append(params)
        elif upper.startswith("SELECT") and "INSTANCE_IDENTITY" in upper:
            self._result = None
            return
        self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []


class OrderCheckingFakeConnection:
    def __init__(self):
        self.calls = []
        self.database_created = False
        self.tables = set()
        self.triggers = set()
        self.instance_identity_rows = []

    def cursor(self):
        return OrderCheckingFakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ============================================================
# 1. THE LIVE BUG ITSELF: bootstrap against a virgin, empty database
# must not raise OrderViolation — every table-scoped GRANT/TRIGGER must
# come after that table's own CREATE TABLE.
# ============================================================
conn = OrderCheckingFakeConnection()
try:
    result = run_bootstrap(
        conn,
        readonly_password="pw-readonly",
        migrator_password="pw-migrator",
        runtime_auth_socket_os_user="yandi-agent",
        instance_uuid="d7cf261b-6534-4261-a6d8-5497397118bb",
        instance_created_by_host="test-host",
    )
    order_error = None
except OrderViolation as e:
    result = None
    order_error = e

check(
    "1. THE LIVE BUG: run_bootstrap() against a virgin empty database does NOT "
    "raise a table-doesn't-exist error — every schema table is created before any "
    "statement that references it (per-table GRANT or CREATE TRIGGER)",
    order_error is None,
    f"{order_error}",
)

# ============================================================
# 2. All expected tables were actually created (apply_schema() really
# ran, not just skipped/no-op'd).
# ============================================================
_expected_table_names = {name for name, _ddl in ALL_TABLES_IN_ORDER}
check(
    "2. every table in schema.ALL_TABLES_IN_ORDER was created during bootstrap",
    conn.tables == _expected_table_names,
    f"missing={_expected_table_names - conn.tables} extra={conn.tables - _expected_table_names}",
)

# ============================================================
# 3. Expected table count / schema version — the single source of
# truth this bootstrap flow actually reads from (no second schema
# definition introduced by this fix).
# ============================================================
check(
    "3a. schema.ALL_TABLES_IN_ORDER currently defines exactly 21 tables "
    "(20 domain/history/projection tables + instance_identity)",
    len(ALL_TABLES_IN_ORDER) == 21,
    f"actual={len(ALL_TABLES_IN_ORDER)}",
)
check(
    "3b. SCHEMA_VERSION is defined and non-empty (this bootstrap path itself "
    "does not record it into schema_migrations — that remains agent.db.sql."
    "migrate.py's job, run separately; out of scope for this order-only fix)",
    bool(SCHEMA_VERSION),
    f"SCHEMA_VERSION={SCHEMA_VERSION!r}",
)

# ============================================================
# 4. Explicit ORDER assertion on the raw call log: apply_schema()'s
# CREATE TABLE statements come strictly before ANY GRANT/CREATE USER
# statement — not just "no crash happened to occur", but the actual
# sequence the fix mandates.
# ============================================================
_first_grant_or_user_idx = next(
    (i for i, sql in enumerate(conn.calls) if sql.upper().startswith(("GRANT", "CREATE USER"))),
    None,
)
_last_create_table_idx = max(
    (i for i, sql in enumerate(conn.calls) if sql.upper().startswith("CREATE TABLE")),
    default=-1,
)
check(
    "4. the LAST CREATE TABLE statement occurs strictly before the FIRST "
    "GRANT/CREATE USER statement in the actual call sequence",
    _first_grant_or_user_idx is not None
    and _last_create_table_idx != -1
    and _last_create_table_idx < _first_grant_or_user_idx,
    f"last_create_table_idx={_last_create_table_idx} "
    f"first_grant_or_user_idx={_first_grant_or_user_idx}",
)

# ============================================================
# 5. NOT a stale-table-name bug: `belief` is proven live/current in the
# actual schema.py source — schema.ALL_TABLES_IN_ORDER,
# TABLE_CLASSIFICATION, and security_grants.CLASS_C_TABLES must all
# agree it exists and is classified "C".
# ============================================================
check(
    "5a. `belief` is a real, current table in schema.ALL_TABLES_IN_ORDER "
    "(not a legacy/renamed identifier)",
    "belief" in _expected_table_names,
)
check(
    "5b. `belief` is classified \"C\" in schema.TABLE_CLASSIFICATION, which is "
    "exactly why security_grants.py grants it a per-table UPDATE (class C = "
    "derived projection, UPDATE is legitimate) — this is the single source of "
    "truth security_grants.py reads from, not a second/duplicated classification",
    TABLE_CLASSIFICATION.get("belief") == "C",
)
check(
    "5c. security_grants.CLASS_C_TABLES (what actually drives the per-table "
    "GRANT UPDATE statements) contains `belief`, sourced from the same "
    "TABLE_CLASSIFICATION dict — confirming the dependency is real, not stale",
    "belief" in CLASS_C_TABLES,
)

# ============================================================
# 6. Second run against the SAME (now-populated) fake — idempotent,
# still no order violation, zero duplicate tables/triggers.
# ============================================================
tables_before = set(conn.tables)
triggers_before = set(conn.triggers)
try:
    run_bootstrap(
        conn,
        readonly_password="pw-readonly-2",
        migrator_password="pw-migrator-2",
        runtime_auth_socket_os_user="yandi-agent",
        instance_uuid="d7cf261b-6534-4261-a6d8-5497397118bb",
        instance_created_by_host="test-host",
    )
    second_run_error = None
except OrderViolation as e:
    second_run_error = e

check(
    "6a. a second run_bootstrap() call against an already-bootstrapped database "
    "succeeds with no order violation (auth_socket root path / instance UUID / "
    "reinit guard are untouched by this fix — this only proves the SQL order "
    "fix itself is idempotent)",
    second_run_error is None,
    f"{second_run_error}",
)
check(
    "6b. the second run creates ZERO new tables/triggers (CREATE TABLE IF NOT "
    "EXISTS + explicit trigger-existence check both still hold)",
    conn.tables == tables_before and conn.triggers == triggers_before,
)

# ============================================================
# 7. Runtime role creation strictly requires schema prerequisites: a
# GRANT naming a table NOT in schema.ALL_TABLES_IN_ORDER at all must
# still be rejected by the fake (proves the check itself is real, not
# vacuously true because every table always happens to exist by then).
# ============================================================
canary_conn = OrderCheckingFakeConnection()
canary_cursor = canary_conn.cursor()
raised = False
try:
    with canary_cursor as cur:
        cur.execute(f"GRANT UPDATE ON `{DATABASE_NAME}`.`nonexistent_table` TO %s@%s", ("x", "y"))
except OrderViolation:
    raised = True
check(
    "7. the OrderCheckingFakeCursor genuinely enforces the dependency (a GRANT "
    "on a table that was never created DOES raise) — the harness itself is a "
    "valid test, not a fake that always passes",
    raised,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
