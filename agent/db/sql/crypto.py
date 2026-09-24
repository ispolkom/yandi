"""
agent/db/sql/crypto.py — Этап 5E-S S5: AES-256-GCM application
encryption + blind index (mandate §17/§18/§21).

AEAD via the `cryptography` library's AESGCM — a mature, audited
implementation (mandate §17: "Не писать свою криптографию. Для
шифрования: AES-256-GCM через зрелую библиотеку."). Every encryption
uses a fresh CSPRNG nonce, never reused with the same key (mandate
§18) — proven this pass by agent/db_sql_crypto_regression_test.py
encrypting the same plaintext twice and asserting both the ciphertext
AND the nonce differ.

AAD binds each ciphertext to schema/entity_type/entity_id/field_name/
version (mandate §18's exact design) — swapping ciphertext between
rows/fields/entities changes the AAD, which AES-GCM authenticates as
part of the tag, so a mismatch fails decryption (see the regression
suite's T20-shaped case).

Fails CLOSED: every function here raises rather than ever returning
plaintext or a placeholder when a key is missing or verification fails
(mandate §20/§40). This is a property of these functions in isolation
— nothing in production calls them yet (see SECURITY_ARCHITECTURE.md
§21 for why wiring is a deliberate follow-up, not part of this
commit).

Rust-перенос (2026-09-24): rustlib/yandi_rs/src/crypto.rs — ядро AES-256-GCM / blind index / HKDF (RustCrypto, не самописное).
Формат блоба и AAD байт-в-байт те же; совместимость доказана в обе стороны с `cryptography`
(agent/crypto_rust_parity_test.py). По умолчанию ВЫКЛЮЧЕН; включается YANDI_CRYPTO_ENGINE=rust ПОСЛЕ сборки rustlib/yandi_rs.
Проверки ключа/длины/версии, исключения (KeyMissingError, ValueError, InvalidTag) и .decode остаются здесь, в Python.
Делегирует только для 32-байтового ключа типа bytes; иначе — прежний Python-путь.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Union

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.db.sql.keys import KeyMissingError

NONCE_SIZE = 12
CURRENT_VERSION = 1

_log = logging.getLogger("yandi.crypto")
_rust_cr = None          # None = ещё не пробовали; False = не запрошено/не собрано; модуль = подключён


def _get_rust_cr():
    global _rust_cr
    if _rust_cr is None:
        if os.environ.get("YANDI_CRYPTO_ENGINE") == "rust":
            try:
                import yandi_rs.crypto as _rs
                _rust_cr = _rs
                _log.warning("YANDI_CRYPTO_ENGINE=rust: используется Rust-реализация crypto (rustlib/yandi_rs)")
            except ImportError as e:
                _log.warning("YANDI_CRYPTO_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python", e)
                _rust_cr = False
        else:
            _rust_cr = False
    return _rust_cr or None


def _rust_key_ok(key) -> bool:
    return type(key) is bytes and len(key) == 32


def _rust_eid(entity_id):
    """entity_id в f-строке AAD: для str и int результат совпадает с str(); прочие типы — в Python-путь."""
    t = type(entity_id)
    if t is str:
        return entity_id
    if t is int:
        return str(entity_id)
    return None


def build_aad(entity_type: str, entity_id: Union[str, int], field_name: str, version: int = CURRENT_VERSION) -> bytes:
    """mandate §18's exact AAD design: schema|entity_type|entity_id|field_name|version."""
    return f"YANDI|{entity_type}|{entity_id}|{field_name}|v{version}".encode("utf-8")


def encrypt_field(
    key: bytes, plaintext: str, *, entity_type: str, entity_id: Union[str, int],
    field_name: str, version: int = CURRENT_VERSION,
) -> bytes:
    """Returns `version_byte || nonce(12) || ciphertext+tag` — a single
    opaque blob, safe to store directly in a `VARBINARY`/`BLOB` column.
    version travels WITH the ciphertext (not just as a side parameter)
    so a future decrypt call can reconstruct the exact AAD that was
    used at encryption time even if the CURRENT_VERSION default has
    since moved on (mandate §19: rotation must not require guessing
    which version produced an old blob)."""
    if not key:
        raise KeyMissingError("encrypt_field() called with no key — refusing to write plaintext.")
    if not 0 <= version <= 255:
        raise ValueError(f"version must fit in one byte (0-255), got {version}")

    rs = _get_rust_cr()
    if (rs is not None and _rust_key_ok(key) and type(plaintext) is str and type(entity_type) is str
            and type(field_name) is str and type(version) is int and _rust_eid(entity_id) is not None):
        blob = rs.encrypt_field(key, plaintext, entity_type, _rust_eid(entity_id), field_name, version)
        if blob is not None:
            return blob

    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_SIZE)
    aad = build_aad(entity_type, entity_id, field_name, version)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
    return bytes([version]) + nonce + ciphertext


def decrypt_field(key: bytes, blob: bytes, *, entity_type: str, entity_id: Union[str, int], field_name: str) -> str:
    """Raises (InvalidTag, from the `cryptography` library) on ANY
    tamper: a modified ciphertext byte, a modified tag, or an AAD that
    doesn't match the entity/field this blob is being read for (e.g. a
    ciphertext copied from a different row). Never returns partial or
    best-effort plaintext."""
    if not key:
        raise KeyMissingError("decrypt_field() called with no key.")
    if len(blob) < 1 + NONCE_SIZE:
        raise ValueError("ciphertext blob too short to contain a version byte + nonce")

    version = blob[0]
    nonce = blob[1:1 + NONCE_SIZE]
    ciphertext = blob[1 + NONCE_SIZE:]
    aad = build_aad(entity_type, entity_id, field_name, version)

    rs = _get_rust_cr()
    if (rs is not None and _rust_key_ok(key) and type(blob) is bytes and type(entity_type) is str
            and type(field_name) is str and _rust_eid(entity_id) is not None):
        raw = rs.decrypt_field(key, blob, entity_type, _rust_eid(entity_id), field_name)
        if raw is None:
            raise InvalidTag()
        return raw.decode("utf-8")

    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
    return plaintext.decode("utf-8")


def blind_index(index_key: bytes, namespace: str, normalized_value: str) -> str:
    """mandate §21: HMAC-SHA256(dedicated_index_key, domain_separator ||
    normalized_value) — deliberately NOT unkeyed SHA-256. An unkeyed
    hash of low-entropy human text (a question) is dictionary-
    attackable the moment a stolen, encrypted database is in an
    attacker's hands (they can hash every phrase in a dictionary and
    compare); a keyed HMAC with a key that itself lives outside SQL
    (derive_blind_index_key(), keys.py) is not.

    CONTENT_HASH != BLIND_INDEX (mandate §21's own explicit distinction,
    see SECURITY_ARCHITECTURE.md §13): this function is NOT a
    replacement for agent.claim_identity.canonicalize_claim_text()-based
    content_hash columns already used for cross-run epistemic identity
    — those stay exactly as they are. This is a NEW, separate concept
    for secret exact-match lookup against encrypted columns.

    `namespace` provides domain separation (mandate example:
    "question:v1:<text>" vs "resource:v1:<uri>") — the SAME normalized
    string in two different namespaces produces two different HMACs,
    so a blind index leaked/observed in one context can't be replayed
    to test membership in another."""
    if not index_key:
        raise KeyMissingError("blind_index() called with no index key.")
    rs = _get_rust_cr()
    if rs is not None and type(index_key) is bytes and type(namespace) is str and type(normalized_value) is str:
        return rs.blind_index(index_key, namespace, normalized_value)
    message = f"{namespace}:v1:{normalized_value}".encode("utf-8")
    return hmac.new(index_key, message, hashlib.sha256).hexdigest()
