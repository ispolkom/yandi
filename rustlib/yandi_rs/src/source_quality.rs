//! Перенос agent/source_quality.py — ТОЛЬКО чистая часть: evaluate_source_quality() и её
//! приватные помощники (_hostname, _matches_domain, _classify_source, _refine_source_class).
//! evaluate_evidence_directness() НЕ переносится — она делает настоящий embedding-вызов
//! (llm_gateway.embed, сеть), это не чистая функция и ей не место здесь; остаётся в Python.
//!
//! Оценивается КАЖДЫЙ веб-источник, который проходит через систему — горячий путь, определяет,
//! годится ли источник в принципе как evidence (до какой-либо проверки relevance/NLI).
//!
//! _hostname здесь — ручной, а не через крейт `url`: Python `urlparse().hostname` нарочно
//! терпим к неполным/странным URL (не бросает исключение почти никогда, просто возвращает
//! пустой хост), тогда как строгий RFC3986-парсер (`url` крейт) на многих таких входах вернул
//! бы Err — пришлось бы городить свою терпимость поверх чужой строгости. Проще и вернее
//! повторить ровно ту лёгкую, терпимую логику, которую использует сам Python-код (взять всё
//! после "://" или "//", до первого "/", "?" или "#", отрезать userinfo и порт).
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_SOURCE_QUALITY_ENGINE=rust (см. agent/source_quality.py).

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::HashSet;

static SCIENTIFIC_DOMAINS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    [
        "nature.com", "science.org", "sciencedirect.com", "springer.com",
        "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    ]
    .into_iter()
    .collect()
});
static REFERENCE_DOMAINS: Lazy<HashSet<&'static str>> =
    Lazy::new(|| ["wikipedia.org", "britannica.com"].into_iter().collect());
static FORUM_DOMAINS: Lazy<HashSet<&'static str>> = Lazy::new(|| ["reddit.com", "quora.com"].into_iter().collect());
static SOCIAL_DOMAINS: Lazy<HashSet<&'static str>> =
    Lazy::new(|| ["vk.com", "facebook.com", "x.com", "twitter.com", "t.me"].into_iter().collect());

/// agent/source_quality.py::_hostname — см. модульный docstring про терпимость к странным URL.
pub fn hostname(url: &str) -> String {
    let rest: &str = if let Some(idx) = url.find("://") {
        &url[idx + 3..]
    } else if let Some(stripped) = url.strip_prefix("//") {
        stripped
    } else {
        return String::new();
    };
    let netloc_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let netloc = &rest[..netloc_end];
    let host_port = match netloc.rfind('@') {
        Some(idx) => &netloc[idx + 1..],
        None => netloc,
    };
    let host = if let Some(hp) = host_port.strip_prefix('[') {
        // IPv6-литерал в скобках — port после "]:" не относится к hostname.
        match hp.find(']') {
            Some(idx) => &hp[..idx],
            None => host_port,
        }
    } else {
        match host_port.rfind(':') {
            Some(idx) => &host_port[..idx],
            None => host_port,
        }
    };
    let mut host = host.to_lowercase();
    if let Some(stripped) = host.strip_prefix("www.") {
        host = stripped.to_string();
    }
    host
}

/// agent/source_quality.py::_matches_domain
fn matches_domain(host: &str, domains: &HashSet<&str>) -> bool {
    domains.iter().any(|d| host == *d || host.ends_with(&format!(".{d}")))
}

/// agent/source_quality.py::_classify_source — возвращает (source_class, authority, primaryness)
fn classify_source(host: &str, url: &str) -> (&'static str, f64, f64) {
    if host.is_empty() {
        return ("unknown", 0.25, 0.20);
    }
    if host.ends_with(".gov") || host.ends_with(".gov.uk") || host.contains(".gov.") {
        return ("primary", 0.90, 0.95);
    }
    if host.ends_with(".edu") || host.contains(".edu.") {
        return ("scientific", 0.85, 0.75);
    }
    if matches_domain(host, &SCIENTIFIC_DOMAINS) {
        return ("scientific", 0.90, 0.80);
    }
    if matches_domain(host, &REFERENCE_DOMAINS) {
        return ("reference", 0.75, 0.45);
    }
    if matches_domain(host, &FORUM_DOMAINS) {
        return ("forum", 0.30, 0.25);
    }
    if matches_domain(host, &SOCIAL_DOMAINS) {
        return ("social", 0.20, 0.20);
    }
    let lowered = url.to_lowercase();
    if lowered.contains("blog") {
        return ("blog_opinion", 0.35, 0.30);
    }
    if lowered.contains("forum") {
        return ("forum", 0.30, 0.25);
    }
    ("unknown", 0.50, 0.40)
}

const FORUM_MARKERS: &[&str] = &["форум", "обсуждение", "ответ пользователя", "комментарии пользователей"];
const BLOG_MARKERS: &[&str] = &["livejournal", "личный блог", "мой блог", "мое мнение", "моё мнение", "авторский блог"];
const SPECULATIVE_MARKERS: &[&str] = &[
    "высший разум", "телепат", "астраль", "эзотер", "пятое измерение", "пять измерений",
    "энергетическое тело", "контактёр", "контактер", "ченнелинг", "откровение свыше", "между мирами",
];
const STRONG_SCIENTIFIC_MARKERS: &[&str] = &[
    "doi:", "doi.org/", "peer-reviewed", "рецензируемая статья", "рецензируемое исследование",
    "volume ", "issue ", "abstract", "references", "bibliography",
];
const NEWS_MARKERS: &[&str] = &["информационное агентство", "редакция", "новости", "корреспондент", "опубликовано", "автор:"];

/// agent/source_quality.py::_refine_source_class — возвращает (class, authority, primaryness, reasons)
fn refine_source_class(
    source_class: &'static str,
    authority: f64,
    primaryness: f64,
    url: &str,
    title: &str,
    text: &str,
) -> (&'static str, f64, f64, Vec<String>) {
    let text_head: String = text.chars().take(5000).collect();
    let combined = format!("{url} {title} {text_head}").to_lowercase();

    if FORUM_MARKERS.iter().any(|m| combined.contains(m)) {
        return ("forum", authority.min(0.30), primaryness.min(0.25), vec!["content indicates forum/community source".to_string()]);
    }
    if BLOG_MARKERS.iter().any(|m| combined.contains(m)) {
        return ("blog_opinion", authority.min(0.35), primaryness.min(0.30), vec!["content indicates personal/blog source".to_string()]);
    }

    let speculative_hits = SPECULATIVE_MARKERS.iter().filter(|m| combined.contains(**m)).count();
    let title_url = format!("{url} {title}").to_lowercase();
    let strong_speculative_hit = SPECULATIVE_MARKERS.iter().any(|m| title_url.contains(m));
    if speculative_hits >= 2 || strong_speculative_hit {
        return (
            "speculative",
            authority.min(0.20),
            primaryness.min(0.20),
            vec![format!(
                "content contains speculative markers: {speculative_hits}; strong_title_url={}",
                if strong_speculative_hit { "True" } else { "False" }
            )],
        );
    }

    let strong_scientific_hits = STRONG_SCIENTIFIC_MARKERS.iter().filter(|m| combined.contains(**m)).count();
    if source_class == "unknown" && strong_scientific_hits >= 2 {
        return (
            "scientific",
            authority.max(0.75),
            primaryness.max(0.65),
            vec![format!("content contains strong scientific provenance markers: {strong_scientific_hits}")],
        );
    }

    let news_hits = NEWS_MARKERS.iter().filter(|m| combined.contains(**m)).count();
    if news_hits >= 2 {
        return (
            "news",
            authority.min(0.60).max(0.50),
            primaryness.min(0.40),
            vec![format!("content indicates editorial/news source: {news_hits}")],
        );
    }

    (source_class, authority, primaryness, Vec::new())
}

#[derive(Debug, Clone, PartialEq)]
pub struct SourceQualityResult {
    pub quality_score: f64,
    pub source_class: String,
    pub evidence_eligible: bool,
    pub evidence_role: String,
    pub authority: f64,
    pub traceability: f64,
    pub primaryness: f64,
    pub reasons: Vec<String>,
}

/// Python round(x, 3) — корректно округлённое (по ТОЧНОМУ двоичному значению x, не по
/// приближению) десятичное округление, а не наивное "умножить на 1000, округлить, поделить
/// обратно". Наивный вариант был здесь первой попыткой и НЕ совпал с Python на реальном случае
/// (authority=0.75, traceability=1.0, primaryness=0.45 -> raw=0.75*0.45+1.0*0.35+0.45*0.20;
/// истинное двоичное значение чуть МЕНЬШЕ 0.7775, поэтому Python корректно даёт 0.777, а
/// умножение на 1000 в f64 вносит свою собственную ошибку и наивная версия давала 0.778) — эту
/// ошибку поймал сам parity-тест (agent/source_quality_rust_parity_test.py). Форматирование
/// f64 в Rust через `{:.N}` использует корректно-округляющий алгоритм (тот же класс алгоритмов,
/// что и Python) — round-trip через строку не потому, что "работает", а потому что это и есть
/// правильный способ получить тот же ответ, что `round()` в CPython.
fn round_half_even(x: f64, digits: usize) -> f64 {
    format!("{x:.digits$}").parse::<f64>().unwrap_or(x)
}

/// agent/source_quality.py::evaluate_source_quality
pub fn evaluate_source_quality(url: &str, title: &str, text: &str, source_type: &str) -> SourceQualityResult {
    let mut reasons: Vec<String> = Vec::new();
    let host = hostname(url);
    let (mut source_class, mut authority, mut primaryness) = classify_source(&host, url);

    let (refined_class, refined_authority, refined_primaryness, refinement_reasons) =
        refine_source_class(source_class, authority, primaryness, url, title, text);
    source_class = refined_class;
    authority = refined_authority;
    primaryness = refined_primaryness;
    reasons.extend(refinement_reasons);

    let mut traceability = 0.0f64;
    if !url.is_empty() {
        traceability += 0.45;
        reasons.push("source has URL".to_string());
    }
    if !crate::py_text::py_strip(title).is_empty() && crate::py_text::py_strip(title).chars().count() >= 5 {
        traceability += 0.20;
        reasons.push("source has title".to_string());
    }
    let clean_text = crate::py_text::py_strip(text);
    let clean_len = clean_text.chars().count();
    if clean_len >= 200 {
        traceability += 0.20;
        reasons.push("source contains substantial text".to_string());
    }
    if clean_len >= 1000 {
        traceability += 0.10;
        reasons.push("source contains extended text".to_string());
    }
    if !host.is_empty() {
        traceability += 0.05;
    }
    traceability = traceability.min(1.0);

    let mut source_class_owned = source_class.to_string();
    if source_type.to_lowercase().contains("generated") {
        source_class_owned = "generated_pipeline".to_string();
        authority = authority.min(0.20);
        primaryness = 0.10;
        reasons.push("pipeline-generated source".to_string());
    }

    let quality_score_raw = authority * 0.45 + traceability * 0.35 + primaryness * 0.20;
    let quality_score = round_half_even(quality_score_raw.clamp(0.0, 1.0), 3);

    const BLOCKED_CLASSES: &[&str] =
        &["generated_pipeline", "social", "forum", "blog_opinion", "speculative", "news", "popular_article"];

    let evidence_eligible;
    if BLOCKED_CLASSES.contains(&source_class_owned.as_str()) {
        evidence_eligible = false;
        reasons.push(format!("source class {source_class_owned} is not eligible as standalone factual evidence"));
    } else if source_class_owned == "unknown" {
        evidence_eligible = quality_score >= 0.70 && authority >= 0.50 && traceability >= 0.70;
        reasons.push(if evidence_eligible {
            "unknown source passed strict quality gate".to_string()
        } else {
            "unknown source failed strict quality gate".to_string()
        });
    } else {
        evidence_eligible = quality_score >= 0.55 && traceability >= 0.50;
        reasons.push(if evidence_eligible {
            "source passed base quality gate".to_string()
        } else {
            "source failed base quality gate".to_string()
        });
    }

    let evidence_role = if matches!(source_class_owned.as_str(), "primary" | "scientific" | "reference") {
        "direct"
    } else if matches!(source_class_owned.as_str(), "news" | "popular_article") {
        "secondary"
    } else if source_class_owned == "generated_pipeline" {
        "internal"
    } else {
        "context"
    };
    reasons.push(format!("evidence role: {evidence_role}"));

    SourceQualityResult {
        quality_score,
        source_class: source_class_owned,
        evidence_eligible,
        evidence_role: evidence_role.to_string(),
        authority: round_half_even(authority, 3),
        traceability: round_half_even(traceability, 3),
        primaryness: round_half_even(primaryness, 3),
        reasons,
    }
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────
// Возвращаем dict, а не отдельный pyclass: SourceQualityResult в Python — настоящий @dataclass
// (со своими __eq__/__repr__, на которые может опираться другой код); Rust-обвязка должна
// собрать ТОЧНО ТАКОЙ ЖЕ Python-объект, а не завести второй, отдельный тип с тем же именем.
// Делегирующий код в agent/source_quality.py сам строит SourceQualityResult(**dict).

#[pyfunction]
#[pyo3(name = "evaluate_source_quality", signature = (url, title="", text="", source_type="web"))]
fn py_evaluate_source_quality<'py>(
    py: Python<'py>,
    url: &str,
    title: &str,
    text: &str,
    source_type: &str,
) -> PyResult<Bound<'py, PyDict>> {
    let r = evaluate_source_quality(url, title, text, source_type);
    let d = PyDict::new_bound(py);
    d.set_item("quality_score", r.quality_score)?;
    d.set_item("source_class", r.source_class)?;
    d.set_item("evidence_eligible", r.evidence_eligible)?;
    d.set_item("evidence_role", r.evidence_role)?;
    d.set_item("authority", r.authority)?;
    d.set_item("traceability", r.traceability)?;
    d.set_item("primaryness", r.primaryness)?;
    d.set_item("reasons", r.reasons)?;
    Ok(d)
}

#[pyfunction]
#[pyo3(name = "hostname")]
fn py_hostname(url: &str) -> String {
    hostname(url)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_evaluate_source_quality, m)?)?;
    m.add_function(wrap_pyfunction!(py_hostname, m)?)?;
    Ok(())
}

// ── Юнит-тесты (переносы сценариев из agent/evidence_eligibility_regression_test.py) ────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hostname_variants() {
        assert_eq!(hostname("https://www.nature.com/articles/test"), "nature.com");
        assert_eq!(hostname("https://en.wikipedia.org/wiki/Jupiter"), "en.wikipedia.org");
        assert_eq!(hostname("http://example.com:8080/x"), "example.com");
        assert_eq!(hostname(""), "");
        assert_eq!(hostname("not-a-url-at-all"), "");
        assert_eq!(hostname("//protocol-relative.example/x"), "protocol-relative.example");
    }

    #[test]
    fn gov_domain_is_primary() {
        let r = evaluate_source_quality("https://science.nasa.gov/x", "T".repeat(10).as_str(), "X".repeat(1500).as_str(), "web");
        assert_eq!(r.source_class, "primary");
        assert!(r.evidence_eligible);
    }

    #[test]
    fn wikipedia_is_reference_and_eligible() {
        let r = evaluate_source_quality("https://en.wikipedia.org/wiki/X", "T".repeat(10).as_str(), "X".repeat(1500).as_str(), "web");
        assert_eq!(r.source_class, "reference");
        assert!(r.evidence_eligible);
    }

    #[test]
    fn reddit_is_forum_and_blocked() {
        let r = evaluate_source_quality("https://reddit.com/r/x/y", "T".repeat(10).as_str(), "X".repeat(1500).as_str(), "web");
        assert_eq!(r.source_class, "forum");
        assert!(!r.evidence_eligible);
    }

    #[test]
    fn generated_pipeline_source_type_overrides_class() {
        let r = evaluate_source_quality("", "", "generated answer", "generated_pipeline");
        assert_eq!(r.source_class, "generated_pipeline");
        assert!(!r.evidence_eligible);
        assert_eq!(r.evidence_role, "internal");
    }

    #[test]
    fn no_url_no_traceability_from_url() {
        let r = evaluate_source_quality("", "Registry doc", "X".repeat(1500).as_str(), "local");
        assert_eq!(hostname(""), "");
        assert!(!r.reasons.iter().any(|s| s == "source has URL"));
    }
}
