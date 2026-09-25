//! Модель себя — перенос `agent/self_model.py`: кто она, что делала, как менялась, почему решала, как менялись убеждения. Состояние — одна строка `self_state`, история — только
//! добавляемые `self_event`. ЖЁСТКИЙ ОТКАЗ, не тихий: любая ошибка базы уходит вызывающему (запасного пути нет).
use serde_json::{json, Map, Value};

use crate::ctx::Ctx;
use crate::R;

pub const DEFAULT_CAPABILITIES: [&str; 7] = ["reasoning", "retrieval", "reflection", "epistemic_classification", "trust_evaluation", "belief_management", "self_awareness"];
pub const DEFAULT_LIMITATIONS: [&str; 4] = ["cannot verify subjective experience", "cannot predict future", "limited to available data", "beliefs are probabilistic"];

/// Явное решение владельца: характер женский; её собственное публичное присутствие (читает `_self_knowledge_message` в чате).
pub fn default_character() -> Value {
    json!({
        "gender": "female", "pronoun_ru": "она", "pronoun_en": "she/her",
        "github_repo": "https://github.com/ispolkom/yandi", "website": "https://yandi.su/",
    })
}

fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn is_json_array(v: &Value) -> bool {
    v.is_array()
}

/// Создать строку состояния, если её ещё нет (конструктор `SelfModel()`).
pub fn init(cx: &Ctx) -> R<Value> {
    cx.repo(
        "get_or_create_self_state",
        json!({"identity": "YANDI", "version": "v5.0", "capabilities": DEFAULT_CAPABILITIES, "limitations": DEFAULT_LIMITATIONS, "current_uncertainties": [], "metadata": {"character": default_character()}, "created_at": cx.now_value()}),
    )
}

/// `_row()`: строка состояния; её отсутствие — ошибка (как `TypeError` при `None["..."]`).
pub fn row(cx: &Ctx) -> R<Value> {
    let r = cx.repo("get_self_state", json!({}))?;
    if r.is_object() {
        Ok(r)
    } else {
        Err("TypeError".into())
    }
}

fn field(r: &Value, k: &str) -> R<Value> {
    r.get(k).cloned().ok_or_else(|| "KeyError".to_string())
}

fn update_lists(cx: &Ctx, key: &str, value: Value) -> R<()> {
    cx.repo("update_self_state_lists", json!({key: value, "updated_at": cx.now_value()}))?;
    Ok(())
}

fn counter(cx: &Ctx, name: &str) -> R<()> {
    cx.repo("increment_self_state_counter", json!({"counter": name, "updated_at": cx.now_value()}))?;
    Ok(())
}

/// Слить новые факты в `metadata["character"]`, не затирая остальное в metadata.
pub fn declare_character_trait(cx: &Ctx, traits: &Map<String, Value>) -> R<Value> {
    let current = cx.repo("get_self_state", json!({}))?;
    let mut metadata = current.get("metadata").and_then(|m| m.as_object()).cloned().unwrap_or_default();
    let mut character = metadata.get("character").and_then(|m| m.as_object()).cloned().unwrap_or_default();
    for (k, v) in traits {
        character.insert(k.clone(), v.clone());
    }
    metadata.insert("character".into(), Value::Object(character.clone()));
    update_lists(cx, "metadata", Value::Object(metadata))?;
    Ok(Value::Object(character))
}

pub fn add_event(cx: &Ctx, event_type: &str, description: &str, details: Value, importance: Value) -> R<String> {
    let event_id = format!("ev_{}", cut(&cx.uuid_hex(), 8));
    cx.repo("record_self_event", json!({"event_id": event_id, "event_type": event_type, "description": description, "details": details, "importance": importance, "created_at": cx.now_value()}))?;
    Ok(event_id)
}

fn dget<'a>(d: &'a Value, k: &str) -> R<Option<&'a Value>> {
    if !d.is_object() {
        return Err("AttributeError".into());
    }
    Ok(d.get(k))
}

/// `d.get(k, default)[:n]` для строки: не строка → `TypeError`.
fn dstr(d: &Value, k: &str, default: &str, n: usize) -> R<String> {
    match dget(d, k)? {
        None => Ok(cut(default, n)),
        Some(Value::String(s)) => Ok(cut(s, n)),
        Some(_) => Err("TypeError".into()),
    }
}

pub fn add_decision(cx: &Ctx, decision: &Value) -> R<()> {
    counter(cx, "total_decisions")?;
    let q50 = dstr(decision, "query", "", 50)?;
    let q100 = dstr(decision, "query", "", 100)?;
    let g = |k: &str, default: Value| -> R<Value> { Ok(dget(decision, k)?.cloned().unwrap_or(default)) };
    let confidence = g("confidence", json!(0.5))?;
    add_event(
        cx,
        "decision",
        &format!("Решение по запросу: {q50}"),
        json!({"query": q100, "domain": g("domain", json!(""))?, "answer_mode": g("answer_mode", json!(""))?, "trust": g("trust", json!(""))?, "confidence": confidence, "reason": g("reason", json!(""))?}),
        confidence,
    )?;
    Ok(())
}

pub fn add_learning(cx: &Ctx, lesson: &str, context: &str, importance: Value) -> R<()> {
    counter(cx, "total_learnings")?;
    add_event(cx, "learning", &format!("Урок: {}", cut(lesson, 60)), json!({"lesson": lesson, "context": context, "importance": importance.clone()}), importance)?;
    Ok(())
}

pub fn add_reflection(cx: &Ctx, reflection: &Value) -> R<()> {
    counter(cx, "total_reflections")?;
    let summary = dstr(reflection, "summary", "Рефлексия", 60)?;
    add_event(cx, "reflection", &summary, reflection.clone(), json!(0.7))?;
    Ok(())
}

pub fn add_error(cx: &Ctx, error: &str, context: &Value, severity: Value) -> R<()> {
    counter(cx, "total_errors")?;
    add_event(cx, "error", &format!("Ошибка: {}", cut(error, 60)), json!({"error": error, "context": context}), severity)?;
    Ok(())
}

/// `f"{x:.2f}"` для числа Python (int/float); не число — `TypeError`/`ValueError` как в Python.
fn f2(v: &Value) -> R<String> {
    match v {
        Value::Number(n) => Ok(format!("{:.2}", n.as_f64().unwrap_or(0.0))),
        Value::Bool(b) => Ok(format!("{:.2}", *b as i64 as f64)),
        _ => Err("TypeError".into()),
    }
}

pub fn add_belief_update(cx: &Ctx, topic: &str, old_confidence: Value, new_confidence: Value, reason: &str) -> R<()> {
    counter(cx, "total_belief_updates")?;
    add_event(
        cx,
        "belief_update",
        &format!("Изменение убеждения: {topic} ({} → {})", f2(&old_confidence)?, f2(&new_confidence)?),
        json!({"topic": topic, "old_confidence": old_confidence, "new_confidence": new_confidence, "reason": reason}),
        json!(0.7),
    )?;
    Ok(())
}

pub fn add_change(cx: &Ctx, what_changed: &str, before: &Value, after: &Value, reason: &str) -> R<()> {
    add_event(cx, "change", &format!("Изменение: {}", cut(what_changed, 50)), json!({"what": what_changed, "before": before, "after": after, "reason": reason}), json!(0.6))?;
    Ok(())
}

/// Добавить в список `key`, если такого значения там ещё нет.
fn add_unique(cx: &Ctx, key: &str, item: &str) -> R<()> {
    let r = row(cx)?;
    let list = field(&r, key)?;
    if !is_json_array(&list) {
        return Err("TypeError".into());
    }
    let items = list.as_array().unwrap();
    if !items.iter().any(|x| x.as_str() == Some(item)) {
        let mut next = items.clone();
        next.push(json!(item));
        update_lists(cx, key, Value::Array(next))?;
    }
    Ok(())
}

pub fn add_capability(cx: &Ctx, capability: &str) -> R<()> {
    add_unique(cx, "capabilities", capability)
}

pub fn add_limitation(cx: &Ctx, limitation: &str) -> R<()> {
    add_unique(cx, "limitations", limitation)
}

pub fn add_uncertainty(cx: &Ctx, uncertainty: &str) -> R<()> {
    add_unique(cx, "current_uncertainties", uncertainty)
}

pub fn remove_uncertainty(cx: &Ctx, uncertainty: &str) -> R<()> {
    let r = row(cx)?;
    let list = field(&r, "current_uncertainties")?;
    let items = list.as_array().ok_or_else(|| "TypeError".to_string())?;
    if items.iter().any(|x| x.as_str() == Some(uncertainty)) {
        let remaining: Vec<Value> = items.iter().filter(|x| x.as_str() != Some(uncertainty)).cloned().collect();
        update_lists(cx, "current_uncertainties", Value::Array(remaining))?;
    }
    Ok(())
}

pub fn increment_cycle(cx: &Ctx) -> R<()> {
    counter(cx, "total_cycles")?;
    let r = row(cx)?;
    let n = field(&r, "total_cycles")?;
    add_event(cx, "cycle", &format!("Цикл #{}", n), json!({"cycle": n}), json!(0.3))?;
    Ok(())
}

pub fn increment_queries(cx: &Ctx) -> R<()> {
    counter(cx, "total_queries")
}

pub fn increment_errors(cx: &Ctx) -> R<()> {
    counter(cx, "total_errors")
}

pub fn increment_reflections(cx: &Ctx) -> R<()> {
    counter(cx, "total_reflections")
}

pub fn get_metadata(cx: &Ctx) -> R<Value> {
    field(&row(cx)?, "metadata")
}

pub fn set_metadata_value(cx: &Ctx, key: &str, value: Value) -> R<()> {
    let mut metadata = field(&row(cx)?, "metadata")?.as_object().cloned().ok_or_else(|| "TypeError".to_string())?;
    metadata.insert(key.to_string(), value);
    update_lists(cx, "metadata", Value::Object(metadata))
}

pub fn get_goals(cx: &Ctx) -> R<Value> {
    Ok(get_metadata(cx)?.get("goals").cloned().unwrap_or_else(|| json!([])))
}

pub fn set_goals(cx: &Ctx, goals: Value) -> R<()> {
    set_metadata_value(cx, "goals", goals)
}

pub fn add_goal(cx: &Ctx, goal: &str) -> R<()> {
    let goals = get_goals(cx)?;
    let items = goals.as_array().ok_or_else(|| "TypeError".to_string())?;
    if !items.iter().any(|g| g.as_str() == Some(goal)) {
        let mut next = items.clone();
        next.push(json!(goal));
        set_metadata_value(cx, "goals", Value::Array(next))?;
    }
    Ok(())
}

/// Подробности последних событий данного типа (новые первыми).
pub fn events_details(cx: &Ctx, event_type: &str, limit: i64) -> R<Value> {
    let rows = cx.repo("get_self_events_by_type", json!({"event_type": event_type, "limit": limit}))?;
    Ok(json!(rows.as_array().map(|a| a.iter().map(|r| r["details"].clone()).collect::<Vec<_>>()).unwrap_or_default()))
}

fn fmt_dt(v: &Value) -> String {
    match v.as_str() {
        Some(s) => s.to_string(),
        None => "1970-01-01 00:00:00".to_string(),
    }
}

fn first_n(v: &Value, n: usize) -> Value {
    json!(v.as_array().map(|a| a.iter().take(n).cloned().collect::<Vec<_>>()).unwrap_or_default())
}

fn py_bool(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|x| x != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Рефлексивный отчёт о состоянии.
pub fn reflect(cx: &Ctx) -> R<Value> {
    let r = row(cx)?;
    let events_total = cx.repo("count_self_events", json!({}))?;
    let recent = cx.repo("get_recent_self_events", json!({"limit": 1}))?;
    let last_event = recent.as_array().and_then(|a| a.first()).map(|e| e["description"].clone()).unwrap_or(Value::Null);
    Ok(json!({
        "identity": field(&r, "identity")?, "version": field(&r, "version")?, "age": field(&r, "total_cycles")?,
        "total_queries": field(&r, "total_queries")?, "total_decisions": field(&r, "total_decisions")?, "total_learnings": field(&r, "total_learnings")?,
        "total_reflections": field(&r, "total_reflections")?, "total_errors": field(&r, "total_errors")?, "total_belief_updates": field(&r, "total_belief_updates")?,
        "capabilities": first_n(&field(&r, "capabilities")?, 5), "limitations": first_n(&field(&r, "limitations")?, 5), "uncertainties": first_n(&field(&r, "current_uncertainties")?, 5),
        "events_total": events_total, "last_event": last_event,
        "is_alive": py_bool(&field(&r, "is_alive")?), "last_update": fmt_dt(&field(&r, "updated_at")?),
    }))
}

/// Здоровье: ошибок больше 20, «мёртвая» пометка, мало запросов при большом числе циклов.
pub fn check_health(cx: &Ctx) -> R<Value> {
    let r = row(cx)?;
    let num = |k: &str| -> R<i64> { field(&r, k)?.as_i64().ok_or_else(|| "TypeError".to_string()) };
    let alive = py_bool(&field(&r, "is_alive")?);
    let mut issues: Vec<String> = vec![];
    let mut warnings: Vec<String> = vec![];
    if num("total_errors")? > 20 {
        warnings.push(format!("Много ошибок: {}", num("total_errors")?));
    }
    if !alive {
        issues.push("Система помечена как мёртвая".into());
    }
    if num("total_queries")? < 1 && num("total_cycles")? > 20 {
        warnings.push("Мало запросов при большом количестве циклов".into());
    }
    let score = (100 - issues.len() as i64 * 20 - warnings.len() as i64 * 5).clamp(0, 100);
    Ok(json!({"status": if alive { "alive" } else { "dead" }, "issues": issues, "warnings": warnings, "health_score": score}))
}

pub fn get_timeline(cx: &Ctx, limit: i64) -> R<Value> {
    let rows = cx.repo("get_recent_self_events", json!({"limit": limit}))?;
    Ok(json!(rows
        .as_array()
        .map(|a| a
            .iter()
            .map(|r| json!({"time": fmt_dt(&r["created_at"]), "type": r["event_type"], "description": cut(r["description"].as_str().unwrap_or(""), 100), "importance": r["importance"]}))
            .collect::<Vec<_>>())
        .unwrap_or_default()))
}

fn join3(v: &Value) -> String {
    v.as_array().map(|a| a.iter().take(3).map(|x| x.as_str().map(String::from).unwrap_or_else(|| x.to_string())).collect::<Vec<_>>().join(", ")).unwrap_or_default()
}

fn s(v: &Value) -> String {
    match v {
        Value::String(x) => x.clone(),
        other => other.to_string(),
    }
}

/// Краткое текстовое представление (для человека и журналов).
pub fn summary(cx: &Ctx) -> R<String> {
    let st = reflect(cx)?;
    let recent = get_timeline(cx, 3)?;
    let caps = join3(&st["capabilities"]);
    let lims = join3(&st["limitations"]);
    let unc = join3(&st["uncertainties"]);
    let events: Vec<String> = recent.as_array().unwrap().iter().map(|e| format!("  - {} | {} | {}", s(&e["time"]), s(&e["type"]), s(&e["description"]))).collect();
    let alive = if st["is_alive"] == json!(true) { "✅" } else { "❌" };
    Ok(format!(
        "\n=== YANDI SELF MODEL V5 ===\nИдентичность: {} {}\nВозраст: {} циклов\nЗапросов: {}\nРешений: {}\nУроков: {}\nРефлексий: {}\nОшибок: {}\nОбновлений убеждений: {}\nСобытий: {}\n\nВозможности: {}\nОграничения: {}\nНеопределённости: {}\n\nПоследние события:\n{}\n\nЖива: {}\nПоследнее обновление: {}\n",
        s(&st["identity"]), s(&st["version"]), s(&st["age"]), s(&st["total_queries"]), s(&st["total_decisions"]), s(&st["total_learnings"]), s(&st["total_reflections"]), s(&st["total_errors"]),
        s(&st["total_belief_updates"]), s(&st["events_total"]),
        if caps.is_empty() { "не заданы".to_string() } else { caps },
        if lims.is_empty() { "не заданы".to_string() } else { lims },
        if unc.is_empty() { "нет".to_string() } else { unc },
        if events.is_empty() { "  нет".to_string() } else { events.join("\n") },
        alive, s(&st["last_update"]),
    ))
}

/// Строка-представление `SelfModel(age=…, events=…)`.
pub fn repr(cx: &Ctx) -> R<String> {
    let r = row(cx)?;
    let events_total = cx.repo("count_self_events", json!({}))?;
    Ok(format!("SelfModel(age={}, events={})", field(&r, "total_cycles")?, events_total))
}
