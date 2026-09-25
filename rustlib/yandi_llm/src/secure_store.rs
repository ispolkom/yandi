//! llm_gateway/secure_store.py — зашифрованное, «вставить-или-стереть, но никогда не изменить» локальное хранилище настроек моделей узла.
//! Родная версия ФАЙЛОВО СОВМЕСТИМА с Python: та же SQLite-схема с теми же триггерами, тот же файл ключа (KEK, 32 байта, права 0600), тот же файл контрольной
//! точки цепочки рядом с ключом, те же шифроблоки (`yandi_rs::crypto`: AES-256-GCM, AAD `YANDI|node_model_config|<слепой индекс>|<поле>|v1`), тот же слепой индекс
//! имени (HMAC-SHA256) и та же хеш-цепочка журнала. Хранилище, созданное одной реализацией, читает другая (проверено `llm_gateway/native_secure_store_parity_test.py`).
//!
//! Гарантии оригинала сохранены: KEK создаётся кодом один раз и никогда не перезаписывается; имя модели зашифровано (в базе — слепой индекс); UPDATE запрещён триггером;
//! журнал append-only (без UPDATE и DELETE) с HMAC-цепочкой; контрольная точка лежит ВНЕ файла базы (откат подменой файла обнаруживается); журнал сверяется с фактическим
//! содержимым таблицы (строка, вставленная/удалённая напрямую через SQL, обнаруживается ДО расшифровки). Отличие формата: открытый текст конфигурации сериализуется
//! компактно (`serde_json`), а не `json.dumps` с пробелами — расшифровка даёт тот же объект.

use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use rusqlite::{params, Connection};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use yandi_rs::crypto as crypt;
use yandi_rs::py_text::py_repr_str;

const ENTITY_TYPE: &str = "node_model_config";
const NAME_NAMESPACE: &str = "node-model-name:v1";
const GENESIS_HASH: [u8; 32] = [0u8; 32];
const KEK_SIZE: usize = 32;
pub const KEK_PATH_ENV: &str = "YANDI_KEK_PATH";
pub const NODE_DB_ENV: &str = "YANDI_NODE_DB";

/// Исключения оригинала: `SecureStoreError`, `KeyPermissionError`, `KeyStorageError` (прочее — ввод-вывод/SQLite).
#[derive(Debug, Clone, PartialEq)]
pub enum StoreError {
    Secure(String),
    KeyPermission(String),
    KeyStorage(String),
    Other(String),
}

impl StoreError {
    pub fn kind(&self) -> &'static str {
        match self {
            StoreError::Secure(_) => "SecureStoreError",
            StoreError::KeyPermission(_) => "KeyPermissionError",
            StoreError::KeyStorage(_) => "KeyStorageError",
            StoreError::Other(_) => "Other",
        }
    }
    pub fn message(&self) -> &str {
        match self {
            StoreError::Secure(m) | StoreError::KeyPermission(m) | StoreError::KeyStorage(m) | StoreError::Other(m) => m,
        }
    }
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message())
    }
}
impl std::error::Error for StoreError {}

fn secure<T>(m: impl Into<String>) -> Result<T, StoreError> {
    Err(StoreError::Secure(m.into()))
}
fn other(e: impl std::fmt::Display) -> StoreError {
    StoreError::Other(e.to_string())
}

// ---------------------------------------------------------------- пути

fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."))
}

fn env_nonempty(name: &str) -> Option<String> {
    match std::env::var(name) {
        Ok(v) if !v.is_empty() => Some(v),
        _ => None,
    }
}

pub fn kek_path() -> PathBuf {
    env_nonempty(KEK_PATH_ENV).map(PathBuf::from).unwrap_or_else(|| home().join(".local/share/yandi/keys/node_kek.bin"))
}

pub fn db_path() -> PathBuf {
    env_nonempty(NODE_DB_ENV).map(PathBuf::from).unwrap_or_else(|| home().join(".local/share/yandi/node_config.sqlite"))
}

/// Контрольная точка живёт РЯДОМ с ключом, НЕ внутри файла базы: подмена одного файла базы не подменяет и её.
pub fn chain_tip_path() -> PathBuf {
    kek_path().parent().map(|p| p.to_path_buf()).unwrap_or_default().join("node_config_chain_tip.json")
}

fn mkdir_private(dir: &Path) -> Result<(), StoreError> {
    if dir.as_os_str().is_empty() || dir.exists() {
        return Ok(());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        fs::DirBuilder::new().recursive(true).mode(0o700).create(dir).map_err(other)
    }
    #[cfg(not(unix))]
    {
        fs::create_dir_all(dir).map_err(other)
    }
}

// ---------------------------------------------------------------- ключ (keys.py: save_kek / load_kek)

fn load_kek(path: &Path) -> Result<Option<Vec<u8>>, StoreError> {
    // Ok(None) — файла нет (KeyMissingError оригинала)
    if !path.exists() {
        return Ok(None);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(path).map_err(other)?.permissions().mode() & 0o7777;
        if mode & 0o077 != 0 {
            return Err(StoreError::KeyPermission(format!(
                "KEK file {} is readable by group/other (mode {}) — refusing to load. Fix with `chmod 600 {}`.",
                py_repr_str(&path.to_string_lossy()), py_oct(mode), path.to_string_lossy()
            )));
        }
    }
    let kek = fs::read(path).map_err(other)?;
    if kek.len() != KEK_SIZE {
        return Err(StoreError::KeyStorage(format!("KEK at {} is {} bytes, expected {} (AES-256 key)", py_repr_str(&path.to_string_lossy()), kek.len(), KEK_SIZE)));
    }
    Ok(Some(kek))
}

/// Python `oct(mode)`: "0o600".
fn py_oct(m: u32) -> String {
    format!("0o{:o}", m)
}

fn save_kek(path: &Path, kek: &[u8]) -> Result<(), StoreError> {
    if path.exists() {
        return Err(StoreError::Other(format!("KEK already exists at {} — use an explicit rotation path, not overwrite", path.to_string_lossy())));
    }
    if let Some(parent) = path.parent() {
        mkdir_private(parent)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if !parent.as_os_str().is_empty() {
                fs::set_permissions(parent, fs::Permissions::from_mode(0o700)).map_err(other)?;
            }
        }
    }
    #[cfg(unix)]
    let mut f = {
        use std::os::unix::fs::OpenOptionsExt;
        fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(path).map_err(other)?
    };
    #[cfg(not(unix))]
    let mut f = fs::OpenOptions::new().write(true).create_new(true).open(path).map_err(other)?;
    f.write_all(kek).map_err(other)
}

/// `_ensure_kek`: загрузить ключ или создать ОДИН РАЗ (без участия человека).
fn ensure_kek() -> Result<Vec<u8>, StoreError> {
    let path = kek_path();
    if let Some(k) = load_kek(&path)? {
        return Ok(k);
    }
    use rand::RngCore;
    let mut kek = vec![0u8; KEK_SIZE];
    rand::rngs::OsRng.fill_bytes(&mut kek);
    save_kek(&path, &kek)?;
    println!(
        "[llm_gateway] Создан новый ключ узла: {}\n[llm_gateway] Это единственная копия. Потеря файла = потеря доступа ко всем сохранённым настройкам модели (не к самим моделям — только к записи о том, где их искать). Резервная копия — на усмотрение владельца узла.\n[llm_gateway] Рекомендуется дополнительно закрепить права на файл (см. llm_gateway/harden_key.sh, требует root один раз).",
        path.to_string_lossy()
    );
    Ok(kek)
}

/// `_load_kek_for_read`: НИКОГДА не создаёт ключ — если история не пуста, а ключа нет, это потеря ключа либо попытка обхода проверки.
fn load_kek_for_read() -> Result<Vec<u8>, StoreError> {
    match load_kek(&kek_path())? {
        Some(k) => Ok(k),
        None => secure("история конфигурации не пуста, но ключ узла отсутствует — либо ключ потерян, либо файл ключа был удалён намеренно. Восстановление данных без ключа невозможно."),
    }
}

struct Keys {
    integrity: Vec<u8>,
    index: Vec<u8>,
    config: Vec<u8>,
}

fn derive(kek: &[u8]) -> Keys {
    let h = |info: &[u8]| crypt::hkdf_sha256(kek, info, 32).expect("32 байта — допустимая длина HKDF");
    Keys { integrity: h(b"YANDI|integrity-key|v1"), index: h(b"YANDI|blind-index-key|v1"), config: h(b"YANDI|node-config-key|v1") }
}

// ---------------------------------------------------------------- база

fn ms_now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

fn connect() -> Result<Connection, StoreError> {
    let path = db_path();
    if let Some(parent) = path.parent() {
        mkdir_private(parent)?;
    }
    let conn = Connection::open(&path).map_err(other)?;
    let _: String = conn.query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0)).map_err(other)?;

    // защита от «дрейфа схемы»: старый формат без слепого индекса — пустую таблицу пересоздаём, непустую НЕ трогаем (нужна осознанная миграция)
    let mut cols: Vec<String> = Vec::new();
    {
        let mut st = conn.prepare("PRAGMA table_info(model_config)").map_err(other)?;
        let rows = st.query_map([], |r| r.get::<_, String>(1)).map_err(other)?;
        for c in rows {
            cols.push(c.map_err(other)?);
        }
    }
    if !cols.is_empty() && !cols.iter().any(|c| c == "name_index") {
        let n: i64 = conn.query_row("SELECT COUNT(*) FROM model_config", [], |r| r.get(0)).map_err(other)?;
        if n > 0 {
            return secure(format!(
                "{} has an old model_config schema (pre blind-index/journal redesign) with {} existing row(s) — refusing to touch it automatically. This needs a deliberate, owner-approved migration, not a silent drop.",
                path.to_string_lossy(), n
            ));
        }
        conn.execute("DROP TABLE model_config", []).map_err(other)?;
    }
    for sql in [
        "CREATE TABLE IF NOT EXISTS model_config (\n            name_index TEXT PRIMARY KEY,\n            name_blob BLOB NOT NULL,\n            config_blob BLOB NOT NULL,\n            created_at_ms INTEGER NOT NULL\n        )",
        "CREATE TRIGGER IF NOT EXISTS model_config_no_update\n        BEFORE UPDATE ON model_config\n        BEGIN\n            SELECT RAISE(ABORT, 'model_config is insert/delete-only — rows cannot be modified in place');\n        END",
        "CREATE TABLE IF NOT EXISTS model_config_journal (\n            seq INTEGER PRIMARY KEY,\n            op TEXT NOT NULL,\n            name_index TEXT NOT NULL,\n            content_hash BLOB NOT NULL,\n            entry_hash BLOB NOT NULL,\n            created_at_ms INTEGER NOT NULL\n        )",
        "CREATE TRIGGER IF NOT EXISTS model_config_journal_no_update\n        BEFORE UPDATE ON model_config_journal\n        BEGIN\n            SELECT RAISE(ABORT, 'model_config_journal is append-only — no update');\n        END",
        "CREATE TRIGGER IF NOT EXISTS model_config_journal_no_delete\n        BEFORE DELETE ON model_config_journal\n        BEGIN\n            SELECT RAISE(ABORT, 'model_config_journal is append-only — no delete');\n        END",
    ] {
        conn.execute(sql, []).map_err(other)?;
    }
    Ok(conn)
}

fn sha256(data: &[u8]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(data);
    h.finalize().into()
}

/// `hmac.compare_digest`: сравнение без ранних выходов.
fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b.iter()).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

fn unhex(s: &str) -> Result<Vec<u8>, StoreError> {
    if s.len() % 2 != 0 {
        return Err(StoreError::Other("odd-length hex".into()));
    }
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).map_err(other)).collect()
}

fn read_chain_tip() -> Result<Option<(i64, Vec<u8>)>, StoreError> {
    let p = chain_tip_path();
    if !p.exists() {
        return Ok(None);
    }
    let text = fs::read_to_string(&p).map_err(other)?;
    let v: Value = serde_json::from_str(&text).map_err(other)?;
    let seq = v.get("seq").and_then(|x| x.as_i64()).ok_or_else(|| StoreError::Other("seq".into()))?;
    let hash = unhex(v.get("hash").and_then(|x| x.as_str()).ok_or_else(|| StoreError::Other("hash".into()))?)?;
    Ok(Some((seq, hash)))
}

/// Формат файла — как `json.dumps({"seq": N, "hash": "hex"})` (с пробелами), запись через временный файл + переименование, права 0600.
fn write_chain_tip(seq: i64, entry_hash: &[u8]) -> Result<(), StoreError> {
    let p = chain_tip_path();
    if let Some(parent) = p.parent() {
        mkdir_private(parent)?;
    }
    let mut tmp = p.clone().into_os_string();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    fs::write(&tmp, format!("{{\"seq\": {}, \"hash\": \"{}\"}}", seq, hex(entry_hash))).map_err(other)?;
    fs::rename(&tmp, &p).map_err(other)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&p, fs::Permissions::from_mode(0o600)).map_err(other)?;
    }
    Ok(())
}

fn is_genuinely_empty(conn: &Connection) -> Result<bool, StoreError> {
    let has_journal = conn.query_row("SELECT 1 FROM model_config_journal LIMIT 1", [], |_| Ok(())).is_ok();
    let has_live = conn.query_row("SELECT 1 FROM model_config LIMIT 1", [], |_| Ok(())).is_ok();
    if !has_journal && has_live {
        return secure("несоответствие: журнал истории пуст, а таблица настроек — нет. Похоже, запись была добавлена в обход журнала (set_model_entry() не вызывался).");
    }
    if !has_journal && !has_live {
        if read_chain_tip()?.is_some() {
            return secure("ОБНАРУЖЕН ОТКАТ: журнал и таблица настроек пусты, но файл контрольной точки утверждает, что история уже существовала — база была полностью стёрта и выдаётся за первую установку, чтобы обойти проверку отката.");
        }
        return Ok(true);
    }
    Ok(false)
}

fn append_journal(conn: &Connection, integrity: &[u8], op: &str, name_index: &str, content_bytes: &[u8]) -> Result<(i64, Vec<u8>), StoreError> {
    let last: Option<(i64, Vec<u8>)> =
        conn.query_row("SELECT seq, entry_hash FROM model_config_journal ORDER BY seq DESC LIMIT 1", [], |r| Ok((r.get(0)?, r.get(1)?))).ok();
    let (prev_seq, prev_hash) = last.unwrap_or((0, GENESIS_HASH.to_vec()));
    let seq = prev_seq + 1;
    let content_hash = sha256(content_bytes);
    let entry = crypt::entry_hash(integrity, seq as u64, op, name_index, &content_hash, &prev_hash);
    conn.execute(
        "INSERT INTO model_config_journal (seq, op, name_index, content_hash, entry_hash, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
        params![seq, op, name_index, content_hash.to_vec(), entry.to_vec(), ms_now()],
    )
    .map_err(other)?;
    Ok((seq, entry.to_vec()))
}

fn verify_chain(conn: &Connection, integrity: &[u8]) -> Result<(), StoreError> {
    let mut rows: Vec<(i64, String, String, Vec<u8>, Vec<u8>)> = Vec::new();
    {
        let mut st = conn
            .prepare("SELECT seq, op, name_index, content_hash, entry_hash FROM model_config_journal ORDER BY seq ASC")
            .map_err(other)?;
        let it = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))).map_err(other)?;
        for r in it {
            rows.push(r.map_err(other)?);
        }
    }
    let mut prev_hash: Vec<u8> = GENESIS_HASH.to_vec();
    let mut expected_live: BTreeMap<String, Vec<u8>> = BTreeMap::new();
    for (seq, op, name_index, content_hash, entry_hash) in &rows {
        let expected = crypt::entry_hash(integrity, *seq as u64, op, name_index, content_hash, &prev_hash);
        if !ct_eq(&expected, entry_hash) {
            return secure(format!("цепочка истории конфигурации повреждена или подделана на записи seq={seq}"));
        }
        prev_hash = entry_hash.clone();
        if op == "insert" {
            expected_live.insert(name_index.clone(), content_hash.clone());
        } else if op == "delete" {
            expected_live.remove(name_index);
        }
    }
    let (db_seq, db_hash) = match rows.last() {
        Some(r) => (r.0, r.4.clone()),
        None => (0, GENESIS_HASH.to_vec()),
    };
    match read_chain_tip()? {
        None => {
            if !rows.is_empty() {
                println!("[llm_gateway] Не найден файл контрольной точки истории конфигурации — либо это первое включение этой защиты на уже существующей базе, либо файл был удалён. Записываю контрольную точку по текущему состоянию.");
                write_chain_tip(db_seq, &db_hash)?;
            }
            return Ok(());
        }
        Some((tip_seq, tip_hash)) => {
            if db_seq < tip_seq {
                return secure(format!("ОБНАРУЖЕН ОТКАТ: файл базы настроек отстаёт от последней известной контрольной точки (в базе seq={db_seq}, ожидалось не меньше seq={tip_seq}) — похоже, файл базы был подменён более старой копией."));
            }
            if db_seq == tip_seq && !ct_eq(&db_hash, &tip_hash) {
                return secure(format!("обнаружено расхождение истории на записи seq={db_seq} — данные подделаны."));
            }
            if db_seq > tip_seq {
                write_chain_tip(db_seq, &db_hash)?;
            }
        }
    }
    // журнал ↔ фактическое содержимое таблицы
    let mut actual: BTreeMap<String, Vec<u8>> = BTreeMap::new();
    {
        let mut st = conn.prepare("SELECT name_index, name_blob, config_blob FROM model_config").map_err(other)?;
        let it = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Vec<u8>>(1)?, r.get::<_, Vec<u8>>(2)?))).map_err(other)?;
        for r in it {
            let (ni, nb, cb) = r.map_err(other)?;
            let mut joined = nb;
            joined.push(b'|');
            joined.extend_from_slice(&cb);
            actual.insert(ni, sha256(&joined).to_vec());
        }
    }
    if actual != expected_live {
        let extra = actual.keys().filter(|k| !expected_live.contains_key(*k)).count();
        let missing = expected_live.keys().filter(|k| !actual.contains_key(*k)).count();
        let changed = actual.iter().filter(|(k, v)| expected_live.get(*k).map(|e| e != *v).unwrap_or(false)).count();
        return secure(format!(
            "расхождение между журналом истории и фактической таблицей настроек — данные были изменены в обход журнала (set_model_entry()/remove_model_entry() не вызывались): лишних строк={extra}, отсутствующих строк={missing}, изменённых={changed}"
        ));
    }
    Ok(())
}

fn blind(keys: &Keys, model: &str) -> String {
    crypt::blind_index(&keys.index, NAME_NAMESPACE, model)
}

fn decrypt(keys: &Keys, blob: &[u8], name_index: &str, field: &str) -> Result<String, String> {
    match crypt::decrypt_field(&keys.config, blob, ENTITY_TYPE, name_index, field) {
        Some(b) => String::from_utf8(b).map_err(|e| e.to_string()),
        None => Err(String::new()), // InvalidTag в Python печатается пустой строкой
    }
}

// ---------------------------------------------------------------- публичный API

pub fn get_model_entry(model: &str) -> Result<Option<Value>, StoreError> {
    let conn = connect()?;
    if is_genuinely_empty(&conn)? {
        return Ok(None);
    }
    let kek = load_kek_for_read()?;
    let keys = derive(&kek);
    verify_chain(&conn, &keys.integrity)?;
    let name_index = blind(&keys, model);
    let blob: Option<Vec<u8>> = conn.query_row("SELECT config_blob FROM model_config WHERE name_index = ?", params![name_index], |r| r.get(0)).ok();
    let blob = match blob {
        None => return Ok(None),
        Some(b) => b,
    };
    match decrypt(&keys, &blob, &name_index, "config") {
        Ok(text) => serde_json::from_str(&text).map(Some).map_err(other),
        Err(e) => secure(format!("запись для {} повреждена или подделана (расшифровка не прошла): {}", py_repr_str(model), e)),
    }
}

pub fn set_model_entry(model: &str, entry: &Value) -> Result<(), StoreError> {
    let kek = ensure_kek()?;
    let keys = derive(&kek);
    let name_index = blind(&keys, model);
    let name_blob = crypt::encrypt_field(&keys.config, model, ENTITY_TYPE, &name_index, "model_name", 1).expect("32-байтовый ключ");
    let config_blob =
        crypt::encrypt_field(&keys.config, &serde_json::to_string(entry).map_err(other)?, ENTITY_TYPE, &name_index, "config", 1).expect("32-байтовый ключ");
    let conn = connect()?;
    verify_chain(&conn, &keys.integrity)?;
    let ins = conn.execute(
        "INSERT INTO model_config (name_index, name_blob, config_blob, created_at_ms) VALUES (?, ?, ?, ?)",
        params![name_index, name_blob, config_blob, ms_now()],
    );
    if let Err(e) = ins {
        return match e {
            rusqlite::Error::SqliteFailure(f, _) if f.code == rusqlite::ErrorCode::ConstraintViolation => {
                secure(format!("запись для {} уже существует — сначала удали её явно, перезапись на месте запрещена", py_repr_str(model)))
            }
            other_e => Err(other(other_e)),
        };
    }
    let mut content = name_blob.clone();
    content.push(b'|');
    content.extend_from_slice(&config_blob);
    let (seq, entry_hash) = append_journal(&conn, &keys.integrity, "insert", &name_index, &content)?;
    drop(conn);
    write_chain_tip(seq, &entry_hash)
}

pub fn remove_model_entry(model: &str) -> Result<bool, StoreError> {
    let conn = connect()?;
    if is_genuinely_empty(&conn)? {
        return Ok(false);
    }
    let kek = load_kek_for_read()?;
    let keys = derive(&kek);
    verify_chain(&conn, &keys.integrity)?;
    let name_index = blind(&keys, model);
    let row: Option<(Vec<u8>, Vec<u8>)> =
        conn.query_row("SELECT name_blob, config_blob FROM model_config WHERE name_index = ?", params![name_index], |r| Ok((r.get(0)?, r.get(1)?))).ok();
    let (nb, cb) = match row {
        None => return Ok(false),
        Some(r) => r,
    };
    conn.execute("DELETE FROM model_config WHERE name_index = ?", params![name_index]).map_err(other)?;
    let mut content = nb;
    content.push(b'|');
    content.extend_from_slice(&cb);
    let (seq, entry_hash) = append_journal(&conn, &keys.integrity, "delete", &name_index, &content)?;
    drop(conn);
    write_chain_tip(seq, &entry_hash)?;
    Ok(true)
}

pub fn list_models() -> Result<Map<String, Value>, StoreError> {
    let conn = connect()?;
    if is_genuinely_empty(&conn)? {
        return Ok(Map::new());
    }
    let kek = load_kek_for_read()?;
    let keys = derive(&kek);
    verify_chain(&conn, &keys.integrity)?;
    let mut rows: Vec<(String, Vec<u8>, Vec<u8>)> = Vec::new();
    {
        let mut st = conn.prepare("SELECT name_index, name_blob, config_blob FROM model_config").map_err(other)?;
        let it = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).map_err(other)?;
        for r in it {
            rows.push(r.map_err(other)?);
        }
    }
    drop(conn);
    let mut result = Map::new();
    for (name_index, name_blob, config_blob) in rows {
        let name = decrypt(&keys, &name_blob, &name_index, "model_name");
        let entry = decrypt(&keys, &config_blob, &name_index, "config");
        match (name, entry) {
            (Ok(n), Ok(e)) => {
                result.insert(n, serde_json::from_str(&e).map_err(other)?);
            }
            (Err(e), _) | (_, Err(e)) => return secure(format!("запись повреждена или подделана (расшифровка не прошла): {e}")),
        }
    }
    Ok(result)
}
