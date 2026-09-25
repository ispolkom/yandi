//! Эпизодическая память — перенос `agent/memory_episodic.py`: события жизни системы (запросы, решения, ошибки, рефлексии, уроки), только добавляются и никогда не стираются.
//! ЖЁСТКИЙ ОТКАЗ, как в оригинале: любая ошибка базы уходит вызывающему, запасного пути нет.
//! Время эпизода берётся как UTC (в Python `datetime.timestamp()` от «наивного» UTC-времени сдвигался на местный пояс машины — это ошибка оригинала, здесь исправлена).
use serde_json::{json, Value};

use crate::ctx::{dt_secs, Ctx};
use crate::R;

/// Один эпизод (`Episode`).
#[derive(Debug, Clone, PartialEq)]
pub struct Episode {
    pub id: String,
    pub timestamp: f64,
    pub event_type: String,
    pub summary: String,
    pub details: Value,
    pub importance: f64,
    pub tags: Vec<String>,
    pub related_episodes: Vec<String>,
}

impl Episode {
    pub fn to_json(&self) -> Value {
        json!({
            "id": self.id, "timestamp": self.timestamp, "event_type": self.event_type, "summary": self.summary, "details": self.details,
            "importance": self.importance, "tags": self.tags, "related_episodes": self.related_episodes,
        })
    }
}

fn s_float(x: f64) -> String {
    if x.fract() == 0.0 && x.abs() < 1e16 {
        format!("{x:.1}")
    } else {
        format!("{x}")
    }
}

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

fn strs(v: &Value) -> Vec<String> {
    if !truthy(v) {
        return vec![];
    }
    v.as_array().map(|a| a.iter().map(|e| e.as_str().map(String::from).unwrap_or_else(|| e.to_string())).collect()).unwrap_or_default()
}

fn row_to_episode(cx: &Ctx, row: &Value) -> Episode {
    let details = row.get("details").filter(|d| truthy(d)).cloned().unwrap_or_else(|| json!({}));
    Episode {
        id: row["episode_id"].as_str().unwrap_or("").to_string(),
        // время в БД отсутствует → «сейчас», как `_dt_to_unix(None)`
        timestamp: row.get("created_at").and_then(dt_secs).unwrap_or_else(|| cx.now_secs()),
        event_type: row["event_type"].as_str().unwrap_or("").to_string(),
        summary: row["summary"].as_str().unwrap_or("").to_string(),
        details,
        importance: row.get("importance").and_then(|v| v.as_f64()).unwrap_or(0.5),
        tags: strs(&row["tags"]),
        related_episodes: strs(&row["related_episodes"]),
    }
}

fn episodes(cx: &Ctx, rows: Value) -> Vec<Episode> {
    rows.as_array().map(|a| a.iter().map(|r| row_to_episode(cx, r)).collect()).unwrap_or_default()
}

/// Добавить эпизод; важность зажимается в 0..1; идентификатор — `ep_` + 12 знаков.
pub fn add(cx: &Ctx, event_type: &str, summary: &str, details: Value, importance: f64, tags: &[String]) -> R<String> {
    add_v(cx, event_type, summary, details, importance, json!(tags))
}

/// То же, но теги — произвольные значения JSON (в оригинале в теги может попасть любое значение).
pub fn add_v(cx: &Ctx, event_type: &str, summary: &str, details: Value, importance: f64, tags: Value) -> R<String> {
    let id = format!("ep_{}", cut(&cx.uuid_hex(), 12));
    cx.repo(
        "record_episode",
        json!({"episode_id": id, "event_type": event_type, "summary": summary, "details": details, "importance": importance.min(1.0).max(0.0), "tags": tags, "created_at": cx.now_value()}),
    )?;
    Ok(id)
}

/// `add_query` для значений произвольного вида: запрос — строка (срез `[:60]`), домен / режим / доверие попадают в детали и теги как есть, уверенность — число.
pub fn add_query_values(cx: &Ctx, query: &Value, domain: &Value, answer_mode: &Value, trust: &Value, confidence: &Value) -> R<String> {
    let q = query.as_str().ok_or_else(|| "TypeError".to_string())?;
    let conf = match confidence {
        Value::Number(n) => n.as_f64().unwrap_or(0.0),
        Value::Bool(b) => *b as i64 as f64,
        _ => return Err("TypeError".into()),
    };
    add_v(
        cx,
        "query",
        &format!("Запрос: {}", cut(q, 60)),
        json!({"query": query, "domain": domain, "answer_mode": answer_mode, "trust": trust, "confidence": confidence}),
        conf,
        json!([domain, answer_mode]),
    )
}

pub fn add_query(cx: &Ctx, query: &str, domain: &str, answer_mode: &str, trust: &str, confidence: f64) -> R<String> {
    add(
        cx,
        "query",
        &format!("Запрос: {}", cut(query, 60)),
        json!({"query": query, "domain": domain, "answer_mode": answer_mode, "trust": trust, "confidence": confidence}),
        confidence,
        &[domain.to_string(), answer_mode.to_string()],
    )
}

pub fn add_decision(cx: &Ctx, decision_type: &str, reason: &str, details: Value, importance: f64) -> R<String> {
    add(cx, "decision", &format!("Решение: {} — {}", decision_type, cut(reason, 40)), details, importance, &["decision".to_string(), decision_type.to_string()])
}

pub fn add_error(cx: &Ctx, error: &str, context: Value, severity: f64) -> R<String> {
    add(cx, "error", &format!("Ошибка: {}", cut(error, 60)), context, severity, &["error".to_string()])
}

pub fn add_reflection(cx: &Ctx, reflection: &Value) -> R<String> {
    if !reflection.is_object() {
        return Err("AttributeError".into());
    }
    let summary = match reflection.get("summary") {
        None => "Рефлексия".to_string(),
        Some(Value::String(s)) => s.clone(),
        // MySQL приводит число к строке при вставке (в Python это молча сохраняется); прочие виды и null — ошибка
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => n.to_string(),
        Some(Value::Number(n)) => s_float(n.as_f64().unwrap_or(0.0)),
        Some(Value::Bool(b)) => (*b as i64).to_string(),
        Some(_) => return Err("TypeError".into()),
    };
    add(cx, "reflection", &summary, reflection.clone(), 0.8, &["reflection".to_string()])
}

pub fn add_learning(cx: &Ctx, lesson: &str, context: &Value, importance: f64) -> R<String> {
    let mut details = context.as_object().cloned().ok_or_else(|| "TypeError".to_string())?;
    details.insert("lesson".into(), json!(lesson));
    add(cx, "learning", &format!("Урок: {}", cut(lesson, 60)), Value::Object(details), importance, &["learning".to_string()])
}

pub fn get_by_type(cx: &Ctx, event_type: &str, limit: i64) -> R<Vec<Episode>> {
    Ok(episodes(cx, cx.repo("get_episodes_by_type", json!({"event_type": event_type, "limit": limit}))?))
}

pub fn get_by_tag(cx: &Ctx, tag: &str, limit: i64) -> R<Vec<Episode>> {
    Ok(episodes(cx, cx.repo("get_episodes_by_tag", json!({"tag": tag, "limit": limit}))?))
}

pub fn get_by_importance(cx: &Ctx, min_importance: f64, limit: i64) -> R<Vec<Episode>> {
    Ok(episodes(cx, cx.repo("get_episodes_by_importance", json!({"min_importance": min_importance, "limit": limit}))?))
}

pub fn get_recent(cx: &Ctx, limit: i64) -> R<Vec<Episode>> {
    Ok(episodes(cx, cx.repo("get_recent_episodes", json!({"limit": limit}))?))
}

/// Дата и время (UTC) из секунд Unix: алгоритм «гражданской даты» по числу дней.
fn civil(secs: i64) -> (i64, u32, u32, u32, u32, u32) {
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d, (rem / 3600) as u32, ((rem % 3600) / 60) as u32, (rem % 60) as u32)
}

/// `datetime.fromtimestamp(t).isoformat()` (UTC): `ГГГГ-ММ-ДДTЧЧ:ММ:СС[.дробь]`.
fn iso_utc(t: f64) -> String {
    let whole = t.floor();
    let (y, mo, d, h, mi, se) = civil(whole as i64);
    let base = format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{se:02}");
    let micros = ((t - whole) * 1_000_000.0).round() as i64;
    if micros > 0 {
        format!("{base}.{micros:06}")
    } else {
        base
    }
}

pub fn get_timeline(cx: &Ctx, limit: i64) -> R<Vec<Value>> {
    Ok(get_recent(cx, limit)?
        .iter()
        .map(|e| json!({"id": e.id, "time": iso_utc(e.timestamp), "event_type": e.event_type, "summary": e.summary, "importance": e.importance}))
        .collect())
}

pub fn get_stats(cx: &Ctx) -> R<Value> {
    cx.repo("get_episode_stats", json!({}))
}

fn s(v: &Value) -> String {
    match v {
        Value::Number(n) if n.is_f64() => {
            let x = n.as_f64().unwrap_or(0.0);
            if x.fract() == 0.0 && x.abs() < 1e16 {
                format!("{x:.1}")
            } else {
                format!("{x}")
            }
        }
        Value::String(x) => x.clone(),
        Value::Null => "None".into(),
        other => other.to_string(),
    }
}

/// Краткое текстовое представление.
pub fn summary(cx: &Ctx) -> R<String> {
    let stats = get_stats(cx)?;
    let recent = get_recent(cx, 3)?;
    let types = stats["by_type"].as_object().map(|o| o.iter().map(|(k, v)| format!("{k}={}", s(v))).collect::<Vec<_>>().join(", ")).unwrap_or_default();
    let lines: Vec<String> = recent.iter().map(|e| format!("  - [{}] {}", e.event_type, cut(&e.summary, 60))).collect();
    Ok(format!(
        "\n=== ЭПИЗОДИЧЕСКАЯ ПАМЯТЬ ===\nВсего эпизодов: {}\nТипы: {}\nСредняя важность: {}\nПоследние эпизоды:\n{}\n",
        s(&stats["total_episodes"]),
        types,
        s(&stats["avg_importance"]),
        lines.join("\n")
    ))
}
