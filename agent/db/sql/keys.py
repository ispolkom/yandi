"""
agent/db/sql/keys.py — Этап 5E-S S5: key hierarchy (mandate §19/§20).

KEK (Key Encryption Key / MASTER) lives OUTSIDE SQL entirely — a local
file, path from the YANDI_KEK_PATH environment variable, permission-
checked before every load (refuses group/other-readable key files).
DESIGNED this pass: no bootstrap has generated a real KEK on this
machine (no systemd-creds / TPM verified available here, and standing
up new host infrastructure is out of this pass's scope — see
SECURITY_ARCHITECTURE.md §11), so every function here is unit-tested
against temp files/paths, not a real deployed key.

DEKs (Data Encryption Keys, one per data-classification group, mandate
§19) are generated fresh and WRAPPED (encrypted) under the KEK — the
wrapped bytes are safe to store in SQL (a future key_metadata table,
class E, not created this pass, see SECURITY_ARCHITECTURE.md §21); the
KEK itself must never be (mandate §20: "KEK в SQL хранить ЗАПРЕЩЕНО").

The integrity key (mandate §26's "independent from the encryption key"
rule) and the blind-index key (mandate §21's "dedicated" key) are both
HKDF-derived from the KEK with distinct info labels — independent
purposes without needing a third top-level secret to generate/back up.

Nothing in this module ever logs, prints, or returns key bytes through
any path a log line could capture — enforced by convention and checked
by agent/db_sql_crypto_regression_test.py's static grep.
"""
from __future__ import annotations

import os
import stat
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

KEK_PATH_ENV = "YANDI_KEK_PATH"
KEK_SIZE_BYTES = 32  # AES-256


class KeyStorageError(Exception):
    """Base for this module's key-handling errors."""


class KeyPermissionError(KeyStorageError):
    """A key file's on-disk permissions are too open (group/other
    readable) — fails closed rather than silently trusting a loosely-
    permissioned file."""


class KeyMissingError(KeyStorageError):
    """No KEK is configured/found. Callers (crypto.py) must fail closed
    (raise), never fall back to plaintext (mandate §20/§40)."""


def generate_kek() -> bytes:
    """A fresh, random 256-bit key. NOT auto-called by bootstrap.py —
    key generation is a deliberate, one-time operator action (mandate
    §37: generating a key creates a backup obligation a human must be
    made aware of, not something that happens silently as a side
    effect of installing YANDI)."""
    return AESGCM.generate_key(bit_length=256)


def save_kek(path: str, kek: bytes) -> None:
    """Writes the KEK to `path` with 0600 permissions, creating the
    parent directory with 0700 if needed. Refuses to overwrite an
    existing file — rotation (mandate §38) is a distinct, explicit
    operation, never an accidental overwrite."""
    if len(kek) != KEK_SIZE_BYTES:
        raise ValueError(f"refusing to save a {len(kek)}-byte key as a KEK (expected {KEK_SIZE_BYTES})")
    if os.path.exists(path):
        raise FileExistsError(f"KEK already exists at {path} — use an explicit rotation path, not overwrite")

    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, kek)
    finally:
        os.close(fd)


def load_kek(path: Optional[str] = None) -> bytes:
    """Loads the KEK from `path` (default: the YANDI_KEK_PATH env var).
    Fails closed: raises KeyMissingError if unset/absent, raises
    KeyPermissionError if the file is group/other-readable — never
    silently proceeds with a weakened guarantee."""
    path = path or os.environ.get(KEK_PATH_ENV)
    if not path:
        raise KeyMissingError(
            f"{KEK_PATH_ENV} is not set — no KEK configured. Refusing to "
            f"proceed (no plaintext fallback, mandate §20/§40)."
        )
    if not os.path.exists(path):
        raise KeyMissingError(f"KEK path {path!r} does not exist.")

    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise KeyPermissionError(
            f"KEK file {path!r} is readable by group/other (mode {oct(mode)}) "
            f"— refusing to load. Fix with `chmod 600 {path}`."
        )

    with open(path, "rb") as f:
        kek = f.read()
    if len(kek) != KEK_SIZE_BYTES:
        raise KeyStorageError(f"KEK at {path!r} is {len(kek)} bytes, expected {KEK_SIZE_BYTES} (AES-256 key)")
    return kek


def generate_dek() -> bytes:
    """A fresh DEK for one data-classification group (mandate §19)."""
    return AESGCM.generate_key(bit_length=256)


def wrap_dek(kek: bytes, dek: bytes) -> bytes:
    """Wraps (encrypts) a DEK under the KEK. The output IS safe to
    store in SQL (a future key_metadata table) — the KEK itself never
    is (mandate §20)."""
    aesgcm = AESGCM(kek)
    nonce = os.urandom(12)
    wrapped = aesgcm.encrypt(nonce, dek, b"YANDI|DEK_WRAP|v1")
    return nonce + wrapped


def unwrap_dek(kek: bytes, wrapped: bytes) -> bytes:
    aesgcm = AESGCM(kek)
    nonce, ciphertext = wrapped[:12], wrapped[12:]
    return aesgcm.decrypt(nonce, ciphertext, b"YANDI|DEK_WRAP|v1")


def derive_integrity_key(kek: bytes) -> bytes:
    """HKDF-derived, independent from any encryption DEK (mandate §19's
    "independent purposes" rule; §26's "integrity key separate from
    encryption key"). Rotating the KEK naturally rotates this too —
    intended: the integrity key's lifecycle is tied to the KEK's, not
    to any individual DEK's."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"YANDI|integrity-key|v1")
    return hkdf.derive(kek)


def derive_blind_index_key(kek: bytes) -> bytes:
    """Same HKDF derivation pattern, a DISTINCT info label — a separate
    purpose from both encryption DEKs and the integrity key (mandate
    §19: independent purposes, one key per purpose)."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"YANDI|blind-index-key|v1")
    return hkdf.derive(kek)
