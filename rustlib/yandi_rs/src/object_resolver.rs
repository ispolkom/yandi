//! Перенос agent/object_resolver.py::ObjectResolver.resolve — тип объекта субъективного запроса (песня/фильм/
//! книга/персона/идея/самопознание/игра) по спискам паттернов; вызывается из orchestrator_v2 на запросы.
//! Таблицы паттернов сгенерированы ИЗ Python (resolver_data.rs, gen_resolver_data.py).
//!
//! Fidelity: паттерны применяются с `re.IGNORECASE` к `query.lower().strip()` (py_regex: точный IGNORECASE);
//! `len(pattern)` — число СИМВОЛОВ исходной строки паттерна; уверенность = min(1.0, база + len/200), у
//! "self_reflection" ещё +0.2 (без повторного min до сравнения — как в оригинале), итог min(1.0, …);
//! при равной уверенности выигрывает ПЕРВЫЙ (строгое `>`); порядок типов/паттернов как в Python dict/list.
//! Python-обёртка делегирует, только пока `self.patterns` экземпляра не изменён.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_OBJECT_RESOLVER_ENGINE=rust (см. agent/object_resolver.py).

use crate::py_text::PyLowerExt;
use crate::resolver_data::OBJECT_TYPES;
use once_cell::sync::Lazy;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use regex::Regex;

static COMPILED: Lazy<Vec<Vec<Regex>>> = Lazy::new(|| {
    OBJECT_TYPES
        .iter()
        .map(|t| t.patterns.iter().map(|p| crate::py_text::py_regex(&format!("(?i){p}"))).collect())
        .collect()
});

pub struct Resolved {
    pub type_: &'static str,
    pub confidence: f64,
    pub analyzer: &'static str,
    pub matched_pattern: &'static str,
}

/// ObjectResolver.resolve
pub fn resolve(query: &str) -> Resolved {
    let q = crate::py_text::py_strip(&query.py_lowercase()).to_string();
    let mut best = Resolved { type_: "unknown", confidence: 0.0, analyzer: "GeneralSubjective", matched_pattern: "none" };
    for (t, regs) in OBJECT_TYPES.iter().zip(COMPILED.iter()) {
        for (pattern, re) in t.patterns.iter().zip(regs.iter()) {
            if re.is_match(&q) {
                let raw = t.confidence + (pattern.chars().count() as f64 / 200.0);
                let mut confidence = if raw < 1.0 { raw } else { 1.0 };
                if t.key == "self_reflection" {
                    confidence += 0.2;
                }
                if confidence > best.confidence {
                    best = Resolved {
                        type_: t.type_,
                        confidence: if confidence < 1.0 { confidence } else { 1.0 },
                        analyzer: t.analyzer,
                        matched_pattern: pattern,
                    };
                }
            }
        }
    }
    best
}

#[pyfunction]
#[pyo3(name = "resolve")]
fn py_resolve<'py>(py: Python<'py>, query: &str) -> PyResult<Bound<'py, PyDict>> {
    let r = resolve(query);
    let d = PyDict::new_bound(py);
    d.set_item("type", r.type_)?;
    d.set_item("confidence", r.confidence)?;
    d.set_item("analyzer", r.analyzer)?;
    d.set_item("matched_pattern", r.matched_pattern)?;
    Ok(d)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_resolve, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn typical_queries() {
        assert_eq!(resolve("Твоё мнение о песне Guns N Roses").type_, "song");
        assert_eq!(resolve("расскажи о себе").type_, "self_reflection");
        assert_eq!(resolve("").type_, "unknown");
        assert_eq!(resolve("xyz").matched_pattern, "none");
    }

    #[test]
    fn self_reflection_boost_and_cap() {
        let r = resolve("ты женщина или мужчина");
        assert_eq!(r.type_, "self_reflection");
        assert!(r.confidence <= 1.0 && r.confidence > 0.7);
    }
}
