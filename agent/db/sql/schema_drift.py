"""
agent/db/sql/schema_drift.py — READ ONLY: does the live database really have every column agent/db/sql/schema.py's `CREATE TABLE`
statements declare?

Why this exists: `CREATE TABLE IF NOT EXISTS` never adds a column to a table that already exists (MySQL). Several tables in this
schema have had columns added straight into their `CREATE TABLE` text after the table was already live (`triggering_claim_ids` on
`semantic_edge`, `evidence_for`/`evidence_against`/`claim_ids` on `belief`, …) — on a database whose table predates that change, the
column is silently missing until code that reads or writes it throws `(1054, "Unknown column ...")`, sometimes deep inside a request
that has already done a lot of otherwise-real work.

This script finds every such gap, for every table, by parsing the SAME `CREATE TABLE` text `migrate.py` runs (so it can never drift
from what the schema module actually declares) and comparing it to `INFORMATION_SCHEMA.COLUMNS` on the connected database. It writes
nothing and calls nothing except SELECT.

    python -m agent.db.sql.schema_drift            # human-readable
    python -m agent.db.sql.schema_drift --json      # machine-readable, for a script to act on
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Dict, List

_COLUMN_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z][A-Za-z0-9_]*)")
_RESERVED = {"KEY", "PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "INDEX", "REFERENCES"}


def declared_columns() -> Dict[str, List[str]]:
    """{table: [column, ...]} exactly as schema.py's own CREATE TABLE statements declare them, in the order they appear.
    Only a line that STARTS a new definition counts as a column: a continuation line of a multi-line CONSTRAINT/FOREIGN
    KEY (the previous logical statement had not yet been closed by a comma) is never mistaken for one."""
    from agent.db.sql.schema import ALL_TABLES_IN_ORDER
    out: Dict[str, List[str]] = {}
    for name, ddl in ALL_TABLES_IN_ORDER:
        body = ddl[ddl.index("(") + 1: ddl.rindex(")")]
        columns = []
        continuation = False
        for line in body.split("\n"):
            code = line.split("--", 1)[0].strip()          # a trailing SQL comment must not hide the comma that ends the statement
            if not code:
                continue
            if not continuation:
                m = _COLUMN_LINE_RE.match(line)
                if m and m.group(1).upper() not in _RESERVED:
                    columns.append(m.group(1))
            continuation = not code.endswith(",")
        out[name] = columns
    return out


def live_columns(conn) -> Dict[str, List[str]]:
    """{table: [column, ...]} as INFORMATION_SCHEMA.COLUMNS reports them for the connected database."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() ORDER BY table_name, ordinal_position"
        )
        rows = cur.fetchall()
    out: Dict[str, List[str]] = {}
    for row in rows:
        table = row.get("table_name") or row.get("TABLE_NAME")
        column = row.get("column_name") or row.get("COLUMN_NAME")
        out.setdefault(table, []).append(column)
    return out


def find_drift(conn) -> Dict[str, List[str]]:
    """{table: [missing column, ...]} — only tables that exist live AND are missing at least one declared column.
    A table that does not exist live at all is not drift here (migrate.py's CREATE TABLE IF NOT EXISTS handles that case)."""
    declared = declared_columns()
    live = live_columns(conn)
    drift: Dict[str, List[str]] = {}
    for table, columns in declared.items():
        if table not in live:
            continue
        missing = [c for c in columns if c not in live[table]]
        if missing:
            drift[table] = missing
    return drift


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    from agent.db.sql.connection import SqlUnavailable, get_connection
    try:
        with get_connection(autocommit=True) as conn:
            drift = find_drift(conn)
    except SqlUnavailable as exc:
        print(f"SQL UNAVAILABLE: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(drift, ensure_ascii=False, indent=2))
    elif not drift:
        print("no drift: every table that exists has every column agent/db/sql/schema.py declares for it")
    else:
        print(f"{sum(len(v) for v in drift.values())} missing column(s) in {len(drift)} table(s):")
        for table, columns in drift.items():
            print(f"  {table}: {', '.join(columns)}")
        print("\nEach of these needs an ALTER TABLE ... ADD COLUMN in agent/db/sql/schema.py's ALTER_STATEMENTS_IN_ORDER, "
              "applied with `python -m agent.db.sql.migrate`.")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
