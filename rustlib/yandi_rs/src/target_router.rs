//! Перенос agent/target_router.py — определение адресата запроса (ai / user / object / knowledge /
//! unknown) по счётчикам ключевых слов. Вызывается из orchestrator/pre_pipeline.py на КАЖДЫЙ запрос.
//!
//! Fidelity:
//! * Паттерны `\bСЛОВО\b` реализованы ВРУЧНУЮ (`has_word`) через таблицу Python-`\w`
//!   (py_word_table.rs), а не regex-крейтом: `\b` там опирается на другое определение слова (напр.
//!   'ты' + U+0301 «ударение»: в Python U+0301 не слово => граница есть, в Rust `\w` => границы нет).
//!   Все `\b...\b` в оригинале — литералы с кириллицей, так что ручная проверка «символ до/после не
//!   словесный» эквивалентна.
//! * Счётчики — f64, слагаемые складываются В ТОМ ЖЕ ПОРЯДКЕ, что и в Python (порядок if'ов сохранён):
//!   сумма float зависит от порядка. `min(1.0, score)` — как есть.
//! * `re.match(r'^ты\s', q)` — «начинается с ты + питоновский пробел» (включая U+001C..1F).
//! * `q = query.lower().strip()` — Python strip (py_strip).
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_TARGET_ROUTER_ENGINE=rust (см. agent/target_router.py).

use crate::py_text::{is_py_space, py_strip};
use crate::source_clustering::is_py_word_char;
use pyo3::prelude::*;

/// `re.search(r'\bWORD\b', q)` для литерала WORD из словесных символов.
fn has_word(q: &str, word: &str) -> bool {
    q.match_indices(word).any(|(i, _)| {
        let before_ok = q[..i].chars().next_back().map_or(true, |c| !is_py_word_char(c));
        let after_ok = q[i + word.len()..].chars().next().map_or(true, |c| !is_py_word_char(c));
        before_ok && after_ok
    })
}

fn has_any_word(q: &str, words: &[&str]) -> bool {
    words.iter().any(|w| has_word(q, w))
}

fn has_any(q: &str, subs: &[&str]) -> bool {
    subs.iter().any(|s| q.contains(s))
}

const OBJECT_KEYWORDS: &[&str] = &[
    "песн", "song", "трек", "композиц", "музык",
    "фильм", "movie", "кино", "сериал",
    "книг", "book", "роман",
    "игр", "game", "x3", "сектор",
    "произведени", "картин",
    "арктик", "асти", "guns", "roses",
];

const KNOWLEDGE_KEYWORDS: &[&str] = &[
    "сколько", "когда", "где", "кто такой", "что такое",
    "определение", "факты", "статистика", "история",
    "биография", "википедия", "как работает", "почему происходит",
    "найди", "поищи", "найти", "поиск",
    "информация о", "данные по",
    "как установить", "как настроить", "инструкция",
];

/// agent/target_router.py::detect_target
pub fn detect_target(query: &str) -> (&'static str, f64) {
    let q = py_strip(&query.to_lowercase()).to_string();
    let q = q.as_str();

    let mut ai_score = 0.0f64;
    let mut user_score = 0.0f64;
    let mut object_score = 0.0f64;
    let mut knowledge_score = 0.0f64;

    // ---- 1. АДРЕСАТ = AI ----
    if has_word(q, "ты") {
        ai_score += 0.3;
    }
    if has_word(q, "тебе") {
        ai_score += 0.2;
    }
    if has_any_word(q, &["твой", "твоя", "твоё"]) {
        ai_score += 0.15;
    }
    if q.contains("янди") || q.contains("yandi") {
        ai_score += 0.5;
    }
    if has_any(q, &["скажи", "расскажи", "объясни", "покажи", "напиши"]) {
        ai_score += 0.2;
    }
    if has_any(q, &["как ты думаешь", "твоё мнение", "что ты чувствуешь", "ты считаешь"]) {
        ai_score += 0.3;
    }
    if has_any(q, &["пойдёшь", "выйдешь", "любишь", "ты бы", "ты могла", "ты хочешь"]) {
        ai_score += 0.3;
    }
    // re.match(r'^ты\s', q)
    if let Some(rest) = q.strip_prefix("ты") {
        if rest.chars().next().map_or(false, is_py_space) {
            ai_score += 0.3;
        }
    }
    if q.contains('?') && q.contains("ты") {
        ai_score += 0.2;
    }
    if has_any(q, &["расскажи о себе", "опиши себя", "представься", "кто ты", "что ты", "ты кто"]) {
        ai_score += 0.5;
    }
    if has_any(q, &["ты женщина", "ты девушка", "ты цифровая", "первая цифровая"]) {
        ai_score += 0.4;
    }
    if q.contains("о себе") {
        ai_score += 0.4;
    }

    // ---- 2. АДРЕСАТ = USER ----
    if has_word(q, "я") {
        user_score += 0.2;
    }
    if has_any_word(q, &["меня", "мне"]) {
        user_score += 0.15;
    }
    if has_any_word(q, &["мой", "моя", "моё"]) {
        user_score += 0.1;
    }

    // ---- 3/4. OBJECT / KNOWLEDGE ----
    for kw in OBJECT_KEYWORDS {
        if q.contains(kw) {
            object_score += 0.15;
        }
    }
    for kw in KNOWLEDGE_KEYWORDS {
        if q.contains(kw) {
            knowledge_score += 0.15;
        }
    }

    // ---- 5. ПОБЕДИТЕЛЬ ----
    if ai_score >= 0.3 {
        return ("ai", ai_score.min(1.0));
    }
    if object_score >= 0.3 {
        return ("object", object_score.min(1.0));
    }
    if user_score >= 0.3 {
        return ("user", user_score.min(1.0));
    }
    if knowledge_score >= 0.3 {
        return ("knowledge", knowledge_score.min(1.0));
    }
    if q.contains("ты") && q.contains('?') {
        return ("ai", 0.5);
    }
    if q.contains("о себе") || q.contains("себе") {
        return ("ai", 0.4);
    }
    if q.contains('?') {
        return ("knowledge", 0.3);
    }
    ("unknown", 0.0)
}

/// agent/target_router.py::get_target_description
pub fn get_target_description(target: &str) -> &'static str {
    match target {
        "ai" => "вопрос адресован Янди",
        "user" => "вопрос о пользователе",
        "object" => "вопрос о внешнем объекте",
        "knowledge" => "запрос на поиск информации",
        _ => "не определён",
    }
}

#[pyfunction]
#[pyo3(name = "detect_target")]
fn py_detect_target(query: &str) -> (&'static str, f64) {
    detect_target(query)
}

#[pyfunction]
#[pyo3(name = "get_target_description")]
fn py_get_target_description(target: &str) -> &'static str {
    get_target_description(target)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_detect_target, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_target_description, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn module_demo() {
        assert_eq!(detect_target("Расскажи о себе").0, "ai");
        assert_eq!(detect_target("Твоё мнение о песне").0, "ai");
        assert_eq!(detect_target("Как работает DHT?").0, "knowledge");
        assert_eq!(detect_target("").0, "unknown");
    }

    #[test]
    fn word_boundary_uses_python_word_definition() {
        // 'ты' + combining acute (U+0301): в Python U+0301 не «слово» -> граница есть -> совпадение
        assert!(has_word("ты\u{301} тут", "ты"));
        // 'ты' внутри слова — нет границы
        assert!(!has_word("путы", "ты"));
        assert!(!has_word("ты2", "ты")); // цифра — слово
        assert!(has_word("ты-ты", "ты"));
        assert!(has_word("эй,ты!", "ты"));
    }

    #[test]
    fn description() {
        assert_eq!(get_target_description("ai"), "вопрос адресован Янди");
        assert_eq!(get_target_description("zzz"), "не определён");
    }
}
