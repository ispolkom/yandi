//! Причинная идентичность событий отношений — перенос `agent/causal_events.py`. ОДИН причинный ЭЛЕМЕНТ → ОДНА запись → ОДИН переход состояния; повтор доставки не создаёт истории.
use serde_json::{json, Value};

use crate::ctx::Ctx;
use crate::R;

pub const NEW: &str = "new";
pub const DUPLICATE: &str = "duplicate";
pub const UNSTABLE: &str = "unstable";
pub const UNAVAILABLE: &str = "unavailable";

/// Решить, можно ли применять событие сейчас: NEW / DUPLICATE / UNSTABLE / UNAVAILABLE; запрещает только DUPLICATE.
pub fn claim(cx: &Ctx, user_id: &str, source_turn_id: Option<&str>, event_type: &str, span: Option<(i64, i64)>) -> R<&'static str> {
    let Some(turn) = source_turn_id.filter(|t| !t.is_empty()) else {
        return Ok(UNSTABLE);
    };
    let (start, end) = span.map(|(a, b)| (json!(a), json!(b))).unwrap_or((Value::Null, Value::Null));
    match cx.repo("claim_causal_event", json!({"user_id": user_id, "source_turn_id": turn, "event_type": event_type, "span_start": start, "span_end": end, "created_at": cx.now_value()})) {
        Ok(v) => Ok(if v.as_bool().unwrap_or(false) { NEW } else { DUPLICATE }),
        Err(e) if e.to_lowercase().contains("no such table") => Ok(UNAVAILABLE),
        Err(e) => Err(e),
    }
}

pub fn may_apply(status: &str) -> bool {
    status != DUPLICATE
}
