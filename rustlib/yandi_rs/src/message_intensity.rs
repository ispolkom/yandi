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
//! повторяющую эти же правила на PyJson, а не через Rust bool-конверсию.
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_MESSAGE_INTENSITY_ENGINE=rust (см. agent/message_intensity.py).

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use regex::Regex;

use crate::py_json::{self, Escalate, LoadsError, PyJson};
use crate::py_text::{py_float, py_repr_str, py_strip};

static MARKER_RE: Lazy<Regex> =
    Lazy::new(|| crate::py_text::py_regex(r"(?i)#{0,3}\s*YANDI[_\s.-]STATE\s*#{0,3}"));
static STRIP_ALL_MARKERS_RE: Lazy<Regex> =
    Lazy::new(|| crate::py_text::py_regex(r"(?i)#{0,3}\s*YANDI[_\s.-]STATE\s*#{0,3}\s*:?\s*"));
// Жадный "{...}" через возможные переносы строк — эквивалент Python re.DOTALL.
static JSON_OBJECT_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?s)\{.*\}").expect("статический паттерн валиден"));

/// Исключения Python, которые НЕ ловятся `except` в оригинале и потому должны долететь до вызывающего.
#[derive(Debug, PartialEq, Clone, Copy)]
pub enum Stop {
    Recursion,
    Overflow,
}

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
    py_strip(&STRIP_ALL_MARKERS_RE.replace_all(text, " ")).to_string()
}

/// Python bool(x) для значения из json.loads.
fn json_truthy(v: &PyJson) -> bool {
    match v {
        PyJson::Null => false,
        PyJson::Bool(b) => *b,
        PyJson::Int { zero, .. } => !*zero,
        PyJson::Float(f) => *f != 0.0, // NaN != 0.0 -> true, как bool(nan)
        PyJson::Str(s) => !s.is_empty(),
        PyJson::List(a) => !a.is_empty(),
        PyJson::Dict(o) => !o.is_empty(),
    }
}

/// Итог Python `float(x)`: значение, либо текст исключения (ValueError/TypeError — они ловятся и
/// попадают в диагностику), либо Overflow (не ловится).
enum FloatOf {
    Value(f64),
    Failed(String),
    Overflow,
}

fn py_float_of(v: &PyJson) -> FloatOf {
    let type_error = |t: &str| {
        FloatOf::Failed(format!("float() argument must be a string or a real number, not '{t}'"))
    };
    match v {
        PyJson::Null => type_error("NoneType"),
        PyJson::Bool(b) => FloatOf::Value(if *b { 1.0 } else { 0.0 }),
        PyJson::Int { as_f64, .. } => {
            if as_f64.is_infinite() {
                FloatOf::Overflow
            } else {
                FloatOf::Value(*as_f64)
            }
        }
        PyJson::Float(f) => FloatOf::Value(*f),
        PyJson::Str(s) => match py_float(s) {
            Some(f) => FloatOf::Value(f),
            None => FloatOf::Failed(format!("could not convert string to float: {}", py_repr_str(s))),
        },
        PyJson::List(_) => type_error("list"),
        PyJson::Dict(_) => type_error("dict"),
    }
}

/// Python `max(0.0, min(1.0, x))` — НЕ f64::clamp (тот возвращает NaN как есть, а Python даёт 1.0).
fn py_clamp01(x: f64) -> f64 {
    let m = if x < 1.0 { x } else { 1.0 };
    if m > 0.0 {
        m
    } else {
        0.0
    }
}

/// Последовательность `bool(d["is_insult"]), bool(d["is_apology"]), float(d["severity"]),
/// float(d["sincerity"])` в порядке Python: первое же исключение определяет текст.
/// Ok(Err(text)) — пойманное KeyError/TypeError/ValueError; Err(Stop) — непойманное.
fn read_state_fields(d: &PyJson) -> Result<Result<(bool, bool, f64, f64), String>, Stop> {
    let key_error = |k: &str| Err::<(bool, bool, f64, f64), String>(format!("'{k}'"));
    let Some(is_insult) = d.get("is_insult") else { return Ok(key_error("is_insult")) };
    let is_insult = json_truthy(is_insult);
    let Some(is_apology) = d.get("is_apology") else { return Ok(key_error("is_apology")) };
    let is_apology = json_truthy(is_apology);
    let mut floats = [0.0f64; 2];
    for (i, k) in ["severity", "sincerity"].iter().enumerate() {
        let Some(v) = d.get(k) else { return Ok(key_error(k)) };
        match py_float_of(v) {
            FloatOf::Value(f) => floats[i] = py_clamp01(f),
            FloatOf::Failed(msg) => return Ok(Err(msg)),
            FloatOf::Overflow => return Err(Stop::Overflow),
        }
    }
    Ok(Ok((is_insult, is_apology, floats[0], floats[1])))
}

/// agent/message_intensity.py::intensity_from_state — работает на уже распарсенном JSON-объекте.
pub fn intensity_from_state_json(state: &PyJson, error: &str) -> Result<IntensityResult, Stop> {
    if !state.is_dict() {
        return Ok(IntensityResult::neutral("semantic state missing or not an object"));
    }
    match read_state_fields(state)? {
        Err(e) => Ok(IntensityResult::neutral(&format!("semantic state missing/invalid fields: {e}"))),
        Ok((is_insult, is_apology, severity, sincerity)) => Ok(IntensityResult {
            ok: true,
            is_insult,
            is_apology,
            severity,
            sincerity,
            error: error.to_string(),
            is_promise: state.get("is_promise").map(|v| v.is_true()).unwrap_or(false),
            claims_fulfilled: state.get("claims_fulfilled").map(|v| v.is_true()).unwrap_or(false),
            evidence: String::new(),
        }),
    }
}

/// agent/message_intensity.py::_parse_structured — None если форма не подходит (вызывающий код
/// падает обратно на легаси-путь через маркер).
pub fn parse_structured_json(data: &PyJson) -> Result<Option<(String, IntensityResult)>, Stop> {
    if data.get("reply").is_none() || data.get("state").is_none() {
        return Ok(None);
    }
    let (Some(PyJson::Str(reply)), Some(state)) = (data.get("reply"), data.get("state")) else {
        return Ok(None);
    };
    if !state.is_dict() {
        return Ok(None);
    }

    let result = intensity_from_state_json(state, "")?;
    if !result.ok {
        let mut r = result;
        r.error = r.error.replacen("semantic", "structured", 1);
        return Ok(Some((py_strip(reply).to_string(), r)));
    }
    if py_strip(reply).is_empty() {
        let mut r = result;
        r.error = "model produced no visible reply, tag only".to_string();
        return Ok(Some((String::new(), r)));
    }
    Ok(Some((py_strip(reply).to_string(), result)))
}

/// agent/message_intensity.py::parse_self_report (Err(Stop) = исключение, летящее мимо except).
pub fn try_parse_self_report(raw: &str) -> Result<(String, IntensityResult), Stop> {
    let stripped = py_strip(raw);
    if stripped.starts_with('{') {
        match py_json::loads(stripped) {
            Ok(data) => {
                if data.is_dict() {
                    if let Some(structured) = parse_structured_json(&data)? {
                        return Ok(structured);
                    }
                }
            }
            Err(LoadsError::Decode(_)) => {}
            Err(LoadsError::Escalate(Escalate::Recursion)) => return Err(Stop::Recursion),
        }
    }

    let matches: Vec<_> = MARKER_RE.find_iter(raw).collect();
    let Some(last) = matches.last() else {
        return Ok((py_strip(raw).to_string(), IntensityResult::neutral("no state marker found in model output")));
    };

    let visible = py_strip(&raw[..last.start()]).to_string();
    let tail = &raw[last.end()..];
    let fallback = |visible: &String| if !visible.is_empty() { visible.clone() } else { strip_all_markers(raw) };

    let Some(json_match) = JSON_OBJECT_RE.find(tail) else {
        let tail_preview: String = tail.chars().take(200).collect();
        return Ok((
            fallback(&visible),
            IntensityResult::neutral(&format!("marker present but no JSON object after it: {}", py_repr_str(&tail_preview))),
        ));
    };

    let data = match py_json::loads(json_match.as_str()) {
        Ok(d) => d,
        Err(LoadsError::Decode(e)) => {
            return Ok((fallback(&visible), IntensityResult::neutral(&format!("malformed JSON after marker: {e}"))));
        }
        Err(LoadsError::Escalate(Escalate::Recursion)) => return Err(Stop::Recursion),
    };

    let (is_insult, is_apology, severity, sincerity) = match read_state_fields(&data)? {
        Ok(v) => v,
        Err(e) => {
            return Ok((fallback(&visible), IntensityResult::neutral(&format!("state JSON missing/invalid expected fields: {e}"))));
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
        return Ok((String::new(), r));
    }

    Ok((visible, result))
}

/// Для юнит-тестов и внутренних вызовов, где «мимо except» не интересно.
pub fn parse_self_report(raw: &str) -> (String, IntensityResult) {
    try_parse_self_report(raw).unwrap_or_else(|_| (String::new(), IntensityResult::neutral("escalated")))
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

fn stop_to_pyerr(s: Stop) -> PyErr {
    match s {
        Stop::Recursion => pyo3::exceptions::PyRecursionError::new_err("maximum recursion depth exceeded while decoding a JSON document"),
        Stop::Overflow => pyo3::exceptions::PyOverflowError::new_err("int too large to convert to float"),
    }
}

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
/// приходят эти данные из распарсенного ответа модели), затем разбирается тем же точным
/// json.loads-портом. Приближение (известное): значения, которые json.dumps не умеет, дают общий
/// текст "not an object", а не точное TypeError-сообщение Python о типе.
#[pyfunction]
#[pyo3(name = "intensity_from_state", signature = (state, error=""))]
fn py_intensity_from_state<'py>(py: Python<'py>, state: &Bound<'py, PyAny>, error: &str) -> PyResult<Bound<'py, PyDict>> {
    let json_mod = py.import_bound("json")?;
    let dumped: PyResult<String> = json_mod.call_method1("dumps", (state,)).and_then(|v| v.extract());
    let result = match dumped {
        Ok(s) => match py_json::loads(&s) {
            Ok(v) => intensity_from_state_json(&v, error).map_err(stop_to_pyerr)?,
            Err(LoadsError::Decode(_)) => IntensityResult::neutral("semantic state missing or not an object"),
            Err(LoadsError::Escalate(_)) => return Err(stop_to_pyerr(Stop::Recursion)),
        },
        Err(_) => IntensityResult::neutral("semantic state missing or not an object"),
    };
    result_to_dict(py, &result)
}

#[pyfunction]
#[pyo3(name = "parse_self_report")]
fn py_parse_self_report<'py>(py: Python<'py>, raw: &str) -> PyResult<(String, Bound<'py, PyDict>)> {
    let (visible, result) = try_parse_self_report(raw).map_err(stop_to_pyerr)?;
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

    fn state(s: &str) -> PyJson {
        py_json::loads(s).unwrap()
    }

    #[test]
    fn json_truthy_matches_python_bool() {
        assert!(!json_truthy(&state("null")));
        assert!(!json_truthy(&state("0")));
        assert!(!json_truthy(&state("-0")));
        assert!(!json_truthy(&state("0.0")));
        assert!(json_truthy(&state("0.5")));
        assert!(json_truthy(&state("NaN")));
        assert!(!json_truthy(&state("\"\"")));
        assert!(json_truthy(&state("\"x\"")));
        assert!(!json_truthy(&state("[]")));
        assert!(json_truthy(&state("[1]")));
        assert!(!json_truthy(&state("{}")));
    }

    #[test]
    fn nan_severity_clamps_to_one_like_python() {
        // Python: max(0.0, min(1.0, nan)) == 1.0 (f64::clamp дал бы NaN)
        let raw = "Ответ.\n#YANDI_STATE: {\"is_insult\": false, \"is_apology\": false, \"severity\": NaN, \"sincerity\": 0.5}";
        let (_, r) = parse_self_report(raw);
        assert!(r.ok);
        assert_eq!(r.severity, 1.0);
    }

    #[test]
    fn infinity_and_huge_exponent_accepted_like_python() {
        let raw = "Ответ.\n#YANDI_STATE: {\"is_insult\": false, \"is_apology\": false, \"severity\": 1e999, \"sincerity\": -Infinity}";
        let (_, r) = parse_self_report(raw);
        assert!(r.ok);
        assert_eq!((r.severity, r.sincerity), (1.0, 0.0));
    }

    #[test]
    fn float_failure_texts_are_pythons() {
        let raw = |v: &str| format!("О.\n#YANDI_STATE: {{\"is_insult\": false, \"is_apology\": false, \"severity\": {v}, \"sincerity\": 0.5}}");
        assert_eq!(parse_self_report(&raw("\"abc\"")).1.error, "state JSON missing/invalid expected fields: could not convert string to float: 'abc'");
        assert_eq!(parse_self_report(&raw("[1]")).1.error, "state JSON missing/invalid expected fields: float() argument must be a string or a real number, not 'list'");
        assert_eq!(parse_self_report(&raw("null")).1.error, "state JSON missing/invalid expected fields: float() argument must be a string or a real number, not 'NoneType'");
        assert_eq!(parse_self_report("О.\n#YANDI_STATE: {\"is_insult\": false}").1.error, "state JSON missing/invalid expected fields: 'is_apology'");
    }

    #[test]
    fn no_json_tail_uses_python_repr() {
        let (_, r) = parse_self_report("Ответ.\n#YANDI_STATE: severity\nhigh\t'x'");
        assert_eq!(r.error, "marker present but no JSON object after it: \": severity\\nhigh\\t'x'\"");
    }

    #[test]
    fn deep_nesting_is_recursion_stop_not_crash() {
        let raw = format!("{{\"reply\": {}", "[".repeat(100_000));
        assert_eq!(try_parse_self_report(&raw).unwrap_err(), Stop::Recursion);
    }
}
