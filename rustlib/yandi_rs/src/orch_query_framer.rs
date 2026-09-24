//! Перенос чистых решений agent/orch_query_framer.py (срез 35, 2026-09-24): политика ответа `decide_policy`, уточняющий вопрос из
//! списка недостающего `_auto_cq`, чувствительность домена `_is_safe_domain` — то, что оркестратор считает на КАЖДЫЙ запрос после того,
//! как модель заполнила матрицу запроса (`QueryFrame`). Сам вызов модели, сбор контекста и разбор JSON остаются в Python.
//! Регистр — Python `str.lower()` (`py_text::py_lower`, таблица из Python); таблицы сгенерированы ИЗ Python
//! (`gen_orch_query_framer_data.py`; порядок `_MISSING_TO_QUESTION` важен — побеждает первое вхождение).

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use std::collections::HashSet;

use crate::orch_query_framer_data::{GENERIC_OBJECTS, MISSING_TO_QUESTION, SAFE_DOMAINS};
use crate::py_text::py_lower;

static GENERIC: Lazy<HashSet<&'static str>> = Lazy::new(|| GENERIC_OBJECTS.iter().copied().collect());

/// `_is_safe_domain`
pub fn is_safe_domain(domain: &str) -> bool {
    let d = py_lower(domain);
    SAFE_DOMAINS.iter().any(|s| d.contains(s))
}

/// `decide_policy`: obj — `frame.obj` (None → нет), action_present — `bool(frame.action)`, has_ctx — `bool(frame.constraints)`.
pub fn decide_policy(domain: &str, obj: Option<&str>, action_present: bool, has_ctx: bool) -> &'static str {
    let real_obj = match obj {
        Some(o) if !o.is_empty() => !GENERIC.contains(py_lower(o).as_str()),
        _ => false,
    };
    if is_safe_domain(domain) {
        return "safe_general";
    }
    if !real_obj && !has_ctx {
        return "ask_first";
    }
    if real_obj && action_present && has_ctx {
        return "answer_direct";
    }
    "answer_with_assumptions"
}

/// `_auto_cq`: first_missing — `frame.missing[0]` (None, если список пуст), action — `frame.action`.
pub fn auto_cq(first_missing: Option<&str>, action: Option<&str>) -> String {
    let act = action.filter(|a| !a.is_empty());
    match first_missing {
        None => {
            let action_str = match act {
                Some(a) => format!("«{a}»"),
                None => "сделать".to_string(),
            };
            format!("Что именно нужно {action_str}? Уточните предмет запроса.")
        }
        Some(m) => {
            let first = py_lower(m);
            for (key, tmpl) in MISSING_TO_QUESTION.iter() {
                if first.contains(key) {
                    return tmpl.replace("{action}", act.unwrap_or("сделать"));
                }
            }
            format!("Уточните: {m}?")
        }
    }
}

#[pyfunction]
#[pyo3(name = "is_safe_domain")]
fn py_is_safe_domain(domain: &str) -> bool {
    is_safe_domain(domain)
}

#[pyfunction]
#[pyo3(name = "decide_policy", signature = (domain, obj, action_present, has_ctx))]
fn py_decide_policy(domain: &str, obj: Option<&str>, action_present: bool, has_ctx: bool) -> &'static str {
    decide_policy(domain, obj, action_present, has_ctx)
}

#[pyfunction]
#[pyo3(name = "auto_cq")]
fn py_auto_cq(first_missing: Option<&str>, action: Option<&str>) -> String {
    auto_cq(first_missing, action)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_is_safe_domain, m)?)?;
    m.add_function(wrap_pyfunction!(py_decide_policy, m)?)?;
    m.add_function(wrap_pyfunction!(py_auto_cq, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn policy() {
        assert_eq!(decide_policy("Медицина", Some("x"), true, true), "safe_general");
        assert_eq!(decide_policy("general", None, false, false), "ask_first");
        assert_eq!(decide_policy("general", Some("Задача"), true, false), "ask_first");
        assert_eq!(decide_policy("general", Some("двигатель"), true, true), "answer_direct");
        assert_eq!(decide_policy("general", Some("двигатель"), false, true), "answer_with_assumptions");
    }

    #[test]
    fn cq() {
        assert_eq!(auto_cq(None, Some("купить")), "Что именно нужно «купить»? Уточните предмет запроса.");
        assert_eq!(auto_cq(None, None), "Что именно нужно сделать? Уточните предмет запроса.");
        assert_eq!(auto_cq(Some("Объект"), Some("купить")), "Что именно нужно купить? Уточните предмет запроса.");
        assert_eq!(auto_cq(Some("непонятно"), None), "Уточните: непонятно?");
    }
}
