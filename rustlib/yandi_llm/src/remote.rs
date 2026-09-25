//! llm_gateway/remote_backend.py — подключение ЛЮБОЙ внешней модели по сети (свой Клод, OpenAI, self-hosted vLLM/llama.cpp server/LM Studio):
//! два реальных протокола — OpenAI-совместимый chat completions и родной Anthropic Messages API, а также OpenAI-совместимые embeddings.
//! Одна нода — один выбор владельца (config/secure_store), ключ никогда не хранится в конфиге: только ИМЯ переменной окружения.
//!
//! Построение запроса и разбор ответа — чистые функции (тестируются без сети); отправка — через `Transport`.
//! Тексты ошибок: `"{model} @ {base_url}: {деталь}"`; для статусов HTTP деталь — как у `requests` (`"500 Server Error: … for url: …"`);
//! для ошибок соединения деталь — своя (текст Python-исключения воспроизводить не имеет смысла).

use serde_json::{json, Map, Value};

use crate::pyfmt::py_truthy;
use crate::transport::{HttpRequest, HttpResponse, Transport};
use yandi_rs::py_text::py_repr_str;

pub const DEFAULT_TIMEOUT: u64 = 180;

#[derive(Debug, Clone, PartialEq)]
pub struct RemoteBackendError(pub String);

impl std::fmt::Display for RemoteBackendError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl std::error::Error for RemoteBackendError {}

/// Параметры генерации (общие для обоих протоколов).
#[derive(Debug, Clone, Default)]
pub struct GenerateParams {
    pub temperature: Option<f64>,
    pub max_tokens: Option<i64>,
    /// `"json"` → строгий JSON-режим (только OpenAI-протокол)
    pub response_format: Option<String>,
    pub stop: Option<Vec<String>>,
    pub timeout: u64,
}

fn trim_slashes(base: &str) -> &str {
    base.trim_end_matches('/')
}

fn err(model: &str, base_url: &str, detail: &str) -> RemoteBackendError {
    RemoteBackendError(format!("{model} @ {base_url}: {detail}"))
}

/// `raise_for_status()` как у `requests`: "{code} Client Error: {reason} for url: {url}" / "Server Error".
pub(crate) fn status_error(status: u16, reason: &str, url: &str) -> Option<String> {
    if (400..500).contains(&status) {
        Some(format!("{status} Client Error: {reason} for url: {url}"))
    } else if (500..600).contains(&status) {
        Some(format!("{status} Server Error: {reason} for url: {url}"))
    } else {
        None
    }
}

fn send(t: &dyn Transport, req: &HttpRequest, timeout: u64, model: &str, base_url: &str) -> Result<HttpResponse, RemoteBackendError> {
    let resp = t.post_json(req, timeout).map_err(|e| err(model, base_url, &e.0))?;
    if let Some(d) = status_error(resp.status, &resp.reason, &req.url) {
        return Err(err(model, base_url, &d));
    }
    Ok(resp)
}

/// `resp.json()`: текст ошибки — как у Python (`Expecting value: line 1 column 1 (char 0)`), значения — `serde_json`.
pub(crate) fn parse_json(body: &[u8]) -> Result<Value, String> {
    crate::semantic::loads(&String::from_utf8_lossy(body))
}

// ---------------------------------------------------------------- OpenAI chat completions

pub fn openai_generate_request(messages: &[Value], base_url: &str, api_key: Option<&str>, model: &str, p: &GenerateParams) -> HttpRequest {
    let mut payload = Map::new();
    payload.insert("model".into(), json!(model));
    payload.insert("messages".into(), Value::Array(messages.to_vec()));
    if let Some(t) = p.temperature {
        payload.insert("temperature".into(), json!(t));
    }
    if let Some(m) = p.max_tokens {
        payload.insert("max_tokens".into(), json!(m));
    }
    if p.response_format.as_deref() == Some("json") {
        payload.insert("response_format".into(), json!({"type": "json_object"}));
    }
    if let Some(s) = &p.stop {
        if !s.is_empty() {
            payload.insert("stop".into(), json!(s));
        }
    }
    let headers = match api_key {
        Some(k) if !k.is_empty() => vec![("Authorization".to_string(), format!("Bearer {k}"))],
        _ => vec![],
    };
    HttpRequest { url: format!("{}/chat/completions", trim_slashes(base_url)), headers, body: Value::Object(payload) }
}

pub fn openai_generate_parse(body: &[u8], model: &str, base_url: &str) -> Result<(String, Map<String, Value>), RemoteBackendError> {
    let bad = |d: String| err(model, base_url, &format!("неожиданный формат ответа: {d}"));
    let raw = parse_json(body).map_err(bad)?;
    let choice = raw.get("choices").and_then(|c| c.get(0)).ok_or_else(|| bad("нет choices[0]".into()))?;
    let content = choice.get("message").and_then(|m| m.get("content")).ok_or_else(|| bad("нет message.content".into()))?;
    let text = match content {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        other if !py_truthy(other) => String::new(),
        _ => return Err(bad("content не строка".into())),
    };
    let finish = choice.get("finish_reason");
    let usage = raw.get("usage");
    let mut meta = Map::new();
    meta.insert("done_reason".into(), json!(if finish == Some(&json!("length")) { "length" } else { "stop" }));
    let eval = match usage {
        Some(u) if py_truthy(u) => u.get("completion_tokens").cloned().unwrap_or(Value::Null),
        _ => Value::Null,
    };
    meta.insert("eval_count".into(), eval);
    Ok((text, meta))
}

// ---------------------------------------------------------------- Anthropic Messages

pub fn anthropic_generate_request(messages: &[Value], base_url: &str, api_key: Option<&str>, model: &str, p: &GenerateParams) -> HttpRequest {
    // Anthropic НЕ принимает role="system" внутри messages — только отдельным полем; все system-сообщения склеиваются в один текст
    let mut system_parts: Vec<&str> = Vec::new();
    let mut convo: Vec<Value> = Vec::new();
    for m in messages {
        let role = m.get("role");
        let is_system = role == Some(&json!("system"));
        if is_system {
            if let Some(Value::String(c)) = m.get("content") {
                if !c.is_empty() {
                    system_parts.push(c.as_str());
                }
            }
        } else {
            convo.push(m.clone());
        }
    }
    let mut payload = Map::new();
    payload.insert("model".into(), json!(model));
    payload.insert("messages".into(), Value::Array(convo));
    payload.insert("max_tokens".into(), json!(match p.max_tokens {
        Some(m) if m != 0 => m,
        _ => 4096,
    }));
    if !system_parts.is_empty() {
        payload.insert("system".into(), json!(system_parts.join("\n\n")));
    }
    if let Some(t) = p.temperature {
        payload.insert("temperature".into(), json!(t));
    }
    if let Some(s) = &p.stop {
        if !s.is_empty() {
            payload.insert("stop_sequences".into(), json!(s));
        }
    }
    let headers = vec![
        ("x-api-key".to_string(), api_key.unwrap_or("").to_string()),
        ("anthropic-version".to_string(), "2023-06-01".to_string()),
        ("content-type".to_string(), "application/json".to_string()),
    ];
    HttpRequest { url: format!("{}/v1/messages", trim_slashes(base_url)), headers, body: Value::Object(payload) }
}

pub fn anthropic_generate_parse(body: &[u8], model: &str, base_url: &str) -> Result<(String, Map<String, Value>), RemoteBackendError> {
    let bad = |d: String| err(model, base_url, &format!("неожиданный формат ответа: {d}"));
    let raw = parse_json(body).map_err(bad)?;
    let obj = raw.as_object().ok_or_else(|| bad("корень не объект".into()))?;
    let mut text = String::new();
    match obj.get("content") {
        None => {}
        Some(Value::Array(blocks)) => {
            for b in blocks {
                let bo = b.as_object().ok_or_else(|| bad("блок не объект".into()))?;
                if bo.get("type") == Some(&json!("text")) {
                    match bo.get("text") {
                        None => {}
                        Some(Value::String(s)) => text.push_str(s),
                        Some(_) => return Err(bad("text не строка".into())),
                    }
                }
            }
        }
        Some(_) => return Err(bad("content не список".into())),
    }
    let stop_reason = obj.get("stop_reason");
    let usage = obj.get("usage");
    let mut meta = Map::new();
    meta.insert("done_reason".into(), json!(if stop_reason == Some(&json!("max_tokens")) { "length" } else { "stop" }));
    let out = match usage {
        Some(u) if py_truthy(u) => u.get("output_tokens").cloned().unwrap_or(Value::Null),
        _ => Value::Null,
    };
    meta.insert("eval_count".into(), out);
    Ok((text, meta))
}

// ---------------------------------------------------------------- OpenAI embeddings

pub fn openai_embed_request(texts: &[String], base_url: &str, api_key: Option<&str>, model: &str) -> HttpRequest {
    let headers = match api_key {
        Some(k) if !k.is_empty() => vec![("Authorization".to_string(), format!("Bearer {k}"))],
        _ => vec![],
    };
    HttpRequest {
        url: format!("{}/embeddings", trim_slashes(base_url)),
        headers,
        body: json!({"model": model, "input": texts}),
    }
}

enum Key {
    Num(f64),
    Str(String),
    Other,
}

/// Порядок записей в ответе спецификацией НЕ гарантирован — сортируем по `index` (устойчиво), а не по порядку массива.
pub fn openai_embed_parse(body: &[u8], model: &str, base_url: &str) -> Result<(Vec<Value>, Map<String, Value>), RemoteBackendError> {
    let bad = |d: String| err(model, base_url, &format!("неожиданный формат embedding-ответа: {d}"));
    let raw = parse_json(body).map_err(bad)?;
    let data = raw.get("data").and_then(|d| d.as_array()).ok_or_else(|| bad("нет data".into()))?;
    // `sorted(..., key=lambda item: item.get("index", 0))`: ключи сравниваются только когда записей >= 2 — тогда все числа (bool — как 0/1) ИЛИ все строки,
    // иначе Python бросает TypeError (пойман → ошибка формата). Один элемент сравнивать не с чем — любой ключ годится. Не-словарь → AttributeError (не пойман).
    let mut keyed: Vec<(Key, &Value)> = Vec::new();
    for item in data {
        let obj = item.as_object().ok_or_else(|| bad("запись не объект".into()))?;
        let k = match obj.get("index") {
            None => Key::Num(0.0),
            Some(Value::Bool(b)) => Key::Num(if *b { 1.0 } else { 0.0 }),
            Some(Value::Number(n)) => Key::Num(n.as_f64().unwrap_or(f64::NAN)),
            Some(Value::String(t)) => Key::Str(t.clone()),
            Some(_) => Key::Other,
        };
        keyed.push((k, item));
    }
    if keyed.len() >= 2 {
        let all_num = keyed.iter().all(|(k, _)| matches!(k, Key::Num(_)));
        let all_str = keyed.iter().all(|(k, _)| matches!(k, Key::Str(_)));
        if !(all_num || all_str) {
            return Err(bad("index несопоставим".into()));
        }
        keyed.sort_by(|a, b| match (&a.0, &b.0) {
            (Key::Num(x), Key::Num(y)) => x.partial_cmp(y).unwrap_or(std::cmp::Ordering::Equal),
            (Key::Str(x), Key::Str(y)) => x.chars().map(|c| c as u32).cmp(y.chars().map(|c| c as u32)),
            _ => std::cmp::Ordering::Equal,
        });
    }
    let mut vectors: Vec<Value> = Vec::new();
    for (_, item) in keyed {
        vectors.push(item.get("embedding").cloned().ok_or_else(|| bad("нет embedding".into()))?);
    }
    let dimension = match vectors.first() {
        Some(Value::Array(a)) => a.len(),
        Some(Value::String(s)) => s.chars().count(),
        Some(_) => return Err(bad("embedding без длины".into())),
        None => 0,
    };
    let mut meta = Map::new();
    meta.insert("dimension".into(), json!(dimension));
    meta.insert("normalized".into(), json!(false));
    Ok((vectors, meta))
}

// ---------------------------------------------------------------- точки входа

fn api_key_from_env(api_key_env: Option<&str>) -> Option<String> {
    api_key_env.and_then(|name| std::env::var(name).ok())
}

/// `generate`: единая точка входа для обоих протоколов; `messages` — уже готовый wire-формат (`build_messages`).
pub fn generate(
    t: &dyn Transport, messages: &[Value], base_url: &str, protocol: &str, model: &str, api_key_env: Option<&str>, p: &GenerateParams,
) -> Result<(String, Map<String, Value>), RemoteBackendError> {
    let api_key = api_key_from_env(api_key_env);
    let timeout = if p.timeout == 0 { DEFAULT_TIMEOUT } else { p.timeout };
    match protocol {
        "openai" => {
            let req = openai_generate_request(messages, base_url, api_key.as_deref(), model, p);
            let resp = send(t, &req, timeout, model, base_url)?;
            openai_generate_parse(&resp.body, model, base_url)
        }
        "anthropic" => {
            let req = anthropic_generate_request(messages, base_url, api_key.as_deref(), model, p);
            let resp = send(t, &req, timeout, model, base_url)?;
            anthropic_generate_parse(&resp.body, model, base_url)
        }
        other => Err(RemoteBackendError(format!("неизвестный протокол {} (ожидался 'openai' или 'anthropic')", py_repr_str(other)))),
    }
}

/// `embed`: пока только OpenAI-совместимый протокол (у Anthropic нативных embeddings нет — не притворяемся).
pub fn embed(
    t: &dyn Transport, texts: &[String], base_url: &str, protocol: &str, model: &str, api_key_env: Option<&str>, timeout: u64,
) -> Result<(Vec<Value>, Map<String, Value>), RemoteBackendError> {
    let api_key = api_key_from_env(api_key_env);
    let timeout = if timeout == 0 { DEFAULT_TIMEOUT } else { timeout };
    match protocol {
        "openai" => {
            let req = openai_embed_request(texts, base_url, api_key.as_deref(), model);
            let resp = send(t, &req, timeout, model, base_url)?;
            openai_embed_parse(&resp.body, model, base_url)
        }
        "anthropic" => Err(RemoteBackendError(
            "Anthropic API не предоставляет embeddings — настрой отдельный embedding-провайдер (OpenAI-совместимый self-hosted сервер или другой явный remote-backend), генерация и эмбеддинги — разные способности узла, не обязаны совпадать".to_string(),
        )),
        other => Err(RemoteBackendError(format!("неизвестный протокол {} для embeddings (ожидался 'openai')", py_repr_str(other)))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn openai_payload_order() {
        let p = GenerateParams { temperature: Some(0.2), max_tokens: Some(50), response_format: Some("json".into()), stop: Some(vec!["x".into()]), timeout: 5 };
        let r = openai_generate_request(&[json!({"role": "user", "content": "hi"})], "http://h:1///", Some("k"), "m", &p);
        assert_eq!(r.url, "http://h:1/chat/completions");
        assert_eq!(serde_json::to_string(&r.body).unwrap(), r#"{"model":"m","messages":[{"role":"user","content":"hi"}],"temperature":0.2,"max_tokens":50,"response_format":{"type":"json_object"},"stop":["x"]}"#);
        assert_eq!(r.headers, vec![("Authorization".to_string(), "Bearer k".to_string())]);
    }

    #[test]
    fn anthropic_moves_system() {
        let m = [json!({"role": "system", "content": "a"}), json!({"role": "user", "content": "q"}), json!({"role": "system", "content": "b"})];
        let r = anthropic_generate_request(&m, "http://h", None, "m", &GenerateParams::default());
        assert_eq!(r.body["system"], "a\n\nb");
        assert_eq!(r.body["messages"].as_array().unwrap().len(), 1);
        assert_eq!(r.body["max_tokens"], 4096);
    }

    #[test]
    fn embed_sorted_by_index() {
        let body = br#"{"data":[{"index":1,"embedding":[3.0]},{"index":0,"embedding":[1.0,2.0]}]}"#;
        let (v, meta) = openai_embed_parse(body, "m", "u").unwrap();
        assert_eq!(v[0], json!([1.0, 2.0]));
        assert_eq!(meta["dimension"], 2);
    }
}
