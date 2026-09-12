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
    intelligence_bridge::IntelligenceBridgeClient,
    knowledge::KnowledgeBase,
    ollama::fetch_url,
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
    intelligence: IntelligenceBridgeClient,
    fetch_client: reqwest::Client,
    pub counters: Arc<RpcCounters>,
    pub kb: Arc<Mutex<KnowledgeBase>>,
    /// Node Identity Binding Fix (Barrier 2): this node's own signing
    /// identity, used to sign every outgoing RpcResponse — success AND
    /// error alike — so a requester can verify the answer really came
    /// from the node it asked, independent of whatever the transport
    /// layer's routing table currently believes.
    signing_key: ed25519_dalek::SigningKey,
    node_address: [u8; 32],
}

impl RpcServer {
    pub fn new(
        bridge_url: &str,
        kb: Arc<Mutex<KnowledgeBase>>,
        signing_key: ed25519_dalek::SigningKey,
        node_address: [u8; 32],
    ) -> Result<Self, String> {
        let intelligence = IntelligenceBridgeClient::new(bridge_url)?;
        let fetch_client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .user_agent("YANDI-AI-RPC/1.0")
            .build()
            .map_err(|e| format!("failed to build fetch client: {e}"))?;

        Ok(Self {
            policy: Mutex::new(TrustPolicy::new()),
            intelligence,
            fetch_client,
            counters: RpcCounters::new(),
            kb,
            signing_key,
            node_address,
        })
    }

    /// Build and sign a response. `requester` is the node_id that sent the
    /// original request (`RpcEnvelope::sender`) — bound into the signature
    /// so this exact response can never be replayed as an answer to a
    /// different peer, even if their `request_id` counters collide.
    fn build_response(&self, request_id: u64, requester: [u8; 32], status: RpcStatus, payload: Vec<u8>) -> RpcResponse {
        use ed25519_dalek::Signer;
        let mut resp = RpcResponse {
            request_id,
            requester,
            responder: self.node_address,
            status,
            is_chunk: false,
            chunk_done: true,
            payload,
            signature: Vec::new(),
        };
        let canonical = resp.canonical_bytes();
        resp.signature = self.signing_key.sign(&canonical).to_bytes().to_vec();
        resp
    }

    fn ok_response(&self, request_id: u64, requester: [u8; 32], payload: Vec<u8>) -> RpcResponse {
        self.build_response(request_id, requester, RpcStatus::Ok, payload)
    }

    fn error_response(&self, request_id: u64, requester: [u8; 32], err: RpcError) -> RpcResponse {
        self.build_response(request_id, requester, RpcStatus::Err(err), vec![])
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
                return self.error_response(0, [0u8; 32], RpcError::InvalidPayload(e.to_string()));
            }
        };

        let request_id = env.request_id;

        // Validate: version, timestamp, allowlist, rate limit, nonce, signature
        if let Err(e) = self.policy.lock().await.validate(&env) {
            self.counters.errors_total.fetch_add(1, Ordering::Relaxed);
            return self.error_response(request_id, env.sender, e);
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
                self.error_response(request_id, env.sender, e)
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
        Ok(self.ok_response(env.request_id, env.sender, payload))
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

        // Мандат "Node Intelligence RPC migration" (§6): sanitised.model
        // приходит от ПИРА и логируется здесь только для трассировки —
        // IntelligenceBridgeClient::complete() ниже НИКОГДА не передаёт
        // это имя дальше; какой backend реально отвечает, решает
        // владелец ЭТОЙ ноды через свой llm_gateway, а не отправитель
        // запроса.
        info!(
            "ai_rpc: infer from peer {} requested_model={} tokens={}",
            hex::encode(&env.sender[..8]),
            sanitised.model,
            sanitised.max_tokens,
        );

        let infer_resp = self.intelligence.complete(&sanitised).await?;

        let payload = bincode::serialize(&infer_resp)
            .map_err(|e| RpcError::BackendError(e.to_string()))?;
        Ok(self.ok_response(env.request_id, env.sender, payload))
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
        Ok(self.ok_response(env.request_id, env.sender, payload))
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
        Ok(self.ok_response(env.request_id, env.sender, payload))
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
        Ok(self.ok_response(env.request_id, env.sender, payload))
    }
}

