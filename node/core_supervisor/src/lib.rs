//! Owns the lifecycle of the YANDI Core process for the node (contract 1.0-rc1, P1b).
//!
//! ```text
//! Node ── spawns ──▶ Core (separate process, 127.0.0.1 only, locked)
//!  │                   ▲
//!  ├─ fresh launch secret in a 0600 file (path in the environment, never argv)
//!  ├─ waits for health = locked        (only from the port its own child bound)
//!  ├─ derives HKDF(master_key, "yandi/core/v1") and POSTs /v1/unlock
//!  ├─ waits for health = ready, watches the child
//!  ├─ crash → new process, NEW secret, unlock again, bounded exponential backoff, a ceiling
//!  └─ shutdown → /v1/shutdown, then TERM, then KILL — only the child it owns
//! ```
//!
//! The master key never enters the Core; only the derived key does. A Core crash never takes the node down: every failure
//! is a value (`FailureCategory`) the node can show, never a panic and never a Python traceback.

#![cfg(unix)] // Linux/Unix only for now: process groups, PDEATHSIG and /proc. Windows and macOS need their own launcher.

pub mod client;
pub mod config;
pub mod keys;
pub mod runtime;
pub mod supervisor;

pub use client::{ClientError, CoreClient, HealthState};
pub use config::{BackoffPolicy, SupervisorConfig};
pub use keys::{derive_core_key, CoreKey, CORE_CONTEXT};
pub use runtime::{LaunchDir, LaunchSecret, RuntimeBase};
pub use supervisor::{
    CoreStatus, CoreSupervisor, Event, FailureCategory, KeyProvider, Phase, ShutdownReport,
};
pub use zeroize::Zeroizing;
