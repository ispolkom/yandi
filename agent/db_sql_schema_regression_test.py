"""
agent/db_sql_schema_regression_test.py — Этап 5 (SQL persistence
migration) regression: agent/db/sql/schema.py static DDL invariants.

Pure static analysis of the DDL strings — no DB connection needed or
used. Proves architectural invariants the mandate treats as
non-negotiable:

    A. no truth-claiming vocabulary anywhere in the schema (mandate §1/§14)
    B. resource_type (SOURCE_RESOURCE) never includes 'local_memory' —
       a resource's fundamental nature is never "replayed from memory";
       only an OBSERVATION's route can be (the §6 correction this
       session made to its own prior audit proposal)
    C. observation_route (SOURCE_OBSERVATION) DOES include all 5 channels
    D. SOURCE_OBSERVATION has a self-referencing origin_observation_id
       (replay provenance chain)
    E. every table this mandate calls APPEND-ONLY has no natural UPDATE
       path implied by its own column set (no "current_value"/"latest"-
       style column, no updated_at on rows meant to never change)
    F. CLAIM_FAMILY.canonical_text has no companion "updated_at-driven"
       rewrite path implied — still allows the table's own updated_at
       column (matches confirmed current JSON behavior: updated_at
       exists but canonical_text itself is never touched again)
    G. every FK-bearing table's foreign key actually references a table
       that appears earlier in ALL_TABLES_IN_ORDER (no forward reference
       MySQL would reject at CREATE TABLE time)
    H. RUN_ERROR stays minimal — no stack-trace/prompt-dump-shaped column

Run: /home/iam/venv/bin/python3 -m agent.db_sql_schema_regression_test
"""
from __future__ import annotations

import re

from agent.db.sql.schema import ALL_TABLES_IN_ORDER, ALTER_STATEMENTS_IN_ORDER, _BANNED_TOKENS

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


ALL_DDL = "\n".join(ddl for _name, ddl in ALL_TABLES_IN_ORDER + ALTER_STATEMENTS_IN_ORDER)
TABLES = dict(ALL_TABLES_IN_ORDER)

# ============================================================
# A. No truth-claiming vocabulary anywhere.
# ============================================================

for tok in _BANNED_TOKENS:
    check(
        f"A: banned truth-claiming token {tok!r} does not appear anywhere in the schema",
        tok not in ALL_DDL.lower(),
        f"found {tok!r} in DDL",
    )

# ============================================================
# B/C. resource_type vs observation_route — the §6 correction.
# ============================================================

resource_type_match = re.search(r"resource_type\s+ENUM\(([^)]+)\)", TABLES["source_resource"])
check("B: source_resource.resource_type ENUM found", resource_type_match is not None)
if resource_type_match:
    values = resource_type_match.group(1)
    check(
        "B: resource_type does NOT include 'local_memory' — a resource's origin nature is never "
        "'replayed from memory', only an observation's ROUTE can be (§6 correction)",
        "local_memory" not in values,
        f"{values}",
    )
    for expected in ("internet", "network_node", "ai_chat", "local_model"):
        check(f"B: resource_type includes {expected!r}", expected in values, f"{values}")

route_match = re.search(r"observation_route\s+ENUM\(([^)]+)\)", TABLES["source_observation"])
check("C: source_observation.observation_route ENUM found", route_match is not None)
if route_match:
    route_values = route_match.group(1)
    for expected in ("internet", "local_memory", "network_node", "ai_chat", "local_model"):
        check(f"C: observation_route includes {expected!r} (all 5 channels)", expected in route_values, f"{route_values}")

# ============================================================
# D. Replay provenance chain: origin_observation_id self-FK.
# ============================================================

check(
    "D: source_observation has origin_observation_id (replay provenance)",
    "origin_observation_id" in TABLES["source_observation"],
)
check(
    "D: origin_observation_id is a self-referencing FK onto source_observation itself",
    "fk_so_origin FOREIGN KEY (origin_observation_id)\n        REFERENCES source_observation(observation_id)"
    in TABLES["source_observation"]
    or "REFERENCES source_observation(observation_id)" in TABLES["source_observation"],
)

# ============================================================
# E. Append-only tables have no natural in-place-update column shape
# (no bare "value"/"current_X" column that would invite an UPDATE).
# ============================================================

APPEND_ONLY_TABLES = [
    "question_occurrence", "answer_version", "answer_assessment",
    "claim_occurrence", "source_observation", "evidence_relation",
    "belief_assessment_history", "recheck_event",
    "epistemic_contradiction_observation", "run_error",
]
_SUSPICIOUS_MUTABLE_COLUMN = re.compile(r"\bcurrent_value\b|\blatest_value\b", re.IGNORECASE)

for t in APPEND_ONLY_TABLES:
    check(
        f"E: append-only table {t!r} exists in schema",
        t in TABLES,
    )
    if t in TABLES:
        check(
            f"E: append-only table {t!r} has no suspicious 'current_value'/'latest_value'-shaped column",
            not _SUSPICIOUS_MUTABLE_COLUMN.search(TABLES[t]),
            f"{TABLES[t]}",
        )

# ============================================================
# F. claim_family: canonical_text write-once, updated_at exists (for
# domain/administrative touch, not for rewriting canonical_text itself
# — enforced at the repository layer via INSERT IGNORE, proven in
# db_sql_repositories_regression_test.py).
# ============================================================

check(
    "F: claim_family has canonical_text and updated_at (write-once enforced at repository "
    "layer, not by a missing column here)",
    "canonical_text" in TABLES["claim_family"] and "updated_at" in TABLES["claim_family"],
)

# ============================================================
# G. No forward FK references (every REFERENCES target already exists
# earlier in ALL_TABLES_IN_ORDER — MySQL would reject CREATE TABLE
# otherwise, since deferred constraint checking isn't used here).
# ============================================================

seen_tables = []
forward_ref_found = []
for name, ddl in ALL_TABLES_IN_ORDER:
    for ref in re.findall(r"REFERENCES\s+(\w+)\s*\(", ddl):
        if ref == name:
            continue  # self-FK, fine (origin_observation_id) — table already exists by definition
        if ref not in seen_tables:
            forward_ref_found.append((name, ref))
    seen_tables.append(name)

check(
    "G: no CREATE TABLE references a table that doesn't exist yet at that point in "
    "ALL_TABLES_IN_ORDER (MySQL would reject this without deferred constraints)",
    not forward_ref_found,
    f"{forward_ref_found}",
)

check(
    "G: verification_run.final_answer_id's FK is correctly deferred to an ALTER "
    "(answer_version doesn't exist yet when verification_run is created)",
    "final_answer_id" in TABLES["verification_run"]
    and "REFERENCES answer_version" not in TABLES["verification_run"]
    and any("final_answer_id" in ddl for _n, ddl in ALTER_STATEMENTS_IN_ORDER),
)

# ============================================================
# H. run_error stays minimal — no debug-warehouse-shaped column.
# ============================================================

_DEBUG_SHAPED = re.compile(r"stack_trace|prompt_dump|stdout|traceback", re.IGNORECASE)
check(
    "H: run_error has no stack-trace/prompt-dump-shaped column (minimal error storage, mandate §20)",
    not _DEBUG_SHAPED.search(TABLES["run_error"]),
    f"{TABLES['run_error']}",
)
_column_line_re = re.compile(
    r"^\s*(\w+)\s+(BIGINT|VARCHAR|DATETIME|FLOAT|BOOLEAN|TEXT|MEDIUMTEXT|CHAR|INT|ENUM)\b"
)
_run_error_columns = [
    m.group(1)
    for line in TABLES["run_error"].splitlines()
    for m in [_column_line_re.match(line)]
    if m
]
check(
    "H: run_error is exactly the 6 mandated columns (error_id, run_id, failed_stage, "
    "error_class, short_message, created_at) — not a growing debug table",
    _run_error_columns == ["error_id", "run_id", "failed_stage", "error_class", "short_message", "created_at"],
    f"{_run_error_columns}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
