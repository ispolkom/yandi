// src/ai_rpc/mod.rs
//! AI-RPC — Federated AI task delegation + Knowledge Mesh
//! =======================================================
//!
//! Full chain: store locally → gossip to known peers via P2P.
//! Peers receive KbStore via signed RpcEnvelope and store locally.

pub mod intelligence_bridge;
pub mod knowledge;
pub mod ollama;
pub mod peer_directory;
pub mod policy;
pub mod server;
pub mod types;

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::{mpsc, oneshot, Mutex};
use tracing::{info, warn};

use crate::util::HashId;

pub use intelligence_bridge::DEFAULT_BRIDGE_URL;
pub use knowledge::KnowledgeBase;
pub use peer_directory::{PeerDirectory, PeerDirectoryEntry};
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

/// How long a node will wait for a peer to answer an outbound AiInfer
/// request before giving up. Generous — a peer may itself be waiting on
/// a slow local backend (llama.cpp on CPU, a remote API under load).
const PEER_INFER_TIMEOUT: Duration = Duration::from_secs(90);

/// Outbound-request correlation table, deliberately factored OUT of
/// `AiRpcService` itself and given its own independent `Arc<Mutex<_>>`.
///
/// Why: `AiRpcService` is normally shared as `Arc<Mutex<AiRpcService>>`
/// (see `main.rs`/`ai_rpc_server.rs`). A call chain like
/// `local_infer()` → `send_ai_infer_remote()` holds that OUTER lock for
/// its entire `.await` on a peer's reply. If the response were resolved
/// via a SECOND `svc.lock().await.resolve_pending(...)` call (as an
/// `AiRpcService` method would require), it could never acquire that
/// same outer lock until the first call's own timeout — the reply could
/// never actually be delivered. `PendingRequests` is a tiny standalone
/// handle a caller (`main.rs`) can clone out ONCE and use directly,
/// without ever going through `Mutex<AiRpcService>` again, so resolving
/// a reply never contends with a call that's still waiting for one.
#[derive(Clone)]
pub struct PendingRequests(Arc<Mutex<HashMap<u64, oneshot::Sender<RpcResponse>>>>);

impl PendingRequests {
    fn new() -> Self {
        Self(Arc::new(Mutex::new(HashMap::new())))
    }

    async fn insert(&self, request_id: u64, tx: oneshot::Sender<RpcResponse>) {
        self.0.lock().await.insert(request_id, tx);
    }

    async fn take(&self, request_id: u64) -> Option<oneshot::Sender<RpcResponse>> {
        self.0.lock().await.remove(&request_id)
    }

    /// Resolve a peer's `PKT_AI_RPC_RESPONSE` against a pending outbound
    /// request, if one is still waiting. Unmatched/late responses
    /// (already timed out, or not ours) are silently dropped — not a
    /// protocol violation, just a race we already gave up on.
    pub async fn resolve(&self, resp: RpcResponse) {
        if let Some(tx) = self.take(resp.request_id).await {
            let _ = tx.send(resp);
        }
    }
}

// ── AiRpcService ──────────────────────────────────────────────────────────

/// Top-level AI-RPC service.  Wrap in `Arc` for sharing across axum handlers.
pub struct AiRpcService {
    pub server: Arc<RpcServer>,
    bridge_url: String,
    local_intelligence: Arc<intelligence_bridge::IntelligenceBridgeClient>,
    local_fetch_client: reqwest::Client,
    /// Shared KB — also held by RpcServer for inbound KbStore from peers.
    pub kb: Arc<Mutex<KnowledgeBase>>,
    /// Outbound P2P channel: send (peer_id, framed_bytes) to transport.
    /// Reused both for KbStore gossip AND for outbound AiInfer requests
    /// to a peer (the wire framing is identical: PKT_AI_RPC_REQUEST +
    /// signed envelope — only the envelope's `method` differs).
    gossip_tx: Option<mpsc::Sender<(HashId, Vec<u8>)>>,
    /// Known peer addresses for gossip.
    gossip_peers: Vec<HashId>,
    /// Node identity for signing outbound envelopes.
    signing_key: Option<ed25519_dalek::SigningKey>,
    node_address: [u8; 32],
    req_counter: Arc<AtomicU64>,
    /// Outbound requests awaiting a peer's PKT_AI_RPC_RESPONSE. See
    /// `send_ai_infer_remote` and the `PendingRequests` doc comment for
    /// why this is NOT a plain field guarded by the outer service lock.
    pending: PendingRequests,
    /// Real Node Directory Integration: the trusted AI-RPC peers this
    /// node's owner has explicitly recorded (see `peer_directory.rs`).
    /// Same source `main.rs` uses to populate the policy allowlist —
    /// held here too so the local HTTP surface can list it for Python
    /// (`GET /api/ai-rpc/peers`) without a second source of truth.
    peer_directory: Arc<peer_directory::PeerDirectory>,
}

impl AiRpcService {
    pub fn new(bridge_url: &str) -> Result<Arc<Mutex<Self>>, String> {
        let kb = Arc::new(Mutex::new(KnowledgeBase::new()));
        let server = Arc::new(RpcServer::new(bridge_url, kb.clone())?);
        let local_intelligence = Arc::new(intelligence_bridge::IntelligenceBridgeClient::new(bridge_url)?);
        let local_fetch_client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .user_agent("YANDI-AI-RPC/1.0 (local)")
            .build()
            .map_err(|e| format!("failed to build fetch client: {e}"))?;

        info!("ai_rpc: service initialised (intelligence bridge={})", bridge_url);

        Ok(Arc::new(Mutex::new(Self {
            server,
            bridge_url: bridge_url.to_string(),
            local_intelligence,
            local_fetch_client,
            kb,
            gossip_tx: None,
            gossip_peers: Vec::new(),
            signing_key: None,
            node_address: [0u8; 32],
            req_counter: Arc::new(AtomicU64::new(1)),
            pending: PendingRequests::new(),
            peer_directory: Arc::new(peer_directory::PeerDirectory::default()),
        })))
    }

    /// Clone out the pending-requests handle for a caller (`main.rs`)
    /// to use DIRECTLY in its response-resolving task — see the
    /// `PendingRequests` doc comment for why this must never be reached
    /// through `Arc<Mutex<AiRpcService>>` again after this call.
    pub fn pending_requests(&self) -> PendingRequests {
        self.pending.clone()
    }

    /// Record the trusted-AI-peer directory (loaded once at startup by
    /// `main.rs`, the same source used to populate the policy allowlist)
    /// so `GET /api/ai-rpc/peers` can list it.
    pub fn set_peer_directory(&mut self, dir: Arc<peer_directory::PeerDirectory>) {
        self.peer_directory = dir;
    }

    pub fn peer_directory(&self) -> Arc<peer_directory::PeerDirectory> {
        self.peer_directory.clone()
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
        // "model" здесь — ЛОГИЧЕСКОЕ имя для локального (PET) вызывающего
        // на ЭТОЙ же машине, не выбор физического backend'а — то решает
        // llm_gateway на стороне, которая реально отвечает (эта нода,
        // если remote_peer не задан; чужая, если задан).
        let model = req.model.clone().unwrap_or_else(|| "yandi:peer-default".to_string());
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

        if let Some(peer_hex) = req.remote_peer.as_deref() {
            // Реализация давно объявленного, но ранее мёртвого поля
            // remote_peer (мандат "Node Intelligence RPC migration",
            // §8): нода А спрашивает ноду B через подписанный P2P AI-RPC,
            // а не свою собственную Ollama. B сама решает, каким
            // backend'ом отвечать — А (и этот код) этого не видит.
            let peer_id = HashId::from_hex(peer_hex)
                .map_err(|e| RpcError::InvalidPayload(format!("invalid remote_peer: {e}")))?;
            let resp = self.send_ai_infer_remote(peer_id, payload).await?;
            return Ok(LocalInferResponse {
                content: resp.content,
                model,
                tokens_used: resp.tokens_used,
                via: format!("remote:{peer_hex}"),
            });
        }

        let resp = self.local_intelligence.complete(&payload).await?;

        Ok(LocalInferResponse {
            content: resp.content,
            model,
            tokens_used: resp.tokens_used,
            via: "local".to_string(),
        })
    }

    // ── Outbound peer inference (Node Intelligence RPC) ──────────────────

    /// Send a signed AiInfer request to `peer_id` over the existing P2P
    /// transport and await its `PKT_AI_RPC_RESPONSE`, honestly failing
    /// (timeout / transport error) rather than ever substituting a local
    /// answer. Reuses exactly the same envelope-signing and framing
    /// pattern as `_gossip_kb` — no new wire mechanism, just a new method.
    pub async fn send_ai_infer_remote(
        &self,
        peer_id: HashId,
        payload: AiInferPayload,
    ) -> Result<AiInferResponse, RpcError> {
        let (Some(tx), Some(signing_key)) = (&self.gossip_tx, &self.signing_key) else {
            return Err(RpcError::BackendError(
                "outbound P2P channel not configured on this node".to_string(),
            ));
        };

        let payload_bytes = bincode::serialize(&payload)
            .map_err(|e| RpcError::InvalidPayload(format!("serialize AiInferPayload: {e}")))?;

        let req_id = self.req_counter.fetch_add(1, Ordering::Relaxed);
        let mut env = RpcEnvelope {
            version: AI_RPC_VERSION,
            request_id: req_id,
            nonce: rand::random(),
            timestamp_ms: now_ms(),
            sender: self.node_address,
            method: RpcMethod::AiInfer,
            payload: payload_bytes,
            signature: vec![],
        };
        sign_envelope(&mut env, signing_key);

        let env_bytes = env
            .to_bytes()
            .map_err(|e| RpcError::InvalidPayload(format!("serialize envelope: {e}")))?;
        let mut framed = Vec::with_capacity(1 + env_bytes.len());
        framed.push(PKT_AI_RPC_REQUEST);
        framed.extend_from_slice(&env_bytes);

        // Register the pending slot BEFORE sending — avoids a race where
        // an extremely fast reply arrives before we start waiting for
        // it. This never touches the outer AiRpcService lock — see
        // `PendingRequests` doc comment.
        let (resp_tx, resp_rx) = oneshot::channel();
        self.pending.insert(req_id, resp_tx).await;

        if let Err(e) = tx.try_send((peer_id, framed)) {
            self.pending.take(req_id).await;
            return Err(RpcError::BackendError(format!("failed to send to peer: {e}")));
        }

        let resp = match tokio::time::timeout(PEER_INFER_TIMEOUT, resp_rx).await {
            Ok(Ok(resp)) => resp,
            Ok(Err(_)) => {
                return Err(RpcError::BackendError(
                    "internal: pending response channel closed".to_string(),
                ))
            }
            Err(_) => {
                self.pending.take(req_id).await;
                return Err(RpcError::BackendError(
                    "peer did not answer in time".to_string(),
                ));
            }
        };

        match resp.status {
            RpcStatus::Ok => bincode::deserialize::<AiInferResponse>(&resp.payload)
                .map_err(|e| RpcError::BackendError(format!("malformed peer response: {e}"))),
            RpcStatus::Err(e) => Err(e),
        }
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
        let reachable = self.local_intelligence.is_reachable().await;
        let counters = &self.server.counters;
        AiRpcStatus {
            enabled: true,
            backend_url: self.bridge_url.clone(),
            backend_reachable: reachable,
            allowed_peers: self.server.peer_count().await,
            requests_served: counters.requests_served.load(Ordering::Relaxed),
            errors_total: counters.errors_total.load(Ordering::Relaxed),
        }
    }
}
