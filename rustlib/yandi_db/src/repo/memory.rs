//! Эпизоды, состояние «я», события себя, политики рефлексии, семантические рёбра, знания, биография, контекст, журнал решений, опыт, секретный архив,
//! внутреннее состояние — перенос группы `repositories.py`.
use rusqlite::Connection;
use serde_json::{json, Value};
use yandi_rs::py_text::py_repr_str;

use super::*;

fn dec(rs: Vec<Row>, cols: &[&str]) -> R<Vec<Row>> {
    rs.into_iter().map(|mut r| decode_json_cols(&mut r, cols).map(|_| r)).collect()
}

fn list(c: &Connection, sql: &str, args: Vec<Sql>, cols: &[&str], reverse: bool) -> R<Value> {
    let mut rs = dec(rows(c, sql, args)?, cols)?;
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

fn count(c: &Connection, sql: &str, args: Vec<Sql>) -> R<Value> {
    Ok(row(c, sql, args)?.map(|r| r["c"].clone()).unwrap_or(json!(0)))
}

/// `round(float(x), 2)`.
fn round2(x: f64) -> f64 {
    format!("{x:.2}").parse().unwrap_or(x)
}

fn f32_avg(c: &Connection, sql: &str) -> R<Option<f64>> {
    let vals: Vec<f64> = rows(c, sql, vec![])?.iter().filter_map(|r| r["v"].as_f64()).map(|x| (x as f32) as f64).collect();
    Ok(if vals.is_empty() { None } else { Some(vals.iter().sum::<f64>() / vals.len() as f64) })
}

const EPISODE_JSON: [&str; 3] = ["details", "tags", "related_episodes"];
const SELF_STATE_JSON: [&str; 4] = ["capabilities", "limitations", "current_uncertainties", "metadata"];
const SELF_STATE_COUNTERS: [&str; 7] = ["total_cycles", "total_decisions", "total_learnings", "total_reflections", "total_errors", "total_queries", "total_belief_updates"];
const KNOWLEDGE_JSON: [&str; 3] = ["tags", "sources", "meta"];
const JOURNAL_JSON: [&str; 5] = ["context", "analysis", "alternatives", "outcome", "self_correction"];
const INNER_FIELDS: [&str; 13] = ["mood", "energy", "curiosity", "patience", "openness", "trust", "respect", "forgiveness", "affection", "pattern", "current_feeling", "current_intent", "current_tone"];
const INNER_FLOATS: [&str; 8] = ["energy", "curiosity", "patience", "openness", "trust", "respect", "forgiveness", "affection"];

fn get_self_state(c: &Connection) -> R<Value> {
    one(c, "SELECT * FROM self_state WHERE id=1", vec![], &SELF_STATE_JSON)
}

fn get_peer_config(c: &Connection) -> R<Value> {
    one(c, "SELECT * FROM peer_config WHERE id=1", vec![], &["peers"])
}

fn get_biography(c: &Connection, uid: &str) -> R<Value> {
    one(c, "SELECT * FROM biography WHERE user_id=?", vec![sv(uid)], &["last_principles_change"])
}

fn get_inner_state(c: &Connection, uid: &str) -> R<Value> {
    Ok(row(c, "SELECT * FROM inner_state WHERE user_id=?", vec![sv(uid)])?.map(Value::Object).unwrap_or(Value::Null))
}

/// Общая часть «синглтон: INSERT IGNORE + чтение».
fn seed_json_columns(a: &A, names: &[&str]) -> Vec<Sql> {
    names.iter().map(|n| jv(a.get(n))).collect()
}

fn semantic_edge(c: &Connection, fa: &str, fb: &str, et: &str) -> R<Value> {
    one(c, "SELECT * FROM semantic_edge WHERE family_a=? AND family_b=? AND edge_type=?", vec![sv(fa), sv(fb), sv(et)], &["triggering_claim_ids"])
}

pub fn dispatch(c: &Connection, name: &str, a: &A) -> R<Option<Value>> {
    let limit = |def: i64| -> R<i64> { Ok(a.opt_i64("limit")?.unwrap_or(def)) };
    Ok(Some(match name {
        // ---- эпизоды ----
        "record_episode" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            exec(
                c,
                "INSERT INTO episode (episode_id, event_type, summary, details, importance, tags, related_episodes, created_at) VALUES (?,?,?,?,?,?,?,?)",
                vec![sv(&a.str("episode_id")?), sv(&a.str("event_type")?), sv(&a.str("summary")?), jv(a.get("details")), fv(Some(a.opt_f64("importance")?.unwrap_or(0.5))), jv(a.get("tags")), jv(a.get("related_episodes")), sv(&created_at)],
            )?;
            Value::Null
        }
        "get_episodes_by_type" => list(c, "SELECT * FROM episode WHERE event_type=? ORDER BY created_at DESC, rowid DESC LIMIT ?", vec![sv(&a.str("event_type")?), iv(limit(20)?)], &EPISODE_JSON, true)?,
        "get_episodes_by_tag" => list(c, "SELECT * FROM episode WHERE EXISTS (SELECT 1 FROM json_each(episode.tags) WHERE json_each.type='text' AND json_each.value = ?) ORDER BY created_at DESC, rowid DESC LIMIT ?", vec![sv(&a.str("tag")?), iv(limit(20)?)], &EPISODE_JSON, true)?,
        "get_episodes_by_importance" => list(c, "SELECT * FROM episode WHERE importance >= ? ORDER BY importance DESC, rowid DESC LIMIT ?", vec![Sql::Real(a.opt_f64("min_importance")?.unwrap_or(0.7)), iv(limit(20)?)], &EPISODE_JSON, false)?,
        "get_recent_episodes" => list(c, "SELECT * FROM episode ORDER BY created_at DESC, rowid DESC LIMIT ?", vec![iv(limit(20)?)], &EPISODE_JSON, true)?,
        "get_episode_stats" => {
            let total = count(c, "SELECT COUNT(*) AS c FROM episode", vec![])?.as_i64().unwrap_or(0);
            let by_type = rows(c, "SELECT event_type, COUNT(*) AS c FROM episode GROUP BY event_type", vec![])?;
            let span = row(c, "SELECT MIN(created_at) AS oldest, MAX(created_at) AS newest FROM episode", vec![])?.unwrap_or_default();
            let mut bt = Map::new();
            for r in &by_type {
                bt.insert(r["event_type"].as_str().unwrap_or("").to_string(), r["c"].clone());
            }
            let avg = f32_avg(c, "SELECT importance AS v FROM episode WHERE importance IS NOT NULL")?;
            json!({
                "total_episodes": total, "by_type": Value::Object(bt),
                "avg_importance": match avg { Some(x) if total > 0 => json!(round2(x)), _ => json!(0) },
                "oldest_episode": span.get("oldest").cloned().unwrap_or(Value::Null), "last_episode": span.get("newest").cloned().unwrap_or(Value::Null),
            })
        }
        // ---- состояние «я» ----
        "get_self_state" => get_self_state(c)?,
        "get_or_create_self_state" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let mut args = vec![sv(&a.str("identity")?), sv(&a.str("version")?)];
            args.extend(seed_json_columns(a, &["capabilities", "limitations", "current_uncertainties", "metadata"]));
            args.push(sv(&created_at));
            args.push(sv(&created_at));
            exec(c, "INSERT OR IGNORE INTO self_state (id, identity, version, capabilities, limitations, current_uncertainties, metadata, created_at, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)", args)?;
            get_self_state(c)?
        }
        "update_self_state_lists" => {
            let mut sets = Vec::new();
            let mut args = Vec::new();
            for col in ["capabilities", "limitations", "current_uncertainties", "metadata"] {
                if let Some(v) = a.get(col).filter(|v| !v.is_null()) {
                    sets.push(format!("{col}=?"));
                    args.push(jv(Some(v)));
                }
            }
            if !sets.is_empty() {
                sets.push("updated_at=?".into());
                args.push(sv(&dt_or_now(a.get("updated_at"))?));
                exec(c, &format!("UPDATE self_state SET {} WHERE id=1", sets.join(", ")), args)?;
            }
            Value::Null
        }
        "increment_self_state_counter" => {
            let counter = a.str("counter")?;
            if !SELF_STATE_COUNTERS.contains(&counter.as_str()) {
                return Err(format!("ValueError: unknown self_state counter: {}", py_repr_str(&counter)));
            }
            exec(c, &format!("UPDATE self_state SET {counter} = {counter} + 1, updated_at=? WHERE id=1"), vec![sv(&dt_or_now(a.get("updated_at"))?)])?;
            Value::Null
        }
        // ---- события себя ----
        "record_self_event" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            exec(c, "INSERT INTO self_event (event_id, event_type, description, details, importance, created_at) VALUES (?,?,?,?,?,?)", vec![sv(&a.str("event_id")?), sv(&a.str("event_type")?), sv(&a.str("description")?), jv(a.get("details")), fv(Some(a.opt_f64("importance")?.unwrap_or(0.5))), sv(&created_at)])?;
            Value::Null
        }
        "get_self_events_by_type" => list(c, "SELECT * FROM self_event WHERE event_type=? ORDER BY created_at DESC, rowid DESC LIMIT ?", vec![sv(&a.str("event_type")?), iv(limit(10)?)], &["details"], false)?,
        "get_recent_self_events" => list(c, "SELECT * FROM self_event ORDER BY created_at DESC, rowid DESC LIMIT ?", vec![iv(limit(20)?)], &["details"], false)?,
        "count_self_events" => count(c, "SELECT COUNT(*) AS c FROM self_event", vec![])?,
        // ---- политики рефлексии ----
        "find_reflection_policy_by_rule" => row(c, "SELECT * FROM reflection_policy WHERE rule_hash=?", vec![sv(&sha256_hex(&a.str("rule")?))])?.map(Value::Object).unwrap_or(Value::Null),
        "create_reflection_policy" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let rule = a.str("rule")?;
            exec(c, "INSERT INTO reflection_policy (policy_id, policy_type, rule, rule_hash, confidence, status, observed_count, applied_count, created_at) VALUES (?,?,?,?,?,'observed',1,0,?)", vec![sv(&a.str("policy_id")?), sv(&a.str("policy_type")?), sv(&rule), sv(&sha256_hex(&rule)), fv(a.opt_f64("confidence")?), sv(&created_at)])?;
            Value::Null
        }
        "bump_reflection_policy_observed" => {
            let pid = a.str("policy_id")?;
            if a.bool_or("activate", false) {
                let at = dt_or_now(a.get("activated_at"))?;
                exec(c, "UPDATE reflection_policy SET observed_count = observed_count + 1, status='active', activated_at=? WHERE policy_id=?", vec![sv(&at), sv(&pid)])?;
            } else {
                exec(c, "UPDATE reflection_policy SET observed_count = observed_count + 1 WHERE policy_id=?", vec![sv(&pid)])?;
            }
            Value::Null
        }
        "list_all_reflection_policies" => list(c, "SELECT * FROM reflection_policy ORDER BY created_at ASC, rowid ASC", vec![], &[], false)?,
        // ---- семантические рёбра, статус семейства ----
        "find_semantic_edge" => semantic_edge(c, &a.str("family_a")?, &a.str("family_b")?, &a.str("edge_type")?)?,
        "upsert_semantic_edge" => {
            let (fa, fb, et) = (a.str("family_a")?, a.str("family_b")?, a.str("edge_type")?);
            let now = dt_or_now(a.get("created_at"))?;
            for fam in [&fa, &fb] {
                exec(c, "INSERT OR IGNORE INTO claim_family (family_id, domain, canonical_text, created_at, updated_at) VALUES (?, 'unknown', ?, ?, ?)", vec![sv(fam), sv(fam), sv(&now), sv(&now)])?;
            }
            let claims: Vec<String> = match a.get("triggering_claim_ids") {
                Some(Value::Array(x)) => x.iter().filter_map(|v| v.as_str().map(String::from)).filter(|s| !s.is_empty()).collect(),
                _ => vec![],
            };
            let existing = semantic_edge(c, &fa, &fb, &et)?;
            if let Value::Object(e) = &existing {
                let mut merged: Vec<String> = match e.get("triggering_claim_ids") {
                    Some(Value::Array(x)) => x.iter().filter_map(|v| v.as_str().map(String::from)).collect(),
                    _ => vec![],
                };
                for cid in claims {
                    if !merged.contains(&cid) {
                        merged.push(cid);
                    }
                }
                exec(c, "UPDATE semantic_edge SET observation_count = observation_count + 1, last_seen_at=?, triggering_claim_ids=? WHERE edge_id=?", vec![sv(&now), jv(Some(&json!(merged))), sv(e["edge_id"].as_str().unwrap_or(""))])?;
            } else {
                exec(c, "INSERT INTO semantic_edge (edge_id, family_a, family_b, edge_type, reason, observation_count, triggering_claim_ids, created_at, last_seen_at) VALUES (?,?,?,?,?,1,?,?,?)", vec![sv(&a.str("edge_id")?), sv(&fa), sv(&fb), sv(&et), sv(&a.str("reason")?), jv(Some(&json!(claims))), sv(&now), sv(&now)])?;
            }
            semantic_edge(c, &fa, &fb, &et)?
        }
        "list_dependents" => list(c, "SELECT * FROM semantic_edge WHERE family_b=? AND edge_type='depends_on' ORDER BY rowid", vec![sv(&a.str("family_id")?)], &["triggering_claim_ids"], false)?,
        "list_contradicts_edges" => list(c, "SELECT * FROM semantic_edge WHERE edge_type='contradicts' ORDER BY rowid", vec![], &["triggering_claim_ids"], false)?,
        "get_family_status" => row(c, "SELECT * FROM family_status_state WHERE family_id=?", vec![sv(&a.str("family_id")?)])?.map(Value::Object).unwrap_or(Value::Null),
        "upsert_family_status" => {
            let fid = a.str("family_id")?;
            let at = dt_or_now(a.get("updated_at"))?;
            exec(c, "INSERT OR IGNORE INTO claim_family (family_id, domain, canonical_text, created_at, updated_at) VALUES (?, 'unknown', ?, ?, ?)", vec![sv(&fid), sv(&fid), sv(&at), sv(&at)])?;
            exec(c, "INSERT INTO family_status_state (family_id, last_status, updated_at) VALUES (?,?,?) ON CONFLICT(family_id) DO UPDATE SET last_status=excluded.last_status, updated_at=excluded.updated_at", vec![sv(&fid), osv(a.opt_str("last_status")?.as_deref()), sv(&at)])?;
            Value::Null
        }
        "get_last_recheck" => row(c, "SELECT * FROM recheck_event WHERE family_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1", vec![sv(&a.str("family_id")?)])?.map(Value::Object).unwrap_or(Value::Null),
        // ---- знания ----
        "upsert_knowledge_record" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let updated_at = dt_or_now(a.get("updated_at"))?;
            exec(
                c,
                "INSERT INTO knowledge_record (record_id, question, answer, trust_level, verdict, topic, tags, sources, meta, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET question=excluded.question, answer=excluded.answer, trust_level=excluded.trust_level, verdict=excluded.verdict, topic=excluded.topic, tags=excluded.tags, sources=excluded.sources, meta=excluded.meta, updated_at=excluded.updated_at",
                vec![sv(&a.str("record_id")?), sv(&a.str("question")?), sv(&a.str("answer")?), sv(&a.str("trust_level")?), osv(a.opt_str("verdict")?.as_deref()), sv(&a.opt_str("topic")?.unwrap_or_else(|| "general".into())), jv(a.get("tags")), jv(a.get("sources")), jv(a.get("meta")), sv(&created_at), sv(&updated_at)],
            )?;
            Value::Null
        }
        "get_knowledge_record" => one(c, "SELECT * FROM knowledge_record WHERE record_id=?", vec![sv(&a.str("record_id")?)], &KNOWLEDGE_JSON)?,
        "update_knowledge_record_trust" => {
            let at = dt_or_now(a.get("updated_at"))?;
            let (n, _) = match a.opt_str("verdict")?.filter(|v| !v.is_empty()) {
                Some(v) => exec(c, "UPDATE knowledge_record SET trust_level=?, verdict=?, updated_at=? WHERE record_id=?", vec![sv(&a.str("trust_level")?), sv(&v), sv(&at), sv(&a.str("record_id")?)])?,
                None => exec(c, "UPDATE knowledge_record SET trust_level=?, updated_at=? WHERE record_id=?", vec![sv(&a.str("trust_level")?), sv(&at), sv(&a.str("record_id")?)])?,
            };
            json!(n > 0)
        }
        "list_knowledge_by_trust" => list(c, "SELECT record_id, question, topic, created_at FROM knowledge_record WHERE trust_level=? ORDER BY created_at DESC, rowid DESC LIMIT ?", vec![sv(&a.str("trust_level")?), iv(limit(100)?)], &[], false)?,
        "get_knowledge_stats" => {
            let total = count(c, "SELECT COUNT(*) AS c FROM knowledge_record", vec![])?;
            let mut bt = Map::new();
            for r in rows(c, "SELECT trust_level, COUNT(*) AS c FROM knowledge_record GROUP BY trust_level", vec![])? {
                bt.insert(r["trust_level"].as_str().unwrap_or("").to_string(), r["c"].clone());
            }
            json!({"total": total, "by_trust": Value::Object(bt)})
        }
        // ---- настройки пиров ----
        "get_peer_config" => get_peer_config(c)?,
        "get_or_create_peer_config" => {
            exec(c, "INSERT OR IGNORE INTO peer_config (id, peers, sync_token, sync_enabled, updated_at) VALUES (1, ?, NULL, 0, ?)", vec![sv("[]"), sv(&dt_or_now(a.get("updated_at"))?)])?;
            get_peer_config(c)?
        }
        // ---- биография ----
        "get_biography" => get_biography(c, &a.str("user_id")?)?,
        "get_or_create_biography" => {
            let uid = a.str("user_id")?;
            let birth = dt_or_now(a.get("birth"))?;
            exec(c, "INSERT OR IGNORE INTO biography (user_id, birth, updated_at) VALUES (?,?,?)", vec![sv(&uid), sv(&birth), sv(&birth)])?;
            get_biography(c, &uid)?
        }
        "bump_biography_counter" => {
            let counter = a.str("counter")?;
            if !["cycles", "saved_memories", "forgotten_memories", "reconsidered_decisions", "changed_habits", "total_decisions", "total_reflections"].contains(&counter.as_str()) {
                return Err(format!("ValueError: unknown biography counter: {}", py_repr_str(&counter)));
            }
            exec(c, &format!("UPDATE biography SET {counter} = {counter} + ?, updated_at=? WHERE user_id=?"), vec![iv(a.opt_i64("amount")?.unwrap_or(1)), sv(&dt_or_now(a.get("updated_at"))?), sv(&a.str("user_id")?)])?;
            Value::Null
        }
        "set_biography_principles_change" => {
            let ts = isoformat_of(a.get("updated_at"))?;
            let at = dt_or_now(a.get("updated_at"))?;
            let payload = pydumps(&json!({"old": a.str("old")?, "new": a.str("new")?, "timestamp": ts}));
            // JSON-столбец: MySQL нормализует текст
            let parsed: Value = serde_json::from_str(&payload).map_err(err)?;
            exec(c, "UPDATE biography SET last_principles_change=?, updated_at=? WHERE user_id=?", vec![jv(Some(&parsed)), sv(&at), sv(&a.str("user_id")?)])?;
            Value::Null
        }
        "record_biography_event" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            json!(exec(c, "INSERT INTO biography_event (user_id, event_type, payload, created_at) VALUES (?,?,?,?)", vec![sv(&a.str("user_id")?), sv(&a.str("event_type")?), jv(a.get("payload")), sv(&created_at)])?.1)
        }
        "list_biography_events" => list(c, "SELECT * FROM biography_event WHERE user_id=? AND event_type=? ORDER BY created_at DESC, event_id DESC LIMIT ?", vec![sv(&a.str("user_id")?), sv(&a.str("event_type")?), iv(limit(10)?)], &["payload"], false)?,
        "count_biography_events" => count(c, "SELECT COUNT(*) AS c FROM biography_event WHERE user_id=? AND event_type=?", vec![sv(&a.str("user_id")?), sv(&a.str("event_type")?)])?,
        // ---- контекст ----
        "get_context_topic" => row(c, "SELECT * FROM context_topic WHERE user_id=? AND topic=?", vec![sv(&a.str("user_id")?), sv(&a.str("topic")?)])?.map(Value::Object).unwrap_or(Value::Null),
        "list_context_topics" => list(c, "SELECT * FROM context_topic WHERE user_id=? ORDER BY topic", vec![sv(&a.str("user_id")?)], &[], false)?,
        "touch_context_topic" => {
            let at = dt_or_now(a.get("activity_at"))?;
            exec(c, "INSERT INTO context_topic (user_id, topic, last_activity, total_instances) VALUES (?,?,?,1) ON CONFLICT(user_id, topic) DO UPDATE SET last_activity=max(COALESCE(last_activity, ?), ?), total_instances=total_instances+1", vec![sv(&a.str("user_id")?), sv(&a.str("topic")?), sv(&at), sv(&at), sv(&at)])?;
            Value::Null
        }
        "record_context_instance" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            json!(exec(c, "INSERT INTO context_instance (user_id, topic, query, response, type, source, created_at) VALUES (?,?,?,?,?,?,?)", vec![sv(&a.str("user_id")?), sv(&a.str("topic")?), sv(&a.str("query")?), sv(&a.str("response")?), osv(a.opt_str("type_")?.as_deref()), osv(a.opt_str("source")?.as_deref()), sv(&created_at)])?.1)
        }
        "list_recent_context_instances" => list(c, "SELECT * FROM context_instance WHERE user_id=? AND topic=? ORDER BY created_at DESC, instance_id DESC LIMIT ?", vec![sv(&a.str("user_id")?), sv(&a.str("topic")?), iv(limit(5)?)], &[], false)?,
        // ---- журнал решений ----
        "create_decision_journal_entry" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            exec(
                c,
                "INSERT INTO decision_journal_entry (decision_id, user_id, event_type, event_text, context, analysis, alternatives, decision, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                vec![sv(&a.str("decision_id")?), sv(&a.str("user_id")?), sv(&a.str("event_type")?), sv(&a.str("event_text")?), jv(Some(a.get("context").unwrap_or(&Value::Null))), jv(Some(a.get("analysis").unwrap_or(&Value::Null))), jv(Some(a.get("alternatives").unwrap_or(&Value::Null))), sv(&a.str("decision")?), fv(Some(a.opt_f64("confidence")?.unwrap_or(0.7))), sv(&created_at), sv(&created_at)],
            )?;
            Value::Null
        }
        "get_decision_journal_entry" => one(c, "SELECT * FROM decision_journal_entry WHERE decision_id=?", vec![sv(&a.str("decision_id")?)], &JOURNAL_JSON)?,
        "update_decision_journal_outcome" | "update_decision_journal_self_correction" => {
            let col = if name.ends_with("outcome") { "outcome" } else { "self_correction" };
            let at = dt_or_now(a.get("updated_at"))?;
            let (n, _) = exec(c, &format!("UPDATE decision_journal_entry SET {col}=?, updated_at=? WHERE decision_id=?"), vec![jv(Some(a.get(col).unwrap_or(&Value::Null))), sv(&at), sv(&a.str("decision_id")?)])?;
            json!(n > 0)
        }
        "list_decision_journal_entries" => list(c, "SELECT * FROM decision_journal_entry WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?", vec![sv(&a.str("user_id")?), iv(limit(1000)?)], &JOURNAL_JSON, true)?,
        // ---- опыт ----
        "create_experience" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            exec(c, "INSERT INTO experience (experience_id, user_id, speech_act, topic, query, response, context, created_at) VALUES (?,?,?,?,?,?,?,?)", vec![sv(&a.str("experience_id")?), sv(&a.str("user_id")?), sv(&a.str("speech_act")?), sv(&a.str("topic")?), sv(&a.str("query")?), sv(&a.str("response")?), jv(a.get("context")), sv(&created_at)])?;
            Value::Null
        }
        "list_experiences" => list(c, "SELECT * FROM experience WHERE user_id=? ORDER BY created_at ASC, rowid ASC", vec![sv(&a.str("user_id")?)], &["context"], false)?,
        "increment_experience_used" => {
            exec(c, "UPDATE experience SET used_count = used_count + 1 WHERE experience_id=?", vec![sv(&a.str("experience_id")?)])?;
            Value::Null
        }
        "update_experience_success" => {
            exec(c, "UPDATE experience SET user_reaction=?, success=? WHERE experience_id=?", vec![sv(&a.str("user_reaction")?), fv(a.opt_f64("success")?), sv(&a.str("experience_id")?)])?;
            Value::Null
        }
        // ---- секретный архив ----
        "create_secret_archive_question" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            exec(c, "INSERT INTO secret_archive_question (question_id, user_id, query, reason, context, created_at) VALUES (?,?,?,?,?,?)", vec![sv(&a.str("question_id")?), sv(&a.str("user_id")?), sv(&a.str("query")?), sv(&a.str("reason")?), jv(a.get("context")), sv(&created_at)])?;
            Value::Null
        }
        "answer_secret_archive_question" => {
            let at = dt_or_now(a.get("answer_time"))?;
            json!(exec(c, "UPDATE secret_archive_question SET answered=1, answer=?, answer_time=? WHERE question_id=?", vec![sv(&a.str("answer")?), sv(&at), sv(&a.str("question_id")?)])?.0 > 0)
        }
        "list_secret_archive_questions" => match a.get("answered") {
            None | Some(Value::Null) => list(c, "SELECT * FROM secret_archive_question WHERE user_id=? ORDER BY created_at ASC, rowid ASC", vec![sv(&a.str("user_id")?)], &["context"], false)?,
            Some(v) => list(c, "SELECT * FROM secret_archive_question WHERE user_id=? AND answered=? ORDER BY created_at ASC, rowid ASC", vec![sv(&a.str("user_id")?), bv(a.bool_or("answered", false) && !matches!(v, Value::Null))], &["context"], false)?,
        },
        // ---- внутреннее состояние ----
        "get_inner_state" => get_inner_state(c, &a.str("user_id")?)?,
        "get_or_create_inner_state" => {
            let uid = a.str("user_id")?;
            exec(c, "INSERT OR IGNORE INTO inner_state (user_id, updated_at) VALUES (?,?)", vec![sv(&uid), sv(&dt_or_now(a.get("updated_at"))?)])?;
            get_inner_state(c, &uid)?
        }
        "update_inner_state" => {
            let uid = a.str("user_id")?;
            let mut sets = Vec::new();
            let mut args = Vec::new();
            for (k, v) in a.0.iter() {
                if k == "user_id" || k == "updated_at" {
                    continue;
                }
                if !INNER_FIELDS.contains(&k.as_str()) {
                    return Err(format!("ValueError: unknown inner_state field: {}", py_repr_str(k)));
                }
                sets.push(format!("{k}=?"));
                args.push(match v {
                    Value::Null => Sql::Null,
                    Value::String(s) => sv(s),
                    n if INNER_FLOATS.contains(&k.as_str()) => fv(n.as_f64()),
                    other => sv(&other.to_string()),
                });
            }
            if !sets.is_empty() {
                sets.push("updated_at=?".into());
                args.push(sv(&dt_or_now(a.get("updated_at"))?));
                args.push(sv(&uid));
                exec(c, &format!("UPDATE inner_state SET {} WHERE user_id=?", sets.join(", ")), args)?;
            }
            Value::Null
        }
        "record_inner_state_event" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            json!(exec(c, "INSERT INTO inner_state_event (user_id, event_type, description, sincerity, weight, resolved, created_at) VALUES (?,?,?,?,?,?,?)", vec![sv(&a.str("user_id")?), sv(&a.str("event_type")?), sv(&a.str("description")?), fv(Some(a.opt_f64("sincerity")?.unwrap_or(0.5))), fv(Some(a.opt_f64("weight")?.unwrap_or(0.0))), bv(a.bool_or("resolved", false)), sv(&created_at)])?.1)
        }
        "list_inner_state_events" => list(c, "SELECT * FROM inner_state_event WHERE user_id=? ORDER BY created_at DESC, event_id DESC LIMIT ?", vec![sv(&a.str("user_id")?), iv(limit(200)?)], &[], true)?,
        "list_inner_state_events_in_order" => list(c, "SELECT * FROM inner_state_event WHERE user_id=? ORDER BY event_id ASC", vec![sv(&a.str("user_id")?)], &[], false)?,
        _ => return Ok(None),
    }))
}
