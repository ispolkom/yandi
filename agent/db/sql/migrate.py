"""
agent/db/sql/migrate.py — applies agent/db/sql/schema.py's DDL to a
live MySQL/Percona server. Idempotent (every CREATE TABLE is IF NOT
EXISTS); records the applied schema_version so re-running is a no-op.

Does nothing destructive: no DROP, no TRUNCATE, no DELETE anywhere in
this file.

CLI:
    python3 -m agent.db.sql.migrate --dry-run   # print DDL, connect nothing
    python3 -m agent.db.sql.migrate             # apply (requires
                                                 # YANDI_SQL_USER/PASSWORD)
    python3 -m agent.db.sql.migrate --check     # ping only, report status
"""
from __future__ import annotations

import argparse
import sys

from agent.db.sql.connection import get_connection, ping, is_configured, SqlUnavailable
from agent.db.sql.schema import (
    ALL_TABLES_IN_ORDER,
    ALTER_STATEMENTS_IN_ORDER,
    SCHEMA_VERSION,
)

SCHEMA_VERSION_DESCRIPTION = "v2: + decision_event (append-only decision/reasoning ledger)"


def record_schema_version(
    conn, version: int = SCHEMA_VERSION, description: str = SCHEMA_VERSION_DESCRIPTION,
) -> None:
    """INSERT IGNORE the schema_migrations row for `version` — the single
    place that records "schema V{version} has been fully applied,"
    shared by this module's own apply() AND agent.db.sql.bootstrap.
    apply_schema() (DATABASE BOOTSTRAP V1's own schema-application path,
    used when a bootstrap-capable connection is already open rather than
    one obtained via get_connection()'s env-var credentials).

    `version INT PRIMARY KEY` (schema.py's own SCHEMA_MIGRATIONS DDL)
    makes IGNORE the correct idempotency mechanism here — a second call
    with the SAME version is a silent no-op, never a duplicate row or an
    error; this is what makes re-running either caller's schema
    application safe any number of times.

    Callers are expected to call this ONLY after every required DDL
    statement for `version` has already executed without raising — this
    function does not itself re-verify that any table/column exists, it
    only records that the apply loop leading up to it completed."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO schema_migrations (version, description) VALUES (%s, %s)",
            (version, description),
        )


def dry_run() -> None:
    print(f"-- schema_version target: {SCHEMA_VERSION}")
    print(f"-- {len(ALL_TABLES_IN_ORDER)} CREATE TABLE statements, "
          f"{len(ALTER_STATEMENTS_IN_ORDER)} ALTER statements")
    for name, ddl in ALL_TABLES_IN_ORDER:
        print(f"\n-- ==== {name} ====")
        print(ddl.strip())
    for name, ddl in ALTER_STATEMENTS_IN_ORDER:
        print(f"\n-- ==== {name} ====")
        print(ddl.strip())


def apply() -> bool:
    """
    Returns True on success, False if the SQL layer isn't reachable
    (never raises SqlUnavailable out of this function — the CLI reports
    it and exits non-zero instead).
    """
    if not is_configured():
        print("NOT CONFIGURED: set YANDI_SQL_USER and YANDI_SQL_PASSWORD "
              "in the environment first. No migration attempted.")
        return False

    try:
        with get_connection(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM schema_migrations "
                            "WHERE version = %s", (SCHEMA_VERSION,)) \
                    if _table_exists(cur, "schema_migrations") else None
            for name, ddl in ALL_TABLES_IN_ORDER:
                with conn.cursor() as cur:
                    cur.execute(ddl)
                print(f"OK   CREATE TABLE IF NOT EXISTS {name}")
            for name, ddl in ALTER_STATEMENTS_IN_ORDER:
                with conn.cursor() as cur:
                    try:
                        cur.execute(ddl)
                        print(f"OK   {name}")
                    except Exception as e:
                        # Idempotency for ALTER ADD CONSTRAINT (no native
                        # IF NOT EXISTS in MySQL 8.0 for this form) — a
                        # duplicate-constraint error on re-run is expected
                        # and not a failure.
                        if "Duplicate" in str(e) or "already exists" in str(e).lower():
                            print(f"SKIP {name} (already applied)")
                        else:
                            raise
            record_schema_version(conn)
        return True
    except SqlUnavailable as e:
        print(f"SQL UNAVAILABLE: {e}")
        return False


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) AS c FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (name,),
    )
    row = cur.fetchone()
    return bool(row and row.get("c"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        dry_run()
        sys.exit(0)

    if args.check:
        print("configured:", is_configured())
        print("ping:", ping())
        sys.exit(0)

    ok = apply()
    sys.exit(0 if ok else 1)
