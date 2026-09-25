//! Один ход личного чата — перенос `_respond_with_character` и обвязки из `pet/chat_local.py` + `shadow_*` из `agent/db/sql/shadow_write.py`.
//!
//! ОДИН ОПОЗНАННЫЙ ЛИЧНЫЙ ХОД → ОДНА запись `interaction_turn` → НОЛЬ ИЛИ БОЛЕЕ проверенных причинных/relationship-записей → ОДИН COMMIT (всё или ничего).
//! Две отдельные работы модели (распознавание событий и ответ) не видят друг друга; ответ никогда сам не пишет состояние отношений.
//! Чтения отказоустойчивы (сбой → «неизвестно», никогда не «пусто»); запись — одна короткая транзакция после всей работы модели.
use std::collections::HashSet;

use serde_json::{json, Value};

use crate::chat_prompts as cp;
use crate::commitment_verification::{classify_commitment, drop_if_reported, verify_direct_fulfilment, VerificationResult};
use crate::ctx::Ctx;
use crate::event_extraction::{extract_relational_events, to_intensity, IntensityResult, LlmCall};
use crate::fact_extraction::{extract_personal_facts, FactExtraction, Known};
use crate::{personal_facts as pf, personal_memory as pm, relationship_commitments as rc, relationship_memory as rm, relationship_state as rs, R};

/// Единственный человек этого канала (как `_RELATIONSHIP_USER_ID`).
pub const RELATIONSHIP_USER_ID: &str = "owner";
/// Ниже этой тяжести обида не стоит записи.
pub const INSULT_SEVERITY_THRESHOLD: f64 = 0.3;

/// Ответ шлюза на смысловой вызов: видимый ответ, признак годности и след попыток (для «какая модель/адаптер ответили»).
#[derive(Debug, Clone)]
pub struct Semantic {
    pub reply: String,
    pub reply_ok: bool,
    pub trace: Vec<Value>,
}

/// Три обращения к модели, которыми пользуется ход. Ядро не знает, какой именно движок отвечает.
pub struct Models<'a> {
    /// Структурные вызовы: распознавание событий и фактов.
    pub extract: LlmCall<'a>,
    /// Классификация обещания и проверка выполнения.
    pub verify: LlmCall<'a>,
    /// Ответ: `system` (сообщения-контекст, `None` пропущены как в Python — там они тоже передаются списком), `messages`, температура.
    pub semantic: &'a dyn Fn(&[Option<String>], &[Value], f64) -> R<Semantic>,
}

pub struct TurnInput<'a> {
    pub model: &'a str,
    pub messages: &'a [Value],
    pub temperature: f64,
    pub source_turn_id: Option<&'a str>,
    /// Проверенная сводка оркестратора — только в ответ, никогда в извлечение/память.
    pub verified: Option<&'a Value>,
    /// `metadata.character` из модели «Я» (None — недоступна).
    pub character: Option<&'a Value>,
}

#[derive(Debug, Clone)]
pub struct TurnOutput {
    pub reply: String,
    /// Записана ли транзакция хода (False: записывать было нечего или всё откатилось — ответ от записи не зависит).
    pub persisted: bool,
}

/// Python `round(x, 1)`.
fn round1(x: f64) -> f64 {
    format!("{x:.1}").parse::<f64>().unwrap_or(x)
}

fn no_such_table(e: &str) -> bool {
    e.to_lowercase().contains("no such table")
}

// ---------------------------------------------------------------- чтения (отказоустойчивые)

/// ОБЩЕЕ ЧТЕНИЕ обещаний для контекста: сбой (нет журнала) → None = НЕИЗВЕСТНО, а не «обещаний нет».
fn commitment_context(cx: &Ctx, user_id: &str, current_text: &str) -> Option<Value> {
    let focus = rc::resolve_commitment_focus(cx, user_id, current_text).ok()?;
    let statuses = rc::commitment_statuses(cx, user_id).ok()?;
    let reported: Vec<&Value> = statuses.iter().filter(|c| c["status"] == "reported_fulfilled").collect();
    let reported: Vec<&Value> = reported[reported.len().saturating_sub(2)..].to_vec();
    let target = &focus["commitment"];
    let focus_v = if target.is_object() && !target.as_object().unwrap().is_empty() {
        json!({"commitment_id": target["commitment_id"], "text": target["text"], "status": target["status"]})
    } else {
        Value::Null
    };
    Some(json!({
        "focus": focus_v,
        "basis": focus["basis"],
        "open_count": focus["open_count"],
        "candidates": focus["candidates"].as_array().map(|a| a.iter().map(|c| c["text"].clone()).collect::<Vec<_>>()).unwrap_or_default(),
        "reported": reported.iter().map(|c| c["text"].clone()).collect::<Vec<_>>(),
    }))
}

/// Сырые факты: какая открытая обида относится к ТЕКУЩЕМУ сообщению (по тексту и журналу, а не по одной тяжести). None — память прочесть не удалось.
pub fn relationship_context(cx: &Ctx, user_id: &str, current_text: &str) -> Option<Value> {
    let focus = rm::resolve_relationship_focus(cx, user_id, current_text).ok()?;
    let grievance = &focus["grievance"];
    let state = rs::get_state(cx, user_id).ok()?;
    let mut rounded = serde_json::Map::new();
    if let Some(o) = state.as_object() {
        for (k, v) in o {
            rounded.insert(k.clone(), json!(round1(v.as_f64().unwrap_or(0.0))));
        }
    }
    let grievance_v = if grievance.is_object() {
        let mut facts = rm::memory_facts(grievance);
        facts.as_object_mut().unwrap().insert("grievance_id".into(), grievance["id"].clone());
        facts
    } else {
        Value::Null
    };
    Some(json!({
        "available": true,
        "grievance": grievance_v,
        "focus_basis": focus["basis"],
        "relationship_state": Value::Object(rounded),
        "open_count": focus["open_count"],
        "candidates": focus["candidates"].as_array().map(|a| a.iter().map(rm::memory_facts).collect::<Vec<_>>()).unwrap_or_default(),
        "commitments": commitment_context(cx, user_id, current_text),
    }))
}

// ---------------------------------------------------------------- одна транзакция хода

/// Шаг транзакции хода. Ошибка уходит владельцу транзакции (откатывает всё); `optional_table` — хранилища может ещё не быть: тогда шаг пропускается.
fn step<T>(label: &str, optional_table: bool, f: impl FnOnce() -> R<T>) -> R<Option<T>> {
    match f() {
        Ok(v) => Ok(Some(v)),
        Err(e) if optional_table && no_such_table(&e) => {
            eprintln!("{label} skipped: its table does not exist (schema not applied)");
            Ok(None)
        }
        Err(e) => Err(e),
    }
}

/// События ТЕКУЩЕЙ реплики → состояние обид/прощения/обещаний. Извинение и оскорбление в одной реплике — два независимых события (у каждого свой отрезок): сначала извинение к
/// обиде, вокруг которой построен ответ, потом оскорбление регистрирует свою. Претензия на выполнение пишется как ОТЧЁТ против единственного обещания в фокусе.
fn apply_current_turn_event(cx: &Ctx, text: &str, intensity: &IntensityResult, memory_ctx: Option<&Value>, source_turn_id: Option<&str>, promise_kind: Option<&str>) -> R<()> {
    if !intensity.ok {
        return Ok(());
    }
    let span_of = |kind: &str| intensity.spans.iter().rev().find(|(k, _, _)| k == kind).map(|(_, s, e)| (*s as i64, *e as i64));
    if intensity.is_apology {
        if let Some(g) = cp::relationship_grievance(memory_ctx) {
            let gid = g["grievance_id"].as_str();
            step("apply_apology", false, || rm::apply_apology(cx, RELATIONSHIP_USER_ID, gid, intensity.sincerity, source_turn_id, span_of("apology")))?;
        }
    }
    if intensity.is_insult && intensity.severity >= INSULT_SEVERITY_THRESHOLD {
        step("add_grievance", false, || rm::add_grievance(cx, RELATIONSHIP_USER_ID, "insult", text, intensity.severity, None, source_turn_id, span_of("insult")))?;
    }
    if intensity.is_promise {
        step("create_commitment", true, || rc::create_commitment(cx, RELATIONSHIP_USER_ID, text, &intensity.evidence, None, promise_kind.unwrap_or(rc::KIND_GENERAL), source_turn_id, span_of("promise")))?;
    } else if intensity.claims_fulfilled {
        let target = memory_ctx.and_then(|c| c.get("commitments")).and_then(|c| c.get("focus")).filter(|f| f.is_object());
        if let Some(t) = target {
            let cid = t["commitment_id"].as_str();
            step("record_fulfillment_claim", true, || rc::record_fulfillment_claim(cx, RELATIONSHIP_USER_ID, cid, &intensity.evidence, source_turn_id, span_of("fulfilment_claim")))?;
        }
    }
    Ok(())
}

fn has_current_turn_event(i: &IntensityResult) -> bool {
    i.ok && (i.is_apology || i.is_insult || i.is_promise || i.claims_fulfilled)
}

/// (модель, адаптер) попытки, давшей ответ, по следу шлюза; иначе — запрошенное логическое имя.
pub fn generation_target(trace: &[Value], requested: &str) -> (String, Option<String>) {
    let cut = |s: &str, n: usize| -> String { s.chars().take(n).collect() };
    for attempt in trace.iter().rev() {
        if attempt.is_object() && attempt["result"] == "success" {
            let truthy_str = |k: &str| attempt.get(k).and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(String::from);
            let resolved = truthy_str("resolved_model").unwrap_or_else(|| requested.to_string());
            let adapter = truthy_str("adapter_id").or_else(|| truthy_str("runtime")).map(|s| cut(&s, 40)).filter(|s| !s.is_empty());
            return (cut(&resolved, 120), adapter);
        }
    }
    (cut(requested, 120), None)
}

/// Реплика годна как личность хода только если это простой токен из 8–64 безопасных символов; иначе — «нет личности» (неизвестная идемпотентность ≠ идемпотентная).
pub fn valid_turn_id(value: Option<&Value>) -> Option<String> {
    let s = value?.as_str()?;
    let n = s.chars().count();
    if (8..=64).contains(&n) && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-') {
        Some(s.to_string())
    } else {
        None
    }
}

/// Один ход. Синхронный: вся работа модели — до записи; запись — ОДНА транзакция (при сбое откатывается целиком, ответ от неё не зависит).
pub fn respond_with_character(cx: &Ctx, inp: &TurnInput, models: &Models) -> R<TurnOutput> {
    let mut last_user_text = String::new();
    for m in inp.messages.iter().rev() {
        let Some(o) = m.as_object() else {
            return Err("AttributeError".into());
        };
        if o.get("role").and_then(|r| r.as_str()) == Some("user") {
            match o.get("content") {
                None => {}
                Some(Value::String(s)) => last_user_text = s.clone(),
                Some(_) => return Err("TypeError".into()),
            }
            break;
        }
    }
    let stid = inp.source_turn_id.filter(|s| !s.is_empty());
    let memory_ctx = relationship_context(cx, RELATIONSHIP_USER_ID, &last_user_text);
    let in_context: Vec<String> = inp.messages.iter().filter(|m| m["role"] == "user").filter_map(|m| m["content"].as_str().map(String::from)).collect();
    // Личная память принадлежит собственному чату человека (клиент, выдающий id реплик): запрос без id ничего не читает и не пишет.
    let mut past: Option<Vec<Value>> = if stid.is_some() { pm::recall(cx, RELATIONSHIP_USER_ID, &last_user_text, stid, &in_context).ok() } else { None };
    // Факты человека читаются ДО извлечения: извлекатель может связать новое утверждение с известным фактом.
    let stored_facts: Option<Vec<Value>> = if stid.is_some() { pf::list_facts(cx, RELATIONSHIP_USER_ID).ok() } else { None };

    // Извлекатель событий получает только текущее сообщение.
    let extraction = extract_relational_events(&last_user_text, models.extract)?;
    let fact_extraction: Option<FactExtraction> = match (&stored_facts, stid) {
        (Some(sf), Some(_)) => {
            let known: Vec<Known> = pf::known_for_linking(sf, 30).iter().map(|k| Known { fact_id: k["fact_id"].as_str().unwrap_or("").to_string(), statement: k["statement"].as_str().unwrap_or("").to_string() }).collect();
            Some(extract_personal_facts(&last_user_text, models.extract, &known)?)
        }
        _ => None,
    };
    // Как ИЗВЛЕКАТЬ память: человек спрашивает, что о нём известно — получает профиль; неизвестный маршрут склоняется к выдаче. Никогда не запись.
    let profile = fact_extraction.as_ref().map(|f| f.memory_query != "none" || !f.query_known).unwrap_or(false);
    let person_facts: Option<Vec<Value>> = if stid.is_some() { Some(pf::select_for_prompt(stored_facts.as_deref().unwrap_or(&[]), &last_user_text, profile)) } else { None };
    if let (Some(p), Some(sf)) = (past.as_mut(), stored_facts.as_ref()) {
        if !p.is_empty() && !sf.is_empty() {
            // одно и то же не говорится дважды, а исправленное человеком не воскресает как «память разговора»
            let mut skip: HashSet<String> = person_facts.iter().flatten().map(|f| f["source_turn_id"].as_str().unwrap_or("").to_string()).collect();
            skip.extend(sf.iter().filter(|f| f["status"] == "superseded").map(|f| f["source_turn_id"].as_str().unwrap_or("").to_string()));
            p.retain(|m| !skip.contains(m["source_turn_id"].as_str().unwrap_or("")));
        }
    }
    // Обещание в этом сообщении классифицируется: выполнение появится В ЧАТЕ (единственное, что YANDI видит напрямую) или в мире; всё, кроме ясного in_chat, — внешнее.
    let intensity = to_intensity(&extraction);
    let promise_kind: Option<&'static str> = if stid.is_some() && intensity.ok && intensity.is_promise { Some(classify_commitment(&last_user_text, &intensity.evidence, models.verify)?) } else { None };
    // Выполнение, которое YANDI может УВИДЕТЬ: содержит ли ЭТО сообщение обещанный в-чатный результат ровно одного из прежних открытых обещаний. Доказательство — только текущее сообщение.
    let verifiable: Option<Vec<(String, String)>> = if stid.is_some() {
        rc::verifiable_commitments(cx, RELATIONSHIP_USER_ID, stid).ok().map(|v| v.iter().map(|c| (c["commitment_id"].as_str().unwrap_or("").to_string(), c["evidence"].as_str().unwrap_or("").to_string())).collect())
    } else {
        None
    };
    let verification: Option<VerificationResult> = match &verifiable {
        Some(cands) if !cands.is_empty() && extraction.answered => {
            let v = verify_direct_fulfilment(&last_user_text, models.verify, cands)?;
            Some(drop_if_reported(v, &extraction.judged))
        }
        _ => None,
    };
    // `verified` (проверенная сводка оркестратора) попадает ТОЛЬКО в ответ.
    let system: Vec<Option<String>> = vec![
        Some(cp::BASE_CHARACTER_PROMPT.to_string()),
        cp::self_knowledge_message(inp.character),
        Some(cp::interlocutor_relation_message()),
        cp::memory_context_message(memory_ctx.as_ref())?,
        cp::personal_facts_message(person_facts.as_ref().map(|v| json!(v)).as_ref())?,
        cp::past_conversation_message(past.as_ref().map(|v| json!(v)).as_ref())?,
        cp::verified_digest_message(inp.verified),
    ];
    let semantic = (models.semantic)(&system, inp.messages, inp.temperature)?;
    let visible = if semantic.reply_ok { semantic.reply.clone() } else { cp::failure_reply() };
    let reply = cp::clean_response(&visible);
    let (resolved_model, adapter) = generation_target(&semantic.trace, inp.model);

    // ВСЯ работа модели закончена и каждое событие проверено: только теперь ход пишет в SQL, в ОДНОЙ короткой транзакции.
    let has_event = has_current_turn_event(&intensity);
    let mut persisted = false;
    if stid.is_some() || has_event {
        cx.conn.execute_batch("BEGIN IMMEDIATE").map_err(|e| e.to_string())?;
        let unit = || -> R<()> {
            let proven = stid.is_some() && verification.as_ref().map(|v| v.verified.is_some()).unwrap_or(false);
            if proven {
                // ПЕРВЫЙ оператор транзакции, до чтений: награда за это доказательство зависит от прежних доказательств человека.
                step("lock_relationship_state", false, || cx.repo("get_or_create_inner_state", json!({"user_id": RELATIONSHIP_USER_ID, "updated_at": cx.now_value()})))?;
            }
            if let Some(id) = stid {
                let recalled: Vec<String> = past.iter().flatten().map(|m| m["source_turn_id"].as_str().unwrap_or("").to_string()).collect();
                step("record_interaction_turn", false, || {
                    pm::record_turn(cx, RELATIONSHIP_USER_ID, Some(id), &last_user_text, if semantic.reply_ok { Some(&reply) } else { None }, Some(&resolved_model), adapter.as_deref(), Some(&recalled))
                })
                .or_else(|e| if no_such_table(&e) { Ok(None) } else { Err(e) })?;
            }
            apply_current_turn_event(cx, &last_user_text, &intensity, memory_ctx.as_ref(), stid, promise_kind)?;
            if let (Some(_), Some(v)) = (stid, verification.as_ref().and_then(|v| v.verified.as_ref())) {
                step("record_direct_fulfilment", false, || rc::record_direct_fulfilment(cx, RELATIONSHIP_USER_ID, Some(&v.commitment_id), &v.evidence, stid, Some((v.start as i64, v.end as i64))))?;
            }
            if let (Some(_), Some(fx)) = (stid, fact_extraction.as_ref()) {
                if !fx.facts.is_empty() {
                    let facts: Vec<pf::ExtractedFact> = fx
                        .facts
                        .iter()
                        .map(|f| pf::ExtractedFact {
                            fact_class: f.fact_class.clone(),
                            statement: f.statement.clone(),
                            polarity: f.polarity.clone(),
                            temporality: f.temporality.clone(),
                            evidence: f.evidence.clone(),
                            start: Some(f.start as i64),
                            end: Some(f.end as i64),
                            relation: f.relation.clone(),
                            target_fact_id: f.target_fact_id.clone(),
                        })
                        .collect();
                    step("record_personal_facts", true, || pf::record_turn_facts(cx, RELATIONSHIP_USER_ID, stid, &facts))?;
                }
            }
            Ok(())
        };
        match unit() {
            Ok(()) => match cx.conn.execute_batch("COMMIT") {
                Ok(()) => persisted = true,
                Err(e) => {
                    let _ = cx.conn.execute_batch("ROLLBACK");
                    eprintln!("turn persistence rolled back, nothing was written: {e}");
                }
            },
            Err(e) => {
                let _ = cx.conn.execute_batch("ROLLBACK");
                eprintln!("turn persistence rolled back, nothing was written: {e}");
            }
        }
    }
    Ok(TurnOutput { reply, persisted })
}
