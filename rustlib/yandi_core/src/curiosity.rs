//! Движок любопытства — перенос `agent/curiosity.py`: система сама находит пробелы в знаниях (убеждения с низкой или средней уверенностью, слабые ответы, интерпретативные вопросы, отсутствие
//! консенсуса), формирует из них вопросы и приоритизирует неизвестное. Список неизвестных живёт в экземпляре, как в оригинале; мост Python↔Rust передаёт его явно.
use serde_json::{json, Value};

use crate::belief_manager;
use crate::ctx::Ctx;
use crate::R;

fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Неизвестное, которое система хочет исследовать.
#[derive(Debug, Clone, PartialEq)]
pub struct Unknown {
    pub id: String,
    /// В оригинале — любое значение (берётся из `epistemic["domain"]` как есть).
    pub topic: Value,
    pub question: String,
    pub priority: f64,
    pub reason: String,
    pub created_at: f64,
    pub status: String,
    pub related_claims: Vec<String>,
    pub confidence_gap: f64,
    pub related_belief_id: Option<String>,
}

impl Unknown {
    pub fn to_json(&self) -> Value {
        json!({
            "id": self.id, "topic": self.topic, "question": self.question, "priority": self.priority, "reason": self.reason, "created_at": self.created_at, "status": self.status,
            "related_claims": self.related_claims, "confidence_gap": self.confidence_gap, "related_belief_id": self.related_belief_id,
        })
    }

    pub fn from_json(v: &Value) -> Unknown {
        Unknown {
            id: v["id"].as_str().unwrap_or("").to_string(),
            topic: v["topic"].clone(),
            question: v["question"].as_str().unwrap_or("").to_string(),
            priority: v["priority"].as_f64().unwrap_or(0.0),
            reason: v["reason"].as_str().unwrap_or("").to_string(),
            created_at: v["created_at"].as_f64().unwrap_or(0.0),
            status: v["status"].as_str().unwrap_or("").to_string(),
            related_claims: v["related_claims"].as_array().map(|a| a.iter().map(|x| x.as_str().unwrap_or("").to_string()).collect()).unwrap_or_default(),
            confidence_gap: v["confidence_gap"].as_f64().unwrap_or(0.0),
            related_belief_id: v["related_belief_id"].as_str().map(String::from),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct CuriosityEngine {
    pub unknowns: Vec<Unknown>,
}

impl CuriosityEngine {
    pub fn to_json(&self) -> Value {
        json!({"unknowns": self.unknowns.iter().map(|u| u.to_json()).collect::<Vec<_>>()})
    }

    pub fn from_json(v: &Value) -> CuriosityEngine {
        CuriosityEngine { unknowns: v["unknowns"].as_array().map(|a| a.iter().map(Unknown::from_json).collect()).unwrap_or_default() }
    }

    fn create_unknown(&self, cx: &Ctx, topic: Value, question: String, priority: f64, reason: String, confidence_gap: f64, related_belief_id: Option<String>) -> Unknown {
        Unknown {
            id: format!("unk_{}", cut(&cx.uuid_hex(), 8)),
            topic,
            question,
            priority: priority.min(1.0).max(0.0),
            reason,
            created_at: cx.now_secs(),
            status: "pending".into(),
            related_claims: vec![],
            confidence_gap,
            related_belief_id,
        }
    }

    /// Добавить неизвестное, если такого вопроса ещё нет (иначе — поднять приоритет существующего).
    fn add_unknown(&mut self, unknown: Unknown) {
        for existing in self.unknowns.iter_mut() {
            if existing.question == unknown.question {
                existing.priority = existing.priority.max(unknown.priority);
                return;
            }
        }
        self.unknowns.push(unknown);
        // стабильная сортировка по убыванию приоритета
        self.unknowns.sort_by(|a, b| b.priority.partial_cmp(&a.priority).unwrap_or(std::cmp::Ordering::Equal));
    }

    /// Анализ убеждений: низкая уверенность → «почему», средняя → «какие ещё доказательства».
    pub fn analyze_beliefs(&mut self, cx: &Ctx) -> R<Vec<Unknown>> {
        let mut created: Vec<Unknown> = vec![];
        for b in belief_manager::get_all_active(cx)? {
            if b.confidence < 0.6 {
                created.push(self.create_unknown(
                    cx,
                    json!(b.topic),
                    format!("Почему уверенность в '{}...' составляет {:.2}?", cut(&b.statement, 40), b.confidence),
                    1.0 - b.confidence,
                    "Низкая уверенность в убеждении".into(),
                    1.0 - b.confidence,
                    Some(b.id.clone()),
                ));
            } else if b.confidence < 0.8 {
                created.push(self.create_unknown(
                    cx,
                    json!(b.topic),
                    format!("Какие ещё доказательства подтверждают '{}...'?", cut(&b.statement, 40)),
                    0.5,
                    "Средняя уверенность — нужны дополнительные данные".into(),
                    0.3,
                    Some(b.id.clone()),
                ));
            }
        }
        for u in &created {
            self.add_unknown(u.clone());
        }
        Ok(created)
    }

    /// Анализ ответа: что осталось неизвестным.
    pub fn analyze_response(&mut self, cx: &Ctx, query: &str, epistemic: &Value, confidence: f64, evidence_count: f64, trust: &str) -> R<Vec<Unknown>> {
        if !epistemic.is_object() {
            return Err("AttributeError".into());
        }
        let domain = || epistemic.get("domain").cloned().unwrap_or_else(|| json!("general"));
        let mut created: Vec<Unknown> = vec![];
        if confidence < 0.6 {
            created.push(self.create_unknown(cx, domain(), format!("Что ещё неизвестно о {}?", cut(query, 50)), 0.7, format!("Уверенность {:.2} — нужны дополнительные данные", confidence), 1.0 - confidence, None));
        }
        if evidence_count < 3.0 {
            let shown = if evidence_count.fract() == 0.0 { format!("{}", evidence_count as i64) } else { format!("{evidence_count}") };
            created.push(self.create_unknown(cx, domain(), "Какие ещё источники подтверждают или опровергают это?".into(), 0.6, format!("Найдено только {shown} источников"), 0.3, None));
        }
        if epistemic.get("testability").and_then(|t| t.as_str()) == Some("interpretive") {
            created.push(self.create_unknown(cx, json!("philosophical"), "Какие ещё рамки интерпретации существуют?".into(), 0.5, "Вопрос интерпретативный — нужно больше перспектив".into(), 0.4, None));
        }
        if trust == "UNVERIFIED" || trust == "PARTIALLY_SUPPORTED" {
            created.push(self.create_unknown(cx, domain(), "Почему нет консенсуса по этому вопросу?".into(), 0.6, format!("Trust {trust} — нужно понять причины неопределённости"), 0.5, None));
        }
        let low = yandi_rs::py_text::py_lower(query);
        if low.contains("сознание") {
            created.push(self.create_unknown(cx, json!("philosophy_of_mind"), "Что такое 'трудная проблема сознания'?".into(), 0.8, "Связано с текущим запросом".into(), 0.7, None));
        }
        if low.contains("жизнь") || low.contains("смысл") {
            created.push(self.create_unknown(cx, json!("philosophy"), "Какие основные философские позиции по смыслу жизни существуют?".into(), 0.7, "Связано с текущим запросом".into(), 0.6, None));
        }
        for u in &created {
            self.add_unknown(u.clone());
        }
        Ok(created)
    }

    /// Следующий вопрос для исследования: первый ожидающий становится «исследуется».
    pub fn get_next_question(&mut self) -> Option<Unknown> {
        for u in self.unknowns.iter_mut() {
            if u.status == "pending" {
                u.status = "exploring".into();
                return Some(u.clone());
            }
        }
        None
    }

    pub fn mark_resolved(&mut self, unknown_id: &str) {
        for u in self.unknowns.iter_mut() {
            if u.id == unknown_id {
                u.status = "resolved".into();
                break;
            }
        }
    }

    pub fn get_pending(&self) -> Vec<&Unknown> {
        self.unknowns.iter().filter(|u| u.status == "pending").collect()
    }

    pub fn get_exploring(&self) -> Vec<&Unknown> {
        self.unknowns.iter().filter(|u| u.status == "exploring").collect()
    }

    pub fn get_by_topic(&self, topic: &Value) -> Vec<&Unknown> {
        self.unknowns.iter().filter(|u| &u.topic == topic).collect()
    }

    pub fn get_summary(&self) -> Value {
        let top: Vec<Value> = self.unknowns.iter().take(5).filter(|u| u.status != "resolved").map(|u| json!({"question": cut(&u.question, 80), "priority": u.priority})).collect();
        json!({
            "total_unknowns": self.unknowns.len(), "pending": self.get_pending().len(), "exploring": self.get_exploring().len(),
            "resolved": self.unknowns.iter().filter(|u| u.status == "resolved").count(), "top_questions": top,
        })
    }

    pub fn to_dict(&self) -> Value {
        json!({
            "unknowns": self.unknowns.iter().map(|u| json!({"id": u.id, "topic": u.topic, "question": u.question, "priority": u.priority, "reason": u.reason, "status": u.status, "confidence_gap": u.confidence_gap, "related_belief_id": u.related_belief_id})).collect::<Vec<_>>(),
            "summary": self.get_summary(),
        })
    }
}
