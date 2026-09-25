//! Личный журнал (обязательства, причинные события, реплики, факты о человеке), расхождения, граф черт, внутренние вопросы, профиль саморефлексии, социальные знания,
//! архив запросов знаний — перенос последней группы `repositories.py`. Слова человека запечатываются защитой полей.
use rusqlite::Connection;
use serde_json::{json, Value};

use super::field_protection as fp;
use super::*;

const INTERACTION_TEXT_CAP: usize = 20000;

fn key(pairs: &[(&str, &str)]) -> Map<String, Value> {
    pairs.iter().map(|(k, v)| ((*k).to_string(), json!(v))).collect()
}

fn list(c: &Connection, sql: &str, args: Vec<Sql>, cols: &[&str], reverse: bool) -> R<Value> {
    let mut rs = rows(c, sql, args)?;
    for r in rs.iter_mut() {
        decode_json_cols(r, cols)?;
    }
    if reverse {
        rs.reverse();
    }
    Ok(json!(rs))
}

fn one(c: &Connection, sql: &str, args: Vec<Sql>, cols: &[&str]) -> R<Value> {
    match row(c, sql, args)? {
        Some(mut r) => {
            decode_json_cols(&mut r, cols)?;
            Ok(Value::Object(r))
        }
        None => Ok(Value::Null),
    }
}

fn count(c: &Connection, sql: &str, args: Vec<Sql>) -> R<i64> {
    Ok(row(c, sql, args)?.and_then(|r| r["c"].as_i64()).unwrap_or(0))
}

fn or_empty_list(v: Option<&Value>) -> Value {
    match v {
        Some(x) if !x.is_null() && !matches!(x, Value::Array(a) if a.is_empty()) => x.clone(),
        _ => json!([]),
    }
}

fn opened_rows(c: &Connection, table: &str, sql: &str, args: Vec<Sql>) -> R<Value> {
    let mut rs = rows(c, sql, args)?;
    fp::open_rows(c, table, &mut rs, None)?;
    Ok(json!(rs))
}

const TRAIT_GRAPH_JSON: [&str; 2] = ["nodes", "edges"];
const PROFILE_JSON: [&str; 5] = ["desires", "fears", "likes", "dislikes", "limitations"];
const SOCIAL_JSON: [&str; 3] = ["typical_reactions", "boundaries", "examples"];
const KQA_JSON: [&str; 2] = ["sources", "meta"];

fn get_trait_graph(c: &Connection) -> R<Value> {
    one(c, "SELECT * FROM trait_graph WHERE id=1", vec![], &TRAIT_GRAPH_JSON)
}

fn get_profile(c: &Connection) -> R<Value> {
    one(c, "SELECT * FROM self_reflection_profile WHERE id=1", vec![], &PROFILE_JSON)
}

fn list_internal_questions(c: &Connection) -> R<Value> {
    let mut qs = rows(c, "SELECT * FROM internal_question ORDER BY question_id ASC", vec![])?;
    for q in qs.iter_mut() {
        let answers = rows(c, "SELECT * FROM internal_question_answer WHERE question_id=? ORDER BY created_at ASC, answer_id ASC", vec![iv(q["question_id"].as_i64().unwrap_or(0))])?;
        q.insert("answers".into(), json!(answers));
    }
    Ok(json!(qs))
}

pub fn dispatch(c: &Connection, name: &str, a: &A) -> R<Option<Value>> {
    let limit = |def: i64| -> R<i64> { Ok(a.opt_i64("limit")?.unwrap_or(def)) };
    Ok(Some(match name {
        // ---- обязательства ----
        "record_commitment" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let id = a.str("commitment_id")?;
            let k = key(&[("commitment_id", &id)]);
            let text = fp::seal(c, "commitment", "text", &k, Some(&cut_chars(&a.str("text")?, 500)))?;
            let evidence = fp::seal(c, "commitment", "evidence", &k, Some(&cut_chars(&a.str("evidence")?, 500)))?;
            let due = dt_opt(a.get("due_at"))?;
            exec(
                c,
                "INSERT INTO commitment (commitment_id, user_id, kind, text, evidence, due_at, created_at, source_turn_id) VALUES (?,?,?,?,?,?,?,?)",
                vec![sv(&id), sv(&a.str("user_id")?), sv(&a.str("kind")?), osv(text.as_deref()), osv(evidence.as_deref()), osv(due.as_deref()), sv(&created_at), osv(a.opt_str("source_turn_id")?.as_deref())],
            )?;
            Value::Null
        }
        "get_commitment" => match row(c, "SELECT * FROM commitment WHERE commitment_id=?", vec![sv(&a.str("commitment_id")?)])? {
            Some(mut r) => {
                fp::open_row(c, "commitment", &mut r, None)?;
                Value::Object(r)
            }
            None => Value::Null,
        },
        "list_commitments" => opened_rows(c, "commitment", "SELECT * FROM commitment WHERE user_id=? ORDER BY created_at ASC, commitment_id ASC", vec![sv(&a.str("user_id")?)])?,
        "record_commitment_event" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let (cid, et) = (a.str("commitment_id")?, a.str("event_type")?);
            let ev = a.opt_str("evidence")?.map(|s| cut_chars(&s, 500)).filter(|s| !s.is_empty());
            let sealed = fp::seal(c, "commitment_event", "evidence", &key(&[("commitment_id", &cid), ("event_type", &et)]), ev.as_deref())?;
            let (n, _) = exec_ignore(
                c,
                "INSERT OR IGNORE INTO commitment_event (commitment_id, user_id, event_type, source, evidence, created_at, source_turn_id, span_start, span_end) VALUES (?,?,?,?,?,?,?,?,?)",
                vec![sv(&cid), sv(&a.str("user_id")?), sv(&et), sv(&a.str("source")?), osv(sealed.as_deref()), sv(&created_at), osv(a.opt_str("source_turn_id")?.as_deref()), oiv(a.opt_i64("span_start")?), oiv(a.opt_i64("span_end")?)],
            )?;
            json!(n == 1)
        }
        "list_commitment_events" => opened_rows(c, "commitment_event", "SELECT * FROM commitment_event WHERE user_id=? ORDER BY event_id ASC", vec![sv(&a.str("user_id")?)])?,
        "count_commitment_events" => json!(row(c, "SELECT COUNT(*) AS n FROM commitment_event WHERE user_id=? AND event_type=? AND source=?", vec![sv(&a.str("user_id")?), sv(&a.str("event_type")?), sv(&a.str("source")?)])?.and_then(|r| r["n"].as_i64()).unwrap_or(0)),
        "claim_causal_event" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let (n, _) = exec_ignore(c, "INSERT OR IGNORE INTO causal_event (user_id, source_turn_id, event_type, span_start, span_end, created_at) VALUES (?,?,?,?,?,?)", vec![sv(&a.str("user_id")?), sv(&a.str("source_turn_id")?), sv(&a.str("event_type")?), oiv(a.opt_i64("span_start")?), oiv(a.opt_i64("span_end")?), sv(&created_at)])?;
            json!(n == 1)
        }
        // ---- реплики ----
        "record_interaction_turn" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let (uid, stid) = (a.str("user_id")?, a.str("source_turn_id")?);
            let k = key(&[("user_id", &uid), ("source_turn_id", &stid)]);
            let user_stored = fp::seal(c, "interaction_turn", "user_text", &k, Some(&cut_chars(&a.str("user_text")?, INTERACTION_TEXT_CAP)))?;
            let asst = a.opt_str("assistant_text")?.map(|s| cut_chars(&s, INTERACTION_TEXT_CAP));
            let assistant_stored = fp::seal(c, "interaction_turn", "assistant_text", &k, asst.as_deref())?;
            let recalled = match a.get("recalled_turn_ids") {
                Some(Value::Array(x)) if !x.is_empty() => jv(a.get("recalled_turn_ids")),
                _ => Sql::Null,
            };
            let (n, _) = exec_ignore(
                c,
                "INSERT OR IGNORE INTO interaction_turn (user_id, source_turn_id, turn_id_origin, user_text, assistant_text, model, adapter, recalled_turn_ids, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                vec![sv(&uid), sv(&stid), sv(&a.str("turn_id_origin")?), osv(user_stored.as_deref()), osv(assistant_stored.as_deref()), osv(a.opt_str("model")?.as_deref()), osv(a.opt_str("adapter")?.as_deref()), recalled, sv(&created_at)],
            )?;
            json!(n == 1)
        }
        "get_interaction_turn_text" => {
            let (uid, stid) = (a.str("user_id")?, a.str("source_turn_id")?);
            match row(c, "SELECT user_text FROM interaction_turn WHERE user_id=? AND source_turn_id=?", vec![sv(&uid), sv(&stid)])? {
                None => Value::Null,
                Some(r) => fp::open_value(c, "interaction_turn", "user_text", &key(&[("user_id", &uid), ("source_turn_id", &stid)]), r["user_text"].as_str())?.map(Value::String).unwrap_or(Value::Null),
            }
        }
        "list_recent_interaction_turns" => opened_rows(
            c,
            "interaction_turn",
            "SELECT t.interaction_id, t.user_id, t.source_turn_id, t.user_text, t.assistant_text, t.model, t.adapter, t.created_at, (SELECT group_concat(c.event_type) FROM causal_event c WHERE c.user_id = t.user_id AND c.source_turn_id = t.source_turn_id) AS event_types FROM interaction_turn t WHERE t.user_id=? ORDER BY t.created_at DESC, t.interaction_id DESC LIMIT ?",
            vec![sv(&a.str("user_id")?), iv(limit(300)?)],
        )?,
        // ---- факты о человеке ----
        "insert_personal_fact" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let id = a.str("fact_id")?;
            let k = key(&[("fact_id", &id)]);
            let statement = fp::seal(c, "personal_fact", "statement", &k, Some(&a.str("statement")?))?;
            let evidence = fp::seal(c, "personal_fact", "evidence", &k, Some(&a.str("evidence")?))?;
            exec(
                c,
                "INSERT INTO personal_fact (fact_id, user_id, fact_class, statement, polarity, temporality, evidence, span_start, span_end, source_turn_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                vec![sv(&id), sv(&a.str("user_id")?), sv(&a.str("fact_class")?), osv(statement.as_deref()), sv(&a.str("polarity")?), sv(&a.str("temporality")?), osv(evidence.as_deref()), oiv(a.opt_i64("span_start")?), oiv(a.opt_i64("span_end")?), sv(&a.str("source_turn_id")?), sv(&created_at)],
            )?;
            Value::Null
        }
        "insert_personal_fact_event" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let (fid, et, stid) = (a.str("fact_id")?, a.str("event_type")?, a.str("source_turn_id")?);
            let evidence = fp::seal(c, "personal_fact_event", "evidence", &key(&[("fact_id", &fid), ("event_type", &et), ("source_turn_id", &stid)]), Some(&a.str("evidence")?))?;
            exec(
                c,
                "INSERT INTO personal_fact_event (fact_id, user_id, event_type, by_fact_id, evidence, span_start, span_end, source_turn_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                vec![sv(&fid), sv(&a.str("user_id")?), sv(&et), osv(a.opt_str("by_fact_id")?.as_deref()), osv(evidence.as_deref()), oiv(a.opt_i64("span_start")?), oiv(a.opt_i64("span_end")?), sv(&stid), sv(&created_at)],
            )?;
            Value::Null
        }
        "list_personal_facts" => opened_rows(c, "personal_fact", "SELECT fact_id, user_id, fact_class, statement, polarity, temporality, evidence, source_turn_id, created_at FROM personal_fact WHERE user_id=? ORDER BY created_at DESC, fact_id DESC LIMIT ?", vec![sv(&a.str("user_id")?), iv(limit(1000)?)])?,
        "list_personal_fact_events" => list(c, "SELECT event_id, fact_id, event_type, by_fact_id, source_turn_id, created_at FROM personal_fact_event WHERE user_id=? ORDER BY event_id", vec![sv(&a.str("user_id")?)], &[], false)?,
        // ---- расхождения ----
        "create_disagreement" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            exec(
                c,
                "INSERT INTO disagreement (disagreement_id, topic, old_position, challenge, analysis, new_position, confidence_before, confidence_after, resolved, related_belief_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                vec![sv(&a.str("disagreement_id")?), sv(&a.str("topic")?), sv(&a.str("old_position")?), sv(&a.str("challenge")?), sv(&a.str("analysis")?), sv(&a.str("new_position")?), fv(a.opt_f64("confidence_before")?), fv(a.opt_f64("confidence_after")?), bv(a.bool_or("resolved", true)), osv(a.opt_str("related_belief_id")?.as_deref()), sv(&created_at)],
            )?;
            Value::Null
        }
        "list_disagreements" => list(c, "SELECT * FROM disagreement ORDER BY created_at ASC, rowid ASC", vec![], &[], false)?,
        // ---- граф черт ----
        "get_trait_graph" => get_trait_graph(c)?,
        "get_or_create_trait_graph" => {
            exec_ignore(c, "INSERT OR IGNORE INTO trait_graph (id, nodes, edges, updated_at) VALUES (1, ?, ?, ?)", vec![jv(a.get("nodes")), jv(a.get("edges")), sv(&dt_or_now(a.get("updated_at"))?)])?;
            get_trait_graph(c)?
        }
        "update_trait_graph" => {
            let mut sets = Vec::new();
            let mut args = Vec::new();
            for col in ["nodes", "edges"] {
                if let Some(v) = a.get(col).filter(|v| !v.is_null()) {
                    sets.push(format!("{col}=?"));
                    args.push(jv(Some(v)));
                }
            }
            if !sets.is_empty() {
                sets.push("updated_at=?".into());
                args.push(sv(&dt_or_now(a.get("updated_at"))?));
                exec(c, &format!("UPDATE trait_graph SET {} WHERE id=1", sets.join(", ")), args)?;
            }
            Value::Null
        }
        "record_trait_change" => {
            exec(c, "INSERT INTO trait_change (node, new_value, source, created_at) VALUES (?,?,?,?)", vec![sv(&a.str("node")?), fv(a.opt_f64("new_value")?), osv(a.opt_str("source")?.as_deref()), sv(&dt_or_now(a.get("created_at"))?)])?;
            Value::Null
        }
        "list_trait_changes" => match dt_opt(a.get("since"))? {
            Some(since) => list(c, "SELECT * FROM trait_change WHERE created_at >= ? ORDER BY created_at ASC, change_id ASC LIMIT ?", vec![sv(&since), iv(limit(500)?)], &[], false)?,
            None => list(c, "SELECT * FROM trait_change ORDER BY created_at DESC, change_id DESC LIMIT ?", vec![iv(limit(500)?)], &[], true)?,
        },
        "record_trait_edge_change" => {
            exec(c, "INSERT INTO trait_edge_change (source_node, target_node, old_weight, new_weight, created_at) VALUES (?,?,?,?,?)", vec![sv(&a.str("source_node")?), sv(&a.str("target_node")?), fv(a.opt_f64("old_weight")?), fv(a.opt_f64("new_weight")?), sv(&dt_or_now(a.get("created_at"))?)])?;
            Value::Null
        }
        // ---- внутренние вопросы ----
        "get_or_seed_internal_questions" => {
            if count(c, "SELECT COUNT(*) AS c FROM internal_question", vec![])? == 0 {
                let created_at = dt_or_now(a.get("created_at"))?;
                if let Some(Value::Array(qs)) = a.get("default_questions") {
                    for q in qs {
                        exec(c, "INSERT INTO internal_question (question_text, created_at) VALUES (?,?)", vec![sv(q.as_str().unwrap_or("")), sv(&created_at)])?;
                    }
                }
            }
            list_internal_questions(c)?
        }
        "list_internal_questions" => list_internal_questions(c)?,
        "record_internal_question_answer" => json!(exec(c, "INSERT INTO internal_question_answer (question_id, answer_text, created_at) VALUES (?,?,?)", vec![iv(a.i64("question_id")?), sv(&a.str("answer_text")?), sv(&dt_or_now(a.get("created_at"))?)])?.1),
        // ---- профиль саморефлексии ----
        "get_self_reflection_profile" => get_profile(c)?,
        "get_or_create_self_reflection_profile" => {
            let mut args: Vec<Sql> = PROFILE_JSON.iter().map(|k| jv(a.get(k))).collect();
            args.push(sv(&dt_or_now(a.get("updated_at"))?));
            exec_ignore(c, "INSERT OR IGNORE INTO self_reflection_profile (id, desires, fears, likes, dislikes, limitations, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?)", args)?;
            get_profile(c)?
        }
        "increment_self_reflection_count" => {
            exec(c, "UPDATE self_reflection_profile SET reflections_count = reflections_count + 1, updated_at=? WHERE id=1", vec![sv(&dt_or_now(a.get("updated_at"))?)])?;
            Value::Null
        }
        // ---- социальные знания ----
        "get_social_knowledge" => one(c, "SELECT * FROM social_knowledge WHERE speech_act=? AND topic=?", vec![sv(&a.str("speech_act")?), sv(&a.str("topic")?)], &SOCIAL_JSON)?,
        "upsert_social_knowledge" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let updated_at = dt_or_now(a.get("updated_at"))?;
            exec(
                c,
                "INSERT INTO social_knowledge (speech_act, topic, description, typical_reactions, cultural_context, boundaries, recommended_approach, examples, source, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(speech_act, topic) DO UPDATE SET description=excluded.description, typical_reactions=excluded.typical_reactions, cultural_context=excluded.cultural_context, boundaries=excluded.boundaries, recommended_approach=excluded.recommended_approach, examples=excluded.examples, source=excluded.source, confidence=excluded.confidence, updated_at=excluded.updated_at",
                vec![sv(&a.str("speech_act")?), sv(&a.str("topic")?), sv(&a.str("description")?), jv(a.get("typical_reactions")), sv(&a.str("cultural_context")?), jv(a.get("boundaries")), sv(&a.str("recommended_approach")?), jv(a.get("examples")), sv(&a.opt_str("source")?.unwrap_or_else(|| "research".into())), fv(Some(a.opt_f64("confidence")?.unwrap_or(0.5))), sv(&created_at), sv(&updated_at)],
            )?;
            Value::Null
        }
        // ---- архив запросов знаний ----
        "record_knowledge_query" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let updated_at = dt_or_now(a.get("updated_at"))?;
            let sources = or_empty_list(a.get("sources"));
            let meta = match a.get("meta") {
                Some(Value::Object(m)) if !m.is_empty() => a.get("meta").cloned().unwrap(),
                _ => json!({}),
            };
            const KEEP: &str = "knowledge_query_archive.trust_level != 'VERIFIED' OR excluded.trust_level = 'VERIFIED'";
            let upd = |col: &str| format!("{col} = CASE WHEN {KEEP} THEN excluded.{col} ELSE knowledge_query_archive.{col} END");
            let sql = format!(
                "INSERT INTO knowledge_query_archive (entry_id, query, answer, tag, category, trust_level, confidence, sources, node_id, version, meta, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?) \
                 ON CONFLICT(entry_id) DO UPDATE SET {}, {}, {}, {}, {}, {}, version = CASE WHEN {KEEP} THEN knowledge_query_archive.version + 1 ELSE knowledge_query_archive.version END, {}",
                upd("answer"), upd("trust_level"), upd("confidence"), upd("sources"), upd("node_id"), upd("meta"), upd("updated_at")
            );
            exec(
                c,
                &sql,
                vec![sv(&a.str("entry_id")?), sv(&a.str("query")?), sv(&a.str("answer")?), sv(&a.str("tag")?), sv(&a.str("category")?), sv(&a.opt_str("trust_level")?.unwrap_or_else(|| "UNVERIFIED".into())), fv(Some(a.opt_f64("confidence")?.unwrap_or(0.0))), jv(Some(&sources)), sv(&a.opt_str("node_id")?.unwrap_or_default()), jv(Some(&meta)), sv(&created_at), sv(&updated_at)],
            )?;
            Value::Null
        }
        "get_knowledge_query" => one(c, "SELECT * FROM knowledge_query_archive WHERE entry_id=?", vec![sv(&a.str("entry_id")?)], &KQA_JSON)?,
        "set_knowledge_query_verified" => json!(exec(c, "UPDATE knowledge_query_archive SET trust_level='VERIFIED', updated_at=? WHERE entry_id=?", vec![sv(&dt_or_now(a.get("updated_at"))?), sv(&a.str("entry_id")?)])?.0 > 0),
        "update_knowledge_query_answer" => json!(exec(c, "UPDATE knowledge_query_archive SET answer=?, trust_level=?, version=version+1, updated_at=? WHERE entry_id=?", vec![sv(&a.str("answer")?), sv(&a.opt_str("trust_level")?.unwrap_or_else(|| "VERIFIED".into())), sv(&dt_or_now(a.get("updated_at"))?), sv(&a.str("entry_id")?)])?.0 > 0),
        "delete_knowledge_query" => json!(exec(c, "DELETE FROM knowledge_query_archive WHERE entry_id=?", vec![sv(&a.str("entry_id")?)])?.0 > 0),
        "list_unverified_knowledge_queries" => list(c, "SELECT entry_id, query, answer, tag, confidence, created_at FROM knowledge_query_archive WHERE trust_level != 'VERIFIED' ORDER BY created_at DESC, rowid DESC LIMIT ?", vec![iv(limit(30)?)], &[], false)?,
        "list_knowledge_queries_by_tag" => {
            let tag = a.str("tag")?;
            list(c, "SELECT * FROM knowledge_query_archive WHERE (tag=? OR tag LIKE ?) AND confidence >= ? ORDER BY created_at DESC, rowid DESC LIMIT ?", vec![sv(&tag), sv(&format!("{tag}:%")), Sql::Real(a.opt_f64("min_confidence")?.unwrap_or(0.0)), iv(limit(100)?)], &KQA_JSON, false)?
        }
        "list_knowledge_query_tags" => json!(rows(c, "SELECT DISTINCT tag FROM knowledge_query_archive ORDER BY tag", vec![])?.into_iter().map(|r| r["tag"].clone()).collect::<Vec<_>>()),
        "knowledge_query_archive_stats" => {
            let cats: Vec<Value> = rows(c, "SELECT DISTINCT category FROM knowledge_query_archive ORDER BY category", vec![])?.into_iter().map(|r| r["category"].clone()).collect();
            json!({"categories": cats, "knowledge": count(c, "SELECT COUNT(*) AS c FROM knowledge_query_archive", vec![])?, "verified": count(c, "SELECT COUNT(*) AS c FROM knowledge_query_archive WHERE trust_level='VERIFIED'", vec![])?})
        }
        _ => return Ok(None),
    }))
}
