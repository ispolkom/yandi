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

Flow (mandate §11): ensure_database() -> apply_schema() (delegates to
the EXISTING agent.db.sql.migrate module's own table definitions, not
duplicated) -> ensure_role() x3 -> apply_immutability_triggers() ->
(verify_grants()/run_security_smoke_test() — see
SECURITY_ARCHITECTURE.md §21 for why those are the NEXT continuation,
not built this pass). apply_schema() runs BEFORE any ensure_role()
call: YANDI_RUNTIME's grants include per-table `GRANT UPDATE ON
db.<table>` statements (class C/D tables only), and a table-level GRANT
requires the table to already exist — live-confirmed the hard way
(ERROR 1146 on `belief` against a virgin database) before this
ordering was fixed. See run_bootstrap()'s own inline comment.

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
    yandi_readonly_auth_socket_statement, yandi_readonly_grant_statements,
)
from agent.db.sql.instance_identity import record_instance_identity
from agent.db.sql.security_triggers import immutability_triggers
from agent.db.sql.migrate import record_schema_version


_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def ensure_database(conn, database_name: str = DATABASE_NAME) -> None:
    """CREATE DATABASE IF NOT EXISTS, then USE it — safe to call any
    number of times. utf8mb4 matches every table's own DEFAULT CHARSET
    in schema.py.

    The USE is not cosmetic: apply_schema()'s CREATE TABLE statements
    are bare, unqualified identifiers (schema.py's single source of
    truth is shared with agent.db.sql.migrate.py, which instead relies
    on agent.db.sql.connection.get_connection()'s own `database=`
    argument — not available here, since this module's caller connects
    BEFORE the database necessarily exists). Without an explicit USE
    after creating it, the very next bare CREATE TABLE fails with
    (1046, "No database selected") — live-confirmed.

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
        # CREATE DATABASE does not itself select it for this session —
        # live-confirmed (1046, "No database selected") the moment
        # apply_schema() tried its first bare, unqualified CREATE TABLE
        # right after this call. The connection this module receives
        # (live_bootstrap._connect_as_root_auth_socket()) is opened with
        # NO `database=` argument on purpose — on a virgin instance
        # `yandi_epistemic` doesn't exist yet at connect time, so it
        # can't be selected up front the way agent.db.sql.connection.
        # get_connection() does for the already-bootstrapped case. `USE`
        # is safe here: `database_name` was already validated above by
        # _IDENTIFIER_RE, the same guard the CREATE DATABASE statement
        # above relies on.
        cur.execute(f"USE `{database_name}`")


def user_exists(conn, username: str, host: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM mysql.user WHERE User=%s AND Host=%s", (username, host))
        row = cur.fetchone()
        return bool(row and row.get("c"))


def auth_socket_binding_matches(conn, username: str, host: str, os_user: str) -> bool:
    """True iff `username`@`host` already exists AND is bound via the
    auth_socket plugin to EXACTLY `os_user`. False for "doesn't exist
    yet" (ensure_role()'s own CREATE USER IF NOT EXISTS handles that
    case) AND for "exists but bound to a DIFFERENT OS user" (needs
    rebinding — see run_bootstrap()'s own call site).

    DRIFT DETECTION ("10-year bastion" OS-identity separation, mandate:
    yandi_runtime must be reachable ONLY from the dedicated AGENT_OS_USER
    identity, never an interactive owner/Claude/Codex login):
    `CREATE USER IF NOT EXISTS` is a true no-op against an
    ALREADY-EXISTING account, INCLUDING its auth_socket binding — so
    changing AGENT_OS_USER in deploy/install-yandi.sh alone would
    silently leave an already-bootstrapped yandi_runtime permanently
    bound to the OLD OS user forever. Same class of gap already fixed
    twice this pass for my.cnf (install_config()) and trigger bodies
    (apply_immutability_triggers()) — existence is not enough, live
    content must be compared. `authentication_string` is where
    auth_socket stores the mapped OS username (this plugin has no
    password hash to check instead)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT plugin, authentication_string FROM mysql.user "
            "WHERE User=%s AND Host=%s",
            (username, host),
        )
        row = cur.fetchone()
    if not row:
        return False
    return row.get("plugin") == "auth_socket" and row.get("authentication_string") == os_user


def rebind_auth_socket_statement(username: str, host: str, os_user: str) -> Tuple[str, tuple]:
    """ALTER USER counterpart to security_grants.
    yandi_runtime_auth_socket_statement()'s CREATE USER — issued ONLY
    when auth_socket_binding_matches() is False for an account
    user_exists() confirms already exists (run_bootstrap() is the only
    caller). Changes the auth method/binding alone — never touches any
    GRANT, so re-running this can never widen or narrow privileges."""
    return (
        "ALTER USER %s@%s IDENTIFIED WITH auth_socket AS %s",
        (username, host, os_user),
    )


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
    already established earlier in Этап 5).

    Also records the schema_migrations version row via migrate.py's own
    record_schema_version() — live-confirmed gap: migrate.apply() (the
    ONLY prior caller of that INSERT) is never invoked by this bootstrap
    path (it opens its own connection via get_connection()'s env-var
    credentials, incompatible with the already-open root/auth_socket
    connection this module receives), so a virgin bootstrap left every
    table created but schema_migrations permanently empty —
    security_selfcheck.check_schema_version() reported "version None,
    expected 1" forever, even though the schema WAS fully applied.
    Recording happens ONLY after every CREATE TABLE/ALTER statement
    above has executed without raising — the same "successful apply is
    the proof" precedent migrate.apply() itself already established,
    not a new, stricter rule invented just for this."""
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
    record_schema_version(conn)


def trigger_exists(conn, trigger_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM information_schema.triggers "
            "WHERE trigger_schema = DATABASE() AND trigger_name = %s",
            (trigger_name,),
        )
        row = cur.fetchone()
        return bool(row and row.get("c"))


def _normalize_trigger_body(text: str) -> str:
    """Whitespace-insensitive comparison key — MySQL's stored
    ACTION_STATEMENT is not guaranteed to preserve our own DDL string's
    exact spacing/newlines byte-for-byte."""
    return re.sub(r"\s+", " ", text or "").strip().rstrip(";")


def _expected_trigger_body(ddl: str) -> str:
    """Our own generated CREATE TRIGGER text always has the shape
    `CREATE TRIGGER name\\nBEFORE ... ON ...\\nFOR EACH ROW\\n<body>` —
    information_schema.triggers.ACTION_STATEMENT stores ONLY <body>
    (never the header), so that's the only part worth comparing."""
    marker = "FOR EACH ROW\n"
    idx = ddl.find(marker)
    return ddl[idx + len(marker):] if idx != -1 else ddl


def trigger_definition_matches(conn, trigger_name: str, expected_ddl: str) -> bool:
    """True iff a trigger by this name exists AND its LIVE body matches
    what immutability_triggers() currently generates for it. False for
    both "doesn't exist" and "exists but stale" — apply_immutability_
    triggers() treats them identically (both need a (re)create)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ACTION_STATEMENT FROM information_schema.triggers "
            "WHERE trigger_schema = DATABASE() AND trigger_name = %s",
            (trigger_name,),
        )
        row = cur.fetchone()
    if not row:
        return False
    return (
        _normalize_trigger_body(row.get("ACTION_STATEMENT"))
        == _normalize_trigger_body(_expected_trigger_body(expected_ddl))
    )


def apply_immutability_triggers(conn) -> List[str]:
    """Idempotent via an EXPLICIT existence check against information_
    schema.triggers — deliberately NOT relying on `CREATE TRIGGER IF
    NOT EXISTS` syntax, whose availability across this specific Percona
    8.0.46 build was not independently verified in this pass (no live
    server to test against). Mirrors the same explicit-check pattern
    agent/db/sql/migrate.py already established for schema_migrations,
    rather than assuming untested syntax works (mandate §55: don't
    claim proof that wasn't obtained).

    DRIFT DETECTION (added after a live pentest found a real gap in
    trg_verification_run_guard_update's original logic): existence
    alone is not enough — a trigger's DEFINITION can change in this
    module's source (a security fix, exactly like this one) without
    this function ever noticing on an ALREADY-bootstrapped live
    instance, silently leaving the STALE, less-safe body active forever
    (the same class of bug install_config()'s own drift detection
    fixed earlier for my.cnf). Any existing trigger whose live
    ACTION_STATEMENT no longer matches what immutability_triggers()
    currently generates for it is DROPped and recreated — never a
    data-destructive operation, only enforcement code being refreshed.

    Returns the list of trigger names actually (re)created by THIS
    call — a second call against an unchanged database returns an
    empty list."""
    created: List[str] = []
    for trigger_name, ddl in immutability_triggers():
        if trigger_exists(conn, trigger_name):
            if trigger_definition_matches(conn, trigger_name, ddl):
                continue
            with conn.cursor() as cur:
                cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
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
    conn, *, readonly_password: str = "", migrator_password: str = "", runtime_password: str = "",
    runtime_host: str = "%", readonly_host: str = "localhost", migrator_host: str = "localhost",
    runtime_auth_socket_os_user: str = None,
    readonly_auth_socket_os_user: str = None,
    provision_migrator: bool = False,
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

    `readonly_auth_socket_os_user` ("10-year bastion" Layer 3, owner
    mandate): same idea as `runtime_auth_socket_os_user`, but for
    YANDI_READONLY — bound to the OWNER's own personal OS login instead
    of a stored password file. `readonly_password`/`readonly_host` are
    then ignored entirely for that role. Leaving this None preserves
    the original password-only behavior exactly.

    `provision_migrator` ("10-year bastion" Layer 3, owner mandate: "мы
    не root для базы" — no standing account should be able to change
    the schema at will, not even under the owner's own login): defaults
    to False — YANDI_MIGRATOR is NOT created. If an already-live
    instance has one from a previous bootstrap (the old default), it is
    DROPped instead (an account is trivially recreatable later for a
    genuine one-time migration; leaving a stale standing schema-change
    credential around is the actual risk). Passing True is the
    deliberate, explicit break-glass path for that rare occasion —
    `migrator_password` is then required.

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
    if not readonly_auth_socket_os_user and not readonly_password:
        raise ValueError(
            "run_bootstrap() needs either readonly_password (password auth) or "
            "readonly_auth_socket_os_user (auth_socket auth) for YANDI_READONLY — "
            "refusing to create it with an empty password by omission."
        )
    if provision_migrator and not migrator_password:
        raise ValueError(
            "run_bootstrap() needs migrator_password when provision_migrator=True — "
            "refusing to create YANDI_MIGRATOR with an empty password by omission."
        )
    ensure_database(conn)
    # apply_schema() MUST run before any ensure_role() call: YANDI_RUNTIME's
    # grants include per-table `GRANT UPDATE ON db.<table>` statements for
    # every class-C/D table (yandi_runtime_grant_statements() below) — a
    # table-level GRANT requires the target table to already exist in
    # MySQL, unlike a `db.*` wildcard grant. Creating roles/grants first
    # fails with "Table '<db>.<table>' doesn't exist" (live-confirmed:
    # ERROR 1146 on `belief`, the first class-C table in
    # ALL_TABLES_IN_ORDER) the very first time this runs against an empty
    # database. Triggers/instance_identity already came after apply_schema()
    # and are unaffected by this reordering.
    apply_schema(conn)
    if runtime_auth_socket_os_user:
        actual_runtime_host = "localhost"
        # DRIFT DETECTION: CREATE USER IF NOT EXISTS below is a no-op
        # against an already-existing yandi_runtime — if AGENT_OS_USER
        # changed since the last bootstrap (see deploy/install-yandi.sh),
        # the account would stay bound to the STALE OS user forever
        # without this explicit rebind. See auth_socket_binding_matches()'s
        # own docstring.
        if user_exists(conn, "yandi_runtime", actual_runtime_host) and not auth_socket_binding_matches(
            conn, "yandi_runtime", actual_runtime_host, runtime_auth_socket_os_user
        ):
            with conn.cursor() as cur:
                cur.execute(*rebind_auth_socket_statement(
                    "yandi_runtime", actual_runtime_host, runtime_auth_socket_os_user
                ))
        ensure_role(conn, [yandi_runtime_auth_socket_statement("yandi_runtime", runtime_auth_socket_os_user)]
                    + yandi_runtime_grant_statements("yandi_runtime", actual_runtime_host))
    else:
        actual_runtime_host = runtime_host
        ensure_role(conn, yandi_runtime_statements("yandi_runtime", runtime_host, runtime_password))

    if readonly_auth_socket_os_user:
        actual_readonly_host = "localhost"
        # Same drift-detection reasoning as YANDI_RUNTIME above — an
        # already-live yandi_readonly bound to a stale OS login (e.g.
        # the owner's account was renamed) would otherwise stay
        # mis-bound forever.
        if user_exists(conn, "yandi_readonly", actual_readonly_host) and not auth_socket_binding_matches(
            conn, "yandi_readonly", actual_readonly_host, readonly_auth_socket_os_user
        ):
            with conn.cursor() as cur:
                cur.execute(*rebind_auth_socket_statement(
                    "yandi_readonly", actual_readonly_host, readonly_auth_socket_os_user
                ))
        ensure_role(conn, [yandi_readonly_auth_socket_statement("yandi_readonly", readonly_auth_socket_os_user)]
                    + yandi_readonly_grant_statements("yandi_readonly", actual_readonly_host))
    else:
        actual_readonly_host = readonly_host
        ensure_role(conn, yandi_readonly_statements("yandi_readonly", readonly_host, readonly_password))

    migrator_provisioned = False
    if provision_migrator:
        ensure_role(conn, yandi_migrator_statements("yandi_migrator", migrator_host, migrator_password))
        migrator_provisioned = True
    elif user_exists(conn, "yandi_migrator", migrator_host):
        # "10-year bastion" Layer 3: no standing schema-change account —
        # a previous bootstrap's yandi_migrator (the old default) is
        # revoked here rather than left holding CREATE/ALTER/DROP
        # forever with a password sitting in secrets_dir.
        with conn.cursor() as cur:
            cur.execute(*revoke_all_statement("yandi_migrator", migrator_host))

    triggers_created = apply_immutability_triggers(conn)
    if instance_uuid:
        record_instance_identity(conn, instance_uuid, created_by_host=instance_created_by_host)
    role_principals = {
        "runtime": ("yandi_runtime", actual_runtime_host),
        "readonly": ("yandi_readonly", actual_readonly_host),
    }
    roles_ensured = ["yandi_runtime", "yandi_readonly"]
    if migrator_provisioned:
        role_principals["migrator"] = ("yandi_migrator", migrator_host)
        roles_ensured.append("yandi_migrator")
    return {
        "database": DATABASE_NAME,
        "roles_ensured": roles_ensured,
        "triggers_created": triggers_created,
        "runtime_auth_mode": "auth_socket" if runtime_auth_socket_os_user else "password",
        "readonly_auth_mode": "auth_socket" if readonly_auth_socket_os_user else "password",
        "migrator_provisioned": migrator_provisioned,
        # The EXACT (username, host) pairs just created — single source
        # of truth for any caller that needs to verify these specific
        # accounts' grants afterward (security_selfcheck.run_selfcheck()'s
        # role_principals=), rather than a caller re-deriving/hardcoding
        # the same host defaults a second time and risking drift.
        "role_principals": role_principals,
    }
