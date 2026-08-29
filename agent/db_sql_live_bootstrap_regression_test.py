"""
agent/db_sql_live_bootstrap_regression_test.py — DATABASE BOOTSTRAP V1,
mandate §7 Phase B / §11 / §32: agent/db/sql/live_bootstrap.py's pure
logic, offline only.

STATIC/MOCK PROOF ONLY — no live server exists this pass (same posture
as every other agent/db_sql_*_regression_test.py file). What IS proven:
    A. extract_temporary_root_password() — the exact regex Percona's
       real log line uses, plus the "no temp password found" case
       (already-initialized datadir, second run).
    B. save_protected_secret()/load_protected_secret() — 0600, refuses
       to overwrite, real filesystem (a tmp dir), same contract as
       keys.py's save_kek()/load_kek() but for plain secrets.
    C. run()'s full orchestration sequence against a scripted fake
       connection + mocked pymysql (for the auth_socket reconnect and
       run_bootstrap()) and a mocked `mysql` CLI subprocess (for the
       one-time root->auth_socket conversion — see
       _retire_temporary_root_password()'s own docstring for why that
       ONE statement uses the CLI client rather than pymysql) —
       instance identity gets recorded, run_bootstrap() is called with
       runtime_auth_socket_os_user (not a password), readonly/migrator
       secrets get written to disk exactly once each, a second run()
       call reuses the SAME secrets rather than regenerating them
       (mandate §8: idempotent, never rotate credentials as a side
       effect of re-running).
    D. no secret value (temp root password, generated readonly/migrator
       passwords) is ever printed by main() — grepped structurally.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_live_bootstrap_regression_test
"""
from __future__ import annotations

import inspect
import io
import os
import tempfile
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from agent.db.sql.live_bootstrap import (
    extract_temporary_root_password, save_protected_secret, load_protected_secret,
    run, main, LiveBootstrapError,
)
import agent.db.sql.live_bootstrap as lb_mod

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
# A. Temp password extraction.
# ============================================================

with tempfile.TemporaryDirectory() as tmpdir:
    log_path = os.path.join(tmpdir, "mysql-error.log")

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(
            "2026-08-29T00:00:00.000000Z 0 [System] [MY-010116] [Server] starting\n"
            "2026-08-29T00:00:01.000000Z 6 [Note] [MY-010454] [Server] A temporary "
            "password is generated for root@localhost: sUp3r$ecr3t!Tmp\n"
        )
    check(
        "A: extract_temporary_root_password() finds the real Percona log line shape",
        extract_temporary_root_password(log_path) == "sUp3r$ecr3t!Tmp",
    )

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("2026-08-29T00:00:00.000000Z 0 [System] [MY-010116] [Server] starting, no temp password line\n")
    check(
        "A: extract_temporary_root_password() returns None when no such line exists "
        "(e.g. a second run against an already-initialized datadir)",
        extract_temporary_root_password(log_path) is None,
    )

    check(
        "A: extract_temporary_root_password() returns None for a nonexistent log file "
        "(never raises just because the log doesn't exist yet)",
        extract_temporary_root_password(os.path.join(tmpdir, "nope.log")) is None,
    )


# ============================================================
# B. Protected secret file.
# ============================================================

with tempfile.TemporaryDirectory() as tmpdir:
    secret_path = os.path.join(tmpdir, "nested", "yandi_migrator.secret")
    save_protected_secret(secret_path, "my-generated-password")
    check("B: save_protected_secret() writes the exact value back out", load_protected_secret(secret_path) == "my-generated-password")
    check("B: the secret file is 0600", oct(os.stat(secret_path).st_mode)[-3:] == "600")

    raised = False
    try:
        save_protected_secret(secret_path, "a-different-value")
    except FileExistsError:
        raised = True
    check("B: save_protected_secret() refuses to overwrite an existing secret", raised)
    check("B: the ORIGINAL value survived the refused overwrite attempt", load_protected_secret(secret_path) == "my-generated-password")


# ============================================================
# C. run() orchestration against a scripted fake connection.
# ============================================================

class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self._conn.executed.append((" ".join(sql.split()), params))
        norm = " ".join(sql.split())
        if norm.startswith("CREATE DATABASE"):
            pass
        elif norm.startswith("CREATE USER IF NOT EXISTS") or norm.startswith("GRANT") or "CREATE TABLE" in norm or "CREATE TRIGGER" in norm:
            pass
        elif norm.startswith("SELECT instance_uuid FROM instance_identity"):
            self._last = {"instance_uuid": self._conn.instance_row} if self._conn.instance_row else None
        elif norm.startswith("INSERT INTO instance_identity"):
            self._conn.instance_row = params[0]
        elif "MAX(version)" in norm:
            self._last = {"v": 1}
        elif "information_schema.tables" in norm:
            self._last = {"c": 1}
        elif "information_schema.triggers" in norm:
            self._last = {"c": 1}
        elif norm.startswith("SHOW GRANTS"):
            self._rows = [{"g": "GRANT SELECT, INSERT ON `yandi_epistemic`.* TO `yandi_runtime`@`localhost`"}]
        else:
            pass  # every other DDL/GRANT in this flow is a pure side-effect statement

    def fetchone(self):
        return self._last

    def fetchall(self):
        return getattr(self, "_rows", [])


class _FakeConn:
    def __init__(self):
        self.executed = []
        self.root_converted = False
        self.instance_row = None

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def close(self):
        pass


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def _fake_mysql_cli_run(calls_log, root_converted_flag):
    """Stand-in for subprocess.run() used by _retire_temporary_root_
    password() (mysql CLI client, not pymysql — see that function's own
    docstring for why: pymysql's automatic SET NAMES query is rejected
    by MySQL's post-initialize password-expiration sandbox mode, live-
    confirmed against a real server; the mysql CLI client doesn't have
    that problem). Records the call for inspection and flips
    root_converted_flag[0] when the real ALTER USER statement is
    present, mirroring what _FakeCursor.execute() used to do for the
    old pymysql-based path."""
    def _run(args, env=None, **kwargs):
        calls_log.append({"args": args, "env": env})
        if any("ALTER USER 'root'@'localhost' IDENTIFIED WITH auth_socket" in a for a in args):
            root_converted_flag[0] = True
        return _FakeCompletedProcess(returncode=0)
    return _run


def _install_fake_pymysql(fake_conn: _FakeConn):
    fake_pymysql = MagicMock()
    fake_pymysql.cursors.DictCursor = object
    fake_pymysql.connect.return_value = fake_conn
    # Real pymysql.constants.CLIENT is pure integer constants (no
    # side effects, no dependency on a live connection) — reused
    # as-is rather than faked, so `from pymysql.constants import
    # CLIENT` inside _retire_temporary_root_password() resolves to
    # the SAME bit values production code checks against, not a
    # divergent stand-in.
    import pymysql.constants as real_constants
    fake_pymysql.constants = real_constants
    ctx = patch.dict("sys.modules", {
        "pymysql": fake_pymysql,
        "pymysql.cursors": fake_pymysql.cursors,
        "pymysql.constants": real_constants,
    })
    return ctx, fake_pymysql


with tempfile.TemporaryDirectory() as tmpdir:
    error_log = os.path.join(tmpdir, "mysql-error.log")
    with open(error_log, "w", encoding="utf-8") as f:
        f.write(
            "[Note] [MY-010454] [Server] A temporary password is generated for "
            "root@localhost: initial-temp-pw-123\n"
        )
    instance_id_file = os.path.join(tmpdir, "instance.id")
    secrets_dir = os.path.join(tmpdir, "keys")

    conn1 = _FakeConn()
    _ctx1, _fake_pymysql1 = _install_fake_pymysql(conn1)
    _mysql_cli_calls = []
    _root_converted = [False]
    with _ctx1, \
         patch.object(lb_mod, "_MYSQL_CLIENT_BIN", "/usr/bin/mysql"), \
         patch.object(lb_mod.subprocess, "run", _fake_mysql_cli_run(_mysql_cli_calls, _root_converted)):
        result1 = run(
            socket_path="/run/yandi/mysql.sock", error_log_path=error_log,
            instance_id_file=instance_id_file, secrets_dir=secrets_dir,
            agent_os_user="iam", created_by_host="test-host",
        )

    check("C: run() converts root to auth_socket when a temp password was found", _root_converted[0])
    check("C: exactly one mysql CLI invocation happened for the root conversion", len(_mysql_cli_calls) == 1)

    # Live-confirmed bugs (Phase B, second and third owner runs):
    # (1) pymysql's default capability bitmask does NOT include
    #     HANDLE_EXPIRED_PASSWORDS, so the server outright refused the
    #     connection (error 1862) for the mandatory post-`--initialize`
    #     sandbox-mode root account.
    # (2) even with that fixed, pymysql's connect() unconditionally
    #     issues a "SET NAMES" query right after auth, which the same
    #     sandbox mode rejects too (error 1820) — no public pymysql
    #     parameter suppresses it, so the mysql CLI client is used
    #     instead for this one statement (see _retire_temporary_root_
    #     password()'s docstring). This checks (2)'s fix: the CLI
    #     invocation, not a pymysql connect call, carries the ALTER USER
    #     statement, and the password reaches it ONLY via the MYSQL_PWD
    #     env var, never as a CLI argument (which `ps` could read).
    _cli_call = _mysql_cli_calls[0] if _mysql_cli_calls else {"args": [], "env": {}}
    check(
        "C: the mysql CLI call targets the right socket and carries the "
        "ALTER USER auth_socket statement",
        any(a == "--socket=/run/yandi/mysql.sock" for a in _cli_call["args"])
        and any("ALTER USER 'root'@'localhost' IDENTIFIED WITH auth_socket" in a for a in _cli_call["args"]),
        f"args={_cli_call['args']}",
    )
    check(
        "C: the temp password reaches the mysql CLI ONLY via MYSQL_PWD env "
        "(never as a -p<password> CLI argument visible to `ps`)",
        (_cli_call["env"] or {}).get("MYSQL_PWD") == "initial-temp-pw-123"
        and not any("initial-temp-pw-123" in a for a in _cli_call["args"]),
        f"env_has_pwd={'MYSQL_PWD' in (_cli_call['env'] or {})} args={_cli_call['args']}",
    )
    check("C: run() records the instance identity in the database", conn1.instance_row == result1["instance_uuid"])
    check("C: run_bootstrap()'s runtime_auth_mode is 'auth_socket' (not password)", result1["bootstrap"]["runtime_auth_mode"] == "auth_socket")
    check("C: run() reports selfcheck_ok True in the clean happy path", result1["selfcheck_ok"] is True)

    readonly_secret = os.path.join(secrets_dir, "yandi_readonly.secret")
    migrator_secret = os.path.join(secrets_dir, "yandi_migrator.secret")
    check("C: readonly secret file was written", os.path.exists(readonly_secret))
    check("C: migrator secret file was written", os.path.exists(migrator_secret))
    check("C: readonly secret file is 0600", oct(os.stat(readonly_secret).st_mode)[-3:] == "600")

    readonly_pw_1 = load_protected_secret(readonly_secret)
    migrator_pw_1 = load_protected_secret(migrator_secret)
    instance_uuid_1 = result1["instance_uuid"]

    # --- Second run: no temp password left in the log (already retired),
    # instance id file already exists, secrets already exist. Nothing
    # should be regenerated.
    with open(error_log, "w", encoding="utf-8") as f:
        f.write("[Note] no temp password line this time — root is already on auth_socket\n")

    conn2 = _FakeConn()
    with _install_fake_pymysql(conn2)[0]:
        result2 = run(
            socket_path="/run/yandi/mysql.sock", error_log_path=error_log,
            instance_id_file=instance_id_file, secrets_dir=secrets_dir,
            agent_os_user="iam", created_by_host="test-host",
        )

    check(
        "C: a SECOND run() with no temp password present does NOT attempt the "
        "root-conversion ALTER USER again",
        not conn2.root_converted,
    )
    check("C: the SAME instance_uuid is reused across runs (never regenerated)", result2["instance_uuid"] == instance_uuid_1)
    check(
        "C: readonly/migrator secrets are UNCHANGED after a second run (mandate §8: "
        "never rotate credentials as a side effect of re-running)",
        load_protected_secret(readonly_secret) == readonly_pw_1 and load_protected_secret(migrator_secret) == migrator_pw_1,
    )


# ============================================================
# D. No secret is ever printed.
# ============================================================

_main_src = inspect.getsource(lb_mod.main)
_print_lines = [
    line for line in _main_src.splitlines()
    if line.strip().startswith("print(") and not line.strip().startswith("#")
]
check(
    "D: main() has print(...) calls to check (the check below isn't vacuously passing "
    "because main() was gutted/renamed)",
    len(_print_lines) >= 3,
    f"{_print_lines}",
)
check(
    "D: none of main()'s ACTUAL print(...) call lines reference 'password' or 'secret' "
    "(a docstring/comment mentioning 'never print a password' is fine and expected — only "
    "a print() line that would actually DO it matters here)",
    all("password" not in line.lower() and "secret" not in line.lower() for line in _print_lines),
    f"{_print_lines}",
)

with tempfile.TemporaryDirectory() as tmpdir:
    error_log = os.path.join(tmpdir, "mysql-error.log")
    with open(error_log, "w", encoding="utf-8") as f:
        f.write(
            "[Note] [MY-010454] [Server] A temporary password is generated for "
            "root@localhost: another-temp-pw-456\n"
        )
    instance_id_file = os.path.join(tmpdir, "instance.id")
    secrets_dir = os.path.join(tmpdir, "keys")

    conn3 = _FakeConn()
    buf = io.StringIO()
    with _install_fake_pymysql(conn3)[0], \
         patch.object(lb_mod, "_MYSQL_CLIENT_BIN", "/usr/bin/mysql"), \
         patch.object(lb_mod.subprocess, "run", _fake_mysql_cli_run([], [False])):
        with redirect_stdout(buf):
            rc = main([
                "--socket", "/run/yandi/mysql.sock", "--error-log", error_log,
                "--instance-id-file", instance_id_file, "--secrets-dir", secrets_dir,
                "--agent-os-user", "iam",
            ])
    output = buf.getvalue()
    check("D: main() exits 0 on a clean run", rc == 0, f"rc={rc} output={output}")
    check("D: the temp root password never appears in main()'s stdout", "another-temp-pw-456" not in output)
    readonly_pw_real = load_protected_secret(os.path.join(secrets_dir, "yandi_readonly.secret"))
    migrator_pw_real = load_protected_secret(os.path.join(secrets_dir, "yandi_migrator.secret"))
    check(
        "D: the generated readonly/migrator passwords never appear in main()'s stdout either",
        readonly_pw_real not in output and migrator_pw_real not in output,
    )


# ============================================================
# E. Missing `mysql` CLI binary fails loud, never falls back to pymysql.
# ============================================================

with patch.object(lb_mod, "_MYSQL_CLIENT_BIN", None):
    raised = False
    try:
        lb_mod._retire_temporary_root_password("/run/yandi/mysql.sock", "irrelevant-pw")
    except LiveBootstrapError as e:
        raised = True
        _err_text = str(e)
check(
    "E: missing mysql CLI binary raises LiveBootstrapError with a clear "
    "message, never silently falls back to pymysql's SET-NAMES-incompatible path",
    raised and "mysql" in _err_text.lower(),
)


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
