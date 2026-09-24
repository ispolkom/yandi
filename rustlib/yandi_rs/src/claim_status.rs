//! Перенос ядра agent/orchestrator/claims/status.py::classify_claim_epistemic_status (срез 37, 2026-09-24) — ЭПИСТЕМИЧЕСКОЕ ЯДРО:
//! как отношения «доказательство → утверждение» превращаются в статус утверждения (supported / disputed / contradicted / unverified).
//! Инвариант «trust never truth»: `verified` здесь НИКОГДА не выставляется; N перепечаток одной истории (один source_cluster_id) считаются
//! ОДНИМ независимым источником; авторитетный путь (role=direct + eligible) и путь directness (>= порога, не из «жёстко заблокированных»
//! классов, не из local_registry) — как в оригинале.
//!
//! Что делает Rust: разбор отношений одного утверждения и запись результата В ТОМ ЖЕ словаре (порядок ключей как в Python:
//! `rel["counted_via"]`, затем verification_status, support_count, contradiction_count, *_raw_relations, secondary_/context_relation_count).
//! Журнал (`log`) и сводка по всем утверждениям остаются в Python — Rust возвращает всё нужное для строк журнала.
//! Режим verbose: для отношений, которые попадут в журнал, заранее проверяется, что строка журнала не бросит исключение (иначе — откат:
//! в оригинале исключение случается ПОСРЕДИ цикла, и утверждение остаётся непереписанным).
//! Строгость: значения полей — только «родные» типы (None/bool/int/float/str/list/dict); любой посторонний тип или ошибка приведения
//! (`float("abc")`, нехэшируемый evidence_id…) → функция возвращает None БЕЗ единой записи, и Python выполняет исходный код
//! (который сам бросит то же исключение). Сравнения/хэш/`float()`/истинность делаются средствами самого Python через PyO3 — семантика точная.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyList, PySet, PyString};

use crate::pet_extraction::classify;

/// Родной тип? (иначе — откат)
fn native(o: &Bound<'_, PyAny>) -> bool {
    matches!(classify(o), Ok(Some(_)))
}

fn get_native<'py>(d: &Bound<'py, PyDict>, key: &str) -> Option<Option<Bound<'py, PyAny>>> {
    // Some(None) — ключа нет; Some(Some(v)) — родное значение; None — посторонний тип/ошибка → откат
    match d.get_item(key) {
        Ok(None) => Some(None),
        Ok(Some(v)) => {
            if native(&v) {
                Some(Some(v))
            } else {
                None
            }
        }
        Err(_) => None,
    }
}

fn eq_str(v: &Option<Bound<'_, PyAny>>, s: &str) -> bool {
    match v {
        Some(o) => o.eq(s).unwrap_or(false),
        None => false,
    }
}

/// `float(rel.get("directness", 0.0) or 0.0)`; Err(()) — откат (посторонний тип или ошибка приведения).
fn directness_f64(py: Python<'_>, rel: &Bound<'_, PyDict>) -> Result<f64, ()> {
    let d = get_native(rel, "directness").ok_or(())?;
    match d {
        None => Ok(0.0),
        Some(o) => {
            if o.is_truthy().map_err(|_| ())? {
                let fl = py.get_type_bound::<PyFloat>().call1((o,)).map_err(|_| ())?;
                fl.extract::<f64>().map_err(|_| ())
            } else {
                Ok(0.0)
            }
        }
    }
}

/// `_counts_toward_status(rel)`; Err(()) — откат.
fn counts_toward(
    py: Python<'_>, rel: &Bound<'_, PyDict>, hard_blocked: &Bound<'_, PySet>, threshold: f64,
) -> Result<Option<&'static str>, ()> {
    let role = get_native(rel, "evidence_role").ok_or(())?;
    let elig = get_native(rel, "evidence_eligible").ok_or(())?;
    let elig_true = match &elig {
        Some(o) => o.is(&true.into_py(py).into_bound(py)),
        None => false,
    };
    if eq_str(&role, "direct") && elig_true {
        return Ok(Some("authority"));
    }
    let sc = get_native(rel, "source_class").ok_or(())?;
    let sc_blocked = match &sc {
        // `rel.get("source_class") not in SET`: для отсутствующего ключа — None (не в множестве)
        Some(o) => hard_blocked.contains(o).map_err(|_| ())?,
        None => false,
    };
    if sc_blocked {
        return Ok(None);
    }
    let origin = get_native(rel, "retrieval_origin").ok_or(())?;
    if eq_str(&origin, "local_registry") {
        return Ok(None);
    }
    // float(rel.get("directness", 0.0) or 0.0) >= threshold
    let f = directness_f64(py, rel)?;
    if f >= threshold {
        Ok(Some("directness"))
    } else {
        Ok(None)
    }
}

/// `_distinct_cluster_count`; Err(()) — откат.
fn distinct_cluster_count(
    py: Python<'_>, direct: &[Bound<'_, PyDict>], relation_type: &str, ev_by_id: &Bound<'_, PyAny>,
) -> Result<usize, ()> {
    let seen = PySet::empty_bound(py).map_err(|_| ())?;
    let mut count = 0usize;
    let ev_by_id_truthy = ev_by_id.is_truthy().map_err(|_| ())?;
    for rel in direct {
        let relation = get_native(rel, "relation").ok_or(())?;
        if !eq_str(&relation, relation_type) {
            continue;
        }
        let ev_id = match get_native(rel, "evidence_id").ok_or(())? {
            Some(v) => v,
            None => py.None().into_bound(py),
        };
        // evidence_by_id.get(ev_id) if evidence_by_id else None
        let ev: Option<Bound<'_, PyAny>> = if ev_by_id_truthy {
            let getter = ev_by_id.getattr("get").map_err(|_| ())?;
            let r = getter.call1((&ev_id,)).map_err(|_| ())?;
            if r.is_none() {
                None
            } else {
                Some(r)
            }
        } else {
            None
        };
        // cluster_id = ev.get("source_cluster_id") if ev else None
        let mut cluster: Bound<'_, PyAny> = py.None().into_bound(py);
        if let Some(e) = &ev {
            if e.is_truthy().map_err(|_| ())? {
                let ed = e.downcast_exact::<PyDict>().map_err(|_| ())?; // .get на не-dict → AttributeError в оригинале → откат
                if let Some(c) = ed.get_item("source_cluster_id").map_err(|_| ())? {
                    if !native(&c) {
                        return Err(());
                    }
                    cluster = c;
                }
            }
        }
        if !cluster.is_truthy().map_err(|_| ())? {
            let s = ev_id.str().map_err(|_| ())?.to_string();
            cluster = PyString::new_bound(py, &format!("__unclustered__{s}")).into_any();
        }
        if seen.contains(&cluster).map_err(|_| ())? {
            continue;
        }
        seen.add(&cluster).map_err(|_| ())?;
        count += 1;
    }
    Ok(count)
}

/// Один раз для утверждения. None — откат (ничего не записано). Иначе кортеж:
/// (new_status, supports, contradicts, raw_supports, raw_contradicts, secondary, context, counted_rels).
#[pyfunction]
#[pyo3(name = "classify_claim")]
fn py_classify_claim(
    py: Python<'_>, claim: &Bound<'_, PyAny>, evidence_by_id: &Bound<'_, PyAny>, hard_blocked: &Bound<'_, PySet>, threshold: f64, verbose: bool,
) -> PyResult<Option<PyObject>> {
    let claim = match claim.downcast_exact::<PyDict>() {
        Ok(c) => c.clone(),
        Err(_) => return Ok(None),
    };
    // relations = list(claim.get("evidence_relations", []) or [])
    let rels_v = match claim.get_item("evidence_relations") {
        Ok(v) => v,
        Err(_) => return Ok(None),
    };
    let mut relations: Vec<Bound<'_, PyDict>> = Vec::new();
    if let Some(v) = rels_v {
        let truthy = match v.is_truthy() {
            Ok(t) => t,
            Err(_) => return Ok(None),
        };
        if truthy {
            let l = match v.downcast_exact::<PyList>() {
                Ok(l) => l.clone(),
                Err(_) => return Ok(None), // list(<не list>) — откат
            };
            for r in l.iter() {
                match r.downcast_exact::<PyDict>() {
                    Ok(d) => relations.push(d.clone()),
                    Err(_) => return Ok(None),
                }
            }
        }
    }
    // отношения, прошедшие проверку (анализ БЕЗ записи)
    let mut direct: Vec<(Bound<'_, PyDict>, &'static str)> = Vec::new();
    for rel in &relations {
        match counts_toward(py, rel, hard_blocked, threshold) {
            Err(()) => return Ok(None),
            Ok(Some(via)) => {
                if verbose {
                    // строка журнала `[Claim Support Decision]` в оригинале делает float(directness or 0.0) и печатает evidence_id/relation:
                    // если это бросит исключение, оно случилось бы ПОСРЕДИ цикла (утверждение осталось бы неизменённым) — откат сохраняет это.
                    if directness_f64(py, rel).is_err() || get_native(rel, "evidence_id").is_none() || get_native(rel, "relation").is_none() {
                        return Ok(None);
                    }
                }
                direct.push((rel.clone(), via))
            }
            Ok(None) => {}
        }
    }
    let direct_rels: Vec<Bound<'_, PyDict>> = direct.iter().map(|(r, _)| r.clone()).collect();
    let mut raw_s = 0usize;
    let mut raw_c = 0usize;
    for rel in &direct_rels {
        let relation = match get_native(rel, "relation") {
            Some(v) => v,
            None => return Ok(None),
        };
        if eq_str(&relation, "supports") {
            raw_s += 1;
        }
        if eq_str(&relation, "contradicts") {
            raw_c += 1;
        }
    }
    let supports = match distinct_cluster_count(py, &direct_rels, "supports", evidence_by_id) {
        Ok(n) => n,
        Err(()) => return Ok(None),
    };
    let contradicts = match distinct_cluster_count(py, &direct_rels, "contradicts", evidence_by_id) {
        Ok(n) => n,
        Err(()) => return Ok(None),
    };
    let mut secondary = 0usize;
    let mut context = 0usize;
    for rel in &relations {
        let role = match get_native(rel, "evidence_role") {
            Some(v) => v,
            None => return Ok(None),
        };
        let relation = match get_native(rel, "relation") {
            Some(v) => v,
            None => return Ok(None),
        };
        // rel.get("relation") in {"supports", "contradicts"} (нехэшируемое → TypeError в оригинале → откат)
        let in_set = match &relation {
            Some(o) => {
                if o.is_instance_of::<PyList>() || o.is_instance_of::<PyDict>() {
                    return Ok(None);
                }
                o.eq("supports").unwrap_or(false) || o.eq("contradicts").unwrap_or(false)
            }
            None => false,
        };
        if eq_str(&role, "secondary") && in_set {
            secondary += 1;
        }
        if eq_str(&role, "context") && in_set {
            context += 1;
        }
    }
    let new_status = if supports > 0 && contradicts > 0 {
        "disputed"
    } else if supports > 0 {
        "supported"
    } else if contradicts > 0 {
        "contradicted"
    } else {
        "unverified"
    };
    // ---- запись (после того как откат уже невозможен) ----
    let counted = PyList::empty_bound(py);
    for (rel, via) in &direct {
        rel.set_item("counted_via", *via)?;
        counted.append(rel)?;
    }
    claim.set_item("verification_status", new_status)?;
    claim.set_item("support_count", supports)?;
    claim.set_item("contradiction_count", contradicts)?;
    claim.set_item("support_count_raw_relations", raw_s)?;
    claim.set_item("contradiction_count_raw_relations", raw_c)?;
    claim.set_item("secondary_relation_count", secondary)?;
    claim.set_item("context_relation_count", context)?;
    Ok(Some((new_status, supports, contradicts, raw_s, raw_c, secondary, context, counted).into_py(py)))
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_classify_claim, m)?)?;
    Ok(())
}
