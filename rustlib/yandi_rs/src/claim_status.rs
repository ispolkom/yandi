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

#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::{PyDict, PyFloat, PyList, PySet, PyString};

use crate::pet_extraction::{classify, V};

#[cfg(feature = "python")]
/// Родной тип? (иначе — откат)
fn native(o: &Bound<'_, PyAny>) -> bool {
    matches!(classify(o), Ok(Some(_)))
}

#[cfg(feature = "python")]
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

#[cfg(feature = "python")]
fn eq_str(v: &Option<Bound<'_, PyAny>>, s: &str) -> bool {
    match v {
        Some(o) => o.eq(s).unwrap_or(false),
        None => false,
    }
}

#[cfg(feature = "python")]
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

#[cfg(feature = "python")]
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

#[cfg(feature = "python")]
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

#[cfg(feature = "python")]
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

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_classify_claim, m)?)?;
    register_gate(_py, m)?;
    Ok(())
}

// =====================================================================================================================
// evaluate_claim_status_gate (срез 38) — ШЛЮЗ СТАТУСОВ: подсчёт статусов утверждений и, по результату, потолок доверия/уверенности +
// предупреждение В ТЕЛЕ ответа (`⚠️ ВАЖНО: …`). Пять взаимоисключающих случаев в том же порядке, что в оригинале. Работает над объектом
// `synthesis_result` через getattr/setattr (dataclass/SimpleNamespace/любой объект), `log` вызывается В ТЕХ ЖЕ местах, что в оригинале.
// Откат (None, ничего не сделано и не залогировано): утверждения — не список словарей с «родными» значениями статуса; для веток, которые
// читают `answer`/`confidence`/`trust_level`, они не «родные» (answer — не str, confidence — не число, trust_level нехэшируем).
// =====================================================================================================================

#[cfg(feature = "python")]
fn py_min<'py>(py: Python<'py>, a: &Bound<'py, PyAny>, b: f64) -> PyResult<Bound<'py, PyAny>> {
    py.import_bound("builtins")?.getattr("min")?.call1((a, b))
}

#[cfg(feature = "python")]
fn trust_rank(v: &Bound<'_, PyAny>) -> Option<i32> {
    // trust_rank.get(current, 0): None = нехэшируемое/посторонний тип → откат
    match classify(v) {
        Ok(Some(V::Str(s))) => Some(match s.as_str() {
            "UNVERIFIED" => 0,
            "WEAKLY_SUPPORTED" => 1,
            "PARTIALLY_SUPPORTED" => 2,
            "SUPPORTED" => 3,
            "STRONGLY_SUPPORTED" => 4,
            "VERIFIED" => 5,
            _ => 0,
        }),
        Ok(Some(V::None)) | Ok(Some(V::Bool)) | Ok(Some(V::Int(_))) | Ok(Some(V::Float(_))) => Some(0),
        _ => None,
    }
}

#[cfg(feature = "python")]
fn is_number(v: &Bound<'_, PyAny>) -> bool {
    matches!(classify(v), Ok(Some(V::Int(_))) | Ok(Some(V::Float(_))) | Ok(Some(V::Bool)))
}

const WARN: &str = "⚠️";

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "evaluate_gate")]
fn py_evaluate_gate(
    py: Python<'_>, claims_data: &Bound<'_, PyAny>, synth: &Bound<'_, PyAny>, log: &Bound<'_, PyAny>,
) -> PyResult<Option<(usize, usize, usize)>> {
    // ---- 1. подсчёт (без побочных эффектов) ----
    let list = match claims_data.downcast_exact::<PyList>() {
        Ok(l) => l.clone(),
        Err(_) => return Ok(None),
    };
    let (mut verified, mut supported, mut disputed, mut contradicted, mut candidate, mut rejected, mut unverified) = (0usize, 0, 0, 0, 0, 0, 0);
    for c in list.iter() {
        let d = match c.downcast_exact::<PyDict>() {
            Ok(d) => d.clone(),
            Err(_) => return Ok(None),
        };
        let v = match d.get_item("verification_status") {
            Ok(v) => v,
            Err(_) => return Ok(None),
        };
        match v {
            None => unverified += 1, // .get → None ∈ (…, None, …)
            Some(o) => {
                if !native(&o) {
                    return Ok(None);
                }
                let eq = |s: &str| o.eq(s).unwrap_or(false);
                if eq("verified") {
                    verified += 1;
                }
                if eq("supported") {
                    supported += 1;
                }
                if eq("disputed") {
                    disputed += 1;
                }
                if eq("contradicted") {
                    contradicted += 1;
                }
                if eq("candidate") {
                    candidate += 1;
                }
                if eq("rejected") {
                    rejected += 1;
                }
                if o.is_none() || eq("unverified") || eq("weak") || eq("") {
                    unverified += 1;
                }
            }
        }
    }
    let total = list.len();
    let accepted = verified;

    #[derive(PartialEq)]
    enum Branch {
        NoClaims,
        AllRejected,
        Contradicted,
        Disputed,
        VerifiedZero,
        Fine,
    }
    let branch = if total == 0 {
        Branch::NoClaims
    } else if rejected == total {
        Branch::AllRejected
    } else if contradicted > 0 && contradicted + rejected + unverified + candidate == total {
        Branch::Contradicted
    } else if disputed > 0 {
        Branch::Disputed
    } else if verified == 0 {
        Branch::VerifiedZero
    } else {
        Branch::Fine
    };

    // ---- 2. проверка типов для веток, которые читают поля ответа (до любого эффекта) ----
    if matches!(branch, Branch::Contradicted | Branch::Disputed | Branch::VerifiedZero) {
        let conf = match synth.getattr("confidence") {
            Ok(v) => v,
            Err(_) => return Ok(None),
        };
        let ans = match synth.getattr("answer") {
            Ok(v) => v,
            Err(_) => return Ok(None),
        };
        if !is_number(&conf) || ans.get_type().is(&py.get_type_bound::<PyString>()) == false {
            return Ok(None);
        }
        if matches!(branch, Branch::Disputed | Branch::VerifiedZero) {
            let tl = match synth.getattr("trust_level") {
                Ok(v) => v,
                Err(_) => return Ok(None),
            };
            if trust_rank(&tl).is_none() {
                return Ok(None);
            }
        }
    }

    let say = |m: String| -> PyResult<()> {
        log.call1((m,))?;
        Ok(())
    };
    say(format!(
        "[Claim Status Gate] verified={verified}, supported={supported}, disputed={disputed}, contradicted={contradicted}, candidate={candidate}, unverified={unverified}, rejected={rejected}, total={total}"
    ))?;

    // предупреждение в начало ответа, если его там ещё нет
    let prepend = |notice: String| -> PyResult<()> {
        let ans = synth.getattr("answer")?;
        let s: String = ans.extract()?;
        if !s.starts_with(WARN) {
            synth.setattr("answer", format!("{notice}\n{s}"))?;
        }
        Ok(())
    };
    let cap_conf = |cap: f64| -> PyResult<()> {
        let cur = synth.getattr("confidence")?;
        synth.setattr("confidence", py_min(py, &cur, cap)?)?;
        Ok(())
    };
    let rank_now = || -> PyResult<i32> { Ok(trust_rank(&synth.getattr("trust_level")?).unwrap_or(0)) };

    match branch {
        Branch::NoClaims => {
            say("[Claim Status Gate] Claims отсутствуют — статус UNVERIFIED".to_string())?;
            synth.setattr(
                "answer",
                "Я попыталась найти информацию.\n\nНо мне не удалось выделить достаточно проверяемых утверждений.\nЯ не могу дать уверенный ответ на этот вопрос.\n\nЕсли дашь дополнительный контекст — я попробую ещё раз.",
            )?;
            synth.setattr("trust_level", "UNVERIFIED")?;
            synth.setattr("confidence", 0.0f64)?;
        }
        Branch::AllRejected => {
            say("[Claim Status Gate] Все claims структурно отклонены".to_string())?;
            synth.setattr(
                "answer",
                "Я попыталась сформировать ответ, но выделенные утверждения не прошли структурную проверку.\n\nПоэтому я не могу считать этот ответ надёжным.",
            )?;
            synth.setattr("trust_level", "UNVERIFIED")?;
            synth.setattr("confidence", 0.0f64)?;
        }
        Branch::Contradicted => {
            say("[Claim Status Gate] Поддержанных claims нет, присутствуют опровергающие evidence".to_string())?;
            synth.setattr("trust_level", "UNVERIFIED")?;
            cap_conf(0.25)?;
            prepend(format!(
                "⚠️ ВАЖНО: часть проверяемых утверждений в этом ответе была ОПРОВЕРГНУТА найденными источниками (contradicted={contradicted} из {total}), и ни одно утверждение не получило прямого подтверждения. Текст ниже остаётся гипотезой модели — не считай его установленным фактом.\n"
            ))?;
        }
        Branch::Disputed => {
            say(format!("[Claim Status Gate] Обнаружены спорные claims: {disputed}"))?;
            if rank_now()? > 1 {
                synth.setattr("trust_level", "WEAKLY_SUPPORTED")?;
            }
            cap_conf(0.45)?;
            prepend(format!(
                "⚠️ ВАЖНО: часть проверяемых утверждений в этом ответе является СПОРНОЙ (disputed={disputed} из {total}) — по ним есть и подтверждающие, и опровергающие источники одновременно. Не считай эти пункты установленным фактом.\n"
            ))?;
        }
        Branch::VerifiedZero => {
            say(format!("[Claim Status Gate] verified=0, supported={supported} — ответ остаётся предварительным"))?;
            if rank_now()? > 2 {
                synth.setattr("trust_level", "PARTIALLY_SUPPORTED")?;
            }
            if supported == 0 {
                if rank_now()? > 1 {
                    synth.setattr("trust_level", "WEAKLY_SUPPORTED")?;
                }
                cap_conf(0.40)?;
                prepend(format!(
                    "⚠️ ВАЖНО: ни одно из {total} проверяемых утверждений не получило подтверждающих доказательств (supported=0, verified=0). Всё, что изложено ниже — неподтверждённая гипотеза модели, а не установленный факт. Система не получила достаточной evidence-базы для проверки.\n"
                ))?;
            } else {
                cap_conf(0.60)?;
                let mixed = unverified + candidate;
                if mixed > 0 {
                    prepend(format!(
                        "⚠️ ВАЖНО: не все утверждения в этом ответе подтверждены — {mixed} из {total} проверяемых утверждений не получили ни подтверждающих, ни опровергающих доказательств (unverified/candidate). Не считай их установленным фактом наравне с подтверждённой частью ответа.\n"
                    ))?;
                }
            }
        }
        Branch::Fine => {
            say(format!("[Claim Status Gate] Есть verified claims: {verified}/{total}"))?;
        }
    }
    Ok(Some((accepted, total, rejected)))
}

#[cfg(feature = "python")]
pub fn register_gate(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_evaluate_gate, m)?)?;
    Ok(())
}
