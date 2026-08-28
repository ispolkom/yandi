"""
agent/db/sql/integrity.py — Этап 5E-S S7: tamper-evident integrity
journal (mandate §24-§27).

DESIGNED + unit-tested this pass, NOT wired into any production write
path (see SECURITY_ARCHITECTURE.md §21) — the primitives (canonical
serialization, hash chain, checkpoint comparison) are proven correct
in isolation first; integrating them into every canonical INSERT is
the next continuation.

This is a SECOND, independent wall behind SQL privileges (security_
grants.py) and immutability triggers (security_triggers.py) — it
exists specifically for the case those two cannot fully cover: an
attacker with admin-level bypass (SUPER/root) who disables a trigger
or a database restored from an old-but-internally-valid backup
(mandate §22/§27 — "rollback attack"). The chain DETECTS such tampering
after the fact; it does not prevent or restore (SECURITY_ARCHITECTURE.md
§14 says this plainly).

Design choices, per the mandate's own cautions against over-
engineering (§26): ONE chain per entity_type (not one global chain —
avoids serializing every canonical write through a single lock; "YANDI
сейчас не high-frequency trading system"), no Merkle-tree framework,
HMAC-SHA256 (not a hand-rolled construction).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

INTEGRITY_FORMAT_VERSION = 1
GENESIS_HASH = "0" * 64


class IntegrityError(Exception):
    """Base for this module's errors."""


class UnsupportedFieldType(IntegrityError):
    """A field value passed to canonicalize_record() isn't one of the
    explicitly-supported JSON-safe types — raised rather than silently
    guessing a serialization (mandate §25: no ambiguous formatting)."""


def canonicalize_value(value: Any) -> Any:
    """Normalizes ONE field value to a deterministic JSON-safe form.

    - None -> null (explicit, mandate §25).
    - bool/int/str -> unchanged (json's own serialization of these is
      already deterministic and platform-independent).
    - float -> a FIXED-PRECISION STRING ("%.6f"), never a raw float —
      avoids any ambiguity between "the same number, formatted
      differently" being treated as a hash mismatch, and sidesteps
      relying on Python's float repr staying stable forever.
    - datetime -> REJECTED. Caller must convert to an integer (e.g.
      microseconds since epoch) before calling — mandate §25: "integer
      time precision / normalized datetime", not a raw datetime object
      whose serialization the caller didn't explicitly choose.
    """
    if value is None or isinstance(value, bool) or isinstance(value, int) or isinstance(value, str):
        return value
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, datetime):
        raise UnsupportedFieldType(
            "datetime objects are not canonical-serializable directly — convert to "
            "an integer (e.g. microseconds since epoch) before calling canonicalize_record()"
        )
    raise UnsupportedFieldType(f"unsupported type for canonical serialization: {type(value)!r}")


def canonicalize_record(
    fields: Dict[str, Any], *, entity_type: str, entity_id: Union[str, int],
    format_version: int = INTEGRITY_FORMAT_VERSION,
) -> bytes:
    """Deterministic, versioned byte representation of one canonical
    record (mandate §25). Same logical record -> byte-identical output
    on every call, on every platform: UTF-8, sort_keys, explicit
    separators (no incidental whitespace differences), an explicit
    format_version field so a future serializer change doesn't break
    the ability to verify OLD history (mandate: "Изменение serializer
    в будущем не должно уничтожать возможность проверить старую
    историю" — old events keep re-verifying against THEIR OWN recorded
    format_version, not whatever version canonicalize_record() defaults
    to today)."""
    envelope = {
        "format_version": format_version,
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "fields": {k: canonicalize_value(v) for k, v in fields.items()},
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_hash(canonical_bytes: bytes) -> str:
    return hashlib.sha256(canonical_bytes).hexdigest()


def compute_event_hash(
    integrity_key: bytes, format_version: int, entity_type: str, entity_id: Union[str, int],
    payload_hash_hex: str, previous_event_hash_hex: str,
) -> str:
    """mandate §26: event_hash = HMAC-SHA256(K_integrity, format_version
    || entity_type || entity_id || payload_hash || previous_event_hash).
    K_integrity is a DEDICATED key (keys.py::derive_integrity_key()),
    never an encryption DEK/KEK."""
    if not integrity_key:
        raise IntegrityError("compute_event_hash() called with no integrity key.")
    message = f"{format_version}|{entity_type}|{entity_id}|{payload_hash_hex}|{previous_event_hash_hex}".encode("utf-8")
    return hmac.new(integrity_key, message, hashlib.sha256).hexdigest()


def append_event(
    integrity_key: bytes, chain: List[Dict[str, Any]], *, entity_type: str,
    entity_id: Union[str, int], fields: Dict[str, Any], format_version: int = INTEGRITY_FORMAT_VERSION,
) -> Dict[str, Any]:
    """Builds the NEXT event for one entity_type's chain — does not
    mutate `chain` (caller decides how/where to persist the returned
    event, e.g. a future integrity_event table)."""
    canonical = canonicalize_record(fields, entity_type=entity_type, entity_id=entity_id, format_version=format_version)
    p_hash = payload_hash(canonical)
    previous_event_hash = chain[-1]["event_hash"] if chain else GENESIS_HASH
    seq = (chain[-1]["seq"] + 1) if chain else 1
    event_hash = compute_event_hash(integrity_key, format_version, entity_type, entity_id, p_hash, previous_event_hash)
    return {
        "seq": seq,
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "format_version": format_version,
        "payload_hash": p_hash,
        "previous_event_hash": previous_event_hash,
        "event_hash": event_hash,
    }


def verify_chain(integrity_key: bytes, events: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """Walks a sequence of events and confirms: (1) each event's
    previous_event_hash actually equals the PRECEDING event's real
    event_hash (catches a deleted middle event — the gap breaks this
    link; catches reordering — swapping two events breaks it too), and
    (2) each event's own event_hash correctly recomputes from its
    stored payload_hash/metadata (catches a tampered event_hash or
    metadata field that wasn't recomputed consistently).

    Does NOT by itself catch "attacker modified the row's data AND
    recomputed a consistent payload_hash/event_hash" — that requires
    the integrity_key, which an attacker without it cannot do; but if
    you want to verify actual CURRENT row content against a stored
    event, use verify_record_against_event() below."""
    problems: List[str] = []
    previous_event_hash = GENESIS_HASH
    for event in events:
        if event["previous_event_hash"] != previous_event_hash:
            problems.append(
                f"seq={event.get('seq')}: previous_event_hash mismatch "
                f"(expected {previous_event_hash}, got {event['previous_event_hash']}) "
                f"— a prior event was deleted, reordered, or replaced"
            )
        expected_hash = compute_event_hash(
            integrity_key, event["format_version"], event["entity_type"], event["entity_id"],
            event["payload_hash"], event["previous_event_hash"],
        )
        if event["event_hash"] != expected_hash:
            problems.append(f"seq={event.get('seq')}: event_hash does not match recomputation — tampered")
        previous_event_hash = event["event_hash"]
    return (len(problems) == 0, problems)


def verify_record_against_event(
    fields: Dict[str, Any], event: Dict[str, Any], *, entity_type: str, entity_id: Union[str, int],
) -> bool:
    """Recomputes the canonical payload_hash from ACTUAL CURRENT field
    values and compares against what the stored event claims — this is
    the check that actually catches "the row's content was modified in
    place" (T12/T18/T21), independent of whether the chain itself still
    looks internally consistent."""
    canonical = canonicalize_record(
        fields, entity_type=entity_type, entity_id=entity_id, format_version=event["format_version"],
    )
    return payload_hash(canonical) == event["payload_hash"]


# ============================================================
# External checkpoint (mandate §27 — rollback detection).
# ============================================================

def make_checkpoint(entity_type: str, events: List[Dict[str, Any]], path: str) -> Dict[str, Any]:
    """Writes {entity_type, seq, event_hash, created_at} to `path`
    (mandate: outside SQL, a protected local filesystem path) —
    atomically (write to a temp file, fsync, os.replace) so a crash
    mid-write never leaves a half-written checkpoint. Contains no
    knowledge (mandate §27: "Checkpoint: не содержит knowledge")."""
    if not events:
        raise ValueError("cannot checkpoint an empty chain")
    latest = events[-1]
    checkpoint = {
        "entity_type": entity_type,
        "seq": latest["seq"],
        "event_hash": latest["event_hash"],
        "created_at": int(time.time()),
    }
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    return checkpoint


def load_checkpoint(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_for_rollback(
    entity_type: str, current_events: List[Dict[str, Any]], checkpoint_path: str,
) -> Tuple[str, Optional[str]]:
    """Returns (status, detail). status is one of:
        'no_checkpoint'      — nothing to compare against yet (first run)
        'ok'                 — DB matches the last confirmed checkpoint
        'ahead'              — DB has progressed since the checkpoint (normal)
        'ROLLBACK_SUSPECTED' — DB is BEHIND a previously-confirmed
                               checkpoint, or diverged at the same
                               sequence number — mandate §27/§28: MUST
                               NOT proceed with canonical writes
                               silently when this is returned."""
    checkpoint = load_checkpoint(checkpoint_path)
    if checkpoint is None:
        return ("no_checkpoint", None)

    if not current_events:
        return ("ROLLBACK_SUSPECTED", "DB has zero events for an entity_type with a prior checkpoint")

    latest = current_events[-1]
    if latest["seq"] < checkpoint["seq"]:
        return ("ROLLBACK_SUSPECTED", f"DB seq {latest['seq']} < checkpoint seq {checkpoint['seq']}")
    if latest["seq"] == checkpoint["seq"] and latest["event_hash"] != checkpoint["event_hash"]:
        return ("ROLLBACK_SUSPECTED", "same sequence number but different hash — history has diverged")
    if latest["seq"] == checkpoint["seq"]:
        return ("ok", None)
    return ("ahead", None)
