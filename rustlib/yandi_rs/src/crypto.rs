//! Перенос agent/db/sql/crypto.py (AES-256-GCM поля + слепой индекс) и HKDF-производных ключей из agent/db/sql/keys.py.
//!
//! Формат блоба байт-в-байт тот же: `version(1) || nonce(12) || ciphertext+tag(16)`; AAD =
//! `YANDI|entity_type|entity_id|field_name|v{version}`; blind_index = hex(HMAC-SHA256(key, "{ns}:v1:{value}")).
//! Криптопримитивы — RustCrypto (aes-gcm, hmac, hkdf, sha2), НЕ самописные. Совместимость доказана в обе стороны
//! с Python `cryptography` (agent/crypto_rust_parity_test.py): Rust шифрует → Python расшифровывает и наоборот.
//!
//! Что остаётся в Python (намеренно): проверки «ключ пуст → KeyMissingError», «блоб короткий → ValueError»,
//! «версия 0..255», превращение ошибки тега в `InvalidTag`, `.decode("utf-8")` — чтобы типы исключений были
//! родными Python и не расходились. Rust отдаёт только чистое ядро. Ключи, отличные от 32 байт, идут в Python.

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use hkdf::Hkdf;
use hmac::{Hmac, Mac};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rand::RngCore;
use sha2::Sha256;

pub const NONCE_SIZE: usize = 12;

pub fn build_aad(entity_type: &str, entity_id: &str, field_name: &str, version: u8) -> Vec<u8> {
    format!("YANDI|{}|{}|{}|v{}", entity_type, entity_id, field_name, version).into_bytes()
}

/// Шифрует с заданным nonce (для известных ответов и внутреннего использования). None — ключ не 32 байта.
pub fn seal(key: &[u8], nonce: &[u8; NONCE_SIZE], plaintext: &[u8], aad: &[u8]) -> Option<Vec<u8>> {
    let cipher = Aes256Gcm::new_from_slice(key).ok()?;
    cipher.encrypt(Nonce::from_slice(nonce), Payload { msg: plaintext, aad }).ok()
}

/// Расшифровывает; None — любая порча (тег, AAD, ключ). Ядро без исключений.
pub fn open(key: &[u8], nonce: &[u8], ciphertext: &[u8], aad: &[u8]) -> Option<Vec<u8>> {
    if nonce.len() != NONCE_SIZE {
        return None;
    }
    let cipher = Aes256Gcm::new_from_slice(key).ok()?;
    cipher.decrypt(Nonce::from_slice(nonce), Payload { msg: ciphertext, aad }).ok()
}

pub fn encrypt_field_with_nonce(
    key: &[u8], nonce: &[u8; NONCE_SIZE], plaintext: &str, entity_type: &str, entity_id: &str, field_name: &str, version: u8,
) -> Option<Vec<u8>> {
    let aad = build_aad(entity_type, entity_id, field_name, version);
    let ct = seal(key, nonce, plaintext.as_bytes(), &aad)?;
    let mut out = Vec::with_capacity(1 + NONCE_SIZE + ct.len());
    out.push(version);
    out.extend_from_slice(nonce);
    out.extend_from_slice(&ct);
    Some(out)
}

pub fn encrypt_field(
    key: &[u8], plaintext: &str, entity_type: &str, entity_id: &str, field_name: &str, version: u8,
) -> Option<Vec<u8>> {
    let mut nonce = [0u8; NONCE_SIZE];
    rand::rngs::OsRng.fill_bytes(&mut nonce);
    encrypt_field_with_nonce(key, &nonce, plaintext, entity_type, entity_id, field_name, version)
}

/// blob ≥ 13 байт гарантирует вызывающий (Python-обёртка бросает ValueError раньше). None — провал аутентификации.
pub fn decrypt_field(key: &[u8], blob: &[u8], entity_type: &str, entity_id: &str, field_name: &str) -> Option<Vec<u8>> {
    if blob.len() < 1 + NONCE_SIZE {
        return None;
    }
    let version = blob[0];
    let aad = build_aad(entity_type, entity_id, field_name, version);
    open(key, &blob[1..1 + NONCE_SIZE], &blob[1 + NONCE_SIZE..], &aad)
}

/// keys.py wrap_dek: `nonce(12) || ciphertext+tag` под фиксированным AAD. None — ключ не 32 байта.
pub fn wrap(key: &[u8], data: &[u8], aad: &[u8]) -> Option<Vec<u8>> {
    let mut nonce = [0u8; NONCE_SIZE];
    rand::rngs::OsRng.fill_bytes(&mut nonce);
    let ct = seal(key, &nonce, data, aad)?;
    let mut out = nonce.to_vec();
    out.extend_from_slice(&ct);
    Some(out)
}

/// keys.py unwrap_dek. None — короткий блок/порча/неверный ключ.
pub fn unwrap(key: &[u8], wrapped: &[u8], aad: &[u8]) -> Option<Vec<u8>> {
    if wrapped.len() < NONCE_SIZE {
        return None;
    }
    open(key, &wrapped[..NONCE_SIZE], &wrapped[NONCE_SIZE..], aad)
}

pub fn blind_index(index_key: &[u8], namespace: &str, normalized_value: &str) -> String {
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(index_key).expect("HMAC принимает ключ любой длины");
    mac.update(format!("{}:v1:{}", namespace, normalized_value).as_bytes());
    let out = mac.finalize().into_bytes();
    let mut s = String::with_capacity(64);
    for b in out.iter() {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

/// llm_gateway/secure_store.py::_entry_hash: HMAC-SHA256(integrity_key, seq(8, big-endian) | op | name_index | content_hash | prev_hash) с разделителями «|».
pub fn entry_hash(integrity_key: &[u8], seq: u64, op: &str, name_index: &str, content_hash: &[u8], prev_hash: &[u8]) -> [u8; 32] {
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(integrity_key).expect("HMAC принимает ключ любой длины");
    mac.update(&seq.to_be_bytes());
    mac.update(b"|");
    mac.update(op.as_bytes());
    mac.update(b"|");
    mac.update(name_index.as_bytes());
    mac.update(b"|");
    mac.update(content_hash);
    mac.update(b"|");
    mac.update(prev_hash);
    mac.finalize().into_bytes().into()
}

/// HKDF-SHA256, salt=None (как `HKDF(algorithm=SHA256, length=n, salt=None, info=...)` в keys.py). None — length слишком велик.
pub fn hkdf_sha256(ikm: &[u8], info: &[u8], length: usize) -> Option<Vec<u8>> {
    let hk = Hkdf::<Sha256>::new(None, ikm);
    let mut okm = vec![0u8; length];
    hk.expand(info, &mut okm).ok()?;
    Some(okm)
}

fn b<'py>(py: Python<'py>, v: &[u8]) -> Bound<'py, PyBytes> {
    PyBytes::new_bound(py, v)
}

#[pyfunction]
#[pyo3(name = "build_aad")]
fn py_build_aad<'py>(py: Python<'py>, entity_type: &str, entity_id: &str, field_name: &str, version: u8) -> Bound<'py, PyBytes> {
    b(py, &build_aad(entity_type, entity_id, field_name, version))
}

#[pyfunction]
#[pyo3(name = "encrypt_field")]
fn py_encrypt_field<'py>(
    py: Python<'py>, key: &[u8], plaintext: &str, entity_type: &str, entity_id: &str, field_name: &str, version: u8,
) -> Option<Bound<'py, PyBytes>> {
    encrypt_field(key, plaintext, entity_type, entity_id, field_name, version).map(|v| b(py, &v))
}

/// Только для известных ответов в тестах: детерминированный nonce (12 байт).
#[pyfunction]
#[pyo3(name = "encrypt_field_with_nonce")]
fn py_encrypt_field_with_nonce<'py>(
    py: Python<'py>, key: &[u8], nonce: &[u8], plaintext: &str, entity_type: &str, entity_id: &str, field_name: &str, version: u8,
) -> Option<Bound<'py, PyBytes>> {
    let n: [u8; NONCE_SIZE] = nonce.try_into().ok()?;
    encrypt_field_with_nonce(key, &n, plaintext, entity_type, entity_id, field_name, version).map(|v| b(py, &v))
}

#[pyfunction]
#[pyo3(name = "decrypt_field")]
fn py_decrypt_field<'py>(
    py: Python<'py>, key: &[u8], blob: &[u8], entity_type: &str, entity_id: &str, field_name: &str,
) -> Option<Bound<'py, PyBytes>> {
    decrypt_field(key, blob, entity_type, entity_id, field_name).map(|v| b(py, &v))
}

#[pyfunction]
#[pyo3(name = "wrap")]
fn py_wrap<'py>(py: Python<'py>, key: &[u8], data: &[u8], aad: &[u8]) -> Option<Bound<'py, PyBytes>> {
    wrap(key, data, aad).map(|v| b(py, &v))
}

#[pyfunction]
#[pyo3(name = "unwrap")]
fn py_unwrap<'py>(py: Python<'py>, key: &[u8], wrapped: &[u8], aad: &[u8]) -> Option<Bound<'py, PyBytes>> {
    unwrap(key, wrapped, aad).map(|v| b(py, &v))
}

#[pyfunction]
#[pyo3(name = "blind_index")]
fn py_blind_index(index_key: &[u8], namespace: &str, normalized_value: &str) -> String {
    blind_index(index_key, namespace, normalized_value)
}

#[pyfunction]
#[pyo3(name = "entry_hash")]
fn py_entry_hash<'py>(
    py: Python<'py>, integrity_key: &[u8], seq: u64, op: &str, name_index: &str, content_hash: &[u8], prev_hash: &[u8],
) -> Bound<'py, PyBytes> {
    b(py, &entry_hash(integrity_key, seq, op, name_index, content_hash, prev_hash))
}

#[pyfunction]
#[pyo3(name = "hkdf_sha256")]
fn py_hkdf_sha256<'py>(py: Python<'py>, ikm: &[u8], info: &[u8], length: usize) -> Option<Bound<'py, PyBytes>> {
    hkdf_sha256(ikm, info, length).map(|v| b(py, &v))
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_build_aad, m)?)?;
    m.add_function(wrap_pyfunction!(py_encrypt_field, m)?)?;
    m.add_function(wrap_pyfunction!(py_encrypt_field_with_nonce, m)?)?;
    m.add_function(wrap_pyfunction!(py_decrypt_field, m)?)?;
    m.add_function(wrap_pyfunction!(py_wrap, m)?)?;
    m.add_function(wrap_pyfunction!(py_unwrap, m)?)?;
    m.add_function(wrap_pyfunction!(py_blind_index, m)?)?;
    m.add_function(wrap_pyfunction!(py_entry_hash, m)?)?;
    m.add_function(wrap_pyfunction!(py_hkdf_sha256, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: [u8; 32] = [7u8; 32];

    #[test]
    fn roundtrip() {
        let blob = encrypt_field(&KEY, "привет 🌍", "question", "42", "text", 1).unwrap();
        assert_eq!(blob[0], 1);
        assert_eq!(blob.len(), 1 + 12 + "привет 🌍".len() + 16);
        let pt = decrypt_field(&KEY, &blob, "question", "42", "text").unwrap();
        assert_eq!(pt, "привет 🌍".as_bytes());
    }

    #[test]
    fn aad_binds_and_tamper() {
        let blob = encrypt_field(&KEY, "x", "q", "1", "f", 1).unwrap();
        assert!(decrypt_field(&KEY, &blob, "q", "2", "f").is_none());
        assert!(decrypt_field(&KEY, &blob, "q", "1", "g").is_none());
        assert!(decrypt_field(&KEY, &blob, "r", "1", "f").is_none());
        for i in 0..blob.len() {
            let mut t = blob.clone();
            t[i] ^= 1;
            assert!(decrypt_field(&KEY, &t, "q", "1", "f").is_none(), "byte {}", i);
        }
        assert!(decrypt_field(&[8u8; 32], &blob, "q", "1", "f").is_none());
    }

    #[test]
    fn nonce_fresh() {
        let a = encrypt_field(&KEY, "x", "q", "1", "f", 1).unwrap();
        let b2 = encrypt_field(&KEY, "x", "q", "1", "f", 1).unwrap();
        assert_ne!(a[1..13], b2[1..13]);
        assert_ne!(a, b2);
    }

    #[test]
    fn wrap_roundtrip() {
        let w = wrap(&KEY, &[9u8; 32], b"aad").unwrap();
        assert_eq!(w.len(), 12 + 32 + 16);
        assert_eq!(unwrap(&KEY, &w, b"aad").unwrap(), vec![9u8; 32]);
        assert!(unwrap(&KEY, &w, b"aae").is_none());
        assert!(unwrap(&KEY, &w[..5], b"aad").is_none());
    }

    #[test]
    fn entry_hash_shape() {
        let h = entry_hash(b"k", 7, "insert", "ab", b"c", b"d");
        assert_eq!(h.len(), 32);
        assert_ne!(h, entry_hash(b"k", 8, "insert", "ab", b"c", b"d"));
        assert_ne!(h, entry_hash(b"k", 7, "insert", "ab", b"cd", b""));
    }

    #[test]
    fn bad_key_len() {
        assert!(encrypt_field(&[1u8; 16], "x", "q", "1", "f", 1).is_none());
    }

    #[test]
    fn hkdf_rfc5869_case3() {
        // RFC 5869 test case 3 (пустые salt и info): salt=None даёт нулевую соль — совпадает.
        let ikm = [0x0bu8; 22];
        let okm = hkdf_sha256(&ikm, b"", 42).unwrap();
        let hex: String = okm.iter().map(|x| format!("{:02x}", x)).collect();
        assert_eq!(hex, "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8");
    }

    #[test]
    fn blind_known() {
        // HMAC-SHA256(key="k", "ns:v1:v") — значение сверено с Python hmac в parity-тесте; здесь — форма.
        let h = blind_index(b"k", "ns", "v");
        assert_eq!(h.len(), 64);
        assert!(h.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
        assert_ne!(h, blind_index(b"k", "ns2", "v"));
    }
}
