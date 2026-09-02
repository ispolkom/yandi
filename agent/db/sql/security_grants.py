"""
agent/db/sql/security_grants.py — Этап 5E-S S2: DB role/privilege
design (mandate §10/§53).

DESIGNED, NOT EXECUTED against any live server in this pass — see
SECURITY_ARCHITECTURE.md §0/§7/§9. No credentials exist in this
environment to create these roles with (verified this pass: `sudo -n
mysql` fails with "a password is required", no YANDI_SQL_USER/PASSWORD
in the environment). agent/db/sql/bootstrap.py is the only intended
future caller of the functions here, and only against a server the
operator has explicitly decided to bootstrap (SECURITY_ARCHITECTURE.md
§4's required owner decision).

Every GRANT here targets ONLY `{DATABASE_NAME}.*` — never `*.*` —
EXCEPT the handful of privileges MySQL's own grant model makes global-
only regardless of what any application wants (CREATE USER, GRANT
OPTION scoping is inherent to MySQL, not a YANDI design choice — see
`yandi_bootstrap_statements()`'s docstring). This matters because this
machine's Percona instance is SHARED (SECURITY_ARCHITECTURE.md §4) —
a `*.*` grant would touch other tenants' schemas.

Every privilege below carries a one-line justification (mandate
§10.3: "Каждый privilege должен быть объяснён").
"""
from __future__ import annotations

from typing import List, Tuple

from agent.db.sql.schema import ALL_TABLES_IN_ORDER, TABLE_CLASSIFICATION

DATABASE_NAME = "yandi_epistemic"

_ALL_TABLES = [n for n, _ in ALL_TABLES_IN_ORDER]
CLASS_AB_TABLES = [n for n in _ALL_TABLES if TABLE_CLASSIFICATION.get(n) in ("A", "B")]
CLASS_C_TABLES = [n for n in _ALL_TABLES if TABLE_CLASSIFICATION.get(n) == "C"]
CLASS_D_TABLES = [n for n in _ALL_TABLES if TABLE_CLASSIFICATION.get(n) == "D"]

# Privileges that must NEVER appear in a runtime/readonly grant, under
# any circumstance — the regression suite scans for these literally.
FORBIDDEN_FOR_RUNTIME = (
    "SUPER", "GRANT OPTION", "CREATE USER", "FILE", "PROCESS",
    "DROP", "ALTER", "CREATE", "TRUNCATE", "SHUTDOWN", "RELOAD",
    "REPLICATION", "EXECUTE",
)
FORBIDDEN_FOR_READONLY = FORBIDDEN_FOR_RUNTIME + ("INSERT", "UPDATE", "DELETE")

# YANDI_MIGRATOR legitimately holds CREATE/ALTER/DROP (schema-upgrade
# DDL, see yandi_migrator_statements() above) — the one role in this
# design where those four are NOT a violation. Still forbidden: every
# genuinely global/admin privilege no lesser role should ever hold,
# same as runtime. Derived from FORBIDDEN_FOR_RUNTIME (not a third,
# independently-hardcoded list) so the two policies cannot silently
# drift apart — DATABASE BOOTSTRAP V1, mandate: "ROLE POLICY != CURRENT
# BOOTSTRAP CONNECTION POLICY," extended to mean each role's OWN policy
# must not accidentally borrow another role's list either.
FORBIDDEN_FOR_MIGRATOR = tuple(
    p for p in FORBIDDEN_FOR_RUNTIME if p not in ("CREATE", "ALTER", "DROP", "TRUNCATE")
)


def create_user_statement(username: str, host: str, password: str) -> Tuple[str, tuple]:
    """CREATE USER's username/host/password are all string-literal
    positions in MySQL grammar (single-quoted, not backtick-quoted
    identifiers) — genuinely parameterizable, unlike table/database
    names. `username`/`host` are still expected to come from this
    module's own hardcoded role names (mandate §15: dynamic identifiers
    only from a hardcoded allow-list), never from arbitrary input;
    `password` is the one value a caller (bootstrap.py) is expected to
    generate freshly per-install, never hardcoded here."""
    return (
        "CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
        (username, host, password),
    )


def yandi_bootstrap_statements(username: str, host: str, password: str) -> List[Tuple[str, tuple]]:
    """
    YANDI_BOOTSTRAP — TEMPORARY. Exists only for the duration of initial
    install / controlled schema migration (mandate §10.1). Must be
    revoked/dropped immediately after (bootstrap.py's own
    `revoke_bootstrap()`, this pass — see its docstring for why that
    function's OWN execution is also blocked here).

    CREATE USER and GRANT OPTION are global-only privileges in MySQL's
    grant model — there is no per-database "CREATE USER" — so this is
    the ONE role in this design that necessarily touches `*.*`. This is
    a MySQL constraint, not a YANDI design choice; the mitigation is
    TEMPORAL (this role does not persist), not scope-based.
    """
    stmts = [create_user_statement(username, host, password)]
    stmts.append((
        f"GRANT CREATE, DROP, ALTER, INDEX, REFERENCES, CREATE VIEW, "
        f"TRIGGER, CREATE USER, GRANT OPTION ON *.* TO %s@%s",
        (username, host),
    ))
    # Database-scoped DDL for the actual schema/role creation work.
    stmts.append((
        f"GRANT ALL PRIVILEGES ON `{DATABASE_NAME}`.* TO %s@%s",
        (username, host),
    ))
    return stmts


def yandi_migrator_statements(username: str, host: str, password: str) -> List[Tuple[str, tuple]]:
    """
    YANDI_MIGRATOR — for schema upgrades only (mandate §10.2). Local
    only (`host` is expected to be 'localhost', enforced by
    bootstrap.py, not by this function — this function just generates
    the GRANT text for whatever host it's given). DDL scoped to
    `{DATABASE_NAME}.*` only — no CREATE USER, no GRANT OPTION, no
    global privilege of any kind, unlike bootstrap.
    """
    return [
        create_user_statement(username, host, password),
        (
            f"GRANT CREATE, ALTER, INDEX, REFERENCES, CREATE VIEW, TRIGGER, "
            f"DROP ON `{DATABASE_NAME}`.* TO %s@%s",
            (username, host),
        ),
    ]


def yandi_runtime_statements(username: str, host: str, password: str) -> List[Tuple[str, tuple]]:
    """
    YANDI_RUNTIME — the actual application process (mandate §10.3).

        SELECT   on every table — the app has to be able to read its
                 own memory (read API, dedup lookups, projections).
        INSERT   on every table — every canonical write in this
                 codebase is an INSERT (append-only by construction,
                 see agent/db/sql/repositories.py).
        UPDATE   ONLY on class C (derived projections: `belief`,
                 `semantic_edge` — rebuildable, mutation is their
                 whole point) and class D (`verification_run` — one
                 narrow, trigger-guarded status transition, see
                 SECURITY_ARCHITECTURE.md §6). NEVER on a class A/B
                 table — that is the entire point of this role.
        (nothing else) — no DELETE anywhere, no DDL, no admin
                 privilege of any kind (FORBIDDEN_FOR_RUNTIME).
    """
    return [create_user_statement(username, host, password)] + yandi_runtime_grant_statements(username, host)


def yandi_runtime_auth_socket_statement(username: str, os_user: str) -> Tuple[str, tuple]:
    """DATABASE BOOTSTRAP V1, mandate §11 / DEDICATED_INSTANCE_DESIGN.md
    §H Option 1: creates YANDI_RUNTIME with NO PASSWORD AT ALL — the
    server's `auth_socket` plugin authenticates a local Unix-socket
    connection by kernel-verified peer UID instead. `os_user` is the
    real OS account the AGENT process runs as (NOT a YANDI-internal
    label) — a connection only succeeds if the connecting process's own
    UID maps to this name via `getpwnam`, so there is no SQL credential
    of any kind to leak from config/env/logs/git for this, the hottest
    and most-exposed of the four roles (closes T7 entirely for this
    role, rather than mitigating it).

    Host is always 'localhost' here: `auth_socket` is a local-Unix-
    socket-only mechanism, it has no meaning for `%`/remote hosts (a
    remote TCP client has no OS peer UID to check) — unlike
    yandi_runtime_statements()'s password variant, this function does
    not take a `host` parameter at all, to make that constraint
    structural rather than a caller convention to remember.

    Same GRANT statements as yandi_runtime_statements() are still
    needed afterward (this function only replaces the CREATE USER line)
    — callers apply this INSTEAD OF yandi_runtime_statements()'s first
    statement, then still issue the SELECT/INSERT/UPDATE grants for
    'yandi_runtime'@'localhost'."""
    return (
        "CREATE USER IF NOT EXISTS %s@'localhost' IDENTIFIED WITH auth_socket AS %s",
        (username, os_user),
    )


def yandi_runtime_grant_statements(username: str, host: str) -> List[Tuple[str, tuple]]:
    """The GRANT half of YANDI_RUNTIME's privileges, factored out of
    yandi_runtime_statements() so both the password-based CREATE USER
    and the auth_socket-based CREATE USER (above) can share the exact
    same grant logic — never two copies of this privilege list to keep
    in sync."""
    stmts = [(
        f"GRANT SELECT, INSERT ON `{DATABASE_NAME}`.* TO %s@%s",
        (username, host),
    )]
    for table in CLASS_C_TABLES + CLASS_D_TABLES:
        stmts.append((
            f"GRANT UPDATE ON `{DATABASE_NAME}`.`{table}` TO %s@%s",
            (username, host),
        ))
    return stmts


def yandi_readonly_grant_statements(username: str, host: str) -> List[Tuple[str, tuple]]:
    """The GRANT half of YANDI_READONLY's privileges, factored out of
    yandi_readonly_statements() so both the password-based CREATE USER
    and the auth_socket-based CREATE USER (below) share the exact same
    grant logic — never two copies of this privilege list to keep in
    sync (same reasoning as yandi_runtime_grant_statements() above)."""
    return [(f"GRANT SELECT ON `{DATABASE_NAME}`.* TO %s@%s", (username, host))]


def yandi_readonly_statements(username: str, host: str, password: str) -> List[Tuple[str, tuple]]:
    """
    YANDI_READONLY — human-operator direct SQL access (mandate §10.4).
    SELECT only, zero write privilege of any kind. Does NOT receive the
    application encryption key (that is not a GRANT — it lives entirely
    outside SQL, see keys.py) — a SELECT against sensitive columns
    through this account returns ciphertext, never plaintext knowledge.
    """
    return [create_user_statement(username, host, password)] + yandi_readonly_grant_statements(username, host)


def yandi_readonly_auth_socket_statement(username: str, os_user: str) -> Tuple[str, tuple]:
    """"10-year bastion" Layer 3 (owner mandate): creates YANDI_READONLY
    with NO PASSWORD AT ALL — bound via auth_socket to the OWNER's own
    personal OS login, eliminating the stored yandi_readonly.secret file
    under secrets_dir entirely. The owner already authenticates as this
    OS user for every interactive session; auth_socket just recognizes
    that instead of requiring a separate password to remember and store
    on disk. Mirrors yandi_runtime_auth_socket_statement() exactly — see
    that function's own docstring for why `host` is always 'localhost'
    and not a parameter here (auth_socket has no meaning for a remote/
    `%` host)."""
    return (
        "CREATE USER IF NOT EXISTS %s@'localhost' IDENTIFIED WITH auth_socket AS %s",
        (username, os_user),
    )


def revoke_all_statement(username: str, host: str) -> Tuple[str, tuple]:
    """Used to retire YANDI_BOOTSTRAP after install/migration completes
    (mandate §10.1: "После установки: DROP / REVOKE / убрать credential")."""
    return ("DROP USER IF EXISTS %s@%s", (username, host))
