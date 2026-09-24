//! Перенос agent/criticism_detector.py — CriticismDetector.analyze() и .get_response_template().
//! Чистая rule-based классификация (словари + regex, никакого I/O), вызывается на КАЖДОЕ
//! сообщение пользователя — горячий путь для эмоциональной/relationship-обработки компаньона.
//!
//! Класс CriticismDetector в Python хранит списки слов в self.* (устанавливаются в __init__), но
//! НИКОГДА их не мутирует после этого — по сути это модульные константы, обёрнутые в атрибуты
//! экземпляра; здесь они и есть настоящие модульные константы (без накладных расходов на
//! создание нового Vec на каждый CriticismDetector()).
//!
//! ВНИМАНИЕ, сохранено дословно: в analyze() Python читает `irritation` и `history_criticism` из
//! context, но НИ ОДИН из них не используется дальше в теле функции (мёртвое чтение в
//! оригинале) — не "исправлено" здесь, просто не участвует в вычислении, как и в Python.
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_CRITICISM_ENGINE=rust (см. agent/criticism_detector.py).

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyDictMethods};
use regex::Regex;

const PERSON_INSULTS: &[&str] = &[
    "глуп", "туп", "дур", "идиот", "кретин", "дебил",
    "безмозгл", "бездарн", "ничтож", "урод",
    "неумн", "пустоголов", "бестолков", "недалёк",
    "слабоумн", "малоумн", "тупиц", "болван",
    "язык поворачивается", "совесть есть", "стыдно должно быть",
    "не стыдно", "с ума сошла", "ненормальн",
];

const INTELLIGENCE_INSULTS: &[&str] = &[
    "ничего не понимаешь", "ничего не знаешь",
    "не соображаешь", "не доходит", "не въезжаешь",
    "туго соображаешь", "тормозишь",
    "несешь чушь", "несёшь бред", "несёшь фигню",
    "глупость говоришь", "глупости говоришь",
    "ты вообще понимаешь", "ты в своём уме",
];

const CONSTRUCTIVE_PATTERNS: &[(&str, &str)] = &[
    (r"ты ошиблась в", "ошибка в расчётах"),
    (r"ты не учла", "упущение"),
    (r"можно было бы", "предложение"),
    (r"я бы предложил", "предложение"),
    (r"попробуй", "совет"),
    (r"стоит перепроверить", "совет"),
    (r"обрати внимание на", "совет"),
    (r"возможно, стоит", "предложение"),
    (r"лучше сделать", "предложение"),
    (r"вместо этого", "альтернатива"),
    (r"давай попробуем", "альтернатива"),
    (r"а что если", "альтернатива"),
    (r"не работает", "проблема в подходе"),
];

const NEUTRAL_CRITICISM: &[&str] = &[
    "не сработало", "неправильно",
    "ошибка", "недочёт", "проблема",
    "не соответствует", "не подходит",
    "нужно переделать", "нужно исправить",
    "неправильн", "неверн",
];

const AGGRESSIVE_MARKERS: &[&str] = &["вообще", "абсолютно", "совершенно", "как можно", "как ты можешь"];

static CONSTRUCTIVE_REGEXES: Lazy<Vec<Regex>> = Lazy::new(|| {
    CONSTRUCTIVE_PATTERNS
        .iter()
        .map(|(p, _)| crate::py_text::py_regex(p))
        .collect()
});

#[derive(Debug, Clone, Default)]
pub struct CriticismAnalysis {
    pub is_criticism: bool,
    pub is_insult: bool,
    pub is_constructive: bool,
    pub is_feedback: bool,
    pub specificity: f64,
    pub constructiveness: f64,
    pub severity: f64,
    pub target: String,
    pub suggested_improvement: Option<String>,
    pub confidence: f64,
    pub reason: String,
    pub context_adjustment: f64,
}

impl CriticismAnalysis {
    fn new() -> Self {
        Self { target: "unknown".to_string(), ..Default::default() }
    }
}

fn check_person_insults(text: &str) -> (f64, Vec<&'static str>) {
    let found: Vec<&'static str> = PERSON_INSULTS.iter().copied().filter(|w| text.contains(w)).collect();
    if found.is_empty() {
        return (0.0, found);
    }
    ((found.len() as f64 * 0.25 + 0.1).min(1.0), found)
}

fn check_intelligence_insults(text: &str) -> (f64, Vec<&'static str>) {
    let found: Vec<&'static str> = INTELLIGENCE_INSULTS.iter().copied().filter(|w| text.contains(w)).collect();
    if found.is_empty() {
        return (0.0, found);
    }
    ((found.len() as f64 * 0.3 + 0.2).min(1.0), found)
}

fn check_constructive_with_suggestion(text: &str) -> (f64, Option<&'static str>) {
    for (re, (_, suggestion)) in CONSTRUCTIVE_REGEXES.iter().zip(CONSTRUCTIVE_PATTERNS.iter()) {
        if re.is_match(text) {
            return (0.7, Some(*suggestion));
        }
    }
    if text.contains("попробуй") || text.contains("попробуйте") {
        return (0.5, Some("попробовать альтернативу"));
    }
    if text.contains("стоит") || text.contains("лучше") {
        return (0.4, Some("рассмотреть альтернативу"));
    }
    if text.contains("давай") || text.contains("давайте") {
        return (0.3, Some("совместное решение"));
    }
    (0.0, None)
}

fn check_neutral_criticism(text: &str) -> f64 {
    let mut score: f64 = 0.0;
    for phrase in NEUTRAL_CRITICISM {
        if text.contains(phrase) {
            score += 0.2;
        }
    }
    score.min(1.0)
}

fn check_aggression(text: &str) -> f64 {
    let mut score: f64 = 0.0;
    for marker in AGGRESSIVE_MARKERS {
        if text.contains(marker) {
            score += 0.15;
        }
    }
    if text.contains('!') {
        score += 0.1;
    }
    // Python str.isupper(): true только если есть хотя бы один "cased" символ и ВСЕ cased
    // символы — заглавные (символы без регистра, вроде цифр/пунктуации, не в счёт).
    let has_cased = text.chars().any(|c| c.is_alphabetic());
    let all_upper = !text.chars().any(|c| c.is_lowercase());
    if has_cased && all_upper && text.chars().count() > 10 {
        score += 0.2;
    }
    score.min(0.5)
}

/// agent/criticism_detector.py::CriticismDetector.analyze
pub fn analyze(text: &str, trust: f64, history_insults: f64) -> CriticismAnalysis {
    let text_lower = text.to_lowercase();
    let mut result = CriticismAnalysis::new();

    let (insult_score, insult_words) = check_person_insults(&text_lower);
    let (intelligence_score, intelligence_phrases) = check_intelligence_insults(&text_lower);
    let (constructive_score, suggestion) = check_constructive_with_suggestion(&text_lower);
    let neutral_score = check_neutral_criticism(&text_lower);
    let aggression_boost = check_aggression(&text_lower);

    let repeat_offender_penalty = (history_insults * 0.05).min(0.3);
    let trust_penalty = (50.0 - trust).max(0.0) * 0.005;
    let context_adjustment = repeat_offender_penalty + trust_penalty;

    if insult_score > 0.0 {
        result.is_insult = true;
        result.is_criticism = false;
        result.severity = (insult_score + aggression_boost + context_adjustment).min(1.0);
        result.confidence = (0.5 + insult_score * 0.4 + context_adjustment * 0.3).min(1.0);
        result.target = "personality".to_string();
        let words_joined = insult_words.iter().take(2).copied().collect::<Vec<_>>().join(", ");
        result.reason = format!("личное оскорбление: {words_joined}");
        result.context_adjustment = context_adjustment;

        if constructive_score > 0.3 {
            result.is_criticism = true;
            result.is_constructive = true;
            result.target = "mixed".to_string();
            result.reason.push_str(" (смешанный: оскорбление + критика)");
            result.confidence = 0.75;
        }
        return result;
    }

    if intelligence_score > 0.3 {
        result.is_insult = true;
        result.is_criticism = false;
        result.severity = (intelligence_score * 0.8 + aggression_boost * 0.5 + context_adjustment).min(1.0);
        result.confidence = (0.4 + intelligence_score * 0.4 + context_adjustment * 0.2).min(1.0);
        result.target = "intelligence".to_string();
        let phrases_joined = intelligence_phrases.iter().take(2).copied().collect::<Vec<_>>().join(", ");
        result.reason = format!("оскорбление интеллекта: {phrases_joined}");
        result.context_adjustment = context_adjustment;
        return result;
    }

    if constructive_score > 0.4 {
        result.is_criticism = true;
        result.is_constructive = true;
        result.is_feedback = true;
        result.specificity = constructive_score.min(1.0);
        result.constructiveness = (constructive_score + 0.2).min(1.0);
        result.severity = 0.3 + context_adjustment * 0.2;
        result.confidence = (0.6 + constructive_score * 0.3).min(1.0);
        result.target = "action".to_string();
        result.suggested_improvement = suggestion.map(|s| s.to_string());
        result.reason = "конструктивная критика".to_string();
        result.context_adjustment = context_adjustment;
        return result;
    }

    if neutral_score > 0.3 {
        result.is_criticism = true;
        result.is_constructive = false;
        result.is_feedback = true;
        result.specificity = neutral_score.min(1.0);
        result.constructiveness = 0.2;
        result.severity = 0.2 + context_adjustment * 0.15;
        result.confidence = (0.5 + neutral_score * 0.3).min(1.0);
        result.target = "work".to_string();
        result.reason = "нейтральная критика".to_string();
        result.context_adjustment = context_adjustment;
        return result;
    }

    if text_lower.contains("ошибк") || text_lower.contains("неправильн") || text_lower.contains("неверн") {
        result.is_criticism = false;
        result.is_feedback = true;
        result.specificity = 0.4;
        result.confidence = 0.5;
        result.target = "work".to_string();
        result.reason = "указание на ошибку".to_string();
        result.context_adjustment = context_adjustment;
        return result;
    }

    result.confidence = 0.8;
    result.reason = "нейтральное высказывание".to_string();
    result.context_adjustment = context_adjustment;
    result
}

/// agent/criticism_detector.py::CriticismDetector.get_response_template — возвращает
/// (strategy, tone, template).
pub fn get_response_template(
    is_insult: bool,
    target: &str,
    is_constructive: bool,
    is_criticism: bool,
    is_feedback: bool,
    trust: f64,
    irritation: f64,
    forgiveness: f64,
) -> (&'static str, &'static str, &'static str) {
    if is_insult {
        if target == "mixed" {
            return (
                "insult_with_criticism",
                "firm",
                "Я слышу, что ты указываешь на проблему. Но тон мне неприятен. Давай без оскорблений.",
            );
        }
        if irritation > 60.0 && trust < 30.0 {
            return (
                "insult_repeat",
                "cold",
                "Мне не нравится, когда меня оскорбляют. Я не буду продолжать этот разговор в таком тоне.",
            );
        }
        if forgiveness < 30.0 {
            return (
                "insult_not_forgiven",
                "hurt",
                "Ты уже оскорблял меня раньше. Я не забыла. Если хочешь нормального разговора — извинись.",
            );
        }
        return (
            "insult_first",
            "calm",
            "Мне неприятно слышать оскорбления. Я готова обсуждать идеи, но не в таком тоне.",
        );
    }

    if is_constructive {
        if trust > 60.0 {
            return (
                "constructive_criticism_trusted",
                "warm",
                "Спасибо за конструктивную критику. Я ценю, что ты указываешь на ошибки — это помогает мне расти.",
            );
        }
        return ("constructive_criticism", "neutral", "Поняла. Спасибо за уточнение, я перепроверю.");
    }

    if is_criticism {
        return ("neutral_criticism", "neutral", "Я учту это замечание.");
    }

    if is_feedback {
        return ("feedback", "open", "Спасибо за обратную связь.");
    }

    ("neutral", "neutral", "Я готова продолжить разговор.")
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

fn get_f64(d: &Bound<'_, PyDict>, key: &str, default: f64) -> PyResult<f64> {
    match d.get_item(key)? {
        Some(v) => v.extract::<f64>(),
        None => Ok(default),
    }
}

#[pyfunction]
#[pyo3(name = "analyze", signature = (text, context=None))]
fn py_analyze<'py>(py: Python<'py>, text: &str, context: Option<&Bound<'py, PyDict>>) -> PyResult<Bound<'py, PyDict>> {
    let (trust, history_insults) = match context {
        Some(d) => (get_f64(d, "trust", 50.0)?, get_f64(d, "history_insults", 0.0)?),
        None => (50.0, 0.0),
    };
    let r = analyze(text, trust, history_insults);
    let d = PyDict::new_bound(py);
    d.set_item("is_criticism", r.is_criticism)?;
    d.set_item("is_insult", r.is_insult)?;
    d.set_item("is_constructive", r.is_constructive)?;
    d.set_item("is_feedback", r.is_feedback)?;
    d.set_item("specificity", r.specificity)?;
    d.set_item("constructiveness", r.constructiveness)?;
    d.set_item("severity", r.severity)?;
    d.set_item("target", r.target)?;
    d.set_item("suggested_improvement", r.suggested_improvement)?;
    d.set_item("confidence", r.confidence)?;
    d.set_item("reason", r.reason)?;
    d.set_item("context_adjustment", r.context_adjustment)?;
    Ok(d)
}

#[pyfunction]
#[pyo3(name = "get_response_template", signature = (is_insult, target, is_constructive, is_criticism, is_feedback, context=None))]
fn py_get_response_template<'py>(
    py: Python<'py>,
    is_insult: bool,
    target: &str,
    is_constructive: bool,
    is_criticism: bool,
    is_feedback: bool,
    context: Option<&Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyDict>> {
    let (trust, irritation, forgiveness) = match context {
        Some(d) => (get_f64(d, "trust", 50.0)?, get_f64(d, "irritation", 10.0)?, get_f64(d, "forgiveness", 50.0)?),
        None => (50.0, 10.0, 50.0),
    };
    let (strategy, tone, template) =
        get_response_template(is_insult, target, is_constructive, is_criticism, is_feedback, trust, irritation, forgiveness);
    let d = PyDict::new_bound(py);
    d.set_item("strategy", strategy)?;
    d.set_item("tone", tone)?;
    d.set_item("template", template)?;
    Ok(d)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_analyze, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_response_template, m)?)?;
    Ok(())
}

// ── Юнит-тесты (сценарии из agent/criticism_detector.py's __main__ + прочитанные правила) ───
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_insult() {
        let r = analyze("ты глупая", 50.0, 0.0);
        assert!(r.is_insult);
        assert!(!r.is_criticism);
        assert_eq!(r.target, "personality");
    }

    #[test]
    fn intelligence_insult() {
        let r = analyze("ты ничего не понимаешь", 50.0, 0.0);
        assert!(r.is_insult);
        assert_eq!(r.target, "intelligence");
    }

    #[test]
    fn constructive_with_suggestion() {
        let r = analyze("ты ошиблась в расчётах, попробуй перепроверить", 50.0, 0.0);
        assert!(r.is_criticism);
        assert!(r.is_constructive);
        assert_eq!(r.suggested_improvement.as_deref(), Some("ошибка в расчётах"));
    }

    #[test]
    fn mixed_insult_and_criticism() {
        // "перепроверь" один не даёт constructive_score>0.3 (в CONSTRUCTIVE_PATTERNS есть
        // именно "стоит перепроверить") — сверено с реальным Python перед тем, как писать
        // тест, а не угадано по примеру из __main__ (там результат не печатался в виде
        // проверяемого утверждения).
        let r = analyze("ты дура, но стоит перепроверить расчёты", 50.0, 0.0);
        assert!(r.is_insult);
        assert!(r.is_criticism);
        assert_eq!(r.target, "mixed");
    }

    #[test]
    fn neutral_statement() {
        let r = analyze("привет, как дела", 50.0, 0.0);
        assert!(!r.is_insult && !r.is_criticism && !r.is_feedback);
        assert_eq!(r.reason, "нейтральное высказывание");
    }

    #[test]
    fn repeat_offender_and_low_trust_increase_context_adjustment() {
        let baseline = analyze("нейтральный текст без ничего особенного", 50.0, 0.0);
        let escalated = analyze("нейтральный текст без ничего особенного", 20.0, 3.0);
        assert!(escalated.context_adjustment > baseline.context_adjustment);
    }

    #[test]
    fn response_template_insult_repeat_when_angry_and_distrustful() {
        let (strategy, ..) = get_response_template(true, "personality", false, false, false, 20.0, 70.0, 50.0);
        assert_eq!(strategy, "insult_repeat");
    }

    #[test]
    fn response_template_mixed_takes_priority() {
        let (strategy, ..) = get_response_template(true, "mixed", true, true, false, 20.0, 70.0, 10.0);
        assert_eq!(strategy, "insult_with_criticism");
    }
}
