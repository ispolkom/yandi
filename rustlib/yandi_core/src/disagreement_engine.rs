//! Двигатель спора — перенос `agent/disagreement_engine.py`: спор как обучающий эпизод (старая позиция → возражение → анализ → новая позиция).
//! Эпизод пишется в таблицу `disagreement` (только добавление); если указано связанное убеждение — оно оспаривается через `belief_manager::challenge_belief`.
//! Как и в оригинале, запись эпизода и оспаривание убеждения — две отдельные операции: сбой второй не отменяет первую.
//! Конструктор оригинала применяет затухание убеждений (`BeliefManager()`) — здесь это явный вызов `belief_manager::apply_decay` со стороны хозяина.
use serde_json::{json, Value};

use crate::belief_manager;
use crate::ctx::Ctx;
use crate::reflection_loop::py_repr;
use crate::R;

fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|x| x != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `round(x, 2)` Python.
fn round2(x: f64) -> f64 {
    format!("{x:.2}").parse().unwrap_or(x)
}

pub struct Challenge<'a> {
    pub topic: &'a str,
    pub old_position: &'a str,
    pub challenge: &'a str,
    pub analysis: &'a str,
    pub new_position: &'a str,
    pub confidence_before: f64,
    pub confidence_after: f64,
    pub related_belief_id: Option<&'a str>,
}

/// Зафиксировать эпизод спора.
pub fn challenge(cx: &Ctx, c: &Challenge) -> R<Value> {
    let id = format!("dag_{}", cut(&cx.uuid_hex(), 8));
    cx.repo(
        "create_disagreement",
        json!({
            "disagreement_id": id, "topic": c.topic, "old_position": c.old_position, "challenge": c.challenge, "analysis": c.analysis, "new_position": c.new_position,
            "confidence_before": c.confidence_before, "confidence_after": c.confidence_after, "resolved": true, "related_belief_id": c.related_belief_id, "created_at": cx.now_value(),
        }),
    )?;
    if let Some(bid) = c.related_belief_id.filter(|b| !b.is_empty()) {
        belief_manager::challenge_belief(cx, bid, c.challenge, c.confidence_after, &format!("спор: {}", cut(c.analysis, 50)))?;
    }
    Ok(json!({
        "id": id, "topic": c.topic, "old_position": c.old_position, "challenge": c.challenge, "analysis": c.analysis, "new_position": c.new_position,
        "confidence_before": c.confidence_before, "confidence_after": c.confidence_after, "resolved": true, "related_belief_id": c.related_belief_id,
    }))
}

fn all(cx: &Ctx) -> R<Vec<Value>> {
    Ok(cx.repo("list_disagreements", json!({}))?.as_array().cloned().unwrap_or_default())
}

/// `disagreements[-limit:][::-1]` со всеми особенностями срезов Python (0 — весь список, отрицательное — с отступом с начала).
pub fn get_recent(cx: &Ctx, limit: i64) -> R<Vec<Value>> {
    let v = all(cx)?;
    let n = v.len() as i64;
    let start = if limit == 0 {
        0
    } else if limit > 0 {
        (n - limit).max(0)
    } else {
        (-limit).min(n)
    };
    Ok(v[start as usize..].iter().rev().cloned().collect())
}

pub fn get_by_topic(cx: &Ctx, topic: &str) -> R<Vec<Value>> {
    Ok(all(cx)?.into_iter().filter(|d| d["topic"].as_str() == Some(topic)).collect())
}

fn f(d: &Value, k: &str) -> f64 {
    d[k].as_f64().unwrap_or(0.0)
}

pub fn get_stats(cx: &Ctx) -> R<Value> {
    let v = all(cx)?;
    let mut topics = serde_json::Map::new();
    for d in &v {
        let t = d["topic"].as_str().unwrap_or("").to_string();
        let n = topics.get(&t).and_then(|x| x.as_i64()).unwrap_or(0) + 1;
        topics.insert(t, json!(n));
    }
    let total: f64 = v.iter().fold(0.0, |acc, d| acc + (f(d, "confidence_before") - f(d, "confidence_after")));
    // пустой список: `avg_change = 0` (целое) → `round(0, 2)` = 0 (целое)
    let avg = if v.is_empty() { json!(0) } else { json!(round2(total / v.len() as f64)) };
    Ok(json!({"total": v.len(), "topics": topics, "avg_confidence_change": avg, "resolved": v.iter().filter(|d| truthy(&d["resolved"])).count()}))
}

pub fn summary(cx: &Ctx) -> R<String> {
    let stats = get_stats(cx)?;
    let recent = get_recent(cx, 3)?;
    let topics = stats["topics"].as_object().cloned().unwrap_or_default();
    let topics_s: Vec<String> = topics.iter().map(|(k, v)| format!("{k}={}", v.as_i64().unwrap_or(0))).collect();
    let recent_s = if recent.is_empty() {
        "  нет".to_string()
    } else {
        recent
            .iter()
            .map(|d| format!("  - [{:.2}→{:.2}] {}: {} → {}", f(d, "confidence_before"), f(d, "confidence_after"), d["topic"].as_str().unwrap_or(""), cut(d["old_position"].as_str().unwrap_or(""), 30), cut(d["new_position"].as_str().unwrap_or(""), 30)))
            .collect::<Vec<_>>()
            .join("\n")
    };
    Ok(format!(
        "\n=== DISAGREEMENT ENGINE ===\nВсего споров: {}\nРазрешено: {}\nСреднее изменение уверенности: {}\nТемы: {}\n\nПоследние споры:\n{}\n",
        stats["total"], stats["resolved"], py_repr(&stats["avg_confidence_change"]), topics_s.join(", "), recent_s
    ))
}
