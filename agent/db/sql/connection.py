"""
agent/db/sql/connection.py — MySQL/Percona connection layer.

THE single runtime SQL configuration resolver (DATABASE BOOTSTRAP V1,
seventeenth Phase B attempt) — every caller (repositories.py,
shadow_write.py, orchestrator_v2.py, migrate.py) reads its connection
config through _config()/is_configured()/get_connection() in THIS file
and nowhere else. Nothing scatters its own copy of these defaults.

Config is env-var only, NEVER hardcoded (mandate §27) — but "never
hardcoded" means never a SECRET (password/credential); it does NOT mean
"no default endpoint." The dedicated yandi-db appliance IS this
deployment's canonical, always-present local database (DATABASE
BOOTSTRAP V1 proved it live), so CANONICAL DEFAULTS below point at it
out of the box — no operator has to manually export anything for a
standard install. ENV remains the override mechanism, exactly as
before; each of the three variables below resolves independently
(setting one does not require setting the others).

    YANDI_SQL_SOCKET    default "/run/yandi/mysql/mysql.sock" (the
                        dedicated instance's own socket — DATABASE
                        BOOTSTRAP V1). When resolved (default or
                        explicit), this is the ONLY transport used:
                        host/port are never consulted and there is
                        NEVER a fallback to TCP if the socket
                        connection fails (mandate §26: "Это абсолютный
                        запрет" — a dedicated-socket failure must
                        surface as SqlUnavailable, never silently retry
                        against localhost:3306, 127.0.0.1, or the
                        shared FastPanel MySQL instance, under any
                        circumstance).
    YANDI_SQL_AUTH_MODE default "auth_socket" (DATABASE BOOTSTRAP V1's
                        own runtime role) — sends no password at all,
                        relying on the server's auth_socket plugin to
                        authenticate by kernel-verified peer UID
                        (DEDICATED_INSTANCE_DESIGN.md §H, Option 1).
                        Explicit "password" still requires an explicit
                        YANDI_SQL_PASSWORD — there is no default
                        password, ever, for either mode.
    YANDI_SQL_USER      default "yandi_runtime" (DATABASE BOOTSTRAP
                        V1's least-privilege runtime role — see
                        security_grants.py; holds SELECT/INSERT and a
                        narrow per-table UPDATE only, never DDL/admin).
    YANDI_SQL_HOST      default "127.0.0.1" — only ever consulted if
                        the resolved socket is somehow empty (not
                        reachable in practice: the socket above always
                        resolves to a non-empty value, default or
                        explicit).
    YANDI_SQL_PORT      default "3306" — same caveat as HOST above.
    YANDI_SQL_PASSWORD  required only when YANDI_SQL_AUTH_MODE resolves
                        to "password" (explicit override) — no default.
    YANDI_SQL_DATABASE  default "yandi_epistemic".
    YANDI_SQL_CONNECT_TIMEOUT  default "3" (seconds).

Bootstrap/migration tooling (agent/db/sql/migrate.py, agent/db/sql/
bootstrap.py via live_bootstrap.py's own root/auth_socket connection)
has a DIFFERENT privilege context by necessity — bootstrap always
connects as root to CREATE the yandi_runtime account these defaults
now point at — but it is not a second resolver: migrate.py's own CLI
still calls get_connection() from this same module, so it also
benefits from (and is bound by) these same canonical defaults; an
operator who wants migrate.py to run DDL simply overrides
YANDI_SQL_USER/YANDI_SQL_PASSWORD to a DDL-capable account (e.g.
yandi_migrator), same override mechanism as everything else.

No connection pool in v1 — the mandate explicitly discourages reaching
for heavy infrastructure without justification (§28: "НЕ тащи
SQLAlchemy или тяжёлый ORM автоматически... По умолчанию предпочитай:
простую явную SQL-модель"). A pool is a legitimate later optimization
once real request volume against a live DB is measured (§45); a
short-lived per-call connection is simpler, safer to reason about, and
correct, which matters far more at this stage than shaving connection
setup milliseconds.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional

# Canonical DEDICATED-APPLIANCE defaults (DATABASE BOOTSTRAP V1) — the
# ONLY place these three values are hardcoded anywhere in this
# codebase. ENV always overrides; see the module docstring above for
# the full override semantics and rationale.
_DEFAULT_SOCKET = "/run/yandi/mysql/mysql.sock"
_DEFAULT_AUTH_MODE = "auth_socket"
_DEFAULT_USER = "yandi_runtime"


class SqlUnavailable(Exception):
    """
    Raised for BOTH "not configured" (no credentials in the environment)
    and "configured but unreachable" (connection/auth failure) — callers
    that need to fail open (agent/db/sql/shadow_write.py) catch this ONE
    exception type and never need to distinguish the two cases to do the
    right thing: skip the SQL write, never touch the JSON canonical path.
    """


def _resolve(env_name: str, default: str) -> str:
    """`env_name` present (even as an explicit empty string) -> that
    literal value; genuinely absent -> `default`. Deliberately NOT
    `os.environ.get(env_name) or default` — that would make an
    explicit empty-string override indistinguishable from "unset" and
    silently re-impose the default, with no way left to genuinely opt
    out of it (is_configured()'s own "explicitly empty socket ->
    correctly reports not configured" case relies on this distinction)."""
    return os.environ[env_name] if env_name in os.environ else default


def _auth_mode() -> str:
    return _resolve("YANDI_SQL_AUTH_MODE", _DEFAULT_AUTH_MODE)


def _socket() -> str:
    return _resolve("YANDI_SQL_SOCKET", _DEFAULT_SOCKET)


def _user() -> str:
    return _resolve("YANDI_SQL_USER", _DEFAULT_USER)


def is_configured() -> bool:
    # user/auth_mode always resolve to a non-empty value now (canonical
    # defaults) — the only way this can be "not configured" is an
    # EXPLICIT YANDI_SQL_AUTH_MODE=password override with no explicit
    # YANDI_SQL_PASSWORD given (no default password exists, ever).
    if _auth_mode() == "auth_socket":
        # auth_socket needs no password, but IS meaningless without a
        # socket to connect over — never treat it as "configured" against
        # a bare host/port with no password (that would just be an
        # accidental anonymous-login attempt, not a deliberate choice).
        # In practice _socket() always resolves to a non-empty value
        # (default or explicit) — this check stays real, not vacuous,
        # for the rare case a caller explicitly sets an empty override.
        return bool(_socket())
    return bool(os.environ.get("YANDI_SQL_PASSWORD"))


def _config() -> dict:
    return {
        "host": os.environ.get("YANDI_SQL_HOST", "127.0.0.1"),
        "port": int(os.environ.get("YANDI_SQL_PORT", "3306")),
        "user": _user(),
        "password": os.environ.get("YANDI_SQL_PASSWORD", ""),
        "database": os.environ.get("YANDI_SQL_DATABASE", "yandi_epistemic"),
        "connect_timeout": int(os.environ.get("YANDI_SQL_CONNECT_TIMEOUT", "3")),
        "socket": _socket(),
        "auth_mode": _auth_mode(),
    }


@contextmanager
def get_connection(autocommit: bool = False):
    """
    Yields a live pymysql connection (utf8mb4, autocommit as requested —
    default False, so callers control their own transaction boundaries
    explicitly per mandate §29). Raises SqlUnavailable — never a raw
    pymysql/socket exception — if credentials are missing or the
    connection genuinely fails, so every caller has exactly ONE
    exception type to handle.
    """
    if _auth_mode() not in ("password", "auth_socket"):
        raise SqlUnavailable(
            f"YANDI_SQL_AUTH_MODE={_auth_mode()!r} is not recognized — expected "
            f"'password' or 'auth_socket'. Refusing to guess."
        )
    if _auth_mode() == "auth_socket" and not _socket():
        # _socket() always resolves to the canonical dedicated-appliance
        # default unless a caller explicitly overrides it to something
        # empty — this stays a real, reachable error path for that case,
        # not dead code.
        raise SqlUnavailable(
            "YANDI_SQL_AUTH_MODE=auth_socket requires a socket path (default "
            f"{_DEFAULT_SOCKET!r}, or an explicit YANDI_SQL_SOCKET) — auth_socket "
            "authenticates by Unix peer credentials, which only exist over a Unix "
            "socket connection, never over TCP."
        )
    if not is_configured():
        raise SqlUnavailable(
            "YANDI_SQL_AUTH_MODE=password requires an explicit YANDI_SQL_PASSWORD "
            "(no default password exists for password mode) — SQL layer is not "
            "configured, not a connection failure."
        )

    try:
        import pymysql
        import pymysql.cursors
    except ImportError as e:
        raise SqlUnavailable(f"pymysql not importable: {e}") from e

    cfg = _config()
    conn: Optional["pymysql.connections.Connection"] = None
    connect_kwargs = dict(
        user=cfg["user"],
        password="" if cfg["auth_mode"] == "auth_socket" else cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        connect_timeout=cfg["connect_timeout"],
        autocommit=autocommit,
        cursorclass=pymysql.cursors.DictCursor,
    )
    if cfg["socket"]:
        # Unix socket mode (mandate §10/§26): host/port are NEVER also
        # passed here — pymysql would otherwise accept both and there
        # would be a live ambiguity about which transport actually won.
        # No fallback to host/port exists anywhere in this function: a
        # failed socket connect below raises SqlUnavailable directly.
        connect_kwargs["unix_socket"] = cfg["socket"]
    else:
        connect_kwargs["host"] = cfg["host"]
        connect_kwargs["port"] = cfg["port"]

    try:
        conn = pymysql.connect(**connect_kwargs)
    except Exception as e:
        raise SqlUnavailable(f"SQL connection failed: {e}") from e

    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def ping() -> bool:
    """
    True only if a real connection AND a real round-trip query succeeded.
    Used by the equivalence-audit tooling and by manual operators to
    distinguish "not configured" from "configured but genuinely live" —
    never claim live-tested status anywhere in this codebase's reports
    without this having returned True at least once (mandate §27).
    """
    try:
        with get_connection(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except SqlUnavailable:
        return False
