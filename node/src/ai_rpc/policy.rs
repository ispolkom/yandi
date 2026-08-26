// src/ai_rpc/policy.rs
//! Trust Policy & Request Validation
//! ==================================
//!
//! Three-layer defence before any RPC payload reaches a handler:
//!
//! 1. **Allowlist** — sender must be in `paired_clients` (or explicitly added).
//! 2. **Rate limiter** — per-peer sliding-window token bucket.
//! 3. **Replay guard** — nonce + timestamp window; seen nonces are cached.
//!
//! Validation is intentionally fail-fast: the earliest cheap check runs first.

use std::collections::{HashMap, VecDeque};
use std::time::{SystemTime, UNIX_EPOCH};

use ed25519_dalek::{Signature, VerifyingKey};
use serde::{Deserialize, Serialize};
use tracing::{debug, warn};

use super::types::{
    RpcEnvelope, RpcError, AI_RPC_VERSION, NONCE_CACHE_TTL_MS, REQUEST_EXPIRY_MS,
};

// ── Rate limiter config ────────────────────────────────────────────────────

/// Default: 60 requests per minute per peer.
const DEFAULT_RPM: u32 = 60;

/// Bucket refill window in milliseconds (1 minute).
const BUCKET_WINDOW_MS: u64 = 60_000;

// ── Per-peer rate bucket ───────────────────────────────────────────────────

struct RateBucket {
    count: u32,
    window_start_ms: u64,
    limit: u32,
}

impl RateBucket {
    fn new(limit: u32) -> Self {
        Self {
            count: 0,
            window_start_ms: now_ms(),
            limit,
        }
    }

    /// Returns `true` if the request is allowed (and consumes one token).
    fn allow(&mut self) -> bool {
        let now = now_ms();
        if now.saturating_sub(self.window_start_ms) >= BUCKET_WINDOW_MS {
            self.count = 0;
            self.window_start_ms = now;
        }
        if self.count < self.limit {
            self.count += 1;
            true
        } else {
            false
        }
    }
}

// ── Nonce cache ────────────────────────────────────────────────────────────

/// Ring buffer of (nonce, expire_ms) pairs. Evicts expired entries lazily.
struct NonceCache {
    entries: VecDeque<([u8; 16], u64)>,
}

impl NonceCache {
    fn new() -> Self {
        Self { entries: VecDeque::new() }
    }

    /// Returns `true` if the nonce has NOT been seen before (and records it).
    /// Returns `false` (replay!) if the nonce was already recorded.
    fn check_and_insert(&mut self, nonce: [u8; 16]) -> bool {
        let now = now_ms();
        let expire_at = now + NONCE_CACHE_TTL_MS;

        // Evict expired entries from the front.
        while let Some(&(_, exp)) = self.entries.front() {
            if exp <= now {
                self.entries.pop_front();
            } else {
                break;
            }
        }

        // Check if nonce already present.
        if self.entries.iter().any(|(n, _)| n == &nonce) {
            return false;
        }

        self.entries.push_back((nonce, expire_at));
        true
    }
}

// ── Allowed peer entry ─────────────────────────────────────────────────────

/// An entry in the allowlist: maps node address → Ed25519 signing public key.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AllowedPeer {
    /// 32-byte node address (same as HashId).
    pub address: [u8; 32],
    /// Ed25519 verifying key bytes (32 bytes).
    pub signing_pubkey: [u8; 32],
    /// Optional display name.
    pub name: Option<String>,
    /// Optional per-peer RPM override (falls back to `DEFAULT_RPM`).
    pub rpm_limit: Option<u32>,
}

// ── TrustPolicy ────────────────────────────────────────────────────────────

pub struct TrustPolicy {
    /// address → (verifying_key, rpm_limit)
    peers: HashMap<[u8; 32], (VerifyingKey, u32)>,
    rate_buckets: HashMap<[u8; 32], RateBucket>,
    nonces: NonceCache,
}

impl TrustPolicy {
    pub fn new() -> Self {
        Self {
            peers: HashMap::new(),
            rate_buckets: HashMap::new(),
            nonces: NonceCache::new(),
        }
    }

    /// Register a trusted peer. Can be called at startup and on pairing events.
    pub fn add_peer(&mut self, peer: AllowedPeer) -> Result<(), String> {
        let vk = VerifyingKey::from_bytes(&peer.signing_pubkey)
            .map_err(|e| format!("invalid signing pubkey for peer: {e}"))?;
        let rpm = peer.rpm_limit.unwrap_or(DEFAULT_RPM);
        self.peers.insert(peer.address, (vk, rpm));
        debug!(
            "ai_rpc: registered peer {:?} rpm={}",
            hex::encode(&peer.address[..8]),
            rpm
        );
        Ok(())
    }

    /// Remove a peer (e.g. on unpairing).
    pub fn remove_peer(&mut self, address: &[u8; 32]) {
        self.peers.remove(address);
        self.rate_buckets.remove(address);
    }

    /// Full validation pipeline. Returns `Ok(())` or the first error encountered.
    ///
    /// Order (cheapest first):
    /// 1. Version check
    /// 2. Timestamp expiry
    /// 3. Sender in allowlist
    /// 4. Rate limit
    /// 5. Nonce uniqueness
    /// 6. Signature verification
    pub fn validate(&mut self, env: &RpcEnvelope) -> Result<(), RpcError> {
        // 1. Version
        if env.version != AI_RPC_VERSION {
            return Err(RpcError::VersionMismatch(env.version, AI_RPC_VERSION));
        }

        // 2. Timestamp window
        let now = now_ms();
        let age = now.saturating_sub(env.timestamp_ms);
        if age > REQUEST_EXPIRY_MS || env.timestamp_ms > now + REQUEST_EXPIRY_MS {
            warn!("ai_rpc: expired request id={} age={}ms", env.request_id, age);
            return Err(RpcError::ExpiredRequest);
        }

        // 3. Allowlist
        let addr = env.sender;
        let (vk, rpm) = self
            .peers
            .get(&addr)
            .ok_or_else(|| {
                warn!("ai_rpc: unauthorized sender {}", hex::encode(&addr[..8]));
                RpcError::Unauthorized
            })?;
        let (vk, rpm) = (vk.clone(), *rpm);

        // 4. Rate limit
        let bucket = self
            .rate_buckets
            .entry(addr)
            .or_insert_with(|| RateBucket::new(rpm));
        if !bucket.allow() {
            warn!("ai_rpc: rate limit hit for peer {}", hex::encode(&addr[..8]));
            return Err(RpcError::RateLimited);
        }

        // 5. Nonce uniqueness
        if !self.nonces.check_and_insert(env.nonce) {
            warn!("ai_rpc: replay detected nonce {:?}", hex::encode(&env.nonce));
            return Err(RpcError::ReplayDetected);
        }

        // 6. Signature
        let canonical = env.canonical_bytes();
        let sig_bytes: [u8; 64] = env.signature.as_slice().try_into().map_err(|_| {
            warn!("ai_rpc: malformed signature length={}", env.signature.len());
            RpcError::Unauthorized
        })?;
        let sig = Signature::from_bytes(&sig_bytes);
        vk.verify_strict(&canonical, &sig).map_err(|e| {
            warn!(
                "ai_rpc: signature invalid for peer {}: {e}",
                hex::encode(&addr[..8])
            );
            RpcError::Unauthorized
        })?;

        Ok(())
    }

    pub fn peer_count(&self) -> usize {
        self.peers.len()
    }
}

impl Default for TrustPolicy {
    fn default() -> Self {
        Self::new()
    }
}

// ── Request builder helpers ────────────────────────────────────────────────

/// Sign an `RpcEnvelope`.  The `signature` field is set to the 64-byte Ed25519 signature.
pub fn sign_envelope(
    env: &mut RpcEnvelope,
    signing_key: &ed25519_dalek::SigningKey,
) {
    use ed25519_dalek::Signer;
    let canonical = env.canonical_bytes();
    let sig = signing_key.sign(&canonical);
    env.signature = sig.to_bytes().to_vec();
}

// ── Utilities ──────────────────────────────────────────────────────────────

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

// ── Tests ──────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::SigningKey;
    use rand::rngs::OsRng;

    use crate::ai_rpc::types::{RpcMethod, AiInferPayload, ChatMessage};

    fn make_keypair() -> (SigningKey, [u8; 32]) {
        let sk = SigningKey::generate(&mut OsRng);
        let vk_bytes = sk.verifying_key().to_bytes();
        (sk, vk_bytes)
    }

    fn make_envelope(
        sk: &SigningKey,
        sender: [u8; 32],
        method: RpcMethod,
        payload: Vec<u8>,
    ) -> RpcEnvelope {
        use rand::RngCore;
        let mut nonce = [0u8; 16];
        OsRng.fill_bytes(&mut nonce);

        let mut env = RpcEnvelope {
            version: AI_RPC_VERSION,
            request_id: 1,
            nonce,
            timestamp_ms: now_ms(),
            sender,
            method,
            payload,
            signature: vec![0u8; 64],
        };
        sign_envelope(&mut env, sk);
        env
    }

    fn dummy_payload() -> Vec<u8> {
        let p = AiInferPayload {
            model: "test".into(),
            messages: vec![ChatMessage { role: "user".into(), content: "hi".into() }],
            max_tokens: 100,
            stream: false,
            temperature: None,
        };
        bincode::serialize(&p).unwrap()
    }

    fn policy_with_peer(addr: [u8; 32], vk: [u8; 32]) -> TrustPolicy {
        let mut policy = TrustPolicy::new();
        policy.add_peer(AllowedPeer {
            address: addr,
            signing_pubkey: vk,
            name: None,
            rpm_limit: None,
        }).unwrap();
        policy
    }

    #[test]
    fn valid_request_passes() {
        let (sk, vk_bytes) = make_keypair();
        let addr = [0xABu8; 32];
        let mut policy = policy_with_peer(addr, vk_bytes);

        let env = make_envelope(&sk, addr, RpcMethod::AiInfer, dummy_payload());
        assert!(policy.validate(&env).is_ok());
    }

    #[test]
    fn unknown_sender_rejected() {
        let (sk, _vk) = make_keypair();
        let addr = [0x01u8; 32];
        let mut policy = TrustPolicy::new(); // empty allowlist

        let env = make_envelope(&sk, addr, RpcMethod::Ping, vec![]);
        assert!(matches!(policy.validate(&env), Err(RpcError::Unauthorized)));
    }

    #[test]
    fn tampered_payload_rejected() {
        let (sk, vk_bytes) = make_keypair();
        let addr = [0x02u8; 32];
        let mut policy = policy_with_peer(addr, vk_bytes);

        let mut env = make_envelope(&sk, addr, RpcMethod::AiInfer, dummy_payload());
        // Tamper after signing
        if let Some(b) = env.payload.first_mut() { *b ^= 0xFF; }

        assert!(matches!(policy.validate(&env), Err(RpcError::Unauthorized)));
    }

    #[test]
    fn replay_rejected() {
        let (sk, vk_bytes) = make_keypair();
        let addr = [0x03u8; 32];
        let mut policy = policy_with_peer(addr, vk_bytes);

        let env = make_envelope(&sk, addr, RpcMethod::Ping, vec![]);
        assert!(policy.validate(&env).is_ok());
        // Second use of the same nonce
        assert!(matches!(policy.validate(&env), Err(RpcError::ReplayDetected)));
    }

    #[test]
    fn expired_request_rejected() {
        let (sk, vk_bytes) = make_keypair();
        let addr = [0x04u8; 32];
        let mut policy = policy_with_peer(addr, vk_bytes);

        let mut env = make_envelope(&sk, addr, RpcMethod::Ping, vec![]);
        // Wind timestamp back beyond expiry window
        env.timestamp_ms = now_ms().saturating_sub(REQUEST_EXPIRY_MS + 1_000);
        // Re-sign with the backdated timestamp
        env.signature = vec![0u8; 64];
        sign_envelope(&mut env, &sk);

        assert!(matches!(policy.validate(&env), Err(RpcError::ExpiredRequest)));
    }

    #[test]
    fn rate_limit_enforced() {
        let (sk, vk_bytes) = make_keypair();
        let addr = [0x05u8; 32];
        let mut policy = TrustPolicy::new();
        policy.add_peer(AllowedPeer {
            address: addr,
            signing_pubkey: vk_bytes,
            name: None,
            rpm_limit: Some(2), // very tight limit for testing
        }).unwrap();

        for _ in 0..2 {
            let env = make_envelope(&sk, addr, RpcMethod::Ping, vec![]);
            assert!(policy.validate(&env).is_ok());
        }
        // Third request in the same window should be rate-limited
        let env = make_envelope(&sk, addr, RpcMethod::Ping, vec![]);
        assert!(matches!(policy.validate(&env), Err(RpcError::RateLimited)));
    }
}
