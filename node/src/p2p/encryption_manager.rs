// src/netlayer/encryption.rs
//! Session Encryption
//! ===================
//!
//! ECDH key exchange and AES-256-GCM encryption for peer sessions

use crate::util::HashId;
use std::collections::HashMap;
use aes_gcm::{
    aead::{Aead, KeyInit},
    Aes256Gcm, Nonce,
};
use rand::Rng;
use rand::RngCore;
use rand::SeedableRng;
use sha2::Digest;
use x25519_dalek::{EphemeralSecret, PublicKey, SharedSecret};

/// Local X25519 key pair
///
/// Stores reusable secret for consistent ECDH
#[derive(Clone)]
pub struct LocalX25519 {
    pub public: [u8; 32],
    secret_bytes: [u8; 32],  // Seed for reusable secret
}

impl Default for LocalX25519 {
    fn default() -> Self {
        Self::new()
    }
}

impl LocalX25519 {
    /// Generate new X25519 key pair with deterministic seed
    pub fn new() -> Self {
        // Generate random seed
        let mut seed = [0u8; 32];
        rand::rngs::OsRng.fill_bytes(&mut seed);

        // Use hash of seed as entropy for EphemeralSecret
        let mut hasher = sha2::Sha256::new();
        hasher.update(&seed);
        let hashed_seed = hasher.finalize();

        // Create deterministic RNG from hashed seed
        let mut rng = rand::rngs::StdRng::from_seed(hashed_seed.into());
        let secret = EphemeralSecret::random_from_rng(&mut rng);
        let public = PublicKey::from(&secret).to_bytes();

        println!("[encryption] Generated X25519 key pair (deterministic)");
        println!("[encryption] Public key: {}", hex::encode(&public[..8]));

        Self { public, secret_bytes: seed }
    }

    /// Get public key
    pub fn get_public(&self) -> [u8; 32] {
        self.public
    }

    /// Perform ECDH with remote public key (deterministic)
    pub fn diffie_hellman(&self, remote_public: &[u8; 32]) -> SharedSecret {
        // Hash stored seed to get same secret
        let mut hasher = sha2::Sha256::new();
        hasher.update(&self.secret_bytes);
        let hashed_seed = hasher.finalize();

        // Recreate deterministic RNG from hashed seed
        let mut rng = rand::rngs::StdRng::from_seed(hashed_seed.into());
        let secret = EphemeralSecret::random_from_rng(&mut rng);

        // Convert bytes to PublicKey
        let remote_key = PublicKey::from(*remote_public);

        // Compute shared secret (will be same each time with same peer)
        let shared_secret = secret.diffie_hellman(&remote_key);

        println!("[encryption] ECDH shared secret computed");
        println!("[encryption] Shared: {}", hex::encode(&shared_secret.as_bytes()[..8]));

        shared_secret
    }
}

/// Session key derived from ECDH shared secret
#[derive(Debug, Clone)]
pub struct SessionKey {
    key: [u8; 32],
}

/// Session with versioning to prevent replay attacks
#[derive(Clone, Debug)]
pub struct Session {
    pub node_id: HashId,
    pub key: SessionKey,
    pub version: u64,
    pub created_at: u128,
    pub last_used: u128,
    pub seen_nonces: Vec<[u8; 12]>,  // Anti-replay: store recently seen nonces
}

impl Session {
    pub fn new(node_id: HashId, key: SessionKey, version: u64) -> Self {
        let now = now_millis();
        Self {
            node_id,
            key,
            version,
            created_at: now,
            last_used: now,
            seen_nonces: Vec::new(),
        }
    }

    pub fn touch(&mut self) {
        self.last_used = now_millis();
    }
    
    /// Check if nonce has been seen before (anti-replay)
    pub fn check_and_add_nonce(&mut self, nonce: &[u8; 12]) -> bool {
        if self.seen_nonces.contains(nonce) {
            return false;
        }
        self.seen_nonces.push(*nonce);
        // Keep only last 1024 nonces (sliding window)
        if self.seen_nonces.len() > 1024 {
            self.seen_nonces.remove(0);
        }
        true
    }

    pub fn age_ms(&self) -> u128 {
        now_millis().saturating_sub(self.created_at)
    }

    pub fn idle_ms(&self) -> u128 {
        now_millis().saturating_sub(self.last_used)
    }

    /// Create AES-256-GCM cipher
    pub fn aes(&self) -> Aes256Gcm {
        self.key.aes()
    }

    /// Get key hash for logging (first 4 bytes)
    pub fn key_hash(&self) -> String {
        hex::encode(&self.key.key[..4])
    }
}

fn now_millis() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

impl SessionKey {
    /// Derive session key from shared secret using HKDF-SHA256
    pub fn derive(shared_secret: &SharedSecret) -> Self {
        use sha2::Sha256;
        use hkdf::Hkdf;

        // Extract shared secret bytes
        let ikm = shared_secret.as_bytes();

        // HKDF with SHA-256
        let hk = Hkdf::<Sha256>::new(None, ikm);

        // Derive 32-byte key
        let mut key = [0u8; 32];
        hk.expand(b"YANDI session key", &mut key)
            .expect("HKDF expansion should not fail");

        println!("[encryption] Session key derived: {}", hex::encode(&key[..8]));

        Self { key }
    }

    /// Create AES-256-GCM cipher
    pub fn aes(&self) -> Aes256Gcm {
        Aes256Gcm::new_from_slice(&self.key).unwrap()
    }
}

/// Encryption manager for peer sessions
///
/// Manages:
/// - Ephemeral X25519 key pairs per Hello handshake (PFS)
/// - Session keys for each peer (with versioning)
/// - Encryption/decryption operations
#[derive(Clone)]
pub struct EncryptionManager {
    pub local_keys: LocalX25519,  // kept for test compat; not used in live handshakes
    our_id: HashId,
    sessions: HashMap<HashId, Session>,
    session_counter: u64,
    /// Pending ephemeral secrets for outgoing Hello requests: nonce → seed bytes
    pending_ephemerals: HashMap<u64, [u8; 32]>,
}

impl Default for EncryptionManager {
    fn default() -> Self {
        let mut our_id = [0u8; 32];
        rand::rngs::OsRng.fill_bytes(&mut our_id);
        Self::new(HashId(our_id))
    }
}

impl EncryptionManager {
    /// Create new encryption manager
    pub fn new(our_id: HashId) -> Self {
        println!("[encryption] Initializing EncryptionManager");

        Self {
            local_keys: LocalX25519::new(),
            our_id,
            sessions: HashMap::new(),
            session_counter: 0,
            pending_ephemerals: HashMap::new(),
        }
    }

    // ── PFS: ephemeral Hello key exchange ──────────────────────────────────

    /// Initiator: generate a fresh ephemeral X25519 keypair for this Hello nonce.
    /// Returns the public key bytes to embed in the Hello Request packet.
    /// The secret is stored internally until `complete_hello_initiator` is called.
    pub fn generate_hello_ephemeral(&mut self, nonce: u64) -> [u8; 32] {
        let mut seed = [0u8; 32];
        rand::rngs::OsRng.fill_bytes(&mut seed);
        let pub_bytes = Self::seed_to_x25519_public(&seed);
        self.pending_ephemerals.insert(nonce, seed);
        pub_bytes
    }

    /// Initiator: complete key exchange when Hello Ack arrives.
    /// `ack_nonce` must equal the nonce from the original Hello Request.
    /// The stored ephemeral secret is consumed and wiped after this call.
    pub fn complete_hello_initiator(
        &mut self,
        request_nonce: u64,
        peer_id: HashId,
        their_pub: &[u8; 32],
    ) -> Result<u64, String> {
        let seed = self.pending_ephemerals.remove(&request_nonce)
            .ok_or_else(|| format!("[PFS] No pending ephemeral for nonce {}", request_nonce))?;

        let shared = Self::seed_to_ecdh(&seed, their_pub);
        self.store_session(peer_id, &shared, "initiator")
    }

    /// Responder: generate a fresh ephemeral X25519 keypair, perform ECDH immediately,
    /// store the session, and return the public key bytes to embed in the Hello Ack.
    pub fn complete_hello_responder(
        &mut self,
        peer_id: HashId,
        their_pub: &[u8; 32],
    ) -> Result<[u8; 32], String> {
        let mut seed = [0u8; 32];
        rand::rngs::OsRng.fill_bytes(&mut seed);
        let our_pub = Self::seed_to_x25519_public(&seed);

        // Ephemeral secret is used immediately and never stored
        let shared = Self::seed_to_ecdh(&seed, their_pub);
        // Overwrite seed before dropping
        seed.fill(0);

        self.store_session(peer_id, &shared, "responder")?;
        Ok(our_pub)
    }

    // ── Helpers ─────────────────────────────────────────────────────────────

    fn seed_to_x25519_public(seed: &[u8; 32]) -> [u8; 32] {
        use sha2::Digest;
        let hashed: [u8; 32] = sha2::Sha256::digest(seed).into();
        let mut rng = rand::rngs::StdRng::from_seed(hashed);
        let secret = EphemeralSecret::random_from_rng(&mut rng);
        PublicKey::from(&secret).to_bytes()
    }

    fn seed_to_ecdh(seed: &[u8; 32], their_pub: &[u8; 32]) -> SharedSecret {
        use sha2::Digest;
        let hashed: [u8; 32] = sha2::Sha256::digest(seed).into();
        let mut rng = rand::rngs::StdRng::from_seed(hashed);
        let secret = EphemeralSecret::random_from_rng(&mut rng);
        secret.diffie_hellman(&PublicKey::from(*their_pub))
    }

    fn store_session(&mut self, peer_id: HashId, shared: &SharedSecret, role: &str) -> Result<u64, String> {
        // Anti-replay: reject if a fresh session already exists
        if let Some(existing) = self.sessions.get(&peer_id) {
            if existing.age_ms() < 300_000 {
                println!("[encryption] ⚠️ Rejecting duplicate handshake for {} (age {}ms)",
                    hex::encode(&peer_id.0[..8]), existing.age_ms());
                return Err(format!("Session too fresh, rejecting duplicate handshake"));
            }
        }
        let session_key = SessionKey::derive(shared);
        self.session_counter += 1;
        let version = self.session_counter;
        self.sessions.insert(peer_id, Session::new(peer_id, session_key, version));
        println!("[encryption] ✅ [PFS] Ephemeral session v{} established ({})", version, role);
        Ok(version)
    }

    /// Legacy key exchange using long-term local_keys (kept for tests).
    /// Live code uses `complete_hello_initiator` / `complete_hello_responder` for PFS.
    pub fn handle_key_exchange(&mut self, peer_id: HashId, remote_public: &[u8; 32]) -> Result<u64, String> {
        let shared = self.local_keys.diffie_hellman(remote_public);
        self.store_session(peer_id, &shared, "legacy")
    }

    /// Derive an isolated AES-256-GCM key for a specific file transfer.
    /// Uses the established session key as input material, ensuring the file
    /// key is isolated from other traffic even if re-use of a session key is attempted.
    /// Returns None if no session exists for this peer.
    pub fn derive_file_key(&self, peer_id: &HashId, file_id: &str) -> Option<[u8; 32]> {
        use hkdf::Hkdf;
        use sha2::Sha256;
        let session = self.sessions.get(peer_id)?;
        // Use session key bytes as IKM, file_id as info → per-transfer isolated key
        let hk = Hkdf::<Sha256>::new(Some(file_id.as_bytes()), &session.key.key);
        let mut file_key = [0u8; 32];
        hk.expand(b"yandi-file-transfer-v1", &mut file_key).expect("HKDF expand");
        Some(file_key)
    }

    /// Check if session exists for peer
    pub fn has_session(&self, peer_id: &HashId) -> bool {
        self.sessions.contains_key(peer_id)
    }

    /// Check if session exists for peer by ID
    pub fn has_session_by_id(&self, peer_id: &HashId) -> bool {
        self.sessions.contains_key(peer_id)
    }

    /// Encrypt data for peer
    ///
    /// Returns: [nonce:12][encrypted_data][auth_tag:16]
    pub fn encrypt(&self, peer_id: &HashId, data: &[u8]) -> Result<Vec<u8>, String> {
        let session = self.sessions.get(peer_id)
            .ok_or_else(|| format!("No session for peer: {}", hex::encode(&peer_id.0[..8])))?;

        let cipher = session.aes();

        // Generate random nonce (12 bytes)
        let mut nonce_bytes = [0u8; 12];
        rand::thread_rng().fill_bytes(&mut nonce_bytes);
        let nonce = Nonce::from_slice(&nonce_bytes);

        // Padding for traffic-analysis resistance.
        // Format inside ciphertext: [orig_len:2LE][data][padding bytes]
        // The 2-byte length prefix lets decrypt() strip padding exactly.
        let (min_pad, max_pad) = if data.len() < 64 { (8, 32) } else { (0, 8) };
        let padding_size = if max_pad > 0 {
            rand::thread_rng().gen_range(min_pad..=max_pad)
        } else {
            0
        };
        let orig_len = data.len() as u16;
        let mut padded_data = Vec::with_capacity(2 + data.len() + padding_size);
        padded_data.extend_from_slice(&orig_len.to_le_bytes());
        padded_data.extend_from_slice(data);
        if padding_size > 0 {
            padded_data.extend(std::iter::repeat(0u8).take(padding_size));
        }


        // Encrypt data WITH padding
        let encrypted = cipher.encrypt(nonce, &padded_data[..])
            .map_err(|e| format!("Encryption failed: {}", e))?;

        // NEW FORMAT: [sender_id:32][nonce:12][encrypted_data][tag:16]
        // sender_id is OUR ID (PLAINTEXT) so receiver can find our session
        let mut out = Vec::with_capacity(32 + 12 + encrypted.len());

        // 1. Prepend sender_id = OUR ID (PLAINTEXT - not encrypted)
        out.extend_from_slice(&self.our_id.0);

        // 2. Prepend nonce
        out.extend_from_slice(&nonce_bytes);

        // 3. Append encrypted data (includes auth tag)
        out.extend_from_slice(&encrypted);

        // Silent logging for heartbeat (check message type)
        let is_heartbeat = data.len() >= 1 && (data[0] == 0x01 || data[0] == 0x02);
        if !is_heartbeat {
            println!("[encryption] Encrypted {} bytes for peer: {}, v={}, key={}",
                     data.len(), hex::encode(&peer_id.0[..8]), session.version, session.key_hash());
        }

        Ok(out)
    }

    /// Decrypt data from peer
    ///
    /// New format: [sender_id:32][nonce:12][encrypted_data][auth_tag:16]
    /// sender_id is PLAINTEXT and is the SENDER'S ID (who encrypted the packet)
    pub fn decrypt(&self, peer_id: &HashId, data: &[u8]) -> Result<Vec<u8>, String> {
        // New format: [sender_id:32][nonce:12][encrypted_data][tag:16]
        if data.len() < 32 + 12 {
            return Err("Encrypted data too short (missing sender_id or nonce)".to_string());
        }

        // Extract sender_id from packet (PLAINTEXT header)
        let mut sender_id_bytes = [0u8; 32];
        sender_id_bytes.copy_from_slice(&data[..32]);
        let packet_sender_id = HashId(sender_id_bytes);

        // Verify: the sender_id in packet should match peer.id (who we expect it from)
        if *peer_id != packet_sender_id {
            return Err(format!("Sender ID mismatch: expected {}, got {}",
                hex::encode(&peer_id.0[..8]),
                hex::encode(&packet_sender_id.0[..8])
            ));
        }

        let session = self.sessions.get(peer_id)
            .ok_or_else(|| format!("No session for peer: {}", hex::encode(&peer_id.0[..8])))?;

        let cipher = session.aes();

        // Split nonce and ciphertext (skip sender_id:32)
        let encrypted_part = &data[32..];
        let (nonce_bytes, ciphertext) = encrypted_part.split_at(12);
        let nonce = Nonce::from_slice(nonce_bytes);

        // Decrypt data
        let decrypted = cipher.decrypt(nonce, ciphertext)
            .map_err(|e| format!("Decryption failed: {}", e))?;

        // Strip padding: first 2 bytes = original data length (LE).
        if decrypted.len() < 2 {
            return Err("Decrypted payload too short (missing length prefix)".to_string());
        }
        let orig_len = u16::from_le_bytes([decrypted[0], decrypted[1]]) as usize;
        if decrypted.len() < 2 + orig_len {
            return Err(format!("Decrypted length prefix {} exceeds payload {}", orig_len, decrypted.len() - 2));
        }
        let payload = decrypted[2..2 + orig_len].to_vec();

        // Silent logging for heartbeat (check message type)
        let is_heartbeat = payload.len() >= 1 && (payload[0] == 0x01 || payload[0] == 0x02);
        if !is_heartbeat {
            println!("[encryption] Decrypted {} bytes from peer: {}",
                     payload.len(), hex::encode(&peer_id.0[..8]));
        }

        Ok(payload)
    }

    /// Extract sender_id from encrypted packet header
    ///
    /// Returns sender_id from the packet without decrypting
    /// Packet format: [sender_id:32][nonce:12][encrypted_data][tag:16]
    pub fn extract_peer_id(data: &[u8]) -> Result<HashId, String> {
        if data.len() < 32 {
            return Err("Packet too short to extract sender_id".to_string());
        }
        let mut sender_id_bytes = [0u8; 32];
        sender_id_bytes.copy_from_slice(&data[..32]);
        Ok(HashId(sender_id_bytes))
    }

    /// Decrypt data by sender_id lookup
    ///
    /// Extracts sender_id from packet, looks up sender in sessions, decrypts
    /// This is the preferred method for decrypting incoming packets
    pub fn decrypt_by_peer_id(&self, data: &[u8]) -> Result<(HashId, Vec<u8>), String> {
        // Extract sender_id from packet header
        let sender_id = Self::extract_peer_id(data)?;

        // Check if we have a session for this sender
        let session = self.sessions.get(&sender_id)
            .ok_or_else(|| format!("No session for sender_id: {}", hex::encode(&sender_id.0[..8])))?;

        let cipher = session.aes();

        // Split nonce and ciphertext (skip sender_id:32)
        if data.len() < 32 + 12 {
            return Err("Encrypted data too short (missing nonce)".to_string());
        }
        let encrypted_part = &data[32..];
        let (nonce_bytes, ciphertext) = encrypted_part.split_at(12);
        let nonce = Nonce::from_slice(nonce_bytes);

        // Decrypt data
        let decrypted = cipher.decrypt(nonce, ciphertext)
            .map_err(|e| format!("Decryption failed for sender {}: {}",
                hex::encode(&sender_id.0[..8]), e))?;

        // Silent logging for heartbeat
        let is_heartbeat = decrypted.len() >= 1 && (decrypted[0] == 0x01 || decrypted[0] == 0x02);
        if !is_heartbeat {
            println!("[encryption] Decrypted {} bytes from: {}, v={}, key={}",
                     decrypted.len(), hex::encode(&sender_id.0[..8]), session.version, session.key_hash());
        }

        Ok((sender_id, decrypted))
    }

    /// Remove stale sessions (idle for more than 5 minutes)
    pub fn cleanup_stale_sessions(&mut self) {
        let now = now_millis();
        let mut removed = 0;
        self.sessions.retain(|node_id, session| {
            let idle = session.idle_ms();
            if idle > 300_000 {  // 5 минут
                println!("[encryption] 🧹 Cleanup: removing session v{} for {} (idle={}ms)",
                    session.version, hex::encode(&node_id.0[..8]), idle);
                removed += 1;
                false
            } else {
                true
            }
        });
        if removed > 0 {
            println!("[encryption] 🧹 Cleanup complete: removed {} sessions, {} active",
                removed, self.sessions.len());
        }
    }

    /// Remove session for peer
    pub fn remove_session(&mut self, peer_id: &HashId) {
        self.sessions.remove(peer_id);
        println!("[encryption] Session removed for peer: {}", hex::encode(&peer_id.0[..8]));
    }

    /// Get session count
    pub fn session_count(&self) -> usize {
        self.sessions.len()
    }
}

/// Simple encryption function for direct use (e.g., in privacy tunnels)
pub fn encrypt_data(data: &[u8], key: &[u8; 32]) -> Result<Vec<u8>, String> {
    use chacha20poly1305::{ChaCha20Poly1305, Key, Nonce};

    let cipher = ChaCha20Poly1305::new(Key::from_slice(key));

    let mut nonce_bytes = [0u8; 12];
    rand::thread_rng().fill_bytes(&mut nonce_bytes);
    let nonce = Nonce::from_slice(&nonce_bytes);

    let mut encrypted = cipher.encrypt(nonce, data)
        .map_err(|_| "Encryption failed")?;

    // Prepend nonce
    let mut result = nonce_bytes.to_vec();
    result.append(&mut encrypted);

    Ok(result)
}

/// Simple decryption function for direct use
pub fn decrypt_data(encrypted_data: &[u8], key: &[u8; 32]) -> Result<Vec<u8>, String> {
    use chacha20poly1305::{ChaCha20Poly1305, Key, Nonce};

    if encrypted_data.len() < 12 {
        return Err("Encrypted data too short".to_string());
    }

    let cipher = ChaCha20Poly1305::new(Key::from_slice(key));

    let (nonce_bytes, ciphertext) = encrypted_data.split_at(12);
    let nonce = Nonce::from_slice(nonce_bytes);

    cipher.decrypt(nonce, ciphertext)
        .map_err(|_| "Decryption failed".to_string())
        .map(|v| v.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_local_x25519_generation() {
        let key = LocalX25519::new();
        assert_eq!(key.public.len(), 32);
    }

    #[test]
    fn test_session_establishment() {
        use crate::core::NodeIdentity;

        let identity_a = NodeIdentity::new();
        let identity_b = NodeIdentity::new();

        let mut manager_a = EncryptionManager::new(identity_a.node_id());
        let mut manager_b = EncryptionManager::new(identity_b.node_id());

        let peer_a = identity_a.node_id();
        let peer_b = identity_b.node_id();

        // Exchange keys
        manager_a.handle_key_exchange(peer_b, &manager_b.local_keys.public);
        manager_b.handle_key_exchange(peer_a, &manager_a.local_keys.public);

        // Both should have sessions
        assert!(manager_a.has_session(&peer_b));
        assert!(manager_b.has_session(&peer_a));
    }

    #[test]
    fn test_encryption_decryption() {
        use crate::core::NodeIdentity;

        let identity_a = NodeIdentity::new();
        let identity_b = NodeIdentity::new();

        let mut manager_a = EncryptionManager::new(identity_a.node_id());
        let mut manager_b = EncryptionManager::new(identity_b.node_id());

        let peer_a = identity_a.node_id();
        let peer_b = identity_b.node_id();

        // Establish sessions
        manager_a.handle_key_exchange(peer_b, &manager_b.local_keys.public);
        manager_b.handle_key_exchange(peer_a, &manager_a.local_keys.public);

        // Encrypt from A to B
        let plaintext = b"Hello, encrypted world!";
        let encrypted = manager_a.encrypt(&peer_b, plaintext).unwrap();

        // Decrypt on B
        let decrypted = manager_b.decrypt(&peer_a, &encrypted).unwrap();

        assert_eq!(plaintext.to_vec(), decrypted);
    }
}
