"""
agent/integrity_regression_test.py — "10-year bastion" Layer 4:
tamper-evidence hash-chain (agent/integrity.py).

Covers:
    1. Schema: integrity_journal is positioned correctly in schema.
       ALL_TABLES_IN_ORDER, classified "B" (append-only), and therefore
       covered by security_triggers.immutability_triggers()'s existing
       class-A/B handling with zero new trigger code.
    2. canonical_row_json()/content_hash(): deterministic regardless of
       dict key order; changes when any field value changes.
    3. compute_entry_hash(): deterministic; changes if ANY input
       (prev_hash, table_name, row_pk, row_content_hash, seq) changes.
    4. append_entry(): correct genesis handling (first entry's
       prev_hash is GENESIS_HASH), correct chaining across multiple
       entries, uses SELECT ... FOR UPDATE to serialize concurrent
       appends.
    5. verify_chain(): ok=True on an untouched chain; ok=False with the
       correct broken_at_seq for (a) an in-place content edit, (b) a
       prev_hash edit, (c) a deleted middle entry — the three tamper
       shapes an out-of-band UPDATE/DELETE could actually produce.
    6. verify_row_against_journal(): journaled=False for a never-
       journaled row; ok=True for a match; ok=False for drifted content.
    7. External anchor (git): write_anchor_file()/commit_anchor()/
       anchor_chain_head() against a REAL local temporary git
       repository (git init) — proves the commit genuinely lands, not
       just that subprocess.run() was called with plausible-looking
       arguments.

Run: /home/iam/venv/bin/python3 -m agent.integrity_regression_test
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from agent.db.sql.schema import ALL_TABLES_IN_ORDER, TABLE_CLASSIFICATION
from agent.db.sql.security_triggers import _FULLY_IMMUTABLE_TABLES
import agent.integrity as integrity
from agent.integrity import (
    GENESIS_HASH, canonical_row_json, content_hash, compute_entry_hash,
    append_entry, get_chain_head, verify_chain, verify_row_against_journal,
    anchor_payload, write_anchor_file, commit_anchor, anchor_chain_head,
)

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
# 1. Schema positioning/classification.
# ============================================================

_table_names = [n for n, _ in ALL_TABLES_IN_ORDER]
check("1: integrity_journal exists in ALL_TABLES_IN_ORDER", "integrity_journal" in _table_names)
check(
    "1: integrity_journal comes AFTER decision_event (v3 added after v2, same "
    "append-in-order convention every prior schema addition this session used)",
    _table_names.index("integrity_journal") > _table_names.index("decision_event"),
)
check(
    "1: integrity_journal is classified 'B' (append-only) in TABLE_CLASSIFICATION",
    TABLE_CLASSIFICATION.get("integrity_journal") == "B",
)
check(
    "1: integrity_journal is therefore covered by immutability_triggers()'s "
    "existing class-A/B handling — zero new trigger code needed",
    "integrity_journal" in _FULLY_IMMUTABLE_TABLES,
)

# ============================================================
# 2. canonical_row_json() / content_hash().
# ============================================================

row_a = {"b": 2, "a": 1, "c": "x"}
row_a_reordered = {"c": "x", "a": 1, "b": 2}
check(
    "2: canonical_row_json() is independent of dict key insertion order",
    canonical_row_json(row_a) == canonical_row_json(row_a_reordered),
)
check(
    "2: content_hash() is therefore also independent of key order",
    content_hash(row_a) == content_hash(row_a_reordered),
)
row_b = {"a": 1, "b": 2, "c": "y"}  # one field changed
check(
    "2: content_hash() changes when any field value changes",
    content_hash(row_a) != content_hash(row_b),
)
check(
    "2: content_hash() output is a 64-hex-char SHA-256 digest",
    len(content_hash(row_a)) == 64 and all(c in "0123456789abcdef" for c in content_hash(row_a)),
)

# ============================================================
# 3. compute_entry_hash().
# ============================================================

base_args = (GENESIS_HASH, "decision_event", "abc123", content_hash(row_a), 1)
h0 = compute_entry_hash(*base_args)
check("3: compute_entry_hash() is deterministic", compute_entry_hash(*base_args) == h0)
check(
    "3: changing prev_hash changes the result",
    compute_entry_hash("f" * 64, *base_args[1:]) != h0,
)
check(
    "3: changing table_name changes the result",
    compute_entry_hash(base_args[0], "verification_run", *base_args[2:]) != h0,
)
check(
    "3: changing row_pk changes the result",
    compute_entry_hash(base_args[0], base_args[1], "different-pk", base_args[3], base_args[4]) != h0,
)
check(
    "3: changing seq changes the result",
    compute_entry_hash(*base_args[:-1], 2) != h0,
)


# ============================================================
# Minimal fake connection modeling integrity_journal as a real
# in-memory append-only list — enough to exercise append_entry()/
# verify_chain()/verify_row_against_journal() faithfully, including
# genuinely honoring "ORDER BY seq DESC LIMIT 1 FOR UPDATE".
# ============================================================

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None
        self._results = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.conn.calls.append(norm)
        upper = norm.upper()
        self._result = None
        self._results = None
        if upper.startswith("SELECT SEQ, ENTRY_HASH FROM INTEGRITY_JOURNAL"):
            self.conn.head_reads += 1
            if "FOR UPDATE" in upper:
                self.conn.head_reads_for_update += 1
            self._result = dict(self.conn.rows[-1]) if self.conn.rows else None
        elif upper.startswith("INSERT INTO INTEGRITY_JOURNAL"):
            table_name, row_pk, row_content_hash, prev_hash, entry_hash, created_at = params
            self.conn.rows.append({
                "seq": len(self.conn.rows) + 1, "table_name": table_name, "row_pk": row_pk,
                "row_content_hash": row_content_hash, "prev_hash": prev_hash,
                "entry_hash": entry_hash, "created_at": created_at,
            })
        elif upper.startswith("SELECT * FROM INTEGRITY_JOURNAL ORDER BY SEQ ASC"):
            self._results = [dict(r) for r in self.conn.rows]
        elif upper.startswith("SELECT ROW_CONTENT_HASH FROM INTEGRITY_JOURNAL"):
            table_name, row_pk = params
            matches = [r for r in self.conn.rows if r["table_name"] == table_name and r["row_pk"] == row_pk]
            self._result = {"row_content_hash": matches[-1]["row_content_hash"]} if matches else None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results or []


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.rows = []
        self.head_reads = 0
        self.head_reads_for_update = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


# ============================================================
# 4. append_entry(): genesis + chaining + FOR UPDATE locking.
# ============================================================

conn = FakeConnection()
e1 = append_entry(conn, "decision_event", "evt-1", {"event_type": "DecisionStarted"})
check("4: first entry's seq is 1", e1["seq"] == 1)
check("4: first entry's prev_hash is GENESIS_HASH", e1["prev_hash"] == GENESIS_HASH)
check(
    "4: append_entry() locks the head with SELECT ... FOR UPDATE (concurrency safety)",
    conn.head_reads_for_update == conn.head_reads == 1,
)

e2 = append_entry(conn, "decision_event", "evt-2", {"event_type": "DecisionFinished"})
check("4: second entry's seq is 2", e2["seq"] == 2)
check(
    "4: second entry's prev_hash equals the FIRST entry's entry_hash (real chaining)",
    e2["prev_hash"] == e1["entry_hash"],
)

e3 = append_entry(conn, "verification_run", "run-xyz", {"status": "COMPLETE"})
check("4: a DIFFERENT table_name chains into the SAME sequence (one global chain)", e3["seq"] == 3)
check("4: get_chain_head() reports the latest entry", get_chain_head(conn)["seq"] == 3)

# ============================================================
# 5. verify_chain(): happy path + three tamper shapes.
# ============================================================

result_ok = verify_chain(conn)
check("5: verify_chain() reports ok=True on an untouched chain", result_ok["ok"] is True)
check("5: entries_checked matches the real row count", result_ok["entries_checked"] == 3)

# (a) in-place content edit: someone directly UPDATEs entry #2's stored
# row_content_hash (simulating an out-of-band tamper via raw SQL/file edit).
conn_a = FakeConnection()
append_entry(conn_a, "decision_event", "evt-1", {"x": 1})
append_entry(conn_a, "decision_event", "evt-2", {"x": 2})
append_entry(conn_a, "decision_event", "evt-3", {"x": 3})
conn_a.rows[1]["row_content_hash"] = "f" * 64  # tamper
result_a = verify_chain(conn_a)
check(
    "5a: in-place content-hash edit on entry #2 is detected, broken_at_seq=2",
    result_a["ok"] is False and result_a["broken_at_seq"] == 2,
    f"{result_a}",
)

# (b) prev_hash edit on a middle entry.
conn_b = FakeConnection()
append_entry(conn_b, "decision_event", "evt-1", {"x": 1})
append_entry(conn_b, "decision_event", "evt-2", {"x": 2})
append_entry(conn_b, "decision_event", "evt-3", {"x": 3})
conn_b.rows[1]["prev_hash"] = "e" * 64  # tamper
result_b = verify_chain(conn_b)
check(
    "5b: prev_hash edit on entry #2 is detected, broken_at_seq=2",
    result_b["ok"] is False and result_b["broken_at_seq"] == 2,
    f"{result_b}",
)

# (c) a middle entry is DELETED outright (DELETE somehow bypassed the
# class-B trigger — e.g. root disabling triggers directly).
conn_c = FakeConnection()
append_entry(conn_c, "decision_event", "evt-1", {"x": 1})
append_entry(conn_c, "decision_event", "evt-2", {"x": 2})
append_entry(conn_c, "decision_event", "evt-3", {"x": 3})
del conn_c.rows[1]  # entry #2 vanishes; entry #3 remains with its OLD prev_hash
result_c = verify_chain(conn_c)
check(
    "5c: a deleted middle entry breaks the chain at the NEXT surviving entry "
    "(its prev_hash no longer matches anything actually present)",
    result_c["ok"] is False and result_c["broken_at_seq"] == 3,
    f"{result_c}",
)

# ============================================================
# 6. verify_row_against_journal().
# ============================================================

conn2 = FakeConnection()
append_entry(conn2, "decision_event", "evt-42", {"event_type": "DecisionStarted", "confidence": 0.9})

r_never = verify_row_against_journal(conn2, "decision_event", "evt-does-not-exist", {"x": 1})
check("6: a never-journaled row reports journaled=False, ok=True (nothing to compare)", r_never == {"ok": True, "journaled": False, "reason": None})

r_match = verify_row_against_journal(conn2, "decision_event", "evt-42", {"event_type": "DecisionStarted", "confidence": 0.9})
check("6: an unchanged row reports ok=True, journaled=True", r_match["ok"] is True and r_match["journaled"] is True)

r_drift = verify_row_against_journal(conn2, "decision_event", "evt-42", {"event_type": "DecisionStarted", "confidence": 0.1})
check(
    "6: a row whose live content drifted from its journaled hash reports ok=False",
    r_drift["ok"] is False and r_drift["journaled"] is True,
    f"{r_drift}",
)

# ============================================================
# 7. External anchor — REAL local git repository.
# ============================================================

with tempfile.TemporaryDirectory() as repo_dir:
    subprocess.run(["git", "init", "-q", repo_dir], check=True)
    subprocess.run(["git", "-C", repo_dir, "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Integrity Test"], check=True)

    conn3 = FakeConnection()
    append_entry(conn3, "decision_event", "evt-1", {"x": 1})
    append_entry(conn3, "decision_event", "evt-2", {"x": 2})

    result = anchor_chain_head(conn3, repo_dir)
    check("7: anchor_chain_head() returns verify_chain()'s own ok=True result", result["verify"]["ok"] is True)
    check("7: anchor_chain_head() returns a commit_sha", isinstance(result["commit_sha"], str) and len(result["commit_sha"]) == 40)

    anchor_file = Path(repo_dir) / "anchor.json"
    check("7: anchor.json actually exists on disk", anchor_file.exists())
    payload_on_disk = json.loads(anchor_file.read_text())
    check(
        "7: anchor.json's content matches the current chain head",
        payload_on_disk["seq"] == 2 and payload_on_disk["entry_hash"] == get_chain_head(conn3)["entry_hash"],
        f"{payload_on_disk}",
    )

    log_output = subprocess.run(
        ["git", "-C", repo_dir, "log", "--oneline"], check=True, capture_output=True, text=True,
    ).stdout
    check(
        "7: the commit genuinely landed in this repo's real git history (not just a "
        "subprocess.run() call that happened to be made)",
        result["commit_sha"][:7] in log_output,
        log_output,
    )

    # A second anchor call (chain advanced further) produces a SECOND commit.
    append_entry(conn3, "decision_event", "evt-3", {"x": 3})
    result2 = anchor_chain_head(conn3, repo_dir)
    check(
        "7: a second anchor call after the chain advanced produces a DIFFERENT commit",
        result2["commit_sha"] != result["commit_sha"],
    )
    _anchor_src = __import__("inspect").getsource(integrity.anchor_chain_head) \
        + __import__("inspect").getsource(integrity.commit_anchor)
    check(
        "7: never invokes `git push` as an actual subprocess argument (only the "
        "docstrings TALK about not pushing — this checks the real argv literals, "
        "quoted either way, never contain \"push\")",
        '"push"' not in _anchor_src and "'push'" not in _anchor_src,
    )

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
