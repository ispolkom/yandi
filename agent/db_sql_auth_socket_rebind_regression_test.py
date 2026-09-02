"""
agent/db_sql_auth_socket_rebind_regression_test.py — "10-year bastion"
OS-identity separation (owner mandate: neither the owner, Claude, nor
any future AI session may ever be able to authenticate as
yandi_runtime — only the dedicated AGENT_OS_USER the real YANDI process
runs as).

STATIC/MOCK PROOF ONLY (mandate §55) — no live server exists to
bootstrap for real in this environment.

Covers the DRIFT-DETECTION gap this file exists to close: `CREATE USER
IF NOT EXISTS` is a true no-op against an ALREADY-EXISTING yandi_runtime
account — so changing deploy/install-yandi.sh's AGENT_OS_USER (e.g.
"iam" -> "yandi-agent") would silently leave an already-bootstrapped
live instance's yandi_runtime permanently bound to the OLD OS user
forever, without agent/db/sql/bootstrap.py's auth_socket_binding_
matches()/rebind_auth_socket_statement() (this file's subject).

Covers:
    1. auth_socket_binding_matches(): False for "doesn't exist", False
       for "exists but bound to a DIFFERENT os_user", False for "exists
       with the SAME authentication_string but a non-auth_socket
       plugin" (defense against a coincidental password value matching
       an os_user string), True only for an exact (plugin, os_user)
       match.
    2. rebind_auth_socket_statement(): produces a real ALTER USER
       statement whose params are (username, host, os_user) — never
       touches GRANT statements.
    3. run_bootstrap() end-to-end, three scenarios: (a) fresh bootstrap,
       no prior account -> plain CREATE USER path, zero ALTER USER
       calls; (b) already-live account bound to a STALE os_user ->
       exactly one ALTER USER rebind, then the (idempotent) CREATE USER
       IF NOT EXISTS is still issued as a harmless no-op; (c)
       already-live account ALREADY correctly bound -> zero ALTER USER
       calls (no unnecessary rebind churn every single bootstrap run).
    4. Structural: run_bootstrap()'s own source actually calls both
       auth_socket_binding_matches() and rebind_auth_socket_statement()
       in its auth_socket branch (catches a future refactor silently
       dropping the drift check while scenario 3 above still happens to
       pass for unrelated reasons).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_auth_socket_rebind_regression_test
"""
from __future__ import annotations

import inspect

from agent.db.sql.bootstrap import (
    auth_socket_binding_matches, rebind_auth_socket_statement, run_bootstrap,
)
import agent.db.sql.bootstrap as bootstrap_mod

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
# Minimal fake connection — only auth_socket-relevant state
# (mysql.user rows) is genuinely tracked; everything else run_
# bootstrap() touches along the way (CREATE DATABASE, CREATE TABLE,
# ALTER TABLE, CREATE/DROP TRIGGER, GRANT, schema_migrations INSERT) is
# a harmless no-op here — those are covered by their own dedicated
# regression files (db_sql_security_bootstrap_regression_test.py,
# db_sql_security_bootstrap's own trigger-drift coverage, etc.), not
# re-tested in this one.
# ============================================================

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.conn.calls.append((norm, params))
        upper = norm.strip().upper()
        self._result = None

        if upper.startswith("SELECT PLUGIN, AUTHENTICATION_STRING FROM MYSQL.USER"):
            username, host = params
            self._result = self.conn.users.get((username, host))
        elif upper.startswith("SELECT COUNT(*) AS C FROM MYSQL.USER"):
            username, host = params
            self._result = {"c": 1 if (username, host) in self.conn.users else 0}
        elif upper.startswith("ALTER USER"):
            username, host, os_user = params
            self.conn.users[(username, host)] = {
                "plugin": "auth_socket", "authentication_string": os_user,
            }
            self.conn.alter_user_calls += 1
        elif upper.startswith("CREATE USER IF NOT EXISTS"):
            self.conn.create_user_calls += 1
            if len(params) == 2:
                username, os_user = params
                key = (username, "localhost")
                if key not in self.conn.users:
                    self.conn.users[key] = {
                        "plugin": "auth_socket", "authentication_string": os_user,
                    }
            else:
                username, host, _pw = params
                self.conn.users.setdefault(
                    (username, host),
                    {"plugin": "mysql_native_password", "authentication_string": None},
                )
        elif "INFORMATION_SCHEMA.TRIGGERS" in upper:
            self._result = None if upper.startswith("SELECT ACTION_STATEMENT") else {"c": 0}

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.users = {}
        self.create_user_calls = 0
        self.alter_user_calls = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ============================================================
# 1. auth_socket_binding_matches()
# ============================================================

conn1 = FakeConnection()
check(
    "1: account doesn't exist at all -> False (not a match, not an error)",
    auth_socket_binding_matches(conn1, "yandi_runtime", "localhost", "yandi-agent") is False,
)

conn1.users[("yandi_runtime", "localhost")] = {
    "plugin": "auth_socket", "authentication_string": "iam",
}
check(
    "1: exists but bound to a DIFFERENT os_user -> False",
    auth_socket_binding_matches(conn1, "yandi_runtime", "localhost", "yandi-agent") is False,
)
check(
    "1: exists and bound to the SAME os_user -> True",
    auth_socket_binding_matches(conn1, "yandi_runtime", "localhost", "iam") is True,
)

conn1.users[("yandi_runtime", "localhost")] = {
    "plugin": "mysql_native_password", "authentication_string": "yandi-agent",
}
check(
    "1: authentication_string happens to equal the target os_user, but plugin is NOT "
    "auth_socket -> False (plugin must match too, not just the string value)",
    auth_socket_binding_matches(conn1, "yandi_runtime", "localhost", "yandi-agent") is False,
)

# ============================================================
# 2. rebind_auth_socket_statement()
# ============================================================

sql, params = rebind_auth_socket_statement("yandi_runtime", "localhost", "yandi-agent")
check(
    "2: rebind_auth_socket_statement() produces an ALTER USER statement",
    sql.strip().upper().startswith("ALTER USER"),
    sql,
)
check(
    "2: params are exactly (username, host, os_user)",
    params == ("yandi_runtime", "localhost", "yandi-agent"),
    f"{params}",
)
check(
    "2: never mentions GRANT (auth method change only, privileges untouched)",
    "GRANT" not in sql.upper(),
)

# ============================================================
# 3a. Fresh bootstrap, no prior account -> plain CREATE USER path.
# ============================================================

conn_fresh = FakeConnection()
result_fresh = run_bootstrap(
    conn_fresh, readonly_password="p2", migrator_password="p3",
    runtime_auth_socket_os_user="yandi-agent",
)
check(
    "3a: fresh bootstrap issues ZERO ALTER USER rebinds (nothing existed to drift from)",
    conn_fresh.alter_user_calls == 0,
    f"{conn_fresh.alter_user_calls}",
)
check(
    "3a: yandi_runtime ends up correctly bound to the target os_user",
    conn_fresh.users.get(("yandi_runtime", "localhost"), {}).get("authentication_string") == "yandi-agent",
    f"{conn_fresh.users.get(('yandi_runtime', 'localhost'))}",
)
check(
    "3a: run_bootstrap() still reports auth_socket mode",
    result_fresh["runtime_auth_mode"] == "auth_socket",
)

# ============================================================
# 3b. Already-live account bound to a STALE os_user -> exactly one
# ALTER USER rebind (the core bug this file exists to prevent).
# ============================================================

conn_stale = FakeConnection()
conn_stale.users[("yandi_runtime", "localhost")] = {
    "plugin": "auth_socket", "authentication_string": "iam",
}
result_stale = run_bootstrap(
    conn_stale, readonly_password="p2", migrator_password="p3",
    runtime_auth_socket_os_user="yandi-agent",
)
check(
    "3b: re-running bootstrap after AGENT_OS_USER changed ('iam' -> 'yandi-agent') "
    "issues EXACTLY ONE ALTER USER rebind — the fix for the core drift bug",
    conn_stale.alter_user_calls == 1,
    f"{conn_stale.alter_user_calls}",
)
check(
    "3b: yandi_runtime is REBOUND to the new os_user, not left stale forever",
    conn_stale.users[("yandi_runtime", "localhost")]["authentication_string"] == "yandi-agent",
    f"{conn_stale.users[('yandi_runtime', 'localhost')]}",
)

# ============================================================
# 3c. Already-live account ALREADY correctly bound -> zero ALTER USER
# calls (no rebind churn on every ordinary bootstrap re-run).
# ============================================================

conn_ok = FakeConnection()
conn_ok.users[("yandi_runtime", "localhost")] = {
    "plugin": "auth_socket", "authentication_string": "yandi-agent",
}
run_bootstrap(
    conn_ok, readonly_password="p2", migrator_password="p3",
    runtime_auth_socket_os_user="yandi-agent",
)
check(
    "3c: bootstrap re-run against an ALREADY-correctly-bound account issues ZERO "
    "ALTER USER calls (drift check correctly recognizes 'no drift')",
    conn_ok.alter_user_calls == 0,
    f"{conn_ok.alter_user_calls}",
)

# ============================================================
# 4. Structural: the real call sites exist in run_bootstrap()'s source.
# ============================================================

_src = inspect.getsource(bootstrap_mod.run_bootstrap)
check(
    "4: run_bootstrap()'s auth_socket branch actually calls auth_socket_binding_matches()",
    "auth_socket_binding_matches(" in _src,
)
check(
    "4: run_bootstrap()'s auth_socket branch actually calls rebind_auth_socket_statement()",
    "rebind_auth_socket_statement(" in _src,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
