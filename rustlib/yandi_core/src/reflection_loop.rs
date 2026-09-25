//! Рефлексивный цикл — перенос `agent/reflection_loop.py`: анализ собственных решений, выявление ошибок, уроки, политики планировщика (новая политика применяется лишь после трёх независимых
//! повторений; уверенность фиксируется при создании и от повторов не растёт) и запись результата в эпизодическую память и модель себя. ЖЁСТКИЙ ОТКАЗ: ошибка базы уходит вызывающему.
//! Состояние цикла (счётчик и список рефлексий) живёт в экземпляре, как в оригинале; мост Python↔Rust передаёт его явно.
use serde_json::{json, Value};

use crate::ctx::Ctx;
use crate::{memory_episodic, self_model, R};

/// Независимых повторений одного правила до применения к планировщику.
pub const MIN_OBSERVATIONS_TO_ACTIVATE: i64 = 3;

/// `query[:60]` Python: строка — первые 60 знаков, список — первые 60 элементов, прочее — `TypeError`.
fn slice60(v: &Value) -> R<Value> {
    match v {
        Value::String(s) => Ok(json!(cut(s, 60))),
        Value::Array(a) => Ok(Value::Array(a.iter().take(60).cloned().collect())),
        _ => Err("TypeError".into()),
    }
}

fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

#[derive(Debug, Clone, PartialEq)]
pub struct ReflectionResult {
    pub timestamp: f64,
    pub action_id: String,
    pub action_type: String,
    pub was_correct: bool,
    pub confidence: Value,
    pub alternatives: Vec<String>,
    pub chosen_path: Value,
    pub why_chosen: String,
    pub mistakes: Vec<String>,
    pub missing_information: Vec<String>,
    pub lessons: Vec<String>,
    pub should_change_behavior: bool,
    pub suggested_policy_change: Option<String>,
    pub applied_policy_changes: Vec<String>,
    pub self_state_after: Value,
    pub future_actions: Vec<String>,
}

impl ReflectionResult {
    pub fn to_json(&self) -> Value {
        json!({
            "timestamp": self.timestamp, "action_id": self.action_id, "action_type": self.action_type, "was_correct": self.was_correct, "confidence": self.confidence,
            "alternatives": self.alternatives, "chosen_path": self.chosen_path, "why_chosen": self.why_chosen, "mistakes": self.mistakes, "missing_information": self.missing_information,
            "lessons": self.lessons, "should_change_behavior": self.should_change_behavior, "suggested_policy_change": self.suggested_policy_change,
            "applied_policy_changes": self.applied_policy_changes, "self_state_after": self.self_state_after, "future_actions": self.future_actions,
        })
    }

    pub fn from_json(v: &Value) -> ReflectionResult {
        let strs = |k: &str| -> Vec<String> { v[k].as_array().map(|a| a.iter().map(|x| x.as_str().unwrap_or("").to_string()).collect()).unwrap_or_default() };
        ReflectionResult {
            timestamp: v["timestamp"].as_f64().unwrap_or(0.0),
            action_id: v["action_id"].as_str().unwrap_or("").to_string(),
            action_type: v["action_type"].as_str().unwrap_or("").to_string(),
            was_correct: v["was_correct"].as_bool().unwrap_or(false),
            confidence: v["confidence"].clone(),
            alternatives: strs("alternatives"),
            chosen_path: v["chosen_path"].clone(),
            why_chosen: v["why_chosen"].as_str().unwrap_or("").to_string(),
            mistakes: strs("mistakes"),
            missing_information: strs("missing_information"),
            lessons: strs("lessons"),
            should_change_behavior: v["should_change_behavior"].as_bool().unwrap_or(false),
            suggested_policy_change: v["suggested_policy_change"].as_str().map(String::from),
            applied_policy_changes: strs("applied_policy_changes"),
            self_state_after: v["self_state_after"].clone(),
            future_actions: strs("future_actions"),
        }
    }
}

/// Экземпляр цикла (состояние в памяти).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ReflectionLoop {
    pub reflection_count: usize,
    pub reflections: Vec<ReflectionResult>,
}

// ---------------------------------------------------------------- вспомогательное про `epistemic` (dict Python)

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

fn get<'a>(ep: &'a Value, k: &str) -> R<Option<&'a Value>> {
    if !ep.is_object() {
        return Err("AttributeError".into());
    }
    Ok(ep.get(k))
}

fn get_str(ep: &Value, k: &str, default: &str) -> R<String> {
    Ok(match get(ep, k)? {
        None => default.to_string(),
        Some(Value::String(s)) => s.clone(),
        // нестрока попадает в f-строку через str(); для JSON-значений это их Python-вид
        Some(other) => py_str(other),
    })
}

/// Python `str(x)` для значений JSON (нужно только для подстановки в f-строку): строка — как есть, остальное — `repr`.
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

pub(crate) fn py_str_pub(v: &Value) -> String {
    py_str(v)
}

/// Python `repr(x)` для значений JSON.
pub(crate) fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True".into() } else { "False".into() },
        Value::Number(n) if n.is_i64() || n.is_u64() => n.to_string(),
        Value::Number(n) => {
            let x = n.as_f64().unwrap_or(0.0);
            if x.fract() == 0.0 && x.abs() < 1e16 { format!("{x:.1}") } else { format!("{x}") }
        }
        Value::String(s) => yandi_rs::py_text::py_repr_str(s),
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!("{{{}}}", o.iter().map(|(k, x)| format!("{}: {}", yandi_rs::py_text::py_repr_str(k), py_repr(x))).collect::<Vec<_>>().join(", ")),
    }
}

/// Число из JSON как в сравнении Python (`bool` — 0/1); иное — `TypeError`.
fn num(v: &Value) -> R<f64> {
    match v {
        Value::Number(n) => Ok(n.as_f64().unwrap_or(0.0)),
        Value::Bool(b) => Ok(*b as i64 as f64),
        _ => Err("TypeError".into()),
    }
}

/// `x < порог` Python: нечисло — `TypeError`.
fn lt(x: &Value, threshold: f64) -> R<bool> {
    Ok(num(x)? < threshold)
}

fn get_num(ep: &Value, k: &str, default: f64) -> R<f64> {
    match get(ep, k)? {
        None => Ok(default),
        Some(v) => num(v),
    }
}

fn is_domain(ep: &Value, names: &[&str]) -> R<bool> {
    Ok(get(ep, "domain")?.and_then(|d| d.as_str()).map(|d| names.contains(&d)).unwrap_or(false))
}

fn testability_is(ep: &Value, name: &str) -> R<bool> {
    Ok(get(ep, "testability")?.and_then(|d| d.as_str()) == Some(name))
}

fn contains_ci(list: &[String], needle: &str) -> bool {
    list.iter().any(|m| yandi_rs::py_text::py_lower(m).contains(needle))
}

impl ReflectionLoop {
    /// Конструктор: создаёт модель себя при необходимости.
    pub fn new(cx: &Ctx) -> R<ReflectionLoop> {
        self_model::init(cx)?;
        Ok(ReflectionLoop::default())
    }

    pub fn to_json(&self) -> Value {
        json!({"reflection_count": self.reflection_count, "reflections": self.reflections.iter().map(|r| r.to_json()).collect::<Vec<_>>()})
    }

    pub fn from_json(v: &Value) -> ReflectionLoop {
        ReflectionLoop {
            reflection_count: v["reflection_count"].as_u64().unwrap_or(0) as usize,
            reflections: v["reflections"].as_array().map(|a| a.iter().map(ReflectionResult::from_json).collect()).unwrap_or_default(),
        }
    }

    /// Применить политику к планировщику; `true` — политика ПРИМЕНЕНА (active). Новая политика создаётся как «наблюдаемая»; активной становится на третьем повторении правила.
    fn apply_policy_to_planner(&self, cx: &Ctx, policy_type: &str, rule: &str, confidence: f64) -> R<bool> {
        let existing = cx.repo("find_reflection_policy_by_rule", json!({"rule": rule}))?;
        if existing.is_object() {
            let new_count = existing["observed_count"].as_i64().unwrap_or(0) + 1;
            let status = existing["status"].as_str().unwrap_or("");
            let activate = status != "active" && new_count >= MIN_OBSERVATIONS_TO_ACTIVATE;
            cx.repo("bump_reflection_policy_observed", json!({"policy_id": existing["policy_id"], "activate": activate, "activated_at": cx.now_value()}))?;
            return Ok(activate || status == "active");
        }
        let policy_id = format!("pol_{}", cut(&cx.uuid_hex(), 8));
        cx.repo("create_reflection_policy", json!({"policy_id": policy_id, "policy_type": policy_type, "rule": rule, "confidence": confidence, "created_at": cx.now_value()}))?;
        Ok(false)
    }

    /// Рефлексия над запросом и ответом.
    #[allow(clippy::too_many_arguments)]
    pub fn reflect_on_query(
        &mut self,
        cx: &Ctx,
        query: &Value,
        epistemic: &Value,
        trust: &Value,
        confidence: &Value,
        errors: &[String],
        validation_result: Option<&Value>,
        context: Option<&Value>,
    ) -> R<ReflectionResult> {
        self_model::increment_reflections(cx)?;
        self.reflection_count += 1;
        let action_id = format!("ref_{}", cut(&cx.uuid_hex(), 12));

        let was_correct = evaluate_response(epistemic, trust, confidence, errors)?;
        let alternatives = generate_alternatives(epistemic)?;
        let why_chosen = explain_choice(epistemic)?;
        let mistakes = identify_mistakes(epistemic, trust, confidence, errors)?;
        let missing = identify_missing(epistemic)?;
        let lessons = extract_lessons(&mistakes, was_correct, context, validation_result)?;
        let policy_change = if !mistakes.is_empty() { suggest_policy_change(&mistakes, epistemic)? } else { None };

        let mut applied_changes: Vec<String> = vec![];
        if let Some(change) = &policy_change {
            let conf = if mistakes.len() > 1 { 0.7 } else { 0.5 };
            if self.apply_policy_to_planner(cx, "behavioral", change, conf)? {
                applied_changes.push(change.clone());
            }
        }
        let future_actions = plan_future_actions(&mistakes, epistemic)?;

        let result = ReflectionResult {
            timestamp: cx.now_secs(),
            action_id: action_id.clone(),
            action_type: "query".into(),
            was_correct,
            confidence: confidence.clone(),
            alternatives,
            chosen_path: get(epistemic, "answer_mode")?.cloned().unwrap_or_else(|| json!("unknown")),
            why_chosen,
            mistakes: mistakes.clone(),
            missing_information: missing,
            lessons: lessons.clone(),
            should_change_behavior: !mistakes.is_empty() || lt(confidence, 0.5)?,
            suggested_policy_change: policy_change,
            applied_policy_changes: applied_changes.clone(),
            self_state_after: self_model::reflect(cx)?,
            future_actions,
        };
        self.reflections.push(result.clone());

        // эпизодическая память: уроки, затем ошибки. `query[:60]` вычисляется в тех же местах, что и в Python (нестрока → TypeError именно там)
        let q60 = || -> R<Value> { slice60(query) };
        for lesson in &lessons {
            memory_episodic::add_learning(cx, lesson, &json!({"query": q60()?, "epistemic": epistemic, "trust": trust, "confidence": confidence, "applied_changes": applied_changes}), 0.6)?;
        }
        for mistake in &mistakes {
            let ctx_v = match context {
                Some(c) if truthy(Some(c)) => c.clone(),
                _ => json!({}),
            };
            memory_episodic::add_error(cx, mistake, json!({"query": q60()?, "context": ctx_v, "applied_changes": applied_changes}), 0.7)?;
        }

        // модель себя
        if result.should_change_behavior {
            for lesson in lessons.iter().take(2) {
                self_model::add_learning(cx, lesson, &format!("reflection_{action_id}"), json!(0.7))?;
            }
            if !applied_changes.is_empty() {
                self_model::add_learning(cx, &format!("Применены изменения: {}", applied_changes.join(", ")), &format!("reflection_{action_id}"), json!(0.8))?;
            }
        }
        self_model::add_reflection(
            cx,
            &json!({"action_id": action_id, "query": q60()?, "was_correct": was_correct, "confidence": confidence, "mistakes": mistakes, "lessons": lessons, "applied_changes": applied_changes}),
        )?;
        Ok(result)
    }

    pub fn get_policies(&self, cx: &Ctx) -> R<Value> {
        cx.repo("list_all_reflection_policies", json!({}))
    }

    pub fn get_summary(&self, cx: &Ctx) -> R<Value> {
        let active_policy_count = self.get_policies(cx)?.as_array().map(|a| a.len()).unwrap_or(0);
        if self.reflections.is_empty() {
            return Ok(json!({
                "total_reflections": 0, "recent_reflections": 0, "avg_confidence": 0, "mistakes_rate": 0, "lessons_count": 0, "policy_changes_suggested": 0, "applied_policies": 0,
                "active_policies": active_policy_count,
            }));
        }
        let n = self.reflections.len();
        let recent = &self.reflections[n.saturating_sub(20)..];
        let len = recent.len() as f64;
        Ok(json!({
            "total_reflections": self.reflection_count,
            "recent_reflections": recent.len(),
            "avg_confidence": recent.iter().try_fold(0.0, |a, r| num(&r.confidence).map(|c| a + c))? / len,
            "mistakes_rate": recent.iter().filter(|r| !r.mistakes.is_empty()).count() as f64 / len,
            "lessons_count": recent.iter().map(|r| r.lessons.len()).sum::<usize>(),
            "policy_changes_suggested": self.reflections.iter().filter(|r| r.suggested_policy_change.as_deref().map(|s| !s.is_empty()).unwrap_or(false)).count(),
            "applied_policies": self.reflections.iter().filter(|r| !r.applied_policy_changes.is_empty()).count(),
            "active_policies": active_policy_count,
        }))
    }

    pub fn summary_text(&self, cx: &Ctx) -> R<String> {
        let st = self.get_summary(cx)?;
        let n = self.reflections.len();
        let recent = &self.reflections[n.saturating_sub(3)..];
        let mut lessons_text = String::new();
        for r in recent {
            for l in r.lessons.iter().take(2) {
                lessons_text += &format!("  - {l}\n");
            }
        }
        if lessons_text.is_empty() {
            lessons_text = "  нет\n".into();
        }
        let mut policies_text = String::new();
        let policies = self.get_policies(cx)?;
        for p in policies.as_array().map(|a| a.iter().take(3).collect::<Vec<_>>()).unwrap_or_default() {
            let conf = p.get("confidence").and_then(|c| c.as_f64()).unwrap_or(0.0);
            let rule = match p.get("rule") {
                Some(v) => py_str(v),
                None => "None".into(),
            };
            policies_text += &format!("  - {rule} (conf: {conf:.2})\n");
        }
        if policies_text.is_empty() {
            policies_text = "  нет\n".into();
        }
        let f = |k: &str| st[k].as_f64().unwrap_or(0.0);
        let i = |k: &str| py_str(&st[k]);
        Ok(format!(
            "\n=== РЕФЛЕКСИВНЫЙ ЦИКЛ V7 ===\nВсего рефлексий: {}\nСредняя уверенность: {:.2}\nДоля ошибок: {:.2}\nУроков: {}\nПредложений по политике: {}\nПрименённых политик: {}\nАктивных политик: {}\n\nПоследние уроки:\n{}\n\nАктивные политики:\n{}",
            i("total_reflections"), f("avg_confidence"), f("mistakes_rate"), i("lessons_count"), i("policy_changes_suggested"), i("applied_policies"), i("active_policies"), lessons_text, policies_text
        ))
    }
}

// ---------------------------------------------------------------- чистые эвристики

/// Был ли ответ правильным.
fn evaluate_response(ep: &Value, trust: &Value, confidence: &Value, errors: &[String]) -> R<bool> {
    if !errors.is_empty() {
        return Ok(false);
    }
    if trust.as_str() == Some("UNVERIFIED") && lt(confidence, 0.3)? {
        return Ok(false);
    }
    if testability_is(ep, "fully_testable")? && lt(confidence, 0.5)? {
        return Ok(false);
    }
    Ok(true)
}

/// Альтернативные варианты ответа (не более четырёх).
fn generate_alternatives(ep: &Value) -> R<Vec<String>> {
    let mut alt: Vec<String> = vec![];
    let domain = get_str(ep, "domain", "unknown")?;
    let testability = get_str(ep, "testability", "unknown")?;
    get_str(ep, "answer_mode", "unknown")?;
    if domain == "philosophical" {
        alt.push("factual — если бы были подтверждённые данные".into());
        alt.push("pluralistic_contextual — уже выбрано".into());
    } else if domain == "factual" {
        alt.push("pluralistic_contextual — если бы вопрос был интерпретативным".into());
        alt.push("qualified_factual — если бы была неопределённость".into());
    }
    if testability == "interpretive" {
        alt.push("exploratory — если бы данных было ещё меньше".into());
        alt.push("dialogic — если бы нужно было уточнить".into());
    } else if testability == "fully_testable" {
        alt.push("qualified_factual — если бы источники были спорными".into());
    }
    alt.truncate(4);
    Ok(alt)
}

/// Почему выбран этот путь.
fn explain_choice(ep: &Value) -> R<String> {
    let domain = get_str(ep, "domain", "unknown")?;
    let testability = get_str(ep, "testability", "unknown")?;
    let mode = get_str(ep, "answer_mode", "unknown")?;
    let reason_v = get(ep, "reason")?;
    if truthy(reason_v) {
        return Ok(format!("{mode} выбран потому что: {}", py_str(reason_v.unwrap())));
    }
    Ok(if testability == "interpretive" {
        format!("Выбран {mode}, потому что вопрос интерпретативный (domain={domain})")
    } else if testability == "fully_testable" {
        format!("Выбран {mode}, потому что вопрос проверяемый (domain={domain})")
    } else {
        format!("Выбран {mode} на основе эпистемической классификации")
    })
}

/// Ошибки в решении.
fn identify_mistakes(ep: &Value, trust: &Value, confidence: &Value, errors: &[String]) -> R<Vec<String>> {
    let mut m: Vec<String> = errors.to_vec();
    if trust.as_str() == Some("UNVERIFIED") && lt(confidence, 0.3)? {
        m.push("Низкая уверенность при отсутствии проверки".into());
    }
    if testability_is(ep, "fully_testable")? && lt(confidence, 0.5)? {
        m.push("Проверяемый вопрос получил низкую уверенность".into());
    }
    let domain = get(ep, "domain")?.and_then(|d| d.as_str());
    if domain == Some("philosophical") && truthy(get(ep, "should_use_web")?) {
        m.push("Философский вопрос использует web-поиск (избыточно)".into());
    }
    if domain == Some("factual") && get(ep, "should_use_web")? == Some(&Value::Bool(false)) {
        m.push("Фактологический вопрос без web-поиска (может быть недостаточно данных)".into());
    }
    Ok(m)
}

/// Какой информации не хватало.
fn identify_missing(ep: &Value) -> R<Vec<String>> {
    let mut m: Vec<String> = vec![];
    let domain = get(ep, "domain")?.and_then(|d| d.as_str());
    if domain == Some("factual") && get(ep, "should_use_web")? == Some(&Value::Bool(false)) {
        m.push("Возможно, нужны были дополнительные источники".into());
    }
    if truthy(get(ep, "need_clarification")?) {
        m.push("Требовалось уточнение запроса".into());
    }
    if testability_is(ep, "interpretive")? && get_num(ep, "evidence_count", 0.0)? < 2.0 {
        m.push("Мало источников для интерпретативного ответа".into());
    }
    Ok(m)
}

/// Уроки из опыта (не более трёх). Уроки о валидации — только если проверка реально выполнялась.
fn extract_lessons(mistakes: &[String], was_correct: bool, context: Option<&Value>, validation_result: Option<&Value>) -> R<Vec<String>> {
    let mut lessons: Vec<String> = vec![];
    let performed = match validation_result {
        Some(v) if truthy(Some(v)) => truthy(get(v, "performed")?),
        _ => false,
    };
    if performed {
        let v = validation_result.unwrap();
        let acc_v = get(v, "accepted")?.cloned().unwrap_or(json!(0));
        let rej_v = get(v, "rejected")?.cloned().unwrap_or(json!(0));
        let tot_v = get(v, "total")?.cloned().unwrap_or(json!(0));
        // Python: `==` между несравнимыми типами — просто False, а `>` — TypeError
        let eq = |x: &Value, y: &Value| -> bool {
            match (num(x), num(y)) {
                (Ok(a), Ok(b)) => a == b,
                _ => x == y,
            }
        };
        if num(&tot_v)? > 0.0 {
            if eq(&acc_v, &tot_v) && eq(&rej_v, &json!(0)) {
                lessons.push(format!("Все {} claims прошли валидацию. Стратегия эффективна для этого типа запросов.", py_str(&acc_v)));
            } else if num(&rej_v)? > 0.0 {
                lessons.push(format!("{} из {} claims не прошли валидацию. Стратегия требует доработки.", py_str(&rej_v), py_str(&tot_v)));
            } else if eq(&acc_v, &json!(0)) && eq(&rej_v, &json!(0)) {
                lessons.push("Валидация не дала результатов. Необходимо проверить качество источников.".into());
            }
        } else {
            lessons.push("Внешняя валидация была запущена, но подтверждённых результатов проверки claims нет.".into());
        }
    } else if was_correct {
        lessons.push("Решение было правильным — повторять стратегию".into());
    } else {
        lessons.push("Решение было ошибочным — пересмотреть стратегию".into());
    }
    for mistake in mistakes {
        let low = yandi_rs::py_text::py_lower(mistake);
        if low.contains("web") {
            lessons.push("Проверять необходимость web-поиска перед использованием".into());
        }
        if low.contains("уверенность") {
            lessons.push("Не давать ответы с низкой уверенностью без проверки".into());
        }
        if low.contains("интерпретативный") {
            lessons.push("Интерпретативные вопросы не должны ходить в web".into());
        }
    }
    if let Some(c) = context {
        if truthy(get(c, "entity_not_found")?) {
            lessons.push("Перед ответом нужно проверять существование сущности".into());
        }
    }
    lessons.truncate(3);
    Ok(lessons)
}

/// Предложение изменения политики.
fn suggest_policy_change(mistakes: &[String], ep: &Value) -> R<Option<String>> {
    if mistakes.is_empty() {
        return Ok(None);
    }
    if contains_ci(mistakes, "web") && is_domain(ep, &["philosophical", "interpretive"])? {
        return Ok(Some("Запретить web-поиск для interpretive/non_falsifiable вопросов".into()));
    }
    if contains_ci(mistakes, "уверенность") {
        return Ok(Some("Понижать trust для ответов с confidence < 0.4".into()));
    }
    if contains_ci(mistakes, "интерпретативный") {
        return Ok(Some("Для интерпретативных вопросов использовать pluralistic_contextural".into()));
    }
    Ok(None)
}

/// Будущие действия (не более трёх).
fn plan_future_actions(mistakes: &[String], ep: &Value) -> R<Vec<String>> {
    let mut a: Vec<String> = vec![];
    if mistakes.is_empty() {
        a.push("повторить текущую стратегию".into());
    } else {
        a.push("пересмотреть стратегию поиска".into());
    }
    if contains_ci(mistakes, "web") {
        a.push("проверить необходимость web-поиска перед использованием".into());
    }
    if is_domain(ep, &["philosophical", "interpretive"])? {
        a.push("для интерпретативных вопросов использовать pluralistic_contextual".into());
    }
    if testability_is(ep, "fully_testable")? && get_num(ep, "confidence", 0.0)? < 0.6 {
        a.push("увеличить количество источников для проверяемых вопросов".into());
    }
    a.truncate(3);
    Ok(a)
}
