//! Мост для сверки с Python: собственная база в памяти + вызов функций ядра по имени (JSON → JSON). Не для боевого использования.
use std::collections::HashMap;
use std::sync::Mutex;

use pyo3::prelude::*;
use serde_json::{json, Value};

use crate::ctx::Ctx;
use crate::R;

struct Slot {
    db: yandi_db::Db,
    now: Option<f64>,
    ids: Vec<String>,
}

static DBS: Mutex<Option<HashMap<u64, Slot>>> = Mutex::new(None);
static NEXT: Mutex<u64> = Mutex::new(1);

fn err(e: impl ToString) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(e.to_string())
}

#[pyfunction]
fn open_memory() -> PyResult<u64> {
    let db = yandi_db::Db::open_in_memory().map_err(|e| err(e.0))?;
    let mut n = NEXT.lock().unwrap();
    let id = *n;
    *n += 1;
    DBS.lock().unwrap().get_or_insert_with(HashMap::new).insert(id, Slot { db, now: None, ids: vec![] });
    Ok(id)
}

#[pyfunction]
fn close(handle: u64) {
    if let Some(m) = DBS.lock().unwrap().as_mut() {
        m.remove(&handle);
    }
}

/// Часы и очередь идентификаторов для воспроизводимости.
#[pyfunction]
#[pyo3(signature = (handle, now, ids))]
fn set_clock(handle: u64, now: Option<f64>, ids: Vec<String>) -> PyResult<()> {
    let mut g = DBS.lock().unwrap();
    let s = g.as_mut().and_then(|m| m.get_mut(&handle)).ok_or_else(|| err("нет такой базы"))?;
    s.now = now;
    s.ids = ids;
    Ok(())
}

#[pyfunction]
fn query(handle: u64, sql: &str) -> PyResult<String> {
    let g = DBS.lock().unwrap();
    let s = g.as_ref().and_then(|m| m.get(&handle)).ok_or_else(|| err("нет такой базы"))?;
    let r = yandi_db::repo::rows_pub(s.db.conn(), sql).map_err(err)?;
    Ok(json!(r).to_string())
}

/// Вызов функции ядра или репозитория: `{"ok": …} | {"error": …}`.
#[pyfunction]
fn call(py: Python<'_>, handle: u64, name: &str, args_json: &str) -> PyResult<String> {
    let args: Value = serde_json::from_str(args_json).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let name = name.to_string();
    let out = py.allow_threads(move || {
        let mut g = DBS.lock().unwrap();
        let s = g.as_mut().and_then(|m| m.get_mut(&handle)).ok_or("нет такой базы".to_string())?;
        let mut cx = Ctx::new(s.db.conn());
        cx.fixed_now = s.now;
        let cx = cx.with_ids(std::mem::take(&mut s.ids));
        let r = dispatch(&cx, &name, &args);
        Ok::<_, String>(r)
    });
    Ok(match out {
        Ok(Ok(v)) => json!({"ok": v}),
        Ok(Err(e)) | Err(e) => json!({"error": e}),
    }
    .to_string())
}

fn a_str(a: &Value, k: &str) -> Option<String> {
    a.get(k).and_then(|v| v.as_str()).map(String::from)
}

fn a_f(a: &Value, k: &str) -> f64 {
    a.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0)
}

fn a_span(a: &Value) -> Option<(i64, i64)> {
    let s = a.get("span")?.as_array()?;
    Some((s.first()?.as_i64()?, s.get(1)?.as_i64()?))
}

fn dispatch(cx: &Ctx, name: &str, a: &Value) -> R<Value> {
    use crate::{causal_events as ce, personal_facts as pf, relationship_commitments as rc, relationship_memory as rm, relationship_state as rs};
    let uid = a_str(a, "user_id").unwrap_or_default();
    Ok(match name {
        "causal_claim" => json!(ce::claim(cx, &uid, a_str(a, "source_turn_id").as_deref(), &a_str(a, "event_type").unwrap_or_default(), a_span(a))?),
        "rs_get_state" => rs::get_state(cx, &uid)?,
        "rs_record_insult" => {
            rs::record_insult(cx, &uid, a_f(a, "severity"))?;
            Value::Null
        }
        "rs_record_accepted_apology" => {
            rs::record_accepted_apology(cx, &uid, a_f(a, "offense_severity"), a_f(a, "sincerity"))?;
            Value::Null
        }
        "rs_record_verified_commitment" => {
            rs::record_verified_commitment(cx, &uid, a.get("kept").and_then(|v| v.as_bool()).unwrap_or(false))?;
            Value::Null
        }
        "rs_record_observed_commitment" => json!(rs::record_observed_commitment(cx, &uid, a.get("prior_observed").and_then(|v| v.as_i64()).unwrap_or(0))?),
        "rs_observed_trust_reward" => json!(rs::observed_trust_reward(a.get("prior_observed").and_then(|v| v.as_i64()).unwrap_or(0))),
        "rs_replay" => rs::replay(cx, &uid)?,
        "rm_add_grievance" => json!(rm::add_grievance(cx, &uid, &a_str(a, "event_type").unwrap_or_default(), &a_str(a, "description").unwrap_or_default(), a_f(a, "severity"), a.get("context").filter(|c| !c.is_null()), a_str(a, "source_turn_id").as_deref(), a_span(a))?),
        "rm_acknowledge_apology" => json!(rm::acknowledge_apology(cx, &a_str(a, "grievance_id").unwrap_or_default(), a_f(a, "sincerity"))?),
        "rm_progress_healing" => json!(rm::progress_healing(cx, &a_str(a, "grievance_id").unwrap_or_default())?),
        "rm_get_summary" => rm::get_summary(cx, &uid)?,
        "rm_most_severe_active_grievance" => rm::most_severe_active_grievance(cx, &uid)?,
        "rm_resolve_relationship_focus" => rm::resolve_relationship_focus(cx, &uid, &a_str(a, "current_text").unwrap_or_default())?,
        "rm_apply_apology" => rm::apply_apology(cx, &uid, a_str(a, "grievance_id").as_deref(), a_f(a, "sincerity"), a_str(a, "source_turn_id").as_deref(), a_span(a))?,
        "rm_match_grievance_target" => {
            let list = |k: &str| a.get(k).and_then(|v| v.as_array()).cloned().unwrap_or_default();
            let m = rm::match_grievance_target(&a_str(a, "text").unwrap_or_default(), &list("active"), &list("resolved"), a_f(a, "now"));
            json!({"grievance": m.grievance, "basis": m.basis, "candidates": m.candidates})
        }
        "rc_commitment_statuses" => json!(rc::commitment_statuses(cx, &uid)?),
        "rc_create_commitment" => rc::create_commitment(cx, &uid, &a_str(a, "text").unwrap_or_default(), &a_str(a, "evidence").unwrap_or_default(), a.get("due_at"), &a_str(a, "kind").unwrap_or_else(|| "general".into()), a_str(a, "source_turn_id").as_deref(), a_span(a))?,
        "rc_resolve_commitment_focus" => rc::resolve_commitment_focus(cx, &uid, &a_str(a, "current_text").unwrap_or_default())?,
        "rc_verifiable_commitments" => json!(rc::verifiable_commitments(cx, &uid, a_str(a, "current_turn_id").as_deref())?),
        "rc_record_fulfillment_claim" => rc::record_fulfillment_claim(cx, &uid, a_str(a, "commitment_id").as_deref(), &a_str(a, "evidence").unwrap_or_default(), a_str(a, "source_turn_id").as_deref(), a_span(a))?,
        "rc_record_verification" => rc::record_verification(cx, &uid, a_str(a, "commitment_id").as_deref(), a.get("kept").and_then(|v| v.as_bool()).unwrap_or(false), &a_str(a, "source").unwrap_or_default(), a_str(a, "evidence").as_deref())?,
        "rc_record_direct_fulfilment" => rc::record_direct_fulfilment(cx, &uid, a_str(a, "commitment_id").as_deref(), &a_str(a, "evidence").unwrap_or_default(), a_str(a, "source_turn_id").as_deref(), a_span(a))?,
        "ee_extract" => {
            use crate::event_extraction as ee;
            use std::cell::{Cell, RefCell};
            let responses: Vec<String> = a.get("responses").and_then(|v| v.as_array()).map(|x| x.iter().map(|s| s.as_str().unwrap_or("").to_string()).collect()).unwrap_or_default();
            let idx = Cell::new(0usize);
            let prompts: RefCell<Vec<Value>> = RefCell::new(vec![]);
            let llm = |msgs: &[(String, String)]| -> Result<String, String> {
                prompts.borrow_mut().push(json!(msgs.iter().map(|(r, c)| json!({"role": r, "content": c})).collect::<Vec<_>>()));
                let i = idx.get();
                idx.set(i + 1);
                let r = responses.get(i).cloned().unwrap_or_default();
                match r.strip_prefix("__raise__:") {
                    Some(name) => Err(name.to_string()),
                    None => Ok(r),
                }
            };
            let res = ee::extract_relational_events(&a_str(a, "message").unwrap_or_default(), &llm)?;
            json!({"result": res.to_json(), "intensity": ee::to_intensity(&res).to_json(), "prompts": prompts.into_inner()})
        }
        "ee_to_intensity" => {
            use crate::event_extraction as ee;
            let events: Vec<ee::ExtractedEvent> = a.get("events").and_then(|v| v.as_array()).map(|x| x.iter().map(|e| ee::ExtractedEvent {
                kind: e["type"].as_str().unwrap_or("").to_string(), evidence: e["evidence"].as_str().unwrap_or("").to_string(),
                start: e["start"].as_u64().unwrap_or(0) as usize, end: e["end"].as_u64().unwrap_or(0) as usize,
                severity: e["severity"].as_f64().unwrap_or(0.0), sincerity: e["sincerity"].as_f64().unwrap_or(0.0)}).collect()).unwrap_or_default();
            ee::to_intensity(&ee::ExtractionResult { events, ..Default::default() }).to_json()
        }
        "fe_extract" => {
            use crate::fact_extraction as fe;
            use std::cell::{Cell, RefCell};
            let responses: Vec<String> = a.get("responses").and_then(|v| v.as_array()).map(|x| x.iter().map(|s| s.as_str().unwrap_or("").to_string()).collect()).unwrap_or_default();
            let known: Vec<fe::Known> = a.get("known").and_then(|v| v.as_array()).map(|x| x.iter().map(|k| fe::Known { fact_id: k["fact_id"].as_str().unwrap_or("").to_string(), statement: k["statement"].as_str().unwrap_or("").to_string() }).collect()).unwrap_or_default();
            let idx = Cell::new(0usize);
            let prompts: RefCell<Vec<Value>> = RefCell::new(vec![]);
            let llm = |msgs: &[(String, String)]| -> Result<String, String> {
                prompts.borrow_mut().push(json!(msgs.iter().map(|(r, c)| json!({"role": r, "content": c})).collect::<Vec<_>>()));
                let i = idx.get();
                idx.set(i + 1);
                let r = responses.get(i).cloned().unwrap_or_default();
                match r.strip_prefix("__raise__:") {
                    Some(name) => Err(name.to_string()),
                    None => Ok(r),
                }
            };
            let res = fe::extract_personal_facts(&a_str(a, "message").unwrap_or_default(), &llm, &known)?;
            json!({"result": res.to_json(), "prompts": prompts.into_inner()})
        }
        "cv_run" => {
            use crate::commitment_verification as cv;
            use std::cell::{Cell, RefCell};
            let responses: Vec<String> = a.get("responses").and_then(|v| v.as_array()).map(|x| x.iter().map(|s| s.as_str().unwrap_or("").to_string()).collect()).unwrap_or_default();
            let idx = Cell::new(0usize);
            let prompts: RefCell<Vec<Value>> = RefCell::new(vec![]);
            let llm = |msgs: &[(String, String)]| -> Result<String, String> {
                prompts.borrow_mut().push(json!(msgs.iter().map(|(r, c)| json!({"role": r, "content": c})).collect::<Vec<_>>()));
                let i = idx.get();
                idx.set(i + 1);
                let r = responses.get(i).cloned().unwrap_or_default();
                match r.strip_prefix("__raise__:") {
                    Some(name) => Err(name.to_string()),
                    None => Ok(r),
                }
            };
            let message = a_str(a, "message").unwrap_or_default();
            let out = match a_str(a, "op").as_deref() {
                Some("classify") => json!(cv::classify_commitment(&message, &a_str(a, "evidence").unwrap_or_default(), &llm)?),
                _ => {
                    let cands: Vec<(String, String)> = a.get("candidates").and_then(|v| v.as_array()).map(|x| x.iter().map(|c| (c["commitment_id"].as_str().unwrap_or("").to_string(), c["evidence"].as_str().unwrap_or("").to_string())).collect()).unwrap_or_default();
                    let mut res = cv::verify_direct_fulfilment(&message, &llm, &cands)?;
                    let spans: Vec<(String, usize, usize)> = a.get("event_spans").and_then(|v| v.as_array()).map(|x| x.iter().map(|s| (s[0].as_str().unwrap_or("").to_string(), s[1].as_u64().unwrap_or(0) as usize, s[2].as_u64().unwrap_or(0) as usize)).collect()).unwrap_or_default();
                    if a.get("drop").and_then(|v| v.as_bool()).unwrap_or(false) {
                        res = cv::drop_if_reported(res, &spans);
                    }
                    res.to_json()
                }
            };
            json!({"out": out, "prompts": prompts.into_inner()})
        }
        "cp_call" => {
            use crate::chat_prompts as cp;
            let v = a.get("value");
            match a_str(a, "fn").as_deref().unwrap_or("") {
                "memory_context" => json!(cp::memory_context_message(v.filter(|x| !x.is_null()))?),
                "past" => json!(cp::past_conversation_message(v)?),
                "facts" => json!(cp::personal_facts_message(v)?),
                "verified" => json!(cp::verified_digest_message(v)),
                "self" => json!(cp::self_knowledge_message(v.filter(|x| !x.is_null()))),
                "relation" => json!(cp::interlocutor_relation_message()),
                "quote" => json!(cp::memory_quote(v.and_then(|x| x.as_str()).unwrap_or(""))),
                "clean" => json!(cp::clean_response(v.and_then(|x| x.as_str()).unwrap_or(""))),
                "dedup" => json!(cp::dedup_paragraphs(v.and_then(|x| x.as_str()).unwrap_or(""))),
                "constants" => json!({"prompt": cp::BASE_CHARACTER_PROMPT, "stop": cp::stop_tokens(), "failure": cp::failure_reply()}),
                other => return Err(format!("нет функции {other}")),
            }
        }
        "pf_list_facts" => json!(pf::list_facts(cx, &uid)?),
        "pf_record_turn_facts" => {
            let facts: Vec<pf::ExtractedFact> = a.get("facts").and_then(|v| v.as_array()).map(|x| x.iter().map(pf::ExtractedFact::from_json).collect()).unwrap_or_default();
            pf::record_turn_facts(cx, &uid, a_str(a, "source_turn_id").as_deref(), &facts)?
        }
        "pf_known_for_linking" => {
            let folded = pf::list_facts(cx, &uid)?;
            json!(pf::known_for_linking(&folded, a.get("limit").and_then(|v| v.as_u64()).unwrap_or(30) as usize))
        }
        "pf_select_for_prompt" => {
            let folded = pf::list_facts(cx, &uid)?;
            json!(pf::select_for_prompt(&folded, &a_str(a, "current_text").unwrap_or_default(), a.get("profile").and_then(|v| v.as_bool()).unwrap_or(false)))
        }
        "pf_fold" => {
            let l = |k: &str| a.get(k).and_then(|v| v.as_array()).cloned().unwrap_or_default();
            json!(pf::fold(&l("facts"), &l("events")))
        }
        "query_exec" => {
            cx.conn.execute_batch(&a_str(a, "sql").unwrap_or_default()).map_err(|e| e.to_string())?;
            Value::Null
        }
        "pm_recall" => {
            let in_ctx: Vec<String> = a.get("in_context_texts").and_then(|v| v.as_array()).map(|x| x.iter().map(|t| t.as_str().unwrap_or("").to_string()).collect()).unwrap_or_default();
            json!(crate::personal_memory::recall(cx, &uid, &a_str(a, "current_text").unwrap_or_default(), a_str(a, "current_turn_id").as_deref(), &in_ctx)?)
        }
        "ct_relationship_context" => json!(crate::chat_turn::relationship_context(cx, &uid, &a_str(a, "current_text").unwrap_or_default())),
        "ct_turn" => {
            use crate::chat_turn as ct;
            use std::cell::RefCell;
            let queue: RefCell<Vec<String>> = RefCell::new(a.get("responses").and_then(|v| v.as_array()).map(|x| x.iter().map(|s| s.as_str().unwrap_or("").to_string()).collect()).unwrap_or_default());
            let prompts: RefCell<Vec<Value>> = RefCell::new(vec![]);
            let llm = |msgs: &[(String, String)]| -> Result<String, String> {
                prompts.borrow_mut().push(json!(msgs.iter().map(|(r, c)| json!({"role": r, "content": c})).collect::<Vec<_>>()));
                let r = if queue.borrow().is_empty() { String::new() } else { queue.borrow_mut().remove(0) };
                match r.strip_prefix("__raise__:") {
                    Some(name) => Err(name.to_string()),
                    None => Ok(r),
                }
            };
            let sem = a.get("semantic").cloned().unwrap_or(Value::Null);
            let sem_calls: RefCell<Vec<Value>> = RefCell::new(vec![]);
            let semantic = |system: &[Option<String>], messages: &[Value], temperature: f64| -> Result<ct::Semantic, String> {
                sem_calls.borrow_mut().push(json!({"system": system, "messages": messages, "temperature": temperature}));
                if sem.get("raise").and_then(|v| v.as_bool()).unwrap_or(false) {
                    return Err("SemanticError".into());
                }
                Ok(ct::Semantic { reply: sem["reply"].as_str().unwrap_or("").to_string(), reply_ok: sem.get("reply_ok").and_then(|v| v.as_bool()).unwrap_or(true), trace: sem.get("trace").and_then(|v| v.as_array()).cloned().unwrap_or_default() })
            };
            let models = ct::Models { extract: &llm, verify: &llm, semantic: &semantic };
            let messages: Vec<Value> = a.get("messages").and_then(|v| v.as_array()).cloned().unwrap_or_default();
            let stid = ct::valid_turn_id(a.get("turn_id"));
            let character = a.get("character").filter(|v| !v.is_null()).cloned();
            let verified = a.get("verified").filter(|v| !v.is_null()).cloned();
            let inp = ct::TurnInput { model: &a_str(a, "model").unwrap_or_default(), messages: &messages, temperature: a_f(a, "temperature"), source_turn_id: stid.as_deref(), verified: verified.as_ref(), character: character.as_ref() };
            let out = ct::respond_with_character(cx, &inp, &models)?;
            json!({"reply": out.reply, "persisted": out.persisted, "prompts": prompts.into_inner(), "semantic": sem_calls.into_inner(), "ids_left": cx.ids_left()})
        }
        name if name.starts_with("sm_") => {
            use crate::self_model as sm;
            let st = |k: &str| a_str(a, k).unwrap_or_default();
            let lim = |d: i64| a.get("limit").and_then(|v| v.as_i64()).unwrap_or(d);
            let field = |k: &str| -> R<Value> { sm::row(cx)?.get(k).cloned().ok_or_else(|| "KeyError".to_string()) };
            match &name[3..] {
                "init" => {
                    sm::init(cx)?;
                    Value::Null
                }
                "declare_character_trait" => sm::declare_character_trait(cx, a.get("traits").and_then(|v| v.as_object()).ok_or("TypeError")?)?,
                "add_event" => json!(sm::add_event(cx, &st("event_type"), &st("description"), a.get("details").cloned().unwrap_or(Value::Null), a.get("importance").cloned().unwrap_or(json!(0.5)))?),
                "add_decision" => {
                    sm::add_decision(cx, a.get("decision").ok_or("KeyError")?)?;
                    Value::Null
                }
                "add_learning" => {
                    sm::add_learning(cx, &st("lesson"), &st("context"), a.get("importance").cloned().unwrap_or(json!(0.6)))?;
                    Value::Null
                }
                "add_reflection" => {
                    sm::add_reflection(cx, a.get("reflection").ok_or("KeyError")?)?;
                    Value::Null
                }
                "add_error" => {
                    sm::add_error(cx, &st("error"), a.get("context").unwrap_or(&Value::Null), a.get("severity").cloned().unwrap_or(json!(0.7)))?;
                    Value::Null
                }
                "add_belief_update" => {
                    sm::add_belief_update(cx, &st("topic"), a.get("old_confidence").cloned().unwrap_or(Value::Null), a.get("new_confidence").cloned().unwrap_or(Value::Null), &st("reason"))?;
                    Value::Null
                }
                "add_change" => {
                    sm::add_change(cx, &st("what_changed"), a.get("before").unwrap_or(&Value::Null), a.get("after").unwrap_or(&Value::Null), &st("reason"))?;
                    Value::Null
                }
                "add_capability" => {
                    sm::add_capability(cx, &st("capability"))?;
                    Value::Null
                }
                "add_limitation" => {
                    sm::add_limitation(cx, &st("limitation"))?;
                    Value::Null
                }
                "add_uncertainty" => {
                    sm::add_uncertainty(cx, &st("uncertainty"))?;
                    Value::Null
                }
                "remove_uncertainty" => {
                    sm::remove_uncertainty(cx, &st("uncertainty"))?;
                    Value::Null
                }
                "increment_cycle" => {
                    sm::increment_cycle(cx)?;
                    Value::Null
                }
                "increment_queries" => {
                    sm::increment_queries(cx)?;
                    Value::Null
                }
                "increment_errors" => {
                    sm::increment_errors(cx)?;
                    Value::Null
                }
                "increment_reflections" => {
                    sm::increment_reflections(cx)?;
                    Value::Null
                }
                "get_identity" => field("identity")?,
                "get_age" => field("total_cycles")?,
                "get_metadata" => sm::get_metadata(cx)?,
                "set_metadata_value" => {
                    sm::set_metadata_value(cx, &st("key"), a.get("value").cloned().unwrap_or(Value::Null))?;
                    Value::Null
                }
                "get_goals" => sm::get_goals(cx)?,
                "get_capabilities" => field("capabilities")?,
                "get_limitations" => field("limitations")?,
                "get_uncertainties" => field("current_uncertainties")?,
                "get_recent_decisions" => sm::events_details(cx, "decision", lim(10))?,
                "get_lessons" => sm::events_details(cx, "learning", lim(10))?,
                "get_belief_history" => sm::events_details(cx, "belief_update", lim(10))?,
                "set_goals" => {
                    sm::set_goals(cx, a.get("goals").cloned().unwrap_or(Value::Null))?;
                    Value::Null
                }
                "add_goal" => {
                    sm::add_goal(cx, &st("goal"))?;
                    Value::Null
                }
                "reflect" => sm::reflect(cx)?,
                "check_health" => sm::check_health(cx)?,
                "get_timeline" => sm::get_timeline(cx, lim(20))?,
                "summary" => json!(sm::summary(cx)?),
                "repr" => json!(sm::repr(cx)?),
                other => return Err(format!("нет метода {other}")),
            }
        }
        name if name.starts_with("bm_") => {
            use crate::belief_manager as bm;
            use std::cell::RefCell;
            let strs = |k: &str| -> Vec<String> { a.get(k).and_then(|v| v.as_array()).map(|x| x.iter().map(|e| e.as_str().unwrap_or("").to_string()).collect()).unwrap_or_default() };
            let st = |k: &str| a_str(a, k).unwrap_or_default();
            // подмена шлюза: «вложения» из заданной карты «текст → вектор» (нет карты → недоступно), «судья» — сценарий ответов
            let embed_map: Option<serde_json::Map<String, Value>> = a.get("embed_map").and_then(|v| v.as_object()).cloned();
            let embed_calls: RefCell<Vec<Value>> = RefCell::new(vec![]);
            let embed = |texts: &[String]| -> Option<Vec<Vec<f32>>> {
                embed_calls.borrow_mut().push(json!(texts));
                let m = embed_map.as_ref()?;
                Some(texts.iter().map(|t| m.get(t).and_then(|v| v.as_array()).map(|x| x.iter().map(|e| e.as_f64().unwrap_or(0.0) as f32).collect()).unwrap_or_else(|| vec![1.0, 0.0, 0.0])).collect())
            };
            let queue: RefCell<Vec<String>> = RefCell::new(strs("judge_responses"));
            let prompts: RefCell<Vec<Value>> = RefCell::new(vec![]);
            let judge = |prompt: &str| -> Result<String, String> {
                prompts.borrow_mut().push(json!(prompt));
                let r = if queue.borrow().is_empty() { String::new() } else { queue.borrow_mut().remove(0) };
                match r.strip_prefix("__raise__:") {
                    Some(name) => Err(name.to_string()),
                    None => Ok(r),
                }
            };
            let out = match &name[3..] {
                "apply_decay" => {
                    bm::apply_decay(cx)?;
                    Value::Null
                }
                "add_belief" => {
                    let b = bm::add_belief(cx, &st("topic"), &st("statement"), a_f(a, "confidence"), &strs("evidence_for"), &strs("evidence_against"), &strs("claim_ids"), a.get("prior").and_then(|v| v.as_f64()).unwrap_or(0.5), &embed, &judge)?;
                    b.to_json()
                }
                "challenge_belief" => match bm::challenge_belief(cx, &st("belief_id"), &st("counter_evidence"), a_f(a, "new_confidence"), &st("reason"))? {
                    Some(b) => b.to_json(),
                    None => Value::Null,
                },
                "supersede_belief" => json!(bm::supersede_belief(cx, &st("old_belief_id"), &st("new_belief_id"))?),
                "get_beliefs_by_topic" => json!(bm::get_beliefs_by_topic(cx, &st("topic"))?.iter().map(|b| b.to_json()).collect::<Vec<_>>()),
                "get_all_active" => json!(bm::get_all_active(cx)?.iter().map(|b| b.to_json()).collect::<Vec<_>>()),
                "get_all" => json!(bm::get_all(cx)?.iter().map(|b| b.to_json()).collect::<Vec<_>>()),
                "get_belief" => match bm::get_belief(cx, &st("belief_id"))? {
                    Some(b) => b.to_json(),
                    None => Value::Null,
                },
                "get_belief_history" => bm::get_belief_history(cx, &st("belief_id"))?,
                "get_contradictory" => json!(bm::get_contradictory(cx, a.get("min_score").and_then(|v| v.as_f64()).unwrap_or(0.5))?.iter().map(|b| b.to_json()).collect::<Vec<_>>()),
                "get_stats" => bm::get_stats(cx)?,
                "summary" => json!(bm::summary(cx)?),
                other => return Err(format!("нет метода {other}")),
            };
            json!({"out": out, "embed_calls": embed_calls.into_inner(), "prompts": prompts.into_inner()})
        }
        name if name.starts_with("me_") => {
            use crate::memory_episodic as me;
            let st = |k: &str| a_str(a, k).unwrap_or_default();
            let f = |k: &str, d: f64| a.get(k).and_then(|v| v.as_f64()).unwrap_or(d);
            let lim = |d: i64| a.get("limit").and_then(|v| v.as_i64()).unwrap_or(d);
            let eps = |v: Vec<me::Episode>| json!(v.iter().map(|e| e.to_json()).collect::<Vec<_>>());
            match &name[3..] {
                "add" => {
                    let tags: Vec<String> = a.get("tags").and_then(|v| v.as_array()).map(|x| x.iter().map(|t| t.as_str().unwrap_or("").to_string()).collect()).unwrap_or_default();
                    json!(me::add(cx, &st("event_type"), &st("summary"), a.get("details").cloned().unwrap_or(Value::Null), f("importance", 0.5), &tags)?)
                }
                "add_query" => json!(me::add_query(cx, &st("query"), &st("domain"), &st("answer_mode"), &st("trust"), f("confidence", 0.0))?),
                "add_decision" => json!(me::add_decision(cx, &st("decision_type"), &st("reason"), a.get("details").cloned().unwrap_or(Value::Null), f("importance", 0.5))?),
                "add_error" => json!(me::add_error(cx, &st("error"), a.get("context").cloned().unwrap_or(Value::Null), f("severity", 0.7))?),
                "add_reflection" => json!(me::add_reflection(cx, a.get("reflection").ok_or("KeyError")?)?),
                "add_learning" => json!(me::add_learning(cx, &st("lesson"), a.get("context").ok_or("KeyError")?, f("importance", 0.6))?),
                "get_by_type" => eps(me::get_by_type(cx, &st("event_type"), lim(20))?),
                "get_by_tag" => eps(me::get_by_tag(cx, &st("tag"), lim(20))?),
                "get_by_importance" => eps(me::get_by_importance(cx, f("min_importance", 0.7), lim(20))?),
                "get_recent" => eps(me::get_recent(cx, lim(20))?),
                "get_timeline" => json!(me::get_timeline(cx, lim(50))?),
                "get_stats" => me::get_stats(cx)?,
                "summary" => json!(me::summary(cx)?),
                other => return Err(format!("нет метода {other}")),
            }
        }
        // состояние мотивации проходит через вызовы явно (в Python оно живёт в экземпляре): {"state": …|null, "method": …, "args": …}
        "mo_call" => {
            use crate::motivation as mo;
            let mut sys = match a.get("state").filter(|v| !v.is_null()) {
                None => mo::MotivationSystem::load(cx)?,
                Some(s) => mo::MotivationSystem { m: mo::Motivation::from_state(s) },
            };
            let args = a.get("args").cloned().unwrap_or(json!({}));
            let f = |k: &str| args.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0);
            let st = |k: &str| args.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let out = match a_str(a, "method").as_deref().unwrap_or("") {
                "init" => Value::Null,
                "set" => {
                    sys.set(cx, &st("which"), f("value"))?;
                    Value::Null
                }
                "should_explore" => json!(sys.should_explore(args.get("confidence").and_then(|v| v.as_f64()).unwrap_or(0.5), args.get("uncertainty").and_then(|v| v.as_f64()).unwrap_or(0.5))),
                "should_verify" => json!(sys.should_verify(&st("trust"), f("confidence"))),
                "should_ask_clarification" => json!(sys.should_ask_clarification(f("uncertainty"))),
                "should_use_web" => json!(sys.should_use_web(&st("testability"), f("confidence"))),
                "get_answer_mode_preference" => json!(sys.get_answer_mode_preference(&st("domain"), &st("testability"))),
                "update_from_experience" => {
                    sys.update_from_experience(cx, args.get("result").ok_or("KeyError")?)?;
                    Value::Null
                }
                "get_summary" => sys.get_summary(),
                "summary_text" => json!(sys.summary_text()),
                other => return Err(format!("нет метода {other}")),
            };
            json!({"out": out, "state": sys.m.to_json()})
        }
        // рефлексивный цикл: состояние экземпляра (счётчик и список рефлексий) проходит через вызовы явно
        "rl_call" => {
            use crate::reflection_loop as rl;
            let mut lp = match a.get("state").filter(|v| !v.is_null()) {
                None => rl::ReflectionLoop::new(cx)?,
                Some(s) => rl::ReflectionLoop::from_json(s),
            };
            let args = a.get("args").cloned().unwrap_or(json!({}));
            let out = match a_str(a, "method").as_deref().unwrap_or("") {
                "init" => Value::Null,
                "reflect_on_query" => {
                    let errors: Vec<String> = args.get("errors").and_then(|v| v.as_array()).map(|x| x.iter().map(|e| e.as_str().unwrap_or("").to_string()).collect()).unwrap_or_default();
                    let r = lp.reflect_on_query(
                        cx,
                        args.get("query").unwrap_or(&Value::Null),
                        args.get("epistemic").ok_or("KeyError")?,
                        args.get("trust").unwrap_or(&Value::Null),
                        args.get("confidence").unwrap_or(&Value::Null),
                        &errors,
                        args.get("validation_result").filter(|v| !v.is_null()),
                        args.get("context").filter(|v| !v.is_null()),
                    )?;
                    r.to_json()
                }
                "get_policies" => lp.get_policies(cx)?,
                "get_summary" => lp.get_summary(cx)?,
                "summary_text" => json!(lp.summary_text(cx)?),
                other => return Err(format!("нет метода {other}")),
            };
            json!({"out": out, "state": lp.to_json()})
        }
        // главный цикл жизни: состояние экземпляра (счётчики, рефлексия, мотивация) проходит через вызовы явно
        "cl_call" => {
            use crate::core_loop as cl;
            let mut lp = match a.get("state").filter(|v| !v.is_null()) {
                None => cl::CoreLoop::new(cx)?,
                Some(s) => cl::CoreLoop::from_json(s),
            };
            let args = a.get("args").cloned().unwrap_or(json!({}));
            let out = match a_str(a, "method").as_deref().unwrap_or("") {
                "init" => Value::Null,
                "run_cycle" => lp.run_cycle(cx, args.get("input").filter(|v| !v.is_null()))?,
                "perceive" => lp.perceive(cx, args.get("input").filter(|v| !v.is_null()))?,
                "update_world_model" => lp.update_world_model(cx, args.get("perception").ok_or("KeyError")?)?,
                "update_self_model" => lp.update_self_model(cx)?,
                "evaluate_goals" => lp.evaluate_goals(cx)?,
                "reflect" => lp.reflect(cx)?,
                "act" => lp.act(cx, args.get("action_type").and_then(|v| v.as_str()).unwrap_or(""), args.get("data").ok_or("KeyError")?)?,
                "remember" => lp.remember(cx, args.get("action_result").ok_or("KeyError")?)?,
                "get_status" => lp.get_status(cx)?,
                "summary_text" => json!(lp.summary_text(cx)?),
                other => return Err(format!("нет метода {other}")),
            };
            json!({"out": out, "state": lp.to_json()})
        }
        // внутреннее состояние личности (состояние — только в базе)
        name if name.starts_with("is_") => {
            use crate::inner_state as ist;
            let uid = a_str(a, "user_id").unwrap_or_default();
            match &name[3..] {
                "init" => {
                    ist::init(cx, &uid)?;
                    Value::Null
                }
                "add_event" => ist::add_event(cx, &uid, &a_str(a, "event_type").unwrap_or_default(), &a_str(a, "description").unwrap_or_default(), a.get("sincerity").and_then(|v| v.as_f64()).unwrap_or(0.5))?,
                "get_summary" => ist::get_summary(cx, &uid)?,
                "get_response_context" => ist::get_response_context(cx, &uid)?,
                "get_inner_monologue" => json!(ist::get_inner_monologue(cx, &uid)?),
                "get_history" => json!(ist::get_history(cx, &uid)?),
                other => return Err(format!("нет метода {other}")),
            }
        }
        // граф личности
        name if name.starts_with("pg_") => {
            use crate::personality_graph as pg;
            let st = |k: &str| a_str(a, k).unwrap_or_default();
            let fl = |k: &str, d: f64| a.get(k).and_then(|v| v.as_f64()).unwrap_or(d);
            match &name[3..] {
                "init" => {
                    pg::init(cx)?;
                    Value::Null
                }
                "get_traits" => pg::get_traits(cx)?,
                "get_trait_value" => json!(pg::get_trait_value(cx, &st("name"))?),
                "get_trait_description" => json!(pg::get_trait_description(cx, &st("name"))?),
                "get_all_traits_data" => pg::get_all_traits_data(cx)?,
                "get_high_traits" => json!(pg::get_high_traits(cx, fl("threshold", 0.7))?),
                "get_low_traits" => json!(pg::get_low_traits(cx, fl("threshold", 0.3))?),
                "get_evolving_traits" => json!(pg::get_evolving_traits(cx)?),
                "get_edges" => pg::get_edges(cx)?,
                "get_edge_weight" => json!(pg::get_edge_weight(cx, &st("source"), &st("target"))?),
                "get_edges_for_node" => pg::get_edges_for_node(cx, &st("node"))?,
                "get_conflicts" => json!(pg::get_conflicts(cx)?),
                "get_internal_questions" => json!(pg::get_internal_questions(cx)?),
                "get_evolution" => pg::get_evolution(cx, fl("days", 7.0))?,
                "set_trait" => {
                    pg::set_trait(cx, &st("name"), fl("value", 0.0))?;
                    Value::Null
                }
                "change_trait" => {
                    pg::change_trait(cx, &st("name"), fl("delta", 0.0), a.get("source").and_then(|v| v.as_str()))?;
                    Value::Null
                }
                "set_edge_weight" => {
                    pg::set_edge_weight(cx, &st("source"), &st("target"), fl("weight", 0.0))?;
                    Value::Null
                }
                "learn_edge" => {
                    pg::learn_edge(cx, &st("source"), &st("target"), a.get("success").and_then(|v| v.as_bool()).unwrap_or(false))?;
                    Value::Null
                }
                "reflect" => {
                    pg::reflect(cx, &st("event"), fl("intensity", 0.05))?;
                    Value::Null
                }
                "answer_internal_question" => {
                    pg::answer_internal_question(cx, a.get("question_idx").and_then(|v| v.as_i64()).unwrap_or(0), &st("answer"))?;
                    Value::Null
                }
                other => return Err(format!("нет метода {other}")),
            }
        }
        // любопытство: список неизвестных (состояние экземпляра) проходит через вызовы явно
        "cu_call" => {
            use crate::curiosity as cu;
            let mut e = match a.get("state").filter(|v| !v.is_null()) {
                None => cu::CuriosityEngine::default(),
                Some(s) => cu::CuriosityEngine::from_json(s),
            };
            let args = a.get("args").cloned().unwrap_or(json!({}));
            let ul = |v: Vec<cu::Unknown>| json!(v.iter().map(|u| u.to_json()).collect::<Vec<_>>());
            let refs = |v: Vec<&cu::Unknown>| json!(v.iter().map(|u| u.to_json()).collect::<Vec<_>>());
            let out = match a_str(a, "method").as_deref().unwrap_or("") {
                "init" => Value::Null,
                "analyze_beliefs" => ul(e.analyze_beliefs(cx)?),
                "analyze_response" => ul(e.analyze_response(
                    cx,
                    args.get("query").and_then(|v| v.as_str()).unwrap_or(""),
                    args.get("epistemic").ok_or("KeyError")?,
                    args.get("confidence").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    args.get("evidence_count").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    args.get("trust").and_then(|v| v.as_str()).unwrap_or(""),
                )?),
                "get_next_question" => match e.get_next_question() {
                    Some(u) => u.to_json(),
                    None => Value::Null,
                },
                "mark_resolved" => {
                    e.mark_resolved(args.get("unknown_id").and_then(|v| v.as_str()).unwrap_or(""));
                    Value::Null
                }
                "get_pending" => refs(e.get_pending()),
                "get_exploring" => refs(e.get_exploring()),
                "get_by_topic" => refs(e.get_by_topic(args.get("topic").unwrap_or(&Value::Null))),
                "unknowns" => json!(e.unknowns.iter().map(|u| u.to_json()).collect::<Vec<_>>()),
                "get_summary" => e.get_summary(),
                "to_dict" => e.to_dict(),
                other => return Err(format!("нет метода {other}")),
            };
            json!({"out": out, "state": e.to_json()})
        }
        other => yandi_db::repo::call(cx.conn, other, a)?,
    })
}

#[pymodule]
fn yandi_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(open_memory, m)?)?;
    m.add_function(wrap_pyfunction!(close, m)?)?;
    m.add_function(wrap_pyfunction!(set_clock, m)?)?;
    m.add_function(wrap_pyfunction!(query, m)?)?;
    m.add_function(wrap_pyfunction!(call, m)?)?;
    Ok(())
}
