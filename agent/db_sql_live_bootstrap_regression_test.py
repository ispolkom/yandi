"""
agent/db_sql_live_bootstrap_regression_test.py — DATABASE BOOTSTRAP V1,
mandate §7 Phase B / §11 / §32: agent/db/sql/live_bootstrap.py's pure
logic, offline only.

STATIC/MOCK PROOF ONLY — no live server exists this pass (same posture
as every other agent/db_sql_*_regression_test.py file). What IS proven:
    A. load_and_consume_fresh_init_marker() — reads and DELETES the
       one-time marker install-yandi.sh's initialize_datadir() writes
       directly from its OWN --initialize output; absent marker ->
       None (never an error); a consumed marker can never be read
       twice (mandate: reruns must not need the one-time password
       again). Replaces the old log-scraping approach entirely — see
       Case A/B/C below for why that was a real, live-confirmed bug.
    B. save_protected_secret()/load_protected_secret() — 0600, refuses
       to overwrite, real filesystem (a tmp dir), same contract as
       keys.py's save_kek()/load_kek() but for plain secrets.
    C. run()'s Case A/B/C auth-state handling (module docstring):
       C1 (Case A, fresh marker present) -> root conversion attempted
       via the mocked mysql CLI, marker consumed; C2 (Case B, marker
       absent, auth_socket already works) -> no conversion attempted,
       proceeds directly, idempotent across reruns (same instance_uuid,
       same secrets, never regenerated); C3 (Case C, marker absent AND
       auth_socket unreachable) -> LiveBootstrapError, run_bootstrap()
       never even attempted — no guessing, no historical log scan.
    D. no secret value (temp root password, generated readonly/migrator
       passwords) is ever printed by main() — grepped structurally.
    E. missing `mysql` CLI binary fails loud in both
       _retire_temporary_root_password() and the Case B auth_socket
       probe, never silently falls back to pymysql's SET-NAMES-
       incompatible path.

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
    load_and_consume_fresh_init_marker, save_protected_secret, load_protected_secret,
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
# A. Fresh-init marker: load-and-consume semantics.
# ============================================================

with tempfile.TemporaryDirectory() as tmpdir:
    marker_path = os.path.join(tmpdir, "fresh_init_temp_password")

    check(
        "A: load_and_consume_fresh_init_marker() returns None when the marker "
        "is absent (this invocation did not just run --initialize)",
        load_and_consume_fresh_init_marker(marker_path) is None,
    )

    with open(marker_path, "w", encoding="utf-8") as f:
        f.write("sUp3r$ecr3t!Tmp")
    check(
        "A: load_and_consume_fresh_init_marker() returns the exact captured value",
        load_and_consume_fresh_init_marker(marker_path) == "sUp3r$ecr3t!Tmp",
    )
    check(
        "A: the marker file is DELETED immediately after a successful read — "
        "it can never be reused across runs",
        not os.path.exists(marker_path),
    )
    check(
        "A: reading the (now-consumed) marker again returns None, not the old value",
        load_and_consume_fresh_init_marker(marker_path) is None,
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
# C. run() orchestration — Case A/B/C auth-state handling.
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
            # Realistic, PRINCIPAL-AWARE canned grants — run_selfcheck()'s
            # role_principals= path (DATABASE BOOTSTRAP V1 fix) issues
            # `SHOW GRANTS FOR %s@%s` per named account, each of which
            # must come back with ONLY that role's own legitimate
            # privileges, or a happy-path run() would spuriously fail
            # (e.g. readonly's FORBIDDEN list includes INSERT — a canned
            # response that always said "SELECT, INSERT" regardless of
            # which principal was asked about would falsely violate it).
            username = params[0] if params else None
            grants_by_user = {
                "yandi_runtime": "GRANT SELECT, INSERT ON `yandi_epistemic`.* TO `yandi_runtime`@`localhost`",
                "yandi_readonly": "GRANT SELECT ON `yandi_epistemic`.* TO `yandi_readonly`@`localhost`",
                "yandi_migrator": "GRANT CREATE, ALTER, INDEX, REFERENCES, CREATE VIEW, TRIGGER, DROP "
                                  "ON `yandi_epistemic`.* TO `yandi_migrator`@`localhost`",
            }
            self._rows = [{"g": grants_by_user.get(
                username, "GRANT SELECT, INSERT ON `yandi_epistemic`.* TO `yandi_runtime`@`localhost`",
            )}]
        else:
            pass  # every other DDL/GRANT in this flow is a pure side-effect statement

    def fetchone(self):
        return self._last

    def fetchall(self):
        return getattr(self, "_rows", [])


class _FakeConn:
    def __init__(self):
        self.executed = []
        self.instance_row = None

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def close(self):
        pass


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def _fake_mysql_cli_run(
    calls_log, root_converted_flag, probe_ok=True, alter_ok=True,
    plugin_initially_active=False, plugin_install_ok=True,
    plugin_active_after_install=True, exit_sandbox_ok=True,
    verification_ok=True,
):
    """Stand-in for subprocess.run(), covering EVERY mysql CLI call
    site in live_bootstrap.py's full auth_socket bootstrap sequence
    (twelfth Phase B attempt fix):
        1. _probe_temp_password_auth()'s SELECT 1 (Case A) / same
           statement shape reused by _root_reachable_via_auth_socket()
           (Case B/C, and the final post-conversion verification) —
           one probe_ok/verification_ok pair controls these.
        2. the THROWAWAY `ALTER USER ... IDENTIFIED BY '...'` that
           exits password-expiration sandbox mode (exit_sandbox_ok).
        3. the `SELECT PLUGIN_STATUS FROM INFORMATION_SCHEMA.PLUGINS`
           check (stateful: starts at plugin_initially_active, flips
           to True after a successful INSTALL PLUGIN if
           plugin_active_after_install).
        4. `INSTALL PLUGIN auth_socket SONAME 'auth_socket.so'`
           (plugin_install_ok).
        5. the REAL `ALTER USER ... IDENTIFIED WITH auth_socket`
           (alter_ok) — distinct from statement 2, which uses
           IDENTIFIED BY (a password), not IDENTIFIED WITH (a plugin).
    """
    state = {"plugin_active": plugin_initially_active}

    def _run(args, env=None, **kwargs):
        calls_log.append({"args": args, "env": env})
        joined = " ".join(args)

        if "IDENTIFIED WITH auth_socket" in joined:
            if alter_ok:
                root_converted_flag[0] = True
                return _FakeCompletedProcess(returncode=0)
            return _FakeCompletedProcess(
                returncode=1,
                stderr="ERROR 1524 (HY000): Plugin 'auth_socket' is not loaded",
            )

        if "IDENTIFIED BY" in joined:
            # the throwaway password, exits sandbox mode
            if exit_sandbox_ok:
                return _FakeCompletedProcess(returncode=0)
            return _FakeCompletedProcess(
                returncode=1,
                stderr="ERROR 1045 (28000): Access denied for user 'root'@'localhost' (using password: YES)",
            )

        if "PLUGIN_STATUS FROM INFORMATION_SCHEMA.PLUGINS" in joined:
            return _FakeCompletedProcess(returncode=0, stdout="ACTIVE" if state["plugin_active"] else "")

        if "INSTALL PLUGIN auth_socket" in joined:
            if not plugin_install_ok:
                return _FakeCompletedProcess(
                    returncode=1,
                    stderr="ERROR 1126 (HY000): Can't open shared library 'auth_socket.so'",
                )
            if plugin_active_after_install:
                state["plugin_active"] = True
            return _FakeCompletedProcess(returncode=0)

        if "SELECT 1" in joined:
            ok = probe_ok if not root_converted_flag[0] else verification_ok
            return _FakeCompletedProcess(
                returncode=0 if ok else 1,
                stderr="" if ok else "ERROR 1045 (28000): Access denied for user 'root'@'localhost' (using password: YES)",
            )

        return _FakeCompletedProcess(returncode=0)
    return _run


def _install_fake_pymysql(fake_conn: _FakeConn):
    fake_pymysql = MagicMock()
    fake_pymysql.cursors.DictCursor = object
    fake_pymysql.connect.return_value = fake_conn
    ctx = patch.dict("sys.modules", {
        "pymysql": fake_pymysql,
        "pymysql.cursors": fake_pymysql.cursors,
    })
    return ctx, fake_pymysql


# --- C1: Case A — fresh-init marker present, mysql CLI conversion. ---

with tempfile.TemporaryDirectory() as tmpdir:
    marker_path = os.path.join(tmpdir, "fresh_init_temp_password")
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write("initial-temp-pw-123")
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
            socket_path="/run/yandi/mysql.sock", fresh_init_marker=marker_path,
            instance_id_file=instance_id_file, secrets_dir=secrets_dir,
            agent_os_user="iam", created_by_host="test-host",
        )

    check("C1 (Case A): run() converts root to auth_socket when a fresh-init marker was present", _root_converted[0])
    check(
        "C1: the marker is CONSUMED (deleted) by run() — never reusable",
        not os.path.exists(marker_path),
    )

    _throwaway_calls = [c for c in _mysql_cli_calls if any("IDENTIFIED BY" in a for a in c["args"])]
    _alter_calls = [c for c in _mysql_cli_calls if any("IDENTIFIED WITH auth_socket" in a for a in c["args"])]
    _install_calls = [c for c in _mysql_cli_calls if any("INSTALL PLUGIN auth_socket" in a for a in c["args"])]
    check("C1: exactly one throwaway sandbox-exit ALTER USER (IDENTIFIED BY) happened", len(_throwaway_calls) == 1)
    check("C1: exactly one auth_socket plugin INSTALL happened (was not already active)", len(_install_calls) == 1)
    check("C1: exactly one REAL ALTER USER ... IDENTIFIED WITH auth_socket invocation happened", len(_alter_calls) == 1)
    _cli_call = _alter_calls[0] if _alter_calls else {"args": [], "env": {}}
    check(
        "C1: the REAL ALTER USER call targets the right socket and carries "
        "the auth_socket statement",
        any(a == "--socket=/run/yandi/mysql.sock" for a in _cli_call["args"])
        and any("ALTER USER 'root'@'localhost' IDENTIFIED WITH auth_socket" in a for a in _cli_call["args"]),
        f"args={_cli_call['args']}",
    )
    check(
        "C1: the ORIGINAL temp password reaches the mysql CLI ONLY via the "
        "throwaway-ALTER call's MYSQL_PWD env (never as a CLI argument "
        "visible to `ps`, and never reused for the REAL auth_socket ALTER, "
        "which correctly uses the throwaway password instead)",
        (_throwaway_calls[0]["env"] or {}).get("MYSQL_PWD") == "initial-temp-pw-123"
        and not any(
            "initial-temp-pw-123" in a
            for c in _mysql_cli_calls
            for a in c["args"]
        ),
        f"throwaway_env={_throwaway_calls[0]['env']!r}",
    )
    check(
        "C1: the REAL auth_socket ALTER call's MYSQL_PWD is the THROWAWAY "
        "password, not the original temp password (proves the sandbox-exit "
        "step's password is actually being used downstream, not discarded)",
        (_cli_call["env"] or {}).get("MYSQL_PWD") not in (None, "initial-temp-pw-123")
        and (_cli_call["env"] or {}).get("MYSQL_PWD") == (_install_calls[0]["env"] or {}).get("MYSQL_PWD"),
        f"real_alter_env_pwd_set={'MYSQL_PWD' in (_cli_call['env'] or {})}",
    )
    check("C1: run() records the instance identity in the database", conn1.instance_row == result1["instance_uuid"])
    check("C1: run_bootstrap()'s runtime_auth_mode is 'auth_socket' (not password)", result1["bootstrap"]["runtime_auth_mode"] == "auth_socket")
    check("C1: run() reports selfcheck_ok True in the clean happy path", result1["selfcheck_ok"] is True)

    readonly_secret = os.path.join(secrets_dir, "yandi_readonly.secret")
    migrator_secret = os.path.join(secrets_dir, "yandi_migrator.secret")
    check("C1: readonly secret file was written", os.path.exists(readonly_secret))
    check("C1: migrator secret file was written", os.path.exists(migrator_secret))
    check("C1: readonly secret file is 0600", oct(os.stat(readonly_secret).st_mode)[-3:] == "600")

    readonly_pw_1 = load_protected_secret(readonly_secret)
    migrator_pw_1 = load_protected_secret(migrator_secret)
    instance_uuid_1 = result1["instance_uuid"]

    # --- C2: Case B — SECOND run(), no marker (consumed above), root
    # ALREADY reachable via auth_socket (an earlier run converted it).
    # This is the live-confirmed-bug fix itself: a naive "re-scan the
    # error log" approach could pick up a STALE password here and fail;
    # the Case B probe must succeed directly with no password at all.
    conn2 = _FakeConn()
    _mysql_cli_calls_2 = []
    _root_converted_2 = [False]
    with _install_fake_pymysql(conn2)[0], \
         patch.object(lb_mod, "_MYSQL_CLIENT_BIN", "/usr/bin/mysql"), \
         patch.object(lb_mod.subprocess, "run", _fake_mysql_cli_run(_mysql_cli_calls_2, _root_converted_2, probe_ok=True)):
        result2 = run(
            socket_path="/run/yandi/mysql.sock", fresh_init_marker=marker_path,
            instance_id_file=instance_id_file, secrets_dir=secrets_dir,
            agent_os_user="iam", created_by_host="test-host",
        )

    check(
        "C2 (Case B): a SECOND run() with no marker present does NOT attempt "
        "the root-conversion ALTER USER again",
        not _root_converted_2[0],
    )
    check(
        "C2: the Case B auth_socket probe (SELECT 1) was actually used to "
        "verify root, not skipped/assumed",
        any("SELECT 1" in a for c in _mysql_cli_calls_2 for a in c["args"]),
    )
    check("C2: the SAME instance_uuid is reused across runs (never regenerated)", result2["instance_uuid"] == instance_uuid_1)
    check(
        "C2: readonly/migrator secrets are UNCHANGED after a second run (mandate §8: "
        "never rotate credentials as a side effect of re-running)",
        load_protected_secret(readonly_secret) == readonly_pw_1 and load_protected_secret(migrator_secret) == migrator_pw_1,
    )

    # --- C3: Case C — no marker AND auth_socket probe fails too ->
    # ambiguous auth state, must STOP with a precise error, never guess.
    conn3_unused = _FakeConn()
    with _install_fake_pymysql(conn3_unused)[0], \
         patch.object(lb_mod, "_MYSQL_CLIENT_BIN", "/usr/bin/mysql"), \
         patch.object(lb_mod.subprocess, "run", _fake_mysql_cli_run([], [False], probe_ok=False)):
        raised_c3 = False
        try:
            run(
                socket_path="/run/yandi/mysql.sock", fresh_init_marker=marker_path,
                instance_id_file=instance_id_file, secrets_dir=secrets_dir,
                agent_os_user="iam", created_by_host="test-host",
            )
        except LiveBootstrapError as e:
            raised_c3 = True
            _c3_err = str(e)

    check(
        "C3 (Case C): no marker + unreachable auth_socket raises "
        "LiveBootstrapError with a precise 'AMBIGUOUS AUTH STATE' diagnostic "
        "— never guesses, never scans a historical log",
        raised_c3 and "AMBIGUOUS" in _c3_err,
        f"raised={raised_c3}",
    )
    check(
        "C3: run_bootstrap() was never even attempted in the ambiguous case "
        "(no fabricated identity/schema state)",
        conn3_unused.instance_row is None and conn3_unused.executed == [],
    )


# ============================================================
# C4/C5: eighth Phase B attempt — isolated auth-probe error
# classification. Must NEVER collapse "wrong credential" and "ALTER
# USER itself failed" into one ambiguous message again.
# ============================================================

with tempfile.TemporaryDirectory() as tmpdir:
    marker_path = os.path.join(tmpdir, "fresh_init_temp_password")
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write("some-temp-pw-for-c4")
    instance_id_file = os.path.join(tmpdir, "instance.id")
    secrets_dir = os.path.join(tmpdir, "keys")

    # C4: probe itself fails (auth rejected, 1045) -> TEMP_PASSWORD_AUTH_FAILED,
    # ALTER USER must NEVER even be attempted.
    conn4 = _FakeConn()
    _ctx4, _ = _install_fake_pymysql(conn4)
    _calls4 = []
    _converted4 = [False]
    with _ctx4, \
         patch.object(lb_mod, "_MYSQL_CLIENT_BIN", "/usr/bin/mysql"), \
         patch.object(lb_mod.subprocess, "run", _fake_mysql_cli_run(_calls4, _converted4, probe_ok=False)):
        raised_c4 = False
        try:
            run(
                socket_path="/run/yandi/mysql.sock", fresh_init_marker=marker_path,
                instance_id_file=instance_id_file, secrets_dir=secrets_dir,
                agent_os_user="iam", created_by_host="test-host",
            )
        except LiveBootstrapError as e:
            raised_c4 = True
            _c4_err = str(e)

    check(
        "C4: auth probe failing (1045) raises with TEMP_PASSWORD_AUTH_FAILED "
        "specifically, distinguishing a credential-value bug from a SQL/"
        "plugin bug",
        raised_c4 and "TEMP_PASSWORD_AUTH_FAILED" in _c4_err,
        f"raised={raised_c4} err={_c4_err if raised_c4 else None!r}",
    )
    check(
        "C4: the ALTER USER statement is NEVER attempted when the auth "
        "probe itself already failed",
        not _converted4[0]
        and not any("ALTER USER" in a for c in _calls4 for a in c["args"]),
    )

with tempfile.TemporaryDirectory() as tmpdir:
    marker_path = os.path.join(tmpdir, "fresh_init_temp_password")
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write("some-temp-pw-for-c5")
    instance_id_file = os.path.join(tmpdir, "instance.id")
    secrets_dir = os.path.join(tmpdir, "keys")

    # C5: probe succeeds (auth OK, correctly sandbox-restricted) but the
    # SEPARATE ALTER USER statement itself fails -> AUTH_SOCKET_CONVERSION_FAILED.
    conn5 = _FakeConn()
    _ctx5, _ = _install_fake_pymysql(conn5)
    with _ctx5, \
         patch.object(lb_mod, "_MYSQL_CLIENT_BIN", "/usr/bin/mysql"), \
         patch.object(lb_mod.subprocess, "run", _fake_mysql_cli_run([], [False], probe_ok=True, alter_ok=False)):
        raised_c5 = False
        try:
            run(
                socket_path="/run/yandi/mysql.sock", fresh_init_marker=marker_path,
                instance_id_file=instance_id_file, secrets_dir=secrets_dir,
                agent_os_user="iam", created_by_host="test-host",
            )
        except LiveBootstrapError as e:
            raised_c5 = True
            _c5_err = str(e)

    check(
        "C5: auth probe succeeding but the ALTER USER statement itself "
        "failing raises with AUTH_SOCKET_CONVERSION_FAILED specifically — "
        "a SQL/plugin bug, explicitly distinguished from a credential bug",
        raised_c5 and "AUTH_SOCKET_CONVERSION_FAILED" in _c5_err,
        f"raised={raised_c5} err={_c5_err if raised_c5 else None!r}",
    )
    check(
        "C5: the error message references the plugin status reached "
        "(proving auth + plugin steps both succeeded) rather than "
        "re-blaming the credential",
        raised_c5 and "the plugin is" in _c5_err and "AUTH_SOCKET_PLUGIN" in _c5_err,
    )

    # The real secret value used in this test scenario must NEVER
    # appear in either exception message.
    check(
        "C4/C5: neither error message ever contains the actual temp "
        "password value used in these test scenarios",
        "some-temp-pw-for-c4" not in _c4_err and "some-temp-pw-for-c5" not in _c5_err,
    )


# ============================================================
# C6-C10: twelfth Phase B attempt — auth_socket plugin lifecycle.
# Live-confirmed bug: ERROR 1524 (HY000) "Plugin 'auth_socket' is not
# loaded" surfaced only AFTER auth succeeded and the ALTER USER
# statement was accepted by the sandbox-mode gate — a completely
# different failure class than a credential mismatch, requiring its
# own explicit states (mandate: no general "bootstrap failed" catch-all).
# ============================================================

def _run_with_plugin_scenario(**plugin_kwargs):
    with tempfile.TemporaryDirectory() as tmpdir:
        marker_path = os.path.join(tmpdir, "fresh_init_temp_password")
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write("plugin-scenario-temp-pw")
        instance_id_file = os.path.join(tmpdir, "instance.id")
        secrets_dir = os.path.join(tmpdir, "keys")

        conn = _FakeConn()
        ctx, _ = _install_fake_pymysql(conn)
        calls = []
        converted = [False]
        with ctx, \
             patch.object(lb_mod, "_MYSQL_CLIENT_BIN", "/usr/bin/mysql"), \
             patch.object(lb_mod.subprocess, "run", _fake_mysql_cli_run(calls, converted, **plugin_kwargs)):
            raised = False
            err = None
            try:
                run(
                    socket_path="/run/yandi/mysql/mysql.sock", fresh_init_marker=marker_path,
                    instance_id_file=instance_id_file, secrets_dir=secrets_dir,
                    agent_os_user="iam", created_by_host="test-host",
                )
            except LiveBootstrapError as e:
                raised = True
                err = str(e)
        return raised, err, calls, converted[0]

# C6: plugin already ACTIVE -> INSTALL PLUGIN is skipped entirely.
_raised, _err, _calls, _converted = _run_with_plugin_scenario(plugin_initially_active=True)
_install_calls_c6 = [c for c in _calls if any("INSTALL PLUGIN auth_socket" in a for a in c["args"])]
check(
    "C6: plugin already ACTIVE -> INSTALL PLUGIN auth_socket is NEVER "
    "attempted, conversion still succeeds",
    not _raised and _converted and len(_install_calls_c6) == 0,
    f"raised={_raised} err={_err} install_calls={len(_install_calls_c6)}",
)

# C7: auth_socket.so missing (INSTALL PLUGIN reports a missing shared
# library) -> AUTH_SOCKET_PLUGIN_NOT_FOUND, fail closed.
_raised, _err, _calls, _converted = _run_with_plugin_scenario(plugin_install_ok=False)
check(
    "C7: auth_socket.so missing/unreadable -> AUTH_SOCKET_PLUGIN_NOT_FOUND, "
    "fail closed, real ALTER USER never attempted",
    _raised and "AUTH_SOCKET_PLUGIN_NOT_FOUND" in _err and not _converted,
    f"raised={_raised} err={_err}",
)

# C8: INSTALL PLUGIN fails for a reason OTHER than a missing .so ->
# AUTH_SOCKET_PLUGIN_INSTALL_FAILED.
def _fake_mysql_cli_run_generic_install_failure(calls_log, root_converted_flag):
    def _run(args, env=None, **kwargs):
        calls_log.append({"args": args, "env": env})
        joined = " ".join(args)
        if "IDENTIFIED BY" in joined:
            return _FakeCompletedProcess(returncode=0)
        if "PLUGIN_STATUS FROM INFORMATION_SCHEMA.PLUGINS" in joined:
            return _FakeCompletedProcess(returncode=0, stdout="")
        if "INSTALL PLUGIN auth_socket" in joined:
            return _FakeCompletedProcess(returncode=1, stderr="ERROR 1045 (28000): some other server-side failure")
        if "SELECT 1" in joined:
            return _FakeCompletedProcess(returncode=0)
        return _FakeCompletedProcess(returncode=0)
    return _run

with tempfile.TemporaryDirectory() as tmpdir:
    marker_path = os.path.join(tmpdir, "fresh_init_temp_password")
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write("plugin-scenario-temp-pw-2")
    instance_id_file = os.path.join(tmpdir, "instance.id")
    secrets_dir = os.path.join(tmpdir, "keys")
    conn = _FakeConn()
    ctx, _ = _install_fake_pymysql(conn)
    with ctx, \
         patch.object(lb_mod, "_MYSQL_CLIENT_BIN", "/usr/bin/mysql"), \
         patch.object(lb_mod.subprocess, "run", _fake_mysql_cli_run_generic_install_failure([], [False])):
        _raised8 = False
        _err8 = None
        try:
            run(
                socket_path="/run/yandi/mysql/mysql.sock", fresh_init_marker=marker_path,
                instance_id_file=instance_id_file, secrets_dir=secrets_dir,
                agent_os_user="iam", created_by_host="test-host",
            )
        except LiveBootstrapError as e:
            _raised8 = True
            _err8 = str(e)
check(
    "C8: INSTALL PLUGIN fails for a non-missing-library reason -> "
    "AUTH_SOCKET_PLUGIN_INSTALL_FAILED",
    _raised8 and "AUTH_SOCKET_PLUGIN_INSTALL_FAILED" in _err8,
    f"raised={_raised8} err={_err8}",
)

# C9: INSTALL PLUGIN reports success but plugin never actually becomes
# ACTIVE -> AUTH_SOCKET_PLUGIN_NOT_ACTIVE, ambiguous state, fail closed.
_raised, _err, _calls, _converted = _run_with_plugin_scenario(plugin_active_after_install=False)
check(
    "C9: INSTALL PLUGIN succeeds but a follow-up check finds it still "
    "not ACTIVE -> AUTH_SOCKET_PLUGIN_NOT_ACTIVE, fail closed",
    _raised and "AUTH_SOCKET_PLUGIN_NOT_ACTIVE" in _err and not _converted,
    f"raised={_raised} err={_err}",
)

# C10: the REAL ALTER USER succeeds but the POST-conversion passwordless
# verification connection fails -> AUTH_SOCKET_VERIFICATION_FAILED
# (mandate: never trust the ALTER USER's own success alone).
_raised, _err, _calls, _converted = _run_with_plugin_scenario(verification_ok=False)
check(
    "C10: ALTER USER succeeds but the post-conversion passwordless "
    "verification connection fails -> AUTH_SOCKET_VERIFICATION_FAILED "
    "(the ALTER USER's own reported success is never trusted alone)",
    _raised and "AUTH_SOCKET_VERIFICATION_FAILED" in _err,
    f"raised={_raised} err={_err} converted={_converted}",
)

# C1 happy path already proves: plugin-not-active -> INSTALL -> ACTIVE ->
# real ALTER USER -> verification succeeds -> AUTH_SOCKET_READY.


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

# The eighth-attempt diagnostic print()s (PYTHON_LEN/PYTHON_FP,
# MYSQL_AUTH) live in run()/_retire_temporary_root_password(), not
# main() — statically confirm they interpolate only len()/_fingerprint()
# of the secret variables, never the raw variable itself.
_diag_print_lines = [
    line for line in (inspect.getsource(lb_mod.run) + inspect.getsource(lb_mod._retire_temporary_root_password)).splitlines()
    if line.strip().startswith("print(")
]
check(
    "D2: run()/_retire_temporary_root_password()'s diagnostic print() "
    "lines exist and interpolate only len(...)/_fingerprint(...) of the "
    "secret variable, never the raw variable by itself",
    len(_diag_print_lines) >= 2
    and all(
        ("temp_password}" not in line and "temp_password:" not in line)
        for line in _diag_print_lines
    ),
    f"{_diag_print_lines}",
)

with tempfile.TemporaryDirectory() as tmpdir:
    marker_path = os.path.join(tmpdir, "fresh_init_temp_password")
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write("another-temp-pw-456")
    instance_id_file = os.path.join(tmpdir, "instance.id")
    secrets_dir = os.path.join(tmpdir, "keys")

    conn4 = _FakeConn()
    buf = io.StringIO()
    with _install_fake_pymysql(conn4)[0], \
         patch.object(lb_mod, "_MYSQL_CLIENT_BIN", "/usr/bin/mysql"), \
         patch.object(lb_mod.subprocess, "run", _fake_mysql_cli_run([], [False])):
        with redirect_stdout(buf):
            rc = main([
                "--socket", "/run/yandi/mysql.sock", "--fresh-init-marker", marker_path,
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

    _probe_result_no_binary = lb_mod._root_reachable_via_auth_socket("/run/yandi/mysql.sock")

check(
    "E: missing mysql CLI binary raises LiveBootstrapError with a clear "
    "message (root conversion path), never silently falls back to pymysql's "
    "SET-NAMES-incompatible path",
    raised and "mysql" in _err_text.lower(),
)
check(
    "E: missing mysql CLI binary also makes the Case B auth_socket probe "
    "report unreachable (False), rather than raising or assuming success",
    _probe_result_no_binary is False,
)


# ============================================================
# F. Eleventh Phase B attempt: --no-defaults on every mysql CLI
# invocation, positioned FIRST (same positional requirement as
# mysqld's own --defaults-file). Live-confirmed bug: TEMP_SOURCE_FP/
# MARKER_FP/PYTHON_FP matched EXACTLY across three separate live runs
# yet auth still failed with 1045 — root cause was an AMBIENT config
# file (root's own ~/.my.cnf or a global my.cnf) supplying an unwanted
# password that mysql CLI reads by default. --no-defaults disables all
# config-file reading, leaving only explicit CLI flags/env vars.
# ============================================================

_probe_src = inspect.getsource(lb_mod._probe_temp_password_auth)
_retire_src = inspect.getsource(lb_mod._retire_temporary_root_password)
_reachable_src = inspect.getsource(lb_mod._root_reachable_via_auth_socket)

for _label, _src in (
    ("_probe_temp_password_auth() (the SELECT 1 probe)", _probe_src),
    ("_retire_temporary_root_password() (the ALTER USER call)", _retire_src),
    ("_root_reachable_via_auth_socket() (the Case B probe)", _reachable_src),
):
    check(
        f"F: {_label} passes --no-defaults to the mysql CLI, as the FIRST "
        f"argument after the binary path (same positional requirement as "
        f"mysqld's own --defaults-file bug found earlier this mandate)",
        "_MYSQL_CLIENT_BIN, \"--no-defaults\"," in _src,
        f"source snippet did not contain the expected argv ordering",
    )


print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
