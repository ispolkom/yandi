// src/ai_rpc/ollama.rs
//! Ollama HTTP Proxy
//! =================
//!
//! Thin async wrapper around the Ollama OpenAI-compatible API.
//! Supports both one-shot completion and token-by-token streaming.
//!
//! Only talks to localhost — the Ollama URL must be a loopback address;
//! this is enforced at construction time.

use std::time::Duration;

use reqwest::Client;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;
use tracing::{debug, error, warn};

use super::types::{AiInferPayload, AiInferResponse, ChatMessage, RpcError};

// ── Config ─────────────────────────────────────────────────────────────────

pub const DEFAULT_OLLAMA_URL: &str = "http://127.0.0.1:11434";

/// Hard cap on fetch response body: 512 KB.
pub const MAX_FETCH_BYTES: usize = 512 * 1024;

/// Connect timeout to Ollama.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// Total request timeout for inference (long completions take time).
const INFER_TIMEOUT: Duration = Duration::from_secs(120);

/// Timeout for the reachability probe.
const PROBE_TIMEOUT: Duration = Duration::from_secs(3);

// ── Ollama wire types ──────────────────────────────────────────────────────

#[derive(Debug, Serialize)]
struct OllamaChatRequest<'a> {
    model: &'a str,
    messages: &'a [OllamaMessage],
    stream: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    options: Option<OllamaOptions>,
}

#[derive(Debug, Serialize)]
struct OllamaMessage {
    role: String,
    content: String,
}

#[derive(Debug, Serialize)]
struct OllamaOptions {
    num_predict: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    temperature: Option<f32>,
}

#[derive(Debug, Deserialize)]
struct OllamaChatResponse {
    message: OllamaResponseMessage,
    #[serde(default)]
    done: bool,
    eval_count: Option<u32>,
}

#[derive(Debug, Deserialize)]
struct OllamaResponseMessage {
    content: String,
}

// ── OllamaProxy ────────────────────────────────────────────────────────────

pub struct OllamaProxy {
    base_url: String,
    client: Client,
}

impl OllamaProxy {
    /// Create a new proxy. `base_url` must be a loopback URL (safety check).
    pub fn new(base_url: &str) -> Result<Self, String> {
        if !is_loopback_url(base_url) {
            return Err(format!(
                "Ollama URL must point to localhost, got: {base_url}"
            ));
        }
        let client = Client::builder()
            .connect_timeout(CONNECT_TIMEOUT)
            .timeout(INFER_TIMEOUT)
            .build()
            .map_err(|e| format!("failed to build HTTP client: {e}"))?;

        Ok(Self { base_url: base_url.trim_end_matches('/').to_string(), client })
    }

    /// Quick liveness probe. Returns `true` if Ollama responds to `/api/tags`.
    pub async fn is_reachable(&self) -> bool {
        let url = format!("{}/api/tags", self.base_url);
        match self.client
            .get(&url)
            .timeout(PROBE_TIMEOUT)
            .send()
            .await
        {
            Ok(r) => r.status().is_success(),
            Err(_) => false,
        }
    }

    /// One-shot completion (non-streaming). Returns the full generated text.
    pub async fn complete(&self, req: &AiInferPayload) -> Result<AiInferResponse, RpcError> {
        let messages = to_ollama_messages(&req.messages);
        let body = OllamaChatRequest {
            model: &req.model,
            messages: &messages,
            stream: false,
            options: Some(OllamaOptions {
                num_predict: req.max_tokens,
                temperature: req.temperature,
            }),
        };

        let url = format!("{}/api/chat", self.base_url);
        debug!("ollama: POST {} model={}", url, req.model);

        let resp = self
            .client
            .post(&url)
            .json(&body)
            .send()
            .await
            .map_err(|e| {
                error!("ollama: request failed: {e}");
                RpcError::BackendError(e.to_string())
            })?;

        if !resp.status().is_success() {
            let status = resp.status().as_u16();
            let text = resp.text().await.unwrap_or_default();
            return Err(RpcError::BackendError(format!("Ollama HTTP {status}: {text}")));
        }

        let parsed: OllamaChatResponse = resp.json().await.map_err(|e| {
            RpcError::BackendError(format!("failed to parse Ollama response: {e}"))
        })?;

        Ok(AiInferResponse {
            content: parsed.message.content,
            tokens_used: parsed.eval_count,
        })
    }

    /// Streaming completion. Sends each token chunk to `tx`.
    /// Sends a final chunk with `is_chunk=false` to signal completion.
    pub async fn stream_complete(
        &self,
        req: &AiInferPayload,
        tx: mpsc::Sender<Result<String, RpcError>>,
    ) {
        let messages = to_ollama_messages(&req.messages);
        let body = OllamaChatRequest {
            model: &req.model,
            messages: &messages,
            stream: true,
            options: Some(OllamaOptions {
                num_predict: req.max_tokens,
                temperature: req.temperature,
            }),
        };

        let url = format!("{}/api/chat", self.base_url);
        debug!("ollama: streaming POST {} model={}", url, req.model);

        let resp = match self.client.post(&url).json(&body).send().await {
            Ok(r) if r.status().is_success() => r,
            Ok(r) => {
                let status = r.status().as_u16();
                let text = r.text().await.unwrap_or_default();
                let _ = tx
                    .send(Err(RpcError::BackendError(format!("Ollama HTTP {status}: {text}"))))
                    .await;
                return;
            }
            Err(e) => {
                let _ = tx.send(Err(RpcError::BackendError(e.to_string()))).await;
                return;
            }
        };

        // Read newline-delimited JSON stream
        use tokio::io::AsyncBufReadExt;
        use tokio_util::io::StreamReader;
        use futures_util::TryStreamExt;

        let stream = resp
            .bytes_stream()
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e));
        let reader = StreamReader::new(stream);
        let mut lines = reader.lines();

        loop {
            match lines.next_line().await {
                Ok(Some(line)) if !line.is_empty() => {
                    match serde_json::from_str::<OllamaChatResponse>(&line) {
                        Ok(chunk) => {
                            let done = chunk.done;
                            if tx.send(Ok(chunk.message.content)).await.is_err() {
                                // Receiver dropped — abort streaming
                                break;
                            }
                            if done {
                                break;
                            }
                        }
                        Err(e) => {
                            warn!("ollama: failed to parse stream chunk: {e} — line: {line}");
                        }
                    }
                }
                Ok(Some(_)) => {} // empty line, skip
                Ok(None) => break, // EOF
                Err(e) => {
                    let _ = tx
                        .send(Err(RpcError::BackendError(format!("stream read error: {e}"))))
                        .await;
                    break;
                }
            }
        }
    }
}

// ── HTTP fetch proxy ───────────────────────────────────────────────────────

/// Fetch a remote HTTP(S) resource via the local anchor.
/// Only HTTPS is allowed (privacy + security).
pub async fn fetch_url(
    client: &Client,
    url: &str,
    extra_headers: &[(String, String)],
) -> Result<super::types::FetchResponse, RpcError> {
    // Enforce HTTPS only
    if !url.starts_with("https://") {
        return Err(RpcError::FetchError(
            "only HTTPS URLs are permitted".to_string(),
        ));
    }

    let mut req = client.get(url).timeout(Duration::from_secs(30));
    for (k, v) in extra_headers {
        req = req.header(k.as_str(), v.as_str());
    }

    let resp = req.send().await.map_err(|e| RpcError::FetchError(e.to_string()))?;

    let status = resp.status().as_u16();
    let content_type = resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("text/plain")
        .to_string();

    let bytes = resp.bytes().await.map_err(|e| RpcError::FetchError(e.to_string()))?;
    let truncated = &bytes[..bytes.len().min(MAX_FETCH_BYTES)];
    let body = String::from_utf8_lossy(truncated).into_owned();

    Ok(super::types::FetchResponse { status_code: status, body, content_type })
}

// ── Helpers ────────────────────────────────────────────────────────────────

fn to_ollama_messages(msgs: &[ChatMessage]) -> Vec<OllamaMessage> {
    msgs.iter()
        .map(|m| OllamaMessage {
            role: m.role.clone(),
            content: m.content.clone(),
        })
        .collect()
}

fn is_loopback_url(url: &str) -> bool {
    url.starts_with("http://127.")
        || url.starts_with("http://[::1]")
        || url.starts_with("http://localhost")
}

// ── Tests ──────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_non_loopback_url() {
        assert!(OllamaProxy::new("http://192.168.1.10:11434").is_err());
        assert!(OllamaProxy::new("https://ollama.example.com").is_err());
    }

    #[test]
    fn accepts_loopback_url() {
        assert!(OllamaProxy::new("http://127.0.0.1:11434").is_ok());
        assert!(OllamaProxy::new("http://localhost:11434").is_ok());
    }

    #[test]
    fn loopback_url_check() {
        assert!(is_loopback_url("http://127.0.0.1:11434"));
        assert!(is_loopback_url("http://localhost:11434"));
        assert!(!is_loopback_url("http://192.168.1.1:11434"));
        assert!(!is_loopback_url("https://api.openai.com"));
    }
}
