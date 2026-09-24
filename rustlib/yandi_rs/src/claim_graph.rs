//! Перенос текстового ядра agent/claim_graph.py::ClaimGraph — извлечение «world claims» из evidence и связи
//! между ними: разбиение на предложения, очистка, фильтр «утверждение о мире», тип утверждения, уверенность,
//! надёжность источника по URL, проверки «противоречит»/«поддерживает» и попарное построение рёбер графа
//! (`build_edges`, O(n²) — слово-множества и lower() считаются ОДИН раз на утверждение, а не на каждую пару).
//! Класс ClaimGraph, датакласс Claim, дедупликация и время (`time.time()`) остаются в Python.
//! Вызывается из orchestrator_v2.py в горячем пути каждого запроса.
//!
//! Fidelity:
//! * Все regex — с `(?i)` (и `\s`, `\d`, `[^...]`) через py_regex: точная питоновская семантика IGNORECASE,
//!   пробелов и цифр; `len(...)` — в СИМВОЛАХ (`< 20`, `> 350`, `> 100`, `> 20`).
//! * Проверка «противоречия»: `"не" in text.lower()` — ПОДСТРОКА (совпадёт со словом «неделя») — сохранено;
//!   маркеры "является"/"не является" ищутся в ИСХОДНОМ тексте (регистр важен), не в lower().
//! * `_is_support`: пересечение МНОЖЕСТВ слов `text.split()` (питоновские пробелы), порог `> 3`.
//! * Уверенность: `min(1.0, max(0.1, base))` — питоновские max/min (при NaN max(0.1, nan) = 0.1), не f64::clamp.
//! * Порядок рёбер `build_edges` = порядок вложенных циклов Python (i по возрастанию, j > i), `elif` между
//!   противоречием и поддержкой сохранён.
//! * Python-обёртка делегирует только на строках/числах нужных типов (иначе исходный Python-путь).
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_CLAIM_GRAPH_ENGINE=rust (см. agent/claim_graph.py).

use crate::py_text::PyLowerExt;
use crate::py_text::{py_split_whitespace, py_strip};
use once_cell::sync::Lazy;
#[cfg(feature = "python")]
use pyo3::prelude::*;
use regex::Regex;
use std::collections::HashSet;

fn re(p: &str) -> Regex {
    crate::py_text::py_regex(p)
}

static SENTENCE_SPLIT: Lazy<Regex> = Lazy::new(|| re(r"[.!?]\s+"));
static BRACKETS: Lazy<Regex> = Lazy::new(|| re(r"\[[^\]]+\]"));
static BOLD: Lazy<Regex> = Lazy::new(|| re(r"\*\*([^*]+)\*\*"));
static ITALIC: Lazy<Regex> = Lazy::new(|| re(r"\*([^*]+)\*"));
static NUMBERED: Lazy<Regex> = Lazy::new(|| re(r"^\d+\.\s+"));
static DIGITS: Lazy<Regex> = Lazy::new(|| re(r"\d+"));

const META_PATTERNS: &[&str] = &[
    r"(?i)вопрос\s+пользователя", r"(?i)запрос\s+пользователя", r"(?i)пользователь\s+спрашивает",
    r"(?i)требует\s+философских", r"(?i)отвечая\s+на\s+вопрос", r"(?i)данный\s+документ", r"(?i)эта\s+статья",
    r"(?i)источник\s+содержит", r"(?i)содержание\s+источников", r"(?i)проанализировав", r"(?i)результат\s+анализа",
    r"(?i)сырые\s+данные", r"(?i)извлечённые\s+факты", r"(?i)матрица\s+эпистемического",
];
const WORLD_PATTERNS: &[&str] = &[
    r"(?i)является", r"(?i)определяется", r"(?i)называется", r"(?i)представляет собой", r"(?i)состоит из",
    r"(?i)включает", r"(?i)содержит", r"(?i)возник", r"(?i)произошёл", r"(?i)создан", r"(?i)относится к",
    r"(?i)связан с", r"(?i)может быть", r"(?i)является результатом", r"(?i)существует", r"(?i)известно",
    r"(?i)установлено",
];
static META: Lazy<Vec<Regex>> = Lazy::new(|| META_PATTERNS.iter().map(|p| re(p)).collect());
static WORLD: Lazy<Vec<Regex>> = Lazy::new(|| WORLD_PATTERNS.iter().map(|p| re(p)).collect());

static T_HYPOTHESIS: Lazy<Regex> = Lazy::new(|| re(r"(?i)вероятно|возможно|предположительно|может быть|гипотеза"));
static T_UNCERTAINTY: Lazy<Regex> = Lazy::new(|| re(r"(?i)неизвестно|не\s+установлено|остаётся\s+неясным|предмет\s+дискуссии"));
static T_DEFINITION: Lazy<Regex> = Lazy::new(|| re(r"(?i)определяется|называется|это|означает|представляет собой"));
static T_FACTUAL: Lazy<Regex> = Lazy::new(|| re(r"(?i)согласно|по\s+данным|исследование|показывает|установлено|факт|доказано"));
static C_FACT: Lazy<Regex> = Lazy::new(|| re(r"(?i)согласно|по данным|установлено|факт|доказано"));
static C_HYP: Lazy<Regex> = Lazy::new(|| re(r"(?i)вероятно|возможно|предположительно|гипотеза"));
static C_UNC: Lazy<Regex> = Lazy::new(|| re(r"(?i)неизвестно|не установлено|остаётся неясным"));

/// `_split_into_sentences`
pub fn split_into_sentences(text: &str) -> Vec<String> {
    SENTENCE_SPLIT
        .split(text)
        .map(py_strip)
        .filter(|s| s.chars().count() > 20)
        .map(|s| s.to_string())
        .collect()
}

/// `_clean_sentence`
pub fn clean_sentence(sent: &str) -> String {
    let joined = py_split_whitespace(sent).collect::<Vec<_>>().join(" ");
    let s = BRACKETS.replace_all(&joined, "");
    let s = BOLD.replace_all(&s, "$1");
    let s = ITALIC.replace_all(&s, "$1");
    let s = NUMBERED.replace(&s, "");
    py_strip(&s).to_string()
}

/// `_is_world_claim`
pub fn is_world_claim(text: &str) -> bool {
    let n = text.chars().count();
    if n < 20 || n > 350 {
        return false;
    }
    if META.iter().any(|r| r.is_match(text)) {
        return false;
    }
    if WORLD.iter().any(|r| r.is_match(text)) {
        return true;
    }
    DIGITS.is_match(text)
}

/// `_determine_claim_type`
pub fn determine_claim_type(text: &str) -> &'static str {
    if T_HYPOTHESIS.is_match(text) {
        "hypothesis"
    } else if T_UNCERTAINTY.is_match(text) {
        "uncertainty"
    } else if T_DEFINITION.is_match(text) {
        "definition"
    } else if T_FACTUAL.is_match(text) {
        "factual"
    } else {
        "interpretation"
    }
}

fn py_min(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}
fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// `_calculate_confidence(text, ev)` при `relevance = ev.get("relevance_to_query", 0.5)` (число)
pub fn calculate_confidence(text: &str, relevance: f64) -> f64 {
    let mut base = 0.5f64;
    if C_FACT.is_match(text) {
        base += 0.25;
    } else if C_HYP.is_match(text) {
        base -= 0.1;
    } else if C_UNC.is_match(text) {
        base += 0.1;
    }
    if text.chars().count() > 100 {
        base += 0.05;
    }
    base += relevance * 0.15;
    py_min(1.0, py_max(0.1, base))
}

/// `_get_source_reliability(ev)` при строковых `source_uri` и `source_type`
pub fn source_reliability(uri: &str, source_type: &str) -> f64 {
    if uri.contains("wikipedia") {
        return 0.9;
    }
    if uri.contains("science") || uri.contains("nature") {
        return 0.85;
    }
    if uri.contains("news") {
        return 0.6;
    }
    if uri.contains("blog") {
        return 0.4;
    }
    if source_type.contains("local") {
        return 0.7;
    }
    0.5
}

const NEG_MARKERS: &[&str] = &["не", "нет", "нельзя", "невозможно"];

fn has_neg(lower: &str) -> bool {
    NEG_MARKERS.iter().any(|w| lower.contains(w))
}

fn contradiction_core(t1: &str, neg1: bool, t2: &str, neg2: bool) -> bool {
    if neg1 != neg2 {
        return true;
    }
    if t1.contains("является") && t2.contains("не является") {
        return true;
    }
    if t1.contains("не является") && t2.contains("является") {
        return true;
    }
    false
}

/// `_is_contradiction`
pub fn is_contradiction(t1: &str, t2: &str) -> bool {
    contradiction_core(t1, has_neg(&t1.py_lowercase()), t2, has_neg(&t2.py_lowercase()))
}

/// `_is_support`
pub fn is_support(t1: &str, t2: &str) -> bool {
    let a: HashSet<&str> = py_split_whitespace(t1).collect();
    let b: HashSet<&str> = py_split_whitespace(t2).collect();
    a.intersection(&b).count() > 3
}

/// Все рёбра `_build_graph` в порядке вложенных циклов Python: (i, j, 0=противоречие | 1=поддержка).
pub fn build_edges(texts: &[String]) -> Vec<(usize, usize, u8)> {
    let n = texts.len();
    let negs: Vec<bool> = texts.iter().map(|t| has_neg(&t.py_lowercase())).collect();
    let words: Vec<HashSet<&str>> = texts.iter().map(|t| py_split_whitespace(t).collect()).collect();
    let mut edges = Vec::new();
    for i in 0..n {
        for j in (i + 1)..n {
            if contradiction_core(&texts[i], negs[i], &texts[j], negs[j]) {
                edges.push((i, j, 0u8));
            } else if words[i].intersection(&words[j]).count() > 3 {
                edges.push((i, j, 1u8));
            }
        }
    }
    edges
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "split_into_sentences")]
fn py_split(text: &str) -> Vec<String> {
    split_into_sentences(text)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "clean_sentence")]
fn py_clean(sent: &str) -> String {
    clean_sentence(sent)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "is_world_claim")]
fn py_world(text: &str) -> bool {
    is_world_claim(text)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "determine_claim_type")]
fn py_type(text: &str) -> &'static str {
    determine_claim_type(text)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "calculate_confidence")]
fn py_conf(text: &str, relevance: f64) -> f64 {
    calculate_confidence(text, relevance)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "source_reliability")]
fn py_rel(uri: &str, source_type: &str) -> f64 {
    source_reliability(uri, source_type)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "is_contradiction")]
fn py_contra(t1: &str, t2: &str) -> bool {
    is_contradiction(t1, t2)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "is_support")]
fn py_support(t1: &str, t2: &str) -> bool {
    is_support(t1, t2)
}
#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "build_edges")]
fn py_edges(texts: Vec<String>) -> Vec<(usize, usize, u8)> {
    build_edges(&texts)
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_split, m)?)?;
    m.add_function(wrap_pyfunction!(py_clean, m)?)?;
    m.add_function(wrap_pyfunction!(py_world, m)?)?;
    m.add_function(wrap_pyfunction!(py_type, m)?)?;
    m.add_function(wrap_pyfunction!(py_conf, m)?)?;
    m.add_function(wrap_pyfunction!(py_rel, m)?)?;
    m.add_function(wrap_pyfunction!(py_contra, m)?)?;
    m.add_function(wrap_pyfunction!(py_support, m)?)?;
    m.add_function(wrap_pyfunction!(py_edges, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sentences_and_cleaning() {
        let v = split_into_sentences("Юпитер является крупнейшей планетой. Это газовый гигант! Коротко. ");
        assert_eq!(v.len(), 1);
        assert_eq!(clean_sentence("1.  **Жирный** и *курсив* [1] текст"), "Жирный и курсив  текст"); // двойной пробел: как в Python (нормализация ДО удаления [1])
    }

    #[test]
    fn world_claim_filters() {
        assert!(is_world_claim("Юпитер является крупнейшей планетой Солнечной системы"));
        assert!(!is_world_claim("Вопрос пользователя требует философских размышлений о жизни"));
        assert!(is_world_claim("Температура на поверхности составляет около 145 градусов"));
        assert!(!is_world_claim("коротко"));
    }

    #[test]
    fn confidence_python_min_max() {
        assert!((calculate_confidence("Согласно исследованиям", 0.5) - 0.825).abs() < 1e-12);
        assert_eq!(calculate_confidence("x", f64::NAN), 0.1); // max(0.1, nan) == 0.1
    }

    #[test]
    fn contradiction_is_substring_based() {
        assert!(is_contradiction("неделя длинная", "день короткий")); // "не" внутри слова
        assert!(!is_contradiction("день длинный", "ночь короткая"));
    }

    #[test]
    fn edges_order_and_elif() {
        let t = vec!["а б в г д е".to_string(), "а б в г д ж".to_string(), "не так".to_string()];
        let e = build_edges(&t);
        assert_eq!(e, vec![(0, 1, 1), (0, 2, 0), (1, 2, 0)]);
    }
}
