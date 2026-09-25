//! Долговременная личная память — перенос `agent/personal_memory.py`. ИСТОЧНИК: одна неизменяемая запись на реплику человека (`interaction_turn`). ТОЛКОВАНИЕ: какие прошлые
//! реплики важны для текущего сообщения — считается при чтении, ничего производного не хранится. Вспоминается ограниченный список (MAX_RECALLED), каждая сторона обрезана.
use std::collections::HashSet;

use serde_json::{json, Value};
use yandi_rs::py_text::{is_py_space, py_split_whitespace};

use crate::ctx::{dt_secs, Ctx};
use crate::relationship_memory::content_stems;
use crate::R;

pub const MAX_RECALLED: usize = 4;
pub const RECENT_SLOTS: usize = 2;
pub const RECENT_MAX_AGE_DAYS: f64 = 30.0;
pub const WINDOW: i64 = 300;
pub const MIN_RELEVANCE: f64 = 0.18;
pub const EVENT_BONUS: f64 = 0.35;
pub const MAX_SIDE_CHARS: usize = 240;
pub const CLIENT: &str = "client";

/// Добавить исходную запись реплики. Реплика без личности не записывается вовсе (ошибка), личность никогда не выдумывается.
pub fn record_turn(cx: &Ctx, user_id: &str, source_turn_id: Option<&str>, user_text: &str, assistant_text: Option<&str>, model: Option<&str>, adapter: Option<&str>, recalled_turn_ids: Option<&[String]>) -> R<Value> {
    let Some(stid) = source_turn_id.filter(|s| !s.is_empty()) else {
        return Err("a personal-memory turn needs the client-minted source turn id".into());
    };
    let mut args = json!({
        "user_id": user_id, "source_turn_id": stid, "turn_id_origin": CLIENT, "user_text": user_text,
        "assistant_text": assistant_text, "model": model, "adapter": adapter, "created_at": cx.now_value(),
    });
    if let Some(ids) = recalled_turn_ids {
        args["recalled_turn_ids"] = json!(ids);
    }
    let recorded = cx.repo("record_interaction_turn", args)?;
    Ok(json!({"recorded": recorded, "source_turn_id": stid}))
}

fn collapse(text: &str) -> String {
    py_split_whitespace(text).collect::<Vec<_>>().join(" ")
}

fn clip(text: Option<&str>) -> String {
    let t = collapse(text.unwrap_or(""));
    if t.chars().count() <= MAX_SIDE_CHARS {
        return t;
    }
    let cut: String = t.chars().take(MAX_SIDE_CHARS - 1).collect();
    format!("{}…", cut.trim_end_matches(is_py_space))
}

fn relevance(current: &HashSet<String>, past_user_text: &str) -> f64 {
    let past = content_stems(past_user_text);
    if current.is_empty() || past.is_empty() {
        return 0.0;
    }
    let shared = current.intersection(&past).count();
    if shared == 0 {
        0.0
    } else {
        shared as f64 / ((current.len() * past.len()) as f64).sqrt()
    }
}

struct Cand {
    row: Value,
    events: Vec<String>,
    created: Option<f64>,
    age_days: f64,
    relevance: f64,
    id: i64,
}

impl Cand {
    fn score(&self) -> f64 {
        self.relevance + if self.events.is_empty() { 0.0 } else { EVENT_BONUS } + 0.2 * (-self.age_days / 30.0).exp()
    }
}

/// Ограниченный, хронологически упорядоченный список воспоминаний, важных для этого сообщения. Пусто — «нечего вспоминать»; ошибка — НЕИЗВЕСТНО (решает вызывающий).
pub fn recall(cx: &Ctx, user_id: &str, current_text: &str, current_turn_id: Option<&str>, in_context_texts: &[String]) -> R<Vec<Value>> {
    let in_context: HashSet<String> = in_context_texts.iter().map(|t| collapse(t)).collect();
    let now = cx.now_secs();
    let current = content_stems(current_text);
    let rows = cx.repo("list_recent_interaction_turns", json!({"user_id": user_id, "limit": WINDOW}))?;

    let mut candidates: Vec<Cand> = vec![];
    for row in rows.as_array().map(|a| a.as_slice()).unwrap_or(&[]) {
        let stid = row["source_turn_id"].as_str().unwrap_or("");
        if current_turn_id.map(|t| !t.is_empty() && t == stid).unwrap_or(false) {
            continue;
        }
        let user_text = row["user_text"].as_str().unwrap_or("");
        if in_context.contains(&collapse(user_text)) {
            continue;
        }
        let events: Vec<String> = row["event_types"].as_str().unwrap_or("").split(',').filter(|e| !e.is_empty()).map(String::from).collect();
        let created = row.get("created_at").and_then(dt_secs);
        let age_days = created.map(|c| ((now - c) / 86400.0).max(0.0)).unwrap_or(1e9);
        candidates.push(Cand { relevance: relevance(&current, user_text), events, created, age_days, id: row["interaction_id"].as_i64().unwrap_or(0), row: row.clone() });
    }

    // выбранные: interaction_id → основание (порядок вставки не важен — в конце сортировка)
    let mut chosen: Vec<(i64, &'static str)> = vec![];
    let has = |chosen: &Vec<(i64, &'static str)>, id: i64| chosen.iter().any(|(i, _)| *i == id);
    for c in candidates.iter().filter(|c| c.age_days <= RECENT_MAX_AGE_DAYS).take(RECENT_SLOTS) {
        if !has(&chosen, c.id) {
            chosen.push((c.id, "recent"));
        }
    }
    let mut eligible: Vec<&Cand> = candidates.iter().filter(|c| c.relevance >= MIN_RELEVANCE && !has(&chosen, c.id)).collect();
    eligible.sort_by(|a, b| b.score().partial_cmp(&a.score()).unwrap_or(std::cmp::Ordering::Equal));
    for c in eligible {
        if chosen.len() >= MAX_RECALLED {
            break;
        }
        // ключ словаря: повтор того же interaction_id перезаписал бы основание — как в Python
        match chosen.iter_mut().find(|(i, _)| *i == c.id) {
            Some(slot) => slot.1 = "relevant",
            None => chosen.push((c.id, "relevant")),
        }
    }
    if chosen.len() < MAX_RECALLED {
        let mut evented: Vec<&Cand> = candidates.iter().filter(|c| !c.events.is_empty() && !has(&chosen, c.id) && c.age_days <= RECENT_MAX_AGE_DAYS).collect();
        evented.sort_by(|a, b| b.score().partial_cmp(&a.score()).unwrap_or(std::cmp::Ordering::Equal));
        for c in evented {
            if chosen.len() >= MAX_RECALLED {
                break;
            }
            match chosen.iter_mut().find(|(i, _)| *i == c.id) {
                Some(slot) => slot.1 = "event",
                None => chosen.push((c.id, "event")),
            }
        }
    }

    let mut picked: Vec<(&Cand, &'static str)> = chosen.iter().filter_map(|(id, basis)| candidates.iter().find(|c| c.id == *id).map(|c| (c, *basis))).collect();
    picked.sort_by(|(a, _), (b, _)| {
        a.created.unwrap_or(f64::NEG_INFINITY).partial_cmp(&b.created.unwrap_or(f64::NEG_INFINITY)).unwrap_or(std::cmp::Ordering::Equal).then(a.id.cmp(&b.id))
    });
    Ok(picked
        .into_iter()
        .map(|(c, basis)| {
            let created_s = c.row["created_at"].as_str().unwrap_or("");
            let assistant = c.row["assistant_text"].as_str().filter(|s| !s.is_empty());
            json!({
                "interaction_id": c.id, "source_turn_id": c.row["source_turn_id"],
                "when": created_s.get(..10).unwrap_or(""),
                "user_text": clip(c.row["user_text"].as_str()), "assistant_text": assistant.map(|s| clip(Some(s))),
                "events": c.events, "basis": basis,
            })
        })
        .collect())
}
