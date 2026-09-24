//! Перенос agent/epistemic_router.py — ВСЕ "детекторные" функции (_detect_domain,
//! _detect_hypothetical, _detect_negative_claim, _detect_testability,
//! _detect_knowledge_stability, _get_answer_mode, _determine_analysis_depth,
//! get_trust_cap_for_testability, get_trust_label_for_epistemic,
//! get_response_mode_description, get_objectivity_score). classify_claim() САМА НЕ перенесена —
//! она лишь собирает EpistemicClassification (~30 полей) из результатов этих функций плюс кучи
//! неизменных литералов (confidence=0.5 ВСЕГДА, игнорируя параметр функции; trust_score=0.2,
//! max_trust_cap="PARTIALLY_SUPPORTED" и т.д. — не вычисляются, просто константы) — переносить
//! в Rust там нечего, вся реальная логика уже в перенесённых функциях, которые classify_claim()
//! как звала по имени, так и продолжает звать (теперь потенциально Rust-делегирующие).
//!
//! Сохранены дословно, НЕ "исправлены": get_trust_cap_for_testability() здесь ВСЕГДА возвращает
//! "PARTIALLY_SUPPORTED" (это ДРУГАЯ функция с тем же именем, что в agent/claim_types.py — та
//! делает настоящий словарный поиск, эта — нет); get_trust_label_for_epistemic() не использует
//! свой параметр вообще; _detect_knowledge_stability() принимает domain/testability, но
//! использует только q; get_objectivity_score() всегда возвращает True третьим элементом.
//!
//! _DOMAIN_MARKERS — Vec, не HashMap: порядок влияет на то, какой домен победит при равном score
//! (Python max() на ничьей берёт первый по порядку итерации), и на то, какой ключ ("stable"/
//! "emerging"/"controversial") победит в _detect_knowledge_stability по той же причине.
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_EPISTEMIC_ROUTER_ENGINE=rust (см. agent/epistemic_router.py).

use crate::py_text::PyLowerExt;
use once_cell::sync::Lazy;
use pyo3::prelude::*;

type DomainMarker = (&'static str, &'static [&'static str], f64);

static DOMAIN_MARKERS: Lazy<Vec<DomainMarker>> = Lazy::new(|| {
    vec![
        ("factual", &["сколько", "чему равна", "какой", "какая", "какое", "равен", "равно"], 0.9),
        (
            "scientific",
            &[
                "эксперимент", "наблюдение", "данные", "научный", "исследование", "лаборатория",
                "гипотеза", "теория", "измерение", "статистика", "анализ", "результат",
                "доказательство", "гравитация", "квант",
            ],
            1.0,
        ),
        (
            "historical",
            &[
                "год", "век", "тысячелетие", "до н.э.", "событие", "исторический", "древний",
                "средневековый", "советский", "война", "первый", "история", "хроника", "архив",
            ],
            0.9,
        ),
        (
            "mathematical",
            &["доказательство", "аксиома", "теорема", "число", "формула", "расчёт", "дедукция", "логика", "уравнение"],
            1.0,
        ),
        (
            "procedural",
            &[
                "как сделать", "как работает", "как построить", "инструкция", "алгоритм", "метод",
                "способ", "процесс", "последовательность", "этап", "шаг", "руководство", "dht",
                "p2p", "протокол", "как поймать", "как приготовить", "рецепт",
            ],
            0.9,
        ),
        (
            "religious",
            &[
                "вера", "религия", "церковь", "пророк", "откровение", "грех", "спасение",
                "молитва", "ислам", "христианство", "буддизм", "коран", "библия", "бог", "каин",
                "авель", "адам", "ева", "библейский", "ветхий завет", "новый завет", "евангелие",
                "иуда", "моисей", "пророчество", "ангел", "архангел", "сотворение",
                "грехопадение", "рай", "ад",
            ],
            0.95,
        ),
        (
            "philosophical",
            &[
                "смысл", "ценность", "этика", "добро", "зло", "справедливость", "свобода", "воля",
                "долг", "мораль", "экзистенция", "истина", "знание", "бытие",
            ],
            0.8,
        ),
        (
            "axiological",
            &[
                "самое ценное", "что важнее", "что главное", "зачем жить", "ценность",
                "стоит ли", "имеет ли значение", "ради чего", "ценный", "важный",
            ],
            0.8,
        ),
        (
            "metaphysical",
            &[
                "бытие", "сущность", "первопричина", "абсолют", "сверхъестественный", "дух",
                "душа", "сознание вне", "трансцендентный", "вечность", "бесконечность", "дуализм",
            ],
            0.9,
        ),
        (
            "normative",
            &["должен", "правильно ли", "справедливо ли", "можно ли", "имеет ли право", "обязан", "следует", "надлежит"],
            0.8,
        ),
        (
            "biological",
            &["жизнь", "живой", "неживой", "клетка", "орга", "ген", "эволюция", "вид", "популяция", "экосистем"],
            0.7,
        ),
        (
            "media_interpretation",
            &[
                "фильм", "сериал", "кино", "картина", "лента", "трейлер", "смысл фильма",
                "о чем фильм", "объясни концовку", "разбор фильма", "что хотел сказать режиссёр",
                "смысл сериала", "смысл книги", "смысл игры", "о чем сериал", "о чем книга",
                "о чем игра", "киновселенная", "сюжет", "персонаж", "режиссёр", "экранизация",
                "посткредитная сцена", "концовка", "интерпретация фильма", "что означает фильм",
            ],
            0.95,
        ),
    ]
});

/// agent/epistemic_router.py::_detect_domain — возвращает (domain, subdomain, confidence).
/// subdomain у оригинала ВСЕГДА "" (второй элемент кортежа никогда не вычисляется отдельно).
pub fn detect_domain(q: &str) -> (&'static str, &'static str, f64) {
    let mut scores: Vec<(&'static str, f64)> = Vec::new();
    for (domain, words, weight) in DOMAIN_MARKERS.iter() {
        let score: f64 = words.iter().filter(|w| q.contains(**w)).map(|_| *weight).sum();
        if score > 0.0 {
            scores.push((domain, score));
        }
    }

    if scores.is_empty() {
        return ("factual", "", 0.3);
    }

    let max_score = scores.iter().map(|(_, s)| *s).fold(f64::MIN, f64::max);
    // top_domains в порядке появления в scores (== порядок DOMAIN_MARKERS, как и в Python).
    let top_domains: Vec<&'static str> = scores.iter().filter(|(_, s)| *s == max_score).map(|(d, _)| *d).collect();

    let domain = if top_domains.len() > 1 {
        // max(top_domains, key=weight) — на ничьей по весу тоже побеждает первый по порядку.
        let weight_of = |d: &str| DOMAIN_MARKERS.iter().find(|(dm, _, _)| *dm == d).map(|(_, _, w)| *w).unwrap_or(0.5);
        let mut best = top_domains[0];
        let mut best_w = weight_of(best);
        for d in &top_domains[1..] {
            let w = weight_of(d);
            if w > best_w {
                best = d;
                best_w = w;
            }
        }
        best
    } else {
        top_domains[0]
    };

    (domain, "", (max_score / 3.0).min(1.0))
}

const HYPOTHETICAL_MARKERS: &[&str] = &[
    "гипотеза", "теория", "предположительно", "возможно", "существовал", "могла",
    "не доказано", "гипотетический", "вероятно", "по легенде", "согласно гипотезе", "если",
    "допустим", "предположим", "может быть", "считается", "считают", "по мнению",
    "неизвестно", "загадка", "тайна", "спорно", "дискуссионно",
];

/// agent/epistemic_router.py::_detect_hypothetical
pub fn detect_hypothetical(query: &str) -> bool {
    let q = query.py_lowercase();
    HYPOTHETICAL_MARKERS.iter().any(|m| q.contains(m))
}

const NEGATIVE_CLAIM_MARKERS: &[&str] = &[
    "не обнаружен", "не найден", "не зафиксирован", "не выявлен", "не установлен",
    "нет доказательств", "нет свидетельств", "нет подтверждени", "не подтвержд", "отсутству",
];

/// agent/epistemic_router.py::_detect_negative_claim
pub fn detect_negative_claim(query: &str) -> bool {
    let q = query.py_lowercase();
    NEGATIVE_CLAIM_MARKERS.iter().any(|m| q.contains(m))
}

/// agent/epistemic_router.py::_detect_testability — возвращает (testability, confidence).
pub fn detect_testability(q: &str, domain: &str) -> (&'static str, f64) {
    match domain {
        "mathematical" => return ("fully_testable", 0.7),
        "procedural" => return ("fully_testable", 0.85),
        "religious" | "metaphysical" => return ("non_falsifiable", 0.95),
        "axiological" => return ("interpretive", 0.9),
        "normative" => return ("interpretive", 0.85),
        "philosophical" => return ("interpretive", 0.85),
        "media_interpretation" => return ("partially_testable", 0.8),
        _ => {}
    }
    if detect_hypothetical(q) {
        return ("partially_testable", 0.5);
    }
    ("partially_testable", 0.5)
}

// Порядок важен — как и в Python dict literal (stable, emerging, controversial), на ничьей
// побеждает первый в этом порядке.
const STABILITY_MARKERS: &[(&str, &[&str])] = &[
    ("stable", &["доказано", "установлено", "известно", "факт", "закон", "аксиома", "константа"]),
    ("emerging", &["новое исследование", "недавно обнаружено", "экспериментальное", "предварительные"]),
    ("controversial", &["спорно", "дискуссионно", "противоречиво", "разные мнения", "не согласны"]),
];

/// agent/epistemic_router.py::_detect_knowledge_stability — domain/testability параметры
/// НЕ используются в оригинале (сохранено дословно). Возвращает (stability, confidence, reason).
pub fn detect_knowledge_stability(q: &str, _domain: &str, _testability: &str) -> (&'static str, f64, &'static str) {
    let q_lower = q.py_lowercase();
    // Первый ключ со строго наибольшим счётом побеждает (Python max() на ничьей берёт первый
    // по порядку итерации) — обновляем best только на СТРОГО большем счёте, не на равном.
    let mut best_key: Option<&'static str> = None;
    let mut best_count: usize = 0;
    for (key, markers) in STABILITY_MARKERS {
        let count = markers.iter().filter(|m| q_lower.contains(**m)).count();
        if count > best_count {
            best_count = count;
            best_key = Some(key);
        }
    }
    match best_key {
        Some("stable") => ("stable", 0.6, "Обнаружены маркеры устоявшегося мнения"),
        Some("emerging") => ("emerging", 0.5, "Обнаружены маркеры нового знания"),
        Some("controversial") => ("controversial", 0.7, "Обнаружены маркеры спорного вопроса"),
        _ => ("unknown", 0.4, "Недостаточно данных"),
    }
}

/// agent/epistemic_router.py::_get_answer_mode
pub fn get_answer_mode(domain: &str, testability: &str) -> &'static str {
    if matches!(testability, "interpretive" | "non_falsifiable") {
        return "pluralistic_contextual";
    }
    if matches!(
        domain,
        "scientific" | "historical" | "religious" | "philosophical" | "media_interpretation"
            | "metaphysical" | "axiological" | "normative" | "biological" | "factual"
            | "physical" | "chemical" | "astronomical"
    ) {
        return "hypothesis_first";
    }
    "qualified_factual"
}

/// agent/epistemic_router.py::_determine_analysis_depth
pub fn determine_analysis_depth(domain: &str, testability: &str) -> &'static str {
    let full_domains = matches!(
        domain,
        "religious" | "philosophical" | "historical" | "axiological" | "normative"
            | "media_interpretation" | "metaphysical" | "scientific"
    );
    if full_domains || matches!(testability, "interpretive" | "non_falsifiable") {
        "full"
    } else {
        "basic"
    }
}

/// agent/epistemic_router.py::get_trust_cap_for_testability — ВСЕГДА константа, параметр
/// игнорируется (это ДРУГАЯ функция, не agent/claim_types.py::get_trust_cap_for_testability).
pub fn get_trust_cap_for_testability(_testability: &str) -> &'static str {
    "PARTIALLY_SUPPORTED"
}

/// agent/epistemic_router.py::get_trust_label_for_epistemic — ВСЕГДА константа, параметр
/// (весь classification) не используется вообще, поэтому здесь без параметров.
pub fn get_trust_label_for_epistemic() -> &'static str {
    "PARTIALLY_SUPPORTED"
}

/// agent/epistemic_router.py::get_response_mode_description — ДРУГОЙ словарь, чем в
/// agent/claim_types.py::get_response_mode_description (те же ключи, другие тексты).
pub fn get_response_mode_description(mode: &str) -> &'static str {
    match mode {
        "factual" => "отвечать фактами (но это гипотеза)",
        "qualified_factual" => "отвечать с оговорками о неопределённости",
        "contextual" => "отвечать с учётом контекста",
        "pluralistic_contextual" => "давать обзор различных позиций",
        "procedural" => "давать пошаговую инструкцию",
        "exploratory" => "исследовательский режим",
        _ => "стандартный режим с оговорками",
    }
}

const EPISTEMIC_WARNING: &str = "⚠️ **Честное предупреждение от YANDI:**\n\nЯ не знаю, что такое \"истина\". Всё, что я могу — передавать наблюдения и интерпретации других людей.\n- Научные теории — это модели, которые могут быть ошибочны.\n- Исторические факты — это интерпретация источников.\n- Любое знание — это гипотеза, пока вы не проверили её на своём опыте.\n\nЕдинственный способ узнать — проверить самому. Я лишь помогаю собрать информацию.\n";

/// agent/epistemic_router.py::get_objectivity_score — возвращает (score, warning, True).
/// Третий элемент кортежа у оригинала ВСЕГДА True — сохранено дословно, не "убрано".
pub fn get_objectivity_score(testability: &str, domain: &str, knowledge_stability: &str, is_hypothetical: bool) -> (f64, String, bool) {
    let mut base_score: f64 = 0.1;
    if domain == "procedural" {
        base_score = 0.3;
    }
    if testability == "fully_testable" && domain == "procedural" {
        base_score = 0.4;
    }
    if domain == "mathematical" {
        base_score = 0.3;
    }
    if is_hypothetical {
        base_score = 0.05;
    }

    if knowledge_stability == "stable" {
        base_score += 0.1;
    } else if knowledge_stability == "controversial" {
        base_score -= 0.05;
    } else if knowledge_stability == "emerging" {
        base_score -= 0.05;
    }

    let objectivity_score = base_score.max(0.0).min(0.5);

    let mut warning = EPISTEMIC_WARNING.to_string();
    if is_hypothetical {
        warning.push_str("\n\n⚠️ **Это гипотетическое утверждение.** Нет прямых доказательств.");
    }
    if matches!(domain, "scientific" | "biological") {
        warning.push_str("\n\n🧪 **Это научная модель.** Она объясняет наблюдаемые явления, но не является окончательной истиной.");
    }
    if knowledge_stability == "controversial" {
        warning.push_str("\n\n⚡ **Это спорное утверждение.** Существуют разные, иногда противоположные точки зрения.");
    }
    if domain == "historical" {
        warning.push_str("\n\n📜 **Это историческая интерпретация.** История пишется на основе источников, которые могут быть неполными или предвзятыми.");
    }

    (objectivity_score, warning, true)
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(name = "detect_domain")]
fn py_detect_domain(q: &str) -> (String, String, f64) {
    let (d, s, c) = detect_domain(q);
    (d.to_string(), s.to_string(), c)
}

#[pyfunction]
#[pyo3(name = "detect_hypothetical")]
fn py_detect_hypothetical(query: &str) -> bool {
    detect_hypothetical(query)
}

#[pyfunction]
#[pyo3(name = "detect_negative_claim")]
fn py_detect_negative_claim(query: &str) -> bool {
    detect_negative_claim(query)
}

#[pyfunction]
#[pyo3(name = "detect_testability")]
fn py_detect_testability(q: &str, domain: &str) -> (String, f64) {
    let (t, c) = detect_testability(q, domain);
    (t.to_string(), c)
}

#[pyfunction]
#[pyo3(name = "detect_knowledge_stability")]
fn py_detect_knowledge_stability(q: &str, domain: &str, testability: &str) -> (String, f64, String) {
    let (s, c, r) = detect_knowledge_stability(q, domain, testability);
    (s.to_string(), c, r.to_string())
}

#[pyfunction]
#[pyo3(name = "get_answer_mode")]
fn py_get_answer_mode(domain: &str, testability: &str) -> String {
    get_answer_mode(domain, testability).to_string()
}

#[pyfunction]
#[pyo3(name = "determine_analysis_depth")]
fn py_determine_analysis_depth(domain: &str, testability: &str) -> String {
    determine_analysis_depth(domain, testability).to_string()
}

#[pyfunction]
#[pyo3(name = "get_trust_cap_for_testability")]
fn py_get_trust_cap_for_testability(testability: &str) -> String {
    get_trust_cap_for_testability(testability).to_string()
}

#[pyfunction]
#[pyo3(name = "get_trust_label_for_epistemic")]
fn py_get_trust_label_for_epistemic() -> String {
    get_trust_label_for_epistemic().to_string()
}

#[pyfunction]
#[pyo3(name = "get_response_mode_description")]
fn py_get_response_mode_description(mode: &str) -> String {
    get_response_mode_description(mode).to_string()
}

#[pyfunction]
#[pyo3(name = "get_objectivity_score", signature = (testability, domain, knowledge_stability, is_hypothetical=false))]
fn py_get_objectivity_score(testability: &str, domain: &str, knowledge_stability: &str, is_hypothetical: bool) -> (f64, String, bool) {
    get_objectivity_score(testability, domain, knowledge_stability, is_hypothetical)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_detect_domain, m)?)?;
    m.add_function(wrap_pyfunction!(py_detect_hypothetical, m)?)?;
    m.add_function(wrap_pyfunction!(py_detect_negative_claim, m)?)?;
    m.add_function(wrap_pyfunction!(py_detect_testability, m)?)?;
    m.add_function(wrap_pyfunction!(py_detect_knowledge_stability, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_answer_mode, m)?)?;
    m.add_function(wrap_pyfunction!(py_determine_analysis_depth, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_trust_cap_for_testability, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_trust_label_for_epistemic, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_response_mode_description, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_objectivity_score, m)?)?;
    Ok(())
}

// ── Юнит-тесты ────────────────────────────────────────────────────────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn domain_detection_basic() {
        let (d, sub, _c) = detect_domain("почему погибла планета фаэтон гравитация квант");
        assert_eq!(d, "scientific");
        assert_eq!(sub, "");
    }

    #[test]
    fn domain_no_markers_defaults_factual() {
        let (d, _sub, c) = detect_domain("бла бла бла без всяких маркеров совсем");
        assert_eq!(d, "factual");
        assert_eq!(c, 0.3);
    }

    #[test]
    fn multiple_word_hits_add_weight() {
        // "эксперимент" И "данные" оба в scientific -> score = 1.0+1.0 = 2.0, а не 1.0.
        // ("результате" НЕ используется здесь специально: оно само содержит "результат" как
        // подстроку — тоже matched-слово scientific — что при первой попытке написания этого
        // теста дало неожиданные 3.0 вместо "чистых" 2.0; проверено напрямую через реальный
        // Python перед тем, как менять ожидание, а не угадано.)
        let (d, _, conf) = detect_domain("эксперимент и данные подтверждают гипотезу");
        assert_eq!(d, "scientific");
        assert!((conf - (2.0f64 / 3.0).min(1.0)).abs() < 1e-9, "{conf}");
    }

    #[test]
    fn hypothetical_and_negative_markers() {
        assert!(detect_hypothetical("возможно это гипотеза"));
        assert!(!detect_hypothetical("это точно факт без всяких сомнений"));
        assert!(detect_negative_claim("частица не обнаружена в ходе опытов"));
        assert!(!detect_negative_claim("частица найдена и подтверждена"));
    }

    #[test]
    fn testability_by_domain() {
        assert_eq!(detect_testability("x", "mathematical").0, "fully_testable");
        assert_eq!(detect_testability("x", "religious").0, "non_falsifiable");
        assert_eq!(detect_testability("x", "axiological").0, "interpretive");
        assert_eq!(detect_testability("x", "media_interpretation").0, "partially_testable");
    }

    #[test]
    fn knowledge_stability_tie_break_order() {
        // содержит и "доказано" (stable) и "спорно" (controversial), поровну по одному совпадению
        // -> stable побеждает, т.к. идёт первым в STABILITY_MARKERS
        let (s, ..) = detect_knowledge_stability("доказано, но всё равно спорно", "x", "x");
        assert_eq!(s, "stable");
    }

    #[test]
    fn objectivity_score_hypothetical_lowers_base() {
        let (score_normal, ..) = get_objectivity_score("partially_testable", "factual", "unknown", false);
        let (score_hyp, ..) = get_objectivity_score("partially_testable", "factual", "unknown", true);
        assert!(score_hyp < score_normal);
    }

    #[test]
    fn trust_functions_are_constants() {
        assert_eq!(get_trust_cap_for_testability("fully_testable"), "PARTIALLY_SUPPORTED");
        assert_eq!(get_trust_cap_for_testability("whatever"), "PARTIALLY_SUPPORTED");
        assert_eq!(get_trust_label_for_epistemic(), "PARTIALLY_SUPPORTED");
    }
}
