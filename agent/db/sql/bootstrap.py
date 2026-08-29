"""
agent/db/sql/bootstrap.py — Этап 5E-S S3: zero-config database
bootstrap (mandate §11).

DESIGNED, NOT EXECUTED against any live server in this pass — see
SECURITY_ARCHITECTURE.md §4/§9. No YANDI_BOOTSTRAP-capable credential
exists in this environment (verified: `sudo -n mysql` fails, no
passwordless local admin path), and this machine's Percona instance is
a SHARED, FastPanel-managed one (§4) — running this for real requires
an explicit owner decision this pass does not make on its own.

Every function here is unit-tested for IDEMPOTENCY LOGIC against a
stateful fake connection (calling run_bootstrap() twice must not
attempt to create duplicate users/triggers) — never against a real
server.

Flow (mandate §11): ensure_database() -> ensure_role() x3 ->
apply_schema() (delegates to the EXISTING agent.db.sql.migrate module,
not duplicated) -> apply_immutability_triggers() -> (verify_grants()/
run_security_smoke_test() — see SECURITY_ARCHITECTURE.md §21 for why
those are the NEXT continuation, not built this pass).

Key generation (keys.py::generate_kek()) is DELIBERATELY NOT called
anywhere in this file — mandate §37: generating a key creates a backup
obligation a human operator must consciously accept, never a silent
side effect of running an installer.

YANDI_BOOTSTRAP's OWN account creation is likewise out of scope for
this module — the caller is assumed to already hold a bootstrap-
capable connection (however that was obtained is an operational
decision, not something this Python module decides); this module only
uses that connection to create the THREE LESSER roles + schema +
triggers, then hands back control for the caller to retire the
bootstrap credential (revoke_bootstrap()).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from agent.db.sql.schema import ALL_TABLES_IN_ORDER, ALTER_STATEMENTS_IN_ORDER
from agent.db.sql.security_grants import (
    DATABASE_NAME, revoke_all_statement,
    yandi_migrator_statements, yandi_readonly_statements, yandi_runtime_statements,
    yandi_runtime_auth_socket_statement, yandi_runtime_grant_statements,
)
from agent.db.sql.instance_identity import record_instance_identity
from agent.db.sql.security_triggers import immutability_triggers


_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def ensure_database(conn, database_name: str = DATABASE_NAME) -> None:
    """CREATE DATABASE IF NOT EXISTS — safe to call any number of
    times. utf8mb4 matches every table's own DEFAULT CHARSET in
    schema.py.

    MySQL identifiers (database/table names) cannot be parameterized
    at all — %s binding only works for VALUES, never identifiers, a
    real driver/protocol limitation, not a choice. `database_name`
    defaults to the hardcoded DATABASE_NAME constant and every current
    caller relies on that default, but this function accepts it as a
    parameter — so it validates the identifier shape itself (mandate
    §15: "Dynamic identifiers... ТОЛЬКО из hardcoded allow-list") rather
    than trusting every future caller to remember never to pass
    anything else through it."""
    if not _IDENTIFIER_RE.match(database_name):
        raise ValueError(
            f"refusing to use {database_name!r} as a database identifier — "
            f"must match {_IDENTIFIER_RE.pattern} (defense against identifier injection)"
        )
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{database_name}` "
            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )


def user_exists(conn, username: str, host: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM mysql.user WHERE User=%s AND Host=%s", (username, host))
        row = cur.fetchone()
        return bool(row and row.get("c"))


def ensure_role(conn, statements: List[Tuple[str, tuple]]) -> None:
    """Executes a list of (sql, params) statements. `CREATE USER IF NOT
    EXISTS` makes account creation idempotent; re-issuing the SAME
    GRANT in MySQL is always a safe no-op (granting an already-held
    privilege changes nothing) — no extra existence-check needed for
    GRANT statements themselves, unlike triggers (see
    apply_immutability_triggers())."""
    for sql, params in statements:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def apply_schema(conn) -> None:
    """Delegates to agent.db.sql.schema's own table/ALTER definitions —
    the SAME ones agent.db.sql.migrate.apply() uses. Not duplicated:
    this function and migrate.apply() both read from schema.py's single
    source of truth (mandate's own "no parallel truths" discipline,
    already established earlier in Этап 5)."""
    for _name, ddl in ALL_TABLES_IN_ORDER:
        with conn.cursor() as cur:
            cur.execute(ddl)
    for _name, ddl in ALTER_STATEMENTS_IN_ORDER:
        with conn.cursor() as cur:
            try:
                cur.execute(ddl)
            except Exception as e:
                if "Duplicate" not in str(e) and "already exists" not in str(e).lower():
                    raise


def trigger_exists(conn, trigger_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM information_schema.triggers "
            "WHERE trigger_schema = DATABASE() AND trigger_name = %s",
            (trigger_name,),
        )
        row = cur.fetchone()
        return bool(row and row.get("c"))


def apply_immutability_triggers(conn) -> List[str]:
    """Idempotent via an EXPLICIT existence check against information_
    schema.triggers — deliberately NOT relying on `CREATE TRIGGER IF
    NOT EXISTS` syntax, whose availability across this specific Percona
    8.0.46 build was not independently verified in this pass (no live
    server to test against). Mirrors the same explicit-check pattern
    agent/db/sql/migrate.py already established for schema_migrations,
    rather than assuming untested syntax works (mandate §55: don't
    claim proof that wasn't obtained).

    Returns the list of trigger names actually created by THIS call —
    a second call against the same database returns an empty list."""
    created: List[str] = []
    for trigger_name, ddl in immutability_triggers():
        if trigger_exists(conn, trigger_name):
            continue
        with conn.cursor() as cur:
            cur.execute(ddl)
        created.append(trigger_name)
    return created


def revoke_bootstrap(conn, username: str, host: str) -> None:
    """Mandate §10.1: "После установки: DROP / REVOKE / убрать
    credential." Retires the YANDI_BOOTSTRAP account. This function's
    own execution against a real server is exactly as BLOCKED as
    everything else in this module this pass — no bootstrap has run
    against a live server yet, so there is nothing real to revoke."""
    sql, params = revoke_all_statement(username, host)
    with conn.cursor() as cur:
        cur.execute(sql, params)


def run_bootstrap(
    conn, *, readonly_password: str, migrator_password: str, runtime_password: str = "",
    runtime_host: str = "%", readonly_host: str = "localhost", migrator_host: str = "localhost",
    runtime_auth_socket_os_user: str = None,
    instance_uuid: str = None, instance_created_by_host: str = None,
) -> Dict[str, Any]:
    """The full flow, minus YANDI_BOOTSTRAP's own account creation —
    the caller is assumed to already be connected AS a bootstrap-
    capable account (mandate §10.1: that identity's lifecycle is
    managed by whatever invoked this, not by this function).

    Passwords are explicit arguments — this function never generates or
    defaults one (mandate §52: no credential guessing). It also never
    logs them: every password value here only ever flows into a
    `%s`-bound SQL parameter (see security_grants.py)
    (agent/db_sql_security_bootstrap_regression_test.py greps this
    module for exactly that property).

    `runtime_auth_socket_os_user` (DATABASE BOOTSTRAP V1, mandate §11):
    when given, YANDI_RUNTIME is created with `auth_socket` instead of a
    password (DEDICATED_INSTANCE_DESIGN.md §H Option 1) —
    `runtime_password`/`runtime_host` are then ignored entirely for that
    role (auth_socket is inherently 'localhost'-only, see
    security_grants.yandi_runtime_auth_socket_statement()'s docstring).
    Leaving this None preserves the original password-only behavior
    exactly — existing callers/tests are unaffected.

    `instance_uuid` (mandate §4/§27): when given, also records the
    single instance_identity row via instance_identity.record_
    instance_identity() — idempotent (a second call with the SAME uuid
    is a no-op; a DIFFERENT uuid raises rather than silently
    overwriting, see that function's own docstring). Left None, no
    identity row is touched (a caller doing a plain schema-only
    bootstrap does not have to reason about identity at all).

    Idempotent end-to-end: calling this twice against the SAME database
    creates zero duplicate users, zero duplicate tables (CREATE TABLE
    IF NOT EXISTS), and zero duplicate triggers (apply_immutability_
    triggers()'s own explicit check)."""
    if not runtime_auth_socket_os_user and not runtime_password:
        raise ValueError(
            "run_bootstrap() needs either runtime_password (password auth) or "
            "runtime_auth_socket_os_user (auth_socket auth) for YANDI_RUNTIME — "
            "refusing to create it with an empty password by omission."
        )
    ensure_database(conn)
    if runtime_auth_socket_os_user:
        ensure_role(conn, [yandi_runtime_auth_socket_statement("yandi_runtime", runtime_auth_socket_os_user)]
                    + yandi_runtime_grant_statements("yandi_runtime", "localhost"))
    else:
        ensure_role(conn, yandi_runtime_statements("yandi_runtime", runtime_host, runtime_password))
    ensure_role(conn, yandi_readonly_statements("yandi_readonly", readonly_host, readonly_password))
    ensure_role(conn, yandi_migrator_statements("yandi_migrator", migrator_host, migrator_password))
    apply_schema(conn)
    triggers_created = apply_immutability_triggers(conn)
    if instance_uuid:
        record_instance_identity(conn, instance_uuid, created_by_host=instance_created_by_host)
    return {
        "database": DATABASE_NAME,
        "roles_ensured": ["yandi_runtime", "yandi_readonly", "yandi_migrator"],
        "triggers_created": triggers_created,
        "runtime_auth_mode": "auth_socket" if runtime_auth_socket_os_user else "password",
    }
