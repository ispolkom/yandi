//! Перенос ПЯТИ чистых классификаторов из agent/claim_evidence_retriever.py (не всего модуля —
//! остальное там делает retrieval/embedding-вызовы, не чистое): _is_absence_claim(),
//! _is_existence_question(), _extract_existence_target(), _target_overlap(),
//! _classify_claim_role(). Вместе они решают "относится ли этот claim к вопросу существования
//! X, и если да — как именно (CORE/DIRECT_DECISION_EVIDENCE/EXPLANATORY/BACKGROUND)" — часть
//! приоритизации retrieval (_claim_retrieval_priority, которая САМА НЕ перенесена: она ещё
//! вызывает _query_relevance_score — embedding, сеть).
//!
//! Все regex здесь БЕЗ (?i) на _ABSENCE_MARKERS/_EXISTENCE_ASSERTION_MARKERS: оригинал приводит
//! текст к нижнему регистру (`.lower()`) ДО поиска, не через regex-флаг — то же самое сделано
//! здесь (паттерны применяются к уже lowercased строке).
//!
//! Символьная (не байтовая) длина — систематическая ловушка в этом кластере функций (5-й раз
//! подряд с других кусков): `len(w) >= 4`, `word[:max(3, len(word)-2)]` — везде `.chars()`.
//!
//! Последний паттерн `_ABSENCE_MARKERS` — `r"\bнет\s+(?!сомнени)[а-яё]"` — использует негативный
//! просмотр вперёд `(?!сомнени)`; стандартный крейт `regex` его не поддерживает (см. уже
//! известную заметку в claim_identity.rs). Вынесен в ручную функцию `matches_bare_net()`:
//! находит все `\bнет\s+`, и для каждого проверяет, что остаток НЕ начинается с "сомнени" и
//! следующий символ — строчная кириллица — тот же результат, без второго regex-движка.
//!
//! ДОПОЛНЕНО (2026-09-24, кусок 26): `_anchor_hit` (`\b<re.escape(якорь)>\b`) и `_subject_anchor_matches`
//! (шлюз идентичности субъекта: какие из title/url/passage подтверждают якорь). `\b` для ПРОИЗВОЛЬНОГО литерала
//! реализован вручную: граница слева/справа = «словесность» соседних символов (Python-`\w`, py_word_table) различается;
//! это верно и когда якорь начинается/кончается не-словесным символом (тогда граница требует словесного соседа).
//! Перебираются ВСЕ позиции (в т. ч. перекрывающиеся вхождения), как делает `re.search`.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE=rust (см.
//! agent/claim_evidence_retriever.py).

use crate::py_text::PyLowerExt;
use once_cell::sync::Lazy;
#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::PyDict;
use regex::Regex;

fn re(pattern: &str) -> Regex {
    crate::py_text::py_regex(pattern)
}

// agent/claim_evidence_retriever.py::_NEGATION_GAP
const NEGATION_GAP: &str = r"(?:\s+(?:была|было|были|есть|пока|ещё|уже))?";

// agent/claim_evidence_retriever.py::_ABSENCE_MARKERS — построены как строки времени
// выполнения (не статические литералы), т.к. включают NEGATION_GAP.
static ABSENCE_MARKERS: Lazy<Vec<Regex>> = Lazy::new(|| {
    [
        format!(r"не{NEGATION_GAP}\s+обнаруж"),
        format!(r"не{NEGATION_GAP}\s+найден"),
        format!(r"не{NEGATION_GAP}\s+зафиксирова"),
        format!(r"не{NEGATION_GAP}\s+выявлен"),
        format!(r"не{NEGATION_GAP}\s+установ"),
        r"нет\s+доказательств".to_string(),
        r"нет\s+свидетельств".to_string(),
        r"нет\s+подтверждени".to_string(),
        r"не\s+подтвержд".to_string(),
        r"отсутству".to_string(),
        r"ни\s+один[^.]*не\s+".to_string(),
        // 12-й паттерн — `\bнет\s+(?!сомнени)[а-яё]` — не регекс: см. matches_bare_net() ниже
        // (крейт `regex` не поддерживает негативный lookahead).
    ]
    .iter()
    .map(|p| re(p))
    .collect()
});

/// Ручная замена паттерна `\bнет\s+(?!сомнени)[а-яё]` (негативный lookahead не поддержан
/// крейтом `regex`, а «съедающая» граница слова здесь сместила бы позиции). Для каждого вхождения "нет"
/// с несловесным символом (или началом строки) слева и хотя бы одним питоновским пробелом справа:
/// после ВСЕЙ серии пробелов остаток НЕ начинается с "сомнени" и следующий символ — строчная кириллица а-я/ё.
/// (Только полная серия пробелов может дать совпадение: при более короткой `[а-яё]` попал бы на пробел.)
fn matches_bare_net(lower: &str) -> bool {
    for (idx, _) in lower.match_indices("нет") {
        let before_ok = lower[..idx].chars().next_back().map_or(true, |c| !crate::source_clustering::is_py_word_char(c));
        if !before_ok {
            continue;
        }
        let after = &lower[idx + "нет".len()..];
        let rest = after.trim_start_matches(crate::py_text::is_py_space);
        if rest.len() == after.len() {
            continue; // нужен хотя бы один пробел
        }
        if rest.starts_with("сомнени") {
            continue;
        }
        if let Some(c) = rest.chars().next() {
            if ('а'..='я').contains(&c) || c == 'ё' {
                return true;
            }
        }
    }
    false
}

// agent/claim_evidence_retriever.py::_EXISTENCE_ASSERTION_MARKERS = _ABSENCE_MARKERS + доп.
static EXISTENCE_ASSERTION_EXTRA: Lazy<Vec<Regex>> = Lazy::new(|| {
    [
        r"\bобнаружен[аоы]?\b",
        r"\bнайден[аоы]?\b",
        r"\bзафиксирован[аоы]?\b",
        r"\bвыявлен[аоы]?\b",
        r"\bподтвержд[её]н[аоы]?\b",
        r"\bустановлен[аоы]?\b",
        r"маловероятн",
        r"крайне\s+невероятн",
        r"считается\s+(?:маловероятн|невозможн|возможн)",
    ]
    .into_iter()
    .map(re)
    .collect()
});

static EXISTENCE_QUESTION_RE: Lazy<Regex> = Lazy::new(|| {
    re(r"(?i)(?:есть\s+ли|существует\s+ли|имеется\s+ли|обнаружен[аоы]?\s+ли|найден[аоы]?\s+ли|зафиксирован[аоы]?\s+ли)")
});

static EXISTENCE_TARGET_RE: Lazy<Regex> = Lazy::new(|| {
    re(r"(?i)(?:есть\s+ли|существует\s+ли|имеется\s+ли|обнаружен[аоы]?\s+ли|найден[аоы]?\s+ли|зафиксирован[аоы]?\s+ли)\s+(.+?)(?:\s+(?:на|в|у|при|под|около|близ|для)\s+|[?.!]|$)")
});

static WORD_RE: Lazy<Regex> = Lazy::new(|| re(r"[A-Za-zА-Яа-яЁё-]+"));

const TARGET_STOPWORDS: &[&str] = &["какие", "какой", "какая", "какие-то", "какая-то", "какой-то", "хоть", "вообще", "действительно", "точно"];
const EVIDENCE_INSTRUMENT_MARKERS: &[&str] = &["телескоп", "зонд", "аппарат", "сигнал", "сигнатур", "спектр", "наблюдени", "радар", "датчик", "мисси"];

/// agent/claim_evidence_retriever.py::_is_absence_claim
pub fn is_absence_claim(claim_text: &str) -> bool {
    let lower = claim_text.py_lowercase();
    ABSENCE_MARKERS.iter().any(|m| m.is_match(&lower)) || matches_bare_net(&lower)
}

/// agent/claim_evidence_retriever.py::_is_existence_question
pub fn is_existence_question(query: &str) -> bool {
    EXISTENCE_QUESTION_RE.is_match(query)
}

/// agent/claim_evidence_retriever.py::_extract_existence_target
pub fn extract_existence_target(query: &str) -> Vec<String> {
    let query = crate::py_text::py_strip(query);
    let Some(caps) = EXISTENCE_TARGET_RE.captures(query) else {
        return Vec::new();
    };
    let phrase = caps.get(1).map(|m| m.as_str()).unwrap_or("");
    WORD_RE
        .find_iter(phrase)
        .map(|m| m.as_str().py_lowercase())
        .filter(|w| w.chars().count() >= 4 && !TARGET_STOPWORDS.contains(&w.as_str()))
        .collect()
}

/// agent/claim_evidence_retriever.py::_target_overlap
pub fn target_overlap(claim_lower: &str, target_words: &[String]) -> bool {
    target_words.iter().filter(|w| w.chars().count() >= 4).any(|word| {
        let n = (word.chars().count().saturating_sub(2)).max(3);
        let stem: String = word.chars().take(n).collect();
        claim_lower.contains(&stem)
    })
}

fn is_word(c: char) -> bool {
    crate::source_clustering::is_py_word_char(c)
}

/// `_anchor_hit`: `re.search(r"\b" + re.escape(anchor) + r"\b", haystack) is not None`
pub fn anchor_hit(anchor: &str, haystack: &str) -> bool {
    if anchor.is_empty() {
        return false;
    }
    let a: Vec<char> = anchor.chars().collect();
    let h: Vec<char> = haystack.chars().collect();
    if a.len() > h.len() {
        return false;
    }
    let (first_w, last_w) = (is_word(a[0]), is_word(a[a.len() - 1]));
    for i in 0..=(h.len() - a.len()) {
        if h[i..i + a.len()] != a[..] {
            continue;
        }
        let before_w = i > 0 && is_word(h[i - 1]);
        let after_w = i + a.len() < h.len() && is_word(h[i + a.len()]);
        if before_w != first_w && last_w != after_w {
            return true;
        }
    }
    false
}

/// `_subject_anchor_matches` без выбора якорей (его делает Python-обёртка): какие поля подтвердили якоря.
pub fn subject_fields(anchors: &[String], title: &str, url: &str, passage: &str) -> Vec<&'static str> {
    let mut out = Vec::new();
    for (name, hay) in [("title", title), ("url", url), ("passage", passage)] {
        let lower = hay.py_lowercase();
        if anchors.iter().any(|a| anchor_hit(a, &lower)) {
            out.push(name);
        }
    }
    out
}

pub struct ClaimRole {
    pub role: Option<&'static str>,
    pub target_match: bool,
    pub has_assertion: bool,
    pub has_instrument: bool,
}

/// agent/claim_evidence_retriever.py::_classify_claim_role
pub fn classify_claim_role(claim_text: &str, query: &str) -> ClaimRole {
    if !is_existence_question(query) {
        return ClaimRole { role: None, target_match: false, has_assertion: false, has_instrument: false };
    }

    let target_words = extract_existence_target(query);
    let lower = claim_text.py_lowercase();

    let target_match = target_overlap(&lower, &target_words);
    let has_assertion = ABSENCE_MARKERS.iter().any(|m| m.is_match(&lower))
        || matches_bare_net(&lower)
        || EXISTENCE_ASSERTION_EXTRA.iter().any(|m| m.is_match(&lower));
    let has_instrument = EVIDENCE_INSTRUMENT_MARKERS.iter().any(|m| lower.contains(m));

    let role = if has_instrument && (target_match || has_assertion) {
        "DIRECT_DECISION_EVIDENCE"
    } else if target_match && has_assertion {
        "CORE"
    } else if target_match {
        "EXPLANATORY"
    } else {
        "BACKGROUND"
    };

    ClaimRole { role: Some(role), target_match, has_assertion, has_instrument }
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "is_absence_claim")]
fn py_is_absence_claim(claim_text: &str) -> bool {
    is_absence_claim(claim_text)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "is_existence_question")]
fn py_is_existence_question(query: &str) -> bool {
    is_existence_question(query)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "extract_existence_target")]
fn py_extract_existence_target(query: &str) -> Vec<String> {
    extract_existence_target(query)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "target_overlap")]
fn py_target_overlap(claim_lower: &str, target_words: Vec<String>) -> bool {
    target_overlap(claim_lower, &target_words)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "classify_claim_role")]
fn py_classify_claim_role<'py>(py: Python<'py>, claim_text: &str, query: &str) -> PyResult<Bound<'py, PyDict>> {
    let r = classify_claim_role(claim_text, query);
    let d = PyDict::new_bound(py);
    d.set_item("role", r.role)?;
    d.set_item("target_match", r.target_match)?;
    d.set_item("has_assertion", r.has_assertion)?;
    d.set_item("has_instrument", r.has_instrument)?;
    Ok(d)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "anchor_hit")]
fn py_anchor_hit(anchor: &str, haystack: &str) -> bool {
    anchor_hit(anchor, haystack)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "subject_fields")]
fn py_subject_fields(anchors: Vec<String>, title: &str, url: &str, passage: &str) -> Vec<&'static str> {
    subject_fields(&anchors, title, url, passage)
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_anchor_hit, m)?)?;
    m.add_function(wrap_pyfunction!(py_subject_fields, m)?)?;
    m.add_function(wrap_pyfunction!(py_is_absence_claim, m)?)?;
    m.add_function(wrap_pyfunction!(py_is_existence_question, m)?)?;
    m.add_function(wrap_pyfunction!(py_extract_existence_target, m)?)?;
    m.add_function(wrap_pyfunction!(py_target_overlap, m)?)?;
    m.add_function(wrap_pyfunction!(py_classify_claim_role, m)?)?;
    Ok(())
}

// ── Юнит-тесты (сценарии из agent/claim_priority_regression_test.py) ────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn existence_question_detection() {
        assert!(is_existence_question("Есть ли разумная жизнь на Юпитере?"));
        assert!(!is_existence_question("Расскажи о Юпитере"));
    }

    #[test]
    fn extract_target_basic() {
        let target = extract_existence_target("Есть ли разумная жизнь на Юпитере?");
        assert!(target.iter().any(|w| w.contains("жизн")), "{target:?}");
    }

    #[test]
    fn absence_claim_forms() {
        assert!(is_absence_claim("Жизнь не обнаружена на Юпитере."));
        assert!(is_absence_claim("Жизнь не была обнаружена на Юпитере.")); // разрыв "не БЫЛА обнаружена"
        assert!(is_absence_claim("Нет доказательств существования жизни."));
        assert!(is_absence_claim("Жизнь отсутствует на планете."));
        assert!(!is_absence_claim("Температура не превышает -145°C.")); // НЕ absence (количественное сравнение)
    }

    #[test]
    fn absence_claim_bare_net_x() {
        assert!(is_absence_claim("Нет жизни на Марсе."));
        assert!(!is_absence_claim("Нет сомнений, что жизнь существует.")); // исключение "нет сомнений"
    }

    #[test]
    fn core_vs_background_vs_explanatory() {
        let query = "Есть ли разумная жизнь на Юпитере?";
        let core = classify_claim_role("Разумная жизнь на Юпитере не обнаружена.", query);
        assert_eq!(core.role, Some("CORE"));
        assert!(core.target_match && core.has_assertion);

        let background = classify_claim_role("Атмосфера Юпитера состоит из водорода и гелия.", query);
        assert_eq!(background.role, Some("BACKGROUND"));

        let non_existence = classify_claim_role("Что-то про Юпитер", "Расскажи о Юпитере");
        assert_eq!(non_existence.role, None);
    }

    #[test]
    fn direct_decision_evidence_instrument_wins_over_core() {
        let query = "Есть ли разумная жизнь на Юпитере?";
        let r = classify_claim_role("Телескопические наблюдения не зафиксировали жизнь на Юпитере.", query);
        assert_eq!(r.role, Some("DIRECT_DECISION_EVIDENCE"));
    }

    #[test]
    fn target_overlap_handles_case_endings() {
        // "вода" (4 симв.) должно совпасть с "воды"/"водой" через стем длиной 3.
        assert!(target_overlap("следы воды обнаружены", &["вода".to_string()]));
        assert!(target_overlap("много водой залито", &["вода".to_string()]));
    }

    #[test]
    fn anchor_hit_word_boundaries() {
        assert!(anchor_hit("sun", "the sun is bright"));
        assert!(!anchor_hit("sun", "sunlight and sunday"));
        assert!(anchor_hit("европейский союз", "решение европейский союз принял"));
        assert!(!anchor_hit("c++", "a c++ b")); // якорь кончается не-словесным: справа нужен словесный сосед — здесь пробел
        assert!(anchor_hit("c++", "a c++x b"));
    }

    #[test]
    fn short_words_excluded_from_target() {
        // "все"/"хоть" короче 4 симв. либо в stopwords — не должны попасть в target.
        let target = extract_existence_target("Есть ли хоть какая жизнь на Юпитере около звезды?");
        assert!(!target.contains(&"хоть".to_string()));
        assert!(!target.contains(&"все".to_string()));
    }
}
