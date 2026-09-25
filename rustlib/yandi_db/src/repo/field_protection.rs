//! Защита личных слов пользователя на диске (AES-256-GCM) — перенос `agent/db/sql/field_protection.py`. Формат значений и привязка (таблица, колонка, ключ строки)
//! совпадают с Python побайтно, поэтому значение, запечатанное одной реализацией, открывает другая.
//!
//! Переключатель — сама база (последняя запись `storage_protection_event`; нет записи = выключено), ключ — способность процесса. Выключено: пишется/читается
//! открытый текст (значения, похожие на запечатанные, экранируются префиксом `yp0:`). Включено: запись без ключа отвергается (`StorageLocked`), чтение открытого
//! значения там, где нужно запечатанное, — `StorageTampered`. Тихого отката на открытый текст при нехватке ключа НЕТ.
use std::sync::Mutex;
use std::time::{Duration, Instant};

use base64::Engine;
use hmac::{Hmac, Mac};
use rusqlite::Connection;
use serde_json::Value;
use sha2::Sha256;
use yandi_rs::crypto;

use super::*;

pub const SEALED_PREFIX: &str = "yp1:";
pub const ESCAPED_PREFIX: &str = "yp0:";
const STORAGE_INFO: &[u8] = b"yandi/storage/personal/v1";
const PROOF_INFO: &[u8] = b"yandi/storage/proof/v1";
const MODE_TTL: Duration = Duration::from_secs(10);
const CURRENT_VERSION: u8 = 1;

pub const PROTECTED: [(&str, &[&str]); 6] = [
    ("interaction_turn", &["user_text", "assistant_text"]),
    ("personal_fact", &["statement", "evidence"]),
    ("personal_fact_event", &["evidence"]),
    ("commitment", &["text", "evidence"]),
    ("commitment_event", &["evidence"]),
    ("grievance", &["description", "context"]),
];

pub const ENTITY_KEYS: [(&str, &[&str]); 6] = [
    ("interaction_turn", &["user_id", "source_turn_id"]),
    ("personal_fact", &["fact_id"]),
    ("personal_fact_event", &["fact_id", "event_type", "source_turn_id"]),
    ("commitment", &["commitment_id"]),
    ("commitment_event", &["commitment_id", "event_type"]),
    ("grievance", &["id"]),
];

struct State {
    key: Option<Vec<u8>>,
    proof_key: Option<Vec<u8>>,
    mode: Option<(String, Instant)>,
}

static STATE: Mutex<State> = Mutex::new(State { key: None, proof_key: None, mode: None });

fn st() -> std::sync::MutexGuard<'static, State> {
    STATE.lock().unwrap_or_else(|e| e.into_inner())
}

pub fn install_key(core_key: &[u8]) -> R<()> {
    if core_key.len() != 32 {
        return Err("ValueError: the storage key must be derived from a 32-byte key".into());
    }
    let mut s = st();
    s.key = crypto::hkdf_sha256(core_key, STORAGE_INFO, 32);
    s.proof_key = crypto::hkdf_sha256(core_key, PROOF_INFO, 32);
    s.mode = None;
    Ok(())
}

pub fn clear_key() {
    let mut s = st();
    s.key = None;
    s.proof_key = None;
    s.mode = None;
}

pub fn forget_mode() {
    st().mode = None;
}

pub fn mode_proof(proof_key: &[u8], nonce: &str, mode: &str) -> Vec<u8> {
    let mut m = <Hmac<Sha256> as Mac>::new_from_slice(proof_key).expect("HMAC принимает ключ любой длины");
    m.update(format!("YANDI|storage-mode|v1|{nonce}|{mode}").as_bytes());
    m.finalize().into_bytes().to_vec()
}

pub fn read_mode(c: &Connection) -> R<String> {
    let rec = c.query_row("SELECT mode, nonce, proof FROM storage_protection_event ORDER BY event_id DESC LIMIT 1", [], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<Vec<u8>>>(2)?)));
    let (mode, nonce, proof) = match rec {
        Ok(x) => x,
        Err(rusqlite::Error::QueryReturnedNoRows) => return Ok("off".into()),
        Err(e) if e.to_string().contains("no such table") => return Ok("off".into()),
        Err(e) => return Err(err(e)),
    };
    if !["off", "migrating", "on"].contains(&mode.as_str()) {
        return Err("StorageTampered: the storage mode record is not recognised".into());
    }
    if let Some(pk) = st().proof_key.clone() {
        let expected = mode_proof(&pk, &nonce, &mode);
        let ok = proof.as_deref().map(|p| !p.is_empty() && p == expected.as_slice()).unwrap_or(false);
        if !ok {
            return Err("StorageTampered: the storage mode record does not verify with this key".into());
        }
    }
    Ok(mode)
}

fn current_mode(c: &Connection) -> R<String> {
    if let Some((m, t)) = &st().mode {
        if t.elapsed() < MODE_TTL {
            return Ok(m.clone());
        }
    }
    let mode = read_mode(c)?;
    st().mode = Some((mode.clone(), Instant::now()));
    Ok(mode)
}

fn columns(table: &str) -> R<&'static [&'static str]> {
    PROTECTED.iter().find(|(t, _)| *t == table).map(|(_, c)| *c).ok_or_else(|| format!("KeyError: '{table}'"))
}

fn entity_keys(table: &str) -> &'static [&'static str] {
    ENTITY_KEYS.iter().find(|(t, _)| *t == table).map(|(_, k)| *k).unwrap_or(&[])
}

/// `str(value)` Python для значений ключа строки.
fn py_str(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn entity_id(table: &str, row_key: &Map<String, Value>) -> R<String> {
    let mut parts = Vec::new();
    for k in entity_keys(table) {
        match row_key.get(*k) {
            Some(v) => parts.push(py_str(v)),
            None => return Err(format!("StorageProtectionError: the row of {table} does not name its key field '{k}'")),
        }
    }
    Ok(parts.join("|"))
}

pub fn seal_with(key: &[u8], table: &str, column: &str, row_key: &Map<String, Value>, text: &str) -> R<String> {
    let blob = crypto::encrypt_field(key, text, table, &entity_id(table, row_key)?, column, CURRENT_VERSION).ok_or("KeyMissingError")?;
    Ok(format!("{SEALED_PREFIX}{}", base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(blob)))
}

pub fn open_with(key: &[u8], table: &str, column: &str, row_key: &Map<String, Value>, stored: &str) -> R<String> {
    let tampered = || format!("StorageTampered: a stored value of {table}.{column} does not open (wrong key, moved or altered)");
    let body = &stored[SEALED_PREFIX.len()..];
    let blob = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(body.trim_end_matches('=')).map_err(|_| tampered())?;
    let plain = crypto::decrypt_field(key, &blob, table, &entity_id(table, row_key)?, column).ok_or_else(tampered)?;
    String::from_utf8(plain).map_err(|_| tampered())
}

/// Что записать вместо `value`. NULL остаётся NULL.
pub fn seal(c: &Connection, table: &str, column: &str, row_key: &Map<String, Value>, value: Option<&str>) -> R<Option<String>> {
    if !columns(table)?.contains(&column) {
        return Err(format!("StorageProtectionError: {table}.{column} is not a protected column"));
    }
    let Some(value) = value else {
        return Ok(None);
    };
    if current_mode(c)? == "off" {
        return Ok(Some(if value.starts_with(SEALED_PREFIX) || value.starts_with(ESCAPED_PREFIX) { format!("{ESCAPED_PREFIX}{value}") } else { value.to_string() }));
    }
    let key = st().key.clone().ok_or("StorageLocked: storage protection is on and this process holds no key: nothing was written")?;
    Ok(Some(seal_with(&key, table, column, row_key, value)?))
}

pub fn open_value(c: &Connection, table: &str, column: &str, row_key: &Map<String, Value>, stored: Option<&str>) -> R<Option<String>> {
    let Some(stored) = stored else {
        return Ok(None);
    };
    if stored.starts_with(SEALED_PREFIX) {
        let key = st().key.clone().ok_or("StorageLocked: this value is sealed and this process holds no key")?;
        return Ok(Some(open_with(&key, table, column, row_key, stored)?));
    }
    if current_mode(c)? == "on" {
        return Err(format!("StorageTampered: a stored value of {table}.{column} is not sealed although protection is on"));
    }
    Ok(Some(stored.strip_prefix(ESCAPED_PREFIX).unwrap_or(stored).to_string()))
}

/// Открыть каждую защищённую колонку строки (`known` — поля ключа, которых SELECT не вернул).
pub fn open_row(c: &Connection, table: &str, row: &mut Row, known: Option<&Map<String, Value>>) -> R<()> {
    let mut row_key: Map<String, Value> = known.cloned().unwrap_or_default();
    for k in entity_keys(table) {
        if let Some(v) = row.get(*k) {
            row_key.insert((*k).to_string(), v.clone());
        }
    }
    for column in columns(table)? {
        if let Some(v) = row.get(*column).cloned() {
            let opened = open_value(c, table, column, &row_key, v.as_str())?;
            row.insert((*column).to_string(), opened.map(Value::String).unwrap_or(Value::Null));
        }
    }
    Ok(())
}

pub fn open_rows(c: &Connection, table: &str, rows: &mut [Row], known: Option<&Map<String, Value>>) -> R<()> {
    for r in rows.iter_mut() {
        open_row(c, table, r, known)?;
    }
    Ok(())
}

/// Запись нового режима (режим, nonce, proof) — только процесс с ключом (`new_mode_record`).
pub fn new_mode_record(mode: &str) -> R<(String, String, Vec<u8>)> {
    use rand::RngCore;
    if !["off", "migrating", "on"].contains(&mode) {
        return Err("ValueError: unknown mode".into());
    }
    let pk = st().proof_key.clone().ok_or("StorageLocked: a mode record can only be written by a process that holds the key")?;
    let mut b = [0u8; 16];
    rand::rngs::OsRng.fill_bytes(&mut b);
    let nonce: String = b.iter().map(|x| format!("{x:02x}")).collect();
    let proof = mode_proof(&pk, &nonce, mode);
    Ok((mode.to_string(), nonce, proof))
}

/// Ключ хранения, установленный в этом процессе (только для утилиты перепечатывания).
pub fn storage_key() -> Option<Vec<u8>> {
    st().key.clone()
}
