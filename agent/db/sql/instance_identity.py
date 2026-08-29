"""
agent/db/sql/instance_identity.py — DATABASE BOOTSTRAP V1, mandate §4/§27:
"NO VERIFIED YANDI INSTANCE ID -> NO DESTRUCTIVE/PRIVILEGED DATABASE
OPERATION."

The mandate's own invariants this module exists to satisfy:
    EXISTING SQL != YANDI SQL
    SHARED INSTANCE != YANDI DATABASE
    A database named `yandi_epistemic` is NOT proof of ownership.

Two independent markers, cross-checked, neither trusted alone:
    1. A FILESYSTEM marker (`ensure_instance_id_file()`) — written once by
       deploy/install-yandi.sh during the root-run OS-bootstrap phase,
       readable WITHOUT a database connection (so "is this host's
       dedicated datadir even ours?" can be answered before attempting
       one, and before any DB-level bootstrap runs at all).
    2. A DATABASE row (`instance_identity` table, schema.py) — written
       once by the DB-level bootstrap phase, INSERTed with the SAME uuid
       the file already holds.

`verify_instance_identity()` is the one function every future
destructive/privileged database operation (migration, schema change,
re-bootstrap) is expected to call FIRST — mismatch or absence is always
a STOP, never an auto-repair (mandate §8: "ambiguous state -> STOP,
never rm -rf && recreate").

Nothing here connects to a socket/TCP itself — callers pass an already-
open `conn` (same convention as bootstrap.py/repositories.py) or a
plain filesystem path.
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

# ── Verdicts returned by verify_instance_identity() ──────────────────────
MATCH = "MATCH"
NO_DB_IDENTITY = "NO_DB_IDENTITY"
NO_FILE_IDENTITY = "NO_FILE_IDENTITY"
MISMATCH = "MISMATCH"


def generate_instance_id() -> str:
    """A fresh random identity — mandate §4: 'не усложняй PKI, нужна
    простая стабильная identity V1.' A plain UUID4 string, nothing more:
    not a certificate, not a keypair, not derived from any host fact
    (hostname/port/socket path are all mutable/spoofable labels the
    mandate explicitly says NOT to trust as identity)."""
    return str(uuid.uuid4())


def read_instance_id_file(path: str) -> Optional[str]:
    """Returns the stored instance id, or None if the file does not
    exist. Never raises on absence — absence is a normal, expected state
    before first bootstrap (mandate §8: DETECT -> VERIFY -> CREATE ONLY
    IF ABSENT, not an error path)."""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        value = f.read().strip()
    return value or None


def ensure_instance_id_file(path: str) -> str:
    """Idempotent: if `path` already holds an id, returns it UNCHANGED
    (mandate §8: 'НЕ regenerate keys/identity over existing ones'). Only
    creates a new id when the file is genuinely absent.

    Atomic write (temp file + rename) so a crash mid-write can never
    leave a half-written, unparseable identity file — the same pattern
    already established by agent/system_state_store.py's _save_latest().

    Written 0644 (world-readable, not a secret — the value only proves
    "this datadir/database belongs to YANDI," it grants no access by
    itself) but the PARENT directory's own permissions (root:root 0755
    for /etc/yandi/mysql, per DEDICATED_INSTANCE_DESIGN.md §C) already
    control who may replace it."""
    existing = read_instance_id_file(path)
    if existing is not None:
        return existing

    new_id = generate_instance_id()
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)

    tmp_path = path + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, new_id.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp_path, path)
    return new_id


def get_db_instance_id(conn) -> Optional[str]:
    """Reads the single instance_identity row, if any. None means the DB
    bootstrap phase has not yet run against this database — NOT an
    error, just an earlier point in the mandate §7 Phase A/Phase B
    sequence than 'fully bootstrapped'."""
    with conn.cursor() as cur:
        cur.execute("SELECT instance_uuid FROM instance_identity WHERE id = 1")
        row = cur.fetchone()
        return row.get("instance_uuid") if row else None


def record_instance_identity(
    conn, instance_uuid: str, created_by_host: Optional[str] = None, label: Optional[str] = None,
) -> None:
    """INSERTs the singleton identity row — ONLY if the table is
    genuinely empty. Never INSERT ... ON DUPLICATE KEY UPDATE: a
    duplicate call with a DIFFERENT uuid would mean two bootstrap
    attempts raced or disagreed, which is exactly the ambiguous state
    mandate §8 says must STOP, never silently resolve by picking one."""
    existing = get_db_instance_id(conn)
    if existing is not None:
        if existing == instance_uuid:
            return  # idempotent: same identity already recorded
        raise RuntimeError(
            f"instance_identity already holds a DIFFERENT uuid ({existing!r}) than "
            f"the one being recorded ({instance_uuid!r}) — refusing to overwrite "
            f"(mandate §8/§27: ambiguous identity state must STOP, never auto-resolve)."
        )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO instance_identity (id, instance_uuid, created_at, created_by_host, label) "
            "VALUES (1, %s, %s, %s, %s)",
            (instance_uuid, datetime.now(timezone.utc).replace(tzinfo=None), created_by_host or socket.gethostname(), label),
        )


def verify_instance_identity(conn, expected_uuid: str) -> Tuple[bool, str]:
    """The mandate §27 gate: 'NO VERIFIED YANDI INSTANCE ID -> NO
    DESTRUCTIVE/PRIVILEGED DATABASE OPERATION.' Callers about to run a
    migration, schema change, or re-bootstrap against a connection are
    expected to call this FIRST and refuse to proceed on anything but
    (True, MATCH).

    Returns (ok, reason) — reason is always one of the controlled
    vocabulary constants above, never a raw exception message (matches
    system_awareness.py's / security_selfcheck.py's established
    controlled-vocabulary convention)."""
    db_id = get_db_instance_id(conn)
    if db_id is None:
        return False, NO_DB_IDENTITY
    if db_id != expected_uuid:
        return False, MISMATCH
    return True, MATCH


def describe(file_path: str, conn=None) -> Dict[str, Any]:
    """A read-only summary for reporting/selfcheck use (never used to
    gate anything itself — security_selfcheck.py's check_instance_
    identity() calls verify_instance_identity() directly for that)."""
    file_id = read_instance_id_file(file_path)
    db_id = get_db_instance_id(conn) if conn is not None else None
    if file_id is None and db_id is None:
        status = "NOT_BOOTSTRAPPED"
    elif file_id is not None and db_id is None:
        status = "FILE_ONLY_AWAITING_DB_BOOTSTRAP"
    elif file_id is None and db_id is not None:
        status = "DB_ONLY_NO_FILE_MARKER"  # ambiguous — should never happen via the normal flow
    elif file_id == db_id:
        status = "CONSISTENT"
    else:
        status = "MISMATCH"
    return {"file_instance_id": file_id, "db_instance_id": db_id, "status": status}
