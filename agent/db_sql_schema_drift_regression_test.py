"""
agent/db_sql_schema_drift_regression_test.py — agent/db/sql/schema_drift.py: does the DDL parser read schema.py the way it is
actually written, and does it never mistake a multi-line CONSTRAINT/FOREIGN KEY for a column?

    THE PARSER SEES EVERY TABLE'S REAL COLUMNS.   A FOREIGN KEY'S "REFERENCES other_table(...)" CONTINUATION LINE IS NEVER A COLUMN.
    A TRAILING "-- comment" AFTER THE COMMA NEVER HIDES THE COMMA.

Purely structural (no database): parses the real agent/db/sql/schema.py. The real-engine proof (does it actually find and fix drift
on a live-shaped database) is agent/db_sql_schema_drift_sql_integration_test.py.

Run: python -m agent.db_sql_schema_drift_regression_test
"""
from __future__ import annotations

import sys

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    from agent.db.sql.schema import ALL_TABLES_IN_ORDER, TABLE_CLASSIFICATION
    from agent.db.sql.schema_drift import declared_columns

    declared = declared_columns()

    check("1: every table in schema.py is parsed, no fewer, no extra", set(declared) == set(TABLE_CLASSIFICATION))
    check("2: none of the two known drift cases are silently missing from what the parser sees",
          "triggering_claim_ids" in declared["semantic_edge"] and {"evidence_for", "evidence_against", "claim_ids"} <= set(declared["belief"]))

    # A multi-line FOREIGN KEY ... REFERENCES continuation is never read as a column, on every table that has one.
    reserved = {"KEY", "PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "INDEX", "REFERENCES"}
    bad = {table: cols for table, cols in declared.items() if any(c.upper() in reserved for c in cols)}
    check("3: no table's column list contains a SQL keyword (a FK continuation line leaking through)", not bad, repr(bad)[:200])

    # Every declared column is a real identifier (word characters), never a fragment of a type or a stray token.
    import re
    ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    bad2 = {t: [c for c in cols if not ident.match(c)] for t, cols in declared.items() if any(not ident.match(c) for c in cols)}
    check("4: every declared column name is a plain identifier", not bad2, repr(bad2)[:200])

    # No duplicate columns within one table (would mean the parser double-counted a continuation).
    dup = {t: cols for t, cols in declared.items() if len(cols) != len(set(cols))}
    check("5: no table has a duplicate column (a sign the parser split one statement into two)", not dup, repr(dup)[:200])

    # Spot checks against the DDL text itself, independent of the parser's own logic (read the literal CREATE TABLE strings).
    ddl_by_name = dict(ALL_TABLES_IN_ORDER)
    check("6: interaction_turn's ten real columns, in order, nothing from its two multi-line-less FK-free constraints lost",
          declared["interaction_turn"] == ["interaction_id", "user_id", "source_turn_id", "turn_id_origin", "user_text",
                                           "assistant_text", "model", "adapter", "recalled_turn_ids", "created_at"])
    check("7: personal_fact_event's columns, with its two multi-line FOREIGN KEY ... REFERENCES constraints correctly skipped",
          declared["personal_fact_event"] == ["event_id", "fact_id", "user_id", "event_type", "by_fact_id", "evidence",
                                              "span_start", "span_end", "source_turn_id", "created_at"])
    check("8: semantic_edge's columns, with its two multi-line FOREIGN KEY constraints skipped (this is the table that actually broke live)",
          declared["semantic_edge"] == ["edge_id", "family_a", "family_b", "edge_type", "reason", "observation_count",
                                        "triggering_claim_ids", "created_at", "last_seen_at"])
    check("9: a column followed by a trailing '-- comment' on the SAME line after its comma is still read, and the NEXT line is not treated as its continuation",
          "edge_id" in declared["semantic_edge"] and "created_at" in declared["semantic_edge"] and declared["semantic_edge"].index("edge_id") == 0)

    # Cross-check: the parser's count for every table's CREATE TABLE must equal manually counting top-level lines that are not KEY/CONSTRAINT/closing.
    for table, ddl in ALL_TABLES_IN_ORDER:
        body = ddl[ddl.index("(") + 1: ddl.rindex(")")]
        manual = 0
        cont = False
        for line in body.split("\n"):
            code = line.split("--", 1)[0].strip()
            if not code:
                continue
            first_word = code.split(None, 1)[0].upper()
            if not cont and first_word not in ("KEY", "PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN"):
                manual += 1
            cont = not code.endswith(",")
        check(f"10: {table}: the parser's count matches an independent manual count ({len(declared[table])} vs {manual})",
              len(declared[table]) == manual)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
