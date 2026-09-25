//! Родной «мост интеллекта» узла: то, что делал `llm_gateway/intelligence_bridge.py` (HTTP на 127.0.0.1:18083), но внутри процесса узла.
//! Межнодовые запросы (и локальные от PET) идут ВСЕГДА на один логический алиас `yandi:peer-default`, настроенный владельцем этого узла;
//! ни `model`, ни `base_url`, ни любое поле выбора backend'а из тела запроса не читается. Пиру никогда не уходят сырые детали ошибки backend'а
//! (только `backend_error`); полный текст — в локальный лог. Семантика проверки входа и форма ответа — как у Python-моста (порядок ключей тоже).
use serde_json::{json, Map, Value};

use crate::client::{CompleteParams, Gateway};
use crate::messages::SystemArg;
use crate::pyfmt::py_repr;

pub const PEER_DEFAULT_MODEL: &str = "yandi:peer-default";
pub const MAX_BODY_BYTES: usize = 64 * 1024;
pub const MAX_MESSAGES: usize = 64;
pub const MAX_MESSAGE_CHARS: usize = 32_768;

struct BridgeError(String);

fn validate_messages(raw: Option<&Value>) -> Result<Vec<Value>, BridgeError> {
    let list = match raw {
        Some(Value::Array(a)) if !a.is_empty() => a,
        _ => return Err(BridgeError("messages must be a non-empty list".into())),
    };
    if list.len() > MAX_MESSAGES {
        return Err(BridgeError("too many messages".into()));
    }
    let mut out = Vec::with_capacity(list.len());
    for m in list {
        let obj = m.as_object().ok_or_else(|| BridgeError("each message must be an object".into()))?;
        let role = obj.get("role").cloned().unwrap_or(Value::Null);
        let content = obj.get("content").cloned().unwrap_or(Value::Null);
        if !matches!(&role, Value::String(r) if r == "system" || r == "user" || r == "assistant") {
            return Err(BridgeError(format!("invalid role: {}", py_repr(&role))));
        }
        let text = match &content {
            Value::String(s) if !s.is_empty() => s,
            _ => return Err(BridgeError("message content must be a non-empty string".into())),
        };
        if text.chars().count() > MAX_MESSAGE_CHARS {
            return Err(BridgeError("message content too large".into()));
        }
        let mut o = Map::new();
        o.insert("role".into(), role);
        o.insert("content".into(), content);
        out.push(Value::Object(o));
    }
    Ok(out)
}

/// Один запрос: `(HTTP-статус, тело ответа)`. `body` — уже разобранное JSON-тело; `log` получает полный текст ошибки backend'а (только для локального лога).
pub fn handle_infer(gw: &Gateway, body: &Value, log: &dyn Fn(&str)) -> (u16, Value) {
    let Some(body) = body.as_object() else {
        return (400, json!({"success": false, "error": "malformed_request"}));
    };
    match infer(gw, body, log) {
        Ok(r) => {
            let ok = r["success"] == json!(true);
            (if ok { 200 } else { 502 }, r)
        }
        Err(BridgeError(e)) => (400, json!({"success": false, "error": e})),
    }
}

fn infer(gw: &Gateway, body: &Map<String, Value>, log: &dyn Fn(&str)) -> Result<Value, BridgeError> {
    let request_id = body.get("request_id").cloned().unwrap_or(Value::Null);
    let messages = validate_messages(body.get("messages"))?;

    // `isinstance(x, int)` у Python верно и для bool; float (даже 5.0) — нет
    let max_tokens = match body.get("max_tokens") {
        None | Some(Value::Null) => None,
        Some(Value::Bool(b)) => Some(*b as i64),
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => Some(n.as_i64().unwrap_or(i64::MAX)),
        Some(_) => return Err(BridgeError("max_tokens must be a positive integer".into())),
    };
    if matches!(max_tokens, Some(m) if m <= 0) {
        return Err(BridgeError("max_tokens must be a positive integer".into()));
    }
    let temperature = match body.get("temperature") {
        None | Some(Value::Null) => None,
        Some(Value::Bool(b)) => Some(*b as i64 as f64),
        Some(Value::Number(n)) => n.as_f64(),
        Some(_) => return Err(BridgeError("temperature must be a number".into())),
    };
    let stop = match body.get("stop") {
        None | Some(Value::Null) => None,
        Some(Value::Array(a)) if a.iter().all(|s| s.is_string()) => Some(a.iter().map(|s| s.as_str().unwrap().to_string()).collect::<Vec<_>>()),
        Some(_) => return Err(BridgeError("stop must be a list of strings".into())),
    };
    let p = CompleteParams { temperature, max_tokens, stop, ..CompleteParams::new() };
    match gw.complete_with_meta(None, PEER_DEFAULT_MODEL, &SystemArg::None, Some(&messages), &gw.opts.default_base_url, &p) {
        Ok((text, truncated, count)) => Ok(json!({"request_id": request_id, "success": true, "text": text, "truncated": truncated, "tokens_used": count})),
        Err(e) => {
            log(&format!("[intelligence_bridge] backend error: {}", e.0));
            Ok(json!({"request_id": request_id, "success": false, "error": "backend_error"}))
        }
    }
}
