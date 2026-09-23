//! Перенос agent/message_intensity.py — parse_self_report() (+ intensity_from_state,
//! _parse_structured, _strip_all_markers). Чистый текстовый/JSON парсинг, без сети и БД,
//! вызывается на КАЖДЫЙ ответ локальной модели (pet/chat_local.py) — горячий путь.
//!
//! IntensityResult собирается в Python-обёртке из dict, который отдаёт Rust — тот же принцип,
//! что для SourceQualityResult/CriticismAnalysis (Rust отдаёт данные, Python строит настоящий
//! @dataclass). Поле `spans` в оригинале НИГДЕ не устанавливается ни одной из перенесённых
//! функций (всегда остаётся дефолтным `()`) — сохранено как есть, не добавлено вычисление.
//!
//! Python `bool(state[key])` — это truthiness ЛЮБОГО JSON-значения (0/""/[]/{}/null -> false,
//! всё остальное -> true), не просто bool-каст. Реализовано здесь через `json_truthy()`,
//! повторяющую эти же правила на serde_json::Value, а не через Rust bool-конверсию.
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_MESSAGE_INTENSITY_ENGINE=rust (см. agent/message_intensity.py).

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use regex::Regex;
use serde_json::Value as Json;

static MARKER_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)#{0,3}\s*YANDI[_\s.-]STATE\s*#{0,3}").expect("статический паттерн валиден"));
static STRIP_ALL_MARKERS_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)#{0,3}\s*YANDI[_\s.-]STATE\s*#{0,3}\s*:?\s*").expect("статический паттерн валиден"));
// Жадный "{...}" через возможные переносы строк — эквивалент Python re.DOTALL.
static JSON_OBJECT_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?s)\{.*\}").expect("статический паттерн валиден"));

#[derive(Debug, Clone)]
pub struct IntensityResult {
    pub ok: bool,
    pub is_insult: bool,
    pub is_apology: bool,
    pub severity: f64,
    pub sincerity: f64,
    pub error: String,
    pub is_promise: bool,
    pub claims_fulfilled: bool,
    pub evidence: String,
}

impl IntensityResult {
    fn neutral(error: &str) -> Self {
        Self {
            ok: false,
            is_insult: false,
            is_apology: false,
            severity: 0.0,
            sincerity: 0.0,
            error: error.to_string(),
            is_promise: false,
            claims_fulfilled: false,
            evidence: String::new(),
        }
    }
}

/// agent/message_intensity.py::_strip_all_markers
pub fn strip_all_markers(text: &str) -> String {
    STRIP_ALL_MARKERS_RE.replace_all(text, " ").trim().to_string()
}

/// Python bool(x) truthiness для JSON-значения, как оно приходит из json.loads.
fn json_truthy(v: &Json) -> bool {
    match v {
        Json::Null => false,
        Json::Bool(b) => *b,
        Json::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Json::String(s) => !s.is_empty(),
        Json::Array(a) => !a.is_empty(),
        Json::Object(o) => !o.is_empty(),
    }
}

/// Python float(x) — включая терпимость к числовым строкам ("0.5" -> 0.5), как настоящий float().
fn json_as_f64(v: &Json) -> Option<f64> {
    match v {
        Json::Number(n) => n.as_f64(),
        Json::String(s) => s.trim().parse::<f64>().ok(),
        Json::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}

/// agent/message_intensity.py::intensity_from_state — работает на уже распарсенном JSON-объекте
/// (то, что реально приходит из json.loads в обоих реальных вызывающих местах этого модуля).
pub fn intensity_from_state_json(state: &Json, error: &str) -> IntensityResult {
    let obj = match state.as_object() {
        Some(o) => o,
        None => return IntensityResult::neutral("semantic state missing or not an object"),
    };
    let is_insult = match obj.get("is_insult") {
        Some(v) => json_truthy(v),
        None => return IntensityResult::neutral("semantic state missing/invalid fields: 'is_insult'"),
    };
    let is_apology = match obj.get("is_apology") {
        Some(v) => json_truthy(v),
        None => return IntensityResult::neutral("semantic state missing/invalid fields: 'is_apology'"),
    };
    let severity = match obj.get("severity").and_then(json_as_f64) {
        Some(v) => v.clamp(0.0, 1.0),
        None => return IntensityResult::neutral("semantic state missing/invalid fields: 'severity'"),
    };
    let sincerity = match obj.get("sincerity").and_then(json_as_f64) {
        Some(v) => v.clamp(0.0, 1.0),
        None => return IntensityResult::neutral("semantic state missing/invalid fields: 'sincerity'"),
    };
    let is_promise = obj.get("is_promise").map(|v| v == &Json::Bool(true)).unwrap_or(false);
    let claims_fulfilled = obj.get("claims_fulfilled").map(|v| v == &Json::Bool(true)).unwrap_or(false);

    IntensityResult {
        ok: true,
        is_insult,
        is_apology,
        severity,
        sincerity,
        error: error.to_string(),
        is_promise,
        claims_fulfilled,
        evidence: String::new(),
    }
}

/// agent/message_intensity.py::_parse_structured — None если форма не подходит (вызывающий код
/// падает обратно на легаси-путь через маркер).
pub fn parse_structured_json(data: &Json) -> Option<(String, IntensityResult)> {
    let obj = data.as_object()?;
    let reply_v = obj.get("reply")?;
    let state_v = obj.get("state")?;
    let reply = reply_v.as_str()?;
    if !state_v.is_object() {
        return None;
    }

    let result = intensity_from_state_json(state_v, "");
    if !result.ok {
        let mut r = result;
        r.error = r.error.replacen("semantic", "structured", 1);
        return Some((reply.trim().to_string(), r));
    }
    if reply.trim().is_empty() {
        let mut r = result;
        r.error = "model produced no visible reply, tag only".to_string();
        return Some((String::new(), r));
    }
    Some((reply.trim().to_string(), result))
}

/// agent/message_intensity.py::parse_self_report
pub fn parse_self_report(raw: &str) -> (String, IntensityResult) {
    let stripped = raw.trim();
    if stripped.starts_with('{') {
        if let Ok(data) = serde_json::from_str::<Json>(stripped) {
            if data.is_object() {
                if let Some(structured) = parse_structured_json(&data) {
                    return structured;
                }
            }
        }
    }

    let matches: Vec<_> = MARKER_RE.find_iter(raw).collect();
    let Some(last) = matches.last() else {
        return (raw.trim().to_string(), IntensityResult::neutral("no state marker found in model output"));
    };

    let visible = raw[..last.start()].trim().to_string();
    let tail = &raw[last.end()..];

    let Some(json_match) = JSON_OBJECT_RE.find(tail) else {
        let fallback = if !visible.is_empty() { visible } else { strip_all_markers(raw) };
        let tail_preview: String = tail.chars().take(200).collect();
        // Python `{tail[:200]!r}`: repr() по умолчанию оборачивает в одинарные кавычки (кроме
        // редкого случая текста с одинарной, но без двойной кавычки внутри — этот единственный
        // угловой случай здесь не воспроизведён, цена/выгода не оправданы для диагностической
        // строки; во всех реалистичных случаях битого JSON совпадает побайтово).
        return (fallback, IntensityResult::neutral(&format!("marker present but no JSON object after it: '{tail_preview}'")));
    };

    let data: Json = match serde_json::from_str(json_match.as_str()) {
        Ok(d) => d,
        Err(e) => {
            let fallback = if !visible.is_empty() { visible.clone() } else { strip_all_markers(raw) };
            return (fallback, IntensityResult::neutral(&format!("malformed JSON after marker: {e}")));
        }
    };

    let obj = match data.as_object() {
        Some(o) => o,
        None => {
            let fallback = if !visible.is_empty() { visible.clone() } else { strip_all_markers(raw) };
            return (fallback, IntensityResult::neutral("state JSON missing/invalid expected fields"));
        }
    };

    let get_bool = |k: &str| obj.get(k).map(json_truthy);
    let get_f64 = |k: &str| obj.get(k).and_then(json_as_f64);

    // Python вычисляет data["is_insult"]/data["is_apology"]/data["severity"]/data["sincerity"]
    // В ЭТОМ ПОРЯДКЕ как позиционные kwargs конструктора — первый же отсутствующий/невалидный
    // ключ бросает KeyError с ИМЕНЕМ ЭТОГО ключа, message "...: 'имя_поля'". Воспроизведено:
    // проверяем в том же порядке, останавливаемся на первом отсутствующем.
    for field in ["is_insult", "is_apology", "severity", "sincerity"] {
        let present = match field {
            "is_insult" | "is_apology" => get_bool(field).is_some(),
            _ => get_f64(field).is_some(),
        };
        if !present {
            let fallback = if !visible.is_empty() { visible.clone() } else { strip_all_markers(raw) };
            return (fallback, IntensityResult::neutral(&format!("state JSON missing/invalid expected fields: '{field}'")));
        }
    }

    let (is_insult, is_apology, severity, sincerity) = match (get_bool("is_insult"), get_bool("is_apology"), get_f64("severity"), get_f64("sincerity")) {
        (Some(ii), Some(ia), Some(sv), Some(sc)) => (ii, ia, sv.clamp(0.0, 1.0), sc.clamp(0.0, 1.0)),
        _ => {
            let fallback = if !visible.is_empty() { visible.clone() } else { strip_all_markers(raw) };
            return (fallback, IntensityResult::neutral("state JSON missing/invalid expected fields"));
        }
    };

    let result = IntensityResult {
        ok: true,
        is_insult,
        is_apology,
        severity,
        sincerity,
        error: String::new(),
        is_promise: false,
        claims_fulfilled: false,
        evidence: String::new(),
    };

    if visible.is_empty() {
        let mut r = result;
        r.error = "model produced no visible reply, tag only".to_string();
        return (String::new(), r);
    }

    (visible, result)
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

fn result_to_dict<'py>(py: Python<'py>, r: &IntensityResult) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new_bound(py);
    d.set_item("ok", r.ok)?;
    d.set_item("is_insult", r.is_insult)?;
    d.set_item("is_apology", r.is_apology)?;
    d.set_item("severity", r.severity)?;
    d.set_item("sincerity", r.sincerity)?;
    d.set_item("error", &r.error)?;
    d.set_item("is_promise", r.is_promise)?;
    d.set_item("claims_fulfilled", r.claims_fulfilled)?;
    d.set_item("evidence", &r.evidence)?;
    Ok(d)
}

#[pyfunction]
#[pyo3(name = "strip_all_markers")]
fn py_strip_all_markers(text: &str) -> String {
    strip_all_markers(text)
}

/// state: любой Python-объект — сериализуется через json.dumps (тот же путь, каким реально
/// приходят эти данные из распарсенного ответа модели), затем разбирается тем же JSON-парсером,
/// что и остальной модуль — не отдельная, потенциально расходящаяся конверсия типов.
#[pyfunction]
#[pyo3(name = "intensity_from_state", signature = (state, error=""))]
fn py_intensity_from_state<'py>(py: Python<'py>, state: &Bound<'py, PyAny>, error: &str) -> PyResult<Bound<'py, PyDict>> {
    let json_mod = py.import_bound("json")?;
    let dumped: PyResult<String> = json_mod.call_method1("dumps", (state,)).and_then(|v| v.extract());
    let result = match dumped {
        Ok(s) => match serde_json::from_str::<Json>(&s) {
            Ok(v) => intensity_from_state_json(&v, error),
            Err(_) => IntensityResult::neutral("semantic state missing or not an object"),
        },
        Err(_) => IntensityResult::neutral("semantic state missing or not an object"),
    };
    result_to_dict(py, &result)
}

#[pyfunction]
#[pyo3(name = "parse_self_report")]
fn py_parse_self_report<'py>(py: Python<'py>, raw: &str) -> PyResult<(String, Bound<'py, PyDict>)> {
    let (visible, result) = parse_self_report(raw);
    Ok((visible, result_to_dict(py, &result)?))
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_strip_all_markers, m)?)?;
    m.add_function(wrap_pyfunction!(py_intensity_from_state, m)?)?;
    m.add_function(wrap_pyfunction!(py_parse_self_report, m)?)?;
    Ok(())
}

// ── Юнит-тесты (сценарии из agent/message_intensity_regression_test.py) ─────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn well_formed_tag() {
        let raw = "Ого, сразу переходим к оскорблениям? Мне это не нравится.\n\n###YANDI_STATE###\n{\"is_insult\": true, \"is_apology\": false, \"severity\": 0.75, \"sincerity\": 0.0}";
        let (visible, r) = parse_self_report(raw);
        assert_eq!(visible, "Ого, сразу переходим к оскорблениям? Мне это не нравится.");
        assert!(r.ok);
        assert!(r.is_insult);
        assert!(!r.is_apology);
        assert_eq!(r.severity, 0.75);
        assert_eq!(r.sincerity, 0.0);
    }

    #[test]
    fn severity_clamped() {
        let raw = "Текст\n\n###YANDI_STATE###\n{\"is_insult\": false, \"is_apology\": true, \"severity\": 5.0, \"sincerity\": -3.0}";
        let (_, r) = parse_self_report(raw);
        assert_eq!(r.severity, 1.0);
        assert_eq!(r.sincerity, 0.0);
    }

    #[test]
    fn no_marker_returns_full_text_untouched() {
        let raw = "Просто обычный ответ без всякого тега состояния.";
        let (visible, r) = parse_self_report(raw);
        assert_eq!(visible, raw);
        assert!(!r.ok);
        assert!(!r.is_insult && !r.is_apology);
    }

    #[test]
    fn malformed_json_recovers_visible_prefix() {
        let raw = "Ладно.\n\n###YANDI_STATE###\n{not valid json at all";
        let (visible, r) = parse_self_report(raw);
        assert_eq!(visible, "Ладно.");
        assert!(!r.ok);
    }

    #[test]
    fn missing_field_recovers_visible_prefix() {
        let raw = "Хорошо.\n\n###YANDI_STATE###\n{\"is_insult\": false, \"severity\": 0.1}";
        let (visible, r) = parse_self_report(raw);
        assert_eq!(visible, "Хорошо.");
        assert!(!r.ok);
    }

    #[test]
    fn tag_only_empty_reply_flagged_not_fabricated() {
        let raw = "\n###YANDI_STATE###\n{\"is_insult\": false, \"is_apology\": false, \"severity\": 0.0, \"sincerity\": 0.0}";
        let (visible, r) = parse_self_report(raw);
        assert_eq!(visible, "");
        assert!(r.ok);
        assert!(r.error.contains("no visible reply"));
    }

    #[test]
    fn structured_contract() {
        let raw = r#"{"reply": "Ну вот опять — и всё из-за чего?", "state": {"is_insult": true, "is_apology": false, "severity": 0.7, "sincerity": 0.1}}"#;
        let (visible, r) = parse_self_report(raw);
        assert_eq!(visible, "Ну вот опять — и всё из-за чего?");
        assert!(r.ok && r.is_insult);
        assert_eq!(r.severity, 0.7);
    }

    #[test]
    fn structured_bad_fields_fail_open_reply_shown() {
        let raw = r#"{"reply": "Ладно.", "state": {"is_insult": true}}"#;
        let (visible, r) = parse_self_report(raw);
        assert_eq!(visible, "Ладно.");
        assert!(!r.ok);
    }

    #[test]
    fn structured_empty_reply_flagged() {
        let raw = r#"{"reply": "", "state": {"is_insult": false, "is_apology": false, "severity": 0.0, "sincerity": 0.0}}"#;
        let (visible, r) = parse_self_report(raw);
        assert_eq!(visible, "");
        assert!(r.ok);
        assert!(r.error.contains("no visible reply"));
    }

    #[test]
    fn json_truthy_matches_python_bool() {
        assert!(!json_truthy(&Json::Null));
        assert!(!json_truthy(&serde_json::json!(0)));
        assert!(json_truthy(&serde_json::json!(0.5)));
        assert!(!json_truthy(&serde_json::json!("")));
        assert!(json_truthy(&serde_json::json!("x")));
        assert!(!json_truthy(&serde_json::json!([])));
        assert!(json_truthy(&serde_json::json!([1])));
    }
}
