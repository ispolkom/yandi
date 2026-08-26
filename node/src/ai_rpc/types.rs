// src/ai_rpc/types.rs
//! AI-RPC Wire Format
//! ==================
//!
//! Wire protocol for inter-node AI task delegation.
//! Every request is signed with the sender's Ed25519 key;
//! receivers verify before any processing occurs.

use serde::{Deserialize, Serialize};

// ── Protocol constants ─────────────────────────────────────────────────────

pub const AI_RPC_VERSION: u8 = 1;

/// Max prompt size: 32 KB. Prevents memory exhaustion on the receiving side.
pub const MAX_PROMPT_BYTES: usize = 32_768;

/// Max tokens per inference request.
pub const MAX_TOKENS_ALLOWED: u32 = 4_096;

/// Request expires after 30 s. Clocks of paired nodes should stay in sync;
/// 30 s gives ample room for network jitter while keeping the replay window tight.
pub const REQUEST_EXPIRY_MS: u64 = 30_000;

/// Nonce cache TTL: keep nonces for 2× expiry window so we catch late replays.
pub const NONCE_CACHE_TTL_MS: u64 = REQUEST_EXPIRY_MS * 2;

/// Packet type tag used to identify AI-RPC frames in the outer wagon stream.
pub const PKT_AI_RPC_REQUEST: u8 = 0xD0;
pub const PKT_AI_RPC_RESPONSE: u8 = 0xD1;
pub const PKT_AI_RPC_CHUNK: u8 = 0xD2;
pub const PKT_AI_RPC_ERROR: u8 = 0xD3;

// ── Methods ────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[repr(u8)]
pub enum RpcMethod {
    /// Liveness probe — no payload, instant pong.
    Ping = 0x01,
    /// Run LLM inference on the remote anchor (proxy → Ollama).
    AiInfer = 0x10,
    /// Fetch an HTTP resource via the remote anchor.
    Fetch = 0x20,
    /// Search the remote anchor's local knowledge base.
    KbSearch = 0x30,
    /// Store a Q&A synthesis entry into the knowledge base.
    KbStore = 0x31,
}

impl RpcMethod {
    pub fn from_byte(b: u8) -> Option<Self> {
        match b {
            0x01 => Some(Self::Ping),
            0x10 => Some(Self::AiInfer),
            0x20 => Some(Self::Fetch),
            0x30 => Some(Self::KbSearch),
            0x31 => Some(Self::KbStore),
            _ => None,
        }
    }

    pub fn to_byte(self) -> u8 {
        self as u8
    }
}

// ── Request payloads ───────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,    // "system" | "user" | "assistant"
    pub content: String,
}

/// Payload for `AiInfer`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AiInferPayload {
    /// Model name as Ollama understands it, e.g. `"deepseek-r1:14b"`.
    pub model: String,
    pub messages: Vec<ChatMessage>,
    /// Hard cap on generated tokens (capped server-side by `MAX_TOKENS_ALLOWED`).
    pub max_tokens: u32,
    /// Whether to stream chunks back.
    pub stream: bool,
    pub temperature: Option<f32>,
}

/// Payload for `Fetch`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FetchPayload {
    /// Target URL. HTTPS only; HTTP is rejected server-side.
    pub url: String,
    /// Optional headers forwarded to the upstream server.
    pub headers: Vec<(String, String)>,
}

/// Payload for `KbSearch`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KbSearchPayload {
    pub query: String,
    pub top_k: u8,
}

/// Payload for `KbStore` — gossip a Q&A entry to a peer.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KbStorePayload {
    pub question: String,
    pub synthesis: String,
    pub models: Vec<String>,
    pub domain: Option<String>,
}

// ── Envelope (signed wire frame) ───────────────────────────────────────────

/// Signed request envelope transmitted over the P2P transport.
///
/// **Signing canonical bytes** (in order, little-endian integers):
/// `version(1) || request_id(8) || nonce(16) || timestamp_ms(8) || method(1) || payload_bytes`
///
/// The signature covers everything; `sender` is **not** signed because the receiver
/// must look up the sender's public key from its peer store to verify — including the
/// sender field would create a chicken-and-egg problem.
///
/// Note: `signature` is stored as `Vec<u8>` (always 64 bytes) because serde does not
/// implement Serialize/Deserialize for `[u8; 64]` out of the box.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RpcEnvelope {
    pub version: u8,
    /// Monotonically increasing per-sender request counter for dedup.
    pub request_id: u64,
    /// 16-byte random nonce. Prevents replay even if clocks are perfectly synced.
    pub nonce: [u8; 16],
    /// Unix time in milliseconds at the moment of request creation.
    pub timestamp_ms: u64,
    /// Sender's node address (HashId bytes). Used to look up their signing pubkey.
    pub sender: [u8; 32],
    pub method: RpcMethod,
    /// bincode-encoded method-specific payload.
    pub payload: Vec<u8>,
    /// Ed25519 signature over canonical bytes (always 64 bytes).
    pub signature: Vec<u8>,
}

impl RpcEnvelope {
    /// Compute the byte slice that must be signed / verified.
    pub fn canonical_bytes(&self) -> Vec<u8> {
        let mut buf = Vec::with_capacity(34 + self.payload.len());
        buf.push(self.version);
        buf.extend_from_slice(&self.request_id.to_le_bytes());
        buf.extend_from_slice(&self.nonce);
        buf.extend_from_slice(&self.timestamp_ms.to_le_bytes());
        buf.push(self.method.to_byte());
        buf.extend_from_slice(&self.payload);
        buf
    }

    pub fn to_bytes(&self) -> Result<Vec<u8>, bincode::Error> {
        bincode::serialize(self)
    }

    pub fn from_bytes(data: &[u8]) -> Result<Self, bincode::Error> {
        bincode::deserialize(data)
    }
}

// ── Response ───────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RpcResponse {
    pub request_id: u64,
    pub status: RpcStatus,
    /// True when this is a streaming chunk (more to follow unless `chunk_done`).
    pub is_chunk: bool,
    /// True on the final chunk of a streaming response, or on a non-streaming response.
    pub chunk_done: bool,
    pub payload: Vec<u8>, // bincode-encoded RpcResponsePayload
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum RpcStatus {
    Ok,
    Err(RpcError),
}

#[derive(Debug, Clone, Serialize, Deserialize, thiserror::Error)]
pub enum RpcError {
    #[error("unauthorized: sender not in paired peers")]
    Unauthorized,
    #[error("rate limited: too many requests")]
    RateLimited,
    #[error("method not supported on this node")]
    MethodNotSupported,
    #[error("invalid payload: {0}")]
    InvalidPayload(String),
    #[error("request expired")]
    ExpiredRequest,
    #[error("replay detected")]
    ReplayDetected,
    #[error("version mismatch: got {0}, expected {1}")]
    VersionMismatch(u8, u8),
    #[error("payload too large: {0} bytes")]
    PayloadTooLarge(usize),
    #[error("backend error: {0}")]
    BackendError(String),
    #[error("upstream fetch failed: {0}")]
    FetchError(String),
}

/// Payload inside `RpcResponse.payload` for a successful AiInfer call.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AiInferResponse {
    /// Generated text (full response or one streaming chunk).
    pub content: String,
    /// Token count, if reported by the backend.
    pub tokens_used: Option<u32>,
}

/// Payload inside `RpcResponse.payload` for a successful Fetch call.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FetchResponse {
    pub status_code: u16,
    /// Response body (UTF-8). Truncated server-side at `MAX_FETCH_BYTES`.
    pub body: String,
    pub content_type: String,
}

/// Payload inside `RpcResponse.payload` for Pong.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PongResponse {
    pub echo_request_id: u64,
    pub server_time_ms: u64,
}

impl RpcResponse {
    pub fn to_bytes(&self) -> Result<Vec<u8>, bincode::Error> {
        bincode::serialize(self)
    }

    pub fn from_bytes(data: &[u8]) -> Result<Self, bincode::Error> {
        bincode::deserialize(data)
    }
}

// ── Local HTTP API types (PET ↔ YANDI-local) ──────────────────────────────
// These are **not** transmitted over P2P; they are the JSON API shapes
// exposed on localhost for PET integration.

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalInferRequest {
    pub model: Option<String>,
    pub messages: Vec<ChatMessage>,
    pub max_tokens: Option<u32>,
    pub stream: Option<bool>,
    pub temperature: Option<f32>,
    /// If set, forward the request to this remote peer instead of local Ollama.
    pub remote_peer: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalInferResponse {
    pub content: String,
    pub model: String,
    pub tokens_used: Option<u32>,
    pub via: String, // "local" | "remote:<peer_id>"
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalFetchRequest {
    pub url: String,
    pub headers: Option<Vec<(String, String)>>,
    /// Forward to remote anchor instead of fetching locally.
    pub remote_peer: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AiRpcStatus {
    pub enabled: bool,
    pub ollama_url: String,
    pub ollama_reachable: bool,
    pub allowed_peers: usize,
    pub requests_served: u64,
    pub errors_total: u64,
}

// ── Knowledge base types ──────────────────────────────────────────────────

/// One entry in the local knowledge base (question + council synthesis).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KbEntry {
    pub id: String,
    pub question: String,
    pub synthesis: String,
    pub models: Vec<String>,
    pub domain: Option<String>,
    pub timestamp_ms: u64,
}

/// HTTP request body for POST /api/ai-rpc/knowledge/store
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalKbStoreRequest {
    pub question: String,
    pub synthesis: String,
    pub models: Vec<String>,
    pub domain: Option<String>,
}

/// HTTP request body for POST /api/ai-rpc/knowledge/search
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocalKbSearchRequest {
    pub query: String,
    pub top_k: Option<usize>,
}

/// Response for knowledge/store and knowledge/search
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KbSearchResponse {
    pub entries: Vec<KbEntry>,
    pub total: usize,
}
