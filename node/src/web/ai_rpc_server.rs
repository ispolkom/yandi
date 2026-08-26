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
//!   POST /api/ai-rpc/infer        — one-shot LLM inference via local Ollama
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
    AiRpcService, LocalFetchRequest, LocalInferRequest,
    LocalKbStoreRequest, LocalKbSearchRequest,
};

pub const DEFAULT_AI_RPC_PORT: u16 = 18082;

// ── Shared state ───────────────────────────────────────────────────────────

type SharedService = Arc<Mutex<AiRpcService>>;

// ── Server ─────────────────────────────────────────────────────────────────

/// Start the AI-RPC local HTTP server.
/// Binds to `127.0.0.1:<port>` only.
pub async fn run(service: SharedService, port: u16) -> Result<(), String> {
    let addr = format!("127.0.0.1:{port}");
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .map_err(|e| format!("ai_rpc_server: bind {addr} failed: {e}"))?;

    info!("ai_rpc: local API listening on http://{addr}");

    let app = build_router(service);

    axum::serve(listener, app)
        .await
        .map_err(|e| format!("ai_rpc_server: serve error: {e}"))
}

fn build_router(service: SharedService) -> Router {
    Router::new()
        .route("/api/ai-rpc/status", get(handle_status))
        .route("/api/ai-rpc/infer", post(handle_infer))
        .route("/api/ai-rpc/fetch", post(handle_fetch))
        .route("/api/ai-rpc/knowledge/store", post(handle_kb_store))
        .route("/api/ai-rpc/knowledge/search", post(handle_kb_search))
        .with_state(service)
}

// ── Handlers ───────────────────────────────────────────────────────────────

async fn handle_status(State(svc): State<SharedService>) -> impl IntoResponse {
    let status = svc.lock().await.status().await;
    Json(status)
}

async fn handle_infer(
    State(svc): State<SharedService>,
    Json(req): Json<LocalInferRequest>,
) -> impl IntoResponse {
    match svc.lock().await.local_infer(req).await {
        Ok(resp) => (StatusCode::OK, Json(json!(resp))),
        Err(e) => {
            error!("ai_rpc/infer: {e}");
            let code = error_status(&e);
            (code, Json(json!({ "error": e.to_string() })))
        }
    }
}

async fn handle_fetch(
    State(svc): State<SharedService>,
    Json(req): Json<LocalFetchRequest>,
) -> impl IntoResponse {
    match svc.lock().await.local_fetch(req).await {
        Ok(resp) => (StatusCode::OK, Json(json!(resp))),
        Err(e) => {
            error!("ai_rpc/fetch: {e}");
            let code = error_status(&e);
            (code, Json(json!({ "error": e.to_string() })))
        }
    }
}

async fn handle_kb_store(
    State(svc): State<SharedService>,
    Json(req): Json<LocalKbStoreRequest>,
) -> impl IntoResponse {
    let id = svc.lock().await.kb_store(req).await;
    (StatusCode::OK, Json(json!({ "ok": true, "id": id })))
}

async fn handle_kb_search(
    State(svc): State<SharedService>,
    Json(req): Json<LocalKbSearchRequest>,
) -> impl IntoResponse {
    let resp = svc.lock().await.kb_search(req).await;
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
