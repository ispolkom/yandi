//! Перенос agent/claim_semantic_identity_hardening.py — детерминированный regex-guard,
//! понижающий вердикт "equivalent" -> "different" при обнаружении асимметричного маркера
//! (причина/корреляция, точно/возможно, отрицание, числа, разные субъекты и т.д.). Чистая
//! функция (два текста на входе, строка-причина или ничего на выходе), стоит на горячем пути
//! каждого сравнения пары claim'ов.
//!
//! Все паттерны скопированы ПОСЛОВНО из Python-источника (не переписаны "по смыслу") — синтаксис
//! regex у Python `re` и Rust `regex` для этих конкретных паттернов совместим один в один
//! (символьные классы, `\s`/`\d`, группы, `(?i)`, `(?x)`). Проверено отдельно перед переносом:
//! Rust-крейт `regex` по умолчанию Unicode-осведомлён для `&str` — `\b` учитывает кириллицу как
//! часть слова так же, как Python `\b` в режиме Unicode (по умолчанию для str-паттернов) — без
//! этого весь перенос был бы тихо неверен для русского текста.
//!
//! extract_subject_anchors переиспользуется НАПРЯМУЮ из уже перенесённого src/claim_identity.rs
//! (не через Python) — ровно то преимущество, ради которого куски переносятся по одному: более
//! поздний кусок опирается на код уже перенесённого, а не заново ходит через FFI в Python.
//!
//! ЖИВАЯ ОШИБКА ПЕРЕНОСА, пойманная собственным юнит-тестом ДО parity-теста (не гипотетический
//! пример, а то, что реально произошло при написании этого файла): при первой транскрипции
//! `_EVIDENCE_OF_ABSENCE` флаг `(?i)` был пропущен (все остальные 13 паттернов в исходнике его
//! имеют, этот — не исключение, просто визуально легко потерять один флаг среди полутора
//! десятков похожих строк). Без `(?i)` паттерн не матчился на "Доказано..." (с заглавной буквы в
//! начале предложения) и guard тихо возвращал другую (тоже валидную, но НЕ ТУ) причину через
//! более позднюю проверку в списке — `each_dimension_fires_on_a_real_asymmetric_pair` поймал это
//! сразу. Вывод для следующих кусков: копировать regex-флаги нужно сверять посимвольно, не "на
//! глаз похоже", даже когда сам паттерн скопирован дословно.
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_HARDENING_ENGINE=rust (см. agent/claim_semantic_identity_hardening.py).

use crate::claim_identity::extract_subject_anchors;
use once_cell::sync::Lazy;
use pyo3::prelude::*;
use regex::Regex;
use std::collections::HashSet;

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).unwrap_or_else(|e| panic!("статический паттерн должен быть валиден: {pattern}: {e}"))
}

static CAUSAL: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(вызывает|вызвал[а-я]*|приводит\s+к|привёл[а-я]*\s+к|является\s+причиной|causes?|leads?\s+to)\b"));
static CORRELATIONAL: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(связан[а-я]*\s+с|ассоциирован[а-я]*\s+с|коррелирует|статистически\s+связан[а-я]*|associated\s+with|correlated\s+with|linked\s+to)\b"));

static NECESSARY: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(необходим[а-я]*|required|necessary)\b"));
static SUFFICIENT: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(достаточн[а-я]*|sufficient)\b"));

static CERTAINTY: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(точно|определённо|доказан[а-я]*|установлен[а-я]*|certainly|definitely|proven)\b"));
static POSSIBILITY: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(возможно|может\s+быть|вероятно|есть\s+вероятность|probably|might|could\b|may\b)\b"));

static CURRENT: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(сейчас|в\s+настоящее\s+время|currently|now\b)\b"));
static HISTORICAL: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(ранее|прежде|в\s+прошлом|previously|used\s+to|earlier)\b"));

static ABSOLUTE: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(всегда|никогда|полностью|always|never)\b"));
static QUALIFIED: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(обычно|как\s+правило|часто|редко|usually|generally|often)\b"));

static SCOPE_ALL: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(все|всё|каждый|каждая|каждое|all\b|every\b)\b"));
static SCOPE_SOME: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(некоторые|несколько|большинство|часть\s+из|some\b|several\b|most\b)\b"));

static ATTRIBUTION: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(по\s+словам|согласно\s+заявлению|считает,?\s+что|заявил[а-я]*|according\s+to|\bsaid\b|\bsays\b)\b"));

static PREDICTION: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(ожидается|прогнозируется|ожидают|will\s+\w+|expected\s+to)\b"));
static OBSERVATION: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(вырос[а-я]*|снизил[а-я]*|зафиксирован[а-я]*|наблюдал[а-я]*|подтвержд[её]н[а-я]*)\b"));

static ABSENCE_OF_EVIDENCE: Lazy<Regex> = Lazy::new(|| re(r"(?i)\b(не\s+найден[а-я]*|не\s+обнаружен[а-я]*|не\s+зафиксирован[а-я]*|not\s+found|no\s+evidence\s+of)\b"));
static EVIDENCE_OF_ABSENCE: Lazy<Regex> = Lazy::new(|| re(r"(?i)(доказан[а-я]*\s+отсутствие|доказано,?\s+что\b.*\bне\b|установлено,?\s+что\b.*\bневозмож)"));

static NEGATION: Lazy<Regex> = Lazy::new(|| {
    re(r"(?ix)
    \b(
        не\s+явля[а-я]* | неявля[а-я]* |
        не\s+был[а-я]* |
        не\s+обнаруж[а-я]* |
        не\s+найден[а-я]* |
        неэффектив[а-я]* |
        нельзя | невозможно |
        не\s+вызыва[а-я]* |
        не\s+влия[а-я]* |
        не\s+подтвержда[а-я]* |
        не\s+сниж[а-я]* |
        не\s+привод[а-я]* |
        does\s+not | do\s+not | did\s+not |
        is\s+not | are\s+not | was\s+not | were\s+not |
        cannot | can\s+not | will\s+not |
        has\s+not | have\s+not | had\s+not |
        \w+n't
    )\b
")
});

static NUMBER: Lazy<Regex> = Lazy::new(|| re(r"\d[\d.,]*"));

// agent/claim_semantic_identity_hardening.py::_DIMENSION_PAIRS — тот же ПОРЯДОК: guard
// возвращается на первом сработавшем измерении, порядок списка определяет, какая причина
// вернётся, если асимметрия есть сразу в нескольких измерениях.
static DIMENSION_PAIRS: Lazy<Vec<(&'static str, &'static Lazy<Regex>, &'static Lazy<Regex>)>> = Lazy::new(|| {
    vec![
        ("causal_vs_correlational", &CAUSAL, &CORRELATIONAL),
        ("necessary_vs_sufficient", &NECESSARY, &SUFFICIENT),
        ("possibility_vs_certainty", &POSSIBILITY, &CERTAINTY),
        ("current_vs_historical", &CURRENT, &HISTORICAL),
        ("absolute_vs_qualified", &ABSOLUTE, &QUALIFIED),
        ("scope_all_vs_some", &SCOPE_ALL, &SCOPE_SOME),
        ("prediction_vs_observation", &PREDICTION, &OBSERVATION),
        ("absence_of_evidence_vs_evidence_of_absence", &ABSENCE_OF_EVIDENCE, &EVIDENCE_OF_ABSENCE),
    ]
});

/// agent/claim_semantic_identity_hardening.py::_asymmetric — 'x'/'y'/None как &str для простоты.
fn asymmetric(text: &str, pattern_x: &Regex, pattern_y: &Regex) -> Option<&'static str> {
    let hit_x = pattern_x.is_match(text);
    let hit_y = pattern_y.is_match(text);
    if hit_x && !hit_y {
        return Some("x");
    }
    if hit_y && !hit_x {
        return Some("y");
    }
    None
}

/// agent/claim_semantic_identity_hardening.py::hardening_guard
pub fn hardening_guard(claim_a: &str, claim_b: &str) -> Option<String> {
    for (name, pattern_x, pattern_y) in DIMENSION_PAIRS.iter() {
        let side_a = asymmetric(claim_a, pattern_x, pattern_y);
        let side_b = asymmetric(claim_b, pattern_x, pattern_y);
        if let (Some(sa), Some(sb)) = (side_a, side_b) {
            if sa != sb {
                return Some(format!("{name}_marker_mismatch"));
            }
        }
    }

    let attr_a = ATTRIBUTION.is_match(claim_a);
    let attr_b = ATTRIBUTION.is_match(claim_b);
    if attr_a != attr_b {
        return Some("attribution_marker_mismatch".to_string());
    }

    let neg_a = NEGATION.is_match(claim_a);
    let neg_b = NEGATION.is_match(claim_b);
    if neg_a != neg_b {
        return Some("negation_marker_mismatch".to_string());
    }

    let nums_a: HashSet<&str> = NUMBER.find_iter(claim_a).map(|m| m.as_str()).collect();
    let nums_b: HashSet<&str> = NUMBER.find_iter(claim_b).map(|m| m.as_str()).collect();
    if !nums_a.is_empty() && !nums_b.is_empty() && nums_a != nums_b {
        return Some("numeric_mismatch".to_string());
    }

    let anchors_a: HashSet<String> = extract_subject_anchors(claim_a).into_iter().collect();
    let anchors_b: HashSet<String> = extract_subject_anchors(claim_b).into_iter().collect();
    if !anchors_a.is_empty() && !anchors_b.is_empty() && anchors_a.is_disjoint(&anchors_b) {
        return Some("entity_subject_mismatch".to_string());
    }

    None
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(name = "hardening_guard")]
fn py_hardening_guard(claim_a: &str, claim_b: &str) -> Option<String> {
    hardening_guard(claim_a, claim_b)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_hardening_guard, m)?)?;
    Ok(())
}

// ── Юнит-тесты (переносы сценариев из agent/epistemic_claim_semantic_identity_hardening_
// regression_test.py и agent/polarity_hardening_regression_test.py) ─────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_dimension_fires_on_a_real_asymmetric_pair() {
        let cases = [
            ("causal_vs_correlational", "Курение вызывает рак лёгких.", "Курение статистически связано с раком лёгких."),
            ("necessary_vs_sufficient", "Кислород необходим для горения.", "Кислорода достаточно для горения."),
            ("possibility_vs_certainty", "Возможно, это верно.", "Это точно верно."),
            ("current_vs_historical", "Компания сейчас проводит реформу.", "Компания ранее проводила реформу."),
            ("absolute_vs_qualified", "Метод всегда работает.", "Метод обычно работает."),
            ("scope_all_vs_some", "Все птицы летают.", "Некоторые птицы летают."),
            ("prediction_vs_observation", "Ожидается рост показателей.", "Зафиксирован рост показателей."),
            ("absence_of_evidence_vs_evidence_of_absence", "Частица не найдена.", "Доказано отсутствие частицы."),
        ];
        for (label, a, b) in cases {
            let got = hardening_guard(a, b);
            assert_eq!(got.as_deref(), Some(format!("{label}_marker_mismatch")).as_deref(), "{label}");
        }
    }

    #[test]
    fn attribution_and_negation_one_sided() {
        assert_eq!(
            hardening_guard("По словам эксперта, рынок растёт.", "Рынок растёт.").as_deref(),
            Some("attribution_marker_mismatch")
        );
        assert_eq!(
            hardening_guard("Препарат эффективен.", "Препарат неэффективен.").as_deref(),
            Some("negation_marker_mismatch")
        );
    }

    #[test]
    fn numeric_mismatch_generic_not_hardcoded() {
        assert_eq!(
            hardening_guard("У Юпитера 95 спутников.", "У Юпитера 96 спутников.").as_deref(),
            Some("numeric_mismatch")
        );
        assert_eq!(
            hardening_guard("В отчёте указано 250 случаев.", "В отчёте указано 340 случаев.").as_deref(),
            Some("numeric_mismatch")
        );
        assert_eq!(
            hardening_guard("В 1976 году компания была основана.", "Компания основана в 1976 году в гараже."),
            None
        );
    }

    #[test]
    fn genuine_paraphrases_not_vetoed() {
        let cases = [
            ("Аспартам является одобренной безопасной пищевой добавкой согласно FDA.",
             "По данным FDA, аспартам признан допустимым и безопасным подсластителем."),
            ("Юпитер является крупнейшей планетой Солнечной системы.",
             "Крупнейшей планетой Солнечной системы является Юпитер."),
            ("Исследование показало снижение уровня холестерина у участников.",
             "У участников исследования зафиксировано снижение уровня холестерина."),
        ];
        for (a, b) in cases {
            assert_eq!(hardening_guard(a, b), None, "unexpectedly fired for {a:?}/{b:?}");
        }
    }

    #[test]
    fn negation_family_predicate_stems() {
        // "не явля-" family (реальный баг, исправленный в Python: trailing \b раньше не
        // доходил до длинных спрягаемых форм).
        for text in [
            "Кофе не является канцерогеном.",
            "Эти утверждения не являются эквивалентными.",
            "Он не являлся членом организации.",
            "Она не являлась участницей исследования.",
        ] {
            assert!(NEGATION.is_match(text), "{text}");
        }
        let cases = [
            ("вызывает", "Кофе вызывает рак.", "Кофе не вызывает рак."),
            ("влияет", "X влияет на Y.", "X не влияет на Y."),
            ("подтверждает", "Исследование подтверждает связь.", "Исследование не подтверждает связь."),
            ("снижает", "Препарат снижает риск.", "Препарат не снижает риск."),
            ("приводит", "X приводит к Y.", "X не приводит к Y."),
            ("является", "Кофе является канцерогеном.", "Кофе не является канцерогеном."),
        ];
        for (verb, pos, neg) in cases {
            assert_eq!(hardening_guard(pos, neg).as_deref(), Some("negation_marker_mismatch"), "{verb}");
        }
    }

    #[test]
    fn negation_idioms_do_not_false_veto() {
        for (label, text) in [
            ("не только", "Это не только экономический союз, но и политический."),
            ("не менее", "В ЕС входит не менее 27 государств."),
            ("не более", "В ЕС входит не более 27 государств."),
            ("не обязательно", "Это не обязательно означает рост."),
            ("не просто", "Это не просто экономический союз."),
        ] {
            assert!(!NEGATION.is_match(text), "{label}: {text}");
        }
    }

    #[test]
    fn english_predicate_negation() {
        let cases = [
            ("does not", "Coffee causes cancer.", "Coffee does not cause cancer."),
            ("is not", "This is proven.", "This is not proven."),
            ("cannot", "It can be true.", "It cannot be true."),
            ("was not", "It was proven.", "It was not proven."),
            ("n't", "It works.", "It doesn't work."),
        ];
        for (label, pos, neg) in cases {
            assert!(NEGATION.is_match(neg), "{label}: {neg}");
            assert!(!NEGATION.is_match(pos), "{label}: {pos}");
        }
    }

    #[test]
    fn symmetry_and_genuine_paraphrase_with_new_stems() {
        let (pos, neg) = ("Кофе вызывает рак.", "Кофе не вызывает рак.");
        assert_eq!(hardening_guard(pos, neg).as_deref(), Some("negation_marker_mismatch"));
        assert_eq!(hardening_guard(neg, pos).as_deref(), Some("negation_marker_mismatch"));

        let (a, b) = ("Кофе вызывает рак у некоторых людей.", "У некоторых людей кофе вызывает рак.");
        assert_eq!(hardening_guard(a, b), None);
    }
}
