//! Собственные факты человека, запоминаемые с происхождением — перенос `agent/personal_facts.py`. ФАКТ = «человек так сказал», не объективная истина; у факта всегда есть исходная реплика;
//! поправка ДОБАВЛЯЕТ, а не перезаписывает; повтор той же реплики не создаёт новой истории. Статус факта СВОРАЧИВАЕТСЯ из событий при чтении (current / historical / superseded).
use std::collections::HashSet;

use serde_json::{json, Map, Value};

use crate::ctx::Ctx;
use crate::relationship_memory::content_stems;
use crate::{causal_events, R};

pub const CLAIM_TYPE: &str = "personal_facts";
pub const MAX_PROFILE_CURRENT: usize = 15;
pub const MAX_PROFILE_HISTORICAL: usize = 3;
pub const MAX_RELEVANT: usize = 4;
pub const MIN_RELEVANCE: f64 = 0.18;
pub const MAX_STATEMENT_CHARS: usize = 200;

/// Проверенный факт, извлечённый из ОДНОЙ реплики (`pet.fact_extraction.ExtractedFact`).
#[derive(Debug, Clone)]
pub struct ExtractedFact {
    pub fact_class: String,
    pub statement: String,
    pub polarity: String,
    pub temporality: String,
    pub evidence: String,
    pub start: Option<i64>,
    pub end: Option<i64>,
    pub relation: String,
    pub target_fact_id: Option<String>,
}

impl ExtractedFact {
    pub fn from_json(v: &Value) -> ExtractedFact {
        let s = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
        ExtractedFact {
            fact_class: s("fact_class"),
            statement: s("statement"),
            polarity: s("polarity"),
            temporality: s("temporality"),
            evidence: s("evidence"),
            start: v.get("start").and_then(|x| x.as_i64()),
            end: v.get("end").and_then(|x| x.as_i64()),
            relation: v.get("relation").and_then(|x| x.as_str()).unwrap_or("none").to_string(),
            target_fact_id: v.get("target_fact_id").and_then(|x| x.as_str()).map(String::from),
        }
    }
}

fn st<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("")
}

/// Факты, новые сначала, каждый со свёрнутым `status`, `restated` (число позднейших повторов) и `superseded_by`. Чисто.
pub fn fold(facts: &[Value], events: &[Value]) -> Vec<Value> {
    facts
        .iter()
        .map(|f| {
            let evs: Vec<&Value> = events.iter().filter(|e| e["fact_id"] == f["fact_id"]).collect();
            let superseded = evs.iter().find(|e| st(e, "event_type") == "superseded");
            let restated = evs.iter().filter(|e| st(e, "event_type") == "restated").count();
            let status = if superseded.is_some() {
                "superseded"
            } else if st(f, "temporality") == "past" {
                "historical"
            } else {
                "current"
            };
            let mut o: Map<String, Value> = f.as_object().cloned().unwrap_or_default();
            o.insert("status".into(), json!(status));
            o.insert("restated".into(), json!(restated));
            o.insert("superseded_by".into(), superseded.map(|e| e["by_fact_id"].clone()).unwrap_or(Value::Null));
            Value::Object(o)
        })
        .collect()
}

pub fn list_facts(cx: &Ctx, user_id: &str) -> R<Vec<Value>> {
    let facts = cx.repo("list_personal_facts", json!({"user_id": user_id}))?;
    let events = cx.repo("list_personal_fact_events", json!({"user_id": user_id}))?;
    Ok(fold(facts.as_array().map(|a| a.as_slice()).unwrap_or(&[]), events.as_array().map(|a| a.as_slice()).unwrap_or(&[])))
}

/// ТЕКУЩИЕ факты человека как цели связывания для извлекателя (новые сначала).
pub fn known_for_linking(folded: &[Value], limit: usize) -> Vec<Value> {
    folded.iter().filter(|f| st(f, "status") == "current").take(limit).map(|f| json!({"fact_id": f["fact_id"], "statement": f["statement"]})).collect()
}

/// Ключ ННОРМАЛИЗОВАННОГО утверждения: то же утверждение в другой реплике — ещё одно вхождение факта, не второй факт.
fn proposition_key(statement: &str, polarity: &str, temporality: &str) -> (String, String, String) {
    let lowered = yandi_rs::py_text::py_casefold(statement).replace('ё', "е");
    let words = yandi_rs::py_text::py_split_whitespace(&lowered).collect::<Vec<_>>().join(" ");
    let words = words.trim_matches(|c| " .,;:!?…".contains(c)).to_string();
    (words, polarity.to_string(), temporality.to_string())
}

/// Применить проверенные факты ОДНОЙ опознанной реплики. `applied=false` — нечего применять либо факты этой реплики уже применены (повтор).
pub fn record_turn_facts(cx: &Ctx, user_id: &str, source_turn_id: Option<&str>, facts: &[ExtractedFact]) -> R<Value> {
    let mut result = json!({"applied": false, "new": 0, "restated": 0, "superseded": 0});
    if facts.is_empty() {
        return Ok(result);
    }
    let Some(turn) = source_turn_id.filter(|t| !t.is_empty()) else {
        return Err("ValueError: personal facts need the client-minted source turn id".into());
    };
    if !causal_events::may_apply(causal_events::claim(cx, user_id, Some(turn), CLAIM_TYPE, None)?) {
        return Ok(result);
    }
    // текущие факты в порядке списка (новые сначала); словарь Python сохраняет порядок вставки
    let mut current: Vec<Value> = list_facts(cx, user_id)?.into_iter().filter(|f| st(f, "status") == "current").collect();
    let now = cx.now_value();
    let (mut n_new, mut n_restated, mut n_superseded) = (0, 0, 0);
    for f0 in facts {
        let mut f = f0.clone();
        let mut target: Option<Value> = f.target_fact_id.as_deref().and_then(|id| current.iter().find(|c| st(c, "fact_id") == id).cloned());
        if target.is_none() && f.relation != "replaces" {
            let key = proposition_key(&f.statement, &f.polarity, &f.temporality);
            target = current.iter().find(|c| proposition_key(st(c, "statement"), st(c, "polarity"), st(c, "temporality")) == key).cloned();
            if target.is_some() {
                f.relation = "same".into();
            }
        }
        if f.relation == "same" {
            if let Some(t) = &target {
                if proposition_key(&f.statement, &f.polarity, &f.temporality) != proposition_key(st(t, "statement"), st(t, "polarity"), st(t, "temporality")) {
                    target = None; // экстрактор назвал это повтором, но сказано другое: новый факт
                }
            }
        }
        if f.relation == "same" {
            if let Some(t) = &target {
                cx.repo("insert_personal_fact_event", json!({"fact_id": t["fact_id"], "user_id": user_id, "event_type": "restated", "by_fact_id": null, "evidence": f.evidence, "span_start": f.start, "span_end": f.end, "source_turn_id": turn, "created_at": now}))?;
                n_restated += 1;
                continue;
            }
        }
        let fact_id = format!("pf_{}_{}", cx.now_secs() as i64, &cx.uuid_hex()[..10]);
        let statement: String = f.statement.chars().take(MAX_STATEMENT_CHARS).collect();
        cx.repo("insert_personal_fact", json!({"fact_id": fact_id, "user_id": user_id, "fact_class": f.fact_class, "statement": statement, "polarity": f.polarity, "temporality": f.temporality, "evidence": f.evidence, "span_start": f.start, "span_end": f.end, "source_turn_id": turn, "created_at": now}))?;
        n_new += 1;
        if f.relation == "replaces" {
            if let Some(t) = &target {
                cx.repo("insert_personal_fact_event", json!({"fact_id": t["fact_id"], "user_id": user_id, "event_type": "superseded", "by_fact_id": fact_id, "evidence": f.evidence, "span_start": f.start, "span_end": f.end, "source_turn_id": turn, "created_at": now}))?;
                current.retain(|c| c["fact_id"] != t["fact_id"]);
                n_superseded += 1;
            }
        }
    }
    result = json!({"applied": true, "new": n_new, "restated": n_restated, "superseded": n_superseded});
    Ok(result)
}

fn relevance(current: &HashSet<String>, fact: &Value) -> f64 {
    let stems = content_stems(&format!("{} {}", st(fact, "statement"), st(fact, "evidence")));
    if current.is_empty() || stems.is_empty() {
        return 0.0;
    }
    let shared = current.intersection(&stems).count();
    if shared == 0 {
        return 0.0;
    }
    shared as f64 / ((current.len() * stems.len()) as f64).sqrt()
}

/// Факты для промпта ответа. `profile=true` (человек спрашивает, что о нём известно): текущие (с ограничением) + несколько исторических; иначе только разделяющие
/// содержание с сообщением. Поправленные (superseded) не называются никогда.
pub fn select_for_prompt(folded: &[Value], current_text: &str, profile: bool) -> Vec<Value> {
    if folded.is_empty() {
        return vec![];
    }
    let live: Vec<&Value> = folded.iter().filter(|f| st(f, "status") != "superseded").collect();
    let chosen: Vec<&Value> = if profile {
        let cur = live.iter().filter(|f| st(f, "status") == "current").take(MAX_PROFILE_CURRENT).cloned();
        let hist = live.iter().filter(|f| st(f, "status") == "historical").take(MAX_PROFILE_HISTORICAL).cloned();
        cur.chain(hist).collect()
    } else {
        let stems = content_stems(current_text);
        let mut scored: Vec<(f64, &Value)> = live.iter().map(|f| (relevance(&stems, f), *f)).collect();
        scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal)); // стабильно, по убыванию
        scored.into_iter().filter(|(s, _)| *s >= MIN_RELEVANCE).take(MAX_RELEVANT).map(|(_, f)| f).collect()
    };
    chosen
        .into_iter()
        .map(|f| {
            let when = f["created_at"].as_str().map(|s| s.chars().take(10).collect::<String>()).unwrap_or_default();
            json!({"fact_id": f["fact_id"], "statement": f["statement"], "polarity": f["polarity"], "status": f["status"], "when": when, "source_turn_id": f["source_turn_id"], "restated": f["restated"]})
        })
        .collect()
}
