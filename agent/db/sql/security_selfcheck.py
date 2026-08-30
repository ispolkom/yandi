"""
agent/db/sql/security_selfcheck.py — Этап 5E-S S8: startup/CI security
self-check (mandate §39, §44 section N).

Extends what agent/db/sql/migrate.py's --check flow already does
(ping + schema_version) with security-specific assertions: required
tables exist, required immutability triggers exist, and — the LIVE
counterpart of agent/db_sql_security_privilege_regression_test.py's
STATIC proof — that the CURRENTLY CONNECTED account's actual `SHOW
GRANTS FOR CURRENT_USER()` output contains nothing outside its
declared allow-list.

DESIGNED, LIVE VERIFICATION BLOCKED this pass — no credentials exist
in this environment to connect with. Unit-tested against a SCRIPTED
fake connection that returns realistic SHOW GRANTS text.

Per mandate §39: "При mismatch: не делать silent repair от runtime
account." This module only REPORTS — it never attempts to fix a
mismatch itself (no CREATE TABLE/CREATE TRIGGER/GRANT calls anywhere
in this file). What a caller does with a failing result (refuse
startup, page an operator, just log) is a deployment decision outside
this module's scope.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from agent.db.sql.schema import ALL_TABLES_IN_ORDER, SCHEMA_VERSION
from agent.db.sql.security_grants import (
    FORBIDDEN_FOR_MIGRATOR, FORBIDDEN_FOR_READONLY, FORBIDDEN_FOR_RUNTIME,
)
from agent.db.sql.security_triggers import immutability_triggers
from agent.db.sql.instance_identity import verify_instance_identity, MATCH, NO_DB_IDENTITY

# Which forbidden-privilege policy applies to which ROLE — never to
# "whatever account happens to be connected." Deliberately has no entry
# for bootstrap/root: that account is INTENTIONALLY privileged (it is
# how every other role gets created in the first place) and must never
# be compared against a lesser role's deny-list (DATABASE BOOTSTRAP V1
# live bug: run_selfcheck() was being called on the bootstrap
# connection itself — root@localhost via auth_socket — and evaluating
# ROOT's own legitimate CREATE/DROP/SUPER/... grants against
# FORBIDDEN_FOR_RUNTIME, which will always "fail" for any bootstrap-
# capable account and proves nothing about the actual yandi_runtime
# account's privileges).
FORBIDDEN_BY_ROLE = {
    "runtime": FORBIDDEN_FOR_RUNTIME,
    "readonly": FORBIDDEN_FOR_READONLY,
    "migrator": FORBIDDEN_FOR_MIGRATOR,
}


def check_schema_version(conn, expected_version: int = SCHEMA_VERSION) -> Tuple[bool, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(version) AS v FROM schema_migrations")
        row = cur.fetchone()
    actual = row.get("v") if row else None
    if actual != expected_version:
        return (False, f"schema_migrations reports version {actual!r}, expected {expected_version}")
    return (True, "")


def check_required_tables(conn) -> Tuple[bool, List[str]]:
    missing: List[str] = []
    for name, _ddl in ALL_TABLES_IN_ORDER:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = %s",
                (name,),
            )
            row = cur.fetchone()
        if not (row and row.get("c")):
            missing.append(name)
    return (len(missing) == 0, missing)


def check_required_triggers(conn) -> Tuple[bool, List[str]]:
    missing: List[str] = []
    for trigger_name, _ddl in immutability_triggers():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM information_schema.triggers "
                "WHERE trigger_schema = DATABASE() AND trigger_name = %s",
                (trigger_name,),
            )
            row = cur.fetchone()
        if not (row and row.get("c")):
            missing.append(trigger_name)
    return (len(missing) == 0, missing)


def parse_show_grants(rows: List[str]) -> List[str]:
    """`rows`: raw `SHOW GRANTS FOR CURRENT_USER()` result lines (each a
    string like "GRANT SELECT, INSERT ON `yandi_epistemic`.* TO ..."').
    Returns the flat list of privilege tokens actually granted, upper-
    cased. Best-effort text parse — MySQL doesn't expose grants in a
    more structured form via this statement."""
    privileges: List[str] = []
    for row in rows:
        if not row.upper().startswith("GRANT "):
            continue
        on_idx = row.upper().find(" ON ")
        if on_idx == -1:
            continue
        priv_text = row[len("GRANT "):on_idx]
        privileges.extend(p.strip().upper() for p in priv_text.split(","))
    return privileges


def check_current_grants_against_allowlist(conn, forbidden: Tuple[str, ...]) -> Tuple[bool, List[str]]:
    """Runs SHOW GRANTS FOR CURRENT_USER() and flags any forbidden
    privilege actually present on the account currently connected —
    the LIVE counterpart of the static GRANT-text checks in
    agent/db_sql_security_privilege_regression_test.py.

    Only correct when the caller KNOWS the current connection actually
    IS the role being checked (e.g. a production runtime process
    self-checking its own auth_socket connection on startup). A
    bootstrap-time check, run over an admin/root connection used to
    CREATE the roles, must use check_named_principal_grants() below
    instead — see FORBIDDEN_BY_ROLE's own docstring for the live bug
    this distinction fixes."""
    with conn.cursor() as cur:
        cur.execute("SHOW GRANTS FOR CURRENT_USER()")
        rows = [list(r.values())[0] for r in cur.fetchall()]
    granted = parse_show_grants(rows)
    violations = [p for p in granted if p in forbidden or p == "ALL PRIVILEGES"]
    return (len(violations) == 0, violations)


def check_named_principal_grants(
    conn, username: str, host: str, forbidden: Tuple[str, ...],
) -> Tuple[bool, List[str]]:
    """SHOW GRANTS FOR the EXPLICITLY NAMED account — never CURRENT_USER()
    — so a bootstrap-time caller connected as root/admin can verify the
    grants actually held by a JUST-CREATED role account (yandi_runtime/
    yandi_readonly/yandi_migrator), rather than accidentally checking
    its own (necessarily privileged) connection instead.

    `username`/`host` go through the exact same %s-bound-parameter path
    security_grants.create_user_statement() already uses for `CREATE
    USER %s@%s` — MySQL's user-spec grammar accepts a quoted string
    literal there, so pymysql's normal client-side %s substitution is
    the same safe, already-established pattern, not a new one."""
    with conn.cursor() as cur:
        cur.execute("SHOW GRANTS FOR %s@%s", (username, host))
        rows = [list(r.values())[0] for r in cur.fetchall()]
    granted = parse_show_grants(rows)
    violations = [p for p in granted if p in forbidden or p == "ALL PRIVILEGES"]
    return (len(violations) == 0, violations)


def check_instance_identity(conn, expected_instance_uuid: str) -> Tuple[bool, str]:
    """DATABASE BOOTSTRAP V1, mandate §27/§28: the selfcheck's own
    ownership gate — thin wrapper over instance_identity.verify_
    instance_identity() so run_selfcheck() reports identity alongside
    schema/tables/triggers/grants rather than as a separate, easy-to-
    forget call site. `expected_instance_uuid` is deliberately a
    required argument with no default sourced from inside this module —
    the caller (whoever holds the filesystem marker, e.g. deploy/
    install-yandi.sh's live_bootstrap.py) is the only one who can say
    what identity SHOULD be here; this function never guesses it from
    the connection alone."""
    ok, reason = verify_instance_identity(conn, expected_instance_uuid)
    return ok, reason


def run_selfcheck(
    conn, *, role: str = "runtime", expected_instance_uuid: str = None,
    role_principals: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """`role`: 'runtime' or 'readonly' — selects which forbidden-
    privilege list to check the CURRENTLY CONNECTED account's grants
    against. Only correct when the caller's own connection genuinely
    IS that role (e.g. a production runtime process self-checking its
    own auth_socket connection on startup). Ignored entirely when
    `role_principals` is given (see below).

    `role_principals` (DATABASE BOOTSTRAP V1): an optional {"runtime":
    (username, host), "readonly": (...), "migrator": (...)} mapping —
    when given, grants are checked via SHOW GRANTS FOR each NAMED
    account instead of CURRENT_USER(), each against ITS OWN policy
    (FORBIDDEN_BY_ROLE). This is the correct mode for a bootstrap-time
    caller: the connection doing the checking is necessarily an admin/
    root/bootstrap-capable one (that's how the roles got created in the
    first place), so CURRENT_USER()'s own grants say nothing about
    whether yandi_runtime/yandi_readonly/yandi_migrator are correctly
    scoped — live-confirmed bug: run_selfcheck(conn, role="runtime")
    called on the bootstrap connection itself reported grants_ok=False
    with violations=[CREATE, DROP, RELOAD, SHUTDOWN, PROCESS, FILE,
    ALTER, SUPER, EXECUTE, CREATE USER] — ROOT's own legitimate
    privileges, not yandi_runtime's. bootstrap.run_bootstrap()'s return
    dict already carries the exact principals it just created under
    result["role_principals"] — pass that straight through rather than
    re-deriving/hardcoding host values a second time.

    A caller passing `role_principals` does not need `role` at all (it
    checks all three named roles, not just one) — `role` is silently
    ignored in that mode, kept only for the CURRENT_USER() fallback
    path's own signature compatibility.

    Returns a summary dict; raises nothing itself (a caller decides
    what "not ok" means for its own context — refuse startup, enter a
    degraded mode, just log — mandate §28's SECURITY_INTEGRITY_BROKEN
    state is a caller-level decision, not made inside this function)."""
    if role not in ("runtime", "readonly"):
        raise ValueError(f"role must be 'runtime' or 'readonly', got {role!r}")

    version_ok, version_detail = check_schema_version(conn)
    tables_ok, missing_tables = check_required_tables(conn)
    triggers_ok, missing_triggers = check_required_triggers(conn)

    grants_detail: Optional[Dict[str, Any]] = None
    if role_principals:
        grants_detail = {}
        grants_ok = True
        grant_violations: List[str] = []
        for role_name, (username, host) in role_principals.items():
            forbidden = FORBIDDEN_BY_ROLE.get(role_name)
            if forbidden is None:
                raise ValueError(
                    f"no forbidden-privilege policy defined for role {role_name!r} "
                    f"(known roles: {sorted(FORBIDDEN_BY_ROLE)}) — refusing to guess"
                )
            role_ok, role_violations = check_named_principal_grants(conn, username, host, forbidden)
            grants_detail[role_name] = {
                "principal": f"{username}@{host}", "ok": role_ok, "violations": role_violations,
            }
            grants_ok = grants_ok and role_ok
            grant_violations.extend(f"{role_name}:{v}" for v in role_violations)
    else:
        forbidden = FORBIDDEN_FOR_READONLY if role == "readonly" else FORBIDDEN_FOR_RUNTIME
        grants_ok, grant_violations = check_current_grants_against_allowlist(conn, forbidden)

    # Identity is checked ONLY when the caller supplies an expected uuid
    # (mandate §28: "don't treat a deliberately-deferred check's absence
    # as everything broken" — the same posture already applied to TDE).
    # Existing callers that don't pass expected_instance_uuid keep their
    # exact original "ok" semantics (schema+tables+triggers+grants only)
    # — identity_ok/identity_detail are reported as NOT_REQUESTED rather
    # than silently failing the whole selfcheck for a check nobody asked
    # for. Once a caller DOES pass expected_instance_uuid (mandate §27's
    # gate before a destructive/privileged operation), a mismatch DOES
    # flip "ok" to False, same as any other failing check.
    if expected_instance_uuid is None:
        identity_ok, identity_detail = True, "NOT_REQUESTED"
    else:
        identity_ok, identity_detail = check_instance_identity(conn, expected_instance_uuid)

    result = {
        "ok": version_ok and tables_ok and triggers_ok and grants_ok and identity_ok,
        "schema_version_ok": version_ok, "schema_version_detail": version_detail,
        "tables_ok": tables_ok, "missing_tables": missing_tables,
        "triggers_ok": triggers_ok, "missing_triggers": missing_triggers,
        "grants_ok": grants_ok, "grant_violations": grant_violations,
        "identity_ok": identity_ok, "identity_detail": identity_detail,
    }
    if grants_detail is not None:
        result["grants_detail"] = grants_detail
    return result
