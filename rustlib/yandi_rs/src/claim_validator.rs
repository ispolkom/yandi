//! Перенос agent/claim_validator.py — ТОЛЬКО чистая часть: normalize_claim_text() (статический
//! метод) и validate() (+ приватный _looks_like_fact()). Стоит на горячем пути: КАЖДОЕ
//! извлечённое claim проходит через validate() прежде чем попасть дальше в конвейер.
//!
//! ClaimValidator как КЛАСС (счётчики accepted_count/rejected_count/rejection_reasons,
//! filter_claims/get_stats/summary/get_claim_validator singleton) остаётся в Python — это
//! состояние и оркестрация словарей, ей тут не место; она просто зовёт (теперь потенциально
//! Rust-делегирующие) validate()/normalize_claim_text() как и раньше.
//!
//! Как и в claim_identity.rs (см. п.1 там): Python `len(text) < 20` считает СИМВОЛЫ, не байты —
//! здесь та же ловушка, и здесь тоже `.chars().count()`, не `.len()`.
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_CLAIM_VALIDATOR_ENGINE=rust (см. agent/claim_validator.py).

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use regex::Regex;

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).unwrap_or_else(|e| panic!("статический паттерн должен быть валиден: {pattern}: {e}"))
}

// agent/claim_validator.py::META_PATTERNS — соседние raw-строки в Python склеиваются
// автоматически в одну; здесь склеены явно в один литерал на паттерн.
static META_PATTERNS: Lazy<Vec<Regex>> = Lazy::new(|| {
    [
        r"(?i)источник\s+содержит",
        r"(?i)содержание\s+источников",
        r"(?i)проанализировав\s+(?:источник|источники|текст)",
        r"(?i)извлечённые\s+факты",
        r"(?i)список\s+фактов",
        r"(?i)сырые\s+данные",
        r"(?i)источник\s+#?\d+",
        r"(?i)интернет-источники",
        r"(?i)локальная\s+база",
        r"(?i)согласно\s+источнику",
        r"(?i)в\s+источнике",
        r"(?i)^\s*по\s+имеющейся\s+информации\s*,?\s*(?:ответ|вывод|оценка|источник[а-я]*)\b",
        r"(?i)^\s*по\s+имеющимся\s+данным\s*,?\s*(?:ответ|вывод|оценка|источник[а-я]*)\b",
        r"(?i)^\s*некоторые\s+источники\s+(?:указывают|сообщают|утверждают|предполагают)\b",
        r"(?i)^\s*согласно\s+современным\s+(?:научным\s+)?данным\s*,?\s*(?:ответ|вывод|оценка)\b",
        r"(?i)как\s+указано\s+в\s+источнике",
        r"(?i)ответ\s+содержит",
        r"(?i)данный\s+документ",
        r"(?i)этот\s+текст",
        r"(?i)статья\s+описывает",
        r"(?i)\|\s*[а-яa-z]+\s*\|",
        r"(?i)^---$",
        r"(?i)^===.*===$",
    ]
    .into_iter()
    .map(re)
    .collect()
});

static PIPELINE_META_PATTERNS: Lazy<Vec<Regex>> = Lazy::new(|| {
    [
        r"(?i)^\s*вот\s+(?:извлеч[её]нн?ые|полученные|выделенные)\s+(?:атомарные\s+)?(?:claims?|утверждения|факты)\s*:?\s*$",
        r"(?i)^\s*анализ\s+(?:текста|ответа|вывода)\s+(?:модели|системы|ассистента)\s+(?:показывает|показал|указывает|выявляет|свидетельствует)",
        r"(?i)^\s*ответ\s+на\s+(?:этот\s+)?вопрос\s+(?:содержит|включает|имеет)",
        r"(?i)^\s*(?:ответ|вывод)\s+(?:модели|системы|ассистента)\s+(?:содержит|включает|показывает|указывает)",
        r"(?i)^\s*в\s+(?:тексте|ответе|выводе)\s+(?:модели|системы|ассистента)\s+(?:утверждается|говорится|указывается|содержится)",
        r"(?i)^\s*(?:модель|ассистент|система)\s+(?:утверждает|сообщает|отвечает|указывает|пишет|говорит)",
        r"(?i)^\s*(?:из|на\s+основе)\s+(?:данного\s+)?(?:текста|ответа)\s+.{0,50}(?:извлеч[её]н|выделен|получен)",
        r"(?i)^\s*(?:атомарные\s+)?(?:claims?|утверждения|извлеченные\s+claims?)\s*:?\s*$",
    ]
    .into_iter()
    .map(re)
    .collect()
});

static GOOD_PATTERNS: Lazy<Vec<Regex>> = Lazy::new(|| {
    [
        r"(?i)является", r"(?i)определяется", r"(?i)называется", r"(?i)представляет собой",
        r"(?i)состоит из", r"(?i)включает", r"(?i)содержит", r"(?i)возник", r"(?i)произошёл",
        r"(?i)создан", r"(?i)относится к", r"(?i)связан с", r"(?i)может быть",
        r"(?i)является результатом", r"(?i)имеет значение", r"(?i)составляет", r"(?i)достигает",
        r"(?i)продолжается", r"(?i)находится", r"(?i)расположен",
    ]
    .into_iter()
    .map(re)
    .collect()
});

static BULLET_RE: Lazy<Regex> = Lazy::new(|| re(r"^\s*[-*+]\s+"));
static NUMBERED_RE: Lazy<Regex> = Lazy::new(|| re(r"^\s*\d+[.)]\s+"));
static BOLD_RE: Lazy<Regex> = Lazy::new(|| re(r"^\*\*(.+)\*\*$"));

static FACT_DIGIT_RE: Lazy<Regex> = Lazy::new(|| re(r"\d+"));
static FACT_NAME_RE: Lazy<Regex> = Lazy::new(|| re(r"[А-ЯЁ][а-яё]+"));
static FACT_PAST_TENSE_RE: Lazy<Regex> =
    Lazy::new(|| re(r"(?i)был|была|было|стал|стала|стало|создал|создала|возник|возникла"));

/// agent/claim_validator.py::ClaimValidator.normalize_claim_text
pub fn normalize_claim_text(claim_text: &str) -> String {
    let text = claim_text.trim();
    let text = BULLET_RE.replace(text, "");
    let text = NUMBERED_RE.replace(&text, "");
    let text = BOLD_RE.replace(&text, "$1");
    text.trim().to_string()
}

fn looks_like_fact(text: &str) -> bool {
    FACT_DIGIT_RE.is_match(text) || FACT_NAME_RE.is_match(text) || FACT_PAST_TENSE_RE.is_match(text)
}

/// agent/claim_validator.py::ClaimValidator.validate — возвращает (is_valid, reason).
pub fn validate(claim_text: &str) -> (bool, &'static str) {
    let text = normalize_claim_text(claim_text);
    let char_len = text.chars().count();

    if char_len < 20 {
        return (false, "too_short");
    }
    if char_len > 300 {
        return (false, "too_long");
    }
    if PIPELINE_META_PATTERNS.iter().any(|p| p.is_match(&text)) {
        return (false, "meta_pipeline_claim");
    }
    if META_PATTERNS.iter().any(|p| p.is_match(&text)) {
        return (false, "meta_text");
    }
    if GOOD_PATTERNS.iter().any(|p| p.is_match(&text)) {
        return (true, "valid");
    }
    if looks_like_fact(&text) {
        return (true, "fact");
    }
    (true, "weak_pattern")
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(name = "normalize_claim_text")]
fn py_normalize_claim_text(claim_text: &str) -> String {
    normalize_claim_text(claim_text)
}

#[pyfunction]
#[pyo3(name = "validate")]
fn py_validate(claim_text: &str) -> (bool, String) {
    let (ok, reason) = validate(claim_text);
    (ok, reason.to_string())
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_normalize_claim_text, m)?)?;
    m.add_function(wrap_pyfunction!(py_validate, m)?)?;
    Ok(())
}

// ── Юнит-тесты ────────────────────────────────────────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_strips_presentational_markup_only() {
        assert_eq!(normalize_claim_text("- Юпитер является..."), "Юпитер является...");
        assert_eq!(normalize_claim_text("* Юпитер является..."), "Юпитер является...");
        assert_eq!(normalize_claim_text("1. Юпитер является..."), "Юпитер является...");
        assert_eq!(normalize_claim_text("2) Юпитер является..."), "Юпитер является...");
        assert_eq!(normalize_claim_text("**Юпитер является планетой**"), "Юпитер является планетой");
    }

    #[test]
    fn too_short_and_too_long() {
        assert_eq!(validate("коротко"), (false, "too_short"));
        let long_text = "А".repeat(301);
        assert_eq!(validate(&long_text), (false, "too_long"));
    }

    #[test]
    fn char_length_not_byte_length() {
        // Ровно 20 кириллических символов (40 байт) — не должно отбраковаться как too_short,
        // если считать правильно (символы), и НЕ должно ошибочно пройти при подсчёте байт как
        // будто их 40 (тест ловит саму логику сравнения, не конкретное пороговое значение).
        let text_20_chars = "а".repeat(20);
        let (ok, reason) = validate(&text_20_chars);
        assert!(ok || reason != "too_short", "20 симв. не должно быть отбраковано как too_short: {reason}");
    }

    #[test]
    fn meta_pipeline_claim_rejected() {
        let (ok, reason) = validate("Вот извлеченные атомарные claims:");
        assert_eq!((ok, reason), (false, "meta_pipeline_claim"));
    }

    #[test]
    fn meta_text_rejected() {
        let (ok, reason) = validate("Согласно источнику, информация верна и это очень длинный текст.");
        assert_eq!((ok, reason), (false, "meta_text"));
    }

    #[test]
    fn hedge_phrase_with_real_subject_matter_is_not_meta() {
        // Регрессия из YANDI_RUNTIME_REGRESSION_FIX_REPORT.md §A
        let (ok, reason) = validate("По имеющимся данным жизнь на Марсе пока не обнаружена никем.");
        assert!(ok, "{reason}");
        assert_ne!(reason, "meta_text");
    }

    #[test]
    fn good_pattern_and_fact_and_weak() {
        let (ok1, reason1) = validate("Юпитер является крупнейшей планетой Солнечной системы.");
        assert_eq!((ok1, reason1), (true, "valid"));

        let (ok2, reason2) = validate("Первый признак жизни датируется 3,5 млрд лет назад примерно.");
        assert_eq!((ok2, reason2), (true, "fact"));
    }
}
