//! llm_gateway/ollama_backend.py — Ollama-совместимый транспорт (`/api/chat`, `stream: false`). Возвращает текст и ВЕСЬ сырой ответ (там `done_reason`, `eval_count`, …).
//! Ошибки: `"{model}: {деталь}"` (в отличие от remote — без адреса). Адрес используется как есть, БЕЗ отрезания хвостовых слэшей (как в оригинале).

use serde_json::{json, Map, Value};

use crate::remote::status_error;
use crate::transport::{HttpRequest, Transport};

#[derive(Debug, Clone, PartialEq)]
pub struct OllamaBackendError(pub String);

impl std::fmt::Display for OllamaBackendError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl std::error::Error for OllamaBackendError {}

#[derive(Debug, Clone, Default)]
pub struct OllamaParams {
    pub temperature: Option<f64>,
    pub max_tokens: Option<i64>,
    pub timeout: u64,
    pub extra_options: Option<Map<String, Value>>,
    /// любой JSON: строка `"json"` или схема (объект) — уходит в верхнеуровневое поле `format`
    pub response_format: Option<Value>,
    pub stop: Option<Vec<String>>,
}

pub fn generate_request(messages: &[Value], model: &str, base_url: &str, p: &OllamaParams) -> HttpRequest {
    let mut options: Map<String, Value> = p.extra_options.clone().unwrap_or_default();
    if let Some(t) = p.temperature {
        options.insert("temperature".into(), json!(t));
    }
    if let Some(m) = p.max_tokens {
        options.insert("num_predict".into(), json!(m));
    }
    if let Some(s) = &p.stop {
        if !s.is_empty() {
            options.insert("stop".into(), json!(s));
        }
    }
    let mut payload = Map::new();
    payload.insert("model".into(), json!(model));
    payload.insert("messages".into(), Value::Array(messages.to_vec()));
    payload.insert("stream".into(), json!(false));
    if let Some(f) = &p.response_format {
        payload.insert("format".into(), f.clone());
    }
    if !options.is_empty() {
        payload.insert("options".into(), Value::Object(options));
    }
    HttpRequest { url: format!("{base_url}/api/chat"), headers: vec![], body: Value::Object(payload) }
}

pub fn generate_parse(body: &[u8], model: &str) -> Result<(String, Value), OllamaBackendError> {
    let bad = |d: String| OllamaBackendError(format!("{model}: неожиданный формат ответа: {d}"));
    let raw: Value = serde_json::from_slice(body).map_err(|e| bad(e.to_string()))?;
    let content = raw.get("message").and_then(|m| m.get("content")).ok_or_else(|| bad("нет message.content".into()))?;
    match content {
        Value::String(s) => Ok((s.clone(), raw.clone())),
        _ => Err(bad("content не строка".into())),
    }
}

pub fn generate(t: &dyn Transport, messages: &[Value], model: &str, base_url: &str, p: &OllamaParams) -> Result<(String, Value), OllamaBackendError> {
    let req = generate_request(messages, model, base_url, p);
    let timeout = if p.timeout == 0 { crate::remote::DEFAULT_TIMEOUT } else { p.timeout };
    let resp = t.post_json(&req, timeout).map_err(|e| OllamaBackendError(format!("{model}: {}", e.0)))?;
    if let Some(d) = status_error(resp.status, &resp.reason, &req.url) {
        return Err(OllamaBackendError(format!("{model}: {d}")));
    }
    generate_parse(&resp.body, model)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn payload_shape() {
        let p = OllamaParams {
            temperature: Some(0.5), max_tokens: Some(10), timeout: 5, extra_options: Some(json!({"seed": 1}).as_object().unwrap().clone()),
            response_format: Some(json!("json")), stop: Some(vec!["x".into()]),
        };
        let r = generate_request(&[json!({"role": "user", "content": "hi"})], "m", "http://h:1", &p);
        assert_eq!(r.url, "http://h:1/api/chat");
        assert_eq!(
            serde_json::to_string(&r.body).unwrap(),
            r#"{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":false,"format":"json","options":{"seed":1,"temperature":0.5,"num_predict":10,"stop":["x"]}}"#
        );
    }
}
