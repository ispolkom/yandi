//! Журнал целостности с цепочкой хешей — перенос `agent/db/sql/integrity.py`: детерминированная сериализация записи, HMAC-SHA256 цепочка, контрольная точка вне базы,
//! обнаружение отката. Формат байт-в-байт как у Python: события и контрольные точки, созданные одной реализацией, проверяет другая.
use std::fs;
use std::io::Write;
use std::path::Path;

use hmac::{Hmac, Mac};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

pub const INTEGRITY_FORMAT_VERSION: i64 = 1;
pub const GENESIS_HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";

pub type R<T> = Result<T, String>;

/// Значение поля в каноническом виде: None/bool/int/str — как есть, float — строка `%.6f`; остальное отвергается.
pub fn canonicalize_value(v: &Value) -> R<Value> {
    match v {
        Value::Null | Value::Bool(_) | Value::String(_) => Ok(v.clone()),
        Value::Number(n) if n.is_i64() || n.is_u64() => Ok(v.clone()),
        Value::Number(n) => Ok(Value::String(format!("{:.6}", n.as_f64().unwrap_or(0.0)))),
        other => Err(format!("UnsupportedFieldType: unsupported type for canonical serialization: {}", match other {
            Value::Array(_) => "<class 'list'>",
            _ => "<class 'dict'>",
        })),
    }
}

/// `json.dumps(..., ensure_ascii=False)` строки.
fn dumps_str(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn dumps_scalar(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => dumps_str(s, out),
        _ => unreachable!("канонизация пропускает только скаляры"),
    }
}

/// Детерминированные байты записи: ключи отсортированы, разделители `,` и `:`, UTF-8 без экранирования не-ASCII.
pub fn canonicalize_record(fields: &Map<String, Value>, entity_type: &str, entity_id: &str, format_version: i64) -> R<Vec<u8>> {
    let mut sorted: Vec<(&String, Value)> = Vec::new();
    for (k, v) in fields {
        sorted.push((k, canonicalize_value(v)?));
    }
    sorted.sort_by(|a, b| a.0.cmp(b.0));
    let mut s = String::from("{");
    // корневые ключи по алфавиту: entity_id, entity_type, fields, format_version
    s.push_str("\"entity_id\":");
    dumps_str(entity_id, &mut s);
    s.push_str(",\"entity_type\":");
    dumps_str(entity_type, &mut s);
    s.push_str(",\"fields\":{");
    for (i, (k, v)) in sorted.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        dumps_str(k, &mut s);
        s.push(':');
        dumps_scalar(v, &mut s);
    }
    s.push_str(&format!("}},\"format_version\":{format_version}}}"));
    Ok(s.into_bytes())
}

pub fn payload_hash(canonical: &[u8]) -> String {
    Sha256::digest(canonical).iter().map(|b| format!("{b:02x}")).collect()
}

pub fn compute_event_hash(key: &[u8], format_version: i64, entity_type: &str, entity_id: &str, payload_hash_hex: &str, previous_hex: &str) -> R<String> {
    if key.is_empty() {
        return Err("IntegrityError: compute_event_hash() called with no integrity key.".into());
    }
    let mut m = <Hmac<Sha256> as Mac>::new_from_slice(key).map_err(|e| e.to_string())?;
    m.update(format!("{format_version}|{entity_type}|{entity_id}|{payload_hash_hex}|{previous_hex}").as_bytes());
    Ok(m.finalize().into_bytes().iter().map(|b| format!("{b:02x}")).collect())
}

fn s<'a>(e: &'a Value, k: &str) -> &'a str {
    e.get(k).and_then(|v| v.as_str()).unwrap_or("")
}

/// Следующее событие цепочки одного `entity_type` (цепочку не изменяет).
pub fn append_event(key: &[u8], chain: &[Value], entity_type: &str, entity_id: &str, fields: &Map<String, Value>, format_version: i64) -> R<Value> {
    let canonical = canonicalize_record(fields, entity_type, entity_id, format_version)?;
    let p = payload_hash(&canonical);
    let prev = chain.last().map(|e| s(e, "event_hash").to_string()).unwrap_or_else(|| GENESIS_HASH.to_string());
    let seq = chain.last().and_then(|e| e.get("seq").and_then(|v| v.as_i64())).map(|x| x + 1).unwrap_or(1);
    let h = compute_event_hash(key, format_version, entity_type, entity_id, &p, &prev)?;
    Ok(json!({"seq": seq, "entity_type": entity_type, "entity_id": entity_id, "format_version": format_version, "payload_hash": p, "previous_event_hash": prev, "event_hash": h}))
}

/// Проверка цепочки: `(целая ли, найденные проблемы)`.
pub fn verify_chain(key: &[u8], events: &[Value]) -> R<(bool, Vec<String>)> {
    let mut problems = Vec::new();
    let mut prev = GENESIS_HASH.to_string();
    for e in events {
        if s(e, "previous_event_hash") != prev {
            problems.push(format!(
                "seq={}: previous_event_hash mismatch (expected {}, got {}) — a prior event was deleted, reordered, or replaced",
                e.get("seq").map(py_str).unwrap_or_else(|| "None".into()),
                prev,
                s(e, "previous_event_hash")
            ));
        }
        let expected = compute_event_hash(key, e.get("format_version").and_then(|v| v.as_i64()).unwrap_or(0), s(e, "entity_type"), s(e, "entity_id"), s(e, "payload_hash"), s(e, "previous_event_hash"))?;
        if s(e, "event_hash") != expected {
            problems.push(format!("seq={}: event_hash does not match recomputation — tampered", e.get("seq").map(py_str).unwrap_or_else(|| "None".into())));
        }
        prev = s(e, "event_hash").to_string();
    }
    Ok((problems.is_empty(), problems))
}

fn py_str(v: &Value) -> String {
    match v {
        Value::String(x) => x.clone(),
        Value::Null => "None".into(),
        other => other.to_string(),
    }
}

pub fn verify_record_against_event(fields: &Map<String, Value>, event: &Value, entity_type: &str, entity_id: &str) -> R<bool> {
    let canonical = canonicalize_record(fields, entity_type, entity_id, event.get("format_version").and_then(|v| v.as_i64()).unwrap_or(0))?;
    Ok(payload_hash(&canonical) == s(event, "payload_hash"))
}

/// `json.dump(cp, sort_keys=True)` Python (разделители `, ` и `: `).
fn checkpoint_text(cp: &Map<String, Value>) -> String {
    let mut keys: Vec<&String> = cp.keys().collect();
    keys.sort();
    let mut out = String::from("{");
    for (i, k) in keys.iter().enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        dumps_str(k, &mut out);
        out.push_str(": ");
        match &cp[*k] {
            Value::String(x) => dumps_str(x, &mut out),
            other => out.push_str(&other.to_string()),
        }
    }
    out.push('}');
    out
}

/// Контрольная точка ВНЕ базы: запись во временный файл, fsync, атомарная замена (обрыв на середине не оставляет половинного файла).
pub fn make_checkpoint(entity_type: &str, events: &[Value], path: &Path, now_secs: i64) -> R<Value> {
    let Some(latest) = events.last() else {
        return Err("ValueError: cannot checkpoint an empty chain".into());
    };
    let mut cp = Map::new();
    cp.insert("entity_type".into(), json!(entity_type));
    cp.insert("seq".into(), latest.get("seq").cloned().unwrap_or(Value::Null));
    cp.insert("event_hash".into(), latest.get("event_hash").cloned().unwrap_or(Value::Null));
    cp.insert("created_at".into(), json!(now_secs));
    let parent = path.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
    if !parent.exists() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(parent, fs::Permissions::from_mode(0o700));
        }
    }
    let tmp = format!("{}.tmp", path.display());
    {
        let mut f = fs::File::create(&tmp).map_err(|e| e.to_string())?;
        f.write_all(checkpoint_text(&cp).as_bytes()).map_err(|e| e.to_string())?;
        f.flush().map_err(|e| e.to_string())?;
        f.sync_all().map_err(|e| e.to_string())?;
    }
    fs::rename(&tmp, path).map_err(|e| e.to_string())?;
    Ok(Value::Object(cp))
}

pub fn load_checkpoint(path: &Path) -> R<Option<Value>> {
    if !path.exists() {
        return Ok(None);
    }
    let t = fs::read_to_string(path).map_err(|e| e.to_string())?;
    serde_json::from_str(&t).map(Some).map_err(|e| format!("JSONDecodeError: {e}"))
}

/// `(статус, подробность)`: no_checkpoint | ok | ahead | ROLLBACK_SUSPECTED.
pub fn check_for_rollback(current: &[Value], checkpoint_path: &Path) -> R<(String, Option<String>)> {
    let Some(cp) = load_checkpoint(checkpoint_path)? else {
        return Ok(("no_checkpoint".into(), None));
    };
    let Some(latest) = current.last() else {
        return Ok(("ROLLBACK_SUSPECTED".into(), Some("DB has zero events for an entity_type with a prior checkpoint".into())));
    };
    let (ls, cs) = (latest.get("seq").and_then(|v| v.as_i64()).unwrap_or(0), cp.get("seq").and_then(|v| v.as_i64()).unwrap_or(0));
    if ls < cs {
        return Ok(("ROLLBACK_SUSPECTED".into(), Some(format!("DB seq {ls} < checkpoint seq {cs}"))));
    }
    if ls == cs && s(latest, "event_hash") != s(&cp, "event_hash") {
        return Ok(("ROLLBACK_SUSPECTED".into(), Some("same sequence number but different hash — history has diverged".into())));
    }
    if ls == cs {
        return Ok(("ok".into(), None));
    }
    Ok(("ahead".into(), None))
}

/// Вызов по имени для моста тестов: JSON → JSON.
pub fn call(name: &str, args: &Value) -> R<Value> {
    let empty = Map::new();
    let o = args.as_object().unwrap_or(&empty);
    let key = |o: &Map<String, Value>| -> Vec<u8> { s_hex(o.get("key").and_then(|v| v.as_str()).unwrap_or("")) };
    let fields = |o: &Map<String, Value>| -> Map<String, Value> { o.get("fields").and_then(|v| v.as_object()).cloned().unwrap_or_default() };
    let arr = |o: &Map<String, Value>, k: &str| -> Vec<Value> { o.get(k).and_then(|v| v.as_array()).cloned().unwrap_or_default() };
    let text = |o: &Map<String, Value>, k: &str| -> String { o.get(k).map(py_str).unwrap_or_default() };
    let fv = |o: &Map<String, Value>| o.get("format_version").and_then(|v| v.as_i64()).unwrap_or(INTEGRITY_FORMAT_VERSION);
    Ok(match name {
        "canonicalize_value" => canonicalize_value(o.get("value").unwrap_or(&Value::Null))?,
        "canonicalize_record" => json!(String::from_utf8(canonicalize_record(&fields(o), &text(o, "entity_type"), &text(o, "entity_id"), fv(o))?).map_err(|e| e.to_string())?),
        "compute_event_hash" => json!(compute_event_hash(&key(o), fv(o), &text(o, "entity_type"), &text(o, "entity_id"), &text(o, "payload_hash"), &text(o, "previous"))?),
        "append_event" => append_event(&key(o), &arr(o, "chain"), &text(o, "entity_type"), &text(o, "entity_id"), &fields(o), fv(o))?,
        "verify_chain" => {
            let (ok, problems) = verify_chain(&key(o), &arr(o, "events"))?;
            json!([ok, problems])
        }
        "verify_record_against_event" => json!(verify_record_against_event(&fields(o), o.get("event").unwrap_or(&Value::Null), &text(o, "entity_type"), &text(o, "entity_id"))?),
        "make_checkpoint" => make_checkpoint(&text(o, "entity_type"), &arr(o, "events"), Path::new(&text(o, "path")), o.get("now").and_then(|v| v.as_i64()).unwrap_or(0))?,
        "load_checkpoint" => load_checkpoint(Path::new(&text(o, "path")))?.unwrap_or(Value::Null),
        "check_for_rollback" => {
            let (st, d) = check_for_rollback(&arr(o, "current_events"), Path::new(&text(o, "path")))?;
            json!([st, d])
        }
        other => return Err(format!("неизвестная функция {other}")),
    })
}

fn s_hex(h: &str) -> Vec<u8> {
    (0..h.len() / 2).map(|i| u8::from_str_radix(&h[2 * i..2 * i + 2], 16).unwrap_or(0)).collect()
}
