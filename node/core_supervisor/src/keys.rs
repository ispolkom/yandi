use base64::Engine;
use hkdf::Hkdf;
use sha2::Sha256;
use zeroize::Zeroizing;

/// The only key-derivation context of contract v1 (section 5.2).
pub const CORE_CONTEXT: &str = "yandi/core/v1";

/// The key the Core receives: `HKDF-SHA256(master_key, info = "yandi/core/v1")`. It is derived, so the master key itself never
/// leaves the node, and it is wiped when dropped (best effort: Rust cannot promise more than that about copies the allocator or
/// the kernel may have made).
pub struct CoreKey(Zeroizing<[u8; 32]>);

impl CoreKey {
    /// Standard base64 with padding, the wire format of `POST /v1/unlock`.
    pub fn to_base64(&self) -> Zeroizing<String> {
        Zeroizing::new(base64::engine::general_purpose::STANDARD.encode(self.0.as_slice()))
    }

    pub fn as_bytes(&self) -> &[u8; 32] {
        &self.0
    }
}

impl std::fmt::Debug for CoreKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("CoreKey(<redacted>)")
    }
}

pub fn derive_core_key(master_key: &[u8; 32]) -> CoreKey {
    let mut okm = Zeroizing::new([0u8; 32]);
    Hkdf::<Sha256>::new(None, master_key)
        .expand(CORE_CONTEXT.as_bytes(), okm.as_mut_slice())
        .expect("32 bytes is a valid HKDF-SHA256 output length");
    CoreKey(okm)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_the_python_core_library_vector() {
        // Computed with cryptography's HKDF (the library the Python core uses): HKDF(SHA256, salt=None, info=b"yandi/core/v1")
        // over the master key bytes 0..32.
        let mut master = [0u8; 32];
        for (i, b) in master.iter_mut().enumerate() {
            *b = i as u8;
        }
        let key = derive_core_key(&master);
        assert_eq!(
            hex::encode(key.as_bytes()),
            "5aab27467b165155474d411e443967114e5681074b3d95397715a5795734c8aa"
        );
        assert_eq!(
            key.to_base64().as_str(),
            "WqsnRnsWUVVHTUEeRDlnEU5WgQdLPZU5dxWleVc0yKo="
        );
    }

    #[test]
    fn the_derived_key_is_not_the_master_key() {
        let master = [7u8; 32];
        assert_ne!(derive_core_key(&master).as_bytes(), &master);
    }

    #[test]
    fn debug_never_prints_the_key() {
        let key = derive_core_key(&[1u8; 32]);
        assert!(!format!("{key:?}").contains(&hex::encode(key.as_bytes())));
    }
}
