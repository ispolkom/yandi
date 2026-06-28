// src/ai_rpc/mod.rs
//! AI-RPC — Federated AI task delegation + Knowledge Mesh
//! =======================================================
//!
//! Full chain: store locally → gossip to known peers via P2P.
//! Peers receive KbStore via signed RpcEnvelope and store locally.

pub mod knowledge;
pub mod ollama;
pub mod policy;
pub mod server;
pub mod types;

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use tokio::sync::{mpsc, Mutex};
use tracing::{info, warn};

use crate::util::HashId;

pub use knowledge::KnowledgeBase;
pub use ollama::DEFAULT_OLLAMA_URL;
pub use policy::{now_ms, sign_envelope, AllowedPeer, TrustPolicy};
pub use server::RpcServer;
pub use types::{
    AiInferPayload, AiInferResponse, AiRpcStatus, ChatMessage, FetchPayload,
    FetchResponse, KbSearchResponse, KbStorePayload, LocalFetchRequest, LocalInferRequest,
    LocalInferResponse, LocalKbSearchRequest, LocalKbStoreRequest, PongResponse,
    RpcEnvelope, RpcError, RpcMethod, RpcResponse, RpcStatus,
    AI_RPC_VERSION, PKT_AI_RPC_CHUNK, PKT_AI_RPC_ERROR, PKT_AI_RPC_REQUEST,
    PKT_AI_RPC_RESPONSE,
};

// ── AiRpcService ──────────────────────────────────────────────────────────

/// Top-level AI-RPC service.  Wrap in `Arc` for sharing across axum handlers.
pub struct AiRpcService {
    pub server: Arc<RpcServer>,
    ollama_url: String,
    local_ollama: Arc<ollama::OllamaProxy>,
    local_fetch_client: reqwest::Client,
    /// Shared KB — also held by RpcServer for inbound KbStore from peers.
    pub kb: Arc<Mutex<KnowledgeBase>>,
    /// Outbound P2P channel: send (peer_id, framed_bytes) to transport.
    gossip_tx: Option<mpsc::Sender<(HashId, Vec<u8>)>>,
    /// Known peer addresses for gossip.
    gossip_peers: Vec<HashId>,
    /// Node identity for signing outbound envelopes.
    signing_key: Option<ed25519_dalek::SigningKey>,
    node_address: [u8; 32],
    req_counter: Arc<AtomicU64>,
}

impl AiRpcService {
    pub fn new(ollama_url: &str) -> Result<Arc<Mutex<Self>>, String> {
        let kb = Arc::new(Mutex::new(KnowledgeBase::new()));
        let server = Arc::new(RpcServer::new(ollama_url, kb.clone())?);
        let local_ollama = Arc::new(ollama::OllamaProxy::new(ollama_url)?);
        let local_fetch_client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .user_agent("YANDI-AI-RPC/1.0 (local)")
            .build()
            .map_err(|e| format!("failed to build fetch client: {e}"))?;

        info!("ai_rpc: service initialised (ollama={})", ollama_url);

        Ok(Arc::new(Mutex::new(Self {
            server,
            ollama_url: ollama_url.to_string(),
            local_ollama,
            local_fetch_client,
            kb,
            gossip_tx: None,
            gossip_peers: Vec::new(),
            signing_key: None,
            node_address: [0u8; 32],
            req_counter: Arc::new(AtomicU64::new(1)),
        })))
    }

    // ── Gossip setup ───────────────────────────────────────────────────

    /// Set the outbound P2P channel and this node's signing key.
    pub fn set_gossip_channel(
        &mut self,
        tx: mpsc::Sender<(HashId, Vec<u8>)>,
        signing_key: ed25519_dalek::SigningKey,
        node_address: [u8; 32],
    ) {
        self.gossip_tx = Some(tx);
        self.node_address = node_address;
        self.signing_key = Some(signing_key);
        info!("ai_rpc: gossip channel set, node={}", hex::encode(&node_address[..8]));
    }

    /// Register a peer for gossip (call after each successful P2P handshake).
    pub fn register_gossip_peer(&mut self, peer_id: HashId) {
        if !self.gossip_peers.iter().any(|p| p.0 == peer_id.0) {
            info!("ai_rpc: gossip peer registered {}", hex::encode(&peer_id.0[..8]));
            self.gossip_peers.push(peer_id);
        }
    }

    // ── Peer management ────────────────────────────────────────────────

    pub async fn add_peer(&self, peer: AllowedPeer) -> Result<(), String> {
        self.server.add_peer(peer).await
    }

    pub async fn remove_peer(&self, address: &[u8; 32]) {
        self.server.remove_peer(address).await;
    }

    // ── P2P transport entry point ──────────────────────────────────────

    pub async fn handle_p2p_request(&self, raw: &[u8]) -> Vec<u8> {
        let resp = self.server.handle(raw).await;
        resp.to_bytes().unwrap_or_else(|e| {
            tracing::error!("ai_rpc: failed to serialise response: {e}");
            vec![]
        })
    }

    // ── Local (same-node) API ─────────────────────────────────────────

    pub async fn local_infer(
        &self,
        req: LocalInferRequest,
    ) -> Result<LocalInferResponse, RpcError> {
        let model = req.model.unwrap_or_else(|| "deepseek-r1:14b".to_string());
        let max_tokens = req.max_tokens.unwrap_or(2048);

        let total_chars: usize = req.messages.iter().map(|m| m.content.len()).sum();
        if total_chars > types::MAX_PROMPT_BYTES {
            return Err(RpcError::PayloadTooLarge(total_chars));
        }
        if model.trim().is_empty() {
            return Err(RpcError::InvalidPayload("model is empty".to_string()));
        }

        let payload = AiInferPayload {
            model: model.clone(),
            messages: req.messages,
            max_tokens,
            stream: false,
            temperature: req.temperature,
        };

        let resp = self.local_ollama.complete(&payload).await?;

        Ok(LocalInferResponse {
            content: resp.content,
            model,
            tokens_used: resp.tokens_used,
            via: "local".to_string(),
        })
    }

    pub async fn local_fetch(
        &self,
        req: LocalFetchRequest,
    ) -> Result<FetchResponse, RpcError> {
        let headers = req.headers.unwrap_or_default();
        ollama::fetch_url(&self.local_fetch_client, &req.url, &headers).await
    }

    // ── Knowledge base ─────────────────────────────────────────────────

    /// Store locally and gossip to all known peers.
    pub async fn kb_store(&mut self, req: LocalKbStoreRequest) -> String {
        let id = self.kb.lock().await.store(
            req.question.clone(),
            req.synthesis.clone(),
            req.models.clone(),
            req.domain.clone(),
        );

        self._gossip_kb(req.question, req.synthesis, req.models, req.domain).await;
        id
    }

    /// Search the local knowledge base.
    pub async fn kb_search(&self, req: LocalKbSearchRequest) -> KbSearchResponse {
        self.kb.lock().await.search(&req.query, req.top_k.unwrap_or(5))
    }

    async fn _gossip_kb(
        &self,
        question: String,
        synthesis: String,
        models: Vec<String>,
        domain: Option<String>,
    ) {
        let (Some(tx), Some(signing_key)) = (&self.gossip_tx, &self.signing_key) else {
            return;
        };
        if self.gossip_peers.is_empty() {
            return;
        }

        let payload = KbStorePayload { question, synthesis, models, domain };
        let payload_bytes = match bincode::serialize(&payload) {
            Ok(b) => b,
            Err(e) => { warn!("ai_rpc: gossip serialize error: {e}"); return; }
        };

        for peer_id in &self.gossip_peers {
            let req_id = self.req_counter.fetch_add(1, Ordering::Relaxed);
            let mut env = RpcEnvelope {
                version: AI_RPC_VERSION,
                request_id: req_id,
                nonce: rand::random(),
                timestamp_ms: now_ms(),
                sender: self.node_address,
                method: RpcMethod::KbStore,
                payload: payload_bytes.clone(),
                signature: vec![],
            };
            sign_envelope(&mut env, signing_key);

            let env_bytes = match env.to_bytes() {
                Ok(b) => b,
                Err(e) => { warn!("ai_rpc: gossip envelope error: {e}"); continue; }
            };

            let mut framed = Vec::with_capacity(1 + env_bytes.len());
            framed.push(PKT_AI_RPC_REQUEST);
            framed.extend_from_slice(&env_bytes);

            if let Err(e) = tx.try_send((*peer_id, framed)) {
                warn!("ai_rpc: gossip send error peer {}: {e}", hex::encode(&peer_id.0[..8]));
            } else {
                info!("ai_rpc: gossiped kb entry to peer {}", hex::encode(&peer_id.0[..8]));
            }
        }
    }

    // ── Status ─────────────────────────────────────────────────────────

    pub async fn status(&self) -> AiRpcStatus {
        let reachable = self.local_ollama.is_reachable().await;
        let counters = &self.server.counters;
        AiRpcStatus {
            enabled: true,
            ollama_url: self.ollama_url.clone(),
            ollama_reachable: reachable,
            allowed_peers: self.server.peer_count().await,
            requests_served: counters.requests_served.load(Ordering::Relaxed),
            errors_total: counters.errors_total.load(Ordering::Relaxed),
        }
    }
}
