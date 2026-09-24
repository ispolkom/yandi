//! Мост для дифференциальных тестов: `yandi_llm.call(имя, json_аргументов) -> json_результата`. Не для боевого использования.

use pyo3::prelude::*;
use serde_json::{json, Map, Value};

use crate::messages::{append_system_instruction, build_messages, SystemArg};
use crate::ollama::{self, OllamaParams};
use crate::remote::{self, GenerateParams};
use crate::transport::ReqwestTransport;
use crate::semantic::*;
use crate::types::*;
use crate::vector_space::*;

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
        "ollama_generate" => {
            let msgs: Vec<Value> = match a("messages") {
                Value::Array(m) => m,
                _ => vec![],
            };
            let p = OllamaParams {
                temperature: a("temperature").as_f64(),
                max_tokens: a("max_tokens").as_i64(),
                timeout: a("timeout").as_u64().unwrap_or(0),
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
            };
            let t = ReqwestTransport::new();
            match ollama::generate(&t, &msgs, a("model").as_str().unwrap_or(""), a("base_url").as_str().unwrap_or(""), &p) {
                Ok((text, raw)) => json!({"ok": {"text": text, "raw": raw}}),
                Err(e) => json!({"error": e.0}),
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
