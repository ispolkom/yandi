//! `client._build_messages` — единая точка сборки wire-формата messages для ВСЕХ бэкендов.
//! system: строка ИЛИ список строк (склеивается в ОДНО system-сообщение через пустую строку — принимает любой чат-шаблон);
//! messages, если задан, — ПОЛНАЯ история (заменяет одиночный prompt); иначе prompt → одно user-сообщение.

use serde_json::{json, Value};

/// `system`: None | строка | список строк (как в Python-сигнатуре `str | list[str] | None`).
#[derive(Debug, Clone, PartialEq)]
pub enum SystemArg {
    None,
    One(String),
    Many(Vec<String>),
}

pub fn build_messages(prompt: Option<&str>, system: &SystemArg, messages: Option<&[Value]>) -> Vec<Value> {
    let sys_list: Vec<&str> = match system {
        SystemArg::Many(v) => v.iter().map(|s| s.as_str()).collect(),
        SystemArg::One(s) => vec![s.as_str()],
        SystemArg::None => vec![],
    };
    let sys_list: Vec<&str> = sys_list.into_iter().filter(|s| !s.is_empty()).collect();
    let mut result: Vec<Value> = Vec::new();
    if !sys_list.is_empty() {
        result.push(json!({"role": "system", "content": sys_list.join("\n\n")}));
    }
    if let Some(m) = messages {
        result.extend(m.iter().cloned());
    } else if let Some(p) = prompt {
        result.push(json!({"role": "user", "content": p}));
    }
    result
}

/// `client._append_system_instruction`: список → копия с добавленной инструкцией; непустая строка → список из двух; иначе — сама инструкция.
pub fn append_system_instruction(system: &SystemArg, instruction: &str) -> Value {
    match system {
        SystemArg::Many(v) => {
            let mut out: Vec<Value> = v.iter().map(|s| Value::String(s.clone())).collect();
            out.push(Value::String(instruction.to_string()));
            Value::Array(out)
        }
        SystemArg::One(s) if !s.is_empty() => json!([s, instruction]),
        _ => Value::String(instruction.to_string()),
    }
}
