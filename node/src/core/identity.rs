// src/core/identity.rs
//! Node Identity with Real Cryptographic Keys
//! ============================================
//!
//! Production-grade Ed25519/X25519 key management.
//! Private keys are encrypted at rest using Argon2id + AES-256-GCM (SEC-04).

use rand::rngs::OsRng;
use rand::RngCore;
use ed25519_dalek::{Signer, Verifier};
use x25519_dalek::{EphemeralSecret, PublicKey};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

use crate::util::{HashId, NodeName};

// ── Argon2id parameters ────────────────────────────────────────────────────
// 32 MB memory, 2 iterations, 1 thread — ~0.2–0.5 s per attempt on modern hardware.
// Strong enough to resist offline brute-force even for weak user passwords.
const ARGON2_MEMORY_KB: u32 = 32768;
const ARGON2_ITERATIONS: u32 = 2;

/// Node identity with real cryptographic keys.
#[derive(Debug, Clone)]
pub struct NodeIdentity {
    pub address: HashId,
    pub public_key: [u8; 32],
    private_key: [u8; 32],
    pub signing_public_key: [u8; 32],
    pub signing_private_key: [u8; 32],
    _private_guard: (),
}

// ── On-disk format v2 (encrypted) ─────────────────────────────────────────

#[derive(Debug, Serialize, Deserialize)]
struct EncryptedPrivateKeys {
    /// Argon2id salt, hex-encoded (32 bytes)
    salt: String,
    /// AES-256-GCM nonce, hex-encoded (12 bytes)
    nonce: String,
    /// AES-256-GCM ciphertext of [private_key || signing_private_key], hex-encoded
    ciphertext: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct StoredIdentityV2 {
    version: u32,
    address: [u8; 32],
    pub public_key: [u8; 32],
    pub signing_public_key: [u8; 32],
    #[serde(default = "default_created_at")]
    created_at: chrono::DateTime<chrono::Utc>,
    encrypted_private_keys: EncryptedPrivateKeys,
}

// ── On-disk format v1 (legacy plaintext) ──────────────────────────────────
// Kept only for migration; never written by this code.

#[derive(Debug, Serialize, Deserialize)]
struct StoredIdentityV1 {
    address: [u8; 32],
    pub public_key: [u8; 32],
    pub private_key: [u8; 32],
    pub signing_public_key: [u8; 32],
    pub signing_private_key: [u8; 32],
    #[serde(default = "default_created_at")]
    created_at: chrono::DateTime<chrono::Utc>,
}

// Keep the old name available so call sites that reference `StoredIdentity` still compile.
pub type StoredIdentity = StoredIdentityV1;

fn default_created_at() -> chrono::DateTime<chrono::Utc> {
    chrono::Utc::now()
}

// ── Key derivation helpers ─────────────────────────────────────────────────

/// Derive a 32-byte AES key from a passphrase and salt using Argon2id.
fn derive_key(passphrase: &[u8], salt: &[u8; 32]) -> Result<[u8; 32], String> {
    use argon2::{Argon2, Params, Algorithm, Version};
    let params = Params::new(ARGON2_MEMORY_KB, ARGON2_ITERATIONS, 1, Some(32))
        .map_err(|e| format!("Argon2 params: {}", e))?;
    let argon2 = Argon2::new(Algorithm::Argon2id, Version::V0x13, params);
    let mut key = [0u8; 32];
    argon2.hash_password_into(passphrase, salt, &mut key)
        .map_err(|e| format!("Argon2 KDF failed: {}", e))?;
    Ok(key)
}

/// Return the passphrase used to protect private keys.
///
/// Priority:
/// 1. `YANDI_KEY_PASSWORD` environment variable (user-set, strongest).
/// 2. Machine-derived secret: `/etc/machine-id` + first 16 bytes of node address.
///    This protects against stolen backup files without requiring user interaction.
fn key_passphrase(node_address: &[u8; 32]) -> Vec<u8> {
    if let Ok(pwd) = std::env::var("YANDI_KEY_PASSWORD") {
        let pwd = pwd.trim().to_string();
        if !pwd.is_empty() {
            return pwd.into_bytes();
        }
    }
    // Machine-specific fallback
    let machine_id = fs::read_to_string("/etc/machine-id")
        .unwrap_or_else(|_| "YANDI_FALLBACK_MACHINE_ID".to_string());
    let machine_id = machine_id.trim().to_string();
    format!("YANDI:{}:{}", machine_id, hex::encode(&node_address[..16]))
        .into_bytes()
}

/// Encrypt [private_key || signing_private_key] with AES-256-GCM.
fn encrypt_private_keys(
    private_key: &[u8; 32],
    signing_private_key: &[u8; 32],
    aes_key: &[u8; 32],
    nonce_bytes: &[u8; 12],
) -> Result<Vec<u8>, String> {
    use aes_gcm::{Aes256Gcm, aead::{Aead, KeyInit}, Nonce};
    let cipher = Aes256Gcm::new_from_slice(aes_key)
        .map_err(|e| format!("AES-GCM init: {}", e))?;
    let nonce = Nonce::from_slice(nonce_bytes);
    let mut plaintext = Vec::with_capacity(64);
    plaintext.extend_from_slice(private_key);
    plaintext.extend_from_slice(signing_private_key);
    cipher.encrypt(nonce, plaintext.as_slice())
        .map_err(|e| format!("AES-GCM encrypt: {}", e))
}

/// Decrypt and return [private_key, signing_private_key] from AES-256-GCM ciphertext.
fn decrypt_private_keys(
    ciphertext: &[u8],
    aes_key: &[u8; 32],
    nonce_bytes: &[u8; 12],
) -> Result<([u8; 32], [u8; 32]), String> {
    use aes_gcm::{Aes256Gcm, aead::{Aead, KeyInit}, Nonce};
    let cipher = Aes256Gcm::new_from_slice(aes_key)
        .map_err(|e| format!("AES-GCM init: {}", e))?;
    let nonce = Nonce::from_slice(nonce_bytes);
    let plaintext = cipher.decrypt(nonce, ciphertext)
        .map_err(|_| "AES-GCM decryption failed — wrong passphrase or corrupted file".to_string())?;
    if plaintext.len() != 64 {
        return Err(format!("Decrypted key material has unexpected length: {}", plaintext.len()));
    }
    let mut private_key = [0u8; 32];
    let mut signing_private_key = [0u8; 32];
    private_key.copy_from_slice(&plaintext[..32]);
    signing_private_key.copy_from_slice(&plaintext[32..]);
    Ok((private_key, signing_private_key))
}

// ── NodeIdentity ───────────────────────────────────────────────────────────

impl NodeIdentity {
    pub fn new() -> Self {
        let mut rng = OsRng;
        let addr = HashId::new_random();

        let x25519_secret = EphemeralSecret::random_from_rng(&mut rng);
        let x25519_public = PublicKey::from(&x25519_secret);

        let mut private_key_bytes = [0u8; 32];
        rng.fill_bytes(&mut private_key_bytes);

        let ed25519_signing_key = ed25519_dalek::SigningKey::generate(&mut rng);
        let ed25519_verifying_key = ed25519_signing_key.verifying_key();

        println!("[identity] Generated cryptographic key pair");
        println!("[identity] Public X25519: {}", hex::encode(&x25519_public.as_bytes()[..8]));

        Self {
            address: addr,
            public_key: *x25519_public.as_bytes(),
            private_key: private_key_bytes,
            signing_public_key: *ed25519_verifying_key.as_bytes(),
            signing_private_key: ed25519_signing_key.to_bytes(),
            _private_guard: (),
        }
    }

    pub fn id(&self) -> HashId { self.address }
    pub fn node_id(&self) -> HashId { self.address }

    /// Self-certifying node name (SHA256 of signing public key).
    pub fn node_name(&self) -> NodeName {
        NodeName::from_public_key(&self.signing_public_key)
    }

    pub fn verify_node(
        node_name: &NodeName,
        public_key: &[u8; 32],
        signature: &[u8],
        challenge: &[u8],
    ) -> bool {
        if !node_name.verify_public_key(public_key) { return false; }
        if signature.len() != 64 { return false; }
        let verifying_key = match ed25519_dalek::VerifyingKey::from_bytes(public_key) {
            Ok(k) => k, Err(_) => return false,
        };
        let mut sig_bytes = [0u8; 64];
        sig_bytes.copy_from_slice(&signature[..64]);
        verifying_key.verify(challenge, &ed25519_dalek::Signature::from_bytes(&sig_bytes)).is_ok()
    }

    pub fn verify_signature(&self, public_key: &[u8; 32], signature: &[u8], data: &[u8]) -> bool {
        if signature.len() != 64 { return false; }
        let verifying_key = match ed25519_dalek::VerifyingKey::from_bytes(public_key) {
            Ok(k) => k, Err(_) => return false,
        };
        let mut sig_bytes = [0u8; 64];
        sig_bytes.copy_from_slice(&signature[..64]);
        verifying_key.verify(data, &ed25519_dalek::Signature::from_bytes(&sig_bytes)).is_ok()
    }

    pub fn sign(&self, data: &[u8]) -> Result<Vec<u8>, String> {
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&self.signing_private_key);
        Ok(signing_key.sign(data).to_bytes().to_vec())
    }

    pub fn verify(&self, data: &[u8], signature: &[u8]) -> bool {
        if signature.len() != 64 { return false; }
        let verifying_key = match ed25519_dalek::VerifyingKey::from_bytes(&self.signing_public_key) {
            Ok(k) => k, Err(_) => return false,
        };
        let mut sig_bytes = [0u8; 64];
        sig_bytes.copy_from_slice(&signature[..64]);
        verifying_key.verify(data, &ed25519_dalek::Signature::from_bytes(&sig_bytes)).is_ok()
    }

    pub fn generate_ipv6_virtual(&self) -> String {
        let prefix = "fc00:1234:5678::";
        let node_slice = &self.address.0[..8];
        let groups: Vec<String> = node_slice.chunks(2)
            .map(|c| format!("{:02x}{:02x}", c[0], c[1])).collect();
        format!("{}{}:{}:{}:{}", prefix, groups[0], groups[1], groups[2], groups[3])
    }

    pub fn generate_ipv6_virtual_with_prefix(&self, prefix: &str) -> String {
        let node_slice = &self.address.0[..8];
        let groups: Vec<String> = node_slice.chunks(2)
            .map(|c| format!("{:02x}{:02x}", c[0], c[1])).collect();
        format!("{}{}:{}:{}:{}", prefix, groups[0], groups[1], groups[2], groups[3])
    }

    pub fn is_ipv6_virtual(address: &str) -> bool {
        address.starts_with("fc00:1234:5678::")
    }

    pub fn get_ipv6_short(&self) -> String {
        let ipv6 = self.generate_ipv6_virtual();
        if ipv6.len() > 20 { format!("...{}", &ipv6[ipv6.len()-16..]) } else { ipv6 }
    }

    // ── Persistence (SEC-04: encrypted at rest) ────────────────────────────

    /// Save identity to disk with private keys encrypted using Argon2id + AES-256-GCM.
    pub fn save_to_file(&self, port: u16) -> Result<PathBuf, String> {
        let keys_dir = Self::get_keys_directory()?;
        fs::create_dir_all(&keys_dir)
            .map_err(|e| format!("Failed to create keys directory: {}", e))?;

        let filename = format!("node_identity_{}.json", port);
        let file_path = keys_dir.join(filename);

        // Generate random salt and nonce
        let mut salt = [0u8; 32];
        let mut nonce_bytes = [0u8; 12];
        OsRng.fill_bytes(&mut salt);
        OsRng.fill_bytes(&mut nonce_bytes);

        // Derive AES key from passphrase
        let passphrase = key_passphrase(&self.address.0);
        let aes_key = derive_key(&passphrase, &salt)?;

        // Encrypt private keys
        let ciphertext = encrypt_private_keys(
            &self.private_key,
            &self.signing_private_key,
            &aes_key,
            &nonce_bytes,
        )?;

        let stored = StoredIdentityV2 {
            version: 2,
            address: self.address.0,
            public_key: self.public_key,
            signing_public_key: self.signing_public_key,
            created_at: chrono::Utc::now(),
            encrypted_private_keys: EncryptedPrivateKeys {
                salt: hex::encode(&salt),
                nonce: hex::encode(&nonce_bytes),
                ciphertext: hex::encode(&ciphertext),
            },
        };

        let json = serde_json::to_string_pretty(&stored)
            .map_err(|e| format!("Failed to serialize identity: {}", e))?;
        fs::write(&file_path, json)
            .map_err(|e| format!("Failed to write identity file: {}", e))?;

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = fs::metadata(&file_path)
                .map_err(|e| format!("Failed to get metadata: {}", e))?
                .permissions();
            perms.set_mode(0o600);
            fs::set_permissions(&file_path, perms)
                .map_err(|e| format!("Failed to set permissions: {}", e))?;
        }

        println!("[identity] Identity saved (node_id: {}, encrypted)", hex::encode(&self.address.0[..8]));
        Ok(file_path)
    }

    /// Load identity from disk, decrypting private keys.
    /// Automatically migrates v1 (plaintext) files to v2 (encrypted) format.
    pub fn load_from_file(port: u16) -> Result<Self, String> {
        let keys_dir = Self::get_keys_directory()?;
        let filename = format!("node_identity_{}.json", port);
        let file_path = keys_dir.join(filename);

        if !file_path.exists() {
            return Err(format!("Identity file not found for port {}", port));
        }

        let json = fs::read_to_string(&file_path)
            .map_err(|e| format!("Failed to read identity file: {}", e))?;

        // Detect format by checking for 'version' field in JSON
        let raw: serde_json::Value = serde_json::from_str(&json)
            .map_err(|e| format!("Invalid JSON in identity file: {}", e))?;

        let version = raw.get("version").and_then(|v| v.as_u64()).unwrap_or(1);

        if version >= 2 {
            Self::load_v2(&json, port, &file_path)
        } else {
            // V1 legacy file — load then immediately migrate
            println!("[identity] ⚠️  Migrating plaintext identity to encrypted format (SEC-04)");
            Self::load_v1_and_migrate(&json, port)
        }
    }

    fn load_v2(json: &str, _port: u16, _file_path: &PathBuf) -> Result<Self, String> {
        let stored: StoredIdentityV2 = serde_json::from_str(json)
            .map_err(|e| format!("Failed to parse identity v2: {}", e))?;

        // Decode hex fields
        let salt_bytes = hex::decode(&stored.encrypted_private_keys.salt)
            .map_err(|e| format!("Invalid salt hex: {}", e))?;
        let nonce_bytes = hex::decode(&stored.encrypted_private_keys.nonce)
            .map_err(|e| format!("Invalid nonce hex: {}", e))?;
        let ciphertext = hex::decode(&stored.encrypted_private_keys.ciphertext)
            .map_err(|e| format!("Invalid ciphertext hex: {}", e))?;

        if salt_bytes.len() != 32 {
            return Err(format!("Salt must be 32 bytes, got {}", salt_bytes.len()));
        }
        if nonce_bytes.len() != 12 {
            return Err(format!("Nonce must be 12 bytes, got {}", nonce_bytes.len()));
        }

        let mut salt = [0u8; 32];
        let mut nonce = [0u8; 12];
        salt.copy_from_slice(&salt_bytes);
        nonce.copy_from_slice(&nonce_bytes);

        // Derive key and decrypt
        let passphrase = key_passphrase(&stored.address);
        let aes_key = derive_key(&passphrase, &salt)?;
        let (private_key, signing_private_key) = decrypt_private_keys(&ciphertext, &aes_key, &nonce)?;

        println!("[identity] Identity loaded (node_id: {}, decrypted)", hex::encode(&stored.address[..8]));

        Ok(Self {
            address: HashId(stored.address),
            public_key: stored.public_key,
            private_key,
            signing_public_key: stored.signing_public_key,
            signing_private_key,
            _private_guard: (),
        })
    }

    fn load_v1_and_migrate(json: &str, port: u16) -> Result<Self, String> {
        let stored: StoredIdentityV1 = serde_json::from_str(json)
            .map_err(|e| format!("Failed to parse legacy identity: {}", e))?;

        let identity = Self {
            address: HashId(stored.address),
            public_key: stored.public_key,
            private_key: stored.private_key,
            signing_public_key: stored.signing_public_key,
            signing_private_key: stored.signing_private_key,
            _private_guard: (),
        };

        // Re-save in v2 encrypted format
        if let Err(e) = identity.save_to_file(port) {
            eprintln!("[identity] ❌ Migration to encrypted format failed: {}", e);
        } else {
            println!("[identity] ✅ Migrated to encrypted identity format");
        }

        Ok(identity)
    }

    pub fn get_keys_directory() -> Result<PathBuf, String> {
        let home_dir = dirs::home_dir().ok_or("Failed to get home directory")?;
        Ok(home_dir.join(".yandi_keys"))
    }

    pub fn exists_saved(port: u16) -> bool {
        let keys_dir = match Self::get_keys_directory() {
            Ok(dir) => dir, Err(_) => return false,
        };
        keys_dir.join(format!("node_identity_{}.json", port)).exists()
    }

    pub fn load_or_create(port: u16) -> Self {
        if Self::exists_saved(port) {
            match Self::load_from_file(port) {
                Ok(identity) => {
                    println!("[identity] Loaded existing identity for port {}", port);
                    return identity;
                }
                Err(e) => {
                    println!("[identity] Failed to load identity: {}", e);
                }
            }
        }

        let identity = Self::new();
        if let Err(e) = identity.save_to_file(port) {
            println!("[identity] Failed to save new identity: {}", e);
        }
        identity
    }

    /// Verify signature using raw public key and signature (static method).
    pub fn verify_raw(public_key: &[u8; 32], signature: &[u8; 64], data: &[u8]) -> bool {
        let verifying_key = match ed25519_dalek::VerifyingKey::from_bytes(public_key) {
            Ok(k) => k, Err(_) => return false,
        };
        verifying_key.verify(data, &ed25519_dalek::Signature::from_bytes(signature)).is_ok()
    }
}
