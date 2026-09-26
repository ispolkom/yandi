//! Ядро личности — перенос `agent/personality_core.py`: устойчивая идентичность (имя, черты, цели, принципы, ограничения, предпочтения, счётчики) в одной строке таблицы `personality`
//! и журнал изменений `personality_change` (только добавление). Ничего не кэшируется между вызовами — каждое чтение идёт в базу.
//! Ошибки базы не глотаются, как в оригинале.
use serde_json::{json, Value};

use crate::ctx::Ctx;
use crate::R;

const DEFAULT_TRAITS: [&str; 5] = ["curious", "cautious", "honest", "reflective", "adaptive"];
const DEFAULT_GOALS: [&str; 4] = ["understand the world", "avoid misinformation", "help users effectively", "learn from mistakes"];
const DEFAULT_PRINCIPLES: [&str; 4] = ["never lie", "admit uncertainty", "separate facts from interpretations", "learn from evidence"];
const DEFAULT_LIMITATIONS: [&str; 3] = ["cannot verify subjective experience", "cannot predict future with certainty", "limited to available data"];

/// Конструктор оригинала: создаёт строку с настройками по умолчанию, если её ещё нет.
pub fn init(cx: &Ctx) -> R<()> {
    cx.repo(
        "get_or_create_personality",
        json!({
            "name": "YANDI", "version": "v6.0", "traits": DEFAULT_TRAITS, "goals": DEFAULT_GOALS, "principles": DEFAULT_PRINCIPLES, "limitations": DEFAULT_LIMITATIONS,
            "preferences": {"reasoning_style": "balanced", "response_style": "clear_and_honest", "risk_tolerance": 0.3, "curiosity_level": 0.7},
            "created_at": cx.now_value(),
        }),
    )?;
    Ok(())
}

/// Единственная строка личности; нет строки — `TypeError` (в оригинале `None["name"]`).
fn row(cx: &Ctx) -> R<Value> {
    let r = cx.repo("get_personality", json!({}))?;
    if r.is_null() {
        return Err("TypeError".into());
    }
    Ok(r)
}

pub fn get_name(cx: &Ctx) -> R<Value> {
    Ok(row(cx)?["name"].clone())
}

pub fn get_list(cx: &Ctx, key: &str) -> R<Value> {
    Ok(row(cx)?[key].clone())
}

/// `add_trait` / `add_goal` / `add_principle` / `add_limitation`: добавляет, только если такого значения ещё нет.
pub fn add_to_list(cx: &Ctx, key: &str, item: &Value) -> R<()> {
    let r = row(cx)?;
    let cur = r[key].as_array().cloned().unwrap_or_default();
    if !cur.contains(item) {
        let mut next = cur;
        next.push(item.clone());
        cx.repo("update_personality_lists", json!({key: next, "updated_at": cx.now_value()}))?;
    }
    Ok(())
}

pub fn record_change(cx: &Ctx, what_changed: &str, reason: &str) -> R<()> {
    cx.repo("record_personality_change", json!({"what_changed": what_changed, "reason": reason, "created_at": cx.now_value()}))?;
    Ok(())
}

/// `increment_cycles` / `increment_decisions` / `increment_learnings`.
pub fn increment(cx: &Ctx, counter: &str) -> R<()> {
    cx.repo("increment_personality_counter", json!({"counter": counter, "updated_at": cx.now_value()}))?;
    Ok(())
}

fn first3(v: &Value) -> Value {
    json!(v.as_array().map(|a| a.iter().take(3).cloned().collect::<Vec<_>>()).unwrap_or_default())
}

pub fn get_summary(cx: &Ctx) -> R<Value> {
    let r = row(cx)?;
    let changes = cx.repo("count_personality_changes", json!({}))?;
    Ok(json!({
        "name": r["name"], "version": r["version"], "traits": r["traits"], "goals": first3(&r["goals"]), "principles": first3(&r["principles"]), "limitations": first3(&r["limitations"]),
        "cycles": r["total_cycles"], "decisions": r["total_decisions"], "learnings": r["total_learnings"], "changes": changes,
    }))
}

fn join(v: &Value) -> String {
    v.as_array().map(|a| a.iter().map(|x| x.as_str().unwrap_or("").to_string()).collect::<Vec<_>>().join(", ")).unwrap_or_default()
}

pub fn summary(cx: &Ctx) -> R<String> {
    let s = get_summary(cx)?;
    let t = |k: &str| s[k].as_str().unwrap_or("").to_string();
    Ok(format!(
        "\n=== PERSONALITY CORE ===\nИмя: {} {}\nЧерты: {}\nЦели: {}\nПринципы: {}\nОграничения: {}\nЦиклов: {} | Решений: {} | Обучений: {}\nИзменений личности: {}\n",
        t("name"), t("version"), join(&s["traits"]), join(&s["goals"]), join(&s["principles"]), join(&s["limitations"]), s["cycles"], s["decisions"], s["learnings"], s["changes"]
    ))
}
