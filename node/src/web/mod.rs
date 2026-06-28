// src/web/mod.rs
//!
//! # YANDI Web UI
//!
//! Локальный веб-сервер для управления нодой

pub mod server;
pub mod api;
pub mod auth;
pub mod ai_rpc_server;

pub use server::{WebServer, NodeInfo};
pub use ai_rpc_server::{run as run_ai_rpc_server, DEFAULT_AI_RPC_PORT};

pub mod media_api;
