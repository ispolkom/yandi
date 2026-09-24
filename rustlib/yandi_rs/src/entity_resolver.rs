//! Перенос agent/entity_resolver.py::EntityResolver.resolve — что именно ищет пользователь (игровой термин /
//! медиа / собственное имя) по словарям и капитализации слов; вызывается из pre_pipeline на КАЖДЫЙ запрос.
//! `get_search_strategy` остаётся в Python (пара сравнений над dict). Словари — из Python (resolver_data.rs).
//!
//! Fidelity:
//! * `words = q.split()` — питоновское разбиение по пробелам (py_split_whitespace); «слово с заглавной» —
//!   `w[0].isupper()` по ПЕРВОМУ символу (точная Python-семантика Unicode, py_isupper).
//! * `capital_count >= len(words) * 0.6` — сравнение с float; для ПУСТОГО запроса `0 >= 0.0` истинно
//!   (is_proper_name=True, уверенность 0.7) — так и в оригинале, сохранено.
//! * Порядок прибавления к confidence сохранён (база 0.5, +0.2, термины по 0.1, игры по 0.3, медиа по 0.1,
//!   +0.3 за название): сумма float зависит от порядка ГРУПП (внутри группы слагаемые одинаковы).
//! * `type = "proper_name"` в п.4 перезаписывает "game_location" из п.2 — как в оригинале.
//! * НЕДЕТЕРМИНИЗМ ОРИГИНАЛА: словари в Python — `set` строк, порядок обхода зависит от хеш-рандомизации.
//!   Влияет на (а) поле `game`, когда совпало несколько игр сразу ("x3" и "x3 terran conflict": в Python
//!   победит последняя в порядке обхода set — то есть любая из совпавших), (б) порядок элементов
//!   `categories`. Здесь: `game` — последняя совпавшая в порядке списка KNOWN_GAMES; `categories` — в порядке
//!   списков. parity-тест допускает любой из возможных Python-исходов (множество/мультимножество).
//! * Python-обёртка делегирует, только пока множества экземпляра не изменены.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_ENTITY_RESOLVER_ENGINE=rust (см. agent/entity_resolver.py).

use crate::py_text::PyLowerExt;
use crate::py_text::{py_isupper, py_split_whitespace, py_strip};
use crate::resolver_data::{KNOWN_GAMES, KNOWN_GAME_TERMS, KNOWN_MEDIA};
use pyo3::prelude::*;
use pyo3::types::PyDict;

pub struct Entity {
    pub type_: &'static str,
    pub game: Option<String>,
    pub canonical_name: String,
    pub confidence: f64,
    pub is_proper_name: bool,
    pub needs_exact_search: bool,
    pub categories: Vec<String>,
}

fn first_upper(w: &str) -> bool {
    w.chars().next().map_or(false, |c| py_isupper(&c.to_string()))
}

/// EntityResolver.resolve
pub fn resolve(query: &str) -> Entity {
    let q = py_strip(query);
    let q_lower = q.py_lowercase();

    let mut type_: &'static str = "unknown";
    let mut game: Option<String> = None;
    let mut confidence = 0.5f64;
    let mut is_proper_name = false;
    let mut categories: Vec<String> = Vec::new();

    let words: Vec<&str> = py_split_whitespace(q).collect();
    let capital_count = words.iter().filter(|w| first_upper(w)).count();
    if capital_count as f64 >= words.len() as f64 * 0.6 {
        is_proper_name = true;
        confidence += 0.2;
    }

    for term in KNOWN_GAME_TERMS {
        if q_lower.contains(term) {
            categories.push("game".to_string());
            confidence += 0.1;
        }
    }
    for g in KNOWN_GAMES {
        if q_lower.contains(g) {
            game = Some(g.to_uppercase());
            type_ = "game_location";
            confidence += 0.3;
        }
    }
    for m in KNOWN_MEDIA {
        if q_lower.contains(m) {
            categories.push(m.to_string());
            confidence += 0.1;
        }
    }

    if words.len() >= 2 && words.iter().all(|w| first_upper(w)) {
        is_proper_name = true;
        type_ = "proper_name";
        confidence += 0.3;
    }

    let confidence = if confidence < 1.0 { confidence } else { 1.0 };
    Entity { type_, game, canonical_name: q.to_string(), confidence, is_proper_name, needs_exact_search: confidence > 0.4, categories }
}

#[pyfunction]
#[pyo3(name = "resolve")]
fn py_resolve<'py>(py: Python<'py>, query: &str) -> PyResult<Bound<'py, PyDict>> {
    let e = resolve(query);
    let d = PyDict::new_bound(py);
    d.set_item("type", e.type_)?;
    d.set_item("game", e.game)?;
    d.set_item("canonical_name", e.canonical_name)?;
    d.set_item("confidence", e.confidence)?;
    d.set_item("is_proper_name", e.is_proper_name)?;
    d.set_item("needs_exact_search", e.needs_exact_search)?;
    d.set_item("categories", e.categories)?;
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
    fn empty_query_is_proper_name_like_python() {
        let e = resolve("");
        assert!(e.is_proper_name && e.confidence > 0.69 && e.confidence < 0.71);
    }

    #[test]
    fn game_location_then_proper_name_overrides_type() {
        assert_eq!(resolve("x3 сектор").type_, "game_location");
        assert_eq!(resolve("Легенда Форнема").type_, "proper_name");
        assert_eq!(resolve("Сектор X3").type_, "proper_name");
    }

    #[test]
    fn categories_and_game() {
        let e = resolve("фильм про x3 terran conflict");
        assert_eq!(e.categories, vec!["фильм".to_string()]);
        assert!(e.game.is_some());
    }
}
