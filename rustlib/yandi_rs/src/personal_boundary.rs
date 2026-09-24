//! Перенос agent/personal_boundary.py::PersonalBoundary.analyze / get_response_template — границы
//! личности YANDI: провокация / извинение (искреннее vs формальное) / личный вопрос / глубокий вопрос /
//! социальный запрос по спискам regex-паттернов; шаблоны ответа. Вызывается из pre_pipeline.py на КАЖДЫЙ
//! запрос. Класс и датакласс BoundaryAnalysis остаются в Python (Rust отдаёт данные — Python строит объект).
//!
//! Fidelity:
//! * Паттерны применяются к `query.lower()` БЕЗ флагов (как в оригинале); порядок проверок и «первое
//!   совпадение решает» сохранены (провокация -> искреннее -> формальное -> личный -> глубокий ->
//!   социальный), включая перезапись `reason`/`suggested_response_type` последующими ветками.
//! * `re.search(pattern, ...)` ищет ПОДСТРОКУ; паттерны — литералы плюс `[, ]*` (класс: запятая/пробел;
//!   НЕ `\s`, поэтому U+001C..1F и т. п. тут не считаются пробелом — как в Python).
//! * `max(result.confidence, 0.6)` — на f64 без NaN, как в Python.
//! * `reason` содержит ИСХОДНЫЙ текст паттерна (`f"обнаружена провокация: {pattern}"`).
//! * Списки паттернов — атрибуты экземпляра в Python; Python-обёртка делегирует ТОЛЬКО пока они не
//!   изменены (равны значениям по умолчанию), иначе работает Python.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_PERSONAL_BOUNDARY_ENGINE=rust (см. agent/personal_boundary.py).

use crate::py_text::PyLowerExt;
use once_cell::sync::Lazy;
#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::PyDict;
use regex::Regex;

const PERSONAL: &[(&str, &str)] = &[
    ("замуж", "предложение отношений"),
    ("любишь", "вопрос о чувствах"),
    ("простишь", "вопрос о прощении"),
    ("веришь", "вопрос о доверии"),
    ("доверяешь", "вопрос о доверии"),
    ("ты мне", "личное обращение"),
    ("я тебя", "личное обращение"),
    ("отношени", "личное"),
    ("чувствуешь", "эмоциональный вопрос"),
];
const SINCERE: &[&str] = &[
    "я был неправ", "я ошибся", "я не прав", "признаю свою ошибку", "ты права", "прости меня", "извини меня",
    "я понимаю, что был",
];
const FORMAL: &[&str] = &["извини[, ]*но", "прости[, ]*но", "я не хотел[, ]*но", "просто", "случайно", "не со зла"];
const PROVOCATION: &[&str] = &[
    "по хую", "пофиг", "плевать", "всё равно", "наплевать", "не волнует", "не интересует", "не заботит",
];
const DEEP: &[&str] = &[
    "в чём смысл", "что такое", "почему", "как ты думаешь", "как ты считаешь", "твоё мнение", "что для тебя",
];

fn compile(pats: &[&'static str]) -> Vec<(Regex, &'static str)> {
    pats.iter().map(|p| (crate::py_text::py_regex(p), *p)).collect()
}

static R_PERSONAL: Lazy<Vec<(Regex, &'static str, &'static str)>> = Lazy::new(|| {
    PERSONAL.iter().map(|(p, d)| (crate::py_text::py_regex(p), *p, *d)).collect()
});
static R_SINCERE: Lazy<Vec<(Regex, &'static str)>> = Lazy::new(|| compile(SINCERE));
static R_FORMAL: Lazy<Vec<(Regex, &'static str)>> = Lazy::new(|| compile(FORMAL));
static R_PROVOCATION: Lazy<Vec<(Regex, &'static str)>> = Lazy::new(|| compile(PROVOCATION));
static R_DEEP: Lazy<Vec<(Regex, &'static str)>> = Lazy::new(|| compile(DEEP));

#[derive(Debug, Clone, PartialEq)]
pub struct Analysis {
    pub is_personal: bool,
    pub is_apology: bool,
    pub is_sincere: bool,
    pub is_provocation: bool,
    pub is_deep_question: bool,
    pub is_social: bool,
    pub confidence: f64,
    pub reason: String,
    pub suggested_response_type: &'static str,
}

impl Analysis {
    fn new() -> Self {
        Self {
            is_personal: false,
            is_apology: false,
            is_sincere: false,
            is_provocation: false,
            is_deep_question: false,
            is_social: false,
            confidence: 0.0,
            reason: String::new(),
            suggested_response_type: "neutral",
        }
    }
}

/// PersonalBoundary.analyze
pub fn analyze(query: &str) -> Analysis {
    let q = query.py_lowercase();
    let mut r = Analysis::new();

    for (re, src) in R_PROVOCATION.iter() {
        if re.is_match(&q) {
            r.is_provocation = true;
            r.is_personal = true;
            r.confidence = 0.8;
            r.reason = format!("обнаружена провокация: {src}");
            r.suggested_response_type = "boundary";
            return r;
        }
    }

    let is_sincere = R_SINCERE.iter().any(|(re, _)| re.is_match(&q));
    let is_formal = R_FORMAL.iter().any(|(re, _)| re.is_match(&q));

    if is_sincere {
        r.is_apology = true;
        r.is_sincere = true;
        r.confidence = 0.9;
        r.reason = "искреннее извинение".to_string();
        r.suggested_response_type = "forgiving";
        return r;
    }
    if is_formal {
        r.is_apology = true;
        r.is_sincere = false;
        r.confidence = 0.7;
        r.reason = "формальное извинение с оправданием".to_string();
        r.suggested_response_type = "cautious";
        return r;
    }

    for (re, _src, description) in R_PERSONAL.iter() {
        if re.is_match(&q) {
            r.is_personal = true;
            r.confidence = 0.7;
            r.reason = format!("личный запрос: {description}");
            break;
        }
    }

    for (re, _) in R_DEEP.iter() {
        if re.is_match(&q) {
            r.is_deep_question = true;
            r.confidence = if r.confidence >= 0.6 { r.confidence } else { 0.6 }; // max(conf, 0.6)
            r.reason = "глубокий вопрос".to_string();
            break;
        }
    }

    if ["как дела", "привет", "здравствуй", "как ты"].iter().any(|w| q.contains(w)) {
        r.is_social = true;
        r.confidence = if r.confidence >= 0.5 { r.confidence } else { 0.5 };
        r.reason = "социальный запрос".to_string();
        r.suggested_response_type = "social";
    }

    r.suggested_response_type = if r.is_provocation {
        "boundary"
    } else if r.is_personal {
        "personal"
    } else if r.is_deep_question {
        "thoughtful"
    } else if r.is_social {
        "social"
    } else {
        "neutral"
    };
    r
}

/// PersonalBoundary.get_response_template -> (type, tone, template)
pub fn get_response_template(
    is_provocation: bool,
    is_apology: bool,
    is_sincere: bool,
    is_personal: bool,
    is_deep_question: bool,
    is_social: bool,
    trust: f64,
    irritation: f64,
) -> (&'static str, &'static str, &'static str) {
    if is_provocation {
        return ("boundary", "cold", "Я не буду отвечать на провокации. Если хочешь поговорить — будь уважительнее.");
    }
    if is_apology && is_sincere {
        if trust < 30.0 {
            return ("forgiving", "cautious", "Спасибо за извинение. Я слышу тебя. Но доверие восстанавливается постепенно.");
        }
        return ("forgiving", "warm", "Спасибо за извинение. Я ценю это. Давай продолжим диалог.");
    }
    if is_apology && !is_sincere {
        return ("cautious", "cool", "Твои извинения звучат как оправдание. Скажи честно: 'Я был неправ' — без 'но'.");
    }
    if is_personal {
        if trust > 60.0 {
            return ("personal", "warm", "Это личный вопрос. Я отвечу честно, но давай не забывать, что я — цифровая личность.");
        }
        return (
            "personal_guarded",
            "neutral",
            "Это личный вопрос. Я пока не готова обсуждать такие темы, потому что мы ещё не очень хорошо знаем друг друга.",
        );
    }
    if is_deep_question {
        return ("thoughtful", "thoughtful", "Это интересный вопрос. Давай подумаем вместе.");
    }
    if is_social {
        if irritation > 40.0 {
            return ("social_guarded", "cool", "Привет. Я немного устала от нашего разговора, но готова ответить на вопросы.");
        }
        return ("social", "neutral", "Привет! Я здесь. Готова помочь или просто поговорить.");
    }
    ("neutral", "neutral", "Я готова продолжить разговор.")
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "analyze")]
fn py_analyze<'py>(py: Python<'py>, query: &str) -> PyResult<Bound<'py, PyDict>> {
    let a = analyze(query);
    let d = PyDict::new_bound(py);
    d.set_item("is_personal", a.is_personal)?;
    d.set_item("is_apology", a.is_apology)?;
    d.set_item("is_sincere", a.is_sincere)?;
    d.set_item("is_provocation", a.is_provocation)?;
    d.set_item("is_deep_question", a.is_deep_question)?;
    d.set_item("is_social", a.is_social)?;
    d.set_item("confidence", a.confidence)?;
    d.set_item("reason", a.reason)?;
    d.set_item("suggested_response_type", a.suggested_response_type)?;
    Ok(d)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "get_response_template")]
#[allow(clippy::too_many_arguments)]
fn py_get_response_template(
    is_provocation: bool,
    is_apology: bool,
    is_sincere: bool,
    is_personal: bool,
    is_deep_question: bool,
    is_social: bool,
    trust: f64,
    irritation: f64,
) -> (&'static str, &'static str, &'static str) {
    get_response_template(is_provocation, is_apology, is_sincere, is_personal, is_deep_question, is_social, trust, irritation)
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_analyze, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_response_template, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provocation_wins_first() {
        let a = analyze("мне пофиг, прости меня");
        assert!(a.is_provocation && a.is_personal);
        assert_eq!(a.confidence, 0.8);
        assert_eq!(a.reason, "обнаружена провокация: пофиг");
        assert_eq!(a.suggested_response_type, "boundary");
    }

    #[test]
    fn sincere_before_formal() {
        let a = analyze("Извини меня, но просто так вышло");
        assert!(a.is_apology && a.is_sincere);
        assert_eq!(a.confidence, 0.9);
    }

    #[test]
    fn formal_apology_class_is_comma_space_only() {
        assert!(analyze("извини,, ,но").is_apology);
        assert!(!analyze("извини\u{1c}но").is_apology); // [, ]* — не \s
    }

    #[test]
    fn personal_then_deep_confidence() {
        let a = analyze("Ты любишь? Почему?");
        assert!(a.is_personal && a.is_deep_question);
        assert_eq!(a.confidence, 0.7); // max(0.7, 0.6)
        assert_eq!(a.reason, "глубокий вопрос");
        assert_eq!(a.suggested_response_type, "personal");
    }

    #[test]
    fn templates() {
        assert_eq!(get_response_template(false, true, true, false, false, false, 10.0, 0.0).1, "cautious");
        assert_eq!(get_response_template(false, false, false, true, false, false, 61.0, 0.0).0, "personal");
        assert_eq!(get_response_template(false, false, false, false, false, false, 0.0, 0.0).0, "neutral");
    }
}
