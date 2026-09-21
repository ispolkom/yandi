"""
agent/db/sql/field_protection.py — P1c-2: the person's own words are sealed (AES-256-GCM) before they reach the database.

WHAT IT PROTECTS. A fixed list of columns in the personal ledger (PROTECTED below): what the person said and what the
assistant answered (interaction_turn), the facts, promises and grievances derived from it. The database then holds
ciphertext; ids, timestamps, statuses and numbers stay readable (they are structure, not words).

THE SWITCH IS THE DATABASE, THE KEY IS THE CAPABILITY.
  * The database says whether protection is on: the newest row of `storage_protection_event` (none = off). Only
    `python -m agent.db.sql.protect` writes it, after every existing row has been sealed and checked.
  * Off        writes plaintext, reads plaintext (and sealed values, if a key is present) — exactly the behaviour before P1c-2.
  * Migrating  writes are sealed, reads accept both (the tool is in the middle of the change).
  * On         writes are sealed, reads REQUIRE sealed values. Without the key a write is refused (StorageLocked), a read of
               a sealed value is refused (StorageLocked); a plaintext value where a sealed one is required is refused
               (StorageTampered: somebody wrote around the application).
  Nothing here ever falls back to plaintext when a key is missing in a protected mode, and nothing returns partial text.

THE KEY. The Core installs it when it is unlocked (pet/core_lifecycle.py) and clears it on lock: the storage key is
HKDF(core key, "yandi/storage/personal/v1"), the core key being what the node derives from its root (HKDF(root, "yandi/core/v1")).
It is never written anywhere by this module. The mode record carries an HMAC (a different derived key), so a process that
holds the key notices a forged or foreign mode record instead of trusting it.

BINDING. Each value is bound (as AES-GCM associated data) to its table, its column and the natural key of its row, so a
sealed value cannot be moved to another row or column and still open. A value of one column can therefore not be swapped for
another either.

PLAINTEXT THAT LOOKS SEALED. A person can type "yp1:...". While protection is off such a value is stored with an escape
prefix ("yp0:") so that reading never mistakes it for a sealed one; the escape is removed on reading.

NOT COVERED (see docs/STORAGE_PROTECTION.md): tables outside PROTECTED, NULL-ness of optional columns, row counts,
timestamps, and anyone who holds the running process or the key. It is protection of data at rest, not of a live process.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import threading
import time
from typing import Any, Dict, Mapping, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agent.db.sql.crypto import decrypt_field, encrypt_field

SEALED_PREFIX = "yp1:"
ESCAPED_PREFIX = "yp0:"

MODE_OFF, MODE_MIGRATING, MODE_ON = "off", "migrating", "on"
MODES = (MODE_OFF, MODE_MIGRATING, MODE_ON)

STORAGE_INFO = b"yandi/storage/personal/v1"
PROOF_INFO = b"yandi/storage/proof/v1"
MODE_TTL_SECONDS = 10.0            # how long a process trusts the mode it read; a change reaches every process within this time

# table -> the columns sealed in it
PROTECTED: Dict[str, tuple] = {
    "interaction_turn": ("user_text", "assistant_text"),
    "personal_fact": ("statement", "evidence"),
    "personal_fact_event": ("evidence",),
    "commitment": ("text", "evidence"),
    "commitment_event": ("evidence",),
    "grievance": ("description", "context"),
}

# table -> the fields that identify a row by its content, bound into every sealed value of it
ENTITY_KEYS: Dict[str, tuple] = {
    "interaction_turn": ("user_id", "source_turn_id"),
    "personal_fact": ("fact_id",),
    "personal_fact_event": ("fact_id", "event_type", "source_turn_id"),
    "commitment": ("commitment_id",),
    "commitment_event": ("commitment_id", "event_type"),
    "grievance": ("id",),
}


class StorageProtectionError(Exception):
    """Base class. Messages never contain a key, a value or a row."""


class StorageLocked(StorageProtectionError):
    """Protection needs a key that this process does not hold."""


class StorageTampered(StorageProtectionError):
    """A stored value or the mode record is not what the application wrote (or it was sealed with another key)."""


# ── the key ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _hkdf(key: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(key)


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.key: Optional[bytes] = None
        self.proof_key: Optional[bytes] = None
        self.mode: Optional[str] = None
        self.mode_expires = 0.0


_state = _State()


def install_key(core_key: bytes) -> None:
    """Called by the Core when it is unlocked. `core_key` is the 32-byte key the node gave it."""
    if not isinstance(core_key, (bytes, bytearray)) or len(core_key) != 32:
        raise ValueError("the storage key must be derived from a 32-byte key")
    with _state.lock:
        _state.key = _hkdf(bytes(core_key), STORAGE_INFO)
        _state.proof_key = _hkdf(bytes(core_key), PROOF_INFO)
        _state.mode = None


def install_key_from_tool(tool_path: str, timeout: float = 30.0) -> None:
    """For the legacy PET (./start.sh), which is not unlocked by the node: ask the node's key tool (`yandi-keys core-key`, which opens the
    root with THIS machine's device key and refuses to print into a terminal) for the core key and install it. Fails closed: any problem is
    a StorageLocked, and the caller must not start serving as if nothing had happened."""
    import subprocess
    try:
        proc = subprocess.run([tool_path, "core-key"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise StorageLocked("the key tool could not be run") from None
    if proc.returncode != 0:
        reason = proc.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
        raise StorageLocked("the key tool refused: " + (reason[0][:120] if reason else f"exit {proc.returncode}"))
    try:
        key = base64.b64decode(proc.stdout.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise StorageLocked("the key tool did not print a key") from None
    if len(key) != 32:
        raise StorageLocked("the key tool did not print a 32-byte key")
    install_key(key)


def clear_key() -> None:
    with _state.lock:
        _state.key = None
        _state.proof_key = None
        _state.mode = None


def has_key() -> bool:
    return _state.key is not None


def storage_key() -> Optional[bytes]:
    """For the protect tool only (it seals and unseals rows in bulk)."""
    return _state.key


def forget_mode() -> None:
    """The next read looks at the database again."""
    with _state.lock:
        _state.mode = None


# ── the mode record ───────────────────────────────────────────────────────────────────────────────────────────────

def mode_proof(proof_key: bytes, nonce: str, mode: str) -> bytes:
    return hmac.new(proof_key, f"YANDI|storage-mode|v1|{nonce}|{mode}".encode("utf-8"), hashlib.sha256).digest()


def new_mode_record(mode: str) -> tuple:
    """(mode, nonce, proof) for a new record, made with the installed key."""
    if mode not in MODES:
        raise ValueError("unknown mode")
    if _state.proof_key is None:
        raise StorageLocked("a mode record can only be written by a process that holds the key")
    nonce = secrets.token_hex(16)
    return mode, nonce, mode_proof(_state.proof_key, nonce, mode)


def _is_missing_table(exc: BaseException) -> bool:
    args = getattr(exc, "args", ())
    return bool((args and args[0] == 1146) or "doesn't exist" in str(exc).lower())


def read_mode(conn) -> str:
    """The mode the database is in, verified when a key is present. A database without the table, or with no record, is off."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mode, nonce, proof FROM storage_protection_event ORDER BY event_id DESC LIMIT 1")
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        if _is_missing_table(exc):
            return MODE_OFF
        raise
    if not row:
        return MODE_OFF
    mode, nonce, proof = row["mode"], row["nonce"], row["proof"]
    if mode not in MODES:
        raise StorageTampered("the storage mode record is not recognised")
    with _state.lock:
        proof_key = _state.proof_key
    if proof_key is not None:
        expected = mode_proof(proof_key, str(nonce), mode)
        if not proof or not hmac.compare_digest(bytes(proof), expected):
            raise StorageTampered("the storage mode record does not verify with this key")
    return mode


def current_mode(conn) -> str:
    now = time.monotonic()
    with _state.lock:
        if _state.mode is not None and now < _state.mode_expires:
            return _state.mode
    mode = read_mode(conn)
    with _state.lock:
        _state.mode = mode
        _state.mode_expires = time.monotonic() + MODE_TTL_SECONDS
    return mode


# ── values ────────────────────────────────────────────────────────────────────────────────────────────────────────

def entity_id(table: str, values: Mapping[str, Any]) -> str:
    keys = ENTITY_KEYS[table]
    try:
        return "|".join(str(values[k]) for k in keys)
    except KeyError as exc:
        raise StorageProtectionError(f"the row of {table} does not name its key field {exc.args[0]!r}") from None


def is_sealed(stored: Any) -> bool:
    return isinstance(stored, str) and stored.startswith(SEALED_PREFIX)


def seal_with(key: bytes, table: str, column: str, row_key: Mapping[str, Any], text: str) -> str:
    blob = encrypt_field(key, text, entity_type=table, entity_id=entity_id(table, row_key), field_name=column)
    return SEALED_PREFIX + base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def open_with(key: bytes, table: str, column: str, row_key: Mapping[str, Any], stored: str) -> str:
    body = stored[len(SEALED_PREFIX):]
    try:
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        return decrypt_field(key, blob, entity_type=table, entity_id=entity_id(table, row_key), field_name=column)
    except (InvalidTag, binascii.Error, ValueError, UnicodeDecodeError):
        raise StorageTampered(f"a stored value of {table}.{column} does not open (wrong key, moved or altered)") from None


def seal(conn, table: str, column: str, row_key: Mapping[str, Any], value: Optional[str]) -> Optional[str]:
    """What to write for `value`. NULL stays NULL."""
    if column not in PROTECTED[table]:
        raise StorageProtectionError(f"{table}.{column} is not a protected column")
    if value is None:
        return None
    mode = current_mode(conn)
    if mode == MODE_OFF:
        return ESCAPED_PREFIX + value if value.startswith((SEALED_PREFIX, ESCAPED_PREFIX)) else value
    with _state.lock:
        key = _state.key
    if key is None:
        raise StorageLocked("storage protection is on and this process holds no key: nothing was written")
    return seal_with(key, table, column, row_key, value)


def open_value(conn, table: str, column: str, row_key: Mapping[str, Any], stored: Optional[str]) -> Optional[str]:
    if stored is None:
        return None
    if is_sealed(stored):
        with _state.lock:
            key = _state.key
        if key is None:
            raise StorageLocked("this value is sealed and this process holds no key")
        return open_with(key, table, column, row_key, stored)
    if current_mode(conn) == MODE_ON:
        raise StorageTampered(f"a stored value of {table}.{column} is not sealed although protection is on")
    return stored[len(ESCAPED_PREFIX):] if isinstance(stored, str) and stored.startswith(ESCAPED_PREFIX) else stored


def open_row(conn, table: str, row: Optional[Dict[str, Any]], known: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Open every protected column present in `row` (a dict from a SELECT). `known` supplies key fields the SELECT did not return."""
    if not row:
        return row
    row_key: Dict[str, Any] = dict(known or {})
    row_key.update({k: row[k] for k in ENTITY_KEYS[table] if k in row})
    for column in PROTECTED[table]:
        if column in row:
            row[column] = open_value(conn, table, column, row_key, row[column])
    return row


def open_rows(conn, table: str, rows, known: Optional[Mapping[str, Any]] = None):
    return [open_row(conn, table, row, known) for row in rows]
