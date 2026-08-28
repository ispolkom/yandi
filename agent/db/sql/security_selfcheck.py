"""
agent/db/sql/security_selfcheck.py — Этап 5E-S S8: startup/CI security
self-check (mandate §39, §44 section N).

Extends what agent/db/sql/migrate.py's --check flow already does
(ping + schema_version) with security-specific assertions: required
tables exist, required immutability triggers exist, and — the LIVE
counterpart of agent/db_sql_security_privilege_regression_test.py's
STATIC proof — that the CURRENTLY CONNECTED account's actual `SHOW
GRANTS FOR CURRENT_USER()` output contains nothing outside its
declared allow-list.

DESIGNED, LIVE VERIFICATION BLOCKED this pass — no credentials exist
in this environment to connect with. Unit-tested against a SCRIPTED
fake connection that returns realistic SHOW GRANTS text.

Per mandate §39: "При mismatch: не делать silent repair от runtime
account." This module only REPORTS — it never attempts to fix a
mismatch itself (no CREATE TABLE/CREATE TRIGGER/GRANT calls anywhere
in this file). What a caller does with a failing result (refuse
startup, page an operator, just log) is a deployment decision outside
this module's scope.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from agent.db.sql.schema import ALL_TABLES_IN_ORDER, SCHEMA_VERSION
from agent.db.sql.security_grants import FORBIDDEN_FOR_READONLY, FORBIDDEN_FOR_RUNTIME
from agent.db.sql.security_triggers import immutability_triggers


def check_schema_version(conn, expected_version: int = SCHEMA_VERSION) -> Tuple[bool, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(version) AS v FROM schema_migrations")
        row = cur.fetchone()
    actual = row.get("v") if row else None
    if actual != expected_version:
        return (False, f"schema_migrations reports version {actual!r}, expected {expected_version}")
    return (True, "")


def check_required_tables(conn) -> Tuple[bool, List[str]]:
    missing: List[str] = []
    for name, _ddl in ALL_TABLES_IN_ORDER:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = %s",
                (name,),
            )
            row = cur.fetchone()
        if not (row and row.get("c")):
            missing.append(name)
    return (len(missing) == 0, missing)


def check_required_triggers(conn) -> Tuple[bool, List[str]]:
    missing: List[str] = []
    for trigger_name, _ddl in immutability_triggers():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM information_schema.triggers "
                "WHERE trigger_schema = DATABASE() AND trigger_name = %s",
                (trigger_name,),
            )
            row = cur.fetchone()
        if not (row and row.get("c")):
            missing.append(trigger_name)
    return (len(missing) == 0, missing)


def parse_show_grants(rows: List[str]) -> List[str]:
    """`rows`: raw `SHOW GRANTS FOR CURRENT_USER()` result lines (each a
    string like "GRANT SELECT, INSERT ON `yandi_epistemic`.* TO ..."').
    Returns the flat list of privilege tokens actually granted, upper-
    cased. Best-effort text parse — MySQL doesn't expose grants in a
    more structured form via this statement."""
    privileges: List[str] = []
    for row in rows:
        if not row.upper().startswith("GRANT "):
            continue
        on_idx = row.upper().find(" ON ")
        if on_idx == -1:
            continue
        priv_text = row[len("GRANT "):on_idx]
        privileges.extend(p.strip().upper() for p in priv_text.split(","))
    return privileges


def check_current_grants_against_allowlist(conn, forbidden: Tuple[str, ...]) -> Tuple[bool, List[str]]:
    """Runs SHOW GRANTS FOR CURRENT_USER() and flags any forbidden
    privilege actually present on the account currently connected —
    the LIVE counterpart of the static GRANT-text checks in
    agent/db_sql_security_privilege_regression_test.py."""
    with conn.cursor() as cur:
        cur.execute("SHOW GRANTS FOR CURRENT_USER()")
        rows = [list(r.values())[0] for r in cur.fetchall()]
    granted = parse_show_grants(rows)
    violations = [p for p in granted if p in forbidden or p == "ALL PRIVILEGES"]
    return (len(violations) == 0, violations)


def run_selfcheck(conn, *, role: str = "runtime") -> Dict[str, Any]:
    """`role`: 'runtime' or 'readonly' — selects which forbidden-
    privilege list to check the CURRENTLY CONNECTED account's grants
    against. Returns a summary dict; raises nothing itself (a caller
    decides what "not ok" means for its own context — refuse startup,
    enter a degraded mode, just log — mandate §28's SECURITY_INTEGRITY_
    BROKEN state is a caller-level decision, not made inside this
    function)."""
    if role not in ("runtime", "readonly"):
        raise ValueError(f"role must be 'runtime' or 'readonly', got {role!r}")
    forbidden = FORBIDDEN_FOR_READONLY if role == "readonly" else FORBIDDEN_FOR_RUNTIME

    version_ok, version_detail = check_schema_version(conn)
    tables_ok, missing_tables = check_required_tables(conn)
    triggers_ok, missing_triggers = check_required_triggers(conn)
    grants_ok, grant_violations = check_current_grants_against_allowlist(conn, forbidden)

    return {
        "ok": version_ok and tables_ok and triggers_ok and grants_ok,
        "schema_version_ok": version_ok, "schema_version_detail": version_detail,
        "tables_ok": tables_ok, "missing_tables": missing_tables,
        "triggers_ok": triggers_ok, "missing_triggers": missing_triggers,
        "grants_ok": grants_ok, "grant_violations": grant_violations,
    }
