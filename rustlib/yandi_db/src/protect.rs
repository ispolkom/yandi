//! Запечатать / открыть / сохранить / восстановить защищённые колонки личного журнала — перенос `agent/db/sql/protect.py`. В едином бинарнике это подкоманды
//! `yandi storage status|seal|unseal|backup|restore`; ключ приходит из вызывающего кода и нигде не пишется.
//!
//! Как `seal` остаётся безопасным: сначала измеряется открытое содержимое каждой защищённой колонки (SHA-256 по таблице, строке, колонке, тексту), затем таблица за таблицей
//! (каждая в ОДНОЙ транзакции, с перечитыванием и сравнением с тем же измерением) значения запечатываются, и только когда весь журнал открывается ровно в измеренное содержимое —
//! режим переключается на ON. Обрыв оставляет режим MIGRATING (приложение читает обе формы); повторный `seal` доводит дело до конца. В таблицах «только добавлять» стоит триггер
//! BEFORE UPDATE: утилита снимает ровно тот триггер, что создала схема (`trg_<таблица>_no_update`), и в любом исходе возвращает его; незнакомый UPDATE-триггер останавливает до изменений.
use std::path::Path;
use std::sync::Mutex;

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use rand::RngCore;
use rusqlite::Connection;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::repo::field_protection as fp;
use crate::repo::{exec, row, rows, sv, Row, R};

pub const ORDER: [&str; 6] = ["interaction_turn", "personal_fact", "personal_fact_event", "commitment", "commitment_event", "grievance"];
const PRIMARY_KEY: [(&str, &str); 6] = [("interaction_turn", "interaction_id"), ("personal_fact", "fact_id"), ("personal_fact_event", "event_id"), ("commitment", "commitment_id"), ("commitment_event", "event_id"), ("grievance", "id")];
const WIDE: [&str; 3] = ["text", "mediumtext", "longtext"];
const BATCH: i64 = 500;
const BACKUP_MAGIC: &[u8] = b"YANDIBK1";
const BACKUP_AAD: &[u8] = b"YANDI|personal-backup|v1";

/// Точка отказа для проверки защитных ветвей (в бою не задана): вызывается в названных местах и может изменить базу или вернуть ошибку, как это сделала бы гонка или обрыв.
pub type Failpoint = Box<dyn Fn(&str, &Connection) -> R<()> + Send + Sync>;
pub static FAILPOINT: Mutex<Option<Failpoint>> = Mutex::new(None);

fn fail(point: &str, c: &Connection) -> R<()> {
    match FAILPOINT.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
        Some(f) => f(point, c),
        None => Ok(()),
    }
}

/// Одновременный запуск двух утилит в одном процессе не допускается (в MySQL это был именованный замок).
static RUNNING: Mutex<bool> = Mutex::new(false);

fn refuse<T>(m: impl std::fmt::Display) -> R<T> {
    Err(format!("ProtectError: {m}"))
}

fn pk(table: &str) -> &'static str {
    PRIMARY_KEY.iter().find(|(t, _)| *t == table).map(|(_, k)| *k).expect("таблица журнала")
}

fn protected(table: &str) -> &'static [&'static str] {
    fp::PROTECTED.iter().find(|(t, _)| *t == table).map(|(_, c)| *c).expect("таблица журнала")
}

/// Каждая защищённая колонка должна быть текстовой (схема v19): шифротекст не влезает в VARCHAR(500) или JSON. В SQLite все текстовые колонки — TEXT.
pub fn check_schema(c: &Connection) -> R<()> {
    for table in ORDER {
        let cols = rows(c, &format!("PRAGMA table_info({table})"), vec![])?;
        if cols.is_empty() {
            return refuse(format!("table {table} does not exist: apply the schema first"));
        }
        for column in protected(table) {
            let ty = cols.iter().find(|r| r["name"].as_str() == Some(column)).and_then(|r| r["type"].as_str()).map(|s| s.to_lowercase());
            if !ty.as_deref().map(|t| WIDE.contains(&t)).unwrap_or(false) {
                return refuse(format!("{table}.{column} is {ty:?}: apply schema v19 first"));
            }
        }
    }
    Ok(())
}

/// Все строки таблицы в порядке первичного ключа пачками (keyset-пагинация).
fn all_rows(c: &Connection, table: &str) -> R<Vec<Row>> {
    let key = pk(table);
    let mut out = Vec::new();
    let mut last: Option<Value> = None;
    loop {
        let batch = match &last {
            None => rows(c, &format!("SELECT * FROM {table} ORDER BY {key} LIMIT ?"), vec![crate::repo::iv(BATCH)])?,
            Some(v) => rows(c, &format!("SELECT * FROM {table} WHERE {key} > ? ORDER BY {key} LIMIT ?"), vec![to_sql(v), crate::repo::iv(BATCH)])?,
        };
        if batch.is_empty() {
            return Ok(out);
        }
        last = batch.last().map(|r| r[key].clone());
        out.extend(batch);
    }
}

fn to_sql(v: &Value) -> rusqlite::types::Value {
    match v {
        Value::Null => rusqlite::types::Value::Null,
        Value::Number(n) if n.is_i64() => rusqlite::types::Value::Integer(n.as_i64().unwrap()),
        Value::Number(n) => rusqlite::types::Value::Real(n.as_f64().unwrap_or(0.0)),
        Value::String(s) => rusqlite::types::Value::Text(s.clone()),
        other => rusqlite::types::Value::Text(other.to_string()),
    }
}

fn is_sealed(s: &str) -> bool {
    s.starts_with(fp::SEALED_PREFIX)
}

#[derive(PartialEq)]
enum Kind {
    Null,
    Plain,
    Sealed,
}

/// `(вид, открытый текст)` одного хранимого значения. Значение, похожее на запечатанное и не открывающееся, — ошибка, а не открытый текст.
fn classify(key: &[u8], table: &str, column: &str, r: &Row) -> R<(Kind, Option<String>)> {
    let Some(stored) = r.get(column).and_then(|v| v.as_str()) else {
        return Ok((Kind::Null, None));
    };
    if is_sealed(stored) {
        return match fp::open_with(key, table, column, r, stored) {
            Ok(t) => Ok((Kind::Sealed, Some(t))),
            Err(_) => refuse(format!("a value of {table}.{column} (row {}) looks sealed but does not open with this key", py_pk(&r[pk(table)]))),
        };
    }
    Ok((Kind::Plain, Some(stored.strip_prefix(fp::ESCAPED_PREFIX).unwrap_or(stored).to_string())))
}

fn py_pk(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// Значение в `json.dumps(..., ensure_ascii=False)`.
fn dumps(v: &Value, out: &mut String) {
    match v {
        Value::String(s) => {
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
        other => out.push_str(&other.to_string()),
    }
}

#[derive(Default)]
pub struct Scan {
    pub rows: Map<String, Value>,
    pub plain: Map<String, Value>,
    pub sealed: Map<String, Value>,
    digest: Sha256,
}

impl Scan {
    pub fn digest(&self) -> String {
        self.digest.clone().finalize().iter().map(|b| format!("{b:02x}")).collect()
    }
    fn total(m: &Map<String, Value>) -> i64 {
        m.values().filter_map(|v| v.as_i64()).sum()
    }
    fn get(m: &Map<String, Value>, t: &str) -> i64 {
        m.get(t).and_then(|v| v.as_i64()).unwrap_or(0)
    }
}

fn scan(c: &Connection, key: &[u8], tables: &[&str]) -> R<Scan> {
    let mut s = Scan::default();
    for table in tables {
        let (mut nr, mut np, mut ns) = (0, 0, 0);
        for r in all_rows(c, table)? {
            nr += 1;
            for column in protected(table) {
                let (kind, text) = classify(key, table, column, &r)?;
                match kind {
                    Kind::Plain => np += 1,
                    Kind::Sealed => ns += 1,
                    Kind::Null => {}
                }
                let mut line = String::from("[");
                dumps(&json!(table), &mut line);
                line.push_str(", ");
                dumps(&r[pk(table)], &mut line);
                line.push_str(", ");
                dumps(&json!(column), &mut line);
                line.push_str(", ");
                dumps(&text.map(Value::String).unwrap_or(Value::Null), &mut line);
                line.push_str("]\n");
                s.digest.update(line.as_bytes());
            }
        }
        s.rows.insert((*table).to_string(), json!(nr));
        s.plain.insert((*table).to_string(), json!(np));
        s.sealed.insert((*table).to_string(), json!(ns));
    }
    Ok(s)
}

/// Счётчики по видам без ключа (значение «запечатано» по префиксу).
pub fn status(c: &Connection) -> R<Value> {
    let mode = match fp::read_mode(c) {
        Ok(m) => m,
        Err(e) if e.starts_with("StorageTampered") => "unverified".into(),
        Err(e) => return Err(e),
    };
    let mut tables = Map::new();
    for table in ORDER {
        let (mut nr, mut sealed, mut plain) = (0, 0, 0);
        for r in all_rows(c, table)? {
            nr += 1;
            for column in protected(table) {
                match r.get(*column).and_then(|v| v.as_str()) {
                    None => {}
                    Some(s) if is_sealed(s) => sealed += 1,
                    Some(_) => plain += 1,
                }
            }
        }
        tables.insert(table.to_string(), json!({"rows": nr, "sealed_values": sealed, "plain_values": plain}));
    }
    Ok(json!({"mode": mode, "tables": Value::Object(tables)}))
}

fn set_mode(c: &Connection, mode: &str) -> R<()> {
    let (m, nonce, proof) = fp::new_mode_record(mode)?;
    exec(c, "INSERT INTO storage_protection_event (mode, nonce, proof, created_at) VALUES (?, ?, ?, datetime('now'))", vec![sv(&m), sv(&nonce), rusqlite::types::Value::Blob(proof)])?;
    fp::forget_mode();
    Ok(())
}

/// UPDATE-триггеры таблицы: `(имя, sql)`.
fn update_triggers(c: &Connection, table: &str) -> R<Vec<(String, String)>> {
    Ok(rows(c, "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", vec![sv(table)])?
        .into_iter()
        .filter(|r| r["sql"].as_str().map(|s| s.to_uppercase().contains("BEFORE UPDATE") || s.to_uppercase().contains("AFTER UPDATE")).unwrap_or(false))
        .map(|r| (r["name"].as_str().unwrap_or("").to_string(), r["sql"].as_str().unwrap_or("").to_string()))
        .collect())
}

fn preflight(c: &Connection) -> R<()> {
    for table in ORDER {
        let known = format!("trg_{table}_no_update");
        let unknown = update_triggers(c, table)?.into_iter().filter(|(n, _)| *n != known).count();
        if unknown > 0 {
            return refuse(format!("{table} has an UPDATE trigger this tool did not create ({unknown}): nothing was changed"));
        }
    }
    Ok(())
}

fn rewrite_table(c: &Connection, key: &[u8], table: &str, seal: bool, log: &mut dyn FnMut(&str)) -> R<i64> {
    let known = format!("trg_{table}_no_update");
    let triggers = update_triggers(c, table)?;
    let unknown = triggers.iter().filter(|(n, _)| *n != known).count();
    if unknown > 0 {
        return refuse(format!("{table} has an UPDATE trigger this tool did not create ({unknown}): nothing was changed"));
    }
    let saved = triggers.into_iter().find(|(n, _)| *n == known);
    let before = scan(c, key, &[table])?;
    fail(&format!("after_scan:{table}"), c)?;
    if saved.is_some() {
        c.execute_batch(&format!("DROP TRIGGER IF EXISTS {known}")).map_err(crate::repo::err)?;
    }
    let result = (|| -> R<i64> {
        c.execute_batch("BEGIN IMMEDIATE").map_err(crate::repo::err)?;
        let inner = (|| -> R<i64> {
            let mut changed = 0;
            let key_col = pk(table);
            let read = all_rows(c, table)?;
            fail(&format!("after_read:{table}"), c)?;
            for r in read {
                let mut updates: Vec<(&str, String)> = Vec::new();
                for column in protected(table) {
                    let (kind, text) = classify(key, table, column, &r)?;
                    if seal && kind == Kind::Plain {
                        updates.push((column, fp::seal_with(key, table, column, &r, text.as_deref().unwrap_or(""))?));
                    } else if !seal && kind == Kind::Sealed {
                        let t = text.unwrap_or_default();
                        updates.push((column, if t.starts_with(fp::SEALED_PREFIX) || t.starts_with(fp::ESCAPED_PREFIX) { format!("{}{t}", fp::ESCAPED_PREFIX) } else { t }));
                    }
                }
                if updates.is_empty() {
                    continue;
                }
                let set = updates.iter().map(|(c, _)| format!("{c} = ?")).collect::<Vec<_>>().join(", ");
                let guards = updates.iter().map(|(c, _)| format!("{c} IS ?")).collect::<Vec<_>>().join(" AND ");
                let mut args: Vec<rusqlite::types::Value> = updates.iter().map(|(_, v)| sv(v)).collect();
                args.push(to_sql(&r[key_col]));
                args.extend(updates.iter().map(|(c, _)| to_sql(&r[*c])));
                let (n, _) = exec(c, &format!("UPDATE {table} SET {set} WHERE {key_col} = ? AND {guards}"), args)?;
                if n != 1 {
                    return refuse(format!("a row of {table} changed while it was being rewritten: rolled back"));
                }
                changed += updates.len() as i64;
            }
            fail(&format!("in_tx:{table}"), c)?;
            let after = scan(c, key, &[table])?;
            if after.digest() != before.digest() {
                return refuse(format!("{table}: the content after the rewrite is not the content before it: rolled back"));
            }
            let leftover = if seal { Scan::get(&after.plain, table) } else { Scan::get(&after.sealed, table) };
            if leftover > 0 {
                return refuse(format!("{table}: {leftover} values were left in the old form: rolled back"));
            }
            Ok(changed)
        })();
        match inner {
            Ok(n) => {
                c.execute_batch("COMMIT").map_err(crate::repo::err)?;
                Ok(n)
            }
            Err(e) => {
                let _ = c.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    })();
    if let Some((_, sql)) = saved {
        c.execute_batch(&sql).map_err(crate::repo::err)?; // тот же триггер возвращается в любом исходе
    }
    let changed = result?;
    log(&format!("{table}: {changed} values {}", if seal { "sealed" } else { "opened" }));
    Ok(changed)
}

fn convert(c: &Connection, core_key: &[u8], seal: bool, log: &mut dyn FnMut(&str)) -> R<Value> {
    fp::install_key(core_key)?;
    let key = fp::storage_key().ok_or("StorageLocked")?;
    check_schema(c)?;
    {
        let mut g = RUNNING.lock().unwrap_or_else(|e| e.into_inner());
        if *g {
            return refuse("another protect run holds the lock");
        }
        *g = true;
    }
    let out = (|| -> R<Value> {
        let mode = fp::read_mode(c)?;
        preflight(c)?;
        let before = scan(c, &key, &ORDER)?;
        log(&format!("mode {mode}; rows {}; plaintext values {}; sealed values {}", Scan::total(&before.rows), Scan::total(&before.plain), Scan::total(&before.sealed)));
        if seal && mode == "on" && Scan::total(&before.plain) == 0 {
            return Ok(json!({"changed": 0, "mode": mode, "digest": before.digest()}));
        }
        if !seal && mode == "off" && Scan::total(&before.sealed) == 0 {
            return Ok(json!({"changed": 0, "mode": mode, "digest": before.digest()}));
        }
        if mode != "migrating" {
            set_mode(c, "migrating")?;
        }
        let mut changed = 0;
        let order: Vec<&str> = if seal { ORDER.to_vec() } else { ORDER.iter().rev().cloned().collect() };
        for table in order {
            changed += rewrite_table(c, &key, table, seal, log)?;
        }
        let after = scan(c, &key, &ORDER)?;
        if after.digest() != before.digest() {
            return refuse("the content of the ledger is not what it was before: the mode was NOT switched");
        }
        if (if seal { Scan::total(&after.plain) } else { Scan::total(&after.sealed) }) > 0 {
            return refuse("values remain in the old form (a write happened during the run?): the mode was NOT switched");
        }
        let target = if seal { "on" } else { "off" };
        set_mode(c, target)?;
        log(&format!("mode is now {target}; the content is identical to what it was"));
        Ok(json!({"changed": changed, "mode": target, "digest": after.digest()}))
    })();
    *RUNNING.lock().unwrap_or_else(|e| e.into_inner()) = false;
    out
}

pub fn seal_all(c: &Connection, core_key: &[u8], log: &mut dyn FnMut(&str)) -> R<Value> {
    convert(c, core_key, true, log)
}

pub fn unseal_all(c: &Connection, core_key: &[u8], log: &mut dyn FnMut(&str)) -> R<Value> {
    convert(c, core_key, false, log)
}

// ---------------------------------------------------------------- резервная копия

fn backup_key(storage_key: &[u8]) -> Vec<u8> {
    yandi_rs::crypto::hkdf_sha256(storage_key, b"yandi/storage/backup/v1", 32).expect("HKDF 32 байта")
}

fn columns_of(c: &Connection, table: &str) -> R<Vec<String>> {
    Ok(rows(c, &format!("PRAGMA table_info({table})"), vec![])?.into_iter().map(|r| r["name"].as_str().unwrap_or("").to_string()).collect())
}

/// Все строки «как хранятся» в ОДИН зашифрованный файл (AES-GCM под ключом из того же корня): резервная копия открытых строк на диске тоже не открытый текст.
pub fn backup(c: &Connection, core_key: &[u8], path: &Path, log: &mut dyn FnMut(&str)) -> R<Value> {
    fp::install_key(core_key)?;
    let key = fp::storage_key().ok_or("StorageLocked")?;
    check_schema(c)?;
    let mode = fp::read_mode(c)?;
    let mut tables = Map::new();
    let mut counts = Map::new();
    for table in ORDER {
        let rs = all_rows(c, table)?;
        counts.insert(table.to_string(), json!(rs.len()));
        tables.insert(table.to_string(), json!({"columns": columns_of(c, table)?, "rows": rs}));
    }
    let measured = scan(c, &key, &ORDER)?;
    let doc = json!({"version": 1, "mode": mode, "digest": measured.digest(), "counts": Value::Object(counts.clone()), "tables": Value::Object(tables)});
    let blob = serde_json::to_vec(&doc).map_err(crate::repo::err)?;
    let mut nonce = [0u8; 12];
    rand::rngs::OsRng.fill_bytes(&mut nonce);
    let cipher = Aes256Gcm::new_from_slice(&backup_key(&key)).map_err(crate::repo::err)?;
    let ct = cipher.encrypt(Nonce::from_slice(&nonce), Payload { msg: &blob, aad: BACKUP_AAD }).map_err(|e| e.to_string())?;
    let mut data = BACKUP_MAGIC.to_vec();
    data.extend_from_slice(&nonce);
    data.extend_from_slice(&ct);
    let dir = path.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
    let tmp = dir.join(format!(".backup-{}", std::process::id()));
    {
        use std::io::Write;
        let mut f = std::fs::File::create(&tmp).map_err(crate::repo::err)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            f.set_permissions(std::fs::Permissions::from_mode(0o600)).map_err(crate::repo::err)?;
        }
        f.write_all(&data).map_err(crate::repo::err)?;
        f.sync_all().map_err(crate::repo::err)?;
    }
    std::fs::rename(&tmp, path).map_err(crate::repo::err)?;
    log(&format!("backup written: {} rows in {} tables (mode {mode})", Scan::total(&counts), ORDER.len()));
    Ok(json!({"counts": Value::Object(counts), "mode": mode, "digest": measured.digest()}))
}

/// Вернуть копию в ПУСТЫЕ защищённые таблицы (непустая не трогается). После — измерение обязано совпасть с записанным в копии.
pub fn restore(c: &Connection, core_key: &[u8], path: &Path, log: &mut dyn FnMut(&str)) -> R<Value> {
    fp::install_key(core_key)?;
    let key = fp::storage_key().ok_or("StorageLocked")?;
    check_schema(c)?;
    let data = std::fs::read(path).map_err(crate::repo::err)?;
    if !data.starts_with(BACKUP_MAGIC) || data.len() < BACKUP_MAGIC.len() + 12 + 16 {
        return refuse("this is not a backup file made by this tool");
    }
    let nonce = &data[BACKUP_MAGIC.len()..BACKUP_MAGIC.len() + 12];
    let cipher = Aes256Gcm::new_from_slice(&backup_key(&key)).map_err(crate::repo::err)?;
    let plain = cipher.decrypt(Nonce::from_slice(nonce), Payload { msg: &data[BACKUP_MAGIC.len() + 12..], aad: BACKUP_AAD });
    let Ok(plain) = plain else {
        return refuse("the backup does not open with this key (wrong key or a damaged file)");
    };
    let doc: Value = serde_json::from_slice(&plain).map_err(|_| "ProtectError: the backup does not open with this key (wrong key or a damaged file)".to_string())?;
    for table in ORDER {
        if row(c, &format!("SELECT COUNT(*) AS n FROM {table}"), vec![])?.and_then(|r| r["n"].as_i64()).unwrap_or(0) > 0 {
            return refuse(format!("{table} is not empty: nothing was restored"));
        }
    }
    c.execute_batch("BEGIN IMMEDIATE").map_err(crate::repo::err)?;
    let inserted = (|| -> R<String> {
        for table in ORDER {
            let spec = &doc["tables"][table];
            let cols: Vec<String> = spec["columns"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default();
            let have = columns_of(c, table)?;
            if !cols.iter().all(|x| have.contains(x)) {
                return refuse(format!("{table}: the backup has columns this database does not"));
            }
            let sql = format!("INSERT INTO {table} ({}) VALUES ({})", cols.join(", "), vec!["?"; cols.len()].join(", "));
            for r in spec["rows"].as_array().cloned().unwrap_or_default() {
                exec(c, &sql, cols.iter().map(|k| dec_value(&r[k])).collect())?;
            }
        }
        fail("restore_inserted", c)?;
        let got = scan(c, &key, &ORDER)?;
        if Some(got.digest().as_str()) != doc["digest"].as_str() {
            return refuse("the restored content is not the content that was backed up: rolled back");
        }
        Ok(got.digest())
    })();
    let digest = match inserted {
        Ok(d) => {
            c.execute_batch("COMMIT").map_err(crate::repo::err)?;
            d
        }
        Err(e) => {
            let _ = c.execute_batch("ROLLBACK");
            return Err(e);
        }
    };
    let mode = doc["mode"].as_str().unwrap_or("off").to_string();
    if mode != "off" && fp::read_mode(c)? != mode {
        set_mode(c, &mode)?;
    }
    let counts = doc["counts"].clone();
    log(&format!("restored {} rows; mode {mode}", counts.as_object().map(Scan::total).unwrap_or(0)));
    Ok(json!({"counts": counts, "mode": mode, "digest": digest}))
}

/// Значение из копии: метки `$dt`/`$d`/`$n`/`$b` (их пишет Python-версия) приводятся к тому, что хранит SQLite.
fn dec_value(v: &Value) -> rusqlite::types::Value {
    if let Value::Object(m) = v {
        if m.len() == 1 {
            if let Some(Value::String(s)) = m.get("$dt") {
                return rusqlite::types::Value::Text(crate::repo::normalize_dt_string(s));
            }
            if let Some(Value::String(s)) = m.get("$d").or_else(|| m.get("$n")) {
                return rusqlite::types::Value::Text(s.clone());
            }
            if let Some(Value::String(s)) = m.get("$b") {
                use base64::Engine;
                return rusqlite::types::Value::Blob(base64::engine::general_purpose::STANDARD.decode(s).unwrap_or_default());
            }
        }
    }
    to_sql(v)
}
