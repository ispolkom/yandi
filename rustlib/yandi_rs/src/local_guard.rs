//! Перенос pet/local_guard.py — только ЧИСТАЯ логика решения «пускать запрос или нет».
//!
//! ASGI-обвязка (WebSocket-рукопожатие, сборка HTTP-ответа, извлечение заголовков из ASGI scope,
//! обнаружение задвоенных заголовков) остаётся в Python — это Starlette/ASGI-специфика, ей тут не
//! место. Сюда перенесены только функции, которые в pet/local_guard.py уже были чистыми (без
//! обращения к сети/диску): дано (host, origin, sec-fetch-site, путь) -> (allowed, reason).
//!
//! Каждая функция здесь — построчный перевод одноимённой функции в pet/local_guard.py. При любом
//! изменении правила ТАМ нужно обновить и ЗДЕСЬ — это не проверяется автоматически компилятором,
//! только тестом (pet/pet_local_guard_rust_parity_test.py), поэтому оба места держим рядом внутри
//! ревью одного и того же коммита.
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python-версией; в бою НЕ включено
//! по умолчанию — pet/local_guard.py продолжает работать на оригинальной Python-реализации. Включить
//! эту, Rust-реализацию можно переменной окружения YANDI_GUARD_ENGINE=rust (см. pet/local_guard.py) —
//! это решение осознанно оставлено на владельца, не включено автоматически.

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use regex::Regex;
use std::collections::HashSet;

/// pet/local_guard.py::EXTENSION_ORIGIN_RE.pattern — без якорей `^`/`$`, как и в Python (там их
/// добавляет не паттерн, а вызов `.fullmatch()`; здесь то же самое делает `fullmatch()` ниже).
pub const EXTENSION_ORIGIN_PATTERN: &str =
    r"moz-extension://[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";

static EXTENSION_ORIGIN_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(EXTENSION_ORIGIN_PATTERN).expect("статический паттерн всегда валиден"));

static LOOPBACK_HOSTS: Lazy<HashSet<&'static str>> =
    Lazy::new(|| ["127.0.0.1", "localhost", "::1"].into_iter().collect());

pub const EXTENSION_PATH_PREFIXES: &[&str] = &["/api/ext/"];

static EXTENSION_PATHS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    [
        "/api/orchestrator/ask",
        "/api/orch/history",
        "/api/council/connections",
    ]
    .into_iter()
    .collect()
});

/// Точный эквивалент Python re.fullmatch: весь `s`, от первого до последнего символа, должен
/// совпасть с `re` — не полагаемся на семантику `^`/`$` в движке regex (она в Rust и Python не
/// всегда совпадает при переносах строки), проверяем span совпадения напрямую.
fn fullmatch(re: &Regex, s: &str) -> bool {
    matches!(re.find(s), Some(m) if m.start() == 0 && m.end() == s.len())
}

/// pet/local_guard.py::_host_name
pub fn host_name(host_header: &str) -> String {
    let host = host_header.trim().to_lowercase();
    if let Some(rest) = host.strip_prefix('[') {
        // "[::1]:9010" -> "::1"
        return match rest.find(']') {
            Some(i) => rest[..i].to_string(),
            None => String::new(),
        };
    }
    if host.matches(':').count() == 1 {
        host.rsplit_once(':')
            .map(|(h, _)| h.to_string())
            .unwrap_or(host)
    } else {
        host
    }
}

/// pet/local_guard.py::is_extension_path
pub fn is_extension_path(path: &str) -> bool {
    if path.is_empty() || path.contains("..") || path.contains("//") || path.contains('\\') {
        return false;
    }
    if EXTENSION_PATHS.contains(path) {
        return true;
    }
    EXTENSION_PATH_PREFIXES
        .iter()
        .any(|p| path.starts_with(p) && path.len() > p.len())
}

/// pet/local_guard.py::is_allowed_request.
/// `host` — сырое значение заголовка Host, БЕЗ нормализации (используется как есть при сборке
/// ожидаемого "http://{host}", ровно как в Python — там тоже сравнивают с сырым `host`, а не с
/// результатом `_host_name(host)`). `origin`/`sec_fetch_site` — None означает «заголовка не было»
/// (отличие от «заголовок есть и пустая строка» сохранено, как и в Python `headers.get(...)`).
pub fn is_allowed_request(
    host: Option<&str>,
    origin: Option<&str>,
    sec_fetch_site: Option<&str>,
    path: Option<&str>,
) -> (bool, &'static str) {
    let host = host.unwrap_or("");
    if !LOOPBACK_HOSTS.contains(host_name(host).as_str()) {
        return (false, "запрос адресован не локальному хосту");
    }
    if let Some(origin) = origin {
        let expected = format!("http://{host}");
        if origin != expected {
            if let Some(path) = path {
                if fullmatch(&EXTENSION_ORIGIN_RE, origin) && is_extension_path(path) {
                    return (true, "");
                }
            }
            return (false, "запрос отправлен страницей другого сайта");
        }
    }
    if let Some(site) = sec_fetch_site {
        if site != "same-origin" && site != "none" {
            return (false, "запрос отправлен страницей другого сайта");
        }
    }
    (true, "")
}

/// pet/local_guard.py::is_local_request — строгая форма (расширению не открыто ничего).
pub fn is_local_request(
    host: Option<&str>,
    origin: Option<&str>,
    sec_fetch_site: Option<&str>,
) -> (bool, &'static str) {
    is_allowed_request(host, origin, sec_fetch_site, None)
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(name = "host_name")]
fn py_host_name(host_header: &str) -> String {
    host_name(host_header)
}

#[pyfunction]
#[pyo3(name = "is_extension_path")]
fn py_is_extension_path(path: &str) -> bool {
    is_extension_path(path)
}

fn get_str(headers: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    match headers.get_item(key)? {
        Some(v) => Ok(Some(v.extract::<String>()?)),
        None => Ok(None),
    }
}

/// headers: Python dict {"host": "...", "origin": "...", "sec-fetch-site": "..."} — ключ
/// отсутствует, если заголовка не было (не кладите None значением, кладите отсутствие ключа).
/// path: Optional[str]. Возвращает (allowed, reason) — тот же контракт, что и в pet/local_guard.py.
#[pyfunction]
#[pyo3(name = "is_allowed_request", signature = (headers, path=None))]
fn py_is_allowed_request(
    headers: &Bound<'_, PyDict>,
    path: Option<&str>,
) -> PyResult<(bool, String)> {
    let host = get_str(headers, "host")?;
    let origin = get_str(headers, "origin")?;
    let sfs = get_str(headers, "sec-fetch-site")?;
    let (allowed, reason) =
        is_allowed_request(host.as_deref(), origin.as_deref(), sfs.as_deref(), path);
    Ok((allowed, reason.to_string()))
}

#[pyfunction]
#[pyo3(name = "is_local_request")]
fn py_is_local_request(headers: &Bound<'_, PyDict>) -> PyResult<(bool, String)> {
    py_is_allowed_request(headers, None)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_host_name, m)?)?;
    m.add_function(wrap_pyfunction!(py_is_extension_path, m)?)?;
    m.add_function(wrap_pyfunction!(py_is_allowed_request, m)?)?;
    m.add_function(wrap_pyfunction!(py_is_local_request, m)?)?;
    Ok(())
}

// ── Юнит-тесты (чистый `cargo test`, до всякого PyO3/Python) ────────────────
// Каждый случай — перенос сценария из pet/pet_web_guard_regression_test.py, чтобы у Rust- и
// Python-теста с самого начала был один и тот же контракт, а не «придумано отдельно».
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_name_variants() {
        assert_eq!(host_name("127.0.0.1:9010"), "127.0.0.1");
        assert_eq!(host_name("127.0.0.1"), "127.0.0.1");
        assert_eq!(host_name("[::1]:9010"), "::1");
        assert_eq!(host_name("LOCALHOST:9010"), "localhost");
        assert_eq!(host_name(""), "");
        assert_eq!(host_name("["), ""); // "[" без закрывающей "]" -> пусто, как в Python
    }

    #[test]
    fn extension_path_rules() {
        assert!(is_extension_path("/api/ext/poll"));
        assert!(is_extension_path("/api/orchestrator/ask"));
        assert!(is_extension_path("/api/orch/history"));
        assert!(is_extension_path("/api/council/connections"));
        assert!(!is_extension_path("/api/extra")); // похожий префикс, но не "/api/ext/"
        assert!(!is_extension_path("/api/ext/../secret"));
        assert!(!is_extension_path("/api//ext/poll"));
        assert!(!is_extension_path("/api/ext\\poll"));
        assert!(!is_extension_path(""));
        assert!(!is_extension_path("/api/ext/")); // ровно префикс, без хвоста после него
    }

    #[test]
    fn own_page_same_origin_allowed() {
        let (ok, reason) = is_allowed_request(
            Some("127.0.0.1:9010"),
            Some("http://127.0.0.1:9010"),
            Some("same-origin"),
            None,
        );
        assert!(ok, "reason={reason}");
    }

    #[test]
    fn no_headers_at_all_allowed_local_program() {
        // curl / агент / нода — не шлют ни Origin, ни Sec-Fetch-Site
        let (ok, _) = is_allowed_request(Some("127.0.0.1:9010"), None, None, None);
        assert!(ok);
    }

    #[test]
    fn foreign_host_rejected() {
        let (ok, reason) = is_allowed_request(Some("evil.example"), None, None, None);
        assert!(!ok);
        assert_eq!(reason, "запрос адресован не локальному хосту");
    }

    #[test]
    fn foreign_origin_rejected() {
        let (ok, reason) =
            is_allowed_request(Some("127.0.0.1:9010"), Some("http://evil.example"), None, None);
        assert!(!ok);
        assert_eq!(reason, "запрос отправлен страницей другого сайта");
    }

    #[test]
    fn cross_site_fetch_rejected() {
        let (ok, _) = is_allowed_request(Some("127.0.0.1:9010"), None, Some("cross-site"), None);
        assert!(!ok);
    }

    #[test]
    fn extension_allowed_only_on_its_paths() {
        let ext = "moz-extension://0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
        let (ok_allowed, _) = is_allowed_request(
            Some("127.0.0.1:9010"),
            Some(ext),
            Some("cross-site"),
            Some("/api/ext/poll"),
        );
        assert!(ok_allowed);
        let (ok_denied, _) = is_allowed_request(
            Some("127.0.0.1:9010"),
            Some(ext),
            Some("cross-site"),
            Some("/api/secret"),
        );
        assert!(!ok_denied);
    }

    #[test]
    fn extension_never_allowed_when_path_unknown() {
        let ext = "moz-extension://0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
        let (ok, _) = is_allowed_request(Some("127.0.0.1:9010"), Some(ext), Some("cross-site"), None);
        assert!(!ok, "is_local_request по сути и есть path=None — расширению тут не место");
    }

    #[test]
    fn fake_extension_uuid_rejected() {
        let (ok, _) = is_allowed_request(
            Some("127.0.0.1:9010"),
            Some("moz-extension://not-a-real-uuid"),
            Some("cross-site"),
            Some("/api/ext/poll"),
        );
        assert!(!ok);
    }

    #[test]
    fn is_local_request_matches_allowed_request_with_no_path() {
        for (host, origin, site) in [
            (Some("127.0.0.1:9010"), None, None),
            (Some("evil.example"), None, None),
            (Some("127.0.0.1:9010"), Some("http://evil.example"), None),
        ] {
            assert_eq!(
                is_local_request(host, origin, site),
                is_allowed_request(host, origin, site, None)
            );
        }
    }
}
