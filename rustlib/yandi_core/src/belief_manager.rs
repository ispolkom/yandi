//! Убеждения с байесовским обновлением — перенос `agent/belief_manager.py`. БЕЛИЕФ ≠ истина: изменяемое производное состояние (уверенность, доказательства «за» и «против», противоречивость,
//! затухание), а не факт; вся история изменений — отдельная только добавляемая таблица. SQL — единственный источник истины; ошибка базы уходит вызывающему (тихого запасного пути нет).
//!
//! Два внешних вызова вынесены наружу (модель выбирает вызывающий, а не зашитое имя): `embed` — векторы текстов для предварительного отбора похожих, `judge` — короткий вызов модели для
//! кандидатов, уже прошедших порог сходства 0.70. Сбой любого из них = «не удалось» (`None` / `""`), а не выдуманное сходство.
use serde_json::{json, Value};
use yandi_rs::py_json::{loads, PyJson};
use yandi_rs::py_text::{py_lower, py_split_whitespace, py_strip};

use crate::ctx::{dt_secs, Ctx};
use crate::R;

pub const SIMILARITY_THRESHOLD: f32 = 0.70;
pub const EMBED_TEXT_CHARS: usize = 2000;

/// Векторы текстов (как их вернул шлюз, до нормировки); `None` — недоступно.
pub type EmbedFn<'a> = &'a dyn Fn(&[String]) -> Option<Vec<Vec<f32>>>;
/// Один короткий вызов модели по тексту подсказки; ошибка — класс сбоя.
pub type JudgeLlm<'a> = &'a dyn Fn(&str) -> Result<String, String>;

#[derive(Debug, Clone, PartialEq)]
pub struct Belief {
    pub id: String,
    pub topic: String,
    pub statement: String,
    pub confidence: f64,
    pub evidence_for: Vec<String>,
    pub evidence_against: Vec<String>,
    pub claim_ids: Vec<String>,
    pub created_at: f64,
    pub updated_at: f64,
    pub status: String,
    pub prior: f64,
    pub likelihood: f64,
    pub contradiction_score: f64,
    pub decay_factor: f64,
    pub superseded_by: Option<String>,
}

impl Belief {
    pub fn to_json(&self) -> Value {
        json!({
            "id": self.id, "topic": self.topic, "statement": self.statement, "confidence": self.confidence,
            "evidence_for": self.evidence_for, "evidence_against": self.evidence_against, "claim_ids": self.claim_ids,
            "created_at": self.created_at, "updated_at": self.updated_at, "history": [], "status": self.status,
            "prior": self.prior, "likelihood": self.likelihood, "contradiction_score": self.contradiction_score,
            "decay_factor": self.decay_factor, "superseded_by": self.superseded_by,
        })
    }
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}

fn f(v: &Value, k: &str) -> f64 {
    v.get(k).and_then(|x| x.as_f64()).unwrap_or(0.0)
}

fn strs(v: &Value, k: &str) -> Vec<String> {
    v.get(k).and_then(|x| x.as_array()).map(|a| a.iter().map(|e| e.as_str().map(String::from).unwrap_or_else(|| e.to_string())).collect()).unwrap_or_default()
}

/// `_dt_to_unix`: время строки базы → секунды Unix (пусто → «сейчас»).
fn dt_to_unix(cx: &Ctx, v: &Value) -> f64 {
    match v {
        Value::Null => cx.now_secs(),
        Value::Number(n) => n.as_f64().unwrap_or(0.0),
        other => dt_secs(other).unwrap_or_else(|| cx.now_secs()),
    }
}

fn row_to_belief(cx: &Ctx, row: &Value) -> Belief {
    Belief {
        id: s(row, "belief_id"),
        topic: s(row, "topic"),
        statement: s(row, "statement"),
        confidence: f(row, "confidence"),
        evidence_for: strs(row, "evidence_for"),
        evidence_against: strs(row, "evidence_against"),
        claim_ids: strs(row, "claim_ids"),
        created_at: dt_to_unix(cx, &row["created_at"]),
        updated_at: dt_to_unix(cx, &row["updated_at"]),
        status: s(row, "status"),
        prior: f(row, "prior"),
        likelihood: f(row, "likelihood"),
        contradiction_score: f(row, "contradiction_score"),
        decay_factor: f(row, "decay_factor"),
        superseded_by: row.get("superseded_by").and_then(|x| x.as_str()).map(String::from),
    }
}

fn rows(v: Value) -> Vec<Value> {
    v.as_array().cloned().unwrap_or_default()
}

fn upsert(cx: &Ctx, b: &Belief, created_at: Value, updated_at: Value) -> R<()> {
    cx.repo(
        "upsert_belief",
        json!({
            "belief_id": b.id, "topic": b.topic, "statement": b.statement, "confidence": b.confidence, "status": b.status,
            "evidence_for": b.evidence_for, "evidence_against": b.evidence_against, "claim_ids": b.claim_ids,
            "prior": b.prior, "likelihood": b.likelihood, "contradiction_score": b.contradiction_score, "decay_factor": b.decay_factor,
            "superseded_by": b.superseded_by, "created_at": created_at, "updated_at": updated_at,
        }),
    )?;
    Ok(())
}

fn assess(cx: &Ctx, belief_id: &str, change_type: &str, old: Value, new: Value, reason: &str, created_at: f64) -> R<()> {
    cx.repo("record_belief_assessment", json!({"belief_id": belief_id, "change_type": change_type, "old_confidence": old, "new_confidence": new, "reason": reason, "created_at": created_at}))?;
    Ok(())
}

/// Затухание уверенности со временем: читает активные убеждения, считает `decay_factor ** возраст_в_днях` и пишет изменившиеся обратно за один проход (как при создании менеджера).
pub fn apply_decay(cx: &Ctx) -> R<()> {
    let now = cx.now_secs();
    let active = rows(cx.repo("list_active_beliefs", json!({}))?);
    for row in &active {
        let updated_at = dt_to_unix(cx, &row["updated_at"]);
        let age_days = (now - updated_at) / 86400.0;
        if age_days <= 1.0 {
            continue;
        }
        let decay = f(row, "decay_factor").powf(age_days);
        let old_conf = f(row, "confidence");
        let new_conf = old_conf * decay;
        let mut b = row_to_belief(cx, row);
        b.confidence = new_conf;
        upsert(cx, &b, row["created_at"].clone(), json!(now))?;
        assess(cx, &b.id, "decayed", json!(old_conf), json!(new_conf), &format!("decay: {:.1} days", age_days), now)?;
    }
    Ok(())
}

/// Байесовское обновление: `new_evidence` — сила свидетельства 0..1, `is_supporting` — за или против убеждения.
pub fn bayesian_update(belief: &Belief, new_evidence: f64, is_supporting: bool) -> R<f64> {
    let prior = belief.confidence;
    let likelihood = new_evidence;
    let (num, den) = if is_supporting {
        (prior * likelihood, prior * likelihood + (1.0 - prior) * (1.0 - likelihood))
    } else {
        (prior * (1.0 - likelihood), prior * (1.0 - likelihood) + (1.0 - prior) * likelihood)
    };
    if den == 0.0 {
        return Err("ZeroDivisionError".into());
    }
    Ok((num / den).min(0.99).max(0.01))
}

/// Доля доказательств «против», удвоенная и ограниченная единицей.
pub fn contradiction_score(belief: &Belief) -> f64 {
    let total = belief.evidence_for.len() + belief.evidence_against.len();
    if total == 0 {
        return 0.0;
    }
    (belief.evidence_against.len() as f64 / total as f64 * 2.0).min(1.0)
}

fn normalise(text: &str) -> String {
    py_split_whitespace(&py_lower(text)).collect::<Vec<_>>().join(" ")
}

/// Один пакетный вызов вложений на N текстов: тексты обрезаются до 2000 знаков, векторы нормируются (`float32`); любой сбой → `None`.
fn embed_batch(embed: EmbedFn, texts: &[String]) -> Option<Vec<Vec<f32>>> {
    let cut: Vec<String> = texts.iter().map(|t| t.chars().take(EMBED_TEXT_CHARS).collect()).collect();
    let vecs = embed(&cut)?;
    Some(
        vecs.into_iter()
            .map(|v| {
                let norm = v.iter().map(|x| x * x).sum::<f32>().sqrt();
                let norm = if norm == 0.0 { 1.0 } else { norm };
                v.iter().map(|x| x / norm).collect()
            })
            .collect(),
    )
}

fn dot(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b.iter()).map(|(x, y)| x * y).sum()
}

fn judge_prompt(a: &str, b: &str) -> String {
    format!(
        "\nТы определяешь отношение между двумя утверждениями.\n\nУТВЕРЖДЕНИЕ A:\n{a}\n\nУТВЕРЖДЕНИЕ B:\n{b}\n\nВыбери ровно одно:\n\nequivalent\n- утверждения выражают по существу одну и ту же мысль;\n- различия только в формулировке или несущественных деталях.\n\ncontradicts\n- утверждения несовместимы или говорят противоположное.\n\ndifferent\n- утверждения относятся к одной теме, но утверждают разные вещи.\n\nВАЖНО:\n- тематическая похожесть НЕ означает equivalent;\n- одинаковые слова НЕ означают equivalent;\n- отрицание необходимо учитывать;\n- не решай, какое утверждение истинно;\n- определи только отношение между ними.\n\nВерни ТОЛЬКО JSON:\n{{\"relation\":\"equivalent\"}}\n"
    )
}

/// Короткий судья отношения; ответ вида `{"relation": "equivalent"}`; любой сбой или иной вид ответа → `""`.
fn llm_judge_relation(judge: JudgeLlm, a: &str, b: &str) -> String {
    let Ok(raw) = judge(&judge_prompt(a, b)) else {
        return String::new();
    };
    let text = if raw.is_empty() { "{}".to_string() } else { raw };
    match loads(&text) {
        Ok(PyJson::Dict(items)) => items
            .iter()
            .rev()
            .find(|(k, _)| k == "relation")
            .map(|(_, v)| match v {
                PyJson::Str(x) => py_lower(py_strip(x)),
                _ => String::new(),
            })
            .unwrap_or_default(),
        _ => String::new(),
    }
}

/// Найти существующее убеждение, эквивалентное новому утверждению: сначала точное совпадение (без обращений к моделям), затем — один пакетный вызов вложений и по кандидатам судья.
fn find_similar(cx: &Ctx, topic: &str, statement: &str, embed: EmbedFn, judge: JudgeLlm) -> R<Option<Belief>> {
    if statement.is_empty() {
        return Ok(None);
    }
    let candidates = rows(cx.repo("list_beliefs_by_topic", json!({"topic": topic}))?);
    if candidates.is_empty() {
        return Ok(None);
    }
    let statement_norm = normalise(statement);
    for row in &candidates {
        if normalise(&s(row, "statement")) == statement_norm {
            return Ok(Some(row_to_belief(cx, row)));
        }
    }
    let mut texts = vec![statement.to_string()];
    texts.extend(candidates.iter().map(|c| s(c, "statement")));
    let Some(vectors) = embed_batch(embed, &texts) else {
        return Ok(None);
    };
    for (i, row) in candidates.iter().enumerate() {
        let (Some(v0), Some(vi)) = (vectors.first(), vectors.get(i + 1)) else {
            return Err("IndexError".into());
        };
        if dot(v0, vi) < SIMILARITY_THRESHOLD {
            continue;
        }
        if llm_judge_relation(judge, &s(row, "statement"), statement) == "equivalent" {
            return Ok(Some(row_to_belief(cx, row)));
        }
    }
    Ok(None)
}

/// Новое убеждение либо обновление найденного эквивалентного. `prior` (априорная) — только для нового.
#[allow(clippy::too_many_arguments)]
pub fn add_belief(cx: &Ctx, topic: &str, statement: &str, confidence: f64, evidence_for: &[String], evidence_against: &[String], claim_ids: &[String], prior: f64, embed: EmbedFn, judge: JudgeLlm) -> R<Belief> {
    if let Some(existing) = find_similar(cx, topic, statement, embed, judge)? {
        return update_existing(cx, existing, confidence, evidence_for, evidence_against);
    }
    let id = format!("bel_{}", cx.uuid_hex().chars().take(8).collect::<String>());
    let now = cx.now_secs();
    let b = Belief {
        id: id.clone(), topic: topic.into(), statement: statement.into(), confidence, evidence_for: evidence_for.to_vec(), evidence_against: evidence_against.to_vec(), claim_ids: claim_ids.to_vec(),
        created_at: now, updated_at: now, status: "active".into(), prior, likelihood: confidence, contradiction_score: 0.0, decay_factor: 0.95, superseded_by: None,
    };
    upsert(cx, &b, json!(now), json!(now))?;
    assess(cx, &id, "created", json!(0.0), json!(confidence), "initial", now)?;
    let row = cx.repo("get_belief", json!({"belief_id": id}))?;
    if !row.is_object() {
        return Err("TypeError".into());
    }
    Ok(row_to_belief(cx, &row))
}

fn update_existing(cx: &Ctx, mut belief: Belief, new_confidence: f64, new_for: &[String], new_against: &[String]) -> R<Belief> {
    let old_confidence = belief.confidence;
    let strength = new_confidence.min(0.95).max(0.05);
    let mut known_for: Vec<String> = belief.evidence_for.clone();
    let mut known_against: Vec<String> = belief.evidence_against.clone();
    for ev in new_for {
        if ev.is_empty() || known_for.contains(ev) || known_against.contains(ev) {
            continue;
        }
        belief.confidence = bayesian_update(&belief, strength, true)?;
        belief.evidence_for.push(ev.clone());
        known_for.push(ev.clone());
    }
    for ev in new_against {
        if ev.is_empty() || known_against.contains(ev) || known_for.contains(ev) {
            continue;
        }
        belief.confidence = bayesian_update(&belief, strength, false)?;
        belief.evidence_against.push(ev.clone());
        known_against.push(ev.clone());
    }
    belief.contradiction_score = contradiction_score(&belief);
    if belief.contradiction_score > 0.5 {
        belief.confidence *= 1.0 - belief.contradiction_score * 0.2;
    }
    belief.updated_at = cx.now_secs();
    upsert(cx, &belief, json!(belief.created_at), json!(belief.updated_at))?;
    assess(cx, &belief.id, "updated", json!(old_confidence), json!(belief.confidence), "bayesian_update", belief.updated_at)?;
    Ok(belief)
}

/// Оспорить убеждение контраргументом; уверенность ниже 0.3 → статус «пересмотрено». Нет такого убеждения → `None`.
pub fn challenge_belief(cx: &Ctx, belief_id: &str, counter_evidence: &str, new_confidence: f64, reason: &str) -> R<Option<Belief>> {
    let row = cx.repo("get_belief", json!({"belief_id": belief_id}))?;
    if !row.is_object() {
        return Ok(None);
    }
    let mut belief = row_to_belief(cx, &row);
    let old_confidence = belief.confidence;
    let strength = new_confidence.min(0.95).max(0.05);
    if !counter_evidence.is_empty() && !belief.evidence_against.iter().any(|e| e == counter_evidence) {
        belief.evidence_against.push(counter_evidence.to_string());
        belief.confidence = bayesian_update(&belief, strength, false)?;
    }
    belief.contradiction_score = contradiction_score(&belief);
    belief.updated_at = cx.now_secs();
    if belief.confidence < 0.3 {
        belief.status = "revised".into();
    }
    upsert(cx, &belief, json!(belief.created_at), json!(belief.updated_at))?;
    assess(cx, &belief.id, "revised", json!(old_confidence), json!(belief.confidence), &format!("challenged: {reason}"), belief.updated_at)?;
    Ok(Some(belief))
}

/// Заменить одно убеждение другим; нет любого из двух → `false`.
pub fn supersede_belief(cx: &Ctx, old_id: &str, new_id: &str) -> R<bool> {
    let old_row = cx.repo("get_belief", json!({"belief_id": old_id}))?;
    let new_row = cx.repo("get_belief", json!({"belief_id": new_id}))?;
    if !old_row.is_object() || !new_row.is_object() {
        return Ok(false);
    }
    let now = cx.now_secs();
    let mut b = row_to_belief(cx, &old_row);
    b.status = "superseded".into();
    b.superseded_by = Some(new_id.to_string());
    upsert(cx, &b, old_row["created_at"].clone(), json!(now))?;
    assess(cx, old_id, "superseded", Value::Null, Value::Null, &format!("superseded by {new_id}"), now)?;
    Ok(true)
}

pub fn get_beliefs_by_topic(cx: &Ctx, topic: &str) -> R<Vec<Belief>> {
    Ok(rows(cx.repo("list_beliefs_by_topic", json!({"topic": topic, "statuses": ["active"]}))?).iter().map(|r| row_to_belief(cx, r)).collect())
}

pub fn get_all_active(cx: &Ctx) -> R<Vec<Belief>> {
    Ok(rows(cx.repo("list_active_beliefs", json!({}))?).iter().map(|r| row_to_belief(cx, r)).collect())
}

pub fn get_belief(cx: &Ctx, belief_id: &str) -> R<Option<Belief>> {
    let row = cx.repo("get_belief", json!({"belief_id": belief_id}))?;
    Ok(if row.is_object() { Some(row_to_belief(cx, &row)) } else { None })
}

/// Настоящая, только добавляемая история (`belief_assessment_history`).
pub fn get_belief_history(cx: &Ctx, belief_id: &str) -> R<Value> {
    cx.repo("list_belief_history", json!({"belief_id": belief_id}))
}

pub fn get_all(cx: &Ctx) -> R<Vec<Belief>> {
    Ok(rows(cx.repo("list_all_beliefs", json!({}))?).iter().map(|r| row_to_belief(cx, r)).collect())
}

pub fn get_contradictory(cx: &Ctx, min_score: f64) -> R<Vec<Belief>> {
    Ok(rows(cx.repo("list_contradictory_beliefs", json!({"min_score": min_score}))?).iter().map(|r| row_to_belief(cx, r)).collect())
}

pub fn get_stats(cx: &Ctx) -> R<Value> {
    cx.repo("get_belief_stats", json!({}))
}

/// Python `str(x)` для чисел статистики (`float` — с «.0»).
fn py_str_num(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Number(n) if n.is_i64() || n.is_u64() => n.to_string(),
        Value::Number(n) => {
            let x = n.as_f64().unwrap_or(0.0);
            if x.fract() == 0.0 && x.abs() < 1e16 {
                format!("{x:.1}")
            } else {
                format!("{x}")
            }
        }
        Value::String(x) => x.clone(),
        other => other.to_string(),
    }
}

pub fn summary(cx: &Ctx) -> R<String> {
    let st = get_stats(cx)?;
    let cur = rows(cx.repo("list_active_beliefs", json!({}))?);
    let recent: Vec<Belief> = cur[cur.len().saturating_sub(3)..].iter().map(|r| row_to_belief(cx, r)).collect();
    let topics = st["topics"].as_object().map(|o| o.iter().map(|(k, v)| format!("{k}={}", py_str_num(v))).collect::<Vec<_>>().join(", ")).unwrap_or_default();
    let lines: Vec<String> = recent
        .iter()
        .map(|b| format!("  - [{:.2}] {} (за: {}, против: {}, конфликт: {:.2})", b.confidence, b.statement.chars().take(40).collect::<String>(), b.evidence_for.len(), b.evidence_against.len(), b.contradiction_score))
        .collect();
    Ok(format!(
        "\n=== BELIEF MANAGER V7 ===\nВсего: {} | Активных: {} | Пересмотренных: {} | Заменённых: {}\nПротиворечивых: {}\nСредняя уверенность: {}\nСредняя противоречивость: {}\nТемы: {}\n\nПоследние убеждения:\n{}\n",
        py_str_num(&st["total"]), py_str_num(&st["active"]), py_str_num(&st["revised"]), py_str_num(&st["superseded"]), py_str_num(&st["contradictory"]),
        py_str_num(&st["avg_confidence"]), py_str_num(&st["avg_contradiction"]), topics,
        if lines.is_empty() { "  нет".to_string() } else { lines.join("\n") },
    ))
}
