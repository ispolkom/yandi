//! llm_gateway/client.py + adapters.py — единая точка вызова языковой модели: выбор цели (`resolve_target`), генерация (`complete`, `complete_with_meta`,
//! `complete_semantic`) и эмбеддинги (`embed`).
//!
//! ГЛАВНЫЙ ИНВАРИАНТ (сохранён дословно): явный выбор владельца узла сильнее любого автоматического отката. Если владелец настроил backend для ТОЧНОГО имени модели
//! (secure_store), есть только два исхода — этот backend ответил, либо наружу выходит честная ошибка ИМЕННО этого backend'а. Никакого тихого перехода на встроенный движок или что-либо ещё; проверяется ВСЕГДА, независимо от флага `local_enabled`. Флаг управляет ТОЛЬКО автоматической загрузкой встроенного движка,
//! когда владелец НИЧЕГО явно не настраивал. Эмбеддинги — отдельная способность узла с тем же инвариантом.
//!
//! OLLAMA ИСКЛЮЧЁН ПОЛНОСТЬЮ (решение владельца, 2026-09-25: «даже как фолбэк — только свои инструменты»): ни бэкенда, ни отката, ни эмбеддингов через Ollama. Цели: явная настройка владельца
//! (remote OpenAI/Anthropic-совместимый сервер, включая собственный `llama-server` узла, или локальный GGUF), встроенный реестр + встроенный движок, либо явный `base_url`
//! (OpenAI-совместимый сервер, напр. `llama-server` соседнего узла). Иначе — честная ошибка «backend не настроен».
//!
//! Что подключается снаружи (трейты): `Transport` (HTTP), `ConfigSource` (secure_store), `LocalEngine` (встроенный движок llama.cpp; в Rust-варианте это будет
//! supervised-процесс `llama-server` + тот же OpenAI-протокол — пока `UnavailableEngine`, как `Llama is None` в оригинале).

use serde_json::{json, Map, Value};

use crate::messages::{append_system_instruction, build_messages, SystemArg};
use crate::pyfmt::py_truthy;
use crate::remote::{self, GenerateParams};
use crate::semantic::{contract_from_response_format, normalize_semantic_completion, semantic_contract_from_target};
use crate::transport::Transport;
use crate::types::{BackendCapabilities, LlmError, OutputContract, SemanticCompletionResult, SemanticOutputRequirement};
use crate::vector_space::VectorSpaceId;
use yandi_rs::py_text::{py_repr_str, py_strip};

pub const DEFAULT_BASE_URL: &str = "http://127.0.0.1:11434";
pub const DEFAULT_TIMEOUT: u64 = 180;

#[derive(Debug, Clone, PartialEq)]
pub struct EmbedError(pub String);

impl std::fmt::Display for EmbedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}
impl std::error::Error for EmbedError {}

// ---------------------------------------------------------------- внешние подключения

/// Источник настроек узла (`config.get_model_entry`): Err — текст исключения (`str(e)`).
pub trait ConfigSource {
    fn get_model_entry(&self, model: &str) -> Result<Option<Value>, String>;
}

/// Настройки из родного `secure_store`.
pub struct SecureStoreConfig;

impl ConfigSource for SecureStoreConfig {
    fn get_model_entry(&self, model: &str) -> Result<Option<Value>, String> {
        crate::secure_store::get_model_entry(model).map_err(|e| e.message().to_string())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ModelSpec {
    pub path: String,
    pub n_ctx: i64,
    pub n_gpu_layers: i64,
}

/// Встроенный движок (llama.cpp). Ошибки — тексты `RuntimeError` оригинала.
pub trait LocalEngine {
    fn registry_error(&self) -> Option<String>;
    fn has_model(&self, model: &str) -> bool;
    /// Спецификация встроенного реестра для имени (для эмбеддингов встроенной модели).
    fn registry_spec(&self, model: &str) -> Option<ModelSpec>;
    fn generate(&self, alias_or_spec: &LocalTarget, messages: &[Value], p: &LocalParams) -> Result<(String, Map<String, Value>), String>;
    fn embed(&self, spec: &ModelSpec, texts: &[String]) -> Result<(Vec<Value>, Map<String, Value>), String>;
}

#[derive(Debug, Clone)]
pub enum LocalTarget {
    Registry(String),
    Spec(ModelSpec),
}

#[derive(Debug, Clone, Default)]
pub struct LocalParams {
    pub temperature: Option<f64>,
    pub max_tokens: Option<i64>,
    pub response_format: Option<Value>,
    pub stop: Option<Vec<String>>,
    pub extra_options: Option<Map<String, Value>>,
}

/// Движок недоступен (как `Llama is None`): любая попытка — ошибка с причиной; реестр встроенных имён и проверка наличия файла работают.
pub struct UnavailableEngine {
    pub reason: String,
    pub registry: Vec<(String, ModelSpec)>,
}

impl UnavailableEngine {
    pub fn new(reason: &str) -> Self {
        UnavailableEngine { reason: reason.to_string(), registry: vec![] }
    }
}

impl LocalEngine for UnavailableEngine {
    fn registry_error(&self) -> Option<String> {
        Some(format!("llama_cpp не установлен или не импортируется: {}", self.reason))
    }
    fn has_model(&self, model: &str) -> bool {
        self.registry.iter().any(|(n, s)| n == model && std::path::Path::new(&s.path).exists())
    }
    fn registry_spec(&self, model: &str) -> Option<ModelSpec> {
        self.registry.iter().find(|(n, _)| n == model).map(|(_, s)| s.clone())
    }
    fn generate(&self, target: &LocalTarget, _m: &[Value], _p: &LocalParams) -> Result<(String, Map<String, Value>), String> {
        if let LocalTarget::Registry(alias) = target {
            if !self.registry.iter().any(|(n, _)| n == alias) {
                return Err(format!("нет встроенной GGUF-записи для модели {}", py_repr_str(alias)));
            }
        }
        Err(format!("llama_cpp недоступен: {}", self.reason))
    }
    fn embed(&self, _s: &ModelSpec, _t: &[String]) -> Result<(Vec<Value>, Map<String, Value>), String> {
        Err(format!("llama_cpp недоступен: {}", self.reason))
    }
}

#[derive(Debug, Clone)]
pub struct GatewayOptions {
    pub default_base_url: String,
    /// `LLM_GATEWAY_ENABLE_LOCAL`: управляет ТОЛЬКО автоматической загрузкой встроенного движка (STEP 2), не явной настройкой владельца
    pub local_enabled: bool,
}

impl GatewayOptions {
    pub fn from_env() -> Self {
        let v = std::env::var("LLM_GATEWAY_ENABLE_LOCAL").unwrap_or_default();
        GatewayOptions { default_base_url: DEFAULT_BASE_URL.to_string(), local_enabled: !(v.is_empty() || v == "0") }
    }
}

pub struct Gateway<'a> {
    pub transport: &'a dyn Transport,
    pub config: &'a dyn ConfigSource,
    pub engine: &'a dyn LocalEngine,
    pub opts: GatewayOptions,
}

// ---------------------------------------------------------------- цель генерации

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Adapter {
    LlamaCpp,
    OpenAiCompat,
    Anthropic,
}

impl Adapter {
    pub fn id(&self) -> &'static str {
        match self {
            Adapter::LlamaCpp => "llama_cpp",
            Adapter::OpenAiCompat => "openai_compatible",
            Adapter::Anthropic => "anthropic",
        }
    }
    pub fn capabilities(&self) -> BackendCapabilities {
        match self {
            Adapter::LlamaCpp => BackendCapabilities { json_object: true, json_schema: false, ..Default::default() },
            Adapter::OpenAiCompat => BackendCapabilities { json_object: true, json_schema: false, ..Default::default() },
            Adapter::Anthropic => BackendCapabilities { json_object: false, json_schema: false, ..Default::default() },
        }
    }
}

#[derive(Debug, Clone)]
pub enum TargetData {
    Remote { base_url: String, api_key_env: Option<String> },
    Local(LocalTarget),
}

#[derive(Debug, Clone)]
pub struct ResolvedTarget {
    pub logical_model: String,
    pub resolved_model: String,
    pub adapter: Adapter,
    pub capabilities: BackendCapabilities,
    pub resolution_reason: String,
    pub attempt: u32,
    pub source: &'static str,
    pub location: Option<String>,
    pub runtime: Option<&'static str>,
    pub provider: Option<&'static str>,
    pub config_ref: Option<String>,
    pub fallback_reason: Option<String>,
    pub target: TargetData,
}

fn key_error(key: &str) -> String {
    py_repr_str(key) // str(KeyError('x')) == "'x'"
}

fn entry_str(entry: &Map<String, Value>, key: &str) -> Result<String, String> {
    match entry.get(key) {
        Some(Value::String(s)) => Ok(s.clone()),
        Some(other) => Ok(crate::pyfmt::py_str(other)),
        None => Err(key_error(key)),
    }
}

/// Явный `base_url` ≠ по умолчанию: OpenAI-совместимый сервер по этому адресу (напр. `llama-server` соседнего узла).
fn explicit_url_target(model: &str, base_url: &str, attempt: u32) -> ResolvedTarget {
    ResolvedTarget {
        logical_model: model.to_string(),
        resolved_model: model.to_string(),
        adapter: Adapter::OpenAiCompat,
        capabilities: Adapter::OpenAiCompat.capabilities(),
        resolution_reason: "explicit non-default base_url normalized as OpenAI-compatible target".to_string(),
        attempt,
        source: "explicit_base_url",
        location: Some(base_url.to_string()),
        runtime: Some("external_server"),
        provider: Some("openai_compatible"),
        config_ref: Some("explicit_base_url".to_string()),
        fallback_reason: None,
        target: TargetData::Remote { base_url: base_url.to_string(), api_key_env: None },
    }
}

impl<'a> Gateway<'a> {
    /// `resolve_target`: ровно ОДНА цель для ОДНОЙ попытки; только решает личность и возможности, ничего не генерирует.
    /// Err — текст исключения оригинала (`RuntimeError`/`KeyError`/исключение хранилища).
    pub fn resolve_target(&self, model: &str, base_url: &str, attempt: u32) -> Result<ResolvedTarget, String> {
        if base_url != self.opts.default_base_url {
            return Ok(explicit_url_target(model, base_url, attempt));
        }
        if let Some(entry) = self.config.get_model_entry(model)? {
            let obj = match &entry {
                Value::Object(o) => o.clone(),
                _ => return Err("'NoneType' object has no attribute 'get'".to_string()),
            };
            let backend = obj.get("backend").cloned().unwrap_or(Value::Null);
            match backend.as_str() {
                Some("llamacpp") => {
                    let path = entry_str(&obj, "path")?;
                    let spec = ModelSpec {
                        path: path.clone(),
                        n_ctx: obj.get("n_ctx").and_then(|v| v.as_i64()).unwrap_or(8192),
                        n_gpu_layers: obj.get("n_gpu_layers").and_then(|v| v.as_i64()).unwrap_or(-1),
                    };
                    return Ok(ResolvedTarget {
                        logical_model: model.to_string(),
                        resolved_model: model.to_string(),
                        adapter: Adapter::LlamaCpp,
                        capabilities: Adapter::LlamaCpp.capabilities(),
                        resolution_reason: "legacy explicit node config normalized from backend=llamacpp".to_string(),
                        attempt,
                        source: "explicit_config",
                        location: Some(path),
                        runtime: Some("llama_cpp"),
                        provider: Some("local"),
                        config_ref: Some(format!("secure_store:{model}")),
                        fallback_reason: None,
                        target: TargetData::Local(LocalTarget::Spec(spec)),
                    });
                }
                Some("remote") => {
                    let protocol = match obj.get("protocol") {
                        None => "openai".to_string(),
                        Some(v) => crate::pyfmt::py_str(v),
                    };
                    let adapter = match protocol.as_str() {
                        "openai" => Adapter::OpenAiCompat,
                        "anthropic" => Adapter::Anthropic,
                        _ => return Err(format!("неизвестный remote protocol {} в настройке узла для модели {}", py_repr_str(&protocol), py_repr_str(model))),
                    };
                    let base = entry_str(&obj, "base_url")?;
                    let api_key_env = match obj.get("api_key_env") {
                        Some(Value::String(s)) => Some(s.clone()),
                        _ => None,
                    };
                    let resolved_model = match obj.get("model") {
                        Some(v) => crate::pyfmt::py_str(v),
                        None => model.to_string(),
                    };
                    let is_anth = protocol == "anthropic";
                    return Ok(ResolvedTarget {
                        logical_model: model.to_string(),
                        resolved_model,
                        adapter,
                        capabilities: adapter.capabilities(),
                        resolution_reason: format!("legacy explicit node config normalized from backend=remote protocol={protocol}"),
                        attempt,
                        source: "explicit_config",
                        location: Some(base.clone()),
                        runtime: Some(if is_anth { "provider" } else { "external_server" }),
                        provider: Some(if is_anth { "anthropic" } else { "openai_compatible" }),
                        config_ref: Some(format!("secure_store:{model}")),
                        fallback_reason: None,
                        target: TargetData::Remote { base_url: base, api_key_env },
                    });
                }
                _ => {
                    let shown = match &backend {
                        Value::String(s) => py_repr_str(s),
                        other => crate::pyfmt::py_repr(other),
                    };
                    return Err(format!("неизвестный backend {} в настройке узла для модели {}", shown, py_repr_str(model)));
                }
            }
        }
        if self.opts.local_enabled && self.engine.has_model(model) {
            return Ok(ResolvedTarget {
                logical_model: model.to_string(),
                resolved_model: model.to_string(),
                adapter: Adapter::LlamaCpp,
                capabilities: Adapter::LlamaCpp.capabilities(),
                resolution_reason: "no explicit config; local engine enabled and builtin registry has model".to_string(),
                attempt,
                source: "builtin_registry",
                location: None,
                runtime: Some("llama_cpp"),
                provider: Some("local"),
                config_ref: Some("builtin_registry".to_string()),
                fallback_reason: None,
                target: TargetData::Local(LocalTarget::Registry(model.to_string())),
            });
        }
        Err(format!(
            "для модели {} не настроен ни один backend: нет явной настройки владельца узла (llm_gateway.setup / secure_store) и нет во встроенном реестре с работающим локальным движком — внешние источники (в т.ч. Ollama) не используются",
            py_repr_str(model)
        ))
    }
}

// ---------------------------------------------------------------- генерация

/// Ошибка попытки: класс исключения оригинала (для трассы) и текст.
#[derive(Debug, Clone, PartialEq)]
pub struct AttemptError {
    pub class: &'static str,
    pub message: String,
}

pub fn location_kind(loc: &Option<String>) -> &'static str {
    match loc {
        None => "none",
        Some(l) if l.is_empty() => "none",
        Some(l) if l.starts_with("http://") || l.starts_with("https://") => "url",
        Some(_) => "file",
    }
}

/// `_trace_attempt`
pub fn trace_attempt(t: &ResolvedTarget, contract: &OutputContract, result: &str, error: Option<&AttemptError>) -> Value {
    let mut m = Map::new();
    m.insert("attempt".into(), json!(t.attempt));
    m.insert("logical_model".into(), json!(t.logical_model));
    m.insert("resolved_model".into(), json!(t.resolved_model));
    m.insert("adapter_id".into(), json!(t.adapter.id()));
    m.insert("runtime".into(), json!(t.runtime));
    m.insert("provider".into(), json!(t.provider));
    m.insert("location_kind".into(), json!(location_kind(&t.location)));
    m.insert("source".into(), json!(t.source));
    m.insert("resolution_reason".into(), json!(t.resolution_reason));
    m.insert("fallback_reason".into(), json!(t.fallback_reason));
    m.insert(
        "capabilities".into(),
        json!({"plain_text": t.capabilities.plain_text, "json_object": t.capabilities.json_object, "json_schema": t.capabilities.json_schema, "streaming": t.capabilities.streaming}),
    );
    m.insert("output_contract".into(), json!(contract.name));
    m.insert("result".into(), json!(result));
    if let Some(e) = error {
        m.insert("error_class".into(), json!(e.class));
        m.insert("error".into(), json!(e.message));
    }
    Value::Object(m)
}

#[derive(Debug, Clone, Default)]
pub struct CompleteParams {
    pub temperature: Option<f64>,
    pub max_tokens: Option<i64>,
    pub timeout: u64,
    pub strip_think: bool,
    pub extra_options: Option<Map<String, Value>>,
    pub response_format: Option<Value>,
    pub stop: Option<Vec<String>>,
}

impl CompleteParams {
    pub fn new() -> Self {
        CompleteParams { timeout: DEFAULT_TIMEOUT, strip_think: true, ..Default::default() }
    }
}

fn explicit_failed(model: &str, cause: &str) -> LlmError {
    LlmError(format!(
        "настроенный владельцем узла backend для модели {} не сработал — автоматический переход на другой источник интеллекта запрещён явным выбором владельца: {}",
        py_repr_str(model),
        cause
    ))
}

impl<'a> Gateway<'a> {
    /// `_generate_with_target`
    fn generate_with_target(
        &self, target: &ResolvedTarget, prompt: Option<&str>, system: &SystemArg, messages: Option<&[Value]>, p: &CompleteParams, contract: &OutputContract,
    ) -> Result<(String, Map<String, Value>), AttemptError> {
        let wire = build_messages(prompt, system, messages);
        let model = target.resolved_model.as_str();
        match (&target.adapter, &target.target) {
            (Adapter::OpenAiCompat, TargetData::Remote { base_url, api_key_env }) | (Adapter::Anthropic, TargetData::Remote { base_url, api_key_env }) => {
                let protocol = if target.adapter == Adapter::Anthropic { "anthropic" } else { "openai" };
                let gp = GenerateParams {
                    temperature: p.temperature,
                    max_tokens: p.max_tokens,
                    response_format: match &contract.response_format {
                        Some(Value::String(s)) => Some(s.clone()),
                        _ => None,
                    },
                    stop: p.stop.clone(),
                    timeout: p.timeout,
                };
                remote::generate(self.transport, &wire, base_url, protocol, model, api_key_env.as_deref(), &gp)
                    .map_err(|e| AttemptError { class: "RemoteBackendError", message: e.0 })
            }
            (Adapter::LlamaCpp, TargetData::Local(lt)) => {
                let lp = LocalParams {
                    temperature: p.temperature,
                    max_tokens: p.max_tokens,
                    response_format: contract.response_format.clone(),
                    stop: p.stop.clone(),
                    extra_options: p.extra_options.clone(),
                };
                self.engine.generate(lt, &wire, &lp).map_err(|m| AttemptError { class: "RuntimeError", message: m })
            }
            _ => Err(AttemptError { class: "RuntimeError", message: "внутренняя ошибка: адаптер не соответствует цели".to_string() }),
        }
    }

    fn check_inputs(&self, fname: &str, prompt: Option<&str>, messages: Option<&[Value]>) -> Result<(), LlmError> {
        if prompt.is_none() && messages.is_none() {
            return Err(LlmError(format!("{fname}() требует либо prompt, либо messages — ни один не задан")));
        }
        if prompt.is_some() && messages.is_some() {
            return Err(LlmError(format!("{fname}() принимает либо prompt, либо messages, но не оба сразу")));
        }
        Ok(())
    }

    /// `_do_complete`: три чётких шага; возвращает очищенный текст и сырой словарь метаданных (с `_llm_gateway_trace`).
    pub fn complete_with_raw(
        &self, prompt: Option<&str>, model: &str, system: &SystemArg, messages: Option<&[Value]>, base_url: &str, p: &CompleteParams,
    ) -> Result<(String, Map<String, Value>), LlmError> {
        self.check_inputs("complete", prompt, messages)?;
        let mut trace: Vec<Value> = Vec::new();
        let contract = contract_from_response_format(p.response_format.as_ref());
        let target = self.resolve_target(model, base_url, 1).map_err(|e| explicit_failed(model, &e))?;
        let (text, raw) = match self.generate_with_target(&target, prompt, system, messages, p, &contract) {
            Ok(ok) => {
                trace.push(trace_attempt(&target, &contract, "success", None));
                ok
            }
            Err(e) => {
                trace.push(trace_attempt(&target, &contract, "failed", Some(&e)));
                if target.source == "explicit_config" {
                    // CONFIGURED_TARGET_FAILED — владелец явно выбрал этот target; переход на другой источник запрещён категорически
                    return Err(explicit_failed(model, &e.message));
                }
                // без автоматического отката (в т.ч. на Ollama — исключён): честная ошибка именно этой цели
                return Err(LlmError(e.message));
            }
        };
        let text = if p.strip_think { crate::semantic::strip_think_closed(&text) } else { text };
        let mut raw = raw;
        raw.insert("_llm_gateway_trace".into(), Value::Array(trace));
        Ok((py_strip(&text).to_string(), raw))
    }

    /// `complete`
    pub fn complete(&self, prompt: Option<&str>, model: &str, system: &SystemArg, messages: Option<&[Value]>, base_url: &str, p: &CompleteParams) -> Result<String, LlmError> {
        self.complete_with_raw(prompt, model, system, messages, base_url, p).map(|(t, _)| t)
    }

    /// `complete_with_meta`: (текст, обрезано ли лимитом токенов, число токенов)
    pub fn complete_with_meta(
        &self, prompt: Option<&str>, model: &str, system: &SystemArg, messages: Option<&[Value]>, base_url: &str, p: &CompleteParams,
    ) -> Result<(String, bool, Value), LlmError> {
        let (text, raw) = self.complete_with_raw(prompt, model, system, messages, base_url, p)?;
        let truncated = raw.get("done_reason") == Some(&json!("length"));
        let count = raw.get("eval_count").cloned().unwrap_or(Value::Null);
        Ok((text, truncated, count))
    }

    /// `complete_semantic`: семантическая нужда вызывающего (реплика + состояние) → шлюз сам выбирает контракт по возможностям цели и нормализует ответ.
    pub fn complete_semantic(
        &self, prompt: Option<&str>, model: &str, requirement: &SemanticOutputRequirement, system: &SystemArg, messages: Option<&[Value]>, base_url: &str,
        p: &CompleteParams,
    ) -> Result<SemanticCompletionResult, LlmError> {
        self.check_inputs("complete_semantic", prompt, messages)?;
        let mut trace: Vec<Value> = Vec::new();
        let target = self.resolve_target(model, base_url, 1).map_err(|e| explicit_failed(model, &e))?;
        let (contract, instruction) = semantic_contract_from_target(requirement, &target.capabilities)?;
        let sem_system = system_from_value(&append_system_instruction(system, &instruction));
        let first = self.generate_with_target(&target, prompt, &sem_system, messages, p, &contract);
        let (text, raw) = match first {
            Ok(ok) => {
                trace.push(trace_attempt(&target, &contract, "success", None));
                ok
            }
            Err(e) => {
                trace.push(trace_attempt(&target, &contract, "failed", Some(&e)));
                if target.source == "explicit_config" {
                    return Err(explicit_failed(model, &e.message));
                }
                return Err(LlmError(e.message));
            }
        };
        let mut raw = raw;
        raw.insert("_llm_gateway_trace".into(), Value::Array(trace));
        Ok(normalize_semantic_completion(&text, requirement, &contract, &raw))
    }
}

fn system_from_value(v: &Value) -> SystemArg {
    match v {
        Value::String(s) => SystemArg::One(s.clone()),
        Value::Array(a) => SystemArg::Many(a.iter().filter_map(|x| x.as_str().map(String::from)).collect()),
        _ => SystemArg::None,
    }
}

// ---------------------------------------------------------------- эмбеддинги

#[derive(Debug, Clone, PartialEq)]
pub struct EmbeddingResult {
    pub vectors: Vec<Value>,
    pub space: VectorSpaceId,
}

/// `_validate_embedding_batch`: проверка ДО того, как ответ дойдёт до вызывающего кода.
pub fn validate_embedding_batch(vectors: &[Value], expected_count: usize, context: &str) -> Result<(), EmbedError> {
    if vectors.is_empty() {
        return Err(EmbedError(format!("{context}: пустой или некорректный список векторов")));
    }
    if vectors.len() != expected_count {
        return Err(EmbedError(format!("{context}: batch вернул {} векторов вместо {} входных текстов", vectors.len(), expected_count)));
    }
    let mut dims: std::collections::BTreeSet<usize> = std::collections::BTreeSet::new();
    for (i, v) in vectors.iter().enumerate() {
        let arr = match v {
            Value::Array(a) if !a.is_empty() => a,
            _ => return Err(EmbedError(format!("{context}: вектор #{i} пуст или не является списком"))),
        };
        for x in arr {
            match x {
                Value::Number(_) => {}
                other => return Err(EmbedError(format!("{context}: вектор #{i} содержит нечисловое значение {}", crate::pyfmt::py_repr(other)))),
            }
        }
        dims.insert(arr.len());
    }
    if dims.len() > 1 {
        let d: Vec<String> = dims.iter().map(|d| d.to_string()).collect();
        return Err(EmbedError(format!("{context}: векторы одного batch'а разной размерности: [{}]", d.join(", "))));
    }
    Ok(())
}

impl<'a> Gateway<'a> {
    /// `_try_configured_embedding_backend`: None — для имени ничего не настроено (не ошибка). Err — текст исключения оригинала.
    fn try_configured_embedding(&self, model: &str, texts: &[String]) -> Result<Option<(Vec<Value>, VectorSpaceId)>, String> {
        let entry = match self.config.get_model_entry(model)? {
            None => return Ok(None),
            Some(Value::Object(o)) => o,
            Some(_) => return Err("'NoneType' object has no attribute 'get'".to_string()),
        };
        let backend = entry.get("backend").cloned().unwrap_or(Value::Null);
        let (vectors, meta, backend_kind, protocol, model_identity) = match backend.as_str() {
            Some("llamacpp") => {
                let path = entry_str(&entry, "path")?;
                let spec = ModelSpec {
                    path: path.clone(),
                    n_ctx: entry.get("n_ctx").and_then(|v| v.as_i64()).unwrap_or(8192),
                    n_gpu_layers: entry.get("n_gpu_layers").and_then(|v| v.as_i64()).unwrap_or(-1),
                };
                let (v, m) = self.engine.embed(&spec, texts)?;
                (v, m, "llamacpp".to_string(), "llamacpp-embed".to_string(), path)
            }
            Some("remote") => {
                let protocol_name = match entry.get("protocol") {
                    None => "openai".to_string(),
                    Some(v) => crate::pyfmt::py_str(v),
                };
                let base = entry_str(&entry, "base_url")?;
                let real_model = match entry.get("model") {
                    Some(v) => crate::pyfmt::py_str(v),
                    None => model.to_string(),
                };
                let api_key_env = match entry.get("api_key_env") {
                    Some(Value::String(s)) => Some(s.clone()),
                    _ => None,
                };
                let (v, m) = remote::embed(self.transport, texts, &base, &protocol_name, &real_model, api_key_env.as_deref(), remote::DEFAULT_TIMEOUT).map_err(|e| e.0)?;
                (v, m, "remote".to_string(), format!("remote-{protocol_name}-embeddings"), real_model)
            }
            _ => {
                let shown = match &backend {
                    Value::String(s) => py_repr_str(s),
                    other => crate::pyfmt::py_repr(other),
                };
                return Err(format!("неизвестный backend {} в настройке узла для embedding-модели {}", shown, py_repr_str(model)));
            }
        };
        let dimension = match meta.get("dimension") {
            Some(d) => d.as_i64().unwrap_or(0),
            None => {
                return Err(format!(
                    "backend {}, настроенный владельцем узла для embedding-модели {}, вернул некорректный результат вместо (векторы, метаданные)",
                    py_repr_str(&backend_kind), py_repr_str(model)
                ))
            }
        };
        validate_embedding_batch(&vectors, texts.len(), &format!("настроенный владельцем backend {} для {}", py_repr_str(&backend_kind), py_repr_str(model))).map_err(|e| e.0)?;
        let normalized = meta.get("normalized").map(py_truthy).unwrap_or(false);
        Ok(Some((vectors, VectorSpaceId { backend: backend_kind, protocol, model: model_identity, dimension, normalized, schema_version: 1 })))
    }

    /// Встроенный движок для эмбеддингов ИМЕНИ из встроенного реестра (свой инструмент вместо Ollama): та же модель, отдельный экземпляр в режиме эмбеддингов.
    fn embed_builtin(&self, texts: &[String], model: &str) -> Result<(Vec<Value>, VectorSpaceId), EmbedError> {
        let spec = self.engine.registry_spec(model).ok_or_else(|| EmbedError(format!("нет встроенной GGUF-записи для модели {}", py_repr_str(model))))?;
        let (vectors, meta) = self.engine.embed(&spec, texts).map_err(EmbedError)?;
        let dimension = meta.get("dimension").and_then(|d| d.as_i64()).unwrap_or(0);
        validate_embedding_batch(&vectors, texts.len(), &format!("встроенный движок для {}", py_repr_str(model)))?;
        let normalized = meta.get("normalized").map(py_truthy).unwrap_or(false);
        Ok((vectors, VectorSpaceId { backend: "llamacpp".into(), protocol: "llamacpp-embed".into(), model: spec.path, dimension, normalized, schema_version: 1 }))
    }

    /// `embed`: STEP 1 — явная настройка владельца (успех или честная ошибка, без перехода на другой источник); STEP 2 — имя из встроенного реестра при
    /// включённом локальном движке (`local_enabled`); иначе — ошибка «embedding-backend не настроен». OLLAMA ИСКЛЮЧЁН: никакого отката на внешний сервер.
    /// Только для локального base_url.
    pub fn embed(&self, texts: &[String], model: &str, base_url: &str, _timeout: u64) -> Result<EmbeddingResult, EmbedError> {
        if texts.is_empty() {
            return Err(EmbedError("embed() вызван с пустым списком текстов".to_string()));
        }
        if base_url != self.opts.default_base_url {
            return Err(EmbedError(format!(
                "embed() для внешнего base_url {} не поддерживается: эмбеддинги — только через явную настройку владельца узла или встроенный движок",
                py_repr_str(base_url)
            )));
        }
        match self.try_configured_embedding(model, texts) {
            Err(e) => {
                return Err(EmbedError(format!(
                    "настроенный владельцем узла embedding-backend для модели {} не сработал — автоматический переход на другой источник эмбеддингов запрещён явным выбором владельца: {}",
                    py_repr_str(model), e
                )))
            }
            Ok(Some((vectors, space))) => return Ok(EmbeddingResult { vectors, space }),
            Ok(None) => {}
        }
        if self.opts.local_enabled && self.engine.has_model(model) {
            let (vectors, space) = self.embed_builtin(texts, model)?;
            return Ok(EmbeddingResult { vectors, space });
        }
        Err(EmbedError(format!(
            "для модели {} не настроен embedding-backend: нет явной настройки владельца узла (llm_gateway.setup / secure_store) и нет во встроенном реестре с работающим локальным движком — внешние источники (в т.ч. Ollama) не используются",
            py_repr_str(model)
        )))
    }
}

#[cfg(test)]
mod tests {
    //! Родной шлюз БЕЗ Ollama: рабочий фальшивый движок + транспорт, который паникует при любом запросе
    //! (доказательство «сеть не трогалась»).
    use super::*;
    use crate::transport::{HttpRequest, HttpResponse, TransportError};

    struct NoNet;
    impl Transport for NoNet {
        fn post_json(&self, req: &HttpRequest, _t: u64) -> Result<HttpResponse, TransportError> {
            panic!("неожиданный сетевой запрос: {}", req.url);
        }
    }
    struct Cfg;
    impl ConfigSource for Cfg {
        fn get_model_entry(&self, _m: &str) -> Result<Option<Value>, String> {
            Ok(None)
        }
    }
    struct Fake;
    impl LocalEngine for Fake {
        fn registry_error(&self) -> Option<String> {
            None
        }
        fn has_model(&self, m: &str) -> bool {
            m == "b"
        }
        fn registry_spec(&self, m: &str) -> Option<ModelSpec> {
            (m == "b").then(|| ModelSpec { path: "/m/b.gguf".into(), n_ctx: 1, n_gpu_layers: 0 })
        }
        fn generate(&self, _t: &LocalTarget, _m: &[Value], _p: &LocalParams) -> Result<(String, Map<String, Value>), String> {
            Ok(("свой ответ".into(), Map::new()))
        }
        fn embed(&self, _s: &ModelSpec, texts: &[String]) -> Result<(Vec<Value>, Map<String, Value>), String> {
            let mut meta = Map::new();
            meta.insert("dimension".into(), json!(2));
            meta.insert("normalized".into(), json!(true));
            Ok((texts.iter().map(|_| json!([0.5, 0.25])).collect(), meta))
        }
    }

    fn gw<'a>(t: &'a NoNet, c: &'a Cfg, e: &'a Fake, local: bool) -> Gateway<'a> {
        Gateway { transport: t, config: c, engine: e, opts: GatewayOptions { default_base_url: DEFAULT_BASE_URL.into(), local_enabled: local } }
    }

    #[test]
    fn builtin_embed_uses_own_engine_without_network() {
        let (t, c, e) = (NoNet, Cfg, Fake);
        let r = gw(&t, &c, &e, true).embed(&["x".into(), "y".into()], "b", DEFAULT_BASE_URL, 5).unwrap();
        assert_eq!(r.vectors.len(), 2);
        assert_eq!(r.space.backend, "llamacpp");
        assert_eq!(r.space.protocol, "llamacpp-embed");
        assert_eq!(r.space.model, "/m/b.gguf");
        assert_eq!(r.space.dimension, 2);
        assert!(r.space.normalized);
    }

    #[test]
    fn embed_without_config_or_engine_is_honest_error_no_network() {
        let (t, c, e) = (NoNet, Cfg, Fake);
        // локальный движок выключен → ошибка, не Ollama
        let err = gw(&t, &c, &e, false).embed(&["x".into()], "b", DEFAULT_BASE_URL, 5).unwrap_err();
        assert!(err.0.contains("не настроен embedding-backend"), "{}", err.0);
        // модели нет в реестре → ошибка
        let err = gw(&t, &c, &e, true).embed(&["x".into()], "zzz", DEFAULT_BASE_URL, 5).unwrap_err();
        assert!(err.0.contains("не настроен embedding-backend"), "{}", err.0);
        // пустой список и внешний адрес
        let err = gw(&t, &c, &e, true).embed(&[], "b", DEFAULT_BASE_URL, 5).unwrap_err();
        assert!(err.0.contains("пустым списком"), "{}", err.0);
        let err = gw(&t, &c, &e, true).embed(&["x".into()], "b", "http://other:1", 5).unwrap_err();
        assert!(err.0.contains("не поддерживается"), "{}", err.0);
    }

    #[test]
    fn builtin_generation_works_and_unknown_model_never_reaches_network() {
        let (t, c, e) = (NoNet, Cfg, Fake);
        let p = CompleteParams { timeout: 5, strip_think: true, ..Default::default() };
        let sys = SystemArg::None;
        let ok = gw(&t, &c, &e, true).complete(Some("q"), "b", &sys, None, DEFAULT_BASE_URL, &p).unwrap();
        assert_eq!(ok, "свой ответ");
        // реестр выключен → честная ошибка «не настроен ни один backend»
        let err = gw(&t, &c, &e, false).complete(Some("q"), "b", &sys, None, DEFAULT_BASE_URL, &p).unwrap_err();
        assert!(err.0.contains("не настроен ни один backend"), "{}", err.0);
        let err = gw(&t, &c, &e, true).complete(Some("q"), "nope", &sys, None, DEFAULT_BASE_URL, &p).unwrap_err();
        assert!(err.0.contains("не настроен ни один backend"), "{}", err.0);
    }

    #[test]
    fn explicit_base_url_is_openai_compatible_target() {
        let (t, c, e) = (NoNet, Cfg, Fake);
        let r = gw(&t, &c, &e, false).resolve_target("m", "http://peer:9", 1).unwrap();
        assert_eq!(r.source, "explicit_base_url");
        assert_eq!(r.location.as_deref(), Some("http://peer:9"));
        assert!(matches!(r.adapter, Adapter::OpenAiCompat));
    }
}
