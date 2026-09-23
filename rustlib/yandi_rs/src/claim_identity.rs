//! Перенос agent/claim_identity.py — детерминированная текстовая идентичность утверждения
//! (canonicalize_claim_text, compute_claim_content_hash) и извлечение "якорей" темы
//! (extract_subject_anchors, extract_content_anchors). Всё здесь ЧИСТО (текст на входе, текст/
//! список на выходе, никакого I/O) и стоит на горячем пути: вызывается на КАЖДОЕ извлечённое
//! утверждение (дедупликация, семейства claim'ов, Subject Gate веб-источников).
//!
//! Важные несовпадения движков, на которые здесь явно обращено внимание (не общие слова "может
//! отличаться" — конкретные места, где naive-перенос был бы тихо неверен):
//!
//! 1. Python `len(tok) < 3` считает СИМВОЛЫ (code points); Rust `String::len()` считает БАЙТЫ.
//!    Кириллица — 2 байта на символ в UTF-8, поэтому `.len()` здесь была бы систематически
//!    неверна для русского текста. Используется `.chars().count()`.
//! 2. Python `re.search` с lookbehind/lookahead (`(?<!...)`, `(?!...)`) — стандартный крейт
//!    `regex` в Rust ЛУКАРАУНД НЕ ПОДДЕРЖИВАЕТ (намеренно, ради гарантии линейного времени).
//!    Вместо ещё одного крейта (`fancy-regex`) написан ручной посимвольный поиск границы слова
//!    (`boundary_search`) — тот же результат, без второго regex-движка.
//! 3. Python `str.casefold()` — полное Unicode-сворачивание регистра (сильнее `.lower()`);
//!    переносится крейтом `caseless`. Python `str.lower()` (используется в этом же модуле для
//!    `word.lower()`/`text.lower()`, НЕ `.casefold()`) — переносится Rust `str::to_lowercase()`.
//!    Это два РАЗНЫХ преобразования и в Python, и здесь — не перепутаны местами.
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_CLAIM_IDENTITY_ENGINE=rust (см. agent/claim_identity.py).

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use regex::Regex;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use unicode_normalization::UnicodeNormalization;

// agent/claim_identity.py::_SUBJECT_ANCHOR_ALIASES — Vec, НЕ HashMap: порядок должен совпасть
// с порядком объявления Python-словаря (Python 3.7+ dict сохраняет порядок вставки, и это
// напрямую влияет на порядок элементов в возвращаемом списке до дедупликации).
type AliasTable = &'static [(&'static str, &'static [&'static str])];

static SUBJECT_ANCHOR_ALIASES: AliasTable = &[
    ("юпитере", &["юпитер", "jupiter"]),
    ("юпитер", &["юпитер", "jupiter"]),
    ("европе", &["европа", "europa"]),
    ("европа", &["европа", "europa"]),
    ("сатурне", &["сатурн", "saturn"]),
    ("сатурн", &["сатурн", "saturn"]),
    ("венере", &["венера", "venus"]),
    ("венера", &["венера", "venus"]),
    ("марсе", &["марс", "mars"]),
    ("марс", &["марс", "mars"]),
    ("ес", &["ес", "евросоюз", "европейский союз", "eu", "european union"]),
    ("евросоюз", &["ес", "евросоюз", "европейский союз", "eu", "european union"]),
    ("европейский", &["ес", "евросоюз", "европейский союз", "eu", "european union"]),
    ("нато", &["нато", "nato"]),
    ("еврозона", &["еврозона", "eurozone"]),
];

static WHOLE_WORD_ONLY_KEYS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    ["юпитере", "европе", "сатурне", "венере", "марсе", "ес", "нато"]
        .into_iter()
        .collect()
});

static RETRIEVAL_FILLER_WORDS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    [
        "evidence", "observations", "observation", "data", "research", "mission", "spacecraft",
        "study", "studies", "measurements", "measurement", "confirmed", "confirmation",
        "contradictory", "discovery", "detection", "detected", "biosignature", "primary",
        "institutional", "direct", "counter", "source", "sources",
        "доказательство", "доказательства", "наблюдение", "наблюдения", "данные",
        "исследование", "исследования", "миссия", "измерения", "измерение", "подтверждено",
        "подтверждение", "противоречащие", "противоречие", "обнаружено", "обнаружение",
        "источник", "источники",
    ]
    .into_iter()
    .collect()
});

static RU_STOPWORDS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    [
        "и", "в", "во", "не", "на", "я", "с", "со", "а", "как", "то", "все", "она", "он", "оно",
        "они", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только",
        "ее", "её", "мне", "было", "вот", "от", "меня", "еще", "ещё", "нет", "о", "об", "из",
        "ему", "теперь", "когда", "даже", "ну", "вдруг", "ли", "если", "уже", "или", "ни",
        "быть", "был", "была", "были", "есть", "для", "что", "чем", "кто", "этот", "эта", "это",
        "эти", "тот", "та", "те", "какой", "какая", "какое", "какие", "особенно", "около",
    ]
    .into_iter()
    .collect()
});

static EN_STOPWORDS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    [
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "of", "to", "in",
        "on", "at", "for", "with", "about", "there", "this", "that", "these", "those", "it",
        "its", "and", "or", "not", "no", "does", "do", "did", "has", "have", "had", "what",
        "which", "who", "whom", "how", "why", "than",
    ]
    .into_iter()
    .collect()
});

static WORD_TOKEN_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"[A-Za-zА-Яа-яЁё0-9-]+").expect("статический паттерн валиден"));
static CAPITALIZED_WORD_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^[А-ЯЁA-Z][A-Za-zА-Яа-яЁё0-9-]+$").expect("статический паттерн валиден"));
static CONTENT_TOKEN_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"[A-Za-zА-Яа-яЁё-]+").expect("статический паттерн валиден"));

/// pet-style: символ входит в [a-zа-яё0-9] Python-паттернов границы слова здесь (нижний регистр,
/// т.к. вызывается только на уже приведённом к нижнему регистру тексте).
fn is_word_char(c: char) -> bool {
    c.is_ascii_lowercase() || c.is_ascii_digit() || ('а'..='я').contains(&c) || c == 'ё'
}

/// agent/claim_identity.py: ручная замена `(?<![a-zа-яё0-9])needle(?![a-zа-яё0-9])?` —见 модульный
/// docstring, п.2. Возвращает true, если `needle` встречается в `haystack` хоть раз с левой
/// границей не-словом (и, если whole_word, тоже с правой).
fn boundary_search(haystack: &str, needle: &str, whole_word: bool) -> bool {
    if needle.is_empty() {
        return false;
    }
    for (byte_pos, _) in haystack.char_indices() {
        if !haystack[byte_pos..].starts_with(needle) {
            continue;
        }
        let end = byte_pos + needle.len();
        let before_ok = haystack[..byte_pos]
            .chars()
            .next_back()
            .map(|c| !is_word_char(c))
            .unwrap_or(true);
        if !before_ok {
            continue;
        }
        let after_ok = if whole_word {
            haystack[end..].chars().next().map(|c| !is_word_char(c)).unwrap_or(true)
        } else {
            true
        };
        if after_ok {
            return true;
        }
    }
    false
}

fn alias_lookup(anchor: &str) -> &'static [&'static str] {
    SUBJECT_ANCHOR_ALIASES
        .iter()
        .find(|(k, _)| *k == anchor)
        .map(|(_, v)| *v)
        .unwrap_or(&[])
}

/// agent/claim_identity.py::canonicalize_claim_text
pub fn canonicalize_claim_text(claim_text: &str) -> String {
    if claim_text.is_empty() {
        return String::new();
    }
    let nfc: String = claim_text.nfc().collect();
    let folded = caseless::default_case_fold_str(&nfc);
    let collapsed = folded.split_whitespace().collect::<Vec<_>>().join(" ");
    let no_trailing_punct =
        collapsed.trim_end_matches(|c: char| c.is_whitespace() || matches!(c, '.' | '!' | '?' | '…'));
    no_trailing_punct.trim().to_string()
}

/// agent/claim_identity.py::compute_claim_content_hash
pub fn compute_claim_content_hash(claim_text: &str) -> Option<String> {
    let canonical = canonicalize_claim_text(claim_text);
    if canonical.is_empty() {
        return None;
    }
    let mut hasher = Sha256::new();
    hasher.update(canonical.as_bytes());
    Some(format!("{:x}", hasher.finalize()))
}

/// agent/claim_identity.py::extract_subject_anchors — stable dedup (первое вхождение решает
/// порядок), как Python `dict.fromkeys(...)`.
pub fn extract_subject_anchors(claim_text: &str) -> Vec<String> {
    let text = claim_text.trim();
    if text.is_empty() {
        return Vec::new();
    }

    let words: Vec<&str> = WORD_TOKEN_RE.find_iter(text).map(|m| m.as_str()).collect();

    let mut anchors: Vec<String> = Vec::new();
    for (i, word) in words.iter().enumerate() {
        if i == 0 {
            continue;
        }
        if CAPITALIZED_WORD_RE.is_match(word) {
            anchors.push(word.to_lowercase());
        }
    }

    let mut expanded: Vec<String> = Vec::new();
    for anchor in &anchors {
        expanded.push(anchor.clone());
        for alias in alias_lookup(anchor) {
            expanded.push((*alias).to_string());
        }
    }

    let claim_lower = text.to_lowercase();
    for (form, form_aliases) in SUBJECT_ANCHOR_ALIASES {
        let whole_word = WHOLE_WORD_ONLY_KEYS.contains(form);
        if boundary_search(&claim_lower, form, whole_word) {
            for alias in *form_aliases {
                expanded.push((*alias).to_string());
            }
        }
    }

    stable_dedup(expanded)
}

/// agent/claim_identity.py::extract_content_anchors
pub fn extract_content_anchors(text: &str) -> Vec<String> {
    let text = text.trim();
    if text.is_empty() {
        return Vec::new();
    }
    let lower = text.to_lowercase();

    let mut anchors: Vec<String> = Vec::new();
    for m in CONTENT_TOKEN_RE.find_iter(&lower) {
        let tok = m.as_str();
        if tok.chars().count() < 3 {
            continue;
        }
        if RU_STOPWORDS.contains(tok) || EN_STOPWORDS.contains(tok) {
            continue;
        }
        if RETRIEVAL_FILLER_WORDS.contains(tok) {
            continue;
        }
        anchors.push(tok.to_string());
    }
    stable_dedup(anchors)
}

fn stable_dedup(items: Vec<String>) -> Vec<String> {
    let mut seen: HashSet<String> = HashSet::new();
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        if seen.insert(item.clone()) {
            out.push(item);
        }
    }
    out
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(name = "canonicalize_claim_text")]
fn py_canonicalize_claim_text(claim_text: &str) -> String {
    canonicalize_claim_text(claim_text)
}

#[pyfunction]
#[pyo3(name = "compute_claim_content_hash")]
fn py_compute_claim_content_hash(claim_text: &str) -> Option<String> {
    compute_claim_content_hash(claim_text)
}

#[pyfunction]
#[pyo3(name = "extract_subject_anchors")]
fn py_extract_subject_anchors(claim_text: &str) -> Vec<String> {
    extract_subject_anchors(claim_text)
}

#[pyfunction]
#[pyo3(name = "extract_content_anchors")]
fn py_extract_content_anchors(text: &str) -> Vec<String> {
    extract_content_anchors(text)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_canonicalize_claim_text, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_claim_content_hash, m)?)?;
    m.add_function(wrap_pyfunction!(py_extract_subject_anchors, m)?)?;
    m.add_function(wrap_pyfunction!(py_extract_content_anchors, m)?)?;
    Ok(())
}

// ── Юнит-тесты (переносы сценариев из agent/epistemic_claim_identity_regression_test.py и
// agent/claim_evidence_bilingual_subject_gate_regression_test.py) ───────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn whitespace_collapses_to_same_canonical_text() {
        let a = "Юпитер   имеет    кольца.";
        let b = "  Юпитер\nимеет\tкольца.  ";
        assert_eq!(canonicalize_claim_text(a), canonicalize_claim_text(b));
    }

    #[test]
    fn nfc_and_nfd_hash_the_same() {
        let nfc = "Café was founded in 1976.".nfc().collect::<String>();
        // NFD составное разложение того же текста
        let nfd: String = nfc.nfd().collect();
        assert_ne!(nfc, nfd, "sanity: байтово действительно разные");
        assert_eq!(compute_claim_content_hash(&nfc), compute_claim_content_hash(&nfd));
    }

    #[test]
    fn case_differences_collapse() {
        assert_eq!(canonicalize_claim_text("ЮПИТЕР"), canonicalize_claim_text("юпитер"));
        assert_eq!(canonicalize_claim_text("Jupiter"), canonicalize_claim_text("JUPITER"));
    }

    #[test]
    fn trailing_punctuation_stripped_internal_preserved() {
        assert_eq!(canonicalize_claim_text("Привет, мир!"), canonicalize_claim_text("Привет, мир"));
        assert_eq!(canonicalize_claim_text("Что?!"), canonicalize_claim_text("Что"));
        // внутренняя запятая — часть смысла, не трогаем
        assert_ne!(canonicalize_claim_text("А, Б"), canonicalize_claim_text("А Б"));
    }

    #[test]
    fn empty_and_whitespace_only_have_no_hash() {
        assert_eq!(compute_claim_content_hash(""), None);
        assert_eq!(compute_claim_content_hash("   \n\t  "), None);
        assert_eq!(canonicalize_claim_text(""), "");
    }

    #[test]
    fn subject_anchor_jupiter() {
        let anchors = extract_subject_anchors("На Юпитере разумная жизнь не обнаружена");
        assert!(anchors.contains(&"юпитере".to_string()));
        assert!(anchors.contains(&"юпитер".to_string()));
        assert!(anchors.contains(&"jupiter".to_string()));
    }

    #[test]
    fn eu_whole_word_fix_does_not_false_positive() {
        // Регрессия из живого бага: "ес" не должно матчиться внутри "Если"/"Естественно"/"Есть"
        let anchors = extract_subject_anchors("Если посмотреть на это, естественно, есть нюанс.");
        assert!(!anchors.iter().any(|a| a == "ес" || a == "eu"), "{anchors:?}");
    }

    #[test]
    fn eu_and_jupiter_alias_groups_stay_separate() {
        let anchors = extract_subject_anchors("Европейский союз обсуждает бюджет.");
        assert!(anchors.iter().any(|a| a == "eu"));
        assert!(!anchors.iter().any(|a| a == "europa"), "{anchors:?}"); // не спутать с планетой/спутником
    }

    #[test]
    fn content_anchors_drop_stopwords_and_filler() {
        let ru = extract_content_anchors("Есть ли жизнь на Солнце?");
        assert_eq!(ru.iter().map(String::as_str).collect::<HashSet<_>>(), HashSet::from(["жизнь", "солнце"]));

        let en = extract_content_anchors("Is there life on the Sun?");
        assert_eq!(en.iter().map(String::as_str).collect::<HashSet<_>>(), HashSet::from(["life", "sun"]));

        let filler = extract_content_anchors(
            "primary evidence research study data observations confirmed detection",
        );
        assert!(filler.is_empty(), "{filler:?}");
    }

    #[test]
    fn content_anchors_char_length_not_byte_length() {
        // "уфа" — 3 символа, 6 байт в UTF-8: должна пройти фильтр длины (>=3 СИМВОЛА), не
        // отфильтроваться неправильной байтовой проверкой длины.
        let anchors = extract_content_anchors("уфа большой город");
        assert!(anchors.iter().any(|a| a == "уфа"), "{anchors:?}");
    }

    #[test]
    fn stable_dedup_keeps_first_occurrence_order() {
        let out = stable_dedup(vec!["a".into(), "b".into(), "a".into(), "c".into(), "b".into()]);
        assert_eq!(out, vec!["a".to_string(), "b".to_string(), "c".to_string()]);
    }
}
