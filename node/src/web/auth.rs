// src/web/auth.rs
//! Authentication and session management for YANDI web UI.
//!
//! Two-password model:
//!   login_password  — protects web UI access; browser may remember it
//!   master_password — derives master_key for all at-rest encryption; never stored in browser

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use aes_gcm::{aead::{Aead, KeyInit}, Aes256Gcm, Nonce};
use argon2::{Argon2, Params, Algorithm, Version};
use rand::RngCore;
use serde::{Deserialize, Serialize};

// ── Constants ──────────────────────────────────────────────────────────────

const ARGON2_MEMORY_KB: u32 = 32768; // 32 MB
const ARGON2_ITERATIONS: u32 = 2;
const SESSION_COOKIE: &str = "yandi_session";
const SESSION_REMEMBER_SECS: u64 = 30 * 24 * 3600; // 30 days

// ── On-disk auth state ─────────────────────────────────────────────────────

#[derive(Debug, Serialize, Deserialize)]
pub struct EncryptedMasterKey {
    /// Argon2id salt for deriving machine key, hex-encoded (32 bytes)
    pub machine_salt: String,
    /// AES-GCM nonce, hex-encoded (12 bytes)
    pub nonce: String,
    /// AES-GCM ciphertext of master_key, hex-encoded (32 + 16 bytes)
    pub ciphertext: String,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct StoredAuth {
    pub version: u32,
    /// Argon2id encoded hash of login_password (from argon2 crate PHC string format)
    pub login_hash: String,
    /// master_key encrypted with machine-derived key
    pub master_key_encrypted: EncryptedMasterKey,
}

// ── Session ────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct Session {
    pub token: String,
    /// Unix timestamp when session expires
    pub expires_at: u64,
}

impl Session {
    pub fn is_expired(&self) -> bool {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        now >= self.expires_at
    }
}

// ── Login rate limiting ────────────────────────────────────────────────────
//
// api_auth_login/api_auth_rebind (web/server.rs) had no throttling at all —
// combined with the web UI defaulting to bind 0.0.0.0 (real, reachable from
// WAN/LAN, confirmed live on this machine) and a login password allowed to
// be as short as 4 characters, this was a genuine, unlimited-speed online
// brute-force surface. A global (not per-IP — this is a single-owner
// dashboard; per-IP would need ConnectInfo plumbing through the whole axum
// server for a marginal gain, and doesn't protect against a distributed
// attempt anyway) exponential backoff after a few free attempts closes it
// without ever permanently locking the real owner out — it only ever slows
// attempts down, never blocks them outright.
const LOGIN_FREE_ATTEMPTS: u32 = 3;
const LOGIN_BACKOFF_BASE_SECS: u64 = 2;
const LOGIN_BACKOFF_MAX_SECS: u64 = 300;

#[derive(Debug, Clone, Copy)]
struct LoginAttempts {
    consecutive_failures: u32,
    last_failure: Instant,
}

impl LoginAttempts {
    fn backoff_after(failures: u32) -> Duration {
        if failures <= LOGIN_FREE_ATTEMPTS {
            return Duration::ZERO;
        }
        let exp = (failures - LOGIN_FREE_ATTEMPTS - 1).min(31);
        let secs = LOGIN_BACKOFF_BASE_SECS.saturating_mul(1u64 << exp);
        Duration::from_secs(secs.min(LOGIN_BACKOFF_MAX_SECS))
    }
}

// ── Auth state (in-memory) ─────────────────────────────────────────────────

#[derive(Clone)]
pub struct AuthState {
    /// In-memory session store
    pub sessions: Arc<Mutex<HashMap<String, Session>>>,
    /// master_key available after successful setup/login/rebind (32 bytes)
    pub master_key: Arc<Mutex<Option<[u8; 32]>>>,
    /// Whether auth has been set up (auth.json exists and loaded)
    pub is_setup: Arc<std::sync::atomic::AtomicBool>,
    /// Whether the machine_id matches (false = need rebind)
    pub needs_rebind: Arc<std::sync::atomic::AtomicBool>,
    /// Login brute-force throttling — see `LoginAttempts`.
    login_attempts: Arc<Mutex<Option<LoginAttempts>>>,
}

impl Default for AuthState {
    fn default() -> Self {
        Self {
            sessions: Arc::new(Mutex::new(HashMap::new())),
            master_key: Arc::new(Mutex::new(None)),
            is_setup: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            needs_rebind: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            login_attempts: Arc::new(Mutex::new(None)),
        }
    }
}

impl AuthState {
    /// Returns true if auth.json was loaded and master_key decrypted successfully.
    pub fn is_ready(&self) -> bool {
        self.is_setup.load(std::sync::atomic::Ordering::Relaxed)
    }

    pub fn needs_rebind(&self) -> bool {
        self.needs_rebind.load(std::sync::atomic::Ordering::Relaxed)
    }

    pub fn get_master_key(&self) -> Option<[u8; 32]> {
        self.master_key.lock().ok()?.clone()
    }

    /// Create a new session token. Returns the token string.
    pub fn create_session(&self, remember_me: bool) -> String {
        let mut token_bytes = [0u8; 32];
        rand::rngs::OsRng.fill_bytes(&mut token_bytes);
        let token = hex::encode(token_bytes);

        let ttl = if remember_me { SESSION_REMEMBER_SECS } else { 3600 * 24 };
        let expires_at = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs() + ttl;

        let session = Session { token: token.clone(), expires_at };
        if let Ok(mut store) = self.sessions.lock() {
            // Evict expired sessions
            store.retain(|_, s| !s.is_expired());
            store.insert(token.clone(), session);
        }
        token
    }

    /// Verify a session token. Returns true if valid and not expired.
    pub fn verify_session(&self, token: &str) -> bool {
        if let Ok(mut store) = self.sessions.lock() {
            if let Some(session) = store.get(token) {
                if session.is_expired() {
                    store.remove(token);
                    return false;
                }
                return true;
            }
        }
        false
    }

    pub fn invalidate_session(&self, token: &str) {
        if let Ok(mut store) = self.sessions.lock() {
            store.remove(token);
        }
    }

    /// If a login attempt should be rejected right now without even
    /// checking the password (still cooling down from prior failures),
    /// returns how much longer to wait.
    pub fn login_backoff_remaining(&self) -> Option<Duration> {
        let attempts = self.login_attempts.lock().ok()?;
        let a = (*attempts)?;
        let required = LoginAttempts::backoff_after(a.consecutive_failures);
        if required.is_zero() {
            return None;
        }
        let elapsed = a.last_failure.elapsed();
        if elapsed >= required {
            None
        } else {
            Some(required - elapsed)
        }
    }

    pub fn record_login_failure(&self) {
        if let Ok(mut attempts) = self.login_attempts.lock() {
            let failures = attempts.map(|a| a.consecutive_failures).unwrap_or(0) + 1;
            *attempts = Some(LoginAttempts { consecutive_failures: failures, last_failure: Instant::now() });
        }
    }

    pub fn record_login_success(&self) {
        if let Ok(mut attempts) = self.login_attempts.lock() {
            *attempts = None;
        }
    }
}

// ── Key derivation helpers ─────────────────────────────────────────────────

/// Derive a 32-byte key from a passphrase + salt using Argon2id.
pub fn derive_key(passphrase: &[u8], salt: &[u8; 32]) -> Result<[u8; 32], String> {
    let params = Params::new(ARGON2_MEMORY_KB, ARGON2_ITERATIONS, 1, Some(32))
        .map_err(|e| format!("Argon2 params: {}", e))?;
    let argon2 = Argon2::new(Algorithm::Argon2id, Version::V0x13, params);
    let mut key = [0u8; 32];
    argon2.hash_password_into(passphrase, salt, &mut key)
        .map_err(|e| format!("Argon2 KDF failed: {}", e))?;
    Ok(key)
}

/// Return machine-specific passphrase used to protect the stored master_key.
fn machine_passphrase() -> Vec<u8> {
    let machine_id = std::fs::read_to_string("/etc/machine-id")
        .unwrap_or_else(|_| "YANDI_FALLBACK_MACHINE_ID".to_string());
    format!("YANDI_MACHINE:{}", machine_id.trim()).into_bytes()
}

/// Hash a login password using Argon2id. Returns a PHC-format string.
pub fn hash_login_password(password: &str) -> Result<String, String> {
    use argon2::password_hash::{PasswordHasher, SaltString};
    let salt = SaltString::generate(&mut rand::rngs::OsRng);
    let argon2 = Argon2::default();
    argon2.hash_password(password.as_bytes(), &salt)
        .map(|h| h.to_string())
        .map_err(|e| format!("Password hash failed: {}", e))
}

/// Verify a login password against stored PHC hash.
pub fn verify_login_password(password: &str, stored_hash: &str) -> bool {
    use argon2::password_hash::{PasswordVerifier, PasswordHash};
    let Ok(hash) = PasswordHash::new(stored_hash) else { return false };
    Argon2::default().verify_password(password.as_bytes(), &hash).is_ok()
}

/// Encrypt master_key with a machine-derived key.
fn encrypt_master_key(master_key: &[u8; 32]) -> Result<EncryptedMasterKey, String> {
    let mut salt = [0u8; 32];
    let mut nonce_bytes = [0u8; 12];
    rand::rngs::OsRng.fill_bytes(&mut salt);
    rand::rngs::OsRng.fill_bytes(&mut nonce_bytes);

    let machine_key = derive_key(&machine_passphrase(), &salt)?;
    let cipher = Aes256Gcm::new_from_slice(&machine_key)
        .map_err(|e| format!("AES init: {}", e))?;
    let nonce = Nonce::from_slice(&nonce_bytes);
    let ciphertext = cipher.encrypt(nonce, master_key.as_slice())
        .map_err(|e| format!("AES encrypt: {}", e))?;

    Ok(EncryptedMasterKey {
        machine_salt: hex::encode(salt),
        nonce: hex::encode(nonce_bytes),
        ciphertext: hex::encode(ciphertext),
    })
}

/// Decrypt master_key using machine-derived key. Returns None if machine-id mismatch.
fn decrypt_master_key(enc: &EncryptedMasterKey) -> Option<[u8; 32]> {
    let salt_bytes = hex::decode(&enc.machine_salt).ok()?;
    let nonce_bytes = hex::decode(&enc.nonce).ok()?;
    let ciphertext = hex::decode(&enc.ciphertext).ok()?;

    let mut salt = [0u8; 32];
    let mut nonce_arr = [0u8; 12];
    if salt_bytes.len() != 32 || nonce_bytes.len() != 12 { return None; }
    salt.copy_from_slice(&salt_bytes);
    nonce_arr.copy_from_slice(&nonce_bytes);

    let machine_key = derive_key(&machine_passphrase(), &salt).ok()?;
    let cipher = Aes256Gcm::new_from_slice(&machine_key).ok()?;
    let nonce = Nonce::from_slice(&nonce_arr);
    let plaintext = cipher.decrypt(nonce, ciphertext.as_slice()).ok()?;
    if plaintext.len() != 32 { return None; }
    let mut key = [0u8; 32];
    key.copy_from_slice(&plaintext);
    Some(key)
}

// ── Auth file path ─────────────────────────────────────────────────────────

pub fn auth_file_path() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".yandi_keys")
        .join("auth.json")
}

// ── Public API ─────────────────────────────────────────────────────────────

/// Try to load auth from disk and auto-decrypt master_key.
/// Returns the loaded AuthState (may be in setup/rebind mode).
pub fn load_auth_state() -> AuthState {
    let state = AuthState::default();
    let path = auth_file_path();

    if !path.exists() {
        // First run — needs setup
        return state;
    }

    let json = match std::fs::read_to_string(&path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[auth] Failed to read auth.json: {}", e);
            return state;
        }
    };

    let stored: StoredAuth = match serde_json::from_str(&json) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[auth] Failed to parse auth.json: {}", e);
            return state;
        }
    };

    match decrypt_master_key(&stored.master_key_encrypted) {
        Some(master_key) => {
            println!("[auth] ✅ Master key loaded (machine verified)");
            state.is_setup.store(true, std::sync::atomic::Ordering::Relaxed);
            if let Ok(mut mk) = state.master_key.lock() {
                *mk = Some(master_key);
            }
        }
        None => {
            println!("[auth] ⚠️ Machine-id mismatch — rebind required");
            state.is_setup.store(true, std::sync::atomic::Ordering::Relaxed);
            state.needs_rebind.store(true, std::sync::atomic::Ordering::Relaxed);
        }
    }

    state
}

/// Perform first-time setup: hash login password, derive master_key, store both.
pub fn setup_auth(
    state: &AuthState,
    login_password: &str,
    master_password: &str,
) -> Result<[u8; 32], String> {
    if login_password.len() < 4 {
        return Err("Login password must be at least 4 characters".to_string());
    }
    if master_password.len() < 8 {
        return Err("Master password must be at least 8 characters".to_string());
    }

    // Derive master_key from master_password
    let mut master_salt = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut master_salt);
    let master_key = derive_key(master_password.as_bytes(), &master_salt)?;

    // Hash login password
    let login_hash = hash_login_password(login_password)?;

    // Encrypt master_key with machine-id
    let master_key_encrypted = encrypt_master_key(&master_key)?;

    let stored = StoredAuth {
        version: 1,
        login_hash,
        master_key_encrypted,
    };

    // Write to disk
    let path = auth_file_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create keys dir: {}", e))?;
    }
    let json = serde_json::to_string_pretty(&stored)
        .map_err(|e| format!("Serialization failed: {}", e))?;
    std::fs::write(&path, &json)
        .map_err(|e| format!("Failed to write auth.json: {}", e))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = std::fs::metadata(&path) {
            let mut perms = meta.permissions();
            perms.set_mode(0o600);
            let _ = std::fs::set_permissions(&path, perms);
        }
    }

    // Update in-memory state
    state.is_setup.store(true, std::sync::atomic::Ordering::Relaxed);
    state.needs_rebind.store(false, std::sync::atomic::Ordering::Relaxed);
    if let Ok(mut mk) = state.master_key.lock() {
        *mk = Some(master_key);
    }

    println!("[auth] ✅ Auth setup complete");
    Ok(master_key)
}

/// Verify login password against stored hash.
pub fn verify_login(login_password: &str) -> Result<bool, String> {
    let path = auth_file_path();
    if !path.exists() {
        return Err("Auth not set up".to_string());
    }
    let json = std::fs::read_to_string(&path)
        .map_err(|e| format!("Failed to read auth.json: {}", e))?;
    let stored: StoredAuth = serde_json::from_str(&json)
        .map_err(|e| format!("Failed to parse auth.json: {}", e))?;
    Ok(verify_login_password(login_password, &stored.login_hash))
}

/// Re-bind master_key to a new machine (hardware migration).
/// User must provide master_password to prove ownership.
pub fn rebind_to_machine(
    state: &AuthState,
    master_password: &str,
) -> Result<(), String> {
    // We need to verify master_password is correct by trying to decrypt the identity
    // For now: derive master_key and re-store it under new machine-id
    // The caller must verify the password was correct (identity decryption success)

    let path = auth_file_path();
    let json = std::fs::read_to_string(&path)
        .map_err(|e| format!("Failed to read auth.json: {}", e))?;
    let mut stored: StoredAuth = serde_json::from_str(&json)
        .map_err(|e| format!("Failed to parse auth.json: {}", e))?;

    // Derive master_key from provided master_password
    // We can't verify it without the old machine-id, so we trust the user
    // (they will know if identity decryption fails)
    let mut master_salt = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut master_salt);
    let master_key = derive_key(master_password.as_bytes(), &master_salt)?;

    // Re-encrypt master_key with new machine-id
    stored.master_key_encrypted = encrypt_master_key(&master_key)?;

    let new_json = serde_json::to_string_pretty(&stored)
        .map_err(|e| format!("Serialization failed: {}", e))?;
    std::fs::write(&path, &new_json)
        .map_err(|e| format!("Failed to write auth.json: {}", e))?;

    state.needs_rebind.store(false, std::sync::atomic::Ordering::Relaxed);
    if let Ok(mut mk) = state.master_key.lock() {
        *mk = Some(master_key);
    }

    println!("[auth] ✅ Master key rebound to new machine");
    Ok(())
}

/// Extract session token from Cookie header value.
pub fn extract_session_token(cookie_header: &str) -> Option<String> {
    for part in cookie_header.split(';') {
        let part = part.trim();
        if let Some(val) = part.strip_prefix(&format!("{}=", SESSION_COOKIE)) {
            return Some(val.to_string());
        }
    }
    None
}

/// Build a Set-Cookie header value for the session token.
pub fn make_session_cookie(token: &str, remember_me: bool) -> String {
    if remember_me {
        format!(
            "{}={}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}",
            SESSION_COOKIE, token, SESSION_REMEMBER_SECS
        )
    } else {
        format!(
            "{}={}; HttpOnly; SameSite=Strict; Path=/",
            SESSION_COOKIE, token
        )
    }
}

/// Build a Set-Cookie header that clears the session.
pub fn clear_session_cookie() -> String {
    format!(
        "{}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
        SESSION_COOKIE
    )
}

#[cfg(test)]
mod login_throttle_tests {
    use super::*;

    /// Live exploit + regression test: the web login endpoint had no rate
    /// limiting at all — combined with a real 0.0.0.0 default bind and a
    /// login password allowed to be just 4 characters, this was an
    /// unlimited-speed online brute-force surface. Proves the schedule is
    /// free for a few genuine typos, then actually slows things down, and
    /// that a real success clears it — the legitimate owner is never
    /// permanently locked out.
    #[test]
    fn backoff_schedule_is_free_then_grows_then_caps() {
        assert_eq!(LoginAttempts::backoff_after(0), Duration::ZERO);
        assert_eq!(LoginAttempts::backoff_after(LOGIN_FREE_ATTEMPTS), Duration::ZERO, "free attempts must not be throttled");
        assert_eq!(LoginAttempts::backoff_after(LOGIN_FREE_ATTEMPTS + 1), Duration::from_secs(2));
        assert_eq!(LoginAttempts::backoff_after(LOGIN_FREE_ATTEMPTS + 2), Duration::from_secs(4));
        assert_eq!(LoginAttempts::backoff_after(LOGIN_FREE_ATTEMPTS + 3), Duration::from_secs(8));
        // Must cap, not overflow or grow unbounded, under a very long attack.
        assert_eq!(LoginAttempts::backoff_after(LOGIN_FREE_ATTEMPTS + 30), Duration::from_secs(LOGIN_BACKOFF_MAX_SECS));
    }

    #[test]
    fn live_brute_force_is_throttled_but_real_success_recovers_immediately() {
        let state = AuthState::default();

        // A few genuine mistakes (e.g. real typos) must never be throttled.
        assert!(state.login_backoff_remaining().is_none());
        for _ in 0..LOGIN_FREE_ATTEMPTS {
            state.record_login_failure();
            assert!(state.login_backoff_remaining().is_none(), "free attempts must not be throttled");
        }

        // The next failure (an attacker guessing fast) must now be throttled.
        state.record_login_failure();
        let remaining = state.login_backoff_remaining();
        assert!(remaining.is_some(), "REPLAY/BRUTE-FORCE SUCCEEDED: no throttle after repeated failures");
        assert!(remaining.unwrap() <= Duration::from_secs(2));

        // A real, correct password must be accepted immediately by the
        // password check itself (the caller is expected to check
        // login_backoff_remaining() BEFORE calling verify_login — this
        // only proves record_login_success() fully clears the throttle
        // once that real success happens).
        state.record_login_success();
        assert!(state.login_backoff_remaining().is_none(), "a real success must fully clear the throttle, not just pause it");

        // Attacking again afterward must still work from a clean slate —
        // success must not have disabled throttling for future abuse.
        for _ in 0..LOGIN_FREE_ATTEMPTS {
            state.record_login_failure();
        }
        state.record_login_failure();
        assert!(state.login_backoff_remaining().is_some(), "throttle must re-arm after a fresh run of failures post-success");
    }

    #[test]
    fn backoff_actually_expires_after_waiting() {
        let state = AuthState::default();
        for _ in 0..=LOGIN_FREE_ATTEMPTS {
            state.record_login_failure();
        }
        assert!(state.login_backoff_remaining().is_some());
        std::thread::sleep(Duration::from_secs(2) + Duration::from_millis(100));
        assert!(state.login_backoff_remaining().is_none(), "backoff must actually expire, not block forever");
    }
}
