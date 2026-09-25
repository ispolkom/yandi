//! Общее хранилище узла: единая встроенная база (`yandi_db`, SQLite) в каталоге данных пользователя. Открывается один раз при запуске узла; остальные части (ядро агента, PET) берут
//! соединение отсюда. Каталог данных лежит вне папки программы: удаление программы базу не трогает, при новой установке узел подключается к ней сам.
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};

static DB: OnceLock<Mutex<yandi_db::Db>> = OnceLock::new();

/// Открыть (или создать) базу. Повторный вызов возвращает тот же путь. Ошибка — текст простым языком (запуск узла из-за неё не останавливается: чаты и транспорт от базы не зависят).
pub fn init() -> Result<PathBuf, String> {
    let path = yandi_db::default_db_path();
    if DB.get().is_some() {
        return Ok(path);
    }
    let db = yandi_db::Db::open(&path).map_err(|e| format!("не удалось открыть базу данных {}: {e}", path.display()))?;
    let _ = DB.set(Mutex::new(db));
    Ok(path)
}

/// Выполнить работу с общей базой (блокирующе; из async-кода — через `spawn_blocking`).
pub fn with_db<T>(f: impl FnOnce(&yandi_db::Db) -> T) -> Result<T, String> {
    let m = DB.get().ok_or("база данных ещё не открыта")?;
    let g = m.lock().unwrap_or_else(|e| e.into_inner());
    Ok(f(&g))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opens_once_and_serves_repository_calls() {
        let dir = std::env::temp_dir().join(format!("yandi-storage-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::env::set_var(yandi_db::DB_PATH_ENV, dir.join("y.sqlite"));
        let p = init().unwrap();
        assert!(p.ends_with("y.sqlite") && p.exists());
        assert_eq!(init().unwrap(), p, "повторное открытие безопасно");
        let r = with_db(|db| yandi_db::repo::call(db.conn(), "resolve_question", &serde_json::json!({"raw_text": "вопрос", "asked_at": "2026-01-01 00:00:00"}))).unwrap().unwrap();
        assert_eq!(r["question_id"], serde_json::json!(1));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
