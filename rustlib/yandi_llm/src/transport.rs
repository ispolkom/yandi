//! Транспорт HTTP для бэкендов шлюза: тонкая обёртка над блокирующим `reqwest`. Отдельный интерфейс (`Transport`), чтобы бэкенды тестировались
//! без сети (подмена) и чтобы позже подключить асинхронный транспорт узла, не трогая логику бэкендов.
//! Как в оригинале (`requests.Session`, `trust_env = False`): переменные окружения прокси игнорируются.

use serde_json::Value;
use std::time::Duration;

#[derive(Debug, Clone)]
pub struct HttpRequest {
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Value,
}

#[derive(Debug, Clone)]
pub struct HttpResponse {
    pub status: u16,
    pub reason: String,
    pub body: Vec<u8>,
}

/// Ошибка транспорта (соединение, таймаут, чтение). Ошибки статуса HTTP разбирает вызывающий.
#[derive(Debug, Clone)]
pub struct TransportError(pub String);

pub trait Transport {
    fn post_json(&self, req: &HttpRequest, timeout_secs: u64) -> Result<HttpResponse, TransportError>;
}

pub struct ReqwestTransport {
    client: reqwest::blocking::Client,
}

impl ReqwestTransport {
    pub fn new() -> Self {
        let client = reqwest::blocking::Client::builder().no_proxy().build().expect("HTTP-клиент");
        ReqwestTransport { client }
    }
}

impl Default for ReqwestTransport {
    fn default() -> Self {
        Self::new()
    }
}

impl Transport for ReqwestTransport {
    fn post_json(&self, req: &HttpRequest, timeout_secs: u64) -> Result<HttpResponse, TransportError> {
        let mut b = self.client.post(&req.url).timeout(Duration::from_secs(timeout_secs)).json(&req.body);
        for (k, v) in &req.headers {
            b = b.header(k.as_str(), v.as_str());
        }
        let resp = b.send().map_err(|e| TransportError(e.to_string()))?;
        let status = resp.status();
        let reason = status.canonical_reason().unwrap_or("").to_string();
        let body = resp.bytes().map_err(|e| TransportError(e.to_string()))?.to_vec();
        Ok(HttpResponse { status: status.as_u16(), reason, body })
    }
}
