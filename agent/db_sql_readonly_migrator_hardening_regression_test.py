"""
agent/db_sql_readonly_migrator_hardening_regression_test.py — "10-year
bastion" Layer 3 (owner mandate, following Layer 1's OS-identity
separation for yandi_runtime): YANDI_READONLY can now bind via
auth_socket to the OWNER's own personal OS login (no stored password
file), and YANDI_MIGRATOR — the one role that can change the schema —
no longer gets a standing account by default at all ("мы не root для
базы": no permanent credential, not even the owner's, should be able to
alter the schema whenever it likes).

STATIC/MOCK PROOF ONLY (mandate §55) — no live server exists to
bootstrap for real in this environment.

Covers:
    1. yandi_readonly_auth_socket_statement(): produces a CREATE USER
       ... IDENTIFIED WITH auth_socket statement, same shape as
       yandi_runtime_auth_socket_statement().
    2. run_bootstrap(readonly_auth_socket_os_user=...): fresh bootstrap
       binds yandi_readonly correctly; an already-live account bound to
       a STALE os_user gets rebound via the same auth_socket_binding_
       matches()/rebind_auth_socket_statement() drift-detection built
       for yandi_runtime in Layer 1 (proves the generic design pays
       off — zero new drift-detection code needed for this role).
    3. run_bootstrap() with NEITHER readonly_password NOR readonly_
       auth_socket_os_user given -> raises ValueError (never silently
       creates an unprotected account).
    4. run_bootstrap()'s NEW DEFAULT (provision_migrator omitted/False):
       yandi_migrator is NOT created on a fresh bootstrap, and result
       dict correctly reports migrator_provisioned=False / it is absent
       from roles_ensured and role_principals.
    5. Drift CLEANUP (the core of this file): an already-live instance
       from a PREVIOUS bootstrap (the old default that DID create
       yandi_migrator) gets it DROPped on the next bootstrap run, once
       provision_migrator is no longer requested — never left as a
       stale standing schema-change credential.
    6. Explicit break-glass (provision_migrator=True): still works
       exactly like the old default, and requires migrator_password
       (raises ValueError if blank).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_readonly_migrator_hardening_regression_test
"""
from __future__ import annotations

from agent.db.sql.bootstrap import run_bootstrap
from agent.db.sql.security_grants import yandi_readonly_auth_socket_statement

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
# Minimal fake connection — same pattern as agent/db_sql_auth_socket_
# rebind_regression_test.py: only mysql.user state is genuinely
# tracked; schema/trigger/GRANT statements are harmless no-ops (covered
# by their own dedicated regression files).
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
        elif upper.startswith("DROP USER IF EXISTS"):
            username, host = params
            self.conn.users.pop((username, host), None)
            self.conn.drop_user_calls.append((username, host))
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
        self.drop_user_calls = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ============================================================
# 1. yandi_readonly_auth_socket_statement()
# ============================================================

sql, params = yandi_readonly_auth_socket_statement("yandi_readonly", "iam")
check(
    "1: produces a CREATE USER ... IDENTIFIED WITH auth_socket statement",
    "CREATE USER" in sql.upper() and "AUTH_SOCKET" in sql.upper(),
    sql,
)
check("1: params are (username, os_user)", params == ("yandi_readonly", "iam"), f"{params}")

# ============================================================
# 2. run_bootstrap(readonly_auth_socket_os_user=...): fresh + drift rebind.
# ============================================================

conn_fresh = FakeConnection()
result_fresh = run_bootstrap(
    conn_fresh, runtime_password="rp", readonly_auth_socket_os_user="iam",
)
check(
    "2a: fresh bootstrap binds yandi_readonly to the given os_user",
    conn_fresh.users.get(("yandi_readonly", "localhost"), {}).get("authentication_string") == "iam",
    f"{conn_fresh.users.get(('yandi_readonly', 'localhost'))}",
)
check(
    "2a: result reports readonly_auth_mode='auth_socket'",
    result_fresh["readonly_auth_mode"] == "auth_socket",
)

conn_stale = FakeConnection()
conn_stale.users[("yandi_readonly", "localhost")] = {
    "plugin": "auth_socket", "authentication_string": "old-owner-login",
}
run_bootstrap(conn_stale, runtime_password="rp", readonly_auth_socket_os_user="iam")
check(
    "2b: an already-live yandi_readonly bound to a STALE os_user is rebound via ALTER USER "
    "(same drift-detection machinery Layer 1 built for yandi_runtime, reused with zero new code)",
    conn_stale.alter_user_calls == 1
    and conn_stale.users[("yandi_readonly", "localhost")]["authentication_string"] == "iam",
    f"alter_calls={conn_stale.alter_user_calls} state={conn_stale.users.get(('yandi_readonly', 'localhost'))}",
)

# ============================================================
# 3. Neither readonly_password nor readonly_auth_socket_os_user -> raises.
# ============================================================

def _raises_on_blank_readonly() -> bool:
    try:
        run_bootstrap(FakeConnection(), runtime_password="rp")
    except ValueError:
        return True
    except Exception:
        return False
    return False


check(
    "3: run_bootstrap() refuses to create YANDI_READONLY with no password AND no "
    "auth_socket os_user (never silently proceeds with an empty credential)",
    _raises_on_blank_readonly(),
)

# ============================================================
# 4. NEW DEFAULT: migrator NOT provisioned on a fresh bootstrap.
# ============================================================

conn_default = FakeConnection()
result_default = run_bootstrap(
    conn_default, runtime_password="rp", readonly_password="ro",
)
check(
    "4: fresh bootstrap with provision_migrator OMITTED does NOT create yandi_migrator "
    "(owner mandate: no standing schema-change account by default)",
    ("yandi_migrator", "localhost") not in conn_default.users,
    f"{conn_default.users}",
)
check(
    "4: result reports migrator_provisioned=False",
    result_default["migrator_provisioned"] is False,
)
check(
    "4: 'yandi_migrator' absent from roles_ensured",
    "yandi_migrator" not in result_default["roles_ensured"],
    f"{result_default['roles_ensured']}",
)
check(
    "4: 'migrator' absent from role_principals",
    "migrator" not in result_default["role_principals"],
    f"{result_default['role_principals']}",
)

# ============================================================
# 5. Drift CLEANUP: a previously-provisioned yandi_migrator (old
# default) gets DROPped once provision_migrator is no longer requested.
# ============================================================

conn_legacy = FakeConnection()
conn_legacy.users[("yandi_migrator", "localhost")] = {
    "plugin": "mysql_native_password", "authentication_string": None,
}
result_legacy = run_bootstrap(
    conn_legacy, runtime_password="rp", readonly_password="ro",
)
check(
    "5: a stale yandi_migrator from a previous (old-default) bootstrap is DROPped, not "
    "left as a standing credential — the actual fix this file exists for",
    ("yandi_migrator", "localhost") not in conn_legacy.users
    and ("yandi_migrator", "localhost") in conn_legacy.drop_user_calls,
    f"users={conn_legacy.users} drops={conn_legacy.drop_user_calls}",
)
check(
    "5: result still reports migrator_provisioned=False after cleanup",
    result_legacy["migrator_provisioned"] is False,
)

# ============================================================
# 6. Explicit break-glass: provision_migrator=True.
# ============================================================

conn_breakglass = FakeConnection()
result_bg = run_bootstrap(
    conn_breakglass, runtime_password="rp", readonly_password="ro",
    provision_migrator=True, migrator_password="mp",
)
check(
    "6a: provision_migrator=True with a password still creates yandi_migrator normally",
    ("yandi_migrator", "localhost") in conn_breakglass.users
    and result_bg["migrator_provisioned"] is True
    and "yandi_migrator" in result_bg["roles_ensured"]
    and "migrator" in result_bg["role_principals"],
)


def _raises_on_breakglass_without_password() -> bool:
    try:
        run_bootstrap(
            FakeConnection(), runtime_password="rp", readonly_password="ro",
            provision_migrator=True,
        )
    except ValueError:
        return True
    except Exception:
        return False
    return False


check(
    "6b: provision_migrator=True WITHOUT migrator_password raises (never silently "
    "creates the break-glass account with a blank credential)",
    _raises_on_breakglass_without_password(),
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
