//! Перенос чистых помощников pet/core_lifecycle.py (срез 33, 2026-09-24) — граница «узел ⇄ ядро» (контракт docs/NODE_CORE_CONTRACT.md):
//!   * `decode_key`  — разбор ключа разблокировки: `^[A-Za-z0-9+/]{43}=$` + `base64.b64decode(validate=True)` == 32 байта. ТОНКОСТИ оригинала:
//!     `$` в `re` совпадает и перед ЗАВЕРШАЮЩИМ `\n` (тогда `b64decode(validate=True)` отвергает `\n` → None); неканонические «лишние» биты
//!     последнего символа Python ПРИНИМАЕТ (крейт base64 по умолчанию — нет), поэтому декодирование ручное;
//!   * `id_match`    — три регулярки идентификаторов (`_REQUEST_ID_RE`, `_IDEM_RE`, `_SECRET_RE`) с тем же правилом `$`;
//!   * `validate`    — `_validate(endpoint, doc)`: схема запросов unlock/lock/shutdown (строгие «родные» типы JSON, остальное → откат на Python);
//!   * `check_key`   — HKDF-SHA256(salt=None, info="yandi/core/v1/check-value").
//! Файлы, права, AES-GCM проверочного значения и сама жизненная логика остаются в Python.

#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::PyBytes;

use crate::pet_extraction::{classify, small_int, V};

/// `re` `^...$`: допускает ОДИН завершающий "\n".
fn strip_one_newline(s: &str) -> &str {
    s.strip_suffix('\n').unwrap_or(s)
}

fn b64_val(c: u8) -> Option<u8> {
    match c {
        b'A'..=b'Z' => Some(c - b'A'),
        b'a'..=b'z' => Some(c - b'a' + 26),
        b'0'..=b'9' => Some(c - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

pub fn decode_key(text: &str) -> Option<Vec<u8>> {
    let t = strip_one_newline(text);
    let b = t.as_bytes();
    // регулярка: 43 символа алфавита + '='
    if b.len() != 44 || b[43] != b'=' || !b[..43].iter().all(|&c| b64_val(c).is_some()) {
        return None;
    }
    // b64decode(validate=True) не принимает завершающий "\n" (он не в алфавите)
    if t.len() != text.len() {
        return None;
    }
    let mut out = Vec::with_capacity(33);
    let mut acc: u32 = 0;
    let mut bits = 0;
    for &c in &b[..43] {
        acc = (acc << 6) | b64_val(c).unwrap() as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((acc >> bits) & 0xFF) as u8);
        }
    }
    // 43 символа = 258 бит → 32 полных байта + 2 лишних бита (Python их не проверяет)
    if out.len() == 32 {
        Some(out)
    } else {
        None
    }
}

/// kind: 0 = request id `[A-Za-z0-9_-]{1,64}`, 1 = idempotency key `[A-Za-z0-9_-]{8,64}`, 2 = launch secret `[0-9a-f]{64}`.
pub fn id_match(kind: u8, s: &str) -> bool {
    let t = strip_one_newline(s);
    let n = t.chars().count();
    match kind {
        0 => (1..=64).contains(&n) && t.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'),
        1 => (8..=64).contains(&n) && t.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'),
        _ => n == 64 && t.chars().all(|c| matches!(c, '0'..='9' | 'a'..='f')),
    }
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "decode_key")]
fn py_decode_key<'py>(py: Python<'py>, text: &str) -> Option<Bound<'py, PyBytes>> {
    decode_key(text).map(|v| PyBytes::new_bound(py, &v))
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "id_match")]
fn py_id_match(kind: u8, s: &str) -> bool {
    id_match(kind, s)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "check_key")]
fn py_check_key<'py>(py: Python<'py>, derived: &[u8], info: &[u8]) -> Option<Bound<'py, PyBytes>> {
    crate::crypto::hkdf_sha256(derived, info, 32).map(|v| PyBytes::new_bound(py, &v))
}

#[cfg(feature = "python")]
/// `_validate`: кортеж `(текст ошибки | None,)`; сам None — посторонний тип, выполнить Python.
#[pyfunction]
#[pyo3(name = "validate")]
fn py_validate(
    py: Python<'_>, endpoint: &str, doc: &Bound<'_, PyAny>, unlock_context: &str,
) -> PyResult<Option<PyObject>> {
    let d = match classify(doc)? {
        Some(V::Dict(d)) => d,
        _ => return Ok(None),
    };
    let allowed: &[&str] = match endpoint {
        "unlock" => &["key", "context", "extensions"],
        "lock" => &["extensions"],
        "shutdown" => &["deadline_ms", "extensions"],
        _ => return Ok(None), // Python бросил бы KeyError
    };
    let err = |m: &str| -> PyResult<Option<PyObject>> { Ok(Some((Some(m.to_string()),).into_py(py))) };
    for (k, _) in d.iter() {
        match classify(&k)? {
            Some(V::Str(s)) => {
                if !allowed.contains(&s.as_str()) {
                    return err("The request contains a field that is not allowed.");
                }
            }
            _ => return Ok(None), // не-строковый ключ — откат
        }
    }
    if let Some(ext) = d.get_item("extensions")? {
        match classify(&ext)? {
            None => return Ok(None),
            Some(V::Dict(_)) => {}
            Some(_) => return err("The field 'extensions' must be an object."),
        }
    }
    if endpoint == "unlock" {
        let key = d.get_item("key")?;
        let context = d.get_item("context")?;
        let mut key_s: Option<String> = None;
        let mut ctx_s: Option<String> = None;
        for (slot, v) in [(&mut key_s, &key), (&mut ctx_s, &context)] {
            if let Some(o) = v {
                match classify(o)? {
                    None => return Ok(None),
                    Some(V::Str(s)) => *slot = Some(s),
                    Some(_) => {}
                }
            }
        }
        let (key_s, ctx_s) = match (key_s, ctx_s) {
            (Some(k), Some(c)) => (k, c),
            _ => return err("The fields 'key' and 'context' are required."),
        };
        if ctx_s != unlock_context {
            return err("The context is not supported.");
        }
        if decode_key(&key_s).is_none() {
            return err("The key is not valid.");
        }
    }
    if endpoint == "shutdown" {
        if let Some(dl) = d.get_item("deadline_ms")? {
            match classify(&dl)? {
                None => return Ok(None),
                Some(V::Int(i)) => {
                    let ok = match small_int(&i) {
                        Some(v) => v >= 1,
                        None => i.gt(0)?, // огромное: положительное — годно
                    };
                    if !ok {
                        return err("The deadline must be a positive integer.");
                    }
                }
                Some(_) => return err("The deadline must be a positive integer."),
            }
        }
    }
    Ok(Some((Option::<String>::None,).into_py(py)))
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_decode_key, m)?)?;
    m.add_function(wrap_pyfunction!(py_id_match, m)?)?;
    m.add_function(wrap_pyfunction!(py_check_key, m)?)?;
    m.add_function(wrap_pyfunction!(py_validate, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key() {
        let k = "A".repeat(43) + "=";
        assert_eq!(decode_key(&k), Some(vec![0u8; 32]));
        assert!(decode_key(&(k.clone() + "\n")).is_none());
        assert!(decode_key(&"A".repeat(44)).is_none());
        assert!(decode_key(&("A".repeat(42) + "=")).is_none());
        // неканонические лишние биты: последний символ 'B' (000001) — Python принимает
        assert!(decode_key(&("A".repeat(42) + "B=")).is_some());
    }

    #[test]
    fn ids() {
        assert!(id_match(0, "abc_-9"));
        assert!(id_match(0, "abc\n"));
        assert!(!id_match(0, "abc\n\n"));
        assert!(!id_match(0, ""));
        assert!(!id_match(1, "short"));
        assert!(id_match(2, &"a".repeat(64)));
        assert!(!id_match(2, &"A".repeat(64)));
    }
}
