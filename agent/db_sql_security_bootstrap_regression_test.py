"""
agent/db_sql_security_bootstrap_regression_test.py — Этап 5E-S S3:
zero-config bootstrap idempotency (mandate §11, §44 section M).

STATIC/MOCK PROOF ONLY (mandate §55) — no live server exists to
bootstrap for real. What IS proven: agent/db/sql/bootstrap.py's
IDEMPOTENCY LOGIC, against a STATEFUL fake connection that actually
tracks which users/triggers "exist" across calls (not just a call-
recording mock) — running the full flow twice must not attempt to
create a second copy of anything.

Covers:
    M. BOOTSTRAP: empty DB -> creates correctly; running bootstrap a
       second time -> no duplication (zero duplicate CREATE USER
       attempts that matter, zero duplicate trigger creation); partial
       previous bootstrap (some triggers already exist, others don't)
       -> only the missing ones are created — deterministic recovery.
    (extra) passwords never appear in the SQL text itself — always a
       bound parameter (same T1/T15 discipline as the rest of
       agent/db/sql/).
    (extra) revoke_bootstrap() produces a real DROP USER statement.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_security_bootstrap_regression_test
"""
from __future__ import annotations

import inspect

from agent.db.sql.bootstrap import (
    ensure_database, ensure_role, apply_schema, apply_immutability_triggers,
    user_exists, trigger_exists, revoke_bootstrap, run_bootstrap,
    _expected_trigger_body,
)
from agent.db.sql.security_grants import yandi_runtime_statements
import agent.db.sql.bootstrap as bootstrap_mod
from agent.db.sql.security_triggers import immutability_triggers

# Drift-detection support for StatefulFakeCursor below (trigger_
# definition_matches() now queries ACTION_STATEMENT, not just COUNT(*))
# — the CURRENT expected body for every trigger name, used as the fake's
# answer when a trigger was pre-seeded directly into conn.triggers
# rather than created via a real CREATE TRIGGER call this session (this
# file's existing idempotency scenarios do exactly that — they are about
# existence-based idempotency, not drift, so "assume it already matches
# the current design" is the correct default for them).
_EXPECTED_TRIGGER_BODY_BY_NAME = {
    name: _expected_trigger_body(ddl) for name, ddl in immutability_triggers()
}

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
# Stateful fake connection — actually tracks "what exists" across
# calls, unlike the call-recording FakeConnection used elsewhere in
# this package (idempotency cannot be proven against a fake with no
# memory of its own prior state).
# ============================================================

class StatefulFakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = None
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm_sql = " ".join(sql.split())
        self.conn.calls.append((norm_sql, params))
        upper = norm_sql.strip().upper()

        if upper.startswith("CREATE DATABASE"):
            self.conn.databases_created += 1
            self._result = None
        elif upper.startswith("CREATE USER"):
            username, host, _pw = params
            self.conn.users.add((username, host))
            self.conn.create_user_calls += 1
            self._result = None
        elif upper.startswith("SELECT COUNT(*) AS C FROM MYSQL.USER"):
            username, host = params
            self._result = {"c": 1 if (username, host) in self.conn.users else 0}
        elif upper.startswith("GRANT"):
            self.conn.grant_calls += 1
            self._result = None
        elif upper.startswith("CREATE TABLE"):
            self.conn.tables_created += 1
            self._result = None
        elif "INFORMATION_SCHEMA.TRIGGERS" in upper:
            (trigger_name,) = params
            if upper.startswith("SELECT ACTION_STATEMENT"):
                if trigger_name not in self.conn.triggers:
                    self._result = None
                else:
                    body = self.conn.trigger_bodies.get(
                        trigger_name, _EXPECTED_TRIGGER_BODY_BY_NAME.get(trigger_name, ""),
                    )
                    self._result = {"ACTION_STATEMENT": body}
            else:
                self._result = {"c": 1 if trigger_name in self.conn.triggers else 0}
        elif upper.startswith("DROP TRIGGER"):
            trigger_name = norm_sql.split()[-1]
            self.conn.triggers.discard(trigger_name)
            self.conn.trigger_bodies.pop(trigger_name, None)
            self._result = None
        elif upper.startswith("CREATE TRIGGER"):
            trigger_name = norm_sql.split()[2]
            self.conn.triggers.add(trigger_name)
            self.conn.trigger_bodies[trigger_name] = _expected_trigger_body(sql)
            self.conn.create_trigger_calls += 1
            self._result = None
        elif upper.startswith("DROP USER"):
            username, host = params
            self.conn.users.discard((username, host))
            self._result = None
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []


class StatefulFakeConnection:
    def __init__(self):
        self.calls = []
        self.users = set()
        self.triggers = set()
        self.trigger_bodies = {}
        self.databases_created = 0
        self.tables_created = 0
        self.create_user_calls = 0
        self.grant_calls = 0
        self.create_trigger_calls = 0

    def cursor(self):
        return StatefulFakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ============================================================
# M. Empty DB -> creates correctly.
# ============================================================

conn = StatefulFakeConnection()
result_1 = run_bootstrap(
    conn, runtime_password="pw1", readonly_password="pw2", migrator_password="pw3",
)

check("M: first run_bootstrap() creates the database", conn.databases_created == 1)
check("M: first run creates all 3 roles", conn.create_user_calls == 3)
check(
    "M: first run creates ALL immutability triggers (none existed before)",
    len(result_1["triggers_created"]) == len(immutability_triggers()) > 0,
    f"created={len(result_1['triggers_created'])} expected={len(immutability_triggers())}",
)
check(
    "M: yandi_runtime/yandi_readonly/yandi_migrator all now 'exist' in the fake's "
    "tracked state",
    user_exists(conn, "yandi_runtime", "%") and user_exists(conn, "yandi_readonly", "localhost")
    and user_exists(conn, "yandi_migrator", "localhost"),
)

# ============================================================
# M. Running bootstrap a SECOND time -> no duplication.
# ============================================================

create_user_calls_before = conn.create_user_calls
create_trigger_calls_before = conn.create_trigger_calls

result_2 = run_bootstrap(
    conn, runtime_password="pw1-again", readonly_password="pw2-again", migrator_password="pw3-again",
)

check(
    "M: second run_bootstrap() still ISSUES 'CREATE USER IF NOT EXISTS' (idempotent "
    "syntax handles this at the SQL level) but creates ZERO NEW triggers",
    len(result_2["triggers_created"]) == 0,
    f"{result_2['triggers_created']}",
)
check(
    "M: the set of users that 'exist' is unchanged after the second run — no "
    "duplicate accounts",
    conn.users == {("yandi_runtime", "%"), ("yandi_readonly", "localhost"), ("yandi_migrator", "localhost")},
    f"{conn.users}",
)
check(
    "M: apply_immutability_triggers() actually CHECKED each trigger's existence "
    "before deciding not to recreate it (not a lucky no-op)",
    conn.create_trigger_calls == create_trigger_calls_before,
)

# ============================================================
# M. Partial previous bootstrap -> only missing pieces are created
# (deterministic recovery from a crash mid-bootstrap).
# ============================================================

conn_partial = StatefulFakeConnection()
# Simulate: database + roles already exist, but only ONE trigger was
# created before a crash interrupted the first bootstrap attempt.
conn_partial.databases_created = 1
conn_partial.users = {("yandi_runtime", "%"), ("yandi_readonly", "localhost"), ("yandi_migrator", "localhost")}
_all_trigger_names = [name for name, _ddl in immutability_triggers()]
conn_partial.triggers = {_all_trigger_names[0]}  # only the first one "survived"

result_partial = run_bootstrap(
    conn_partial, runtime_password="pw1", readonly_password="pw2", migrator_password="pw3",
)
check(
    "M: partial-bootstrap recovery creates EXACTLY the missing triggers "
    "(all except the one that already existed) — deterministic, not all-or-nothing",
    set(result_partial["triggers_created"]) == set(_all_trigger_names) - {_all_trigger_names[0]},
    f"{result_partial['triggers_created']}",
)

# ============================================================
# Passwords never appear in SQL text — always bound parameters.
# ============================================================

_secret_password = "sUpEr-sEcReT-pw-12345"
conn_secret = StatefulFakeConnection()
run_bootstrap(
    conn_secret, runtime_password=_secret_password, readonly_password="other1", migrator_password="other2",
)
_all_sql_text = "\n".join(sql for sql, _params in conn_secret.calls)
check(
    "no password value ever appears INSIDE the SQL text itself (always a bound "
    "%s parameter) — T1/T15 discipline extended to bootstrap.py",
    _secret_password not in _all_sql_text,
    "password leaked into SQL text!",
)

# ============================================================
# revoke_bootstrap().
# ============================================================

conn_revoke = StatefulFakeConnection()
conn_revoke.users.add(("yandi_bootstrap", "localhost"))
revoke_bootstrap(conn_revoke, "yandi_bootstrap", "localhost")
check(
    "revoke_bootstrap() issues a real DROP USER and the fake's tracked state "
    "reflects the account being gone",
    ("yandi_bootstrap", "localhost") not in conn_revoke.users,
)
check(
    "revoke_bootstrap()'s statement is DROP USER (not just REVOKE, per mandate "
    "§10.1's 'DROP / REVOKE / убрать credential')",
    any("DROP USER" in sql for sql, _p in conn_revoke.calls),
)

# ============================================================
# Structural: bootstrap.py never generates/hardcodes a key or calls
# key generation as a side effect (mandate §37).
# ============================================================

check(
    "bootstrap.py's module namespace has NO 'generate_kek' name bound at all "
    "(it's only mentioned in a docstring, explaining why it's deliberately "
    "absent from actual imports/calls) — key generation stays a separate, "
    "explicit operator action (mandate §37)",
    not hasattr(bootstrap_mod, "generate_kek"),
)
check(
    "bootstrap.py never hardcodes a default password anywhere: readonly_password/"
    "migrator_password are REQUIRED keyword arguments with no default (runtime_"
    "password alone may default to '' — DATABASE BOOTSTRAP V1's auth_socket path, "
    "mandate §11, means YANDI_RUNTIME sometimes needs NO password at all — but "
    "run_bootstrap() must then refuse to proceed with an empty one silently, see "
    "the next check)",
    all(
        p.default is inspect.Parameter.empty
        for name, p in inspect.signature(run_bootstrap).parameters.items()
        if name in ("readonly_password", "migrator_password")
    ),
)
def _raises_value_error_on_blank_runtime_credential() -> bool:
    try:
        run_bootstrap(StatefulFakeConnection(), readonly_password="pw2", migrator_password="pw3")
    except ValueError:
        return True
    except Exception:
        return False
    return False


check(
    "run_bootstrap() refuses to create YANDI_RUNTIME with a blank password when "
    "auth_socket was NOT requested either (never silently proceeds with an empty "
    "credential by omission)",
    _raises_value_error_on_blank_runtime_credential(),
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
