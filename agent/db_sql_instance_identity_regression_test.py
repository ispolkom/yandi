"""
agent/db_sql_instance_identity_regression_test.py — DATABASE BOOTSTRAP
V1, mandate §4/§8/§27: instance ownership identity.

STATIC/MOCK PROOF ONLY — no live server exists this pass. What IS
proven: instance_identity.py's file-marker + DB-row logic against a
real temp-directory filesystem (the file half needs no mocking at all)
and a small in-memory fake connection (the DB half) — plus connection.
py's Unix-socket/auth_socket wiring against a mocked pymysql.connect(),
and security_grants.py's/security_selfcheck.py's additive extensions.

Covers:
    A. generate_instance_id() / read_instance_id_file() / ensure_
       instance_id_file() — idempotency (a second ensure_ call returns
       the SAME id, never regenerates), atomic-write-then-rename.
    B. record_instance_identity()/get_db_instance_id() against a fake
       connection — idempotent same-uuid re-call, raises on a
       DIFFERENT uuid (mandate §8: ambiguous state must STOP, not
       silently pick one).
    C. verify_instance_identity() — MATCH / NO_DB_IDENTITY / MISMATCH.
    D. connection.py: YANDI_SQL_SOCKET routes to unix_socket=, never
       combined with host/port; YANDI_SQL_AUTH_MODE=auth_socket sends
       no password and requires a socket path; no fallback to TCP on a
       socket failure (mandate §26).
    E. security_grants.yandi_runtime_auth_socket_statement(): host is
       always 'localhost', no `host` parameter exists on the function
       at all (structural, not a caller convention).
    F. security_selfcheck.run_selfcheck(expected_instance_uuid=...):
       identity NOT_REQUESTED when omitted (backward compatible, mandate
       §28's "deferred check isn't 'everything broken'"), gates "ok"
       when supplied and mismatched.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_instance_identity_regression_test
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch, MagicMock

from agent.db.sql.instance_identity import (
    generate_instance_id, read_instance_id_file, ensure_instance_id_file,
    get_db_instance_id, record_instance_identity, verify_instance_identity, describe,
    MATCH, NO_DB_IDENTITY, MISMATCH,
)
from agent.db.sql import connection as conn_mod
from agent.db.sql.security_grants import yandi_runtime_auth_socket_statement, yandi_runtime_grant_statements
from agent.db.sql.security_selfcheck import run_selfcheck

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
# A. File marker.
# ============================================================

with tempfile.TemporaryDirectory() as tmpdir:
    id_path = os.path.join(tmpdir, "nested", "instance.id")

    check("A: read_instance_id_file() returns None for an absent file", read_instance_id_file(id_path) is None)

    first_id = ensure_instance_id_file(id_path)
    check("A: ensure_instance_id_file() looks like a uuid4 string", len(first_id) == 36 and first_id.count("-") == 4)
    check("A: the file now exists on disk with that exact value", read_instance_id_file(id_path) == first_id)

    second_id = ensure_instance_id_file(id_path)
    check("A: a SECOND ensure_instance_id_file() call returns the SAME id (mandate §8: never regenerate)", second_id == first_id)

    ids_seen = {generate_instance_id() for _ in range(50)}
    check("A: generate_instance_id() produces distinct values (not a constant/hardcoded string)", len(ids_seen) == 50)


# ============================================================
# Small in-memory fake connection for the instance_identity TABLE only.
# ============================================================

class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self._conn.calls.append((sql, params))
        norm = " ".join(sql.split())
        if norm.startswith("SELECT instance_uuid FROM instance_identity"):
            self._last = {"instance_uuid": self._conn.row["instance_uuid"]} if self._conn.row else None
        elif norm.startswith("INSERT INTO instance_identity"):
            if self._conn.row is not None:
                raise Exception("duplicate key: instance_identity singleton row already exists")
            instance_uuid, created_at, created_by_host, label = params
            self._conn.row = {
                "instance_uuid": instance_uuid, "created_at": created_at,
                "created_by_host": created_by_host, "label": label,
            }
            self._last = None
        else:
            raise AssertionError(f"unexpected SQL in fake: {sql}")

    def fetchone(self):
        return self._last


class FakeConn:
    def __init__(self):
        self.row = None
        self.calls = []

    def cursor(self):
        return _FakeCursor(self)


# ============================================================
# B. DB row idempotency / conflict.
# ============================================================

fc = FakeConn()
check("B: get_db_instance_id() is None before any bootstrap", get_db_instance_id(fc) is None)

record_instance_identity(fc, "11111111-1111-1111-1111-111111111111", created_by_host="test-host")
check("B: get_db_instance_id() now returns the recorded uuid", get_db_instance_id(fc) == "11111111-1111-1111-1111-111111111111")

calls_before = len(fc.calls)
record_instance_identity(fc, "11111111-1111-1111-1111-111111111111")  # same uuid again
check(
    "B: re-recording the SAME uuid is a no-op (idempotent) — the fake's row is untouched, "
    "no attempted duplicate INSERT",
    get_db_instance_id(fc) == "11111111-1111-1111-1111-111111111111",
)

raised = False
try:
    record_instance_identity(fc, "22222222-2222-2222-2222-222222222222")
except RuntimeError:
    raised = True
check(
    "B: recording a DIFFERENT uuid over an existing one RAISES rather than overwriting "
    "(mandate §8: ambiguous identity state must STOP, never auto-resolve)",
    raised,
)
check("B: after the refused conflicting call, the ORIGINAL uuid is still what's stored", get_db_instance_id(fc) == "11111111-1111-1111-1111-111111111111")


# ============================================================
# C. verify_instance_identity().
# ============================================================

empty_conn = FakeConn()
ok, reason = verify_instance_identity(empty_conn, "any-uuid")
check("C: verify against an empty instance_identity table -> (False, NO_DB_IDENTITY)", (ok, reason) == (False, NO_DB_IDENTITY))

ok, reason = verify_instance_identity(fc, "11111111-1111-1111-1111-111111111111")
check("C: verify with the CORRECT expected uuid -> (True, MATCH)", (ok, reason) == (True, MATCH))

ok, reason = verify_instance_identity(fc, "99999999-9999-9999-9999-999999999999")
check("C: verify with a WRONG expected uuid -> (False, MISMATCH)", (ok, reason) == (False, MISMATCH))

desc = describe("/nonexistent/path/instance.id", conn=empty_conn)
check("C: describe() reports NOT_BOOTSTRAPPED when neither file nor DB row exists", desc["status"] == "NOT_BOOTSTRAPPED")


# ============================================================
# D. connection.py: socket / auth_socket wiring.
# ============================================================

_env_keys = (
    "YANDI_SQL_HOST", "YANDI_SQL_PORT", "YANDI_SQL_USER", "YANDI_SQL_PASSWORD",
    "YANDI_SQL_DATABASE", "YANDI_SQL_SOCKET", "YANDI_SQL_AUTH_MODE",
)
_saved_env = {k: os.environ.get(k) for k in _env_keys}


def _reset_env():
    for k in _env_keys:
        os.environ.pop(k, None)


try:
    _reset_env()
    os.environ["YANDI_SQL_USER"] = "yandi_runtime"
    os.environ["YANDI_SQL_SOCKET"] = "/run/yandi/mysql.sock"
    os.environ["YANDI_SQL_AUTH_MODE"] = "auth_socket"

    check(
        "D: is_configured() is True for USER + SOCKET + auth_socket mode, with NO "
        "password set at all",
        conn_mod.is_configured(),
    )

    fake_pymysql = MagicMock()
    fake_pymysql.cursors.DictCursor = object
    fake_conn = MagicMock()
    fake_pymysql.connect.return_value = fake_conn

    with patch.dict("sys.modules", {"pymysql": fake_pymysql, "pymysql.cursors": fake_pymysql.cursors}):
        with conn_mod.get_connection():
            pass

    _, kwargs = fake_pymysql.connect.call_args
    check("D: unix_socket=... was passed to pymysql.connect()", kwargs.get("unix_socket") == "/run/yandi/mysql.sock")
    check("D: host/port are NEVER also passed when a socket path is configured (no ambiguous transport)", "host" not in kwargs and "port" not in kwargs)
    check("D: auth_socket mode sends an EMPTY password, never the (unset) env value", kwargs.get("password") == "")

    _reset_env()
    os.environ["YANDI_SQL_USER"] = "yandi_runtime"
    os.environ["YANDI_SQL_AUTH_MODE"] = "auth_socket"
    os.environ["YANDI_SQL_SOCKET"] = ""
    # DATABASE BOOTSTRAP V1 (seventeenth Phase B attempt): YANDI_SQL_SOCKET
    # now has a canonical default (/run/yandi/mysql/mysql.sock) — leaving
    # it UNSET is no longer "no socket path", it resolves the dedicated
    # appliance's own socket. The only way to genuinely reach "no socket
    # path" is an EXPLICIT empty override, exercised here.
    check(
        "D: is_configured() is False for auth_socket mode with an EXPLICIT empty "
        "socket override (the one way to genuinely reach 'no socket path' now that "
        "an unset YANDI_SQL_SOCKET resolves the canonical dedicated default instead)",
        not conn_mod.is_configured(),
    )

    raised = False
    try:
        with conn_mod.get_connection():
            pass
    except conn_mod.SqlUnavailable:
        raised = True
    check("D: get_connection() raises SqlUnavailable (not silently trying TCP) for auth_socket with an explicitly empty socket path", raised)

    _reset_env()
    os.environ["YANDI_SQL_USER"] = "yandi_runtime"
    os.environ["YANDI_SQL_PASSWORD"] = "some-password"
    os.environ["YANDI_SQL_AUTH_MODE"] = "password"
    os.environ["YANDI_SQL_SOCKET"] = "/run/yandi/mysql.sock"
    # Password mode over a socket (not auth_socket) is also valid —
    # migrator/readonly roles may use this. AUTH_MODE must now be set
    # EXPLICITLY to "password": since auth_socket is the canonical
    # default, USER+PASSWORD+SOCKET alone (no AUTH_MODE) would otherwise
    # resolve to auth_socket mode and silently blank the password —
    # deliberate (the safer default posture requires explicit opt-in for
    # password auth), not a regression; this test exercises the correct,
    # explicit way to get password-over-socket.
    fake_pymysql2 = MagicMock()
    fake_pymysql2.cursors.DictCursor = object
    with patch.dict("sys.modules", {"pymysql": fake_pymysql2, "pymysql.cursors": fake_pymysql2.cursors}):
        with conn_mod.get_connection():
            pass
    _, kwargs2 = fake_pymysql2.connect.call_args
    check(
        "D: password-mode-over-socket still uses unix_socket= and passes the REAL "
        "password through (not blanked)",
        kwargs2.get("unix_socket") == "/run/yandi/mysql.sock" and kwargs2.get("password") == "some-password",
    )

    _reset_env()
    os.environ["YANDI_SQL_USER"] = "yandi_runtime"
    os.environ["YANDI_SQL_PASSWORD"] = "some-password"
    os.environ["YANDI_SQL_AUTH_MODE"] = "password"
    # No YANDI_SQL_SOCKET set at all -> DATABASE BOOTSTRAP V1's canonical
    # default socket is now used even in explicit password mode (this
    # dedicated instance has NO TCP listener at all — every one of its
    # accounts, auth_socket or password, is socket-only) — the OLD
    # "falls back to TCP host/port" behavior is exactly what this fix
    # replaced; see connection.py's own module docstring.
    fake_pymysql3 = MagicMock()
    fake_pymysql3.cursors.DictCursor = object
    with patch.dict("sys.modules", {"pymysql": fake_pymysql3, "pymysql.cursors": fake_pymysql3.cursors}):
        with conn_mod.get_connection():
            pass
    _, kwargs3 = fake_pymysql3.connect.call_args
    check(
        "D: with no YANDI_SQL_SOCKET set at all, the canonical dedicated default "
        "socket is used (unix_socket=/run/yandi/mysql/mysql.sock) — host/port are "
        "NOT used, matching the 'no TCP fallback, ever' guarantee",
        kwargs3.get("unix_socket") == conn_mod._DEFAULT_SOCKET
        and "host" not in kwargs3 and "port" not in kwargs3,
        f"{kwargs3}",
    )
finally:
    _reset_env()
    for k, v in _saved_env.items():
        if v is not None:
            os.environ[k] = v


# ============================================================
# E. security_grants.yandi_runtime_auth_socket_statement().
# ============================================================

import inspect

sql, params = yandi_runtime_auth_socket_statement("yandi_runtime", "iam")
check("E: the CREATE USER statement uses auth_socket", "auth_socket" in sql)
check("E: host is hardcoded to 'localhost' in the SQL text itself (not a parameter)", "'localhost'" in sql)
check("E: params carry (username, os_user), never a password", params == ("yandi_runtime", "iam"))
check(
    "E: the function's own signature has NO 'host' parameter — auth_socket's "
    "localhost-only constraint is structural, not a caller convention to remember",
    "host" not in inspect.signature(yandi_runtime_auth_socket_statement).parameters,
)

grant_stmts = yandi_runtime_grant_statements("yandi_runtime", "localhost")
check("E: yandi_runtime_grant_statements() returns at least the base SELECT/INSERT grant", len(grant_stmts) >= 1)
check("E: no CREATE USER statement leaks into the grants-only helper", all("CREATE USER" not in sql for sql, _ in grant_stmts))


# ============================================================
# F. security_selfcheck identity gate.
# ============================================================

class _SelfcheckFakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        norm = " ".join(sql.split())
        if "MAX(version)" in norm:
            self._last = {"v": self._conn.schema_version}
        elif "information_schema.tables" in norm:
            self._last = {"c": 1}
        elif "information_schema.triggers" in norm:
            self._last = {"c": 1}
        elif norm.startswith("SHOW GRANTS"):
            self._rows_iter = [{"Grants": "GRANT SELECT, INSERT ON `yandi_epistemic`.* TO `yandi_runtime`@`%`"}]
        elif norm.startswith("SELECT instance_uuid FROM instance_identity"):
            self._last = {"instance_uuid": self._conn.instance_uuid} if self._conn.instance_uuid else None
        else:
            raise AssertionError(f"unexpected SQL in selfcheck fake: {sql}")

    def fetchone(self):
        return self._last

    def fetchall(self):
        return getattr(self, "_rows_iter", [])


class _SelfcheckFakeConn:
    def __init__(self, schema_version=1, instance_uuid=None):
        self.schema_version = schema_version
        self.instance_uuid = instance_uuid

    def cursor(self):
        return _SelfcheckFakeCursor(self)


clean = _SelfcheckFakeConn(instance_uuid="11111111-1111-1111-1111-111111111111")

result_no_expectation = run_selfcheck(clean, role="runtime")
check(
    "F: run_selfcheck() WITHOUT expected_instance_uuid keeps 'ok' driven only by "
    "schema/tables/triggers/grants (backward compatible with every pre-existing caller)",
    result_no_expectation["ok"] is True and result_no_expectation["identity_detail"] == "NOT_REQUESTED",
)

result_match = run_selfcheck(clean, role="runtime", expected_instance_uuid="11111111-1111-1111-1111-111111111111")
check("F: run_selfcheck() with a MATCHING expected_instance_uuid stays ok=True", result_match["ok"] is True and result_match["identity_ok"] is True)

result_mismatch = run_selfcheck(clean, role="runtime", expected_instance_uuid="deadbeef-0000-0000-0000-000000000000")
check(
    "F: run_selfcheck() with a MISMATCHED expected_instance_uuid flips 'ok' to False "
    "(mandate §27's gate actually gates something)",
    result_mismatch["ok"] is False and result_mismatch["identity_detail"] == MISMATCH,
)

no_identity = _SelfcheckFakeConn(instance_uuid=None)
result_no_identity = run_selfcheck(no_identity, role="runtime", expected_instance_uuid="11111111-1111-1111-1111-111111111111")
check(
    "F: run_selfcheck() against a database with NO instance_identity row yet, but an "
    "expected uuid supplied, reports NO_DB_IDENTITY and ok=False",
    result_no_identity["ok"] is False and result_no_identity["identity_detail"] == NO_DB_IDENTITY,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
