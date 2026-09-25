//! Мост для дифференциальных тестов: Python вызывает репозитории Rust на базе в памяти и сравнивает с Python-репозиториями на MySQL. Не для боевого использования.
use std::collections::HashMap;
use std::sync::Mutex;

use pyo3::prelude::*;
use serde_json::{json, Value};

use crate::Db;

static DBS: Mutex<Option<HashMap<u64, Db>>> = Mutex::new(None);
static NEXT: Mutex<u64> = Mutex::new(1);

#[pyfunction]
fn open_memory() -> PyResult<u64> {
    let db = Db::open_in_memory().map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.0))?;
    let mut n = NEXT.lock().unwrap();
    let id = *n;
    *n += 1;
    DBS.lock().unwrap().get_or_insert_with(HashMap::new).insert(id, db);
    Ok(id)
}

#[pyfunction]
fn close(handle: u64) {
    if let Some(m) = DBS.lock().unwrap().as_mut() {
        m.remove(&handle);
    }
}

fn with_db<T>(handle: u64, f: impl FnOnce(&Db) -> T) -> Result<T, String> {
    let g = DBS.lock().unwrap();
    let db = g.as_ref().and_then(|m| m.get(&handle)).ok_or("нет такой базы")?;
    Ok(f(db))
}

/// `call(handle, name, args_json) -> {"ok": …} | {"error": "…"}`
#[pyfunction]
fn call(py: Python<'_>, handle: u64, name: &str, args_json: &str) -> PyResult<String> {
    let args: Value = serde_json::from_str(args_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let name = name.to_string();
    let out = py.allow_threads(move || with_db(handle, |db| crate::repo::call(db.conn(), &name, &args)));
    Ok(match out {
        Ok(Ok(v)) => json!({"ok": v}),
        Ok(Err(e)) | Err(e) => json!({"error": e}),
    }
    .to_string())
}

/// Произвольный SELECT (только для сверки итогового состояния таблиц).
#[pyfunction]
fn query(handle: u64, sql: &str) -> PyResult<String> {
    let r = with_db(handle, |db| crate::repo::rows_pub(db.conn(), sql)).map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
    Ok(json!(r.map_err(pyo3::exceptions::PyRuntimeError::new_err)?).to_string())
}

/// Установить/снять ключ защиты полей и сбросить кэш режима (общее состояние процесса — как модульные переменные Python).
#[pyfunction]
fn fp_install_key(core_key_hex: &str) -> PyResult<()> {
    let bytes: Vec<u8> = (0..core_key_hex.len() / 2).map(|i| u8::from_str_radix(&core_key_hex[2 * i..2 * i + 2], 16).unwrap_or(0)).collect();
    crate::repo::field_protection::install_key(&bytes).map_err(pyo3::exceptions::PyValueError::new_err)
}

#[pyfunction]
fn fp_clear_key() {
    crate::repo::field_protection::clear_key();
}

#[pyfunction]
fn fp_forget_mode() {
    crate::repo::field_protection::forget_mode();
}

/// Изменяющий запрос для подготовки состояния тестов; параметры — JSON-массив, `{"hex": "…"}` = BLOB.
#[pyfunction]
fn exec_sql(handle: u64, sql: &str, params_json: &str) -> PyResult<usize> {
    let params: Vec<Value> = serde_json::from_str(params_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let bound: Vec<rusqlite::types::Value> = params
        .iter()
        .map(|p| match p {
            Value::Null => rusqlite::types::Value::Null,
            Value::Bool(b) => rusqlite::types::Value::Integer(*b as i64),
            Value::Number(n) if n.is_i64() => rusqlite::types::Value::Integer(n.as_i64().unwrap()),
            Value::Number(n) => rusqlite::types::Value::Real(n.as_f64().unwrap_or(0.0)),
            Value::String(s) => rusqlite::types::Value::Text(s.clone()),
            Value::Object(o) => {
                let h = o.get("hex").and_then(|x| x.as_str()).unwrap_or("");
                rusqlite::types::Value::Blob((0..h.len() / 2).map(|i| u8::from_str_radix(&h[2 * i..2 * i + 2], 16).unwrap_or(0)).collect())
            }
            other => rusqlite::types::Value::Text(other.to_string()),
        })
        .collect();
    with_db(handle, |db| db.conn().execute(sql, rusqlite::params_from_iter(bound)).map_err(|e| e.to_string()))
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)
}

/// Журнал целостности: `integrity_call(name, args_json) -> {"ok": …} | {"error": "…"}`.
#[pyfunction]
fn integrity_call(name: &str, args_json: &str) -> PyResult<String> {
    let args: Value = serde_json::from_str(args_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok(match crate::integrity::call(name, &args) {
        Ok(v) => json!({"ok": v}),
        Err(e) => json!({"error": e}),
    }
    .to_string())
}

#[pymodule]
fn yandi_db(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(open_memory, m)?)?;
    m.add_function(wrap_pyfunction!(close, m)?)?;
    m.add_function(wrap_pyfunction!(call, m)?)?;
    m.add_function(wrap_pyfunction!(query, m)?)?;
    m.add_function(wrap_pyfunction!(fp_install_key, m)?)?;
    m.add_function(wrap_pyfunction!(fp_clear_key, m)?)?;
    m.add_function(wrap_pyfunction!(fp_forget_mode, m)?)?;
    m.add_function(wrap_pyfunction!(exec_sql, m)?)?;
    m.add_function(wrap_pyfunction!(integrity_call, m)?)?;
    Ok(())
}
