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

#[pymodule]
fn yandi_db(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(open_memory, m)?)?;
    m.add_function(wrap_pyfunction!(close, m)?)?;
    m.add_function(wrap_pyfunction!(call, m)?)?;
    m.add_function(wrap_pyfunction!(query, m)?)?;
    Ok(())
}
