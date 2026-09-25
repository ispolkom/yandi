//! Обещания человека и что с ними стало — перенос `agent/relationship_commitments.py`. «Пользователь сказал, что сделал» ≠ «YANDI знает, что сделано» (ДОВЕРИЕ ≠ ИСТИНА).
//! Журнал только добавляется; статус СВОРАЧИВАЕТСЯ из событий (open → reported_fulfilled → verified_fulfilled / verified_broken). Доверие двигает только ПРОВЕРЕННЫЙ исход.
use std::collections::HashSet;

use serde_json::{json, Value};
use yandi_rs::relationship_memory::{stem, stems};

use crate::ctx::{dt_secs, Ctx};
use crate::{causal_events, relationship_state, R};

pub const KIND_IN_CHAT: &str = "in_chat";
pub const KIND_EXTERNAL: &str = "external";
pub const KIND_GENERAL: &str = "general";
pub const VERIFIER_IN_CHAT: &str = "in_chat_direct";
pub const MAX_VERIFIABLE: usize = 5;
pub const CLAIMED: &str = "fulfillment_claimed";
pub const VERIFIED_KEPT: &str = "verified_fulfilled";
pub const VERIFIED_BROKEN: &str = "verified_broken";
pub const USER_REPORT: &str = "user_report";
pub const MAX_FOCUS_CANDIDATES: usize = 3;

/// Слова-«наполнители» выполнения («сделал», «готово», «как договаривались»…): не несут содержания обещания.
const FULFILMENT_FILLER_WORDS: &str = "сделал сделала выполнил выполнила готово готов готова наконец уже всё все обещал обещала обещание  как договаривались договорились исполнил исполнила закончил закончила завершил завершила";

fn filler() -> HashSet<String> {
    FULFILMENT_FILLER_WORDS.split_whitespace().map(stem).collect()
}

fn content(text: &str) -> HashSet<String> {
    let f = filler();
    stems(text, true).into_iter().filter(|s| !f.contains(s)).collect()
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("")
}

fn kinds_of(events: &[&Value]) -> HashSet<String> {
    events.iter().map(|e| s(e, "event_type").to_string()).collect()
}

fn fold(c: &Value, events: &[&Value], now: f64) -> Value {
    let kinds = kinds_of(events);
    let status = if kinds.contains(VERIFIED_KEPT) {
        "verified_fulfilled"
    } else if kinds.contains(VERIFIED_BROKEN) {
        "verified_broken"
    } else if kinds.contains(CLAIMED) {
        "reported_fulfilled"
    } else {
        "open"
    };
    let due = c.get("due_at").and_then(dt_secs);
    let overdue = due.map(|d| now > d).unwrap_or(false) && (status == "open" || status == "reported_fulfilled");
    let mut o = c.as_object().cloned().unwrap_or_default();
    o.insert("status".into(), json!(status));
    o.insert("overdue".into(), json!(overdue));
    Value::Object(o)
}

/// Все обещания человека со статусом, свёрнутым из событий.
pub fn commitment_statuses(cx: &Ctx, user_id: &str) -> R<Vec<Value>> {
    let events = cx.repo("list_commitment_events", json!({"user_id": user_id}))?;
    let events: Vec<Value> = events.as_array().cloned().unwrap_or_default();
    let commitments = cx.repo("list_commitments", json!({"user_id": user_id}))?;
    let now = cx.now_secs();
    Ok(commitments
        .as_array()
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|c| {
            let evs: Vec<&Value> = events.iter().filter(|e| e["commitment_id"] == c["commitment_id"]).collect();
            fold(c, &evs, now)
        })
        .collect())
}

/// Записать обещание из ТЕКУЩЕЙ реплики. Тождество ПРИЧИННОЕ: с `source_turn_id` повтор доставки ничего не создаёт, те же слова в другой реплике — второе обещание.
pub fn create_commitment(cx: &Ctx, user_id: &str, text: &str, evidence: &str, due_at: Option<&Value>, kind: &str, source_turn_id: Option<&str>, span: Option<(i64, i64)>) -> R<Value> {
    if !causal_events::may_apply(causal_events::claim(cx, user_id, source_turn_id, "promise", span)?) {
        return Ok(json!({"commitment_id": null, "created": false, "duplicate": true}));
    }
    let id = format!("c_{}_{}", cx.now_secs() as i64, &cx.uuid_hex()[..8]);
    cx.repo("record_commitment", json!({"commitment_id": id, "user_id": user_id, "kind": kind, "text": text, "evidence": evidence, "due_at": due_at.cloned().unwrap_or(Value::Null), "created_at": cx.now_value(), "source_turn_id": source_turn_id}))?;
    Ok(json!({"commitment_id": id, "created": true}))
}

/// Какое обещание (если есть) относится к ТЕКУЩЕМУ сообщению — до ответа; только чтение, «это заявление о выполнении» не решает.
pub fn resolve_commitment_focus(cx: &Ctx, user_id: &str, current_text: &str) -> R<Value> {
    let items = commitment_statuses(cx, user_id)?;
    let open: Vec<&Value> = items.iter().filter(|c| s(c, "status") == "open").collect();
    let live: Vec<&Value> = items.iter().filter(|c| matches!(s(c, "status"), "open" | "reported_fulfilled")).collect();
    if live.is_empty() {
        return Ok(json!({"commitment": null, "basis": "no_open_commitment", "open_count": 0, "candidates": []}));
    }
    let current = content(current_text);
    if !current.is_empty() {
        let scored: Vec<(f64, &Value)> = live.iter().map(|c| (current.intersection(&content(s(c, "text"))).count() as f64 / current.len() as f64, *c)).collect();
        let best = scored.iter().map(|(sc, _)| *sc).fold(f64::NEG_INFINITY, f64::max);
        if best > 0.0 {
            let top: Vec<&Value> = scored.iter().filter(|(sc, _)| *sc == best).map(|(_, c)| *c).collect();
            if top.len() == 1 {
                return Ok(json!({"commitment": top[0], "basis": "explicit_reference", "open_count": open.len(), "candidates": []}));
            }
            return Ok(json!({"commitment": null, "basis": "ambiguous", "open_count": open.len(), "candidates": top.into_iter().take(MAX_FOCUS_CANDIDATES).collect::<Vec<_>>()}));
        }
    }
    if open.len() == 1 {
        return Ok(json!({"commitment": open[0], "basis": "sole_open_commitment", "open_count": 1, "candidates": []}));
    }
    if open.is_empty() {
        return Ok(json!({"commitment": null, "basis": "no_open_commitment", "open_count": 0, "candidates": []}));
    }
    let tail: Vec<&Value> = open[open.len().saturating_sub(MAX_FOCUS_CANDIDATES)..].to_vec();
    Ok(json!({"commitment": null, "basis": "ambiguous", "open_count": open.len(), "candidates": tail}))
}

/// Обещания, выполнение которых YANDI может увидеть напрямую в чате: `in_chat`, не проверенные и НЕ данные в текущей реплике. Старые сначала, не более пяти (самые новые).
pub fn verifiable_commitments(cx: &Ctx, user_id: &str, current_turn_id: Option<&str>) -> R<Vec<Value>> {
    let rows = commitment_statuses(cx, user_id)?;
    let live: Vec<Value> = rows
        .into_iter()
        .filter(|c| s(c, "kind") == KIND_IN_CHAT && matches!(s(c, "status"), "open" | "reported_fulfilled") && !(current_turn_id.map(|t| !t.is_empty()).unwrap_or(false) && c.get("source_turn_id").and_then(|v| v.as_str()) == current_turn_id))
        .collect();
    let skip = live.len().saturating_sub(MAX_VERIFIABLE);
    Ok(live.into_iter().skip(skip).collect())
}

fn owned(cx: &Ctx, user_id: &str, commitment_id: Option<&str>) -> R<Option<Value>> {
    let Some(id) = commitment_id.filter(|i| !i.is_empty()) else {
        return Ok(None);
    };
    let row = cx.repo("get_commitment", json!({"commitment_id": id}))?;
    Ok(if row.is_object() && s(&row, "user_id") == user_id { Some(row) } else { None })
}

fn resolved_kinds(cx: &Ctx, user_id: &str, commitment_id: &str) -> R<HashSet<String>> {
    let ev = cx.repo("list_commitment_events", json!({"user_id": user_id}))?;
    Ok(ev.as_array().cloned().unwrap_or_default().iter().filter(|e| s(e, "commitment_id") == commitment_id).map(|e| s(e, "event_type").to_string()).collect())
}

/// Человек СООБЩАЕТ, что выполнил обещание: пишется один раз, ни одну координату не двигает (слова, не проверка).
pub fn record_fulfillment_claim(cx: &Ctx, user_id: &str, commitment_id: Option<&str>, evidence: &str, source_turn_id: Option<&str>, span: Option<(i64, i64)>) -> R<Value> {
    let mut result = json!({"target": null, "recorded": false, "state_changed": false});
    if owned(cx, user_id, commitment_id)?.is_none() {
        return Ok(result);
    }
    let cid = commitment_id.unwrap_or_default();
    result["target"] = json!(cid);
    if !causal_events::may_apply(causal_events::claim(cx, user_id, source_turn_id, "fulfilment_claim", span)?) {
        return Ok(result);
    }
    let rec = cx.repo("record_commitment_event", json!({"commitment_id": cid, "user_id": user_id, "event_type": CLAIMED, "source": USER_REPORT, "evidence": evidence, "created_at": cx.now_value(), "source_turn_id": source_turn_id, "span_start": span.map(|s| s.0), "span_end": span.map(|s| s.1)}))?;
    result["recorded"] = rec;
    Ok(result)
}

/// ПРОВЕРЯЮЩИЙ независимо от слов человека установил исход. Дописывает исход и, только если строка новая, применяет ОДИН переход состояния.
pub fn record_verification(cx: &Ctx, user_id: &str, commitment_id: Option<&str>, kept: bool, source: &str, evidence: Option<&str>) -> R<Value> {
    if source.is_empty() || source == USER_REPORT {
        return Err("ValueError: a verified outcome needs a verifier; the person's own report is not verification".into());
    }
    let mut result = json!({"target": null, "recorded": false, "state_changed": false});
    if owned(cx, user_id, commitment_id)?.is_none() {
        return Ok(result);
    }
    let cid = commitment_id.unwrap_or_default();
    result["target"] = json!(cid);
    let kinds = resolved_kinds(cx, user_id, cid)?;
    if kinds.contains(VERIFIED_KEPT) || kinds.contains(VERIFIED_BROKEN) {
        return Ok(result);
    }
    let event_type = if kept { VERIFIED_KEPT } else { VERIFIED_BROKEN };
    let rec = cx.repo("record_commitment_event", json!({"commitment_id": cid, "user_id": user_id, "event_type": event_type, "source": source, "evidence": evidence, "created_at": cx.now_value()}))?;
    result["recorded"] = rec.clone();
    if rec.as_bool().unwrap_or(false) {
        relationship_state::record_verified_commitment(cx, user_id, kept)?;
        result["state_changed"] = json!(true);
    }
    Ok(result)
}

/// Срез строки по символам, как `text[a:b]` Python (отрицательные границы отсчитываются с конца).
fn py_slice(text: &str, a: i64, b: i64) -> String {
    let chars: Vec<char> = text.chars().collect();
    let n = chars.len() as i64;
    let norm = |i: i64| -> i64 { if i < 0 { (n + i).max(0) } else { i.min(n) } };
    let (a, b) = (norm(a), norm(b));
    if a >= b {
        return String::new();
    }
    chars[a as usize..b as usize].iter().collect()
}

/// YANDI НАБЛЮДАЛА обещанное в текущей опознанной реплике: `evidence` — точный отрезок сообщения. Дописывает проверенное событие С происхождением и, только если строка новая,
/// применяет ОДИН ограниченный переход доверия. Отказ (ничего не пишется): нет реплики/отрезка/доказательства; не то обещание; данное в этой же реплике; уже решённое; в неизменяемой записи
/// реплики по отрезку не эти же символы; эта реплика уже что-то проверила. ВСЁ-ИЛИ-НИЧЕГО с транзакцией вызывающего: ошибка не глотается.
pub fn record_direct_fulfilment(cx: &Ctx, user_id: &str, commitment_id: Option<&str>, evidence: &str, source_turn_id: Option<&str>, span: Option<(i64, i64)>) -> R<Value> {
    let mut result = json!({"target": null, "recorded": false, "state_changed": false, "reward": 0.0});
    let (Some(turn), Some(span)) = (source_turn_id.filter(|t| !t.is_empty()), span) else {
        return Ok(result);
    };
    if evidence.is_empty() {
        return Ok(result);
    }
    cx.repo("get_or_create_inner_state", json!({"user_id": user_id, "updated_at": cx.now_value()}))?;
    let Some(commitment) = owned(cx, user_id, commitment_id)? else {
        return Ok(result);
    };
    if s(&commitment, "kind") != KIND_IN_CHAT || commitment.get("source_turn_id").and_then(|v| v.as_str()) == Some(turn) {
        return Ok(result);
    }
    let cid = commitment_id.unwrap_or_default();
    let kinds = resolved_kinds(cx, user_id, cid)?;
    if kinds.contains(VERIFIED_KEPT) || kinds.contains(VERIFIED_BROKEN) {
        return Ok(result);
    }
    let turn_text = cx.repo("get_interaction_turn_text", json!({"user_id": user_id, "source_turn_id": turn}))?;
    let Some(text) = turn_text.as_str() else {
        return Ok(result);
    };
    if py_slice(text, span.0, span.1) != evidence {
        return Ok(result);
    }
    if !causal_events::may_apply(causal_events::claim(cx, user_id, Some(turn), "commitment_verified", Some(span))?) {
        return Ok(result);
    }
    result["target"] = json!(cid);
    let prior = cx.repo("count_commitment_events", json!({"user_id": user_id, "event_type": VERIFIED_KEPT, "source": VERIFIER_IN_CHAT}))?.as_i64().unwrap_or(0);
    let rec = cx.repo("record_commitment_event", json!({"commitment_id": cid, "user_id": user_id, "event_type": VERIFIED_KEPT, "source": VERIFIER_IN_CHAT, "evidence": evidence, "created_at": cx.now_value(), "source_turn_id": turn, "span_start": span.0, "span_end": span.1}))?;
    result["recorded"] = rec.clone();
    if rec.as_bool().unwrap_or(false) {
        result["reward"] = json!(relationship_state::record_observed_commitment(cx, user_id, prior)?);
        result["state_changed"] = json!(true);
    }
    Ok(result)
}
