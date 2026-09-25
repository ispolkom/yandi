//! Единая JSON-граница шлюза: «имя + аргументы → результат» для `complete` / `complete_with_meta` / `complete_semantic` / `embed`.
//! Ею пользуются и мост дифференциальных тестов (Python), и узел (локальный HTTP для ядра на Python): форма аргументов и ответов одна и та же.
//! Ответ: `{"ok": …}` либо `{"error": {"class": "LLMError"|"EmbedError", "msg": …}}`; `Err(_)` — неверные аргументы самого вызова.
use serde_json::{json, Value};

use crate::client::{self, CompleteParams, Gateway};
use crate::messages::SystemArg;
use crate::types::SemanticOutputRequirement;

pub const GATEWAY_CALLS: [&str; 4] = ["gw_complete", "gw_complete_meta", "gw_complete_semantic", "gw_embed"];

pub(crate) fn complete_params(a: &dyn Fn(&str) -> Value) -> CompleteParams {
    CompleteParams {
        temperature: a("temperature").as_f64(),
        max_tokens: a("max_tokens").as_i64(),
        timeout: a("timeout").as_u64().unwrap_or(client::DEFAULT_TIMEOUT),
        strip_think: a("strip_think").as_bool().unwrap_or(true),
        extra_options: match a("extra_options") {
            Value::Object(o) => Some(o),
            _ => None,
        },
        response_format: match a("response_format") {
            Value::Null => None,
            v => Some(v),
        },
        stop: match a("stop") {
            Value::Array(x) => Some(x.iter().filter_map(|v| v.as_str().map(String::from)).collect()),
            _ => None,
        },
    }
}

pub(crate) fn system_arg(v: &Value) -> SystemArg {
    match v {
        Value::String(s) => SystemArg::One(s.clone()),
        Value::Array(a) => SystemArg::Many(a.iter().filter_map(|x| x.as_str().map(String::from)).collect()),
        _ => SystemArg::None,
    }
}

/// Выполнить один вызов шлюза. Блокирующий (сеть/движок) — из async-кода вызывать через `spawn_blocking`.
pub fn gateway_call(gw: &Gateway, name: &str, args: &Value) -> Result<Value, String> {
    let a = |k: &str| args.get(k).cloned().unwrap_or(Value::Null);
    let err = |class: &str, msg: String| json!({"error": {"class": class, "msg": msg}});
    if name == "gw_embed" {
        let texts: Vec<String> = match a("texts") {
            Value::Array(x) => x.iter().filter_map(|v| v.as_str().map(String::from)).collect(),
            Value::String(s) => vec![s],
            _ => vec![],
        };
        return Ok(match gw.embed(&texts, a("model").as_str().unwrap_or(""), a("base_url").as_str().unwrap_or(""), a("timeout").as_u64().unwrap_or(client::DEFAULT_TIMEOUT)) {
            Ok(r) => json!({"ok": {"vectors": r.vectors, "space": r.space.to_dict()}}),
            Err(e) => err("EmbedError", e.0),
        });
    }
    if !GATEWAY_CALLS.contains(&name) {
        return Err(format!("неизвестная функция {name}"));
    }
    let msgs: Option<Vec<Value>> = match a("messages") {
        Value::Array(m) => Some(m),
        _ => None,
    };
    let prompt = a("prompt");
    let model = a("model");
    let base = a("base_url");
    let sys = system_arg(&a("system"));
    let p = complete_params(&a);
    let (model, base) = (model.as_str().unwrap_or(""), base.as_str().unwrap_or(""));
    Ok(match name {
        "gw_complete" => match gw.complete_with_raw(prompt.as_str(), model, &sys, msgs.as_deref(), base, &p) {
            Ok((text, raw)) => json!({"ok": {"text": text, "raw": raw}}),
            Err(e) => err("LLMError", e.0),
        },
        "gw_complete_meta" => match gw.complete_with_meta(prompt.as_str(), model, &sys, msgs.as_deref(), base, &p) {
            Ok((text, truncated, count)) => json!({"ok": {"text": text, "truncated": truncated, "token_count": count}}),
            Err(e) => err("LLMError", e.0),
        },
        _ => {
            let req: SemanticOutputRequirement = serde_json::from_value(a("requirement")).map_err(|e| e.to_string())?;
            match gw.complete_semantic(prompt.as_str(), model, &req, &sys, msgs.as_deref(), base, &p) {
                Ok(r) => json!({"ok": serde_json::to_value(r).map_err(|e| e.to_string())?}),
                Err(e) => err("LLMError", e.0),
            }
        }
    })
}
