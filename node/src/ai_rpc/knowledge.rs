// src/ai_rpc/knowledge.rs
//! Local Knowledge Base
//! ====================
//!
//! Stores Q&A synthesis entries produced by the PET council.
//! Simple in-memory store with text search; persists to JSON on disk.

use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tracing::{info, warn};
use uuid::Uuid;

use crate::ai_rpc::types::{KbEntry, KbSearchResponse};

const MAX_ENTRIES: usize = 10_000;
const PERSIST_FILE: &str = "data/knowledge_base.json";

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct KnowledgeBase {
    entries: Vec<KbEntry>,
}

impl KnowledgeBase {
    pub fn new() -> Self {
        Self::load_from_disk().unwrap_or_default()
    }

    fn data_path() -> PathBuf {
        PathBuf::from(PERSIST_FILE)
    }

    fn load_from_disk() -> Option<Self> {
        let path = Self::data_path();
        if !path.exists() {
            return None;
        }
        match std::fs::read_to_string(&path) {
            Ok(s) => match serde_json::from_str(&s) {
                Ok(kb) => {
                    let kb: KnowledgeBase = kb;
                    info!("knowledge_base: loaded {} entries from disk", kb.entries.len());
                    Some(kb)
                }
                Err(e) => {
                    warn!("knowledge_base: failed to parse {}: {e}", path.display());
                    None
                }
            },
            Err(_) => None,
        }
    }

    fn save_to_disk(&self) {
        let path = Self::data_path();
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        match serde_json::to_string_pretty(self) {
            Ok(s) => {
                if let Err(e) = std::fs::write(&path, s) {
                    warn!("knowledge_base: failed to save: {e}");
                }
            }
            Err(e) => warn!("knowledge_base: serialisation error: {e}"),
        }
    }

    /// Store a new entry. Returns the assigned ID.
    pub fn store(
        &mut self,
        question: String,
        synthesis: String,
        models: Vec<String>,
        domain: Option<String>,
    ) -> String {
        let id = Uuid::new_v4().to_string();
        let timestamp_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64;

        let entry = KbEntry {
            id: id.clone(),
            question,
            synthesis,
            models,
            domain,
            timestamp_ms,
        };

        // Evict oldest if over limit
        if self.entries.len() >= MAX_ENTRIES {
            self.entries.remove(0);
        }

        info!(
            "knowledge_base: stored entry {} (total {})",
            &id[..8],
            self.entries.len() + 1
        );
        self.entries.push(entry);
        self.save_to_disk();
        id
    }

    /// Simple text search across question + synthesis.
    pub fn search(&self, query: &str, top_k: usize) -> KbSearchResponse {
        let q = query.to_lowercase();
        let matched: Vec<KbEntry> = self
            .entries
            .iter()
            .filter(|e| {
                e.question.to_lowercase().contains(&q)
                    || e.synthesis.to_lowercase().contains(&q)
                    || e.domain
                        .as_deref()
                        .map(|d| d.to_lowercase().contains(&q))
                        .unwrap_or(false)
            })
            // newest first
            .rev()
            .take(top_k)
            .cloned()
            .collect();

        let total = matched.len();
        KbSearchResponse { entries: matched, total }
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }
}
