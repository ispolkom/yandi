//! `client.py`: семантический результат «реплика + внутреннее состояние» — выбор контракта вывода по возможностям бэкенда, инструкции для модели,
//! разбор и проверка ответа. Это ядро правила «пользователь видит только реплику; состояние — внутреннее и применяется вызывающим только после проверки».
//!
//! Разбор JSON: валидность и ТЕКСТЫ ошибок — точные тексты `json.loads` Python (`yandi_rs::py_json`); значения — `serde_json` (числа: целые вне i64 →
//! float; NaN/Infinity, приемлемые для Python, `serde_json` не понимает — тогда значения строятся из разбора `py_json` с потерей точности целых).

use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};

use crate::messages::{append_system_instruction, SystemArg};
use crate::pyfmt::{py_repr, py_str, py_truthy};
use crate::types::{BackendCapabilities, LlmError, OutputContract, SemanticCompletionResult, SemanticOutputRequirement};
use yandi_rs::py_json::{self, LoadsError, PyJson};
use yandi_rs::py_text::{py_repr_str, py_strip};

pub const SEMANTIC_STATE_MARKER: &str = "###YANDI_STATE###";

const REPLY_STATE_INSTRUCTION: &str = "Сформируй один результат из двух частей: обычная человеческая реплика пользователю и внутреннее состояние. Пользователь видит только реплику. Внутреннее состояние никогда не является текстом ответа.";

fn json_instruction() -> String {
    format!(
        "{REPLY_STATE_INSTRUCTION} Верни строго один JSON-объект с полями \"reply\" (непустая строка) и \"state\" (объект или null). В поле state, если оно есть, используй только ожидаемые внутренние поля состояния."
    )
}

fn legacy_instruction() -> String {
    format!(
        "{REPLY_STATE_INSTRUCTION} Сначала напиши только видимую пользователю реплику. Затем на новой строке добавь {SEMANTIC_STATE_MARKER} и JSON-объект внутреннего состояния. Маркер и JSON не являются частью реплики пользователя."
    )
}

static THINK_BLOCK: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?s)<think>.*?</think>").unwrap());
static FIRST_TO_LAST_BRACE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?s)\{.*\}").unwrap());

/// `_strip_think_blocks`
pub fn strip_think_blocks(text: &str) -> String {
    THINK_BLOCK.replace_all(text, "").replace("<think>", "").replace("</think>", "")
}

/// `_contract_from_response_format`
pub fn contract_from_response_format(response_format: Option<&Value>) -> OutputContract {
    let is_json = matches!(response_format, Some(Value::String(s)) if s == "json");
    OutputContract { name: if is_json { "json_object" } else { "plain_text" }.to_string(), response_format: response_format.cloned() }
}

/// `_state_schema_prompt_hint`: имена ожидаемых полей состояния СЛОВАМИ, из схемы вызывающего (без PET-специфичных слов в шлюзе).
pub fn state_schema_prompt_hint(state_schema: Option<&Value>) -> String {
    let props = match state_schema {
        Some(Value::Object(o)) => match o.get("properties") {
            Some(Value::Object(p)) if !p.is_empty() => p,
            _ => return String::new(),
        },
        _ => return String::new(),
    };
    // `set(state_schema.get("required") or [])`: список → его строки; СТРОКА → множество её символов (странность оригинала); словарь → ключи;
    // ложное → пусто. Прочие истинные скаляры в Python бросают TypeError — родной код молча считает их пустыми (расхождение на невалидной схеме).
    let required_owned: Vec<String> = match state_schema {
        Some(Value::Object(o)) => match o.get("required") {
            Some(Value::Array(a)) => a.iter().filter_map(|v| v.as_str().map(String::from)).collect(),
            Some(Value::String(s)) => s.chars().map(|c| c.to_string()).collect(),
            Some(Value::Object(m)) => m.keys().cloned().collect(),
            _ => vec![],
        },
        _ => vec![],
    };
    let required: Vec<&str> = required_owned.iter().map(|s| s.as_str()).collect();
    let mut parts: Vec<String> = Vec::new();
    for (name, spec) in props.iter() {
        let empty = Map::new();
        let spec = match spec {
            Value::Object(o) => o,
            _ => &empty,
        };
        let mut details: Vec<String> = vec![match spec.get("type") {
            Some(t) => py_str(t),
            None => "any".to_string(),
        }];
        if let (Some(mn), Some(mx)) = (spec.get("minimum"), spec.get("maximum")) {
            details.push(format!("от {} до {}", py_str(mn), py_str(mx)));
        }
        if required.contains(&name.as_str()) {
            details.push("обязательно".to_string());
        }
        if let Some(d) = spec.get("description") {
            if py_truthy(d) {
                details.push(py_str(d));
            }
        }
        parts.push(format!("\"{}\" ({})", name, details.join(", ")));
    }
    format!(
        " Объект state должен содержать ровно эти поля: {}. Других полей не добавляй. Значения этих полей не могут быть null: если поле неприменимо, используй 0 для числа и false для boolean. Всегда заполняй state, когда можешь оценить ситуацию; null допустим только для самого state, и только если оценить её невозможно.",
        parts.join("; ")
    )
}

/// `_semantic_result_schema`
pub fn semantic_result_schema(req: &SemanticOutputRequirement) -> Value {
    let state_schema = match &req.state_schema {
        Some(v) if py_truthy(v) => v.clone(),
        _ => json!({"type": "object"}),
    };
    let mut required = vec![json!("reply")];
    if req.state_required {
        required.push(json!("state"));
    }
    json!({
        "type": "object",
        "properties": {"reply": {"type": "string", "minLength": 1}, "state": state_schema},
        "required": required,
    })
}

/// `_semantic_contract_from_target`: (контракт вывода, инструкция) по возможностям бэкенда.
pub fn semantic_contract_from_target(req: &SemanticOutputRequirement, caps: &BackendCapabilities) -> Result<(OutputContract, String), LlmError> {
    if req.kind != "reply_state" {
        return Err(LlmError(format!("unsupported semantic output kind {}", py_repr_str(&req.kind))));
    }
    if caps.json_schema {
        return Ok((
            OutputContract { name: "semantic_json_schema".to_string(), response_format: Some(semantic_result_schema(req)) },
            json_instruction(),
        ));
    }
    let hint = state_schema_prompt_hint(req.state_schema.as_ref());
    if caps.json_object {
        return Ok((
            OutputContract { name: "semantic_json_object".to_string(), response_format: Some(json!("json")) },
            json_instruction() + &hint,
        ));
    }
    Ok((OutputContract { name: "semantic_legacy_marker".to_string(), response_format: None }, legacy_instruction() + &hint))
}

/// Разбор JSON как `json.loads`: Ok(значение) либо Err(точный текст ошибки Python).
fn loads(text: &str) -> Result<Value, String> {
    match py_json::loads(text) {
        Ok(pj) => Ok(serde_json::from_str::<Value>(text).unwrap_or_else(|_| pyjson_to_value(&pj))),
        Err(LoadsError::Decode(m)) => Err(m),
        Err(LoadsError::Escalate(_)) => Err("recursion".to_string()),
    }
}

fn pyjson_to_value(p: &PyJson) -> Value {
    match p {
        PyJson::Null => Value::Null,
        PyJson::Bool(b) => Value::Bool(*b),
        PyJson::Int { zero, as_f64 } => {
            if *zero {
                json!(0)
            } else if as_f64.fract() == 0.0 && as_f64.abs() < 9e15 {
                json!(*as_f64 as i64)
            } else {
                serde_json::Number::from_f64(*as_f64).map(Value::Number).unwrap_or(Value::Null)
            }
        }
        PyJson::Float(f) => serde_json::Number::from_f64(*f).map(Value::Number).unwrap_or(Value::Null),
        PyJson::Str(s) => Value::String(s.clone()),
        PyJson::List(l) => Value::Array(l.iter().map(pyjson_to_value).collect()),
        PyJson::Dict(items) => {
            let mut m = Map::new();
            for (k, v) in items {
                m.insert(k.clone(), pyjson_to_value(v));
            }
            Value::Object(m)
        }
    }
}

/// `_looks_like_internal_state_fragment`: текст похож на «голое» внутреннее состояние (его нельзя показывать как реплику).
pub fn looks_like_internal_state_fragment(text: &str) -> bool {
    let stripped = py_strip(text);
    if stripped.is_empty() {
        return false;
    }
    let state_keys = ["is_insult", "severity", "is_apology", "sincerity"];
    if state_keys.iter().filter(|k| stripped.contains(*k)).count() >= 2 {
        return true;
    }
    if stripped.starts_with('{') {
        return match py_json::loads(stripped) {
            Ok(PyJson::Dict(items)) => state_keys.iter().filter(|k| items.iter().any(|(key, _)| key == *k)).count() >= 2,
            _ => false,
        };
    }
    false
}

/// `_state_valid_for_requirement`
pub fn state_valid_for_requirement(state: Option<&Value>, req: &SemanticOutputRequirement) -> bool {
    let state = match state {
        None | Some(Value::Null) => return !req.state_required,
        Some(s) => s,
    };
    let obj = match state {
        Value::Object(o) => o,
        _ => return false,
    };
    if let Some(Value::Object(schema)) = &req.state_schema {
        if let Some(Value::Array(required)) = schema.get("required") {
            return required.iter().all(|k| matches!(k, Value::String(s) if obj.contains_key(s)));
        }
    }
    true
}

/// (reply, state, reply_ok, state_ok, parse_ok, error)
pub type Normalized = (String, Option<Value>, bool, bool, bool, Option<String>);

fn fail(msg: &str) -> Normalized {
    (String::new(), None, false, false, false, Some(msg.to_string()))
}

pub fn normalize_structured_semantic(content: &str, req: &SemanticOutputRequirement) -> Normalized {
    let stripped_owned = strip_think_blocks(content);
    let stripped = py_strip(&stripped_owned);
    let data = match loads(stripped) {
        Ok(v) => v,
        Err(e) => return fail(&format!("malformed semantic JSON: {e}")),
    };
    let obj = match &data {
        Value::Object(o) => o,
        _ => return fail("semantic JSON root is not an object"),
    };
    let reply = obj.get("reply");
    let state = obj.get("state");
    let reply_ok = match reply {
        Some(Value::String(r)) => !py_strip(r).is_empty() || !req.reply_required,
        _ => false,
    };
    let state_present = matches!(state, Some(s) if !s.is_null());
    let state_ok = state_valid_for_requirement(state, req);
    if !reply_ok {
        return fail("semantic result missing visible reply");
    }
    let reply_text = match reply {
        Some(Value::String(r)) => py_strip(r).to_string(),
        _ => String::new(),
    };
    if state_present && !state_ok {
        return (reply_text, None, true, false, false, Some("semantic state malformed".to_string()));
    }
    if req.state_required && !state_present {
        return (reply_text, None, true, false, false, Some("semantic result missing required state".to_string()));
    }
    let st = match state {
        Some(Value::Object(_)) => state.cloned(),
        _ => None,
    };
    (reply_text, st, true, state_ok, true, None)
}

pub fn normalize_legacy_semantic(content: &str, req: &SemanticOutputRequirement) -> Normalized {
    let text_owned = strip_think_blocks(content);
    let text = py_strip(&text_owned);
    if text.is_empty() {
        return fail("empty semantic response");
    }
    let marker_pos = match text.rfind(SEMANTIC_STATE_MARKER) {
        None => {
            if looks_like_internal_state_fragment(text) {
                return fail("state-only response withheld from visible reply");
            }
            if req.state_required {
                return fail("legacy semantic response missing required state marker");
            }
            return (text.to_string(), None, true, true, true, None);
        }
        Some(p) => p,
    };
    let reply = py_strip(&text[..marker_pos]).to_string();
    let tail = &text[marker_pos + SEMANTIC_STATE_MARKER.len()..];
    if reply.is_empty() && req.reply_required {
        return fail("legacy semantic response missing visible reply");
    }
    let has_reply = !reply.is_empty();
    // при обязательном состоянии — parse_ok=false; иначе parse_ok зависит только от наличия реплики
    let soft = |req: &SemanticOutputRequirement, err: String, reply: String| -> Normalized {
        if req.state_required {
            (reply, None, has_reply, false, false, Some(err))
        } else {
            (reply, None, has_reply, false, has_reply, Some(err))
        }
    };
    let m = match FIRST_TO_LAST_BRACE.find(tail) {
        Some(m) => m,
        None => return soft(req, "legacy semantic marker missing JSON state".to_string(), reply),
    };
    let state = match loads(m.as_str()) {
        Ok(v) => v,
        Err(e) => return soft(req, format!("legacy semantic state malformed: {e}"), reply),
    };
    if !state_valid_for_requirement(Some(&state), req) {
        return soft(req, "legacy semantic state malformed".to_string(), reply);
    }
    let st = if state.is_object() { Some(state) } else { None };
    (reply, st, has_reply, true, has_reply, None)
}

/// `_normalize_semantic_completion`
pub fn normalize_semantic_completion(
    content: &str, req: &SemanticOutputRequirement, contract: &OutputContract, metadata: &Map<String, Value>,
) -> SemanticCompletionResult {
    let (reply, state, reply_ok, state_ok, parse_ok, error) = if contract.name == "semantic_json_schema" || contract.name == "semantic_json_object" {
        normalize_structured_semantic(content, req)
    } else {
        normalize_legacy_semantic(content, req)
    };
    let mut sm = Map::new();
    sm.insert("semantic_kind".into(), json!(req.kind));
    sm.insert("reply_required".into(), json!(req.reply_required));
    sm.insert("state_required".into(), json!(req.state_required));
    sm.insert("selected_output_contract".into(), json!(contract.name));
    sm.insert("semantic_parse_ok".into(), json!(parse_ok));
    sm.insert("reply_present".into(), json!(reply_ok));
    sm.insert("state_present".into(), json!(state.is_some()));
    sm.insert("state_valid".into(), json!(state_ok));
    if let Some(e) = &error {
        sm.insert("semantic_error".into(), json!(e));
    }
    let mut result_meta = metadata.clone();
    result_meta.insert("_llm_gateway_semantic".into(), Value::Object(sm));
    SemanticCompletionResult {
        reply,
        state: if state_ok { state } else { None },
        reply_ok,
        state_ok,
        parse_ok,
        error,
        metadata: result_meta,
    }
}

/// Инструкция и новый system для вызывающего (собирает `_semantic_contract_from_target` + `_append_system_instruction`).
pub fn semantic_system(req: &SemanticOutputRequirement, caps: &BackendCapabilities, system: &SystemArg) -> Result<(OutputContract, Value), LlmError> {
    let (contract, instruction) = semantic_contract_from_target(req, caps)?;
    Ok((contract, append_system_instruction(system, &instruction)))
}

#[allow(dead_code)]
fn _keep(_: &str) -> String {
    py_repr(&Value::Null)
}
