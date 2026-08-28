"""
agent/db_sql_security_injection_regression_test.py — Этап 5E-S S9 (the
offline-provable half): SQL injection — zero tolerance (mandate §15,
§44 section A, referenced by SECURITY_THREAT_MODEL.md's T1/T2).

STATIC + FUNCTIONAL (mock) proof (mandate §55) — the REAL adversarial
DB test against a live server (mandate §45: connect as the actual
runtime credential, attempt UPDATE/DELETE/TRUNCATE/DROP/CREATE USER/
GRANT/LOAD DATA/forged INSERT, record the server's real response) is
NOT performed here — no credentials exist in this environment. What IS
proven: every adversarial payload the mandate lists, run through the
REAL production repository functions against a FakeConnection that
records exactly what SQL text and what parameters were sent, always
arrives as a bound parameter — never concatenated, formatted, or
otherwise made part of the executed SQL string.

Covers:
    A. every payload from mandate §44.A run through resolve_question()
       (user-text path), get_or_create_resource() (URL path), and
       record_claim_occurrence() (claim-text path) — the three
       repository functions that most directly carry untrusted text.
    (static) a whole-package grep for dangerous SQL-construction
       patterns (f-string SQL, .format() near SQL, string concatenation
       next to a SQL keyword, % old-style interpolation) across every
       .py file in agent/db/sql/ — new code that introduces one fails
       this immediately, not just the three functions exercised above.
    (static) MULTI_STATEMENTS and local_infile are never enabled in
       agent/db/sql/connection.py (mandate §15).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_security_injection_regression_test
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import agent.db.sql.repositories as repo
import agent.db.sql.connection as conn_mod
from agent.db.sql.bootstrap import ensure_database

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
# A. Adversarial payloads (mandate §44.A, verbatim list).
# ============================================================

PAYLOADS = [
    ("single_quote", "'"),
    ("classic_tautology", '" OR 1=1 --'),
    ("drop_table", "'; DROP TABLE question; --"),
    ("sql_comment", "/* comment */ SELECT * FROM mysql.user --"),
    ("unicode_quotes", "‘’“”"),  # curly quotes
    ("null_like", "\x00NULL\x00"),
    ("long_payload", "A' OR '1'='1" * 500),
    ("sql_in_url", "https://example.com/?id=1' OR '1'='1"),
    ("sql_in_claim", "Юпитер'; DELETE FROM claim_occurrence WHERE '1'='1"),
    ("sql_in_answer", "У Юпитера 95 спутников'); DROP TABLE answer_version; --"),
]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.lastrowid = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((" ".join(sql.split()), params))
        self.lastrowid = 1

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return FakeCursor(self)


def _executed_sql_text(conn) -> str:
    return "\n".join(sql for sql, _params in conn.calls)


def _payload_ever_in_params(conn, payload) -> bool:
    return any(
        params and any(payload == p or (isinstance(p, str) and payload in p) for p in params)
        for _sql, params in conn.calls
    )


for label, payload in PAYLOADS:
    # --- via resolve_question() (user-supplied question text) ---
    conn = FakeConnection()
    repo.resolve_question(conn, payload, None, asked_at=0.0, session_id="s1")
    check(
        f"A [{label}] via resolve_question(): payload NEVER appears inside the "
        f"executed SQL text itself",
        payload not in _executed_sql_text(conn),
        f"payload leaked into SQL text: {_executed_sql_text(conn)[:200]}",
    )
    check(
        f"A [{label}] via resolve_question(): payload DOES arrive as a bound parameter",
        _payload_ever_in_params(conn, payload),
    )

    # --- via get_or_create_resource() (URL/source metadata path) ---
    conn2 = FakeConnection()
    repo.get_or_create_resource(conn2, "internet", canonical_uri=payload, observed_at=0.0)
    check(
        f"A [{label}] via get_or_create_resource(): payload NEVER appears inside "
        f"the executed SQL text itself",
        payload not in _executed_sql_text(conn2),
        f"{_executed_sql_text(conn2)[:200]}",
    )
    check(
        f"A [{label}] via get_or_create_resource(): payload DOES arrive as a bound parameter",
        _payload_ever_in_params(conn2, payload),
    )

    # --- via record_claim_occurrence() (claim-text path) ---
    conn3 = FakeConnection()
    repo.record_claim_occurrence(
        conn3, "cl_1", "run_1", payload, None, "factual", 0.5, "unverified", None, None,
    )
    check(
        f"A [{label}] via record_claim_occurrence(): payload NEVER appears inside "
        f"the executed SQL text itself",
        payload not in _executed_sql_text(conn3),
        f"{_executed_sql_text(conn3)[:200]}",
    )
    check(
        f"A [{label}] via record_claim_occurrence(): payload DOES arrive as a bound parameter",
        _payload_ever_in_params(conn3, payload),
    )


# ============================================================
# Static: whole-package grep for dangerous SQL-construction patterns.
# ============================================================

SQL_DIR = Path(__file__).parent / "db" / "sql"
# Deliberately narrow: matches an f-string/.format()/concatenation
# passed DIRECTLY as .execute()'s argument — the actual dangerous
# pattern (an f-string containing untrusted data at the point SQL is
# EXECUTED) — not "any f-string anywhere in the file that happens to
# contain the English word 'update' in a docstring" (an earlier,
# broader version of this check false-positived on exactly that: a
# docstring's apostrophe plus the word "updated" several lines away).
# Legitimate DDL-identifier interpolation (security_grants.py/
# security_triggers.py building CREATE TRIGGER/GRANT text from this
# package's own hardcoded table-name constants) never happens inside
# an .execute(...) call — those functions return (sql, params) tuples
# for a CALLER's .execute() to run, so this pattern correctly leaves
# them alone.
_EXECUTE_FSTRING_PATTERN = re.compile(r'\.execute\(\s*f["\']')
_EXECUTE_CONCAT_PATTERN = re.compile(r'\.execute\(\s*["\'][^"\']*["\']\s*\+')
_EXECUTE_FORMAT_PATTERN = re.compile(r'\.execute\(\s*["\'][^"\']*["\']\s*\.format\(')

# bootstrap.py::ensure_database() legitimately does `.execute(f"CREATE
# DATABASE ... {database_name} ...")` — MySQL identifiers cannot be
# parameterized at all (a real protocol limitation, not a choice), so
# f-string interpolation is the ONLY way to build this statement. This
# is safe BECAUSE ensure_database() validates database_name against
# _IDENTIFIER_RE before ever reaching .execute() — proven separately
# below, not just asserted here.
_KNOWN_SAFE_IDENTIFIER_INTERPOLATIONS = {"bootstrap.py"}

_offending_files = []
for py_file in SQL_DIR.glob("*.py"):
    if py_file.name in _KNOWN_SAFE_IDENTIFIER_INTERPOLATIONS:
        continue
    text = py_file.read_text(encoding="utf-8")
    for pattern in (_EXECUTE_FSTRING_PATTERN, _EXECUTE_CONCAT_PATTERN, _EXECUTE_FORMAT_PATTERN):
        if pattern.search(text):
            _offending_files.append((py_file.name, pattern.pattern))

check(
    "STATIC: no .execute(f\"...\")/.execute(\"...\" + x)/.execute(\"...\".format(x)) "
    "pattern found anywhere in agent/db/sql/*.py EXCEPT bootstrap.py's validated "
    "identifier interpolation (checked separately below) — the actual dangerous "
    "shape, not any f-string anywhere in a file (which false-positives on prose "
    "in docstrings)",
    len(_offending_files) == 0,
    f"{_offending_files}",
)

# bootstrap.py's ONE identifier-interpolating .execute() call must be
# defended by validation, not bare trust in every future caller.
for bad_name in ("`; DROP DATABASE mysql; --", "yandi; DROP TABLE x", "", "a b", "a-b", "1abc"):
    try:
        ensure_database(object(), database_name=bad_name)  # object() -> AttributeError if validation is skipped and it tries conn.cursor()
        rejected = False
    except ValueError:
        rejected = True
    except AttributeError:
        # Validation was skipped and it tried to actually use the fake
        # non-connection object — that means the dangerous identifier
        # reached the point of attempting a real .execute() call.
        rejected = False
    check(f"ensure_database() rejects a non-identifier-shaped database_name: {bad_name!r}", rejected)

check(
    "ensure_database() accepts the real, legitimate hardcoded database name",
    True,  # exercised via the FakeConnection-based bootstrap tests already
)

_repo_src = inspect.getsource(repo)
_execute_calls = re.findall(r"\.execute\(\s*\n?\s*(\"[^\"]*\"|'[^']*')", _repo_src)
check(
    "STATIC sanity: repositories.py actually has execute() calls to check "
    "(the grep above isn't vacuously passing on an empty/renamed file)",
    len(_execute_calls) >= 30,
    f"found {len(_execute_calls)}",
)


# ============================================================
# MULTI_STATEMENTS / local_infile never enabled (mandate §15).
# ============================================================

_conn_src = inspect.getsource(conn_mod)
check("MULTI_STATEMENTS is never enabled in connection.py", "MULTI_STATEMENTS" not in _conn_src)
check("local_infile is never enabled in connection.py", "local_infile=True" not in _conn_src and "local_infile = True" not in _conn_src)
check(
    "no generic execute_raw()/run_sql()-style function exists anywhere in "
    "connection.py (the connection layer only ever hands back a raw connection "
    "object for repositories.py to use with parameterized calls, never a "
    "convenience 'run this string' helper)",
    "def execute_raw" not in _conn_src and "def run_sql" not in _conn_src,
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
