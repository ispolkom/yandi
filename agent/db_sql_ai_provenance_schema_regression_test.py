"""
agent/db_sql_ai_provenance_schema_regression_test.py — SQL persistence
migration regression: AI SELF-REPORTED PROVENANCE schema + minimal
reported-only repository/shadow write path.

INSPECT CONCLUSION this suite encodes: the EXISTING SOURCE_RESOURCE/
SOURCE_OBSERVATION model does NOT support this without breaking its own
semantics — SOURCE_RESOURCE's only identity/dedup key (uri_hash) is
NULL for any non-internet resource_type (a provider+model has no URI),
and SOURCE_OBSERVATION is one-observation-of-one-resource, while a
single AI answer can report MANY sources at once. Reusing the existing
tables would require either an undeduplicated resource per AI call or
fabricating N "resources" for strings the model merely claimed — the
exact AI_REPORTED_SOURCE == SOURCE_RESOURCE conflation the concept
forbids. Hence: two small, new, additive tables, not an extension.

Covers:
    A. both tables registered in ALL_TABLES_IN_ORDER in valid FK order
       (ai_observation after verification_run; ai_reported_source after
       ai_observation).
    B. no banned truth-claiming vocabulary in either table.
    C. CRITICAL INVARIANT: ai_reported_source carries NO foreign key to
       source_resource — self-reported provenance is never wired as if
       it were proven identity.
    D. provenance_mode_reported / live_search_used_reported ENUMs match
       the future standard prompt's own wire format exactly
       (MODEL_KNOWLEDGE|LIVE_SOURCES|MIXED|UNKNOWN,  YES|NO|UNKNOWN).
    E. ai_observation.run_id is nullable (an AI observation need not
       belong to any single YANDI verification run).
    F. the required architectural comment is present verbatim: "SELF-
       REPORTED PROVENANCE IS AN OBSERVATION, NOT VERIFIED PROVENANCE."
    G. minimal implementation exists: repository/shadow functions write
       ai_observation/ai_reported_source only, never source_resource or
       evidence_relation for reported sources.
    H. both tables are structurally append-only — no "current"/"latest"/
       mutable-state-shaped column on either.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_ai_provenance_schema_regression_test
"""
from __future__ import annotations

import inspect

from agent.db.sql.schema import ALL_TABLES_IN_ORDER, _BANNED_TOKENS
import agent.db.sql.schema as schema_mod
import agent.db.sql.repositories as repo_mod
import agent.db.sql.shadow_write as sw_mod

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


TABLE_NAMES = [n for n, _ in ALL_TABLES_IN_ORDER]
TABLES = dict(ALL_TABLES_IN_ORDER)

# ============================================================
# A. Registration + FK ordering.
# ============================================================

check("A: ai_observation is registered in ALL_TABLES_IN_ORDER", "ai_observation" in TABLE_NAMES)
check("A: ai_reported_source is registered in ALL_TABLES_IN_ORDER", "ai_reported_source" in TABLE_NAMES)

if "ai_observation" in TABLE_NAMES and "ai_reported_source" in TABLE_NAMES:
    idx_vr = TABLE_NAMES.index("verification_run")
    idx_aiobs = TABLE_NAMES.index("ai_observation")
    idx_airs = TABLE_NAMES.index("ai_reported_source")
    check(
        "A: ai_observation appears AFTER verification_run (its FK target must already exist)",
        idx_vr < idx_aiobs, f"vr={idx_vr} ai_observation={idx_aiobs}",
    )
    check(
        "A: ai_reported_source appears AFTER ai_observation (its FK target must already exist)",
        idx_aiobs < idx_airs, f"ai_observation={idx_aiobs} ai_reported_source={idx_airs}",
    )

# ============================================================
# B. No banned truth-claiming vocabulary.
# ============================================================

for tname in ("ai_observation", "ai_reported_source"):
    ddl = TABLES.get(tname, "")
    for tok in _BANNED_TOKENS:
        check(f"B: {tok!r} does not appear in {tname}'s DDL", tok not in ddl)

# ============================================================
# C. CRITICAL: no FK from ai_reported_source to source_resource.
# ============================================================

_airs_ddl = TABLES.get("ai_reported_source", "")
check(
    "C CRITICAL INVARIANT: ai_reported_source has NO foreign key to source_resource "
    "(self-reported provenance must never be wired as proven identity) — the table may "
    "still MENTION source_resource in an explanatory comment, just never REFERENCE it",
    "REFERENCES source_resource" not in _airs_ddl,
    f"{_airs_ddl}",
)
check(
    "C: ai_reported_source DOES have a foreign key to ai_observation (its actual parent)",
    "REFERENCES ai_observation(ai_observation_id)" in _airs_ddl,
    f"{_airs_ddl}",
)

# ============================================================
# D. ENUM values match the future prompt's own wire format exactly.
# ============================================================

_aiobs_ddl = TABLES.get("ai_observation", "")
check(
    "D: provenance_mode_reported ENUM matches the future standard prompt's wire format "
    "(MODEL_KNOWLEDGE|LIVE_SOURCES|MIXED|UNKNOWN) exactly",
    "ENUM('MODEL_KNOWLEDGE','LIVE_SOURCES','MIXED','UNKNOWN')" in _aiobs_ddl,
    f"{_aiobs_ddl}",
)
check(
    "D: live_search_used_reported ENUM matches the future standard prompt's wire format "
    "(YES|NO|UNKNOWN) exactly",
    "ENUM('YES','NO','UNKNOWN')" in _aiobs_ddl,
    f"{_aiobs_ddl}",
)

# ============================================================
# E. run_id is nullable.
# ============================================================

check(
    "E: ai_observation.run_id is nullable (an AI observation need not belong to any "
    "single YANDI verification run — e.g. a council-chat exchange)",
    "run_id                        VARCHAR(40) NULL" in _aiobs_ddl,
    f"{_aiobs_ddl}",
)

# ============================================================
# F. Required architectural comment, verbatim.
# ============================================================

_schema_src = inspect.getsource(schema_mod)
check(
    "F: the required architectural comment appears verbatim: 'SELF-REPORTED PROVENANCE "
    "IS AN OBSERVATION, NOT VERIFIED PROVENANCE.'",
    "SELF-REPORTED PROVENANCE IS AN OBSERVATION, NOT VERIFIED PROVENANCE." in _schema_src,
)

# ============================================================
# G. Minimal implementation exists, still reported-only.
# ============================================================

_repo_src = inspect.getsource(repo_mod)
_sw_src = inspect.getsource(sw_mod)
check(
    "G: repositories.py has a minimal ai_observation writer",
    "def record_ai_observation(" in _repo_src,
)
check(
    "G: repositories.py has a minimal ai_reported_source writer",
    "def record_ai_reported_source(" in _repo_src,
)
check(
    "G: shadow_write.py exposes reported-only AI observation shadow writer",
    "def shadow_record_ai_observation(" in _sw_src,
)
check(
    "G: shadow AI writer does not create verified source_resource rows",
    "get_or_create_resource" not in _sw_src.split("def shadow_record_ai_observation(", 1)[1],
)
check(
    "G: shadow AI writer does not create evidence_relation rows",
    "record_evidence_relation" not in _sw_src.split("def shadow_record_ai_observation(", 1)[1],
)

# ============================================================
# H. Structurally append-only — no mutable-current-state column.
# ============================================================

for tname in ("ai_observation", "ai_reported_source"):
    ddl = TABLES.get(tname, "")
    check(
        f"H: {tname} has no 'current_value'/'latest_value'/'updated_at'-shaped mutable column "
        f"(an AI's self-report is a historical utterance, never corrected in place)",
        not any(bad in ddl.lower() for bad in ("current_value", "latest_value", "updated_at")),
        f"{ddl}",
    )

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
