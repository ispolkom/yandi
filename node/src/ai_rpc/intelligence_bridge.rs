// src/ai_rpc/intelligence_bridge.rs
//! Local Intelligence Bridge Client
//! ================================
//!
//! Replaces the previous hardcoded Ollama coupling
//! (`node/src/ai_rpc/ollama.rs::OllamaProxy`) as the thing this node
//! actually calls to answer an AI-RPC inference request — whether that
//! request came from a peer over P2P (`RpcServer::handle_ai_infer`) or
//! from a local caller on this same machine (`AiRpcService::local_infer`).
//!
//! **Why this exists (mandate "Node Intelligence RPC migration"):** the
//! Rust transport must never re-implement backend selection (explicit
//! config, local engine, remote API, Ollama-compat fallback, secure
//! credential storage) — that logic lives in exactly one place,
//! `llm_gateway` (Python). This client only speaks a tiny, backend-
//! agnostic HTTP contract to `llm_gateway.intelligence_bridge` (loopback
//! only, see that module's docstring). Rust transports; Python decides
//! what actually thinks.
//!
//! **Closes a real hole found during audit:** `AiInferPayload.model` is
//! part of the wire format and is populated by whoever sent the
//! request — including a remote peer. The OLD code forwarded that
//! caller-supplied model name straight to Ollama, meaning a peer could
//! dictate which model answered it. This client deliberately DROPS
//! `req.model` — it is never sent to the bridge. The bridge always
//! resolves its own fixed logical alias (`yandi:peer-default`), which
//! this node's owner configures (or doesn't) exactly like any other
//! `llm_gateway` model alias. A caller can still populate `model` in
//! the wire struct (kept for backward wire compatibility / the
//! "non-empty model" sanity check upstream), but it has no effect on
//! which backend answers.

use std::sync::{Arc, OnceLock};
use std::time::Duration;

use reqwest::Client;
use serde::{Deserialize, Serialize};

use super::types::{AiInferPayload, AiInferResponse, ChatMessage, RpcError};

pub const DEFAULT_BRIDGE_URL: &str = "http://127.0.0.1:18083";

const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const INFER_TIMEOUT: Duration = Duration::from_secs(120);

#[derive(Debug, Serialize)]
struct BridgeMessage<'a> {
    role: &'a str,
    content: &'a str,
}

#[derive(Debug, Serialize)]
struct BridgeInferRequest<'a> {
    request_id: &'a str,
    messages: Vec<BridgeMessage<'a>>,
    max_tokens: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    temperature: Option<f32>,
}

#[derive(Debug, Deserialize)]
struct BridgeInferResponse {
    success: bool,
    text: Option<String>,
    tokens_used: Option<u32>,
    #[serde(default)]
    error: Option<String>,
}

/// Собственный движок узла, общий на весь процесс: RpcServer и локальный клиент НЕ должны поднимать по своему `llama-server` (две копии модели в памяти).
static NATIVE: OnceLock<Arc<NativeIntelligence>> = OnceLock::new();

/// Родной «мост интеллекта» внутри процесса узла (`yandi_llm`): настройки владельца из хранилища → собственный движок (вшитый `llama-server`).
/// HTTP-моста на Python не нужно. Блокирующие вызовы выполняются в `spawn_blocking`.
pub struct NativeIntelligence {
    engine: yandi_llm::server_engine::ServerEngine,
}

impl NativeIntelligence {
    fn shared() -> Arc<NativeIntelligence> {
        NATIVE
            .get_or_init(|| Arc::new(NativeIntelligence { engine: yandi_llm::server_engine::ServerEngine::new(yandi_llm::server_engine::ServerEngineConfig::new(vec![])) }))
            .clone()
    }

    /// Один запрос (блокирующий): `(статус, тело)` в форме Python-моста.
    fn infer_blocking(&self, body: &serde_json::Value) -> (u16, serde_json::Value) {
        use yandi_llm::client::{Gateway, GatewayOptions, SecureStoreConfig};
        // блокирующий HTTP-клиент создаётся ТОЛЬКО в потоке spawn_blocking (в async-контексте он паникует)
        static TRANSPORT: OnceLock<yandi_llm::transport::ReqwestTransport> = OnceLock::new();
        let transport = TRANSPORT.get_or_init(yandi_llm::transport::ReqwestTransport::new);
        let gw = Gateway { transport, config: &SecureStoreConfig, engine: &self.engine, opts: GatewayOptions::from_env() };
        yandi_llm::intelligence::handle_infer(&gw, body, &|m| eprintln!("{m}"))
    }
}

impl NativeIntelligence {
    /// Общий вызов шлюза для ядра на Python (`complete`/`embed`/…): те же настройки владельца и тот же движок, что у моста интеллекта.
    fn call_blocking(&self, name: &str, args: &serde_json::Value) -> Result<serde_json::Value, String> {
        use yandi_llm::client::{Gateway, GatewayOptions, SecureStoreConfig};
        static TRANSPORT: OnceLock<yandi_llm::transport::ReqwestTransport> = OnceLock::new();
        let transport = TRANSPORT.get_or_init(yandi_llm::transport::ReqwestTransport::new);
        let gw = Gateway { transport, config: &SecureStoreConfig, engine: &self.engine, opts: GatewayOptions::from_env() };
        yandi_llm::api::gateway_call(&gw, name, args)
    }
}

/// Для локального HTTP узла (`POST /api/gateway/call`): `(HTTP-статус, тело)`. Тело — `{"ok": …}` / `{"error": {"class", "msg"}}` как у `yandi_llm::api`.
pub async fn gateway_call(name: String, args: serde_json::Value) -> (u16, serde_json::Value) {
    let native = NativeIntelligence::shared();
    match tokio::task::spawn_blocking(move || native.call_blocking(&name, &args)).await {
        Ok(Ok(v)) => (200, v),
        Ok(Err(e)) => (400, serde_json::json!({"error": {"class": "BadRequest", "msg": e}})),
        Err(e) => (500, serde_json::json!({"error": {"class": "InternalError", "msg": e.to_string()}})),
    }
}

/// Какой мост использовать: `YANDI_INTELLIGENCE_ENGINE=native|python`; по умолчанию — родной, если в бинарник вшит движок
/// (`YANDI_EMBED_LLAMA_SERVER` при сборке), иначе прежний Python-мост.
pub fn use_native() -> bool {
    match std::env::var("YANDI_INTELLIGENCE_ENGINE").ok().as_deref() {
        Some("native") => true,
        Some("python") => false,
        _ => yandi_llm::engine_binary::has_embedded(),
    }
}

pub struct IntelligenceBridgeClient {
    base_url: String,
    client: Client,
    native: Option<Arc<NativeIntelligence>>,
}

impl IntelligenceBridgeClient {
    pub fn new(base_url: &str) -> Result<Self, String> {
        if !base_url.starts_with("http://127.0.0.1")
            && !base_url.starts_with("http://localhost")
        {
            return Err(format!(
                "intelligence bridge URL must be loopback, got: {base_url}"
            ));
        }
        let client = Client::builder()
            .connect_timeout(CONNECT_TIMEOUT)
            .timeout(INFER_TIMEOUT)
            .user_agent("YANDI-AI-RPC/1.0 (intelligence-bridge)")
            // Loopback-only traffic must never be routed through a
            // system HTTP(S) proxy — it wouldn't reach 127.0.0.1 anyway,
            // and a local intelligence bridge has no business leaving
            // this machine at all.
            .no_proxy()
            .build()
            .map_err(|e| format!("failed to build intelligence bridge client: {e}"))?;
        let native = if use_native() { Some(NativeIntelligence::shared()) } else { None };
        Ok(Self { base_url: base_url.to_string(), client, native })
    }

    /// Run one inference request through the local `llm_gateway` bridge.
    /// `req.model` is intentionally never transmitted — see module docs.
    pub async fn complete(&self, req: &AiInferPayload) -> Result<AiInferResponse, RpcError> {
        let messages: Vec<BridgeMessage> = req
            .messages
            .iter()
            .map(|m: &ChatMessage| BridgeMessage { role: &m.role, content: &m.content })
            .collect();

        let body = BridgeInferRequest {
            request_id: "ai-rpc",
            messages,
            max_tokens: req.max_tokens,
            temperature: req.temperature,
        };

        if let Some(native) = &self.native {
            let body = serde_json::to_value(&body).map_err(|e| RpcError::BackendError(format!("bad request: {e}")))?;
            let native = native.clone();
            let (status, out) = tokio::task::spawn_blocking(move || native.infer_blocking(&body))
                .await
                .map_err(|e| RpcError::BackendError(format!("native intelligence task failed: {e}")))?;
            if status != 200 || out["success"] != serde_json::json!(true) {
                return Err(RpcError::BackendError(out["error"].as_str().unwrap_or("backend_error").to_string()));
            }
            return Ok(AiInferResponse {
                content: out["text"].as_str().unwrap_or_default().to_string(),
                tokens_used: out["tokens_used"].as_u64().map(|n| n as u32),
            });
        }

        let resp = self
            .client
            .post(format!("{}/infer", self.base_url))
            .json(&body)
            .send()
            .await
            .map_err(|e| RpcError::BackendError(format!("intelligence bridge unreachable: {e}")))?;

        let parsed: BridgeInferResponse = resp
            .json()
            .await
            .map_err(|e| RpcError::BackendError(format!("intelligence bridge returned invalid response: {e}")))?;

        if !parsed.success {
            return Err(RpcError::BackendError(
                parsed.error.unwrap_or_else(|| "backend_error".to_string()),
            ));
        }

        Ok(AiInferResponse {
            content: parsed.text.unwrap_or_default(),
            tokens_used: parsed.tokens_used,
        })
    }

    pub async fn is_reachable(&self) -> bool {
        if let Some(native) = &self.native {
            use yandi_llm::client::LocalEngine;
            return native.engine.registry_error().is_none();
        }
        // A cheap reachability probe that never invokes a backend: an
        // intentionally malformed request (no messages) is rejected by
        // the bridge with HTTP 400 before it ever calls llm_gateway —
        // any HTTP response at all (even an error one) proves the
        // bridge process is alive and listening.
        self.client
            .post(format!("{}/infer", self.base_url))
            .json(&serde_json::json!({}))
            .send()
            .await
            .is_ok()
    }
}
