//! Перенос лексического ядра agent/final_claim_coverage.py — эвристики маршрутизации пар «финальное утверждение ↔
//! утверждение пайплайна» на NLI: `_content_words`, `_lexical_overlap`, `_has_negation`, `_shares_number`,
//! `_is_near_duplicate`, `_mandatory_routing_reason` и матрица `mandatory_matrix` для `_route_candidate_pairs`
//! (O(F×P): слова/числа/отрицания считаются ОДИН раз на утверждение, а не на каждую пару). Эмбеддинги/numpy, вызовы
//! LLM/NLI и `_extract_json` остаются в Python. Стоп-слова сгенерированы ИЗ Python (fcc_data.rs).
//!
//! Fidelity:
//! * Слова: `re.findall(r"[а-яёa-z0-9]+", text.lower())` — ASCII a-z/0-9, кириллица а-я и ё (НЕ Unicode-цифры и НЕ
//!   другие буквы); `len(w) >= 4` — в СИМВОЛАХ; стоп-слова из Python; Жаккар = |∩| / |∪| (float).
//! * Отрицание: `\bне{1,2}[а-яё]*\b|\bнет\b|\bотсутств|\bникак|\bни\s+один` c IGNORECASE по `text.lower()` — py_regex
//!   (точные `\b`, `\s`, IGNORECASE) для is_match; числа: `\d+(?:[.,]\d+)?` findall -> множества (`\d` Python).
//! * Порядок правил `_mandatory_routing_reason` и падение к CORE+CORE после условия перекрытия — как в оригинале.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_FINAL_CLAIM_COVERAGE_ENGINE=rust (см. agent/final_claim_coverage.py).

use crate::py_text::PyLowerExt;
use crate::fcc_data::STOPWORDS;
use crate::py_text::py_strip;
use once_cell::sync::Lazy;
#[cfg(feature = "python")]
use pyo3::prelude::*;
use regex::Regex;
use std::collections::HashSet;

static WORD_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"[а-яёa-z0-9]+").expect("паттерн валиден"));
static NEGATION: Lazy<Regex> = Lazy::new(|| crate::py_text::py_regex(r"(?i)\bне{1,2}[а-яё]*\b|\bнет\b|\bотсутств|\bникак|\bни\s+один"));
static NUMBER: Lazy<Regex> = Lazy::new(|| crate::py_text::py_regex(r"\d+(?:[.,]\d+)?"));

const MANDATORY_OVERLAP_FLOOR: f64 = 0.15;

/// `_content_words`
pub fn content_words(text: &str) -> HashSet<String> {
    let lower = text.py_lowercase();
    WORD_RE
        .find_iter(&lower)
        .map(|m| m.as_str())
        .filter(|w| w.chars().count() >= 4 && !STOPWORDS.contains(w))
        .map(|w| w.to_string())
        .collect()
}

fn jaccard(a: &HashSet<String>, b: &HashSet<String>) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    let inter = a.intersection(b).count();
    let union = a.len() + b.len() - inter;
    inter as f64 / union as f64
}

/// `_lexical_overlap`
pub fn lexical_overlap(a: &str, b: &str) -> f64 {
    jaccard(&content_words(a), &content_words(b))
}

/// `_has_negation`
pub fn has_negation(text: &str) -> bool {
    NEGATION.is_match(&text.py_lowercase())
}

fn numbers(text: &str) -> HashSet<String> {
    NUMBER.find_iter(text).map(|m| m.as_str().to_string()).collect()
}

/// `_shares_number`
pub fn shares_number(a: &str, b: &str) -> bool {
    !numbers(a).is_disjoint(&numbers(b))
}

fn norm(t: &str) -> String {
    py_strip(t).py_lowercase()
}

/// `_is_near_duplicate`
pub fn is_near_duplicate(a: &str, b: &str) -> bool {
    norm(a) == norm(b) || lexical_overlap(a, b) >= 0.8
}

struct Prep {
    norm: String,
    words: HashSet<String>,
    neg: bool,
    nums: HashSet<String>,
}

fn prep(t: &str) -> Prep {
    Prep { norm: norm(t), words: content_words(t), neg: has_negation(t), nums: numbers(t) }
}

fn reason(a: &Prep, b: &Prep, arole: Option<&str>, brole: Option<&str>) -> Option<&'static str> {
    let overlap = jaccard(&a.words, &b.words);
    if a.norm == b.norm || overlap >= 0.8 {
        return Some("exact_or_near_duplicate");
    }
    if overlap >= MANDATORY_OVERLAP_FLOOR {
        if a.neg || b.neg {
            return Some("negation_plus_overlap");
        }
        if !a.nums.is_disjoint(&b.nums) {
            return Some("shared_number_plus_overlap");
        }
    }
    if arole == Some("CORE") && brole == Some("CORE") {
        return Some("core_plus_core");
    }
    None
}

/// `_mandatory_routing_reason`
pub fn mandatory_routing_reason(a: &str, b: &str, arole: Option<&str>, brole: Option<&str>) -> Option<&'static str> {
    reason(&prep(a), &prep(b), arole, brole)
}

/// Матрица причин для всех пар (F×P): каждая строка — по финальному утверждению.
pub fn mandatory_matrix(finals: &[String], pipes: &[String], froles: &[Option<String>], proles: &[Option<String>]) -> Vec<Vec<Option<&'static str>>> {
    let fp: Vec<Prep> = finals.iter().map(|t| prep(t)).collect();
    let pp: Vec<Prep> = pipes.iter().map(|t| prep(t)).collect();
    fp.iter()
        .enumerate()
        .map(|(i, a)| {
            pp.iter()
                .enumerate()
                .map(|(j, b)| reason(a, b, froles.get(i).and_then(|r| r.as_deref()), proles.get(j).and_then(|r| r.as_deref())))
                .collect()
        })
        .collect()
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "content_words")]
fn py_content_words(text: &str) -> Vec<String> {
    let mut v: Vec<String> = content_words(text).into_iter().collect();
    v.sort();
    v
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "lexical_overlap")]
fn py_lexical_overlap(a: &str, b: &str) -> f64 {
    lexical_overlap(a, b)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "has_negation")]
fn py_has_negation(text: &str) -> bool {
    has_negation(text)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "shares_number")]
fn py_shares_number(a: &str, b: &str) -> bool {
    shares_number(a, b)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "is_near_duplicate")]
fn py_is_near_duplicate(a: &str, b: &str) -> bool {
    is_near_duplicate(a, b)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "mandatory_routing_reason", signature = (a, b, arole=None, brole=None))]
fn py_reason(a: &str, b: &str, arole: Option<&str>, brole: Option<&str>) -> Option<&'static str> {
    mandatory_routing_reason(a, b, arole, brole)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "mandatory_matrix")]
fn py_matrix(finals: Vec<String>, pipes: Vec<String>, froles: Vec<Option<String>>, proles: Vec<Option<String>>) -> Vec<Vec<Option<&'static str>>> {
    mandatory_matrix(&finals, &pipes, &froles, &proles)
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_content_words, m)?)?;
    m.add_function(wrap_pyfunction!(py_lexical_overlap, m)?)?;
    m.add_function(wrap_pyfunction!(py_has_negation, m)?)?;
    m.add_function(wrap_pyfunction!(py_shares_number, m)?)?;
    m.add_function(wrap_pyfunction!(py_is_near_duplicate, m)?)?;
    m.add_function(wrap_pyfunction!(py_reason, m)?)?;
    m.add_function(wrap_pyfunction!(py_matrix, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn basics() {
        assert!(has_negation("жизнь не обнаружена"));
        assert!(!has_negation("жизнь была обнаружена"));
        assert!(!shares_number("около 145 градусов", "температура 145,5")); // разные числа — как в Python
        assert!(shares_number("около 145 градусов", "температура 145"));
        assert!(is_near_duplicate("Юпитер", " юпитер "));
        assert!(!is_near_duplicate("Юпитер большой", "Марс маленький и холодный"));
    }

    #[test]
    fn reasons_in_order() {
        assert_eq!(mandatory_routing_reason("одно и то же", "одно и то же", None, None), Some("exact_or_near_duplicate"));
        assert_eq!(mandatory_routing_reason("Юпитер планета", "Марс планета", Some("CORE"), Some("CORE")), Some("core_plus_core"));
        assert_eq!(mandatory_routing_reason("вода", "лёд", None, None), None);
    }
}
