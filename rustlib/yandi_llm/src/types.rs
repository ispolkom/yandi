//! llm_gateway/types.py — общие типы передачи (нейтральный модуль: адаптеры и клиент импортируют его без циклов).

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BackendCapabilities {
    #[serde(default = "yes")]
    pub plain_text: bool,
    #[serde(default)]
    pub json_object: bool,
    #[serde(default)]
    pub json_schema: bool,
    #[serde(default)]
    pub streaming: bool,
}

fn yes() -> bool {
    true
}

impl Default for BackendCapabilities {
    fn default() -> Self {
        BackendCapabilities { plain_text: true, json_object: false, json_schema: false, streaming: false }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OutputContract {
    pub name: String,
    pub response_format: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SemanticOutputRequirement {
    pub kind: String,
    #[serde(default)]
    pub state_schema: Option<Value>,
    #[serde(default = "yes")]
    pub reply_required: bool,
    #[serde(default)]
    pub state_required: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SemanticCompletionResult {
    pub reply: String,
    pub state: Option<Value>,
    pub reply_ok: bool,
    pub state_ok: bool,
    pub parse_ok: bool,
    pub error: Option<String>,
    pub metadata: serde_json::Map<String, Value>,
}

/// Ошибка шлюза (`LLMError` в Python).
#[derive(Debug, Clone, PartialEq)]
pub struct LlmError(pub String);

impl std::fmt::Display for LlmError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl std::error::Error for LlmError {}
