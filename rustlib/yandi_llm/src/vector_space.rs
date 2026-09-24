//! llm_gateway/vector_space.py — идентичность embedding-пространства и проверка совместимости векторов перед сравнением.
//! Одинаковая размерность НЕ означает одинаковое пространство: сравнивать (cosine/dot) можно только доказанно совместимые векторы.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

pub const GATEWAY_EMBED_SCHEMA_VERSION: i64 = 1;
/// Вектор с недоказуемым происхождением: никогда не совместим ни с чем — даже с самим собой.
pub const UNKNOWN_VECTOR_SPACE: &str = "UNKNOWN_VECTOR_SPACE";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VectorSpaceId {
    pub backend: String,
    pub protocol: String,
    pub model: String,
    pub dimension: i64,
    pub normalized: bool,
    #[serde(default = "default_version")]
    pub schema_version: i64,
}

fn default_version() -> i64 {
    GATEWAY_EMBED_SCHEMA_VERSION
}

impl VectorSpaceId {
    /// Первые 32 hex-символа SHA-256 от всех полей: для хранения и сравнения на равенство (не криптографический секрет).
    pub fn fingerprint(&self) -> String {
        let raw = format!(
            "{}|{}|{}|{}|{}|{}",
            self.schema_version, self.backend, self.protocol, self.model, self.dimension, if self.normalized { "norm" } else { "raw" }
        );
        let mut h = Sha256::new();
        h.update(raw.as_bytes());
        let out = h.finalize();
        out.iter().map(|b| format!("{:02x}", b)).collect::<String>()[..32].to_string()
    }

    pub fn to_dict(&self) -> Value {
        json!({
            "backend": self.backend,
            "protocol": self.protocol,
            "model": self.model,
            "dimension": self.dimension,
            "normalized": self.normalized,
            "schema_version": self.schema_version,
            "fingerprint": self.fingerprint(),
        })
    }
}

/// Сторона сравнения: пространство, готовый отпечаток или «нет».
#[derive(Debug, Clone)]
pub enum Space {
    None,
    Id(VectorSpaceId),
    Fingerprint(String),
}

/// `compatible`: единственная точка решения «можно ли сравнивать эти два вектора». None/UNKNOWN на ЛЮБОЙ стороне — всегда false.
pub fn compatible(a: &Space, b: &Space) -> bool {
    let fp = |s: &Space| -> Option<String> {
        match s {
            Space::None => None,
            Space::Fingerprint(f) if f == UNKNOWN_VECTOR_SPACE => None,
            Space::Fingerprint(f) => Some(f.clone()),
            Space::Id(i) => Some(i.fingerprint()),
        }
    };
    match (fp(a), fp(b)) {
        (Some(x), Some(y)) => x == y,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(model: &str, dim: i64) -> VectorSpaceId {
        VectorSpaceId { backend: "llamacpp".into(), protocol: "p".into(), model: model.into(), dimension: dim, normalized: false, schema_version: 1 }
    }

    #[test]
    fn compat() {
        assert!(compatible(&Space::Id(id("m", 3)), &Space::Id(id("m", 3))));
        assert!(!compatible(&Space::Id(id("m", 3)), &Space::Id(id("m", 4))));
        assert!(!compatible(&Space::None, &Space::None));
        let u = Space::Fingerprint(UNKNOWN_VECTOR_SPACE.into());
        assert!(!compatible(&u, &u));
        assert_eq!(id("m", 3).fingerprint().len(), 32);
    }
}
