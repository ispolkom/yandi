"""
agent/db_sql_integrity_regression_test.py — Этап 5E-S S7: tamper-
evident integrity journal (mandate §24-§27, §44 sections I/J).

Fully proven OFFLINE — pure hash-chain math and filesystem operations,
no SQL server needed (mandate §55: not credential-blocked).

Covers:
    (canonicalization) same logical record -> byte-identical
       serialization every time; sorted keys; explicit null; floats as
       fixed-precision strings, not raw repr; datetime rejected
       (caller must pre-normalize); format_version travels with the
       record.
    I. HASH CHAIN: modifying a historical row's content is detected
       (verify_record_against_event); deleting a historical event from
       the sequence is detected (verify_chain — broken previous_event_
       hash link); reordering two events is detected (same mechanism).
    J. SNAPSHOT ROLLBACK: an external checkpoint newer than the
       restored/current DB state -> ROLLBACK SUSPECTED, not silently
       accepted; DB ahead of checkpoint -> fine; DB matching checkpoint
       -> fine; first run with no checkpoint yet -> 'no_checkpoint',
       not a false alarm.
    (extra) two DIFFERENT integrity keys produce different chains for
       the identical record sequence (the key, not just the algorithm,
       determines the chain) — confirms an attacker without the real
       integrity key cannot forge a passing chain.
    (extra) checkpoint file writes are atomic (temp file + os.replace)
       and contain no knowledge (only seq/hash/entity_type/timestamp).

Run: /home/iam/venv/bin/python3 -m agent.db_sql_integrity_regression_test
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from agent.db.sql.integrity import (
    canonicalize_value, canonicalize_record, payload_hash, compute_event_hash,
    append_event, verify_chain, verify_record_against_event,
    make_checkpoint, load_checkpoint, check_for_rollback,
    GENESIS_HASH, UnsupportedFieldType,
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
# Canonicalization.
# ============================================================

fields_a = {"raw_text": "Сколько спутников у Юпитера?", "session_id": None, "confidence": 0.6}
fields_b = {"confidence": 0.6, "session_id": None, "raw_text": "Сколько спутников у Юпитера?"}  # different key order

canon_a = canonicalize_record(fields_a, entity_type="question", entity_id=1)
canon_b = canonicalize_record(fields_b, entity_type="question", entity_id=1)
check("canonicalize_record(): key order does not affect the output (sort_keys)", canon_a == canon_b)

check("canonicalize_record(): output is valid UTF-8 JSON", json.loads(canon_a.decode("utf-8"))["fields"]["raw_text"] == fields_a["raw_text"])
check("canonicalize_record(): None serializes to explicit null", json.loads(canon_a)["fields"]["session_id"] is None)
check(
    "canonicalize_record(): float becomes a fixed-precision string, not raw JSON float",
    isinstance(json.loads(canon_a)["fields"]["confidence"], str) and json.loads(canon_a)["fields"]["confidence"] == "0.600000",
)
check("canonicalize_record(): format_version travels with the record", json.loads(canon_a)["format_version"] == 1)
check(
    "canonicalize_record(): different entity_id -> different serialization even for identical fields",
    canonicalize_record(fields_a, entity_type="question", entity_id=1) != canonicalize_record(fields_a, entity_type="question", entity_id=2),
)

try:
    canonicalize_value(datetime.utcnow())
    datetime_raised = False
except UnsupportedFieldType:
    datetime_raised = True
check("canonicalize_value(): a raw datetime is REJECTED (caller must pre-convert to int)", datetime_raised)

try:
    canonicalize_record({"x": [1, 2, 3]}, entity_type="t", entity_id=1)
    list_raised = False
except UnsupportedFieldType:
    list_raised = True
check("canonicalize_record(): an unsupported type (list) raises rather than guessing a format", list_raised)

check(
    "canonicalize_record() is deterministic: calling it twice on the SAME record "
    "produces byte-identical output",
    canonicalize_record(fields_a, entity_type="question", entity_id=1) ==
    canonicalize_record(fields_a, entity_type="question", entity_id=1),
)


# ============================================================
# Hash chain basics.
# ============================================================

integrity_key = os.urandom(32)
chain = []
ev1 = append_event(integrity_key, chain, entity_type="question", entity_id=1, fields={"raw_text": "v1"})
chain.append(ev1)
ev2 = append_event(integrity_key, chain, entity_type="question", entity_id=1, fields={"raw_text": "v2"})
chain.append(ev2)
ev3 = append_event(integrity_key, chain, entity_type="question", entity_id=1, fields={"raw_text": "v3"})
chain.append(ev3)

check("append_event(): first event's previous_event_hash is the genesis hash", ev1["previous_event_hash"] == GENESIS_HASH)
check("append_event(): sequence numbers increment 1,2,3", [e["seq"] for e in chain] == [1, 2, 3])
check(
    "append_event(): each event's previous_event_hash equals the PRIOR event's own event_hash",
    ev2["previous_event_hash"] == ev1["event_hash"] and ev3["previous_event_hash"] == ev2["event_hash"],
)

ok, problems = verify_chain(integrity_key, chain)
check("verify_chain(): a genuine, untampered 3-event chain verifies clean", ok and not problems, f"{problems}")

check(
    "verify_record_against_event(): the CURRENT (unmodified) content still matches "
    "its recorded event",
    verify_record_against_event({"raw_text": "v2"}, ev2, entity_type="question", entity_id=1),
)
check(
    "verify_record_against_event(): TAMPERED content (row modified in place, T21) "
    "no longer matches its recorded event",
    not verify_record_against_event({"raw_text": "v2-TAMPERED"}, ev2, entity_type="question", entity_id=1),
)


# ============================================================
# I. HASH CHAIN — deletion and reordering detection.
# ============================================================

chain_missing_middle = [ev1, ev3]  # ev2 deleted
ok_del, problems_del = verify_chain(integrity_key, chain_missing_middle)
check(
    "I: deleting a historical event (ev2) from the sequence IS DETECTED "
    "(ev3's previous_event_hash no longer matches ev1's event_hash)",
    not ok_del and len(problems_del) > 0,
    f"{problems_del}",
)

chain_reordered = [ev2, ev1, ev3]  # ev1/ev2 swapped
ok_reorder, problems_reorder = verify_chain(integrity_key, chain_reordered)
check(
    "I: reordering two events IS DETECTED (broken previous_event_hash linkage)",
    not ok_reorder and len(problems_reorder) > 0,
    f"{problems_reorder}",
)

chain_tampered_hash = [ev1, dict(ev2, event_hash="0" * 64), ev3]
ok_tamper, problems_tamper = verify_chain(integrity_key, chain_tampered_hash)
check(
    "I: a directly-forged event_hash (without recomputing correctly) IS DETECTED",
    not ok_tamper and len(problems_tamper) > 0,
    f"{problems_tamper}",
)

wrong_key = os.urandom(32)
ok_wrong_key, problems_wrong_key = verify_chain(wrong_key, chain)
check(
    "an attacker WITHOUT the real integrity key cannot produce a chain that verifies "
    "correctly against the real key — verifying a genuine chain with the WRONG key fails",
    not ok_wrong_key,
    f"{problems_wrong_key}",
)


# ============================================================
# J. SNAPSHOT ROLLBACK — external checkpoint.
# ============================================================

tmp_dir = Path(tempfile.mkdtemp(prefix="p5es_integrity_"))
checkpoint_path = str(tmp_dir / "question.checkpoint.json")

status_none, _ = check_for_rollback("question", chain, checkpoint_path)
check("J: no checkpoint file yet -> 'no_checkpoint', not a false rollback alarm", status_none == "no_checkpoint")

cp = make_checkpoint("question", chain, checkpoint_path)
check("J: make_checkpoint() writes seq/event_hash/entity_type/created_at, no knowledge fields", set(cp.keys()) == {"entity_type", "seq", "event_hash", "created_at"})

status_ok, _ = check_for_rollback("question", chain, checkpoint_path)
check("J: DB state == checkpoint -> 'ok'", status_ok == "ok")

chain_advanced = chain + [append_event(integrity_key, chain, entity_type="question", entity_id=1, fields={"raw_text": "v4"})]
status_ahead, _ = check_for_rollback("question", chain_advanced, checkpoint_path)
check("J: DB has progressed past the checkpoint (legitimate new writes) -> 'ahead'", status_ahead == "ahead")

status_rollback, detail_rollback = check_for_rollback("question", [ev1], checkpoint_path)  # only ev1, checkpoint was made at ev3
check(
    "J CRITICAL: DB restored to an OLDER state than a previously-confirmed checkpoint "
    "-> 'ROLLBACK_SUSPECTED', never silently accepted",
    status_rollback == "ROLLBACK_SUSPECTED",
    f"{status_rollback} {detail_rollback}",
)

# Same seq, different hash (history diverged after an internally-valid
# restore of a DIFFERENT branch at the same sequence number).
diverged_ev3 = dict(ev3, event_hash="f" * 64)
status_diverged, detail_diverged = check_for_rollback("question", [ev1, ev2, diverged_ev3], checkpoint_path)
check(
    "J: same sequence number but a DIFFERENT hash at that position -> ROLLBACK_SUSPECTED "
    "(history diverged, not a benign no-op restore)",
    status_diverged == "ROLLBACK_SUSPECTED",
    f"{status_diverged} {detail_diverged}",
)

loaded_cp = load_checkpoint(checkpoint_path)
check("J: load_checkpoint() reads back exactly what make_checkpoint() wrote", loaded_cp == cp)

check(
    "J: checkpoint write is atomic — no leftover .tmp file after a successful write",
    not os.path.exists(checkpoint_path + ".tmp"),
)

try:
    make_checkpoint("question", [], checkpoint_path + ".empty")
    empty_chain_raised = False
except ValueError:
    empty_chain_raised = True
check("J: make_checkpoint() on an empty chain raises rather than writing a meaningless checkpoint", empty_chain_raised)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
