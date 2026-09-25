//! yandi_db — РОДНОЙ слой базы данных YANDI: встроенная SQLite (решение владельца 2026-09-25: SQLite по умолчанию, внешняя БД — позже, опционально).
//! База = один файл в каталоге данных ПОЛЬЗОВАТЕЛЯ (не рядом с программой): удаление программы файл не трогает, при новой установке узел подключается к нему сам.
//! Схема и триггеры неизменяемости ГЕНЕРИРУЮТСЯ из Python-схемы MySQL (`rustlib/gen_db_schema.py`) — второго источника правды нет.
//! Модель защиты вместо ролей MySQL (GRANT в одном процессе на встроенной БД не существуют): права файла 0600/каталога 0700, триггеры «нельзя изменить/удалить»
//! (классы A/B/C/D из `TABLE_CLASSIFICATION`), защищённые поля (`yandi_rs::crypto`) и цепочка хешей журнала целостности.
use std::fs;
use std::path::{Path, PathBuf};

use rusqlite::{params, Connection};

/// Версия схемы, из которой сгенерированы `schema_sqlite.sql` / `triggers_sqlite.sql`.
pub mod repo;

#[cfg(feature = "python")]
mod bridge;

pub const SCHEMA_VERSION: i64 = 20;

const SCHEMA_SQL: &str = include_str!("schema_sqlite.sql");
const TRIGGERS_SQL: &str = include_str!("triggers_sqlite.sql");
/// Описание схемы (таблицы, классы A/B/C/D, колонки) — для проверок и инструментов.
pub const SCHEMA_META_JSON: &str = include_str!("schema_meta.json");

pub const DB_PATH_ENV: &str = "YANDI_DB";

#[derive(Debug)]
pub struct DbError(pub String);

impl std::fmt::Display for DbError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for DbError {}

fn e<T: std::fmt::Display>(x: T) -> DbError {
    DbError(x.to_string())
}

/// Каталог данных пользователя (переживает удаление программы): Windows `%APPDATA%\yandi`, macOS `~/Library/Application Support/yandi`,
/// Linux `$XDG_DATA_HOME/yandi` или `~/.local/share/yandi`.
pub fn data_dir() -> PathBuf {
    let home = || std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
    if cfg!(windows) {
        return std::env::var_os("APPDATA").map(PathBuf::from).unwrap_or_else(home).join("yandi");
    }
    if cfg!(target_os = "macos") {
        return home().join("Library/Application Support/yandi");
    }
    match std::env::var_os("XDG_DATA_HOME") {
        Some(x) if !x.is_empty() => PathBuf::from(x).join("yandi"),
        _ => home().join(".local/share/yandi"),
    }
}

/// Путь к файлу базы: `YANDI_DB` (переопределение) или `<каталог данных>/yandi.sqlite`.
pub fn default_db_path() -> PathBuf {
    match std::env::var(DB_PATH_ENV) {
        Ok(p) if !p.is_empty() => PathBuf::from(p),
        _ => data_dir().join("yandi.sqlite"),
    }
}

pub struct Db {
    conn: Connection,
}

impl Db {
    /// Открыть (или создать) базу по пути; схема применяется идемпотентно.
    pub fn open(path: &Path) -> Result<Db, DbError> {
        if let Some(dir) = path.parent().filter(|d| !d.as_os_str().is_empty()) {
            if !dir.exists() {
                fs::create_dir_all(dir).map_err(e)?;
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    fs::set_permissions(dir, fs::Permissions::from_mode(0o700)).map_err(e)?;
                }
            }
        }
        let fresh = !path.exists();
        let conn = Connection::open(path).map_err(e)?;
        #[cfg(unix)]
        if fresh {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
        }
        let _ = fresh;
        Db::prepare(conn)
    }

    /// База в памяти (тесты).
    pub fn open_in_memory() -> Result<Db, DbError> {
        Db::prepare(Connection::open_in_memory().map_err(e)?)
    }

    fn prepare(conn: Connection) -> Result<Db, DbError> {
        // WAL — устойчивость к обрыву питания и читатели не мешают писателю; внешние ключи в SQLite по умолчанию ВЫКЛЮЧЕНЫ — включаем
        conn.pragma_update(None, "journal_mode", "WAL").map_err(e)?;
        conn.pragma_update(None, "foreign_keys", "ON").map_err(e)?;
        conn.pragma_update(None, "synchronous", "NORMAL").map_err(e)?;
        conn.busy_timeout(std::time::Duration::from_secs(5)).map_err(e)?;
        let db = Db { conn };
        db.apply_schema()?;
        Ok(db)
    }

    /// Применить схему и триггеры (повторно безопасно). База НОВЕЕ этой программы — отказ (не портим данные более новой версии).
    fn apply_schema(&self) -> Result<(), DbError> {
        if let Some(v) = self.schema_version()? {
            if v > SCHEMA_VERSION {
                return Err(DbError(format!("база создана более новой версией YANDI (схема v{v}, эта программа знает до v{SCHEMA_VERSION}) — обновите программу")));
            }
        }
        self.conn.execute_batch(SCHEMA_SQL).map_err(|x| DbError(format!("схема: {x}")))?;
        self.conn.execute_batch(TRIGGERS_SQL).map_err(|x| DbError(format!("триггеры: {x}")))?;
        self.conn
            .execute("INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?1, ?2)", params![SCHEMA_VERSION, "baseline: схема v20, сгенерирована из agent/db/sql/schema.py"])
            .map_err(e)?;
        Ok(())
    }

    /// Наибольшая применённая версия схемы (None — таблицы миграций ещё нет).
    pub fn schema_version(&self) -> Result<Option<i64>, DbError> {
        let exists: i64 = self.conn.query_row("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'", [], |r| r.get(0)).map_err(e)?;
        if exists == 0 {
            return Ok(None);
        }
        self.conn.query_row("SELECT max(version) FROM schema_migrations", [], |r| r.get(0)).map_err(e)
    }

    pub fn conn(&self) -> &Connection {
        &self.conn
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn db() -> Db {
        Db::open_in_memory().unwrap()
    }

    fn meta() -> Value {
        serde_json::from_str(SCHEMA_META_JSON).unwrap()
    }


    /// Вставить минимальную строку в таблицу (внешние ключи на время вставки выключены; ENUM — первое допустимое значение).
    fn insert_dummy_row(d: &Db, table: &str) -> String {
        let sql: String = d.conn().query_row("SELECT sql FROM sqlite_master WHERE type='table' AND name=?1", [table], |r| r.get(0)).unwrap();
        let mut st = d.conn().prepare(&format!("PRAGMA table_info({table})")).unwrap();
        let cols: Vec<(String, String, bool, bool, bool)> = st
            .query_map([], |r| Ok((r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, i64>(3)? != 0, r.get::<_, Option<String>>(4)?.is_some(), r.get::<_, i64>(5)? != 0)))
            .unwrap()
            .map(|x| x.unwrap())
            .collect();
        let mut names = vec![];
        let mut vals = vec![];
        for (n, ty, notnull, has_default, pk) in &cols {
            let auto = sql.contains(&format!("{n} INTEGER PRIMARY KEY AUTOINCREMENT"));
            if auto || (!*notnull && !*pk) || (*has_default && !*pk) {
                continue;
            }
            let marker = format!("CHECK ({n} IN (");
            let v = if let Some(i) = sql.find(&marker) {
                let rest = &sql[i + marker.len()..];
                rest.split(')').next().unwrap().split(',').next().unwrap().trim().to_string()
            } else if ty == "INTEGER" || ty == "REAL" {
                "1".to_string()
            } else {
                "'x'".to_string()
            };
            names.push(n.clone());
            vals.push(v);
        }
        d.conn().execute_batch("PRAGMA foreign_keys=OFF").unwrap();
        let ins = if names.is_empty() { format!("INSERT INTO {table} DEFAULT VALUES") } else { format!("INSERT INTO {table} ({}) VALUES ({})", names.join(","), vals.join(",")) };
        let r = d.conn().execute_batch(&ins);
        d.conn().execute_batch("PRAGMA foreign_keys=ON").unwrap();
        r.unwrap_or_else(|x| panic!("{table}: {x}; {ins}"));
        cols[0].0.clone()
    }

    fn exec_err(d: &Db, sql: &str) -> String {
        d.conn().execute_batch(sql).unwrap_err().to_string()
    }

    #[test]
    fn schema_applies_and_is_idempotent() {
        let d = db();
        d.apply_schema().unwrap();
        d.apply_schema().unwrap();
        assert_eq!(d.schema_version().unwrap(), Some(SCHEMA_VERSION));
        let n: i64 = d.conn().query_row("SELECT count(*) FROM schema_migrations", [], |r| r.get(0)).unwrap();
        assert_eq!(n, 1);
        let tables: i64 = d.conn().query_row("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'", [], |r| r.get(0)).unwrap();
        assert_eq!(tables as usize, meta()["tables"].as_object().unwrap().len());
    }

    #[test]
    fn columns_match_generated_meta_in_order_and_nullability() {
        let d = db();
        for (table, m) in meta()["tables"].as_object().unwrap() {
            let mut st = d.conn().prepare(&format!("PRAGMA table_info({table})")).unwrap();
            let got: Vec<(String, bool)> = st.query_map([], |r| Ok((r.get::<_, String>(1)?, r.get::<_, i64>(3)? != 0 || r.get::<_, i64>(5)? != 0))).unwrap().map(|x| x.unwrap()).collect();
            let want: Vec<(String, bool)> = m["columns"].as_array().unwrap().iter().map(|c| (c["name"].as_str().unwrap().to_string(), c["notnull"].as_bool().unwrap())).collect();
            assert_eq!(got, want, "таблица {table}");
        }
    }

    #[test]
    fn immutable_tables_reject_update_and_delete() {
        let d = db();
        let m = meta();
        let a_tables: Vec<&String> = m["tables"].as_object().unwrap().iter().filter(|(_, v)| matches!(v["class"].as_str(), Some("A") | Some("B"))).map(|(k, _)| k).collect();
        assert!(a_tables.len() > 30);
        for t in a_tables {
            let c0 = insert_dummy_row(&d, t);
            let u = exec_err(&d, &format!("UPDATE {t} SET {c0} = {c0}"));
            assert!(u.contains("immutable") && u.contains("UPDATE forbidden"), "{t}: {u}");
            let x = exec_err(&d, &format!("DELETE FROM {t}"));
            assert!(x.contains("immutable") && x.contains("DELETE forbidden"), "{t}: {x}");
        }
    }

    #[test]
    fn projection_tables_allow_update_but_not_delete() {
        let d = db();
        let m = meta();
        for (t, v) in m["tables"].as_object().unwrap() {
            if v["class"].as_str() == Some("C") {
                let c0 = insert_dummy_row(&d, t);
                d.conn().execute_batch("PRAGMA foreign_keys=OFF").unwrap(); // строка-заглушка нарушает внешние ключи — здесь проверяется только триггер
                d.conn().execute_batch(&format!("UPDATE {t} SET {c0} = {c0}")).unwrap_or_else(|x| panic!("{t}: UPDATE должен быть разрешён: {x}"));
                let x = exec_err(&d, &format!("DELETE FROM {t}"));
                assert!(x.contains("DELETE forbidden"), "{t}: {x}");
            }
        }
    }

    #[test]
    fn foreign_keys_are_enforced() {
        let d = db();
        let x = exec_err(&d, "INSERT INTO question_occurrence (question_id, raw_text, asked_at) VALUES (999, 'x', '2026-01-01 00:00:00')");
        assert!(x.contains("FOREIGN KEY"), "{x}");
    }

    #[test]
    fn enum_check_and_unique_are_enforced() {
        let d = db();
        d.conn().execute_batch("INSERT INTO question (canonical_hash, first_asked_at) VALUES ('h1', '2026-01-01 00:00:00')").unwrap();
        assert!(exec_err(&d, "INSERT INTO question (canonical_hash, first_asked_at) VALUES ('h1', '2026-01-02 00:00:00')").contains("UNIQUE"));
        d.conn().execute_batch("INSERT INTO question_occurrence (question_id, raw_text, asked_at) VALUES (1, 'q', '2026-01-01 00:00:00')").unwrap();
        assert!(exec_err(&d, "INSERT INTO verification_run (run_id, occurrence_id, started_at, status) VALUES ('r', 1, '2026-01-01 00:00:00', 'bogus')").contains("CHECK"));
    }

    fn seed_run(d: &Db) {
        d.conn()
            .execute_batch(
                "INSERT INTO question (canonical_hash, first_asked_at) VALUES ('h1', 't'), ('h2', 't');
                 INSERT INTO question_occurrence (question_id, raw_text, asked_at) VALUES (1, 'q1', 't'), (2, 'q2', 't');
                 INSERT INTO verification_run (run_id, occurrence_id, started_at, pipeline_version) VALUES ('r1', 1, 't', 'abc');",
            )
            .unwrap();
    }

    #[test]
    fn verification_run_guard() {
        let d = db();
        seed_run(&d);
        // идентичность неизменна
        assert!(exec_err(&d, "UPDATE verification_run SET run_id='zz' WHERE run_id='r1'").contains("immutable"));
        assert!(exec_err(&d, "UPDATE verification_run SET occurrence_id=2 WHERE run_id='r1'").contains("immutable"));
        // допустим только переход running -> терминальный
        assert!(exec_err(&d, "UPDATE verification_run SET status='running' WHERE run_id='r1'").contains("only running"));
        // write-once поля
        assert!(exec_err(&d, "UPDATE verification_run SET status='completed', pipeline_version='evil' WHERE run_id='r1'").contains("write-once"));
        assert!(exec_err(&d, "UPDATE verification_run SET status='completed', web_enabled=1 WHERE run_id='r1'").contains("write-once"));
        // final_answer_id должен принадлежать вопросу ЭТОГО запуска
        d.conn()
            .execute_batch("INSERT INTO answer_version (question_id, run_id, created_at, text_hash, answer_text) VALUES (2, 'r1', 't', 'h', 'чужой ответ')")
            .ok();
        let ans: i64 = d.conn().query_row("SELECT count(*) FROM answer_version", [], |r| r.get(0)).unwrap();
        if ans > 0 {
            assert!(exec_err(&d, "UPDATE verification_run SET status='completed', final_answer_id=1 WHERE run_id='r1'").contains("THIS run"));
        }
        // законный переход
        d.conn().execute_batch("UPDATE verification_run SET status='completed', completed_at='t2' WHERE run_id='r1'").unwrap();
        // повторный переход запрещён
        assert!(exec_err(&d, "UPDATE verification_run SET status='failed' WHERE run_id='r1'").contains("already terminal"));
        assert!(exec_err(&d, "DELETE FROM verification_run WHERE run_id='r1'").contains("DELETE forbidden"));
    }

    #[test]
    fn file_database_persists_and_reopens_with_private_permissions() {
        let dir = std::env::temp_dir().join(format!("yandi_db_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let path = dir.join("sub/yandi.sqlite");
        {
            let d = Db::open(&path).unwrap();
            d.conn().execute_batch("INSERT INTO question (canonical_hash, first_asked_at) VALUES ('persist', 't')").unwrap();
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
            assert_eq!(fs::metadata(path.parent().unwrap()).unwrap().permissions().mode() & 0o777, 0o700);
        }
        // «переустановка»: та же база подключается сама, данные на месте
        let d = Db::open(&path).unwrap();
        let n: i64 = d.conn().query_row("SELECT count(*) FROM question WHERE canonical_hash='persist'", [], |r| r.get(0)).unwrap();
        assert_eq!(n, 1);
        assert_eq!(d.schema_version().unwrap(), Some(SCHEMA_VERSION));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn newer_database_is_refused() {
        let dir = std::env::temp_dir().join(format!("yandi_db_new_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let path = dir.join("y.sqlite");
        {
            let d = Db::open(&path).unwrap();
            d.conn().execute("INSERT INTO schema_migrations (version, description) VALUES (?1, 'из будущего')", params![SCHEMA_VERSION + 1]).unwrap();
        }
        let err = Db::open(&path).err().unwrap();
        assert!(err.0.contains("более новой версией"), "{err}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn data_dir_is_per_user_not_install_dir() {
        let p = data_dir();
        assert!(p.ends_with("yandi"), "{p:?}");
        assert!(default_db_path().ends_with("yandi.sqlite") || std::env::var(DB_PATH_ENV).is_ok());
    }
}
