//! Каноническое состояние отношений к одному человеку — перенос `agent/relationship_state.py`. СОБЫТИЯ ПИШУТ СОСТОЯНИЕ; СОСТОЯНИЕ НЕ ВЫДУМЫВАЕТ СОБЫТИЯ; МОДЕЛЬ НЕ ПИШЕТ СОСТОЯНИЕ.
//! Координаты 0..100: доверие, уважение, привязанность (+ ёмкость прощения — таблица `relationship_memory`). Извинение НЕ возвращает доверие; доверие возвращает ПОВЕДЕНИЕ.
use serde_json::{json, Map, Value};

use crate::ctx::Ctx;
use crate::R;

pub const COORDS: [&str; 3] = ["trust", "respect", "affection"];
pub const DEFAULTS: [(&str, f64); 3] = [("trust", 50.0), ("respect", 50.0), ("affection", 30.0)];

const INSULT_RESPECT_PER_SEVERITY: f64 = 20.0;
const INSULT_TRUST_PER_SEVERITY: f64 = 8.0;
const INSULT_AFFECTION_PER_SEVERITY: f64 = 3.0;
const APOLOGY_RESPECT_RECOVERY_SHARE: f64 = 0.3;
const OBSERVED_TRUST_BASE: f64 = 2.0;
const OBSERVED_TRUST_DECAY: f64 = 0.6;
const OBSERVED_TRUST_MIN_REWARD: f64 = 0.05;
const OBSERVED_TRUST_CEILING: f64 = 60.0;

type Deltas = Vec<(&'static str, f64)>;

fn clamp(v: f64) -> f64 {
    v.max(0.0).min(100.0)
}

fn default_of(c: &str) -> f64 {
    DEFAULTS.iter().find(|(k, _)| *k == c).map(|(_, v)| *v).unwrap_or(0.0)
}

fn current(row: Option<&Value>) -> [f64; 3] {
    let mut out = [0.0; 3];
    for (i, c) in COORDS.iter().enumerate() {
        out[i] = row.and_then(|r| r.get(*c)).and_then(|v| v.as_f64()).unwrap_or_else(|| default_of(c));
    }
    out
}

/// Только чтение: четыре координаты; человек без истории стоит на значениях по умолчанию.
pub fn get_state(cx: &Ctx, user_id: &str) -> R<Value> {
    let row = cx.repo("get_inner_state", json!({"user_id": user_id}))?;
    let cur = current(Some(&row));
    let cap = cx.repo("get_forgiveness_capacity", json!({"user_id": user_id}))?["capacity"].as_f64().unwrap_or(50.0);
    Ok(json!({"trust": cur[0], "respect": cur[1], "affection": cur[2], "forgiveness_capacity": cap}))
}

fn insult_deltas(severity: f64) -> Deltas {
    let s = severity.max(0.0).min(1.0);
    vec![("respect", -INSULT_RESPECT_PER_SEVERITY * s), ("trust", -INSULT_TRUST_PER_SEVERITY * s), ("affection", -INSULT_AFFECTION_PER_SEVERITY * s)]
}

fn apology_deltas(offense_severity: f64, sincerity: f64) -> Deltas {
    let (o, s) = (offense_severity.max(0.0).min(1.0), sincerity.max(0.0).min(1.0));
    vec![("respect", APOLOGY_RESPECT_RECOVERY_SHARE * INSULT_RESPECT_PER_SEVERITY * o * s)]
}

fn commitment_deltas(kept: bool) -> Deltas {
    if kept {
        vec![("trust", 8.0), ("respect", 3.0), ("affection", 0.0)]
    } else {
        vec![("trust", -15.0), ("respect", -5.0), ("affection", -1.0)]
    }
}

/// Награда доверия за k-е непосредственно наблюдённое выполнение: ограничена, геометрически убывает, ноль когда ничтожна. `round(x, 4)` Python.
pub fn observed_trust_reward(prior_observed: i64) -> f64 {
    let reward = OBSERVED_TRUST_BASE * OBSERVED_TRUST_DECAY.powi(prior_observed.max(0) as i32);
    if reward >= OBSERVED_TRUST_MIN_REWARD {
        format!("{reward:.4}").parse().unwrap_or(reward)
    } else {
        0.0
    }
}

fn observed_deltas(reward: f64, trust_before: f64) -> Deltas {
    vec![("trust", 0.0f64.max(reward.min(OBSERVED_TRUST_CEILING - trust_before)))]
}

fn delta_of(d: &Deltas, c: &str) -> f64 {
    d.iter().find(|(k, _)| *k == c).map(|(_, v)| *v).unwrap_or(0.0)
}

/// `f"{x:+.1f}"` Python.
fn signed1(x: f64) -> String {
    let s = format!("{:.1}", x.abs());
    // Python: знак «+» для нуля и положительных, «-» для отрицательных, включая -0.0 → "-0.0"
    if x.is_sign_negative() {
        format!("-{s}")
    } else {
        format!("+{s}")
    }
}

/// Сдвинуть координаты на `deltas` и дописать аудит-событие. Сбой не ломает жизненный цикл обиды, но не глотается молча (в журнал); `strict` — для перехода, являющегося
/// ДОКАЗАТЕЛЬСТВОМ другой записи (проверенное выполнение): его сбой должен дойти до транзакции вызывающего.
fn apply(cx: &Ctx, user_id: &str, event_type: &str, deltas: &Deltas, sincerity: f64, weight: f64, strict: bool) -> R<()> {
    let r = (|| -> R<()> {
        let row = cx.repo("get_or_create_inner_state", json!({"user_id": user_id, "updated_at": cx.now_value()}))?;
        let before = current(Some(&row));
        let mut after = [0.0; 3];
        for i in 0..3 {
            after[i] = clamp(before[i] + delta_of(deltas, COORDS[i]));
        }
        let mut upd = Map::new();
        upd.insert("user_id".into(), json!(user_id));
        upd.insert("updated_at".into(), cx.now_value());
        for i in 0..3 {
            upd.insert(COORDS[i].into(), json!(after[i]));
        }
        cx.repo("update_inner_state", Value::Object(upd))?;
        let note: String = (0..3).filter(|i| after[*i] != before[*i]).map(|i| format!("{}{}", COORDS[i], signed1(after[i] - before[i]))).collect::<Vec<_>>().join(" ");
        let note: String = note.chars().take(255).collect();
        cx.repo("record_inner_state_event", json!({"user_id": user_id, "event_type": event_type, "description": note, "sincerity": sincerity, "weight": weight, "created_at": cx.now_value()}))?;
        Ok(())
    })();
    match r {
        Err(e) if strict => Err(e),
        Err(e) => {
            eprintln!("relationship_state: could not apply {event_type} for {user_id}: {e}");
            Ok(())
        }
        Ok(()) => Ok(()),
    }
}

pub fn record_insult(cx: &Ctx, user_id: &str, severity: f64) -> R<()> {
    let s = severity.max(0.0).min(1.0);
    apply(cx, user_id, "insult", &insult_deltas(s), 0.0, -s, false)
}

pub fn record_accepted_apology(cx: &Ctx, user_id: &str, offense_severity: f64, sincerity: f64) -> R<()> {
    let (o, s) = (offense_severity.max(0.0).min(1.0), sincerity.max(0.0).min(1.0));
    apply(cx, user_id, "apology_accepted", &apology_deltas(o, s), s, o, false)
}

pub fn record_verified_commitment(cx: &Ctx, user_id: &str, kept: bool) -> R<()> {
    apply(cx, user_id, if kept { "commitment_kept" } else { "commitment_broken" }, &commitment_deltas(kept), 1.0, if kept { 1.0 } else { -1.0 }, false)
}

/// Выполнение обещания, наблюдённое напрямую в чате: возвращает награду (до потолка). Вес аудит-строки = награда.
pub fn record_observed_commitment(cx: &Ctx, user_id: &str, prior_observed: i64) -> R<f64> {
    let reward = observed_trust_reward(prior_observed);
    let row = cx.repo("get_or_create_inner_state", json!({"user_id": user_id, "updated_at": cx.now_value()}))?;
    apply(cx, user_id, "commitment_observed", &observed_deltas(reward, current(Some(&row))[0]), 1.0, reward, true)?;
    Ok(reward)
}

/// Восстановить координаты из аудит-следа (старые сначала) теми же чистыми правилами.
pub fn replay_from_events(events: &[Value]) -> [f64; 3] {
    let mut state = [default_of("trust"), default_of("respect"), default_of("affection")];
    for e in events {
        let kind = e["event_type"].as_str().unwrap_or("");
        let (weight, sincerity) = (e["weight"].as_f64().unwrap_or(0.0), e["sincerity"].as_f64().unwrap_or(0.0));
        let deltas = match kind {
            "insult" => insult_deltas(-weight),
            "apology_accepted" => apology_deltas(weight, sincerity),
            "commitment_kept" | "commitment_broken" => commitment_deltas(kind == "commitment_kept"),
            "commitment_observed" => observed_deltas(weight, state[0]),
            _ => continue,
        };
        for i in 0..3 {
            state[i] = clamp(state[i] + delta_of(&deltas, COORDS[i]));
        }
    }
    state
}

pub fn replay(cx: &Ctx, user_id: &str) -> R<Value> {
    let ev = cx.repo("list_inner_state_events_in_order", json!({"user_id": user_id}))?;
    let s = replay_from_events(ev.as_array().map(|a| a.as_slice()).unwrap_or(&[]));
    Ok(json!({"trust": s[0], "respect": s[1], "affection": s[2]}))
}
