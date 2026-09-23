//! Перенос agent/claim_types.py — все 5 функций. Python-энумы (ClaimType, ResponseMode, ...)
//! остаются ЦЕЛИКОМ в Python: они широко используются как настоящие объекты `Enum`
//! (`isinstance`, `==`, ключи словарей) в остальном коде, подменять их Rust-типом через границу
//! FFI значило бы тихо сломать эту совместимость. Здесь функции работают со СТРОКОВЫМИ
//! значениями энумов (`ClaimType.FACTUAL.value == "factual"`), а Python-обёртка сама
//! восстанавливает настоящий `ClaimType(...)`/`ResponseMode(...)` из строки на выходе — то же
//! решение, что уже применялось для SourceQualityResult/CriticismAnalysis (Rust отдаёт данные,
//! Python строит из них РЕАЛЬНЫЙ Python-тип).
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_CLAIM_TYPES_ENGINE=rust (см. agent/claim_types.py).

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use std::collections::HashMap;

// agent/claim_types.py::CLAIM_TO_RESPONSE_MODE — строковые значения энумов как ключи/значения.
static CLAIM_TO_RESPONSE_MODE: Lazy<HashMap<&'static str, &'static str>> = Lazy::new(|| {
    [
        ("factual", "factual"),
        ("descriptive_fact", "factual"),
        ("empirical", "qualified_factual"),
        ("theoretical", "qualified_factual"),
        ("interpretation", "pluralistic_contextual"),
        ("normative_claim", "pluralistic_contextual"),
        ("metaphysical_claim", "pluralistic_contextual"),
        ("procedural", "procedural"),
        ("dogmatic", "qualified_factual"),
        ("unknown", "contextual"),
    ]
    .into_iter()
    .collect()
});

static TRUST_CAPS: Lazy<HashMap<&'static str, &'static str>> = Lazy::new(|| {
    [
        ("fully_testable", "STRONGLY_SUPPORTED"),
        ("partially_testable", "SUPPORTED"),
        ("interpretive", "PARTIALLY_SUPPORTED"),
        ("non_falsifiable", "PARTIALLY_SUPPORTED"),
    ]
    .into_iter()
    .collect()
});

static RESPONSE_MODE_DESCRIPTIONS: Lazy<HashMap<&'static str, &'static str>> = Lazy::new(|| {
    [
        ("factual", "Отвечать проверяемыми фактами и данными"),
        ("qualified_factual", "Отвечать фактами с оговорками о неопределённости"),
        ("contextual", "Отвечать с учётом контекста и истории вопроса"),
        ("pluralistic_contextual", "Давать обзор различных позиций и традиций"),
        ("procedural", "Давать пошаговую инструкцию или алгоритм"),
        ("exploratory", "Исследовательский режим с признанием недостатка данных"),
        ("unknown", "Стандартный режим"),
    ]
    .into_iter()
    .collect()
});

/// agent/claim_types.py::get_response_mode — принимает/возвращает СТРОКОВОЕ значение энума.
pub fn get_response_mode(claim_type_value: &str) -> &'static str {
    CLAIM_TO_RESPONSE_MODE.get(claim_type_value).copied().unwrap_or("contextual")
}

/// agent/claim_types.py::should_use_web_for_type
pub fn should_use_web_for_type(claim_type_value: &str) -> bool {
    matches!(
        claim_type_value,
        "procedural" | "factual" | "descriptive_fact" | "empirical" | "theoretical"
    )
}

/// agent/claim_types.py::guess_claim_type_by_text — возвращает строковое значение ClaimType.
pub fn guess_claim_type_by_text(text: &str) -> &'static str {
    let lower = text.to_lowercase();
    let any_of = |words: &[&str]| words.iter().any(|w| lower.contains(w));

    if any_of(&["бог", "душа", "дух", "абсолют", "трансцендентный", "сверхъестественный"]) {
        return "metaphysical_claim";
    }
    if any_of(&["должен", "следует", "обязан", "надлежит", "правильно ли", "справедливо"]) {
        return "normative_claim";
    }
    if any_of(&["как сделать", "как работает", "инструкция", "алгоритм", "способ"]) {
        return "procedural";
    }
    if any_of(&["теория", "гипотеза", "предположение", "модель"]) {
        return "theoretical";
    }
    if any_of(&["эксперимент", "наблюдение", "данные", "измерение"]) {
        return "empirical";
    }
    if any_of(&["смысл", "ценность", "этика", "мораль"]) {
        return "interpretation";
    }
    if any_of(&["доказано", "установлено", "факт", "наука говорит"]) {
        return "dogmatic";
    }
    "factual"
}

/// agent/claim_types.py::get_trust_cap_for_testability — уже строки на входе/выходе в оригинале.
pub fn get_trust_cap_for_testability(testability: &str) -> &'static str {
    TRUST_CAPS.get(testability).copied().unwrap_or("PARTIALLY_SUPPORTED")
}

/// agent/claim_types.py::get_response_mode_description
pub fn get_response_mode_description(mode_value: &str) -> &'static str {
    RESPONSE_MODE_DESCRIPTIONS.get(mode_value).copied().unwrap_or("Стандартный режим")
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(name = "get_response_mode")]
fn py_get_response_mode(claim_type_value: &str) -> String {
    get_response_mode(claim_type_value).to_string()
}

#[pyfunction]
#[pyo3(name = "should_use_web_for_type")]
fn py_should_use_web_for_type(claim_type_value: &str) -> bool {
    should_use_web_for_type(claim_type_value)
}

#[pyfunction]
#[pyo3(name = "guess_claim_type_by_text")]
fn py_guess_claim_type_by_text(text: &str) -> String {
    guess_claim_type_by_text(text).to_string()
}

#[pyfunction]
#[pyo3(name = "get_trust_cap_for_testability")]
fn py_get_trust_cap_for_testability(testability: &str) -> String {
    get_trust_cap_for_testability(testability).to_string()
}

#[pyfunction]
#[pyo3(name = "get_response_mode_description")]
fn py_get_response_mode_description(mode_value: &str) -> String {
    get_response_mode_description(mode_value).to_string()
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_get_response_mode, m)?)?;
    m.add_function(wrap_pyfunction!(py_should_use_web_for_type, m)?)?;
    m.add_function(wrap_pyfunction!(py_guess_claim_type_by_text, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_trust_cap_for_testability, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_response_mode_description, m)?)?;
    Ok(())
}

// ── Юнит-тесты ────────────────────────────────────────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn response_mode_mapping() {
        assert_eq!(get_response_mode("factual"), "factual");
        assert_eq!(get_response_mode("empirical"), "qualified_factual");
        assert_eq!(get_response_mode("interpretation"), "pluralistic_contextual");
        assert_eq!(get_response_mode("unknown"), "contextual");
        assert_eq!(get_response_mode("не-существующее-значение"), "contextual"); // .get(..., default)
    }

    #[test]
    fn web_needed_for_type() {
        assert!(should_use_web_for_type("factual"));
        assert!(should_use_web_for_type("procedural"));
        assert!(should_use_web_for_type("empirical"));
        assert!(!should_use_web_for_type("interpretation"));
        assert!(!should_use_web_for_type("normative_claim"));
        assert!(!should_use_web_for_type("metaphysical_claim"));
        assert!(!should_use_web_for_type("dogmatic")); // явно не входит ни в один список -> false по умолчанию
    }

    #[test]
    fn guess_type_by_markers() {
        assert_eq!(guess_claim_type_by_text("Существует ли Бог?"), "metaphysical_claim");
        assert_eq!(guess_claim_type_by_text("Как сделать бутерброд?"), "procedural");
        assert_eq!(guess_claim_type_by_text("Это доказанный научный факт."), "dogmatic");
        assert_eq!(guess_claim_type_by_text("Юпитер — газовый гигант."), "factual");
    }

    #[test]
    fn guess_type_order_matters_metaphysical_before_dogmatic() {
        // Текст, где могли бы сработать оба маркера — побеждает ПЕРВАЯ проверка по порядку
        // (metaphysical), как и в Python (последовательность if-ов, первый true побеждает).
        let t = guess_claim_type_by_text("Доказано существование Бога наукой.");
        assert_eq!(t, "metaphysical_claim");
    }

    #[test]
    fn trust_cap_lookup() {
        assert_eq!(get_trust_cap_for_testability("fully_testable"), "STRONGLY_SUPPORTED");
        assert_eq!(get_trust_cap_for_testability("nonsense"), "PARTIALLY_SUPPORTED");
    }

    #[test]
    fn response_mode_description_lookup() {
        assert_eq!(get_response_mode_description("procedural"), "Давать пошаговую инструкцию или алгоритм");
        assert_eq!(get_response_mode_description("nonsense"), "Стандартный режим");
    }
}
