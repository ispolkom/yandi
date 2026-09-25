//! Мост для дифференциальных тестов: `yandi_llm.call(имя, json_аргументов) -> json_результата`. Не для боевого использования.

use pyo3::prelude::*;
use serde_json::{json, Map, Value};

use crate::messages::{append_system_instruction, build_messages, SystemArg};
use crate::remote::{self, GenerateParams};
use crate::transport::ReqwestTransport;
use crate::secure_store as store;
use crate::client::{self, ConfigSource, CompleteParams, Gateway, GatewayOptions, ModelSpec, UnavailableEngine};
use crate::semantic::*;
use crate::types::*;
use crate::vector_space::*;

/// Настройки для тестов: карта «имя → запись»; запись `{"__raise__": "текст"}` имитирует исключение хранилища.
struct MockConfig(Map<String, Value>);

impl ConfigSource for MockConfig {
    fn get_model_entry(&self, model: &str) -> Result<Option<Value>, String> {
        match self.0.get(model) {
            None => Ok(None),
            Some(Value::Object(o)) if o.contains_key("__raise__") => Err(o["__raise__"].as_str().unwrap_or("").to_string()),
            Some(v) => Ok(Some(v.clone())),
        }
    }
}

fn gateway_parts(a: &dyn Fn(&str) -> Value) -> (MockConfig, UnavailableEngine, GatewayOptions) {
    let cfg = MockConfig(match a("config") {
        Value::Object(o) => o,
        _ => Map::new(),
    });
    let mut eng = UnavailableEngine::new(a("engine_reason").as_str().unwrap_or("No module named 'llama_cpp'"));
    if let Value::Object(reg) = a("registry") {
        for (name, path) in reg {
            eng.registry.push((name, ModelSpec { path: path.as_str().unwrap_or("").to_string(), n_ctx: 8192, n_gpu_layers: -1 }));
        }
    }
    let opts = GatewayOptions { default_base_url: a("default_base_url").as_str().unwrap_or(client::DEFAULT_BASE_URL).to_string(), local_enabled: a("local_enabled").as_bool().unwrap_or(false) };
    (cfg, eng, opts)
}

fn complete_params(a: &dyn Fn(&str) -> Value) -> CompleteParams {
    CompleteParams {
        temperature: a("temperature").as_f64(),
        max_tokens: a("max_tokens").as_i64(),
        timeout: a("timeout").as_u64().unwrap_or(client::DEFAULT_TIMEOUT),
        strip_think: a("strip_think").as_bool().unwrap_or(true),
        extra_options: match a("extra_options") {
            Value::Object(o) => Some(o),
            _ => None,
        },
        response_format: match a("response_format") {
            Value::Null => None,
            v => Some(v),
        },
        stop: match a("stop") {
            Value::Array(x) => Some(x.iter().filter_map(|v| v.as_str().map(String::from)).collect()),
            _ => None,
        },
    }
}

fn system_arg(v: &Value) -> SystemArg {
    match v {
        Value::String(s) => SystemArg::One(s.clone()),
        Value::Array(a) => SystemArg::Many(a.iter().filter_map(|x| x.as_str().map(String::from)).collect()),
        _ => SystemArg::None,
    }
}

fn dispatch(name: &str, args: &Value) -> Result<Value, String> {
    let a = |k: &str| args.get(k).cloned().unwrap_or(Value::Null);
    let req = || -> Result<SemanticOutputRequirement, String> { serde_json::from_value(a("requirement")).map_err(|e| e.to_string()) };
    let caps = || -> Result<BackendCapabilities, String> { serde_json::from_value(a("capabilities")).map_err(|e| e.to_string()) };
    Ok(match name {
        "build_messages" => {
            let msgs = match a("messages") {
                Value::Array(m) => Some(m),
                _ => None,
            };
            let prompt = a("prompt");
            json!(build_messages(prompt.as_str(), &system_arg(&a("system")), msgs.as_deref()))
        }
        "append_system_instruction" => append_system_instruction(&system_arg(&a("system")), a("instruction").as_str().unwrap_or("")),
        "contract_from_response_format" => {
            let rf = a("response_format");
            let c = contract_from_response_format(if rf.is_null() { None } else { Some(&rf) });
            json!({"name": c.name, "response_format": c.response_format})
        }
        "strip_think_blocks" => json!(strip_think_blocks(a("text").as_str().unwrap_or(""))),
        "looks_like_internal_state_fragment" => json!(looks_like_internal_state_fragment(a("text").as_str().unwrap_or(""))),
        "state_schema_prompt_hint" => {
            let s = a("state_schema");
            json!(state_schema_prompt_hint(if s.is_null() { None } else { Some(&s) }))
        }
        "semantic_result_schema" => semantic_result_schema(&req()?),
        "semantic_contract_from_target" => match semantic_contract_from_target(&req()?, &caps()?) {
            Ok((c, instr)) => json!({"contract": {"name": c.name, "response_format": c.response_format}, "instruction": instr}),
            Err(e) => json!({"error": e.0}),
        },
        "state_valid_for_requirement" => {
            let st = a("state");
            json!(state_valid_for_requirement(if st.is_null() { None } else { Some(&st) }, &req()?))
        }
        "normalize_semantic_completion" => {
            let contract: OutputContract = serde_json::from_value(a("contract")).map_err(|e| e.to_string())?;
            let meta: Map<String, Value> = match a("metadata") {
                Value::Object(m) => m,
                _ => Map::new(),
            };
            let r = normalize_semantic_completion(a("content").as_str().unwrap_or(""), &req()?, &contract, &meta);
            serde_json::to_value(r).map_err(|e| e.to_string())?
        }
        "vector_fingerprint" => {
            let id: VectorSpaceId = serde_json::from_value(a("id")).map_err(|e| e.to_string())?;
            id.to_dict()
        }
        "vector_compatible" => {
            let conv = |v: Value| -> Result<Space, String> {
                Ok(match v {
                    Value::Null => Space::None,
                    Value::String(s) => Space::Fingerprint(s),
                    other => Space::Id(serde_json::from_value(other).map_err(|e| e.to_string())?),
                })
            };
            json!(compatible(&conv(a("a"))?, &conv(a("b"))?))
        }
        "remote_generate" => {
            let msgs: Vec<Value> = match a("messages") {
                Value::Array(m) => m,
                _ => vec![],
            };
            let p = GenerateParams {
                temperature: a("temperature").as_f64(),
                max_tokens: a("max_tokens").as_i64(),
                response_format: a("response_format").as_str().map(String::from),
                stop: match a("stop") {
                    Value::Array(x) => Some(x.iter().filter_map(|v| v.as_str().map(String::from)).collect()),
                    _ => None,
                },
                timeout: a("timeout").as_u64().unwrap_or(0),
            };
            let t = ReqwestTransport::new();
            match remote::generate(&t, &msgs, a("base_url").as_str().unwrap_or(""), a("protocol").as_str().unwrap_or(""), a("model").as_str().unwrap_or(""), a("api_key_env").as_str(), &p) {
                Ok((text, meta)) => json!({"ok": {"text": text, "meta": meta}}),
                Err(e) => json!({"error": e.0}),
            }
        }
        "store_get" => match store::get_model_entry(a("model").as_str().unwrap_or("")) {
            Ok(v) => json!({"ok": v}),
            Err(e) => json!({"error": {"kind": e.kind(), "msg": e.message()}}),
        },
        "store_set" => match store::set_model_entry(a("model").as_str().unwrap_or(""), &a("entry")) {
            Ok(()) => json!({"ok": null}),
            Err(e) => json!({"error": {"kind": e.kind(), "msg": e.message()}}),
        },
        "store_remove" => match store::remove_model_entry(a("model").as_str().unwrap_or("")) {
            Ok(b) => json!({"ok": b}),
            Err(e) => json!({"error": {"kind": e.kind(), "msg": e.message()}}),
        },
        "store_list" => match store::list_models() {
            Ok(m) => json!({"ok": m}),
            Err(e) => json!({"error": {"kind": e.kind(), "msg": e.message()}}),
        },
        "gw_complete" | "gw_complete_meta" | "gw_complete_semantic" => {
            let (cfg, eng, opts) = gateway_parts(&a);
            let t = ReqwestTransport::new();
            let gw = Gateway { transport: &t, config: &cfg, engine: &eng, opts };
            let msgs: Option<Vec<Value>> = match a("messages") {
                Value::Array(m) => Some(m),
                _ => None,
            };
            let prompt = a("prompt");
            let model = a("model");
            let base = a("base_url");
            let sys = system_arg(&a("system"));
            let p = complete_params(&a);
            let err = |class: &str, msg: String| json!({"error": {"class": class, "msg": msg}});
            match name {
                "gw_complete" => match gw.complete_with_raw(prompt.as_str(), model.as_str().unwrap_or(""), &sys, msgs.as_deref(), base.as_str().unwrap_or(""), &p) {
                    Ok((text, raw)) => json!({"ok": {"text": text, "raw": raw}}),
                    Err(e) => err("LLMError", e.0),
                },
                "gw_complete_meta" => match gw.complete_with_meta(prompt.as_str(), model.as_str().unwrap_or(""), &sys, msgs.as_deref(), base.as_str().unwrap_or(""), &p) {
                    Ok((text, truncated, count)) => json!({"ok": {"text": text, "truncated": truncated, "token_count": count}}),
                    Err(e) => err("LLMError", e.0),
                },
                _ => {
                    let req: SemanticOutputRequirement = serde_json::from_value(a("requirement")).map_err(|e| e.to_string())?;
                    match gw.complete_semantic(prompt.as_str(), model.as_str().unwrap_or(""), &req, &sys, msgs.as_deref(), base.as_str().unwrap_or(""), &p) {
                        Ok(r) => json!({"ok": serde_json::to_value(r).map_err(|e| e.to_string())?}),
                        Err(e) => err("LLMError", e.0),
                    }
                }
            }
        }
        "location_kind" => json!(client::location_kind(&a("location").as_str().map(String::from))),
        "gw_embed" => {
            let (cfg, eng, opts) = gateway_parts(&a);
            let t = ReqwestTransport::new();
            let gw = Gateway { transport: &t, config: &cfg, engine: &eng, opts };
            let texts: Vec<String> = match a("texts") {
                Value::Array(x) => x.iter().filter_map(|v| v.as_str().map(String::from)).collect(),
                Value::String(s) => vec![s],
                _ => vec![],
            };
            match gw.embed(&texts, a("model").as_str().unwrap_or(""), a("base_url").as_str().unwrap_or(""), a("timeout").as_u64().unwrap_or(client::DEFAULT_TIMEOUT)) {
                Ok(r) => json!({"ok": {"vectors": r.vectors, "space": r.space.to_dict()}}),
                Err(e) => json!({"error": {"class": "EmbedError", "msg": e.0}}),
            }
        }
        "remote_embed" => {
            let texts: Vec<String> = match a("texts") {
                Value::Array(x) => x.iter().filter_map(|v| v.as_str().map(String::from)).collect(),
                _ => vec![],
            };
            let t = ReqwestTransport::new();
            match remote::embed(&t, &texts, a("base_url").as_str().unwrap_or(""), a("protocol").as_str().unwrap_or(""), a("model").as_str().unwrap_or(""), a("api_key_env").as_str(), a("timeout").as_u64().unwrap_or(0)) {
                Ok((vectors, meta)) => json!({"ok": {"vectors": vectors, "meta": meta}}),
                Err(e) => json!({"error": e.0}),
            }
        }
        other => return Err(format!("неизвестная функция {other}")),
    })
}

#[pyfunction]
fn call(py: Python<'_>, name: &str, args_json: &str) -> PyResult<String> {
    let args: Value = serde_json::from_str(args_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    // GIL отпускается на время работы (сеть!): иначе тестовый HTTP-сервер на Python в соседнем потоке не сможет ответить
    let out = py.allow_threads(|| dispatch(name, &args)).map_err(pyo3::exceptions::PyValueError::new_err)?;
    Ok(serde_json::to_string(&out).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?)
}

#[pymodule]
fn yandi_llm(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(call, m)?)?;
    Ok(())
}
