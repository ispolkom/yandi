// src/web/ai_rpc_server.rs
//! AI-RPC Local HTTP Server
//! =========================
//!
//! Runs on a dedicated port (default 18082) on localhost only.
//! Exposes a JSON API for PET and other local consumers.
//!
//! **This server is NOT protected by the main auth middleware** — it
//! binds only to `127.0.0.1`, making it inaccessible from the network.
//!
//! Endpoints:
//!   GET  /api/ai-rpc/status       — liveness + counters
//!   GET  /api/ai-rpc/peers        — trusted AI-RPC peers + live online status
//!   POST /api/ai-rpc/infer        — one-shot LLM inference (local, or a
//!                                   trusted peer via remote_peer=node_id)
//!   POST /api/ai-rpc/fetch        — HTTPS fetch proxied through this anchor
//!
//! All endpoints return `application/json`.

use std::sync::Arc;

use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Json},
    routing::{get, post},
    Router,
};
use serde_json::json;
use tokio::sync::Mutex;
use tracing::{error, info};

use crate::ai_rpc::{
    peer_directory::build_directory_response, AiRpcService, LocalFetchRequest, LocalInferRequest,
    LocalKbStoreRequest, LocalKbSearchRequest,
};
use crate::netlayer::transport::P2PTransport;

pub const DEFAULT_AI_RPC_PORT: u16 = 18082;

// ── Shared state ───────────────────────────────────────────────────────────

#[derive(Clone)]
pub struct AppState {
    pub svc: Arc<Mutex<AiRpcService>>,
    /// Real Node Directory Integration: needed ONLY to answer
    /// `GET /api/ai-rpc/peers` with live online status
    /// (`P2PTransport::get_peers()`). Nothing else on this local surface
    /// touches transport internals — Python still never sees them.
    pub transport: Arc<P2PTransport>,
}

// ── Server ─────────────────────────────────────────────────────────────────

/// Start the AI-RPC local HTTP server.
/// Binds to `127.0.0.1:<port>` only.
pub async fn run(service: Arc<Mutex<AiRpcService>>, transport: Arc<P2PTransport>, port: u16) -> Result<(), String> {
    let addr = format!("127.0.0.1:{port}");
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| format!("ai_rpc_server: bind {addr} failed: {e}"))?;

    info!("ai_rpc: local API listening on http://{addr}");

    let app = build_router(AppState { svc: service, transport });

    axum::serve(listener, app)
        .await
        .map_err(|e| format!("ai_rpc_server: serve error: {e}"))
}

fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/api/ai-rpc/status", get(handle_status))
        .route("/api/ai-rpc/peers", get(handle_peers))
        .route("/api/ai-rpc/infer", post(handle_infer))
        .route("/api/ai-rpc/fetch", post(handle_fetch))
        .route("/api/ai-rpc/knowledge/store", post(handle_kb_store))
        .route("/api/ai-rpc/knowledge/search", post(handle_kb_search))
        .merge(gateway_router())
        .with_state(state)
}

/// Единый шлюз к моделям для ядра на Python: настройки владельца → собственный движок узла (loopback, как и весь этот сервер).
pub fn gateway_router<S: Clone + Send + Sync + 'static>() -> Router<S> {
    Router::new().route("/api/gateway/call", post(handle_gateway_call).layer(axum::extract::DefaultBodyLimit::max(16 * 1024 * 1024)))
}

// ── Handlers ───────────────────────────────────────────────────────────────

#[derive(serde::Deserialize)]
struct GatewayCallRequest {
    name: String,
    #[serde(default)]
    args: serde_json::Value,
}

async fn handle_gateway_call(Json(req): Json<GatewayCallRequest>) -> impl IntoResponse {
    let (status, body) = crate::ai_rpc::intelligence_bridge::gateway_call(req.name, req.args).await;
    (StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR), Json(body))
}

async fn handle_status(State(state): State<AppState>) -> impl IntoResponse {
    let status = state.svc.lock().await.status().await;
    Json(status)
}

/// Real Node Directory Integration: the minimal, safe interface Python
/// needs to choose a real node — canonical node_id, an optional label,
/// and whether the live transport currently has a connection to it.
/// Never includes network addresses, keys, or anything about what
/// backend/model that node runs.
async fn handle_peers(State(state): State<AppState>) -> impl IntoResponse {
    let directory = state.svc.lock().await.peer_directory();
    let live_peers: std::collections::HashMap<_, _> = state
        .transport
        .get_peers()
        .await
        .into_iter()
        .map(|p| (p.id, p))
        .collect();
    let entries = build_directory_response(&directory, &live_peers);
    Json(json!({ "peers": entries }))
}

async fn handle_infer(
    State(state): State<AppState>,
    Json(req): Json<LocalInferRequest>,
) -> impl IntoResponse {
    match state.svc.lock().await.local_infer(req).await {
        Ok(resp) => (StatusCode::OK, Json(json!(resp))),
        Err(e) => {
            error!("ai_rpc/infer: {e}");
            let code = error_status(&e);
            (code, Json(json!({ "error": e.to_string() })))
        }
    }
}

async fn handle_fetch(
    State(state): State<AppState>,
    Json(req): Json<LocalFetchRequest>,
) -> impl IntoResponse {
    match state.svc.lock().await.local_fetch(req).await {
        Ok(resp) => (StatusCode::OK, Json(json!(resp))),
        Err(e) => {
            error!("ai_rpc/fetch: {e}");
            let code = error_status(&e);
            (code, Json(json!({ "error": e.to_string() })))
        }
    }
}

async fn handle_kb_store(
    State(state): State<AppState>,
    Json(req): Json<LocalKbStoreRequest>,
) -> impl IntoResponse {
    let id = state.svc.lock().await.kb_store(req).await;
    (StatusCode::OK, Json(json!({ "ok": true, "id": id })))
}

async fn handle_kb_search(
    State(state): State<AppState>,
    Json(req): Json<LocalKbSearchRequest>,
) -> impl IntoResponse {
    let resp = state.svc.lock().await.kb_search(req).await;
    (StatusCode::OK, Json(json!(resp)))
}

// ── Helpers ────────────────────────────────────────────────────────────────

fn error_status(e: &crate::ai_rpc::RpcError) -> StatusCode {
    use crate::ai_rpc::RpcError;
    match e {
        RpcError::Unauthorized => StatusCode::UNAUTHORIZED,
        RpcError::RateLimited => StatusCode::TOO_MANY_REQUESTS,
        RpcError::PayloadTooLarge(_) => StatusCode::PAYLOAD_TOO_LARGE,
        RpcError::InvalidPayload(_) => StatusCode::BAD_REQUEST,
        _ => StatusCode::INTERNAL_SERVER_ERROR,
    }
}
