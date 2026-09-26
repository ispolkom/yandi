//! Внутреннее состояние личности — перенос `agent/inner_state.py`: единая модель вместо россыпи переменных (доверие, уважение, раздражение…). Три слоя: «Я» (настроение, энергия,
//! любопытство, терпение, открытость), отношения с человеком (доверие, уважение, прощение, привязанность, шаблон поведения) и «сейчас» (чувство, намерение, тон). Состояние — одна строка
//! `inner_state`, история событий — только добавляемая `inner_state_event`. ЖЁСТКИЙ ОТКАЗ: ошибка базы уходит вызывающему.
use serde_json::{json, Map, Value};

use crate::ctx::{dt_secs, Ctx};
use crate::R;

fn f(row: &Value, k: &str) -> R<f64> {
    row.get(k).and_then(|v| v.as_f64()).ok_or_else(|| "TypeError".to_string())
}

fn sv(row: &Value, k: &str) -> Option<String> {
    row.get(k).and_then(|v| v.as_str()).map(String::from)
}

fn clamp(x: f64, lo: f64, hi: f64) -> f64 {
    x.min(hi).max(lo)
}

/// Python `round(x, 1)`.
fn round1(x: f64) -> f64 {
    format!("{x:.1}").parse().unwrap_or(x)
}

fn has(s: &str, needle: &str) -> bool {
    s.contains(needle)
}

/// Создать строку состояния человека, если её нет (конструктор `InnerStateManager(user_id)`).
pub fn init(cx: &Ctx, user_id: &str) -> R<Value> {
    cx.repo("get_or_create_inner_state", json!({"user_id": user_id, "updated_at": cx.now_value()}))
}

fn row(cx: &Ctx, user_id: &str) -> R<Value> {
    let r = cx.repo("get_inner_state", json!({"user_id": user_id}))?;
    if r.is_object() {
        Ok(r)
    } else {
        Err("TypeError".into())
    }
}

/// История событий (последние 200, старые первыми).
pub fn get_history(cx: &Ctx, user_id: &str) -> R<Vec<Value>> {
    Ok(cx.repo("list_inner_state_events", json!({"user_id": user_id}))?.as_array().cloned().unwrap_or_default())
}

/// Вес события: базовый вес по типу, искренность усиливает хорошее и смягчает плохое, низкое доверие усугубляет плохое, высокое — ослабляет хорошее, повторные оскорбления усугубляются.
fn calculate_weight(row: &Value, history: &[Value], event_type: &str, sincerity: f64) -> R<f64> {
    let mut weight = match event_type {
        "severe_insult" => -3.0,
        "moderate_insult" => -2.0,
        "mild_insult" => -1.0,
        "sincere_apology" => 2.0,
        "formal_apology" => 0.8,
        "thanks" => 1.0,
        "help" => 0.8,
        "constructive_criticism" => 0.3,
        "honesty" => 1.5,
        "dishonesty" => -2.5,
        "provocation" => -1.5,
        "respect" => 1.0,
        "disrespect" => -1.5,
        _ => 0.0,
    };
    if weight > 0.0 {
        weight *= sincerity;
    } else {
        weight *= 1.0 + (1.0 - sincerity) * 0.3;
    }
    let current_trust = f(row, "trust")?;
    if weight < 0.0 && current_trust < 30.0 {
        weight *= 1.3;
    }
    if weight > 0.0 && current_trust > 70.0 {
        weight *= 0.7;
    }
    if ["insult", "severe_insult", "moderate_insult", "mild_insult"].contains(&event_type) {
        let insult_count = history.iter().filter(|e| e["event_type"].as_str().map(|t| has(t, "insult")).unwrap_or(false)).count();
        if insult_count > 2 {
            weight *= 1.2;
        }
    }
    Ok(weight)
}

/// Обновлённые параметры отношений.
fn update_relationship(cx: &Ctx, row: &Value, event_type: &str, sincerity: f64, weight: f64) -> R<Map<String, Value>> {
    let trust = clamp(f(row, "trust")? + weight * 5.0, 0.0, 100.0);

    let mut respect = f(row, "respect")?;
    if has(event_type, "insult") {
        let severity = if has(event_type, "severe") { 1.0 } else if has(event_type, "moderate") { 0.6 } else { 0.3 };
        respect -= 10.0 * severity;
    } else if event_type == "sincere_apology" || event_type == "thanks" || event_type == "help" {
        respect += if event_type == "sincere_apology" { 8.0 } else { 5.0 };
    }
    let respect = clamp(respect, 0.0, 100.0);

    let mut forgiveness = f(row, "forgiveness")?;
    if event_type == "sincere_apology" {
        forgiveness += 10.0 * sincerity;
    } else if event_type == "formal_apology" {
        forgiveness += 3.0;
    } else if has(event_type, "insult") {
        forgiveness -= 5.0;
    }
    let updated = row.get("updated_at").and_then(dt_secs).unwrap_or_else(|| cx.now_secs());
    let days_since_last = (cx.now_secs() - updated) / 86400.0;
    if days_since_last > 1.0 {
        forgiveness += (days_since_last * 2.0).min(5.0);
    }
    let forgiveness = clamp(forgiveness, 0.0, 100.0);

    let mut affection = f(row, "affection")?;
    if event_type == "thanks" {
        affection += 3.0;
    } else if event_type == "help" {
        affection += 2.0;
    } else if event_type == "constructive_criticism" {
        affection += 1.0;
    } else if has(event_type, "insult") {
        affection -= 5.0;
    }
    let affection = clamp(affection, 0.0, 100.0);

    let mut m = Map::new();
    m.insert("trust".into(), json!(trust));
    m.insert("respect".into(), json!(respect));
    m.insert("forgiveness".into(), json!(forgiveness));
    m.insert("affection".into(), json!(affection));
    Ok(m)
}

/// Настроение по состоянию.
fn calculate_mood(row: &Value) -> R<&'static str> {
    let (trust, energy, curiosity, forgiveness, affection) = (f(row, "trust")?, f(row, "energy")?, f(row, "curiosity")?, f(row, "forgiveness")?, f(row, "affection")?);
    Ok(if energy < 30.0 && trust < 30.0 {
        "tired"
    } else if trust > 70.0 && affection > 50.0 && energy > 60.0 {
        "warm"
    } else if curiosity > 70.0 && energy > 50.0 {
        "curious"
    } else if trust < 30.0 && forgiveness < 30.0 {
        "hurt"
    } else if trust < 30.0 {
        "guarded"
    } else if energy < 40.0 {
        "tired"
    } else if forgiveness < 30.0 {
        "resentful"
    } else if trust > 60.0 && energy > 60.0 {
        "grateful"
    } else {
        "calm"
    })
}

/// Обновлённое самоощущение.
fn update_self(row: &Value, event_type: &str, weight: f64) -> R<Map<String, Value>> {
    let mut energy = f(row, "energy")?;
    if weight < 0.0 {
        energy -= weight.abs() * 3.0;
    } else {
        energy += weight * 2.0;
    }
    let energy = clamp(energy, 20.0, 100.0);

    let mut curiosity = f(row, "curiosity")?;
    if ["constructive_criticism", "help", "honesty"].contains(&event_type) {
        curiosity += 5.0;
    } else if has(event_type, "insult") {
        curiosity -= 5.0;
    }
    let curiosity = clamp(curiosity, 10.0, 100.0);

    let mut patience = f(row, "patience")?;
    if has(event_type, "insult") {
        patience -= 10.0;
    } else if event_type == "sincere_apology" {
        patience += 5.0;
    }
    let patience = clamp(patience, 0.0, 100.0);

    let trust = f(row, "trust")?;
    let mut openness = 30.0 + trust * 0.5;
    if has(event_type, "insult") {
        openness -= 10.0;
    }
    let openness = clamp(openness, 10.0, 100.0);

    let mut merged = row.as_object().cloned().unwrap_or_default();
    merged.insert("energy".into(), json!(energy));
    merged.insert("curiosity".into(), json!(curiosity));
    let mood = calculate_mood(&Value::Object(merged))?;

    let mut m = Map::new();
    m.insert("energy".into(), json!(energy));
    m.insert("curiosity".into(), json!(curiosity));
    m.insert("patience".into(), json!(patience));
    m.insert("openness".into(), json!(openness));
    m.insert("mood".into(), json!(mood));
    Ok(m)
}

/// Текущие чувство / намерение / тон.
fn compute_current(row: &Value, event_type: &str) -> R<(&'static str, &'static str, &'static str)> {
    let (trust, forgiveness, curiosity, energy) = (f(row, "trust")?, f(row, "forgiveness")?, f(row, "curiosity")?, f(row, "energy")?);
    let mood = sv(row, "mood");
    let mood = mood.as_deref();

    let feeling: &'static str = if has(event_type, "insult") {
        "annoyed"
    } else if event_type == "sincere_apology" {
        if trust < 30.0 { "guarded" } else { "warm" }
    } else if event_type == "thanks" {
        "warm"
    } else if event_type == "constructive_criticism" {
        if trust > 40.0 { "interested" } else { "neutral" }
    } else {
        match mood {
            Some("warm") => "warm",
            Some("curious") => "interested",
            Some("tired") => "tired",
            Some("hurt") => "guarded",
            _ => "neutral",
        }
    };
    let intent = if mood == Some("hurt") && forgiveness < 30.0 {
        "set_boundary"
    } else if mood == Some("tired") && energy < 30.0 {
        "withdraw"
    } else if curiosity > 60.0 {
        "explain"
    } else if trust > 50.0 {
        "help"
    } else {
        "listen"
    };
    let tone = if mood == Some("warm") || feeling == "warm" {
        "warm"
    } else if matches!(mood, Some("hurt") | Some("guarded")) {
        "cold"
    } else if mood == Some("curious") {
        "thoughtful"
    } else if has(event_type, "insult") && trust < 30.0 {
        "firm"
    } else {
        "neutral"
    };
    Ok((feeling, intent, tone))
}

/// Шаблон поведения человека по последним десяти событиям.
fn update_pattern(history: &[Value]) -> &'static str {
    if history.len() < 3 {
        return "unknown";
    }
    let recent = &history[history.len().saturating_sub(10)..];
    let et = |e: &Value| e["event_type"].as_str().unwrap_or("").to_string();
    let insults = recent.iter().filter(|e| has(&et(e), "insult")).count();
    let apologies = recent.iter().filter(|e| et(e) == "sincere_apology").count();
    let thanks = recent.iter().filter(|e| et(e) == "thanks").count();
    if insults > 2 && apologies > 1 {
        return "insult_then_apology";
    }
    if thanks > 3 {
        return "grateful";
    }
    if insults > 0 && apologies == 0 {
        return "aggressive";
    }
    if apologies > insults {
        return "recovering";
    }
    "stable"
}

/// Добавить событие и обновить состояние; возвращает сводку. `context` в оригинале не используется.
pub fn add_event(cx: &Ctx, user_id: &str, event_type: &str, description: &str, sincerity: f64) -> R<Value> {
    let mut r = row(cx, user_id)?;
    let mut history = get_history(cx, user_id)?;

    let weight = calculate_weight(&r, &history, event_type, sincerity)?;
    cx.repo("record_inner_state_event", json!({"user_id": user_id, "event_type": event_type, "description": description, "sincerity": sincerity, "weight": weight, "created_at": cx.now_value()}))?;
    history.push(json!({"event_type": event_type, "weight": weight, "sincerity": sincerity}));

    let mut updates = update_relationship(cx, &r, event_type, sincerity, weight)?;
    for (k, v) in &updates {
        r[k] = v.clone();
    }
    let self_updates = update_self(&r, event_type, weight)?;
    for (k, v) in self_updates {
        r[&k] = v.clone();
        updates.insert(k, v);
    }
    updates.insert("pattern".into(), json!(update_pattern(&history)));
    let (feeling, intent, tone) = compute_current(&r, event_type)?;

    let mut args = json!({"user_id": user_id, "updated_at": cx.now_value(), "current_feeling": feeling, "current_intent": intent, "current_tone": tone});
    for k in ["mood", "energy", "curiosity", "patience", "openness", "trust", "respect", "forgiveness", "affection", "pattern"] {
        args[k] = updates.get(k).cloned().unwrap_or(Value::Null);
    }
    cx.repo("update_inner_state", args)?;
    get_summary(cx, user_id)
}

/// Краткая сводка состояния.
pub fn get_summary(cx: &Ctx, user_id: &str) -> R<Value> {
    let r = row(cx, user_id)?;
    let history = get_history(cx, user_id)?;
    Ok(json!({
        "self": {"mood": r["mood"], "energy": round1(f(&r, "energy")?), "curiosity": round1(f(&r, "curiosity")?), "patience": round1(f(&r, "patience")?), "openness": round1(f(&r, "openness")?)},
        "relationship": {"trust": round1(f(&r, "trust")?), "respect": round1(f(&r, "respect")?), "forgiveness": round1(f(&r, "forgiveness")?), "affection": round1(f(&r, "affection")?), "events_count": history.len(), "pattern": r["pattern"]},
        "current": {"feeling": r["current_feeling"], "intent": r["current_intent"], "tone": r["current_tone"]},
        "mood": r["mood"], "pattern": r["pattern"], "events": history.len(),
    }))
}

/// Контекст для генерации ответа (сырые значения строки).
pub fn get_response_context(cx: &Ctx, user_id: &str) -> R<Value> {
    let r = row(cx, user_id)?;
    Ok(json!({
        "mood": r["mood"], "energy": r["energy"], "curiosity": r["curiosity"], "patience": r["patience"], "openness": r["openness"], "trust": r["trust"], "respect": r["respect"],
        "forgiveness": r["forgiveness"], "affection": r["affection"], "pattern": r["pattern"], "feeling": r["current_feeling"], "intent": r["current_intent"], "tone": r["current_tone"],
    }))
}

/// Внутренний монолог по состоянию.
pub fn get_inner_monologue(cx: &Ctx, user_id: &str) -> R<String> {
    let ctx = get_response_context(cx, user_id)?;
    let mut parts: Vec<&str> = vec![];
    let feeling = ctx["feeling"].as_str();
    let pattern = ctx["pattern"].as_str();
    let trust = f(&ctx, "trust")?;
    let respect = f(&ctx, "respect")?;
    match feeling {
        Some("annoyed") => parts.push("Мне неприятно. Это сообщение задевает меня."),
        Some("warm") => parts.push("Мне тепло от этого разговора."),
        Some("guarded") => parts.push("Я насторожена. Нужно быть осторожной."),
        Some("interested") => parts.push("Мне интересно. Хочу узнать больше."),
        Some("tired") => parts.push("Я устала. Энергия на исходе."),
        _ => parts.push("Я спокойна. Разговор идёт нормально."),
    }
    if trust < 30.0 {
        parts.push("Я ему не доверяю. Он уже обижал меня.");
    } else if trust > 70.0 {
        parts.push("Я ему доверяю. Он показал себя надёжным.");
    }
    if respect < 30.0 {
        parts.push("Он не вызывает у меня уважения своим поведением.");
    } else if respect > 70.0 {
        parts.push("Я уважаю его. Он ведёт себя достойно.");
    }
    match pattern {
        Some("insult_then_apology") => parts.push("Он повторяет одну и ту же модель: оскорбляет, потом извиняется. Это утомляет."),
        Some("aggressive") => parts.push("Он агрессивен. Я буду защищать свои границы."),
        Some("grateful") => parts.push("Он часто благодарит. Это приятно."),
        Some("recovering") => parts.push("Он пытается исправиться. Я даю ему шанс."),
        _ => {}
    }
    match ctx["intent"].as_str() {
        Some("set_boundary") => parts.push("Я должна обозначить свои границы. Это важно."),
        Some("withdraw") => parts.push("Я хочу закончить разговор. Мне нужно отдохнуть."),
        Some("explain") => parts.push("Я хочу объяснить свою позицию."),
        Some("help") => parts.push("Я хочу помочь. Это правильно."),
        _ => {}
    }
    Ok(parts.join("\n"))
}
