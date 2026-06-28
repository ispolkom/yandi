// src/ai_rpc/server.rs
//! RPC Server — incoming request handler
//! ======================================
//!
//! `RpcServer` is the receiver side: validates an inbound `RpcEnvelope`,
//! dispatches to the correct handler, and returns an `RpcResponse`.
//!
//! **Integration point for P2P transport:**
//! When a wagon frame carrying `PKT_AI_RPC_REQUEST` arrives from a peer,
//! decode the payload to `RpcEnvelope` and call `RpcServer::handle()`.
//! The returned `RpcResponse` should be re-encoded and sent back via
//! `PKT_AI_RPC_RESPONSE`.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use tokio::sync::Mutex;
use tracing::{info, warn};

use super::{
    knowledge::KnowledgeBase,
    ollama::{fetch_url, OllamaProxy},
    policy::TrustPolicy,
    types::{
        AiInferPayload, FetchPayload, KbSearchPayload, KbStorePayload,
        PongResponse, RpcEnvelope, RpcError, RpcMethod, RpcResponse, RpcStatus,
        MAX_PROMPT_BYTES, MAX_TOKENS_ALLOWED,
    },
};
use crate::ai_rpc::policy::now_ms;

// ── Counters ───────────────────────────────────────────────────────────────

pub struct RpcCounters {
    pub requests_served: AtomicU64,
    pub errors_total: AtomicU64,
}

impl RpcCounters {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            requests_served: AtomicU64::new(0),
            errors_total: AtomicU64::new(0),
        })
    }
}

impl Default for RpcCounters {
    fn default() -> Self {
        Self {
            requests_served: AtomicU64::new(0),
            errors_total: AtomicU64::new(0),
        }
    }
}

// ── RpcServer ──────────────────────────────────────────────────────────────

pub struct RpcServer {
    policy: Mutex<TrustPolicy>,
    ollama: OllamaProxy,
    fetch_client: reqwest::Client,
    pub counters: Arc<RpcCounters>,
    pub kb: Arc<Mutex<KnowledgeBase>>,
}

impl RpcServer {
    pub fn new(ollama_url: &str, kb: Arc<Mutex<KnowledgeBase>>) -> Result<Self, String> {
        let ollama = OllamaProxy::new(ollama_url)?;
        let fetch_client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .user_agent("YANDI-AI-RPC/1.0")
            .build()
            .map_err(|e| format!("failed to build fetch client: {e}"))?;

        Ok(Self {
            policy: Mutex::new(TrustPolicy::new()),
            ollama,
            fetch_client,
            counters: RpcCounters::new(),
            kb,
        })
    }

    /// Grant access to a peer. Thread-safe.
    pub async fn add_peer(&self, peer: super::policy::AllowedPeer) -> Result<(), String> {
        self.policy.lock().await.add_peer(peer)
    }

    /// Revoke access. Thread-safe.
    pub async fn remove_peer(&self, address: &[u8; 32]) {
        self.policy.lock().await.remove_peer(address);
    }

    pub async fn peer_count(&self) -> usize {
        self.policy.lock().await.peer_count()
    }

    /// Main entry point.
    /// Called by the P2P transport layer when a `PKT_AI_RPC_REQUEST` frame arrives.
    pub async fn handle(&self, raw: &[u8]) -> RpcResponse {
        let env = match RpcEnvelope::from_bytes(raw) {
            Ok(e) => e,
            Err(e) => {
                self.counters.errors_total.fetch_add(1, Ordering::Relaxed);
                warn!("ai_rpc: failed to decode envelope: {e}");
                return error_response(0, RpcError::InvalidPayload(e.to_string()));
            }
        };

        let request_id = env.request_id;

        // Validate: version, timestamp, allowlist, rate limit, nonce, signature
        if let Err(e) = self.policy.lock().await.validate(&env) {
            self.counters.errors_total.fetch_add(1, Ordering::Relaxed);
            return error_response(request_id, e);
        }

        // Dispatch
        let result = self.dispatch(&env).await;

        match result {
            Ok(resp) => {
                self.counters.requests_served.fetch_add(1, Ordering::Relaxed);
                resp
            }
            Err(e) => {
                self.counters.errors_total.fetch_add(1, Ordering::Relaxed);
                error_response(request_id, e)
            }
        }
    }

    async fn dispatch(&self, env: &RpcEnvelope) -> Result<RpcResponse, RpcError> {
        match env.method {
            RpcMethod::Ping => self.handle_ping(env).await,
            RpcMethod::AiInfer => self.handle_ai_infer(env).await,
            RpcMethod::Fetch => self.handle_fetch(env).await,
            RpcMethod::KbSearch => self.handle_kb_search(env).await,
            RpcMethod::KbStore => self.handle_kb_store(env).await,
        }
    }

    async fn handle_ping(&self, env: &RpcEnvelope) -> Result<RpcResponse, RpcError> {
        info!(
            "ai_rpc: ping from peer {}",
            hex::encode(&env.sender[..8])
        );
        let pong = PongResponse {
            echo_request_id: env.request_id,
            server_time_ms: now_ms(),
        };
        let payload = bincode::serialize(&pong)
            .map_err(|e| RpcError::BackendError(e.to_string()))?;
        Ok(ok_response(env.request_id, payload))
    }

    async fn handle_ai_infer(&self, env: &RpcEnvelope) -> Result<RpcResponse, RpcError> {
        // Payload size guard (raw bytes, before decode)
        if env.payload.len() > MAX_PROMPT_BYTES {
            return Err(RpcError::PayloadTooLarge(env.payload.len()));
        }

        let req: AiInferPayload = bincode::deserialize(&env.payload)
            .map_err(|e| RpcError::InvalidPayload(e.to_string()))?;

        // Sanitise: cap tokens
        let max_tokens = req.max_tokens.min(MAX_TOKENS_ALLOWED);

        // Validate prompt content size (not just wire bytes)
        let total_chars: usize = req.messages.iter().map(|m| m.content.len()).sum();
        if total_chars > MAX_PROMPT_BYTES {
            return Err(RpcError::PayloadTooLarge(total_chars));
        }

        // Reject empty model strings
        if req.model.trim().is_empty() {
            return Err(RpcError::InvalidPayload("model name is empty".to_string()));
        }

        let sanitised = AiInferPayload { max_tokens, ..req };

        info!(
            "ai_rpc: infer from peer {} model={} tokens={}",
            hex::encode(&env.sender[..8]),
            sanitised.model,
            sanitised.max_tokens,
        );

        let infer_resp = self.ollama.complete(&sanitised).await?;

        let payload = bincode::serialize(&infer_resp)
            .map_err(|e| RpcError::BackendError(e.to_string()))?;
        Ok(ok_response(env.request_id, payload))
    }

    async fn handle_fetch(&self, env: &RpcEnvelope) -> Result<RpcResponse, RpcError> {
        let req: FetchPayload = bincode::deserialize(&env.payload)
            .map_err(|e| RpcError::InvalidPayload(e.to_string()))?;

        // URL length guard
        if req.url.len() > 2048 {
            return Err(RpcError::InvalidPayload("URL too long".to_string()));
        }

        info!(
            "ai_rpc: fetch from peer {} url={}",
            hex::encode(&env.sender[..8]),
            &req.url[..req.url.len().min(80)]
        );

        let fetch_resp =
            fetch_url(&self.fetch_client, &req.url, &req.headers).await?;

        let payload = bincode::serialize(&fetch_resp)
            .map_err(|e| RpcError::BackendError(e.to_string()))?;
        Ok(ok_response(env.request_id, payload))
    }

    async fn handle_kb_store(&self, env: &RpcEnvelope) -> Result<RpcResponse, RpcError> {
        let req: KbStorePayload = bincode::deserialize(&env.payload)
            .map_err(|e| RpcError::InvalidPayload(e.to_string()))?;

        info!(
            "ai_rpc: kb_store from peer {} q={:?}",
            hex::encode(&env.sender[..8]),
            &req.question[..req.question.len().min(60)]
        );

        let id = self.kb.lock().await.store(req.question, req.synthesis, req.models, req.domain);

        let payload = bincode::serialize(&id)
            .map_err(|e| RpcError::BackendError(e.to_string()))?;
        Ok(ok_response(env.request_id, payload))
    }

    async fn handle_kb_search(&self, env: &RpcEnvelope) -> Result<RpcResponse, RpcError> {
        let req: KbSearchPayload = bincode::deserialize(&env.payload)
            .map_err(|e| RpcError::InvalidPayload(e.to_string()))?;

        info!(
            "ai_rpc: kb_search from peer {} query={:?}",
            hex::encode(&env.sender[..8]),
            &req.query[..req.query.len().min(60)]
        );

        // Stub: KbSearch is defined in the protocol but local KB is not yet implemented.
        // Returns an empty result set. Iter 8 (DHT-shared KB) will fill this in.
        let payload = bincode::serialize(&Vec::<String>::new())
            .map_err(|e| RpcError::BackendError(e.to_string()))?;
        Ok(ok_response(env.request_id, payload))
    }
}

// ── Response helpers ───────────────────────────────────────────────────────

fn ok_response(request_id: u64, payload: Vec<u8>) -> RpcResponse {
    RpcResponse {
        request_id,
        status: RpcStatus::Ok,
        is_chunk: false,
        chunk_done: true,
        payload,
    }
}

fn error_response(request_id: u64, err: RpcError) -> RpcResponse {
    RpcResponse {
        request_id,
        status: RpcStatus::Err(err),
        is_chunk: false,
        chunk_done: true,
        payload: vec![],
    }
}
