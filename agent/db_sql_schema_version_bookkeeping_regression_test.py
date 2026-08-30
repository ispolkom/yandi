"""
agent/db_sql_schema_version_bookkeeping_regression_test.py — DATABASE
BOOTSTRAP V1, fifteenth Phase B attempt: schema_migrations version
bookkeeping.

EXACT LIVE SYMPTOM (owner-run, plain `--database-only`, no reinit —
auth_socket bootstrap + schema/role/grant/trigger creation all
succeeded: tables_ok=True, triggers_ok=True, identity_ok=True MATCH,
triggers_created=40):

    selfcheck_ok=False
    schema_version_ok=False
    schema_version_detail: 'schema_migrations reports version None,
    expected 1'

ROOT CAUSE: agent.db.sql.bootstrap.apply_schema() creates every table
(CREATE TABLE IF NOT EXISTS, from schema.ALL_TABLES_IN_ORDER — the
SAME single-source-of-truth list agent.db.sql.migrate.py's own apply()
already uses) but NEVER recorded the schema_migrations row itself. That
INSERT IGNORE lived ONLY inside migrate.apply() — which is never called
by the bootstrap flow (migrate.apply() opens its OWN connection via
connection.get_connection()'s env-var credentials; bootstrap.py
receives an already-open root/auth_socket connection instead, and
reuses it for everything). So a virgin bootstrap left schema_migrations
genuinely empty forever: `SELECT MAX(version) FROM schema_migrations`
on zero rows returns NULL — reported as version 'None'.

FIX: extracted the INSERT IGNORE into agent.db.sql.migrate.
record_schema_version(conn, version=SCHEMA_VERSION, description=...) —
ONE function, used by BOTH migrate.apply() (unchanged behavior for its
own existing callers) and bootstrap.apply_schema() (new call, right
after its CREATE TABLE/ALTER loop completes without raising — the same
"successful, non-raising completion of every DDL statement IS the
proof of correct application" precedent migrate.apply() itself already
established, not a new/stricter rule invented just for this pass).
`version INT PRIMARY KEY` in schema.SCHEMA_MIGRATIONS makes IGNORE the
correct idempotency primitive: a second recording of the SAME version
is a silent no-op, never a duplicate row or an error.

Uses a real, stateful fake connection (not a static grep) — genuinely
tracks which tables/schema_migrations rows "exist" across calls, so
idempotency and the "claimed version but incomplete schema" failure
mode are both provable, not just asserted.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_schema_version_bookkeeping_regression_test
"""
from __future__ import annotations

import re

import agent.db.sql.bootstrap as bootstrap_mod
import agent.db.sql.migrate as migrate_mod
from agent.db.sql.bootstrap import apply_schema
from agent.db.sql.schema import ALL_TABLES_IN_ORDER, SCHEMA_VERSION
from agent.db.sql.security_selfcheck import check_schema_version, check_required_tables

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


class SchemaVersionFakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        upper = norm.upper()
        self.conn.calls.append(norm)
        self._result = None

        if upper.startswith("CREATE TABLE"):
            m = re.search(r"CREATE TABLE IF NOT EXISTS\s+`?(\w+)`?", norm, re.IGNORECASE)
            if m:
                self.conn.tables.add(m.group(1))
        elif upper.startswith("INSERT IGNORE INTO SCHEMA_MIGRATIONS"):
            version, description = params
            # Real MySQL semantics for `version INT PRIMARY KEY` + IGNORE:
            # a duplicate-PK insert is a silent no-op, the ORIGINAL row
            # (including its original description/applied_at) survives.
            if version not in self.conn.schema_migrations:
                self.conn.schema_migrations[version] = description
        elif "MAX(VERSION)" in upper:
            self._result = {
                "v": max(self.conn.schema_migrations) if self.conn.schema_migrations else None,
            }
        elif "INFORMATION_SCHEMA.TABLES" in upper:
            (name,) = params
            self._result = {"c": 1 if name in self.conn.tables else 0}

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []


class SchemaVersionFakeConnection:
    def __init__(self):
        self.calls = []
        self.tables = set()
        self.schema_migrations = {}

    def cursor(self):
        return SchemaVersionFakeCursor(self)


# ============================================================
# 0. NOT DUPLICATED: bootstrap.py imports the EXACT SAME function
# migrate.py's own apply() uses — one source of truth for "how a
# schema version gets recorded," never two independently-maintained
# INSERT statements that could drift apart.
# ============================================================
check(
    "0. bootstrap.record_schema_version IS migrate.record_schema_version "
    "(same function object, imported — not a second, parallel implementation)",
    bootstrap_mod.record_schema_version is migrate_mod.record_schema_version,
)

# ============================================================
# 1. THE LIVE BUG: virgin schema bootstrap (apply_schema() against an
# empty fake) -> schema_migrations ends up with version == SCHEMA_VERSION,
# not left empty/None.
# ============================================================
conn = SchemaVersionFakeConnection()
apply_schema(conn)

check(
    "1. THE LIVE BUG: after apply_schema() against a virgin database, "
    "schema_migrations actually holds SCHEMA_VERSION as a recorded row "
    "(not left empty — the live 'version None, expected 1' symptom)",
    conn.schema_migrations.get(SCHEMA_VERSION) is not None,
    f"schema_migrations={conn.schema_migrations!r}",
)

version_ok, version_detail = check_schema_version(conn)
check(
    "1b. check_schema_version() now reports ok=True against the freshly "
    "bootstrapped fake (this is EXACTLY the live selfcheck field that "
    "reported False before this fix)",
    version_ok is True,
    f"detail={version_detail!r}",
)

tables_ok, missing = check_required_tables(conn)
check(
    "1c. every table in schema.ALL_TABLES_IN_ORDER was actually created "
    "(the version row is not recorded in place of real tables, alongside them)",
    tables_ok,
    f"missing={missing}",
)

# ============================================================
# 2. Rerun -> idempotent: no duplicate row, no destructive migration,
# same version still reported.
# ============================================================
calls_before_rerun = len(conn.calls)
apply_schema(conn)

check(
    "2a. a second apply_schema() call does not create a SECOND "
    "schema_migrations row for the same version (still exactly one entry)",
    conn.schema_migrations == {SCHEMA_VERSION: conn.schema_migrations[SCHEMA_VERSION]}
    and len(conn.schema_migrations) == 1,
    f"schema_migrations={conn.schema_migrations!r}",
)
version_ok_2, _ = check_schema_version(conn)
check(
    "2b. check_schema_version() still reports ok=True after the rerun "
    "(idempotent — matches CREATE TABLE IF NOT EXISTS's own idempotency, "
    "not a one-shot fluke)",
    version_ok_2 is True,
)
check(
    "2c. the rerun actually re-executed the full DDL/INSERT IGNORE "
    "sequence (proving idempotency was CHECKED, not skipped outright — "
    "the call count increased, it did not merely no-op the whole function)",
    len(conn.calls) > calls_before_rerun,
)


# ============================================================
# 3. Version NEWER than this codebase supports -> FAIL CLOSED, never
# silently treated as compatible/ahead.
# ============================================================
newer_conn = SchemaVersionFakeConnection()
newer_conn.schema_migrations[SCHEMA_VERSION + 1] = "a future migration this codebase doesn't know about"
version_ok_newer, detail_newer = check_schema_version(newer_conn, expected_version=SCHEMA_VERSION)
check(
    "3. schema_migrations reporting a NEWER version than this codebase's "
    "SCHEMA_VERSION is NOT silently accepted as 'fine, it's ahead' — "
    "still fails closed",
    version_ok_newer is False and str(SCHEMA_VERSION + 1) in detail_newer,
    f"ok={version_ok_newer} detail={detail_newer!r}",
)


# ============================================================
# 4. Claimed version 1 but the schema is actually INCOMPLETE -> the
# combination (version_ok AND tables_ok) must still catch it — a
# recorded version number is never, by itself, treated as proof the
# schema is really there.
# ============================================================
incomplete_conn = SchemaVersionFakeConnection()
incomplete_conn.schema_migrations[SCHEMA_VERSION] = "claims to be applied"
incomplete_conn.tables = {"schema_migrations", "question"}  # far short of all 21

version_ok_incomplete, _ = check_schema_version(incomplete_conn)
tables_ok_incomplete, missing_incomplete = check_required_tables(incomplete_conn)
check(
    "4. a database that CLAIMS schema_version=1 but is missing most "
    "required tables is still caught — check_schema_version() alone "
    "reporting True is never sufficient; check_required_tables() "
    "independently catches the incomplete schema",
    version_ok_incomplete is True and tables_ok_incomplete is False and len(missing_incomplete) > 0,
    f"version_ok={version_ok_incomplete} tables_ok={tables_ok_incomplete} "
    f"missing_count={len(missing_incomplete)}",
)


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
