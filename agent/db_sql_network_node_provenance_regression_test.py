"""
agent/db_sql_network_node_provenance_regression_test.py — Этап 5 (SQL
persistence migration) regression: NETWORK_NODE PROVENANCE extension
contract (agent/db/sql/schema.py's documentation-only note + the one
structural fix it required).

SCOPE, exactly as instructed — no network protocol, no node discovery,
no DHT, no onion routing, no reputation protocol, no consensus/voting,
no distributed Trust, no remote revalidation, no cryptographic signing
beyond what node/src/core/identity.rs already provides, no NETWORK_NODE
route activation. INSPECT CONCLUSION this pass reached (mandate §19):
the schema is "extensible enough for now" — documented as an extension
contract, NOT five new tables (NETWORK_NODE/NODE_OBSERVATION/NODE_
REPORTED_PROVENANCE/NODE_REPORTED_SOURCE/PROVENANCE_RESOLUTION are all
explicitly NOT created here).

The one REAL structural fix this INSPECT found: source_resource.node_id
was VARCHAR(40) — too narrow for this codebase's actual node identity,
node/src/core/identity.rs::NodeIdentity::node_id() (a real Ed25519/
X25519-derived 32-byte HashId, hex-encoded to exactly 64 chars).

Covers:
    A. source_resource.node_id is now VARCHAR(64) — wide enough for a
       real hex-encoded HashId (proven against the ACTUAL Rust HashId::
       to_hex() output shape: 32 bytes -> 64 hex chars), not just an
       assumption about the number.
    B. the module documents the fundamental rule (shared with AI
       provenance) verbatim: "SELF-REPORT != VERIFIED PROVENANCE",
       "REMOTE NODE PROVENANCE IS A CLAIM ABOUT PROVENANCE. IT IS NOT
       PROVENANCE UNTIL LOCALLY RESOLVED.", "DO NOT COUNT NODES. COUNT
       DISTINGUISHABLE PROVENANCE ROOTS."
    C. NETWORK_NODE_PROVENANCE_INVARIANTS holds exactly the 9 required
       invariant strings from the mandate, verbatim — a canonical,
       checkable list future code can assert against.
    D. the module explicitly documents the difference between the REAL
       future P2P node_id (Rust HashId) and the ALREADY-ACTIVE, UNRELATED
       "node_id" agent/orch_federation.py and agent/orch_reputation.py
       already use for local background-validation council members —
       so a future implementation can't silently conflate the two.
    E. NO new tables were created: NETWORK_NODE/NODE_OBSERVATION/
       NODE_REPORTED_PROVENANCE/NODE_REPORTED_SOURCE/
       PROVENANCE_RESOLUTION do not appear in ALL_TABLES_IN_ORDER —
       proves this stayed documentation + one column width fix, as
       instructed, not a silently-built subsystem.
    F. no banned truth-claiming vocabulary was introduced by this note.
    G. NO implementation exists yet: zero references to any of the 5
       future table names anywhere in repositories.py or shadow_write.py.

Run: /home/iam/venv/bin/python3 -m agent.db_sql_network_node_provenance_regression_test
"""
from __future__ import annotations

import inspect
import re

from agent.db.sql.schema import (
    ALL_TABLES_IN_ORDER, _BANNED_TOKENS, NETWORK_NODE_PROVENANCE_INVARIANTS,
)
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


TABLES = dict(ALL_TABLES_IN_ORDER)
_schema_src = inspect.getsource(schema_mod)

# ============================================================
# A. node_id column width, proven against the REAL Rust HashId shape.
# ============================================================

_sr_ddl = TABLES.get("source_resource", "")
check(
    "A: source_resource.node_id is VARCHAR(64) (was VARCHAR(40) — too narrow)",
    "node_id               VARCHAR(64) NULL" in _sr_ddl,
    f"{_sr_ddl[:400]}",
)

try:
    with open("/home/iam/yandi/node/src/util/types.rs", encoding="utf-8") as f:
        _hashid_src = f.read()
    _has_32_byte_array = "pub struct HashId(pub [u8; 32])" in _hashid_src
    _has_hex_encode = "hex::encode(self.0)" in _hashid_src
except FileNotFoundError:
    _has_32_byte_array = False
    _has_hex_encode = False

check(
    "A: the REAL Rust HashId is a [u8; 32] with a to_hex() that hex-encodes all "
    "32 bytes (32 * 2 = 64 hex chars) — the 64-char width is not a guess",
    _has_32_byte_array and _has_hex_encode,
    f"32-byte array found={_has_32_byte_array} hex-encode found={_has_hex_encode}",
)
# Concrete proof independent of reading the Rust source: any 32-byte value
# hex-encodes to exactly 64 hex characters.
check(
    "A: 32 bytes hex-encode to exactly 64 characters (2 hex chars per byte) — "
    "the fixed fact the VARCHAR(64) width is sized against",
    len((b"\x00" * 32).hex()) == 64,
)

# ============================================================
# B. Fundamental rule documented verbatim.
# ============================================================

for phrase in (
    "SELF-REPORT != VERIFIED PROVENANCE.",
    "REMOTE NODE PROVENANCE IS A CLAIM ABOUT PROVENANCE.",
    "IT IS NOT PROVENANCE UNTIL LOCALLY RESOLVED.",
    "DO NOT COUNT NODES. COUNT DISTINGUISHABLE PROVENANCE ROOTS.",
):
    check(f"B: required phrase present verbatim: {phrase!r}", phrase in _schema_src)

# ============================================================
# C. Canonical invariant list — exact match to the mandate's 9 lines.
# ============================================================

_expected_invariants = {
    "NODE_ID != INDEPENDENT_ROOT",
    "NODE_RELAY != NEW_ROOT",
    "MEMORY_REPLAY != NEW_ROOT",
    "REMOTE_REPORTED_SOURCE != LOCALLY_VERIFIED_SOURCE",
    "REMOTE_REPORTED_TRUST != LOCAL_CANONICAL_TRUST",
    "REMOTE_AI_REPORT != LOCAL_AI_OBSERVATION",
    "SELF_REPORTED_PROVENANCE != VERIFIED_PROVENANCE",
    "SAME URL THROUGH MULTIPLE NODES != MULTIPLE ROOTS",
    "HISTORICAL OBSERVATIONS ARE APPEND-ONLY",
}
check(
    "C: NETWORK_NODE_PROVENANCE_INVARIANTS holds exactly the 9 required invariants, "
    "verbatim, no more, no fewer",
    set(NETWORK_NODE_PROVENANCE_INVARIANTS) == _expected_invariants,
    f"got {set(NETWORK_NODE_PROVENANCE_INVARIANTS)}",
)

# ============================================================
# D. The two DIFFERENT "node_id" concepts are explicitly distinguished.
# ============================================================

check(
    "D: the note explicitly warns against confusing the future P2P node_id with "
    "agent/orch_federation.py's/agent/orch_reputation.py's own unrelated local "
    "background-validation 'node_id' concept",
    "orch_federation.py" in _schema_src and "orch_reputation.py" in _schema_src
    and "local-qwen14b-a" in _schema_src,
    "expected explicit cross-reference to the existing unrelated node_id usage",
)

# Confirm those two Python modules really do use "node_id" for something
# unrelated to P2P peer identity, so the warning above is grounded in fact,
# not a hypothetical.
import agent.orch_federation as fed_mod
import agent.orch_reputation as rep_mod

_fed_src = inspect.getsource(fed_mod)
_rep_src = inspect.getsource(rep_mod)
check(
    "D precondition: agent/orch_federation.py really does use 'node_id' for local "
    "council/model labels (e.g. 'local-qwen14b-a'), confirming this is a real, "
    "not hypothetical, naming collision risk",
    "local-qwen14b-a" in _fed_src and "node_id" in _fed_src,
)
check(
    "D precondition: agent/orch_reputation.py really does define its own 'node_id'-"
    "keyed table, unrelated to any P2P identity",
    "node_id" in _rep_src,
)

# ============================================================
# E. NO new tables were created — documentation + one column fix only.
# ============================================================

_table_names = {n for n, _ in ALL_TABLES_IN_ORDER}
for forbidden in (
    "network_node", "node_observation", "node_reported_provenance",
    "node_reported_source", "provenance_resolution",
):
    check(
        f"E: '{forbidden}' table was NOT created (documentation + one column "
        f"width fix only, as instructed — 'не создавать их все автоматически')",
        forbidden not in _table_names,
        f"tables: {sorted(_table_names)}",
    )

# ============================================================
# F. No banned truth-claiming vocabulary introduced.
# ============================================================

_note_start = _schema_src.find("NETWORK_NODE PROVENANCE — extension contract")
_note_end = _schema_src.find("# ── RUN_ERROR", _note_start)
_note_text = _schema_src[_note_start:_note_end] if _note_start > -1 else ""
check("F: the extension-contract note section was actually found", _note_start > -1 and _note_end > _note_start)
for tok in _BANNED_TOKENS:
    check(f"F: banned token {tok!r} does not appear in the extension-contract note", tok not in _note_text)

# ============================================================
# G. No implementation exists yet.
# ============================================================

_repo_src = inspect.getsource(repo_mod)
_sw_src = inspect.getsource(sw_mod)
for forbidden in ("node_observation", "node_reported_provenance", "node_reported_source", "provenance_resolution"):
    check(
        f"G: zero references to '{forbidden}' in repositories.py (no write/read "
        f"functions built yet)",
        forbidden not in _repo_src,
    )
    check(
        f"G: zero references to '{forbidden}' in shadow_write.py (no shadow-write "
        f"wiring built yet)",
        forbidden not in _sw_src,
    )

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
