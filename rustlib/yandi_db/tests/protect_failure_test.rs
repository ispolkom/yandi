//! Защитные ветви утилиты защиты журнала: гонка записи, обрыв посреди работы, подмена содержимого при восстановлении. Точки отказа задают состояние, которого в бою
//! добиваются параллельные записи или сбои. Общее состояние (ключ, кэш режима, точка отказа) процесса → запускать `-- --test-threads=1`.
use serde_json::json;
use yandi_db::protect::{self, FAILPOINT};
use yandi_db::repo::field_protection as fp;
use yandi_db::repo::call;
use yandi_db::Db;

const KEY: [u8; 32] = [7u8; 32];

fn fresh() -> Db {
    fp::clear_key();
    fp::forget_mode();
    *FAILPOINT.lock().unwrap() = None;
    let db = Db::open_in_memory().unwrap();
    let c = db.conn();
    call(c, "record_interaction_turn", &json!({"user_id": "u1", "source_turn_id": "t1", "turn_id_origin": "c", "user_text": "привет", "assistant_text": "ответ", "created_at": "2026-03-01 10:00:00"})).unwrap();
    call(c, "insert_personal_fact", &json!({"fact_id": "f1", "user_id": "u1", "fact_class": "i", "statement": "Аня", "polarity": "p", "temporality": "t", "evidence": "Я Аня", "span_start": 0, "span_end": 1, "source_turn_id": "t1", "created_at": "2026-03-01 10:00:00"})).unwrap();
    call(c, "record_commitment", &json!({"commitment_id": "c1", "user_id": "u1", "kind": "promise", "text": "приду", "evidence": "обещаю", "created_at": "2026-03-01 10:00:00"})).unwrap();
    call(c, "record_grievance", &json!({"grievance_id": "g1", "user_id": "u1", "event_type": "e", "description": "секрет", "severity": 0.5, "created_at": "2026-03-01 10:00:00"})).unwrap();
    call(c, "record_grievance", &json!({"grievance_id": "g2", "user_id": "u1", "event_type": "e", "description": "другой секрет", "severity": 0.5, "created_at": "2026-03-01 10:00:01"})).unwrap();
    db
}

fn mode(db: &Db) -> String {
    protect::status(db.conn()).unwrap()["mode"].as_str().unwrap().to_string()
}

fn plain(db: &Db, table: &str) -> i64 {
    protect::status(db.conn()).unwrap()["tables"][table]["plain_values"].as_i64().unwrap()
}

fn sealed(db: &Db, table: &str) -> i64 {
    protect::status(db.conn()).unwrap()["tables"][table]["sealed_values"].as_i64().unwrap()
}

fn no_log() -> impl FnMut(&str) {
    |_m: &str| {}
}

fn set_failpoint(f: impl Fn(&str, &rusqlite::Connection) -> Result<(), String> + Send + Sync + 'static) {
    *FAILPOINT.lock().unwrap() = Some(Box::new(f));
}

#[test]
fn row_changed_under_the_rewrite_is_refused() {
    let db = fresh();
    set_failpoint(|p, c| {
        if p == "after_read:grievance" {
            c.execute("UPDATE grievance SET description='подмена' WHERE id='g1'", []).unwrap();
        }
        Ok(())
    });
    let err = protect::seal_all(db.conn(), &KEY, &mut no_log()).unwrap_err();
    assert_eq!(err, "ProtectError: a row of grievance changed while it was being rewritten: rolled back", "{err}");
    *FAILPOINT.lock().unwrap() = None;
    assert_eq!(mode(&db), "migrating");
    assert!(sealed(&db, "interaction_turn") > 0 && sealed(&db, "commitment_event") == 0, "раньше идущие таблицы запечатаны, эта — нет");
}

#[test]
fn resume_after_interruption_completes_and_content_is_identical() {
    let db = fresh();
    set_failpoint(|p, _| if p == "in_tx:commitment" { Err("обрыв питания".into()) } else { Ok(()) });
    let err = protect::seal_all(db.conn(), &KEY, &mut no_log()).unwrap_err();
    assert_eq!(err, "обрыв питания");
    *FAILPOINT.lock().unwrap() = None;
    assert_eq!(mode(&db), "migrating", "обрыв оставляет режим «в процессе»");
    assert!(sealed(&db, "interaction_turn") > 0 && sealed(&db, "personal_fact") > 0 && sealed(&db, "commitment") == 0);
    let ok = protect::seal_all(db.conn(), &KEY, &mut no_log()).unwrap();
    assert_eq!(ok["mode"], json!("on"));
    for t in protect::ORDER {
        assert_eq!(plain(&db, t), 0, "{t}");
    }
    // чтение через репозиторий даёт исходные слова
    let g = call(db.conn(), "get_grievance", &json!({"grievance_id": "g1"})).unwrap();
    assert_eq!(g["description"], json!("секрет"));
}

#[test]
fn change_of_content_inside_transaction_is_rolled_back() {
    let db = fresh();
    set_failpoint(|p, c| {
        if p == "in_tx:grievance" {
            c.execute("INSERT INTO grievance (id, user_id, event_type, description, severity, status, created_at, updated_at) VALUES ('gx','u1','e','лишняя','0.1','registered','t','t')", []).unwrap();
        }
        Ok(())
    });
    let err = protect::seal_all(db.conn(), &KEY, &mut no_log()).unwrap_err();
    assert_eq!(err, "ProtectError: grievance: the content after the rewrite is not the content before it: rolled back");
    *FAILPOINT.lock().unwrap() = None;
    let n: i64 = db.conn().query_row("SELECT COUNT(*) FROM grievance", [], |r| r.get(0)).unwrap();
    assert_eq!(n, 2, "лишняя строка откатилась вместе с перезаписью");
    assert_eq!(sealed(&db, "grievance"), 0);
    assert_eq!(mode(&db), "migrating");
}

#[test]
fn failure_always_puts_back_the_no_update_trigger() {
    let db = fresh();
    set_failpoint(|p, _| if p == "in_tx:interaction_turn" { Err("boom".into()) } else { Ok(()) });
    assert!(protect::seal_all(db.conn(), &KEY, &mut no_log()).is_err());
    *FAILPOINT.lock().unwrap() = None;
    let n: i64 = db.conn().query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name='trg_interaction_turn_no_update'", [], |r| r.get(0)).unwrap();
    assert_eq!(n, 1, "триггер «нельзя менять» возвращён");
    let e = db.conn().execute("UPDATE interaction_turn SET model='x'", []).unwrap_err().to_string();
    assert!(e.contains("immutable"), "{e}");
}

#[test]
fn restore_with_changed_content_is_rolled_back_and_leaves_tables_empty() {
    let db = fresh();
    protect::seal_all(db.conn(), &KEY, &mut no_log()).unwrap();
    let path = std::env::temp_dir().join(format!("yandi_protect_bk_{}", std::process::id()));
    protect::backup(db.conn(), &KEY, &path, &mut no_log()).unwrap();
    let fresh_db = Db::open_in_memory().unwrap();
    set_failpoint(|p, c| {
        if p == "restore_inserted" {
            c.execute("INSERT INTO grievance (id, user_id, event_type, description, severity, status, created_at, updated_at) VALUES ('gz','u1','e','лишняя','0.1','registered','t','t')", []).unwrap();
        }
        Ok(())
    });
    let err = protect::restore(fresh_db.conn(), &KEY, &path, &mut no_log()).unwrap_err();
    assert_eq!(err, "ProtectError: the restored content is not the content that was backed up: rolled back");
    *FAILPOINT.lock().unwrap() = None;
    for t in protect::ORDER {
        let n: i64 = fresh_db.conn().query_row(&format!("SELECT COUNT(*) FROM {t}"), [], |r| r.get(0)).unwrap();
        assert_eq!(n, 0, "{t}");
    }
    // без помех восстановление проходит и возвращает режим «включено»
    let ok = protect::restore(fresh_db.conn(), &KEY, &path, &mut no_log()).unwrap();
    assert_eq!(ok["mode"], json!("on"));
    assert_eq!(mode(&fresh_db), "on");
    let _ = std::fs::remove_file(&path);
}
