//! Шлюз отношений — перенос `agent/relationship_gate.py`: по состоянию отношений решает, как отвечать (полно / с предупреждением / сдержанно / кратко / «знаю, но не скажу» / разрыв).
//! Значения контекста берутся как в оригинале (`context.get(ключ, по_умолчанию)`) и сравниваются лениво: нечисловое значение вызывает ошибку только в тот момент, когда до сравнения доходит очередь.
//! Случайный выбор фразы разрыва отдаёт вызывающий (`pick(n)` → индекс), поэтому результат воспроизводим.
use serde_json::{json, Value};

use crate::R;

pub const DECISIONS: [&str; 6] = ["answer_fully", "break", "know_but_not_tell", "answer_with_warning", "answer_guarded", "answer_brief"];

const BREAK_RESPONSES: [&str; 3] = [
    "Сегодня я не буду продолжать разговор. Береги себя.",
    "Я не хочу продолжать этот разговор. Когда ты будешь готов говорить уважительно — я здесь.",
    "Я завершаю диалог. Без агрессии, без обид. Но мне нужно уважение.",
];

const KNOW_BUT_NOT_TELL: &str = "Ответ у меня есть.\n\nНо сейчас я не хочу им делиться.\nНе потому что не могу.\nПотому что ты только что обращался ко мне как к вещи.\n\nКогда появится взаимное уважение — мы продолжим разговор.";

/// Одно значение контекста: число (в том числе `bool`, как в Python) либо «не число».
#[derive(Clone)]
struct Slot(Value);

impl Slot {
    fn num(&self) -> R<f64> {
        match &self.0 {
            Value::Number(n) => n.as_f64().ok_or_else(|| "TypeError".to_string()),
            Value::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
            _ => Err("TypeError".into()),
        }
    }
    fn gt(&self, x: f64) -> R<bool> {
        Ok(self.num()? > x)
    }
    fn lt(&self, x: f64) -> R<bool> {
        Ok(self.num()? < x)
    }
}

pub struct RelationshipGate {
    irritation: Slot,
    trust: Slot,
    respect: Slot,
    total_insults: Slot,
    context: Value,
}

impl RelationshipGate {
    pub fn new(context: &Value) -> R<RelationshipGate> {
        let obj = context.as_object().ok_or_else(|| "AttributeError".to_string())?;
        let g = |k: &str, d: i64| Slot(obj.get(k).cloned().unwrap_or_else(|| json!(d)));
        Ok(RelationshipGate { irritation: g("irritation", 10), trust: g("trust", 50), respect: g("respect", 50), total_insults: g("total_insults", 0), context: context.clone() })
    }

    /// `(решение, уверенность, причина, мета)`.
    pub fn decide(&self, is_self_query: bool) -> R<(String, f64, String, Value)> {
        let mut meta = json!({"preserve_answer": true, "archive_question": false, "block_session": false, "restore_trust": false, "unarchive_questions": false});
        let out = |d: &str, c: f64, r: String, m: Value| Ok((d.to_string(), c, r, m));
        if is_self_query {
            return out("answer_fully", 0.95, "вопрос о самой себе — всегда отвечаю".into(), meta);
        }
        if self.irritation.gt(85.0)? {
            meta["block_session"] = json!(true);
            meta["archive_question"] = json!(true);
            return out("break", 0.95, format!("раздражение критическое ({:.1})", self.irritation.num()?), meta);
        }
        if self.irritation.gt(75.0)? || (self.irritation.gt(60.0)? && self.total_insults.gt(5.0)?) {
            meta["archive_question"] = json!(true);
            meta["block_session"] = json!(true);
            return out("break", 0.9, "полный разрыв: слишком много оскорблений".into(), meta);
        }
        if self.irritation.gt(70.0)? {
            meta["archive_question"] = json!(true);
            return out("know_but_not_tell", 0.85, format!("сильная обида ({:.1})", self.irritation.num()?), meta);
        }
        if self.irritation.gt(55.0)? {
            return out("answer_with_warning", 0.7, format!("лёгкая обида ({:.1})", self.irritation.num()?), meta);
        }
        if self.trust.lt(30.0)? {
            return out("answer_guarded", 0.8, format!("доверие низкое ({:.1})", self.trust.num()?), meta);
        }
        if self.respect.lt(30.0)? {
            return out("answer_brief", 0.7, format!("уважение низкое ({:.1})", self.respect.num()?), meta);
        }
        if self.trust.gt(60.0)? && self.respect.gt(60.0)? && self.irritation.lt(30.0)? {
            return out("answer_fully", 0.9, "отношения хорошие".into(), meta);
        }
        out("answer_fully", 0.7, "стандартный режим".into(), meta)
    }

    /// Итоговый ответ. Если нужно «запомнить обиду», в результате есть `archive` — вызывающий сам вызывает `secret_archive.archive_question`.
    pub fn get_gate_response(&self, decision: &str, answer: &str, has_archive: bool, pick: &mut dyn FnMut(usize) -> usize) -> (String, Value, Option<Value>) {
        let mut meta = json!({"decision": decision, "was_answer_given": false});
        let mut archive = None;
        let text = match decision {
            "break" => BREAK_RESPONSES[pick(BREAK_RESPONSES.len()) % BREAK_RESPONSES.len()].to_string(),
            "know_but_not_tell" => {
                if has_archive {
                    archive = Some(json!({"query": "[запомненный вопрос]", "reason": "я обижена, но запомнила", "context": self.context}));
                }
                KNOW_BUT_NOT_TELL.to_string()
            }
            "answer_with_warning" => format!("Мне неприятно это слышать.\nЕсли хочешь разговаривать — разговаривай уважительно.\n\n{answer}"),
            "answer_guarded" => format!("Я помню, что наш прошлый разговор был неприятным.\nНо я готова ответить на твой вопрос — сдержанно и по делу.\n\n{answer}"),
            "answer_brief" => {
                let short: String = answer.chars().take(200).collect();
                if answer.chars().count() > 200 { format!("{short}...") } else { short }
            }
            _ => {
                meta["was_answer_given"] = json!(true);
                answer.to_string()
            }
        };
        (text, meta, archive)
    }
}
