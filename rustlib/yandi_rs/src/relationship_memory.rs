//! Перенос стеммера agent/relationship_memory.py (срез 32, 2026-09-24): `_stem`, `_stems` (и через него `content_stems`) —
//! нормализация «содержательных» слов для сопоставления обиды и извинения; вызывается на каждое извинение/обещание/личный факт.
//! `_normalize` = `str(text or "").casefold().replace("ё", "е")` — свёртка по таблице самого Python (`py_text::py_casefold`);
//! токены — `[a-zа-я0-9]+` (ё уже заменена); стем — первый подходящий суффикс из УПОРЯДОЧЕННОГО списка при остатке >= 3 СИМВОЛОВ
//! (не байт!); результат — множество (Python set). Таблицы сгенерированы ИЗ Python (`gen_relationship_memory_data.py`).

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use std::collections::HashSet;

use crate::py_text::py_casefold;
use crate::relationship_memory_data::{NON_CONTENT_STEMS, STEM_SUFFIXES};

static NON_CONTENT: Lazy<HashSet<&'static str>> = Lazy::new(|| NON_CONTENT_STEMS.iter().copied().collect());

fn in_token_class(c: char) -> bool {
    c.is_ascii_lowercase() || c.is_ascii_digit() || ('а'..='я').contains(&c)
}

/// `_stem`
pub fn stem(token: &str) -> String {
    let tlen = token.chars().count();
    for suffix in STEM_SUFFIXES.iter() {
        if token.ends_with(suffix) {
            let slen = suffix.chars().count();
            if tlen - slen >= 3 {
                let keep: String = token.chars().take(tlen - slen).collect();
                return keep;
            }
        }
    }
    token.to_string()
}

/// `_stems`: множество стемов длиной >= 3 символа.
pub fn stems(text: &str, drop_non_content: bool) -> HashSet<String> {
    let norm = py_casefold(text).replace('ё', "е");
    let mut out: HashSet<String> = HashSet::new();
    let mut cur = String::new();
    let mut flush = |cur: &mut String, out: &mut HashSet<String>| {
        if !cur.is_empty() {
            out.insert(stem(cur));
            cur.clear();
        }
    };
    for c in norm.chars() {
        if in_token_class(c) {
            cur.push(c);
        } else {
            flush(&mut cur, &mut out);
        }
    }
    flush(&mut cur, &mut out);
    if drop_non_content {
        out.retain(|s| !NON_CONTENT.contains(s.as_str()));
    }
    out.retain(|s| s.chars().count() >= 3);
    out
}

#[pyfunction]
#[pyo3(name = "stem")]
fn py_stem(token: &str) -> String {
    stem(token)
}

#[pyfunction]
#[pyo3(name = "stems")]
fn py_stems(text: &str, drop_non_content: bool) -> HashSet<String> {
    stems(text, drop_non_content)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_stem, m)?)?;
    m.add_function(wrap_pyfunction!(py_stems, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stemming() {
        assert_eq!(stem("книгами"), "книг");
        assert_eq!(stem("он"), "он");
        assert_eq!(stem("окна"), "окн"); // остаток ровно 3 символа
        assert_eq!(stem("ума"), "ума"); // остаток 2 — не режем
    }

    #[test]
    fn set() {
        let s = stems("Извини, я назвал тебя ДУРАКОМ!", true);
        assert!(s.contains("дурак"));
        assert!(!s.contains("извин"));
    }
}
