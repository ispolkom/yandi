// src/ai_rpc/peer_directory.rs
//! Trusted AI-RPC Peer Directory
//! =============================
//!
//! Fixes a real identity bug found while auditing this mandate
//! ("Real Node Directory Integration"): the previous mandate's AI-RPC
//! allowlist was populated from `PairedClientStore` (anchor↔mobile-CLIENT
//! pairing — a different relationship entirely) using the client's
//! Ed25519 signing public key for BOTH `AllowedPeer::address` and
//! `AllowedPeer::signing_pubkey`. But the real P2P transport
//! (`P2PTransport::send_encrypted`, `get_peers()`, `PeerInfo::id`) routes
//! packets by `node_id()` (`HashId`) — an independent, unverified-but-
//! stable value learned via the Hello handshake, NOT the signing pubkey.
//! On a genuine two-machine network those two value spaces never
//! coincide, so `AllowedPeer::address` (used to look a sender up) would
//! never match a real peer's actual `sender` (their `node_id()`), and
//! `send_encrypted` would never find a route back to them either.
//!
//! `node_id()` is what this whole codebase already treats as "the"
//! address of a node everywhere routing happens — `PeerInfo`,
//! `get_peers()`/`get_peers_map()`, `PairingPayload::anchor_id`, RESUME.
//! It is the canonical node identity for this migration (see the final
//! report's "CANONICAL NODE ID" section for the full reasoning — in
//! short: it is the one identity space the live transport already
//! natively operates in, and it is stable across IP/route changes for a
//! given identity file). The Ed25519 signing public key remains exactly
//! what it always was: the cryptographic credential used to verify a
//! request really came from that node.
//!
//! A trusted AI-RPC peer is therefore BOTH values together — one to
//! route by, one to verify by — recorded once, explicitly, by the node
//! owner (out-of-band, e.g. exchanged with the other node's owner),
//! exactly as manual as the existing `PairedClientStore` pairing act.
//! This is deliberately NOT a new cryptographic design: it is a plain,
//! unencrypted JSON record of two already-public values (a node_id and
//! a public key are not secrets), stored the same way
//! `paired_clients.json` already is.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::util::HashId;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TrustedAiPeer {
    pub node_id_hex: String,
    pub signing_pubkey_hex: String,
    #[serde(default)]
    pub name: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PeerDirectory {
    pub peers: Vec<TrustedAiPeer>,
}

impl PeerDirectory {
    pub fn load_or_default(path: &Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(s) => serde_json::from_str(&s).unwrap_or_default(),
            Err(_) => Self::default(),
        }
    }

    /// Decode every entry to `(HashId, [u8; 32] signing pubkey, name)`,
    /// skipping (and logging) any malformed row rather than failing the
    /// whole directory over one bad entry.
    pub fn decoded(&self) -> Vec<(HashId, [u8; 32], Option<String>)> {
        let mut out = Vec::with_capacity(self.peers.len());
        for p in &self.peers {
            let node_id = match hex::decode(p.node_id_hex.trim()) {
                Ok(b) if b.len() == 32 => HashId(b.try_into().unwrap()),
                _ => {
                    eprintln!("[peer_directory] skipping entry with malformed node_id_hex: {}", p.node_id_hex);
                    continue;
                }
            };
            let pubkey = match hex::decode(p.signing_pubkey_hex.trim()) {
                Ok(b) if b.len() == 32 => {
                    let mut arr = [0u8; 32];
                    arr.copy_from_slice(&b);
                    arr
                }
                _ => {
                    eprintln!("[peer_directory] skipping entry with malformed signing_pubkey_hex: {}", p.signing_pubkey_hex);
                    continue;
                }
            };
            out.push((node_id, pubkey, p.name.clone()));
        }
        out
    }

    pub fn name_for(&self, node_id: &HashId) -> Option<String> {
        let target = node_id.to_hex();
        self.peers.iter().find(|p| p.node_id_hex.eq_ignore_ascii_case(&target)).and_then(|p| p.name.clone())
    }
}

pub fn default_peer_directory_path() -> PathBuf {
    dirs_home().join(".yandi").join("trusted_ai_peers.json")
}

fn dirs_home() -> PathBuf {
    std::env::var_os("YANDI_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("."))
}

/// One row of `GET /api/ai-rpc/peers` — the minimal, safe surface Python
/// needs to pick a node: identity, a human label, and whether the live
/// transport currently has a connection to it. Deliberately excludes
/// anything about what backend/model that node runs (unknown to this
/// node anyway) and anything transport-internal (addresses, keys,
/// session state).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PeerDirectoryEntry {
    pub node_id: String,
    pub name: Option<String>,
    pub trusted: bool,
    pub online: bool,
}

/// Build the directory response by cross-referencing the trusted-peer
/// list against a live peer map. Callers should build `live_peers` from
/// `P2PTransport::get_peers()` (which already filters to
/// `PEER_ONLINE_TIMEOUT_SECS`-recent peers), not `get_peers_map()`
/// (unfiltered) — both keyed by the same `node_id()`/`HashId` space,
/// which is exactly the identity space this module fixes AI-RPC to use.
pub fn build_directory_response(
    directory: &PeerDirectory,
    live_peers: &HashMap<HashId, crate::netlayer::peer::PeerInfo>,
) -> Vec<PeerDirectoryEntry> {
    directory
        .decoded()
        .into_iter()
        .map(|(node_id, _pubkey, name)| PeerDirectoryEntry {
            node_id: node_id.to_hex(),
            name,
            trusted: true,
            online: live_peers.contains_key(&node_id),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn peer(node_id_hex: &str, pubkey_hex: &str, name: Option<&str>) -> TrustedAiPeer {
        TrustedAiPeer {
            node_id_hex: node_id_hex.to_string(),
            signing_pubkey_hex: pubkey_hex.to_string(),
            name: name.map(str::to_string),
        }
    }

    #[test]
    fn decode_skips_malformed_entries_without_dropping_valid_ones() {
        let dir = PeerDirectory {
            peers: vec![
                peer(&"aa".repeat(32), &"bb".repeat(32), Some("good")),
                peer("not-hex-at-all", &"cc".repeat(32), Some("bad-node-id")),
                peer(&"dd".repeat(32), "short", Some("bad-pubkey")),
            ],
        };
        let decoded = dir.decoded();
        assert_eq!(decoded.len(), 1, "only the well-formed entry should survive");
        assert_eq!(decoded[0].2.as_deref(), Some("good"));
    }

    #[test]
    fn node_id_and_signing_pubkey_are_independent_values() {
        // The whole point of this module: node_id (routing identity) and
        // signing_pubkey (verification credential) must NOT be required
        // to be equal — that conflation was the actual bug this fixes.
        let node_id_hex = "11".repeat(32);
        let pubkey_hex = "22".repeat(32); // deliberately DIFFERENT
        let dir = PeerDirectory { peers: vec![peer(&node_id_hex, &pubkey_hex, None)] };
        let decoded = dir.decoded();
        assert_eq!(decoded.len(), 1);
        let (node_id, pubkey, _) = &decoded[0];
        assert_eq!(node_id.to_hex(), node_id_hex);
        assert_eq!(hex::encode(pubkey), pubkey_hex);
        assert_ne!(node_id.0, *pubkey, "node_id and pubkey must be free to differ");
    }

    #[test]
    fn build_directory_response_reports_online_only_for_live_peers() {
        let node_a = "aa".repeat(32);
        let node_b = "bb".repeat(32);
        let dir = PeerDirectory {
            peers: vec![
                peer(&node_a, &"11".repeat(32), Some("A")),
                peer(&node_b, &"22".repeat(32), Some("B")),
            ],
        };
        let mut live: HashMap<HashId, crate::netlayer::peer::PeerInfo> = HashMap::new();
        let id_a = HashId(hex::decode(&node_a).unwrap().try_into().unwrap());
        live.insert(id_a, crate::netlayer::peer::PeerInfo::new(id_a, "1.2.3.4:9000"));

        let entries = build_directory_response(&dir, &live);
        assert_eq!(entries.len(), 2);
        let a = entries.iter().find(|e| e.node_id == node_a).unwrap();
        let b = entries.iter().find(|e| e.node_id == node_b).unwrap();
        assert!(a.online, "A is in the live peer map — must report online");
        assert!(!b.online, "B is NOT in the live peer map — must report offline, never assumed online");
        assert!(a.trusted && b.trusted);
    }

    #[test]
    fn no_peers_configured_yields_empty_directory_not_a_fake_entry() {
        let dir = PeerDirectory::default();
        let entries = build_directory_response(&dir, &HashMap::new());
        assert!(entries.is_empty(), "an empty trusted-peer file must yield an empty list, never a fabricated localhost entry");
    }
}
