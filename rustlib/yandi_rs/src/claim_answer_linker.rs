//! Перенос agent/claim_answer_linker.py — _extract_key_phrases() и _is_claim_supporting() (ядро
//! link_answer_to_claims()). Связывает финальный ответ с claims, которые его подкрепляют —
//! источник supporting_claim_ids в трейсе.
//!
//! link_answer_to_claims() как публичная PyO3-функция принимает список Python dict напрямую (не
//! через сериализацию в JSON, как в message_intensity.rs) — claim_id должен вернуться В ТОЧНОСТИ
//! тем же Python-объектом, что был в исходном dict (Python `claim.get("claim_id")` не приводит
//! тип; если id — число, а не строка, оно и останется числом, а если ключа нет вовсе — None).
//! ClaimAnswerLinker как класс (linked_claims, get_summary, enrich_trace) НЕ перенесён — это
//! тривиальная оркестрация состояния, не чистая логика.
//!
//! Python `len(sent) > 20` и `sent[:50]` — символы (code points), не байты; та же ловушка, что в
//! claim_identity.rs/claim_validator.rs/boundaries.rs — учтена сразу (`.chars()`, не `.len()`).
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_CLAIM_ANSWER_LINKER_ENGINE=rust (см. agent/claim_answer_linker.py).

use crate::py_text::PyLowerExt;
use once_cell::sync::Lazy;
#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::{PyDict, PyList};
use regex::Regex;

static SENTENCE_SPLIT_RE: Lazy<Regex> = Lazy::new(|| crate::py_text::py_regex(r"[.!?]\s+"));

/// agent/claim_answer_linker.py::ClaimAnswerLinker._extract_key_phrases
pub fn extract_key_phrases(text: &str) -> Vec<String> {
    let sentences: Vec<&str> = SENTENCE_SPLIT_RE.split(text).collect();
    let mut phrases = Vec::new();
    for sent in sentences.iter().take(5) {
        let trimmed = crate::py_text::py_strip(sent);
        if trimmed.chars().count() > 20 {
            let head: String = trimmed.chars().take(50).collect();
            phrases.push(head.py_lowercase());
        }
    }
    phrases
}

/// agent/claim_answer_linker.py::ClaimAnswerLinker._is_claim_supporting
pub fn is_claim_supporting(claim_text: &str, key_phrases: &[String]) -> bool {
    let claim_lower = claim_text.py_lowercase();
    for phrase in key_phrases {
        let words: Vec<&str> = crate::py_text::py_split_whitespace(phrase).take(5).collect();
        let word_match = words.iter().filter(|w| claim_lower.contains(*w)).count();
        // Дословно: если words пуст (0 >= 0*0.4 = 0.0 -> true), claim засчитывается как
        // поддерживающий — сохранено как в оригинале, не "исправлено" защитной проверкой.
        if (word_match as f64) >= (words.len() as f64) * 0.4 {
            return true;
        }
    }
    false
}

#[cfg(feature = "python")]
/// agent/claim_answer_linker.py::ClaimAnswerLinker.link_answer_to_claims — возвращает (answer,
/// supporting_claim_ids). Чистая логика (extract_key_phrases + is_claim_supporting) через
/// PyO3-функцию ниже, работающую напрямую со списком Python dict, чтобы claim_id вернулся тем
/// же объектом, что был на входе (не строкой-по-умолчанию).
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_extract_key_phrases, m)?)?;
    m.add_function(wrap_pyfunction!(py_is_claim_supporting, m)?)?;
    m.add_function(wrap_pyfunction!(py_link_answer_to_claims, m)?)?;
    Ok(())
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "extract_key_phrases")]
fn py_extract_key_phrases(text: &str) -> Vec<String> {
    extract_key_phrases(text)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "is_claim_supporting")]
fn py_is_claim_supporting(claim_text: &str, key_phrases: Vec<String>) -> bool {
    is_claim_supporting(claim_text, &key_phrases)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "link_answer_to_claims")]
fn py_link_answer_to_claims<'py>(
    py: Python<'py>,
    answer: &str,
    claims: &Bound<'py, PyList>,
) -> PyResult<(String, Vec<PyObject>)> {
    if claims.is_empty() {
        return Ok((answer.to_string(), Vec::new()));
    }
    let phrases = extract_key_phrases(answer);
    let mut supporting: Vec<PyObject> = Vec::new();
    for item in claims.iter() {
        let dict = item.downcast::<PyDict>()?;
        let claim_text: String = match dict.get_item("claim_text")? {
            Some(v) => v.extract().unwrap_or_default(),
            None => String::new(),
        };
        if is_claim_supporting(&claim_text, &phrases) {
            let claim_id = match dict.get_item("claim_id")? {
                Some(v) => v.unbind(),
                None => py.None(),
            };
            supporting.push(claim_id);
        }
    }
    Ok((answer.to_string(), supporting))
}

// ── Юнит-тесты ────────────────────────────────────────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extract_phrases_basic() {
        let text = "Сознание определяется как способность к субъективному восприятию. Оно возникает из активности нейронных сетей. Современная наука изучает его через функциональную роль в принятии решений.";
        let phrases = extract_key_phrases(text);
        assert_eq!(phrases.len(), 3);
        assert!(phrases[0].starts_with("сознание определяется"));
    }

    #[test]
    fn extract_phrases_skips_short_sentences() {
        let text = "Да. Нет. Это достаточно длинное предложение, длиннее двадцати символов точно.";
        let phrases = extract_key_phrases(text);
        assert_eq!(phrases.len(), 1);
    }

    #[test]
    fn extract_phrases_takes_first_five_only() {
        let text = "Одно предложение длиннее двадцати символов. Два предложение длиннее двадцати символов. Три предложение длиннее двадцати символов. Четыре предложение длиннее двадцати символов. Пять предложение длиннее двадцати символов. Шесть предложение длиннее двадцати символов игнорируется.";
        let phrases = extract_key_phrases(text);
        assert_eq!(phrases.len(), 5);
    }

    #[test]
    fn phrase_capped_at_50_chars() {
        let long_sentence = "А".repeat(100) + ".";
        let phrases = extract_key_phrases(&long_sentence);
        assert_eq!(phrases[0].chars().count(), 50);
    }

    #[test]
    fn claim_supporting_word_overlap() {
        let phrases = extract_key_phrases("Сознание определяется как способность к субъективному восприятию.");
        assert!(is_claim_supporting("Сознание определяется как способность к субъективному восприятию", &phrases));
        assert!(!is_claim_supporting("Совершенно другой текст про планеты и звёзды далёкие", &phrases));
    }

    #[test]
    fn empty_words_edge_case_counts_as_supporting() {
        // Дословное поведение оригинала: пустой words-список после split -> 0 >= 0.0 -> true.
        // Сконструировать phrase, дающий пустой words после split_whitespace, напрямую:
        assert!(is_claim_supporting("любой текст", &["".to_string()]));
    }
}
