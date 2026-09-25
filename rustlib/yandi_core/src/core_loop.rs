//! Главный цикл жизни — перенос `agent/core_loop.py`: восприятие → модель мира → модель себя → оценка целей → рефлексия → действие → запоминание. Система живёт между запросами, а не
//! только реагирует. Один шаг = один полный цикл; ошибка внутри цикла не рвёт процесс: счётчик ошибок растёт, возвращается `{"status": "failed", …}`.
//! Фоновый поток (`start_background` в оригинале) сюда не входит: расписание циклов — забота узла (он вызывает `run_cycle` с пустым входом).
use serde_json::{json, Value};

use crate::ctx::Ctx;
use crate::motivation::MotivationSystem;
use crate::reflection_loop::ReflectionLoop;
use crate::{memory_episodic, self_model, R};

#[derive(Debug, Clone, PartialEq)]
pub struct LoopState {
    pub is_running: bool,
    pub cycle_number: i64,
    pub last_perceive: f64,
    pub last_update: f64,
    pub last_reflect: f64,
    pub last_act: f64,
    pub last_remember: f64,
    pub current_query: Value,
    pub current_response: Value,
    pub current_epistemic: Value,
    pub total_actions: i64,
    pub total_reflections: i64,
    pub total_errors: i64,
}

impl Default for LoopState {
    fn default() -> Self {
        LoopState {
            is_running: false, cycle_number: 0, last_perceive: 0.0, last_update: 0.0, last_reflect: 0.0, last_act: 0.0, last_remember: 0.0,
            current_query: Value::Null, current_response: Value::Null, current_epistemic: json!({}), total_actions: 0, total_reflections: 0, total_errors: 0,
        }
    }
}

impl LoopState {
    pub fn to_json(&self) -> Value {
        json!({
            "is_running": self.is_running, "cycle_number": self.cycle_number, "last_perceive": self.last_perceive, "last_update": self.last_update, "last_reflect": self.last_reflect,
            "last_act": self.last_act, "last_remember": self.last_remember, "current_query": self.current_query, "current_response": self.current_response, "current_epistemic": self.current_epistemic,
            "total_actions": self.total_actions, "total_reflections": self.total_reflections, "total_errors": self.total_errors,
        })
    }

    pub fn from_json(v: &Value) -> LoopState {
        let f = |k: &str| v[k].as_f64().unwrap_or(0.0);
        let i = |k: &str| v[k].as_i64().unwrap_or(0);
        LoopState {
            is_running: v["is_running"].as_bool().unwrap_or(false), cycle_number: i("cycle_number"), last_perceive: f("last_perceive"), last_update: f("last_update"), last_reflect: f("last_reflect"),
            last_act: f("last_act"), last_remember: f("last_remember"), current_query: v["current_query"].clone(), current_response: v["current_response"].clone(),
            current_epistemic: v["current_epistemic"].clone(), total_actions: i("total_actions"), total_reflections: i("total_reflections"), total_errors: i("total_errors"),
        }
    }
}

/// Экземпляр цикла (состояние в памяти + подсистемы).
#[derive(Debug, Clone)]
pub struct CoreLoop {
    pub state: LoopState,
    /// `_last_reflection_query`: `None` — атрибута ещё нет (`hasattr` ложно).
    pub last_reflection_query: Option<Value>,
    pub reflection: ReflectionLoop,
    pub motivation: MotivationSystem,
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

/// `d.get(k, default)` для dict Python; не dict — `AttributeError`.
fn dget<'a>(d: &'a Value, k: &str) -> R<Option<&'a Value>> {
    if !d.is_object() {
        return Err("AttributeError".into());
    }
    Ok(d.get(k))
}

fn py_str_of(v: &Value) -> R<String> {
    v.as_str().map(String::from).ok_or_else(|| "TypeError".to_string())
}

fn number(v: &Value) -> R<f64> {
    match v {
        Value::Number(n) => Ok(n.as_f64().unwrap_or(0.0)),
        Value::Bool(b) => Ok(*b as i64 as f64),
        _ => Err("TypeError".into()),
    }
}

fn first3(v: &Value) -> Value {
    json!(v.as_array().map(|a| a.iter().take(3).cloned().collect::<Vec<_>>()).unwrap_or_default())
}

impl CoreLoop {
    /// Конструктор: модель себя, эпизодическая память (без состояния), рефлексия, мотивация.
    pub fn new(cx: &Ctx) -> R<CoreLoop> {
        self_model::init(cx)?;
        let reflection = ReflectionLoop::new(cx)?;
        let motivation = MotivationSystem::load(cx)?;
        Ok(CoreLoop { state: LoopState::default(), last_reflection_query: None, reflection, motivation })
    }

    pub fn to_json(&self) -> Value {
        json!({
            "state": self.state.to_json(), "last_reflection_query": self.last_reflection_query, "has_last_reflection_query": self.last_reflection_query.is_some(),
            "reflection": self.reflection.to_json(), "motivation": self.motivation.m.to_json(),
        })
    }

    pub fn from_json(v: &Value) -> CoreLoop {
        CoreLoop {
            state: LoopState::from_json(&v["state"]),
            last_reflection_query: if v["has_last_reflection_query"].as_bool().unwrap_or(false) { Some(v["last_reflection_query"].clone()) } else { None },
            reflection: ReflectionLoop::from_json(&v["reflection"]),
            motivation: MotivationSystem { m: crate::motivation::Motivation::from_state(&v["motivation"]) },
        }
    }

    /// Шаг 1: восприятие.
    pub fn perceive(&mut self, cx: &Ctx, input: Option<&Value>) -> R<Value> {
        let now = cx.now_secs();
        self.state.last_perceive = now;
        self.state.cycle_number += 1;
        let perception = json!({
            "timestamp": cx.now_secs(), "cycle": self.state.cycle_number, "has_input": input.is_some(),
            "input": match input { Some(v) if truthy(Some(v)) => v.clone(), _ => json!({}) },
        });
        if let Some(inp) = input.filter(|v| truthy(Some(v))) {
            self.state.current_query = dget(inp, "query")?.cloned().unwrap_or(Value::Null);
            self.state.current_epistemic = dget(inp, "epistemic")?.cloned().unwrap_or_else(|| json!({}));
        }
        self_model::increment_cycle(cx)?;
        Ok(perception)
    }

    /// Шаг 2: обновление модели мира (пока заглушка: новое восприятие и три неопределённости).
    pub fn update_world_model(&self, cx: &Ctx, perception: &Value) -> R<Value> {
        let unc = self_model::row(cx)?;
        Ok(json!({"timestamp": cx.now_secs(), "new_info": dget(perception, "input")?.cloned().unwrap_or_else(|| json!({})), "uncertainties": first3(&unc["current_uncertainties"])}))
    }

    /// Шаг 3: модель себя — здоровье, возраст, цели, ограничения; плохое здоровье записывается ошибкой.
    pub fn update_self_model(&self, cx: &Ctx) -> R<Value> {
        let health = self_model::check_health(cx)?;
        let row = self_model::row(cx)?;
        let goals = self_model::get_goals(cx)?;
        let update = json!({"timestamp": cx.now_secs(), "health": health, "age": row["total_cycles"], "goals": goals, "limits": row["limitations"]});
        if health["health_score"].as_i64().unwrap_or(100) < 50 {
            memory_episodic::add_error(cx, &format!("Низкий health_score: {}", health["health_score"]), json!({"health": health}), 0.8)?;
        }
        Ok(update)
    }

    /// Шаг 4: оценка целей; целей нет — добавляются три базовые.
    pub fn evaluate_goals(&self, cx: &Ctx) -> R<Value> {
        let goals = self_model::get_goals(cx)?;
        let n_before = goals.as_array().map(|a| a.len()).ok_or_else(|| "TypeError".to_string())?;
        if n_before == 0 {
            self_model::add_goal(cx, "Быть полезным")?;
            self_model::add_goal(cx, "Не врать")?;
            self_model::add_goal(cx, "Учиться на ошибках")?;
        }
        Ok(json!({"timestamp": cx.now_secs(), "current_goals": self_model::get_goals(cx)?, "motivation": self.motivation.get_summary(), "needs_new_goals": n_before < 2}))
    }

    /// Шаг 5: рефлексия над текущим запросом (один раз на запрос) либо общая рефлексия состояния.
    pub fn reflect(&mut self, cx: &Ctx) -> R<Value> {
        if truthy(Some(&self.state.current_query)) {
            let same = self.last_reflection_query.as_ref() == Some(&self.state.current_query);
            if !same {
                let ep = self.state.current_epistemic.clone();
                let trust = dget(&ep, "trust")?.cloned().unwrap_or_else(|| json!("UNVERIFIED"));
                let confidence = dget(&ep, "confidence")?.cloned().unwrap_or_else(|| json!(0.5));
                let q = self.state.current_query.clone();
                let result = self.reflection.reflect_on_query(cx, &q, &ep, &trust, &confidence, &[], None, None)?;
                self.last_reflection_query = Some(self.state.current_query.clone());
                self.state.total_reflections += 1;
                return Ok(result.to_json());
            }
        }
        Ok(json!({"timestamp": cx.now_secs(), "type": "general", "state": self_model::reflect(cx)?, "memory_stats": memory_episodic::get_stats(cx)?}))
    }

    /// Шаг 6: действие; мотивация может его пропустить (`explore` / `verify`).
    pub fn act(&mut self, cx: &Ctx, action_type: &str, data: &Value) -> R<Value> {
        self.state.last_act = cx.now_secs();
        self.state.total_actions += 1;
        if action_type == "explore" {
            let conf = number(dget(data, "confidence")?.unwrap_or(&json!(0.5)))?;
            let unc = number(dget(data, "uncertainty")?.unwrap_or(&json!(0.5)))?;
            if !self.motivation.should_explore(conf, unc) {
                return Ok(json!({"status": "skipped", "reason": "мотивация недостаточна"}));
            }
        }
        if action_type == "verify" {
            let trust = dget(data, "trust")?.cloned().unwrap_or_else(|| json!("UNVERIFIED"));
            let conf = number(dget(data, "confidence")?.unwrap_or(&json!(0.5)))?;
            if !self.motivation.should_verify(trust.as_str().unwrap_or("\u{0}"), conf) {
                return Ok(json!({"status": "skipped", "reason": "верификация не требуется"}));
            }
        }
        let result = json!({"timestamp": cx.now_secs(), "type": action_type, "data": data, "status": "executed"});
        self_model::increment_queries(cx)?;
        Ok(result)
    }

    /// Шаг 7: запоминание в эпизодической памяти и сдвиг мотивации от результата.
    pub fn remember(&mut self, cx: &Ctx, action_result: &Value) -> R<Value> {
        self.state.last_remember = cx.now_secs();
        if dget(action_result, "type")?.and_then(|t| t.as_str()) == Some("query") {
            let data = dget(action_result, "data")?.cloned().unwrap_or_else(|| json!({}));
            let g = |k: &str, d: Value| -> R<Value> { Ok(dget(&data, k)?.cloned().unwrap_or(d)) };
            memory_episodic::add_query_values(cx, &g("query", json!(""))?, &g("domain", json!(""))?, &g("answer_mode", json!(""))?, &g("trust", json!(""))?, &g("confidence", json!(0.5))?)?;
        } else {
            let t = dget(action_result, "type")?.cloned().unwrap_or_else(|| json!("action"));
            // `f"Действие: {action_result.get('type')}"` — БЕЗ значения по умолчанию: нет типа → «None»
            let shown = match dget(action_result, "type")? {
                None | Some(Value::Null) => "None".to_string(),
                Some(v) => crate::reflection_loop::py_str_pub(v),
            };
            memory_episodic::add(cx, &t.as_str().map(String::from).ok_or_else(|| "TypeError".to_string())?, &format!("Действие: {shown}"), action_result.clone(), 0.5, &[])?;
        }
        let executed = dget(action_result, "status")?.and_then(|s| s.as_str()) == Some("executed");
        self.motivation.update_from_experience(cx, &json!({"was_useful": executed, "was_correct": executed, "error": if executed { Value::Null } else { json!("action_failed") }}))?;
        Ok(json!({"timestamp": cx.now_secs(), "remembered": true, "memory_stats": memory_episodic::get_stats(cx)?}))
    }

    /// Один полный цикл. Ошибка внутри цикла: счётчик ошибок растёт, возвращается `{"status": "failed", …}`.
    pub fn run_cycle(&mut self, cx: &Ctx, input: Option<&Value>) -> R<Value> {
        if !self.state.is_running {
            self.state.is_running = true;
        }
        match self.cycle_body(cx, input) {
            Ok(v) => Ok(v),
            Err(e) => {
                self.state.total_errors += 1;
                self_model::increment_errors(cx)?;
                Ok(json!({"cycle": self.state.cycle_number, "error": e, "timestamp": cx.now_secs(), "status": "failed"}))
            }
        }
    }

    fn cycle_body(&mut self, cx: &Ctx, input: Option<&Value>) -> R<Value> {
        let perception = self.perceive(cx, input)?;
        let world_update = self.update_world_model(cx, &perception)?;
        let self_update = self.update_self_model(cx)?;
        let goal_eval = self.evaluate_goals(cx)?;
        let reflection = self.reflect(cx)?;
        self.state.total_reflections += 1;
        let action = match input.filter(|v| truthy(Some(v))) {
            Some(inp) => {
                let ep = dget(inp, "epistemic")?.cloned().unwrap_or_else(|| json!({}));
                let g = |k: &str, d: Value| -> R<Value> { Ok(dget(&ep, k)?.cloned().unwrap_or(d)) };
                let data = json!({
                    "query": dget(inp, "query")?.cloned().unwrap_or_else(|| json!("")), "domain": g("domain", json!(""))?, "answer_mode": g("answer_mode", json!(""))?,
                    "trust": g("trust", json!(""))?, "confidence": g("confidence", json!(0.5))?,
                });
                self.act(cx, "query", &data)?
            }
            None => self.act(cx, "idle", &json!({"reason": "no input"}))?,
        };
        let memory_result = self.remember(cx, &action)?;
        Ok(json!({
            "cycle": self.state.cycle_number, "perception": perception, "world_update": world_update, "self_update": self_update, "goal_eval": goal_eval, "reflection": reflection,
            "action": action, "memory": memory_result, "timestamp": cx.now_secs(),
        }))
    }

    /// Статус цикла (`background_active` у ядра всегда пуст — расписание циклов ведёт узел).
    pub fn get_status(&self, cx: &Ctx) -> R<Value> {
        Ok(json!({
            "is_running": self.state.is_running, "cycle_number": self.state.cycle_number, "total_actions": self.state.total_actions, "total_reflections": self.state.total_reflections,
            "total_errors": self.state.total_errors, "current_query": self.state.current_query, "background_active": Value::Null, "self_model": self_model::reflect(cx)?,
            "memory_stats": memory_episodic::get_stats(cx)?, "motivation": self.motivation.get_summary(),
        }))
    }

    /// Текстовое представление статуса.
    pub fn summary_text(&self, cx: &Ctx) -> R<String> {
        let st = self.get_status(cx)?;
        let sm = &st["self_model"];
        let mem = &st["memory_stats"];
        let mot = &st["motivation"];
        let shown = |v: &Value| crate::reflection_loop::py_str_pub(v);
        let cq = if truthy(Some(&st["current_query"])) { shown(&st["current_query"]) } else { "нет".to_string() };
        let bg = if truthy(Some(&st["background_active"])) { "✅ Активен" } else { "❌ Неактивен" };
        let f = |v: &Value| v.as_f64().unwrap_or(0.0);
        Ok(format!(
            "\n=== YANDI CORE LOOP ===\nСтатус: {}\nЦикл: {}\nДействий: {}\nРефлексий: {}\nОшибок: {}\nФоновый поток: {}\nТекущий запрос: {}\n\nSelf Model:\n  Возраст: {} циклов\n  Запросов: {}\n  Жива: {}\n\nПамять:\n  Эпизодов: {}\n  Типы: {}\n\nМотивация:\n  Точность: {:.2}\n  Любопытство: {:.2}\n  Полезность: {:.2}\n",
            if self.state.is_running { "✅ Работает" } else { "⏸ Остановлен" }, st["cycle_number"], st["total_actions"], st["total_reflections"], st["total_errors"], bg, cq,
            shown(&sm["age"]), shown(&sm["total_queries"]), if truthy(Some(&sm["is_alive"])) { "✅" } else { "❌" }, shown(&mem["total_episodes"]),
            crate::reflection_loop::py_repr(&mem["by_type"]), f(&mot["accuracy"]), f(&mot["curiosity"]), f(&mot["usefulness"]),
        ))
    }
}
