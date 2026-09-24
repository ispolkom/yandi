//! Общие «питоновские» текстовые примитивы для ВСЕХ перенесённых модулей — там, где стандартные
//! средства Rust ведут себя иначе, чем Python, и наивный перенос был бы тихо неверен. Это не
//! подмодуль Python, а внутренняя подпорка (как py_word_table.rs).
//!
//! 1. Пробелы. Python `str.strip()/.split()` и `\s` в `re` для str опираются на `str.isspace()`:
//!    29 символов, ВКЛЮЧАЯ разделители U+001C..U+001F. Rust `trim()/split_whitespace()` и `\s` крейта
//!    `regex` используют Unicode White_Space (25 символов) — без U+001C..U+001F. Найдено на живом
//!    коде: `canonicalize_claim_text("a\x1cb")` давал разные ответы (срез 3). Здесь: `is_py_space`,
//!    `py_strip`, `py_split_whitespace`, и `py_regex`, который переписывает `\s` в паттерне на
//!    `[\s\x1c-\x1f]` (внутри класса — на `\s\x1c-\x1f`).
//! 2. `float(str)`. Python: снимает по краям ТОЛЬКО Unicode White_Space (НЕ 29 символов str.isspace:
//!    `float("\x1c1")` — ValueError; проверено перебором всех кодовых точек, поэтому здесь обычный
//!    Rust `trim()`, а не py_strip), переводит ЛЮБЫЕ десятичные цифры Unicode в ASCII (таблица
//!    py_decimal_table.rs, из самого Python), допускает одиночные `_` между цифрами (PEP 515).
//!    Rust `parse::<f64>` — только ASCII, без `_`. Здесь: `py_float`.
//!

use crate::py_case_table::{PY_LOWER_RANGES, PY_TITLE_RANGES, PY_UPPER_RANGES};
use crate::py_decimal_table::PY_DECIMAL_ZEROS;
use crate::py_casefold_table::PY_CASEFOLD_MAP;
use crate::py_unassigned_table::PY_UNASSIGNED;
use unicode_normalization::UnicodeNormalization;
use crate::py_lower_table::{PY_LOWER_MAP, PY_SIGMA_CASED, PY_SIGMA_IGNORABLE};
use crate::py_printable_table::PY_NONPRINTABLE_RANGES;
#[cfg(feature = "python")]
use pyo3::prelude::*;
use regex::Regex;

/// Python `str.isspace()` для одного символа (== `\s` в `re` для str).
pub fn is_py_space(c: char) -> bool {
    matches!(
        c,
        '\u{9}'..='\u{d}'
            | '\u{1c}'..='\u{20}'
            | '\u{85}'
            | '\u{a0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200a}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202f}'
            | '\u{205f}'
            | '\u{3000}'
    )
}

/// Python `s.strip()`
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(is_py_space)
}

/// Python `s.split()` (без аргументов): куски между пробельными символами, без пустых.
pub fn py_split_whitespace(s: &str) -> impl Iterator<Item = &str> {
    s.split(is_py_space).filter(|p| !p.is_empty())
}

fn in_ranges_u32(table: &[(u32, u32)], c: char) -> bool {
    let cp = c as u32;
    table
        .binary_search_by(|&(lo, hi)| {
            if cp < lo {
                std::cmp::Ordering::Greater
            } else if cp > hi {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// CPython `handle_capital_sigma`: «ς» (финальная) или «σ» — по соседям с пропуском case-ignorable символов.
fn is_final_sigma(chars: &[char], i: usize) -> bool {
    let mut j = i;
    let mut prev_cased = false;
    while j > 0 {
        j -= 1;
        if in_ranges_u32(&PY_SIGMA_IGNORABLE, chars[j]) {
            continue;
        }
        prev_cased = in_ranges_u32(&PY_SIGMA_CASED, chars[j]);
        break;
    }
    if !prev_cased {
        return false;
    }
    let mut k = i + 1;
    while k < chars.len() {
        if in_ranges_u32(&PY_SIGMA_IGNORABLE, chars[k]) {
            k += 1;
            continue;
        }
        return !in_ranges_u32(&PY_SIGMA_CASED, chars[k]);
    }
    true
}

/// Python `str.lower()` побитово: одиночные отображения — таблица самого Python (Unicode 14), финальная сигма — правило CPython
/// с классами символов, снятыми у Python. `str::to_lowercase` в Rust знает более новый Unicode и понижает символы (U+A7CB, U+A7D2…),
/// которых Python 3.11 не понижает, — а это меняет `\w`-соседство.
pub fn py_lower(s: &str) -> String {
    if s.is_ascii() {
        return s.to_ascii_lowercase();
    }
    let chars: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len());
    for (i, &c) in chars.iter().enumerate() {
        if c == '\u{3A3}' {
            out.push(if is_final_sigma(&chars, i) { '\u{3C2}' } else { '\u{3C3}' });
            continue;
        }
        if c.is_ascii() {
            out.push(c.to_ascii_lowercase());
            continue;
        }
        let cp = c as u32;
        match PY_LOWER_MAP.binary_search_by_key(&cp, |&(k, _)| k) {
            Ok(idx) => out.push_str(PY_LOWER_MAP[idx].1),
            Err(_) => out.push(c),
        }
    }
    out
}

/// Python `str.casefold()` побитово (таблица из самого Python; свёртка контекстно-независима).
pub fn py_casefold(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if c.is_ascii() {
            out.push(c.to_ascii_lowercase());
            continue;
        }
        let cp = c as u32;
        match PY_CASEFOLD_MAP.binary_search_by_key(&cp, |&(k, _)| k) {
            Ok(idx) => out.push_str(PY_CASEFOLD_MAP[idx].1),
            Err(_) => out.push(c),
        }
    }
    out
}

/// Кодовая точка не назначена в Unicode самого Python (категория Cn).
pub fn is_py_unassigned(c: char) -> bool {
    in_ranges_u32(&PY_UNASSIGNED, c)
}

fn normalize_pieces(s: &str, f: impl Fn(&str) -> String) -> String {
    let mut out = String::with_capacity(s.len());
    let mut piece = String::new();
    for c in s.chars() {
        if is_py_unassigned(c) {
            if !piece.is_empty() {
                out.push_str(&f(&piece));
                piece.clear();
            }
            out.push(c);
        } else {
            piece.push(c);
        }
    }
    if !piece.is_empty() {
        out.push_str(&f(&piece));
    }
    out
}

/// `unicodedata.normalize("NFC", s)` как в Python 3.11 (Unicode 14): символы, которых Python не знает, — барьеры (см. gen_py_unassigned_table.py).
pub fn py_nfc(s: &str) -> String {
    if s.is_ascii() {
        return s.to_string();
    }
    normalize_pieces(s, |p| p.nfc().collect())
}

/// `unicodedata.normalize("NFKC", s)` — то же для NFKC.
pub fn py_nfkc(s: &str) -> String {
    if s.is_ascii() {
        return s.to_string();
    }
    normalize_pieces(s, |p| p.nfkc().collect())
}

/// `.py_lowercase()` — drop-in для `.to_lowercase()` с семантикой Python `str.lower()`.
pub trait PyLowerExt {
    fn py_lowercase(&self) -> String;
}

impl PyLowerExt for str {
    fn py_lowercase(&self) -> String {
        py_lower(self)
    }
}

/// Regex с точной питоновской семантикой (`\s`, `\w`, `\b`, `$`, IGNORECASE) — см. py_regex.rs.
/// ТОЛЬКО для проверок «есть ли совпадение» (is_match/search) рядом с `\b`.
pub fn py_regex(pattern: &str) -> Regex {
    crate::py_regex::compile(pattern)
}

pub(crate) fn py_decimal_value(c: char) -> Option<u32> {
    let cp = c as u32;
    // серии по 10 подряд; таблица отсортирована — ищем последний ноль <= cp
    let idx = PY_DECIMAL_ZEROS.partition_point(|&z| z <= cp);
    if idx == 0 {
        return None;
    }
    let d = cp - PY_DECIMAL_ZEROS[idx - 1];
    if d <= 9 {
        Some(d)
    } else {
        None
    }
}

fn in_ranges(table: &[(u32, u32)], c: char) -> bool {
    let cp = c as u32;
    table
        .binary_search_by(|&(lo, hi)| {
            if cp < lo {
                std::cmp::Ordering::Greater
            } else if cp > hi {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// Python `str.isupper()`: ложь при любом Lowercase/Titlecase символе; истина, если есть хоть один
/// Uppercase (наборы — из самого Python, см. gen_py_case_table.py).
pub fn py_isupper(s: &str) -> bool {
    let mut cased = false;
    for c in s.chars() {
        if in_ranges(&PY_LOWER_RANGES, c) || in_ranges(&PY_TITLE_RANGES, c) {
            return false;
        }
        if !cased && in_ranges(&PY_UPPER_RANGES, c) {
            cased = true;
        }
    }
    cased
}

fn is_py_printable(c: char) -> bool {
    let cp = c as u32;
    if cp < 0x80 {
        return (0x20..0x7f).contains(&cp);
    }
    PY_NONPRINTABLE_RANGES
        .binary_search_by(|&(lo, hi)| {
            if cp < lo {
                std::cmp::Ordering::Greater
            } else if cp > hi {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_err()
}

/// Python `repr(str)`: одинарные кавычки, кроме случая "есть ' и нет \"" (тогда двойные);
/// экранирование \\, кавычки, \t \n \r, непечатаемых как \xNN / \uNNNN / \UNNNNNNNN.
pub fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        if c == quote || c == '\\' {
            out.push('\\');
            out.push(c);
        } else if c == '\t' {
            out.push_str("\\t");
        } else if c == '\n' {
            out.push_str("\\n");
        } else if c == '\r' {
            out.push_str("\\r");
        } else if is_py_printable(c) {
            out.push(c);
        } else {
            let cp = c as u32;
            if cp <= 0xff {
                out.push_str(&format!("\\x{cp:02x}"));
            } else if cp <= 0xffff {
                out.push_str(&format!("\\u{cp:04x}"));
            } else {
                out.push_str(&format!("\\U{cp:08x}"));
            }
        }
    }
    out.push(quote);
    out
}

/// Python `float(s)` для строки (None вместо ValueError).
pub fn py_float(s: &str) -> Option<f64> {
    // НЕ py_strip: float() использует Unicode White_Space (без U+001C..U+001F) — см. заметку 2 вверху.
    let stripped = s.trim();
    let ascii: Vec<char> = stripped
        .chars()
        .map(|c| match py_decimal_value(c) {
            Some(d) => char::from_digit(d, 10).unwrap(),
            None => c,
        })
        .collect();
    // PEP 515: '_' допустим только между двумя цифрами
    for (i, &c) in ascii.iter().enumerate() {
        if c == '_' {
            let ok = i > 0 && i + 1 < ascii.len() && ascii[i - 1].is_ascii_digit() && ascii[i + 1].is_ascii_digit();
            if !ok {
                return None;
            }
        }
    }
    let cleaned: String = ascii.into_iter().filter(|&c| c != '_').collect();
    cleaned.parse::<f64>().ok()
}

// ── PyO3-обвязка: только чтобы parity-тест мог сверить подпорку с настоящим Python ──

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "is_space")]
fn py_is_space(codepoint: u32) -> bool {
    char::from_u32(codepoint).map(is_py_space).unwrap_or(false)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "strip")]
fn py_py_strip(s: &str) -> String {
    py_strip(s).to_string()
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "split")]
fn py_py_split(s: &str) -> Vec<String> {
    py_split_whitespace(s).map(|p| p.to_string()).collect()
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "isupper")]
fn py_py_isupper(s: &str) -> bool {
    py_isupper(s)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "repr_str")]
fn py_py_repr_str(s: &str) -> String {
    py_repr_str(s)
}

#[cfg(feature = "python")]
fn json_to_py(py: Python<'_>, v: &crate::py_json::PyJson) -> PyResult<PyObject> {
    use crate::py_json::PyJson;
    Ok(match v {
        PyJson::Null => py.None(),
        PyJson::Bool(b) => b.into_py(py),
        PyJson::Int { zero, as_f64 } => (if *zero { 0.0 } else { *as_f64 }).into_py(py), // только для проверки: int как float
        PyJson::Float(f) => f.into_py(py),
        PyJson::Str(s) => s.into_py(py),
        PyJson::List(items) => {
            let l = pyo3::types::PyList::empty_bound(py);
            for it in items {
                l.append(json_to_py(py, it)?)?;
            }
            l.into_py(py)
        }
        PyJson::Dict(items) => {
            let d = pyo3::types::PyDict::new_bound(py);
            for (k, it) in items {
                d.set_item(k, json_to_py(py, it)?)?;
            }
            d.into_py(py)
        }
    })
}

#[cfg(feature = "python")]
/// Только для проверки: json.loads-порт -> (True, объект[целые как float]) | (False, текст ошибки) |
/// (None, "recursion").
#[pyfunction]
#[pyo3(name = "json_loads")]
fn py_json_loads(py: Python<'_>, s: &str) -> PyResult<(Option<bool>, PyObject)> {
    match crate::py_json::loads(s) {
        Ok(v) => Ok((Some(true), json_to_py(py, &v)?)),
        Err(crate::py_json::LoadsError::Decode(m)) => Ok((Some(false), m.into_py(py))),
        Err(crate::py_json::LoadsError::Escalate(_)) => Ok((None, "recursion".into_py(py))),
    }
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "lower")]
fn py_py_lower(s: &str) -> String {
    py_lower(s)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "casefold")]
fn py_py_casefold(s: &str) -> String {
    py_casefold(s)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "nfc")]
fn py_py_nfc(s: &str) -> String {
    py_nfc(s)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "nfkc")]
fn py_py_nfkc(s: &str) -> String {
    py_nfkc(s)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "float")]
fn py_py_float(s: &str) -> Option<f64> {
    py_float(s)
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_is_space, m)?)?;
    m.add_function(wrap_pyfunction!(py_py_strip, m)?)?;
    m.add_function(wrap_pyfunction!(py_py_split, m)?)?;
    m.add_function(wrap_pyfunction!(py_py_float, m)?)?;
    m.add_function(wrap_pyfunction!(py_py_lower, m)?)?;
    m.add_function(wrap_pyfunction!(py_py_nfc, m)?)?;
    m.add_function(wrap_pyfunction!(py_py_nfkc, m)?)?;
    m.add_function(wrap_pyfunction!(py_py_casefold, m)?)?;
    m.add_function(wrap_pyfunction!(py_py_repr_str, m)?)?;
    m.add_function(wrap_pyfunction!(py_py_isupper, m)?)?;
    m.add_function(wrap_pyfunction!(py_json_loads, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn space_set_is_pythons() {
        for c in ['\u{1c}', '\u{1d}', '\u{1e}', '\u{1f}', ' ', '\t', '\u{a0}', '\u{3000}', '\u{85}'] {
            assert!(is_py_space(c), "{c:?}");
        }
        for c in ['a', '_', '\u{200b}', '\u{feff}', '\u{180e}'] {
            assert!(!is_py_space(c), "{c:?}");
        }
    }

    #[test]
    fn strip_and_split() {
        assert_eq!(py_strip("\u{1f} a b \u{1c}"), "a b");
        assert_eq!(py_split_whitespace("a\u{1c}b  c\u{3000}d").collect::<Vec<_>>(), vec!["a", "b", "c", "d"]);
    }

    #[test]
    fn repr_matches_python() {
        assert_eq!(py_repr_str("abc"), "'abc'");
        assert_eq!(py_repr_str("it's"), "\"it's\"");
        assert_eq!(py_repr_str("it's \"x\""), "'it\\'s \"x\"'");
        assert_eq!(py_repr_str("a\nb\t\\"), "'a\\nb\\t\\\\'");
        assert_eq!(py_repr_str("\u{1}\u{7f}\u{a0}"), "'\\x01\\x7f\\xa0'");
        assert_eq!(py_repr_str("привет"), "'привет'");
        assert_eq!(py_repr_str("\u{200b}"), "'\\u200b'");
    }

    #[test]
    fn float_matches_python_rules() {
        assert_eq!(py_float(" 0.5 "), Some(0.5));
        assert_eq!(py_float("1_000"), Some(1000.0));
        assert_eq!(py_float("1__0"), None);
        assert_eq!(py_float("_1"), None);
        assert_eq!(py_float("1_"), None);
        assert_eq!(py_float("\u{661}\u{662}\u{663}"), Some(123.0)); // арабо-индийские
        assert_eq!(py_float("\u{1c}0.25\u{1f}"), None); // float() НЕ считает U+001C..1F пробелами
        assert_eq!(py_float("\u{2003} 0.25 \u{a0}"), Some(0.25));
        assert_eq!(py_float(""), None);
        assert_eq!(py_float("abc"), None);
        assert!(py_float("nan").unwrap().is_nan());
        assert_eq!(py_float("-inf"), Some(f64::NEG_INFINITY));
    }
}
