//! Мост для сверки с Python: собственная база в памяти + вызов функций ядра по имени (JSON → JSON). Не для боевого использования.
use std::collections::HashMap;
use std::sync::Mutex;

use pyo3::prelude::*;
use serde_json::{json, Value};

use crate::ctx::Ctx;
use crate::R;

struct Slot {
    db: yandi_db::Db,
    now: Option<f64>,
    ids: Vec<String>,
}

static DBS: Mutex<Option<HashMap<u64, Slot>>> = Mutex::new(None);
static NEXT: Mutex<u64> = Mutex::new(1);

fn err(e: impl ToString) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(e.to_string())
}

#[pyfunction]
fn open_memory() -> PyResult<u64> {
    let db = yandi_db::Db::open_in_memory().map_err(|e| err(e.0))?;
    let mut n = NEXT.lock().unwrap();
    let id = *n;
    *n += 1;
    DBS.lock().unwrap().get_or_insert_with(HashMap::new).insert(id, Slot { db, now: None, ids: vec![] });
    Ok(id)
}

#[pyfunction]
fn close(handle: u64) {
    if let Some(m) = DBS.lock().unwrap().as_mut() {
        m.remove(&handle);
    }
}

/// Часы и очередь идентификаторов для воспроизводимости.
#[pyfunction]
#[pyo3(signature = (handle, now, ids))]
fn set_clock(handle: u64, now: Option<f64>, ids: Vec<String>) -> PyResult<()> {
    let mut g = DBS.lock().unwrap();
    let s = g.as_mut().and_then(|m| m.get_mut(&handle)).ok_or_else(|| err("нет такой базы"))?;
    s.now = now;
    s.ids = ids;
    Ok(())
}

#[pyfunction]
fn query(handle: u64, sql: &str) -> PyResult<String> {
    let g = DBS.lock().unwrap();
    let s = g.as_ref().and_then(|m| m.get(&handle)).ok_or_else(|| err("нет такой базы"))?;
    let r = yandi_db::repo::rows_pub(s.db.conn(), sql).map_err(err)?;
    Ok(json!(r).to_string())
}

/// Вызов функции ядра или репозитория: `{"ok": …} | {"error": …}`.
#[pyfunction]
fn call(py: Python<'_>, handle: u64, name: &str, args_json: &str) -> PyResult<String> {
    let args: Value = serde_json::from_str(args_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let name = name.to_string();
    let out = py.allow_threads(move || {
        let mut g = DBS.lock().unwrap();
        let s = g.as_mut().and_then(|m| m.get_mut(&handle)).ok_or("нет такой базы".to_string())?;
        let mut cx = Ctx::new(s.db.conn());
        cx.fixed_now = s.now;
        let cx = cx.with_ids(std::mem::take(&mut s.ids));
        let r = dispatch(&cx, &name, &args);
        Ok::<_, String>(r)
    });
    Ok(match out {
        Ok(Ok(v)) => json!({"ok": v}),
        Ok(Err(e)) | Err(e) => json!({"error": e}),
    }
    .to_string())
}

fn a_str(a: &Value, k: &str) -> Option<String> {
    a.get(k).and_then(|v| v.as_str()).map(String::from)
}

fn a_f(a: &Value, k: &str) -> f64 {
    a.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0)
}

fn a_span(a: &Value) -> Option<(i64, i64)> {
    let s = a.get("span")?.as_array()?;
    Some((s.first()?.as_i64()?, s.get(1)?.as_i64()?))
}

fn dispatch(cx: &Ctx, name: &str, a: &Value) -> R<Value> {
    use crate::{causal_events as ce, relationship_memory as rm, relationship_state as rs};
    let uid = a_str(a, "user_id").unwrap_or_default();
    Ok(match name {
        "causal_claim" => json!(ce::claim(cx, &uid, a_str(a, "source_turn_id").as_deref(), &a_str(a, "event_type").unwrap_or_default(), a_span(a))?),
        "rs_get_state" => rs::get_state(cx, &uid)?,
        "rs_record_insult" => {
            rs::record_insult(cx, &uid, a_f(a, "severity"))?;
            Value::Null
        }
        "rs_record_accepted_apology" => {
            rs::record_accepted_apology(cx, &uid, a_f(a, "offense_severity"), a_f(a, "sincerity"))?;
            Value::Null
        }
        "rs_record_verified_commitment" => {
            rs::record_verified_commitment(cx, &uid, a.get("kept").and_then(|v| v.as_bool()).unwrap_or(false))?;
            Value::Null
        }
        "rs_record_observed_commitment" => json!(rs::record_observed_commitment(cx, &uid, a.get("prior_observed").and_then(|v| v.as_i64()).unwrap_or(0))?),
        "rs_observed_trust_reward" => json!(rs::observed_trust_reward(a.get("prior_observed").and_then(|v| v.as_i64()).unwrap_or(0))),
        "rs_replay" => rs::replay(cx, &uid)?,
        "rm_add_grievance" => json!(rm::add_grievance(cx, &uid, &a_str(a, "event_type").unwrap_or_default(), &a_str(a, "description").unwrap_or_default(), a_f(a, "severity"), a.get("context").filter(|c| !c.is_null()), a_str(a, "source_turn_id").as_deref(), a_span(a))?),
        "rm_acknowledge_apology" => json!(rm::acknowledge_apology(cx, &a_str(a, "grievance_id").unwrap_or_default(), a_f(a, "sincerity"))?),
        "rm_progress_healing" => json!(rm::progress_healing(cx, &a_str(a, "grievance_id").unwrap_or_default())?),
        "rm_get_summary" => rm::get_summary(cx, &uid)?,
        "rm_most_severe_active_grievance" => rm::most_severe_active_grievance(cx, &uid)?,
        "rm_resolve_relationship_focus" => rm::resolve_relationship_focus(cx, &uid, &a_str(a, "current_text").unwrap_or_default())?,
        "rm_apply_apology" => rm::apply_apology(cx, &uid, a_str(a, "grievance_id").as_deref(), a_f(a, "sincerity"), a_str(a, "source_turn_id").as_deref(), a_span(a))?,
        "rm_match_grievance_target" => {
            let list = |k: &str| a.get(k).and_then(|v| v.as_array()).cloned().unwrap_or_default();
            let m = rm::match_grievance_target(&a_str(a, "text").unwrap_or_default(), &list("active"), &list("resolved"), a_f(a, "now"));
            json!({"grievance": m.grievance, "basis": m.basis, "candidates": m.candidates})
        }
        other => yandi_db::repo::call(cx.conn, other, a)?,
    })
}

#[pymodule]
fn yandi_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(open_memory, m)?)?;
    m.add_function(wrap_pyfunction!(close, m)?)?;
    m.add_function(wrap_pyfunction!(set_clock, m)?)?;
    m.add_function(wrap_pyfunction!(query, m)?)?;
    m.add_function(wrap_pyfunction!(call, m)?)?;
    Ok(())
}
