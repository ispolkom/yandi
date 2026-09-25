//! Мотивационная система — перенос `agent/motivation.py`: внутренние стремления (точность, любопытство, непротиворечивость, полезность, безопасность) и готовность исследовать / осторожность,
//! решения «исследовать / проверять / уточнять / идти в веб» и сдвиг стремлений от опыта. Состояние хранится в `metadata["motivation"]` модели себя.
use serde_json::{json, Map, Value};

use crate::ctx::Ctx;
use crate::{self_model, R};

#[derive(Debug, Clone, PartialEq)]
pub struct Motivation {
    pub accuracy: f64,
    pub curiosity: f64,
    pub coherence: f64,
    pub usefulness: f64,
    pub safety: f64,
    pub exploration_rate: f64,
    pub caution_rate: f64,
    pub last_update: f64,
    pub history: Vec<Value>,
}

const FIELDS: [&str; 9] = ["accuracy", "curiosity", "coherence", "usefulness", "safety", "exploration_rate", "caution_rate", "last_update", "history"];

impl Motivation {
    fn default_at(now: f64) -> Motivation {
        Motivation { accuracy: 0.8, curiosity: 0.6, coherence: 0.7, usefulness: 0.9, safety: 0.8, exploration_rate: 0.3, caution_rate: 0.5, last_update: now, history: vec![] }
    }

    /// `Motivation(**d)`: неизвестный ключ или нечисловое значение → ошибка (вызывающий откатывается к значениям по умолчанию); отсутствующие ключи — значения по умолчанию.
    fn from_dict(d: &Map<String, Value>, now: f64) -> Result<Motivation, ()> {
        let mut m = Motivation::default_at(now);
        for (k, v) in d {
            if !FIELDS.contains(&k.as_str()) {
                return Err(());
            }
            match k.as_str() {
                "history" => m.history = v.as_array().cloned().ok_or(())?,
                _ => {
                    let x = v.as_f64().ok_or(())?;
                    match k.as_str() {
                        "accuracy" => m.accuracy = x,
                        "curiosity" => m.curiosity = x,
                        "coherence" => m.coherence = x,
                        "usefulness" => m.usefulness = x,
                        "safety" => m.safety = x,
                        "exploration_rate" => m.exploration_rate = x,
                        "caution_rate" => m.caution_rate = x,
                        _ => m.last_update = x,
                    }
                }
            }
        }
        Ok(m)
    }

    /// Восстановить из ранее возвращённого `to_json` (состояние экземпляра между вызовами моста).
    pub fn from_state(v: &Value) -> Motivation {
        Motivation::from_dict(v.as_object().unwrap_or(&Map::new()), 0.0).unwrap_or_else(|_| Motivation::default_at(0.0))
    }

    pub fn to_json(&self) -> Value {
        json!({
            "accuracy": self.accuracy, "curiosity": self.curiosity, "coherence": self.coherence, "usefulness": self.usefulness, "safety": self.safety,
            "exploration_rate": self.exploration_rate, "caution_rate": self.caution_rate, "last_update": self.last_update, "history": self.history,
        })
    }
}

/// Живой экземпляр системы (состояние в памяти + запись в модель себя при каждом изменении).
#[derive(Debug, Clone, PartialEq)]
pub struct MotivationSystem {
    pub m: Motivation,
}

fn clamp01(x: f64) -> f64 {
    x.min(1.0).max(0.0)
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().map(|x| x != 0.0).unwrap_or(true),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// `round(x, 2)` Python.
fn round2(x: f64) -> f64 {
    format!("{x:.2}").parse().unwrap_or(x)
}

impl MotivationSystem {
    /// Конструктор: создаёт модель себя при необходимости, читает `metadata["motivation"]`; нет или не подходит — значения по умолчанию.
    pub fn load(cx: &Ctx) -> R<MotivationSystem> {
        self_model::init(cx)?;
        let metadata = self_model::get_metadata(cx)?;
        let now = cx.now_secs();
        if let Some(d) = metadata.get("motivation").and_then(|v| v.as_object()) {
            if let Ok(m) = Motivation::from_dict(d, now) {
                return Ok(MotivationSystem { m });
            }
        }
        Ok(MotivationSystem { m: Motivation::default_at(now) })
    }

    fn save(&mut self, cx: &Ctx) -> R<()> {
        self.m.last_update = cx.now_secs();
        self_model::set_metadata_value(cx, "motivation", self.m.to_json())
    }

    pub fn set(&mut self, cx: &Ctx, which: &str, value: f64) -> R<()> {
        let v = clamp01(value);
        match which {
            "accuracy" => self.m.accuracy = v,
            "curiosity" => self.m.curiosity = v,
            "coherence" => self.m.coherence = v,
            "usefulness" => self.m.usefulness = v,
            "safety" => self.m.safety = v,
            "exploration" => self.m.exploration_rate = v,
            "caution" => self.m.caution_rate = v,
            other => return Err(format!("нет параметра {other}")),
        }
        self.save(cx)
    }

    /// Решение: исследовать ли новое.
    pub fn should_explore(&self, confidence: f64, uncertainty: f64) -> bool {
        self.m.curiosity * (1.0 - confidence) * (1.0 + uncertainty) > 0.3
    }

    /// Решение: проверять ли ответ.
    pub fn should_verify(&self, trust: &str, confidence: f64) -> bool {
        (trust == "UNVERIFIED" && confidence < 0.5) || (self.m.accuracy > 0.7 && confidence < 0.6)
    }

    /// Решение: задать уточняющий вопрос.
    pub fn should_ask_clarification(&self, uncertainty: f64) -> bool {
        uncertainty > 0.7 && self.m.safety > 0.6
    }

    /// Решение: использовать ли веб-поиск.
    pub fn should_use_web(&self, testability: &str, confidence: f64) -> bool {
        (testability == "fully_testable" && confidence < 0.6) || (self.m.usefulness > 0.8 && confidence < 0.5)
    }

    /// Предпочтительный режим ответа по мотивации.
    pub fn get_answer_mode_preference(&self, domain: &str, testability: &str) -> &'static str {
        if self.m.accuracy > 0.8 {
            if testability == "fully_testable" {
                return "factual";
            } else if testability == "partially_testable" {
                return "qualified_factual";
            }
        }
        if self.m.curiosity > 0.7 {
            if testability == "interpretive" {
                return "pluralistic_contextual";
            }
            return "exploratory";
        }
        if self.m.usefulness > 0.8 {
            if domain == "procedural" {
                return "procedural";
            }
            return "factual";
        }
        "contextual"
    }

    /// Сдвиг стремлений от опыта.
    pub fn update_from_experience(&mut self, cx: &Ctx, result: &Value) -> R<()> {
        if !result.is_object() {
            return Err("AttributeError".into());
        }
        let g = |k: &str| result.get(k);
        if truthy(g("was_useful")) {
            self.m.usefulness = (self.m.usefulness + 0.05).min(1.0);
        }
        if truthy(g("error")) {
            self.m.curiosity = (self.m.curiosity + 0.05).min(1.0);
        }
        if truthy(g("had_conflict")) {
            self.m.caution_rate = (self.m.caution_rate + 0.1).min(1.0);
            self.m.safety = (self.m.safety + 0.05).min(1.0);
        }
        if truthy(g("was_correct")) {
            self.m.accuracy = (self.m.accuracy + 0.02).min(1.0);
        }
        self.save(cx)
    }

    pub fn get_summary(&self) -> Value {
        json!({
            "accuracy": round2(self.m.accuracy), "curiosity": round2(self.m.curiosity), "coherence": round2(self.m.coherence), "usefulness": round2(self.m.usefulness),
            "safety": round2(self.m.safety), "exploration_rate": round2(self.m.exploration_rate), "caution_rate": round2(self.m.caution_rate),
        })
    }

    pub fn summary_text(&self) -> String {
        let f = |k: &str| self.get_summary()[k].as_f64().unwrap_or(0.0);
        format!(
            "\n=== МОТИВАЦИОННАЯ СИСТЕМА ===\nТочность (accuracy):    {:.2}\nЛюбопытство (curiosity): {:.2}\nНепротиворечивость:     {:.2}\nПолезность (usefulness): {:.2}\nБезопасность (safety):   {:.2}\nИсследование:           {:.2}\nОсторожность:           {:.2}\n",
            f("accuracy"), f("curiosity"), f("coherence"), f("usefulness"), f("safety"), f("exploration_rate"), f("caution_rate")
        )
    }
}
