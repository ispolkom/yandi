//! Обидчивость и прощение (характер) — перенос `agent/relationship_memory.py`: жизненный цикл обиды registered→acknowledged→understood→healing→forgiven/unforgiven, ёмкость прощения,
//! минимальное время заживления, выбор обиды, к которой относится текущее сообщение. Простое «извини» не канает: извинение с низкой искренностью остаётся «услышанным».
use std::collections::HashSet;

use serde_json::{json, Value};
use yandi_rs::relationship_memory::stems as rs_stems;

use crate::causal_events;
use crate::ctx::{dt_secs, Ctx};
use crate::relationship_state;
use crate::R;

pub const MIN_HEALING_HOURS: f64 = 2.0;
pub const SINCERITY_AUTO_UNDERSTAND_THRESHOLD: f64 = 0.6;
pub const FORGIVENESS_MIN_SINCERITY: f64 = 0.4;
pub const FORGIVENESS_MIN_CAPACITY: f64 = 30.0;
pub const MAX_UNFORGIVEN_FOR_NEW_FORGIVENESS: i64 = 2;
pub const APOLOGY_LOCALITY_HOURS: f64 = 1.0;
pub const MAX_FOCUS_CANDIDATES: usize = 3;

fn f(v: &Value, k: &str) -> f64 {
    v.get(k).and_then(|x| x.as_f64()).unwrap_or(0.0)
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("")
}

/// Часов с начала ТЕКУЩЕЙ фазы заживления (`understood_at` — первое принятое извинение цикла); None — извинение ещё не принято.
fn healing_age_hours(cx: &Ctx, row: &Value) -> Option<f64> {
    let started = dt_secs(row.get("understood_at")?)?;
    Some((cx.now_secs() - started) / 3600.0)
}

/// Регистрирует новую обиду или усиливает открытую с тем же началом описания. Возвращает id (новой или усиленной); None — повтор той же доставки.
pub fn add_grievance(cx: &Ctx, user_id: &str, event_type: &str, description: &str, severity: f64, context: Option<&Value>, source_turn_id: Option<&str>, span: Option<(i64, i64)>) -> R<Option<String>> {
    let severity = severity.min(1.0);
    if !causal_events::may_apply(causal_events::claim(cx, user_id, source_turn_id, event_type, span)?) {
        return Ok(None);
    }
    let existing = cx.repo("find_similar_open_grievance", json!({"user_id": user_id, "description": description}))?;
    if existing.is_object() {
        let old = f(&existing, "severity");
        let new_severity = (old + severity * 0.3).min(1.0);
        let id = s(&existing, "id").to_string();
        cx.repo("bump_grievance", json!({"grievance_id": id, "new_severity": new_severity, "timestamp": cx.now_value()}))?;
        adjust_capacity(cx, user_id, -(new_severity - old) * 10.0)?;
        relationship_state::record_insult(cx, user_id, severity)?;
        return Ok(Some(id));
    }
    let id = format!("g_{}_{}", cx.now_secs() as i64, &cx.uuid_hex()[..8]);
    let mut args = json!({"grievance_id": id, "user_id": user_id, "event_type": event_type, "description": description, "severity": severity, "created_at": cx.now_value()});
    if let Some(c) = context {
        args["context"] = c.clone();
    }
    cx.repo("record_grievance", args)?;
    adjust_capacity(cx, user_id, -severity * 10.0)?;
    relationship_state::record_insult(cx, user_id, severity)?;
    Ok(Some(id))
}

/// Извинение услышано. Достаточно искреннее (> 0.6) сразу «понято» и частично восстанавливает ёмкость; простое «извини» остаётся «услышано».
pub fn acknowledge_apology(cx: &Ctx, grievance_id: &str, sincerity: f64) -> R<bool> {
    let g = cx.repo("get_grievance", json!({"grievance_id": grievance_id}))?;
    if !g.is_object() {
        return Ok(false);
    }
    let now = cx.now_value();
    if sincerity > SINCERITY_AUTO_UNDERSTAND_THRESHOLD {
        let first = g.get("understood_at").map(|v| v.is_null()).unwrap_or(true);
        let understood = if g["understood_at"].is_null() { now.clone() } else { g["understood_at"].clone() };
        cx.repo("update_grievance_status", json!({"grievance_id": grievance_id, "status": "understood", "apology_sincerity": sincerity, "apology_at": now, "understood_at": understood, "timestamp": now}))?;
        if first {
            adjust_capacity(cx, s(&g, "user_id"), sincerity * 5.0)?;
            relationship_state::record_accepted_apology(cx, s(&g, "user_id"), f(&g, "severity"), sincerity)?;
        }
    } else {
        cx.repo("update_grievance_status", json!({"grievance_id": grievance_id, "status": "acknowledged", "apology_sincerity": sincerity, "apology_at": now, "timestamp": now}))?;
    }
    Ok(true)
}

/// Один шаг заживления. True — обида (теперь или уже) прощена.
pub fn progress_healing(cx: &Ctx, grievance_id: &str) -> R<bool> {
    let g = cx.repo("get_grievance", json!({"grievance_id": grievance_id}))?;
    if !g.is_object() {
        return Ok(false);
    }
    let status = s(&g, "status").to_string();
    if status == "forgiven" || status == "unforgiven" {
        return Ok(status == "forgiven");
    }
    if forgiveness_conditions_met(cx, &g)? {
        let now = cx.now_value();
        cx.repo("update_grievance_status", json!({"grievance_id": grievance_id, "status": "forgiven", "forgiven_at": now, "timestamp": now}))?;
        let cap = f(&cx.repo("get_forgiveness_capacity", json!({"user_id": s(&g, "user_id")}))?, "capacity");
        cx.repo("set_forgiveness_capacity", json!({"user_id": s(&g, "user_id"), "capacity": (cap + 10.0).min(100.0), "last_forgiveness": now, "timestamp": now}))?;
        return Ok(true);
    }
    if status == "acknowledged" || status == "understood" {
        cx.repo("update_grievance_status", json!({"grievance_id": grievance_id, "status": "healing", "timestamp": cx.now_value()}))?;
    }
    Ok(false)
}

fn forgiveness_conditions_met(cx: &Ctx, g: &Value) -> R<bool> {
    if g["apology_at"].is_null() || g["understood_at"].is_null() {
        return Ok(false);
    }
    if f(g, "apology_sincerity") < FORGIVENESS_MIN_SINCERITY {
        return Ok(false);
    }
    match healing_age_hours(cx, g) {
        Some(h) if h >= MIN_HEALING_HOURS => {}
        _ => return Ok(false),
    }
    let uid = s(g, "user_id");
    if f(&cx.repo("get_forgiveness_capacity", json!({"user_id": uid}))?, "capacity") < FORGIVENESS_MIN_CAPACITY {
        return Ok(false);
    }
    let n = cx.repo("count_grievances_by_status", json!({"user_id": uid, "status": "unforgiven"}))?.as_i64().unwrap_or(0);
    Ok(n <= MAX_UNFORGIVEN_FOR_NEW_FORGIVENESS)
}

fn adjust_capacity(cx: &Ctx, user_id: &str, delta: f64) -> R<()> {
    let cur = f(&cx.repo("get_forgiveness_capacity", json!({"user_id": user_id}))?, "capacity");
    cx.repo("set_forgiveness_capacity", json!({"user_id": user_id, "capacity": (cur + delta).min(100.0).max(0.0), "timestamp": cx.now_value()}))?;
    Ok(())
}

pub fn get_active_grievances(cx: &Ctx, user_id: &str) -> R<Vec<Value>> {
    Ok(cx.repo("list_active_grievances", json!({"user_id": user_id}))?.as_array().cloned().unwrap_or_default())
}

pub fn get_summary(cx: &Ctx, user_id: &str) -> R<Value> {
    let active = get_active_grievances(cx, user_id)?;
    let cap = cx.repo("get_forgiveness_capacity", json!({"user_id": user_id}))?;
    let c = f(&cap, "capacity");
    Ok(json!({
        "active_grievances": active.len(),
        "forgiven": cx.repo("count_grievances_by_status", json!({"user_id": user_id, "status": "forgiven"}))?,
        "unforgiven": cx.repo("count_grievances_by_status", json!({"user_id": user_id, "status": "unforgiven"}))?,
        "forgiveness_capacity": format!("{c:.1}").parse::<f64>().unwrap_or(c),
        "last_forgiveness": cap["last_forgiveness"],
    }))
}

/// Сырые факты для чата: что было сказано, собственная оценка тяжести, на каком этапе.
pub fn memory_facts(g: &Value) -> Value {
    json!({"description": g["description"], "severity": g["severity"], "status": g["status"]})
}

pub fn most_severe_active_grievance(cx: &Ctx, user_id: &str) -> R<Value> {
    let active = get_active_grievances(cx, user_id)?;
    // max() Python: при равенстве — первый
    let mut best: Option<&Value> = None;
    for g in &active {
        if best.map(|b| f(g, "severity") > f(b, "severity")).unwrap_or(true) {
            best = Some(g);
        }
    }
    Ok(best.cloned().unwrap_or(Value::Null))
}

// ---------------------------------------------------------------- выбор обиды

/// Основы содержательных слов текста (без служебных) — та же нормализация, что у обид и обещаний.
pub fn content_stems(text: &str) -> HashSet<String> {
    rs_stems(text, true)
}

fn stems_all(text: &str) -> HashSet<String> {
    rs_stems(text, false)
}

/// Когда обида случилась в последний раз: у «registered» — updated_at (создание или последний повтор), иначе created_at.
fn offense_time(g: &Value) -> f64 {
    let created = g.get("created_at").and_then(dt_secs).unwrap_or(0.0);
    if s(g, "status") == "registered" {
        return g.get("updated_at").and_then(dt_secs).unwrap_or(created);
    }
    created
}

fn overlap(apology: &HashSet<String>, g: &Value) -> f64 {
    if apology.is_empty() {
        return 0.0;
    }
    let desc = stems_all(s(g, "description"));
    apology.intersection(&desc).count() as f64 / apology.len() as f64
}

#[derive(Debug, Clone)]
pub struct GrievanceMatch {
    pub grievance: Option<Value>,
    pub basis: &'static str,
    pub candidates: usize,
}

/// Чисто: выбирает НЕ БОЛЕЕ ОДНОЙ открытой обиды, о которой текущее сообщение. Что это за сообщение (обида/извинение/нейтральное) — не решает.
pub fn match_grievance_target(apology_text: &str, active: &[Value], resolved: &[Value], now: f64) -> GrievanceMatch {
    if active.is_empty() {
        return GrievanceMatch { grievance: None, basis: "no_active_grievance", candidates: 0 };
    }
    let apology = content_stems(apology_text);
    if !apology.is_empty() {
        let scored: Vec<(f64, &Value)> = active.iter().map(|g| (overlap(&apology, g), g)).collect();
        let best_active = scored.iter().map(|(sc, _)| *sc).fold(f64::NEG_INFINITY, f64::max);
        let best_resolved = resolved.iter().map(|g| overlap(&apology, g)).fold(0.0, f64::max);
        if best_resolved > best_active {
            return GrievanceMatch { grievance: None, basis: "names_resolved_grievance", candidates: active.len() };
        }
        if best_active > 0.0 {
            let top: Vec<&Value> = scored.iter().filter(|(sc, _)| *sc == best_active).map(|(_, g)| *g).collect();
            if top.len() == 1 {
                return GrievanceMatch { grievance: Some(top[0].clone()), basis: "explicit_reference", candidates: active.len() };
            }
            // min по ключу (-время, -тяжесть, id): при равенстве — первый
            let key = |g: &Value| (-offense_time(g), -f(g, "severity"), s(g, "id").to_string());
            let mut chosen = top[0];
            for g in &top[1..] {
                let (a, b) = (key(g), key(chosen));
                let less = a.0 < b.0 || (a.0 == b.0 && (a.1 < b.1 || (a.1 == b.1 && a.2 < b.2)));
                if less {
                    chosen = g;
                }
            }
            return GrievanceMatch { grievance: Some(chosen.clone()), basis: "explicit_reference_tie_recent", candidates: active.len() };
        }
    }
    if active.len() == 1 {
        return GrievanceMatch { grievance: Some(active[0].clone()), basis: "sole_active_grievance", candidates: 1 };
    }
    let local: Vec<&Value> = active.iter().filter(|g| (now - offense_time(g)) / 3600.0 <= APOLOGY_LOCALITY_HOURS).collect();
    if local.len() == 1 {
        return GrievanceMatch { grievance: Some(local[0].clone()), basis: "sole_recent_grievance", candidates: active.len() };
    }
    GrievanceMatch { grievance: None, basis: "ambiguous", candidates: active.len() }
}

/// Какая открытая обида (если есть) относится к ТЕКУЩЕМУ сообщению — решается ДО генерации ответа; ничего не пишет.
pub fn resolve_relationship_focus(cx: &Ctx, user_id: &str, current_text: &str) -> R<Value> {
    let active = get_active_grievances(cx, user_id)?;
    let resolved = if active.is_empty() { vec![] } else { cx.repo("list_recent_resolved_grievances", json!({"user_id": user_id}))?.as_array().cloned().unwrap_or_default() };
    let m = match_grievance_target(current_text, &active, &resolved, cx.now_secs());
    let mut candidates: Vec<Value> = vec![];
    if m.basis == "ambiguous" {
        let mut sorted = active.clone();
        sorted.sort_by(|a, b| offense_time(b).partial_cmp(&offense_time(a)).unwrap_or(std::cmp::Ordering::Equal)); // стабильно, новые первыми
        candidates = sorted.into_iter().take(MAX_FOCUS_CANDIDATES).collect();
    }
    Ok(json!({"grievance": m.grievance, "basis": m.basis, "open_count": active.len(), "candidates": candidates}))
}

/// Применить извинение к обиде, вокруг которой уже построен ответ: услышать → шаг заживления. Пишет не более одной обиды; повтор той же доставки — ничего.
pub fn apply_apology(cx: &Ctx, user_id: &str, grievance_id: Option<&str>, sincerity: f64, source_turn_id: Option<&str>, span: Option<(i64, i64)>) -> R<Value> {
    let mut result = json!({"target": null, "acknowledged": false, "forgiven": false});
    let Some(gid) = grievance_id.filter(|g| !g.is_empty()) else {
        return Ok(result);
    };
    let g = cx.repo("get_grievance", json!({"grievance_id": gid}))?;
    if !g.is_object() || s(&g, "user_id") != user_id || matches!(s(&g, "status"), "forgiven" | "unforgiven") {
        return Ok(result);
    }
    if !causal_events::may_apply(causal_events::claim(cx, user_id, source_turn_id, "apology", span)?) {
        return Ok(result);
    }
    result["target"] = json!(gid);
    result["acknowledged"] = json!(acknowledge_apology(cx, gid, sincerity)?);
    result["forgiven"] = json!(progress_healing(cx, gid)?);
    Ok(result)
}
