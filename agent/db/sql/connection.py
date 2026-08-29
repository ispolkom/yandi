"""
agent/db/sql/connection.py — MySQL/Percona connection layer.

Config is env-var only, NEVER hardcoded, per the mandate's explicit
instruction (§27): "НЕ hardcode password. Использовать environment/
config, секреты не коммитить." No default password, no default user —
if those two are unset, the layer reports itself as NOT CONFIGURED
rather than guessing or trying an empty-password connection.

    YANDI_SQL_HOST      default "127.0.0.1" (ignored if YANDI_SQL_SOCKET is set)
    YANDI_SQL_PORT      default "3306" (ignored if YANDI_SQL_SOCKET is set)
    YANDI_SQL_USER      required, no default
    YANDI_SQL_PASSWORD  required unless YANDI_SQL_AUTH_MODE=auth_socket
    YANDI_SQL_DATABASE  default "yandi_epistemic"
    YANDI_SQL_CONNECT_TIMEOUT  default "3" (seconds)
    YANDI_SQL_SOCKET    optional Unix socket path (DATABASE BOOTSTRAP V1,
                        mandate §10) — e.g. /run/yandi/mysql.sock for
                        YANDI's own dedicated instance. When set, this is
                        the ONLY transport used: host/port are never
                        consulted and there is NEVER a fallback to TCP if
                        the socket connection fails (mandate §26: "Это
                        абсолютный запрет" — a dedicated-socket failure
                        must surface as SqlUnavailable, never silently
                        retry against localhost:3306 or any other host).
    YANDI_SQL_AUTH_MODE default "password"; "auth_socket" sends no
                        password at all, relying on the server's
                        auth_socket plugin to authenticate by kernel-
                        verified peer UID (DEDICATED_INSTANCE_DESIGN.md
                        §H, Option 1) — only meaningful together with
                        YANDI_SQL_SOCKET; requesting it without a socket
                        path configured is a configuration error, not a
                        silent downgrade to password auth.

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


class SqlUnavailable(Exception):
    """
    Raised for BOTH "not configured" (no credentials in the environment)
    and "configured but unreachable" (connection/auth failure) — callers
    that need to fail open (agent/db/sql/shadow_write.py) catch this ONE
    exception type and never need to distinguish the two cases to do the
    right thing: skip the SQL write, never touch the JSON canonical path.
    """


def _auth_mode() -> str:
    return os.environ.get("YANDI_SQL_AUTH_MODE", "password")


def is_configured() -> bool:
    user_set = bool(os.environ.get("YANDI_SQL_USER"))
    if not user_set:
        return False
    if _auth_mode() == "auth_socket":
        # auth_socket needs no password, but IS meaningless without a
        # socket to connect over — never treat it as "configured" against
        # a bare host/port with no password (that would just be an
        # accidental anonymous-login attempt, not a deliberate choice).
        return bool(os.environ.get("YANDI_SQL_SOCKET"))
    return bool(os.environ.get("YANDI_SQL_PASSWORD"))


def _config() -> dict:
    return {
        "host": os.environ.get("YANDI_SQL_HOST", "127.0.0.1"),
        "port": int(os.environ.get("YANDI_SQL_PORT", "3306")),
        "user": os.environ.get("YANDI_SQL_USER", ""),
        "password": os.environ.get("YANDI_SQL_PASSWORD", ""),
        "database": os.environ.get("YANDI_SQL_DATABASE", "yandi_epistemic"),
        "connect_timeout": int(os.environ.get("YANDI_SQL_CONNECT_TIMEOUT", "3")),
        "socket": os.environ.get("YANDI_SQL_SOCKET", ""),
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
    if _auth_mode() == "auth_socket" and not os.environ.get("YANDI_SQL_SOCKET"):
        raise SqlUnavailable(
            "YANDI_SQL_AUTH_MODE=auth_socket requires YANDI_SQL_SOCKET to also be "
            "set — auth_socket authenticates by Unix peer credentials, which only "
            "exist over a Unix socket connection, never over TCP."
        )
    if not is_configured():
        raise SqlUnavailable(
            "YANDI_SQL_USER/YANDI_SQL_PASSWORD (or YANDI_SQL_SOCKET with "
            "YANDI_SQL_AUTH_MODE=auth_socket) not set in the environment — "
            "SQL layer is not configured, not a connection failure."
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
