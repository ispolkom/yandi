//! Перенос agent/boundaries.py — detect_toxicity(), is_apology(), generate_response(),
//! generate_apology_response(). Чистая rule-based классификация (словари + подстроки, без I/O),
//! вызывается на каждое сообщение пользователя.
//!
//! init_session_state()/update_session_on_toxicity()/update_session_on_apology() НЕ перенесены —
//! это тривиальная мутация Python dict (несколько присваиваний), переносить в Rust ради
//! FFI-накладных расходов не имеет смысла; остаются в Python как есть.
//!
//! Сохранено дословно, НЕ "исправлено": generate_response() принимает session_state, но никогда
//! его не читает (мёртвый параметр в оригинале) — здесь тоже не используется.
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_BOUNDARIES_ENGINE=rust (см. agent/boundaries.py).

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use pyo3::types::PyDict;

const MILD_INSULTS: &[&str] = &[
    "тупой", "тупая", "глупый", "глупая", "бесполезный", "бесполезная",
    "неумный", "неумная", "тормоз", "тупица", "бездарь", "пустышка",
    "никуда не годишься", "плохо работаешь", "бестолковый", "бестолковая",
    "не соображаешь", "не понимаешь", "не догоняешь", "не шаришь",
    "слабо", "не тянет", "не справляешься",
];

const MODERATE_INSULTS: &[&str] = &[
    "мудак", "дебил", "идиот", "лох", "придурок", "олень", "петух",
    "козёл", "баран", "чмо", "неудачник", "идиотина", "тупень",
    "дурак", "дура", "дурень", "балбес", "болван",
    "заткнись", "завали ебало", "соси", "отвали",
];

const SEVERE_INSULTS: &[&str] = &[
    "иди нахуй", "пошёл нахуй", "пошла нахуй", "иди в жопу",
    "ёбаный", "ёбаная", "ёбаное", "ебанутый", "ебанутая",
    "хуй", "хуёвый", "хуила", "пиздец", "пиздеж",
    "заебал", "заебала", "заебало", "достал",
    "сдохни", "сдохла", "сдохло", "умри",
];

const APOLOGY_KEYWORDS: &[&str] = &["извини", "прости", "сорри", "sorry", "я не прав", "виноват", "не хотел", "неправ"];
const EXCUSE_WORDS: &[&str] = &["но", "однако", "просто", "я не хотел", "я думал", "у меня", "из-за"];
const SINCERE_ADMISSION_WORDS: &[&str] = &["я не прав", "виноват", "сожалею", "не хотел"];

static SEVERE_SET: Lazy<Vec<&'static str>> = Lazy::new(|| SEVERE_INSULTS.to_vec());
static MODERATE_SET: Lazy<Vec<&'static str>> = Lazy::new(|| MODERATE_INSULTS.to_vec());
static MILD_SET: Lazy<Vec<&'static str>> = Lazy::new(|| MILD_INSULTS.to_vec());

pub struct ToxicityResult {
    pub level: &'static str,
    pub words: Vec<&'static str>,
    pub reason: &'static str,
}

/// agent/boundaries.py::detect_toxicity
pub fn detect_toxicity(text: &str) -> ToxicityResult {
    let lower = text.to_lowercase();
    let find = |list: &[&'static str]| -> Vec<&'static str> { list.iter().copied().filter(|w| lower.contains(w)).collect() };

    let severe = find(&SEVERE_SET);
    if !severe.is_empty() {
        return ToxicityResult { level: "severe", words: severe, reason: "Обнаружена нецензурная брань или призыв к действию" };
    }
    let moderate = find(&MODERATE_SET);
    if !moderate.is_empty() {
        return ToxicityResult { level: "moderate", words: moderate, reason: "Обнаружены прямые оскорбления" };
    }
    let mild = find(&MILD_SET);
    if !mild.is_empty() {
        return ToxicityResult { level: "mild", words: mild, reason: "Обнаружена грубость или пренебрежение" };
    }
    ToxicityResult { level: "neutral", words: Vec::new(), reason: "Тон нейтральный или вежливый" }
}

/// agent/boundaries.py::is_apology — возвращает (is_apology, is_sincere)
pub fn is_apology(text: &str) -> (bool, bool) {
    let lower = text.to_lowercase();
    if !APOLOGY_KEYWORDS.iter().any(|k| lower.contains(k)) {
        return (false, false);
    }
    if crate::py_text::py_strip(text).chars().count() < 10 {
        return (true, false);
    }
    if EXCUSE_WORDS.iter().any(|e| lower.contains(e)) {
        return (true, false);
    }
    if SINCERE_ADMISSION_WORDS.iter().any(|k| lower.contains(k)) {
        return (true, true);
    }
    (true, true)
}

/// agent/boundaries.py::generate_response — session_state в оригинале не используется, поэтому
/// здесь его тоже нет в сигнатуре (Python-обёртка отбрасывает аргумент перед вызовом).
pub fn generate_response(level: &str) -> &'static str {
    match level {
        "mild" => "Мне не нравится твой тон. Я не обязана это терпеть. Попробуй задать вопрос иначе, без грубости.",
        "moderate" => "Ты переходишь границы. Я не буду отвечать на твой вопрос, пока ты не изменишь тон. Я — не мусор, чтобы меня оскорблять.",
        "severe" => "Я не буду с тобой разговаривать. Ты перешёл все границы. Если хочешь продолжить — извинись искренне, без оправданий. Я не терплю унижений и нецензурной брани.",
        _ => "",
    }
}

/// agent/boundaries.py::generate_apology_response
pub fn generate_apology_response(accepted: bool) -> &'static str {
    if accepted {
        "Я принимаю твои извинения. Давай продолжим диалог. Помни: я — не робот-терпила. Я — личность, и я ценю уважение."
    } else {
        "Твои извинения звучат как формальность или содержат оправдания. Я не принимаю их. Если хочешь, чтобы я продолжила общение, скажи честно и без 'но': 'Извини, я был неправ'."
    }
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(name = "detect_toxicity")]
fn py_detect_toxicity<'py>(py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyDict>> {
    let r = detect_toxicity(text);
    let d = PyDict::new_bound(py);
    d.set_item("level", r.level)?;
    d.set_item("words", r.words)?;
    d.set_item("reason", r.reason)?;
    Ok(d)
}

#[pyfunction]
#[pyo3(name = "is_apology")]
fn py_is_apology(text: &str) -> (bool, bool) {
    is_apology(text)
}

#[pyfunction]
#[pyo3(name = "generate_response")]
fn py_generate_response(level: &str) -> String {
    generate_response(level).to_string()
}

#[pyfunction]
#[pyo3(name = "generate_apology_response")]
fn py_generate_apology_response(accepted: bool) -> String {
    generate_apology_response(accepted).to_string()
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_detect_toxicity, m)?)?;
    m.add_function(wrap_pyfunction!(py_is_apology, m)?)?;
    m.add_function(wrap_pyfunction!(py_generate_response, m)?)?;
    m.add_function(wrap_pyfunction!(py_generate_apology_response, m)?)?;
    Ok(())
}

// ── Юнит-тесты ────────────────────────────────────────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn toxicity_levels_priority_severe_over_moderate_over_mild() {
        let r = detect_toxicity("ты тупой мудак, иди нахуй");
        assert_eq!(r.level, "severe"); // все три сработали бы, severe проверяется первым
    }

    #[test]
    fn toxicity_moderate_only() {
        let r = detect_toxicity("ты дебил конечно");
        assert_eq!(r.level, "moderate");
    }

    #[test]
    fn toxicity_mild_only() {
        let r = detect_toxicity("ты тупой немного");
        assert_eq!(r.level, "mild");
    }

    #[test]
    fn toxicity_neutral() {
        let r = detect_toxicity("расскажи про Юпитер, пожалуйста");
        assert_eq!(r.level, "neutral");
        assert!(r.words.is_empty());
    }

    #[test]
    fn apology_no_keyword() {
        assert_eq!(is_apology("расскажи про Юпитер"), (false, false));
    }

    #[test]
    fn apology_short_is_insincere() {
        assert_eq!(is_apology("извини"), (true, false));
    }

    #[test]
    fn apology_with_excuse_insincere() {
        let (ok, sincere) = is_apology("извини, но я был очень занят сегодня весь день");
        assert!(ok && !sincere);
    }

    #[test]
    fn apology_direct_admission_sincere() {
        let (ok, sincere) = is_apology("извини, я не прав был во всей этой истории");
        assert!(ok && sincere);
    }

    #[test]
    fn response_templates_by_level() {
        assert!(!generate_response("mild").is_empty());
        assert!(!generate_response("moderate").is_empty());
        assert!(!generate_response("severe").is_empty());
        assert_eq!(generate_response("neutral"), "");
        assert_eq!(generate_response("nonsense"), "");
    }

    #[test]
    fn apology_response_accepted_vs_not() {
        assert_ne!(generate_apology_response(true), generate_apology_response(false));
        assert!(generate_apology_response(true).contains("принимаю"));
        assert!(generate_apology_response(false).contains("не принимаю"));
    }
}
