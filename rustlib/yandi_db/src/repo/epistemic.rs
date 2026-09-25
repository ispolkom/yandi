//! Вопросы, запуски проверки, версии ответов, семейства утверждений, источники, наблюдения — перенос первой группы `repositories.py`.
use rusqlite::Connection;
use serde_json::{json, Value};
use yandi_rs::claim_identity::canonicalize_claim_text;

use super::*;

pub const REJECTION_REASONS: [&str; 7] = ["unrelated", "no_content", "low_quality", "stoplisted", "transport_failed", "duplicate", "below_eligibility_threshold"];

fn question_hash(raw_text: &str) -> String {
    sha256_hex(&canonicalize_claim_text(raw_text))
}

pub fn resolve_question(c: &Connection, raw_text: &str, anonymized_text: Option<&str>, asked_at: Option<&Value>, session_id: Option<&str>) -> R<Value> {
    let asked_at = dt_or_now(asked_at)?;
    let h = question_hash(raw_text);
    let question_id = match row(c, "SELECT question_id FROM question WHERE canonical_hash = ?", vec![sv(&h)])? {
        Some(r) => r["question_id"].as_i64().unwrap_or(0),
        None => exec(c, "INSERT INTO question (canonical_hash, first_asked_at) VALUES (?, ?)", vec![sv(&h), sv(&asked_at)])?.1,
    };
    let occurrence_id = exec(
        c,
        "INSERT INTO question_occurrence (question_id, raw_text, anonymized_text, asked_at, session_id) VALUES (?, ?, ?, ?, ?)",
        vec![iv(question_id), sv(raw_text), osv(anonymized_text), sv(&asked_at), osv(session_id)],
    )?
    .1;
    Ok(json!({"question_id": question_id, "occurrence_id": occurrence_id}))
}

pub fn start_run(c: &Connection, a: &A) -> R<Value> {
    let started_at = dt_or_now(a.get("started_at"))?;
    exec(
        c,
        "INSERT INTO verification_run (run_id, occurrence_id, started_at, status, web_enabled, validation_enabled, pipeline_version, schema_version) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
        vec![sv(&a.str("run_id")?), iv(a.i64("occurrence_id")?), sv(&started_at), bv(a.bool_or("web_enabled", false)), bv(a.bool_or("validation_enabled", false)), osv(a.opt_str("pipeline_version")?.as_deref()), iv(a.opt_i64("schema_version")?.unwrap_or(1))],
    )?;
    Ok(Value::Null)
}

pub fn complete_run(c: &Connection, a: &A) -> R<Value> {
    let completed_at = dt_or_now(a.get("completed_at"))?;
    exec(
        c,
        "UPDATE verification_run SET status='completed', completed_at=?, final_answer_id=? WHERE run_id=? AND status='running'",
        vec![sv(&completed_at), oiv(a.opt_i64("final_answer_id")?), sv(&a.str("run_id")?)],
    )?;
    Ok(Value::Null)
}

pub fn fail_run(c: &Connection, a: &A) -> R<Value> {
    let outcome = a.opt_str("outcome")?.unwrap_or_else(|| "failed".into());
    if outcome != "failed" && outcome != "aborted" {
        return Err("AssertionError".into());
    }
    let completed_at = dt_or_now(a.get("completed_at"))?;
    exec(
        c,
        "UPDATE verification_run SET status=?, completed_at=?, failed_stage=?, error_class=? WHERE run_id=?",
        vec![sv(&outcome), sv(&completed_at), sv(&a.str("failed_stage")?), sv(&a.str("error_class")?), sv(&a.str("run_id")?)],
    )?;
    Ok(Value::Null)
}

/// Запуски в статусе running дольше `older_than_seconds` → aborted (НИКОГДА не completed). Возвращает число строк.
pub fn reconcile_stale_running_runs(c: &Connection, older_than_seconds: i64) -> R<Value> {
    let now = now_text();
    let (n, _) = exec(
        c,
        "UPDATE verification_run SET status='aborted', completed_at=? WHERE status='running' AND started_at < datetime(?, ?)",
        vec![sv(&now), sv(&now), sv(&format!("-{older_than_seconds} seconds"))],
    )?;
    Ok(json!(n))
}

pub fn record_answer_version(c: &Connection, question_id: i64, answer_text: &str, run_id: &str, created_at: Option<&Value>) -> R<Value> {
    let created_at = dt_or_now(created_at)?;
    let h = sha256_hex(answer_text);
    let latest = row(c, "SELECT answer_id, answer_hash, version_number FROM answer_version WHERE question_id=? ORDER BY version_number DESC LIMIT 1", vec![iv(question_id)])?;
    if let Some(l) = &latest {
        if l["answer_hash"].as_str() == Some(h.as_str()) {
            return Ok(l["answer_id"].clone());
        }
    }
    let version_number = latest.as_ref().map(|l| l["version_number"].as_i64().unwrap_or(0) + 1).unwrap_or(1);
    let supersedes = latest.as_ref().and_then(|l| l["answer_id"].as_i64());
    let id = exec(
        c,
        "INSERT INTO answer_version (question_id, version_number, answer_text, answer_hash, created_by_run_id, supersedes_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        vec![iv(question_id), iv(version_number), sv(answer_text), sv(&h), sv(run_id), oiv(supersedes), sv(&created_at)],
    )?
    .1;
    Ok(json!(id))
}

pub fn record_answer_assessment(c: &Connection, a: &A) -> R<Value> {
    let created_at = dt_or_now(a.get("created_at"))?;
    let id = exec(
        c,
        "INSERT INTO answer_assessment (answer_id, run_id, synthesizer_strand, trust_gate_strand, canonical_trust, diverged, stricter_strand, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        vec![
            iv(a.i64("answer_id")?),
            sv(&a.str("run_id")?),
            osv(a.opt_str("synthesizer_strand")?.as_deref()),
            osv(a.opt_str("trust_gate_strand")?.as_deref()),
            sv(&a.str("canonical_trust")?),
            bv(a.bool_or("diverged", false)),
            osv(a.opt_str("stricter_strand")?.as_deref()),
            osv(a.opt_str("reason")?.as_deref()),
            sv(&created_at),
        ],
    )?
    .1;
    Ok(json!(id))
}

pub fn get_or_create_claim_family(c: &Connection, a: &A) -> R<Value> {
    let created_at = dt_or_now(a.get("created_at"))?;
    exec_ignore(
        c,
        "INSERT OR IGNORE INTO claim_family (family_id, domain, canonical_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        vec![sv(&a.str("family_id")?), sv(&a.str("domain")?), sv(&a.str("canonical_text")?), sv(&created_at), sv(&created_at)],
    )?;
    Ok(Value::Null)
}

pub fn link_family_member(c: &Connection, a: &A) -> R<Value> {
    let linked_at = dt_or_now(a.get("linked_at"))?;
    exec_ignore(c, "INSERT OR IGNORE INTO family_member (family_id, claim_id, linked_at) VALUES (?, ?, ?)", vec![sv(&a.str("family_id")?), sv(&a.str("claim_id")?), sv(&linked_at)])?;
    Ok(Value::Null)
}

pub fn list_claim_families_by_domain(c: &Connection, domain: &str) -> R<Value> {
    // `created_at ASC` у MySQL при равных значениях отдаёт порядок вставки (InnoDB по PK); rowid у SQLite — то же
    Ok(json!(rows(c, "SELECT family_id, canonical_text FROM claim_family WHERE domain=? ORDER BY created_at ASC, rowid ASC", vec![sv(domain)])?))
}

pub fn get_claim_family(c: &Connection, family_id: &str) -> R<Value> {
    let Some(mut family) = row(c, "SELECT * FROM claim_family WHERE family_id=?", vec![sv(family_id)])? else {
        return Ok(Value::Null);
    };
    let members = rows(c, "SELECT claim_id, linked_at FROM family_member WHERE family_id=? ORDER BY linked_at ASC, rowid ASC", vec![sv(family_id)])?;
    family.insert("members".into(), json!(members));
    Ok(Value::Object(family))
}

pub fn record_claim_occurrence(c: &Connection, a: &A) -> R<Value> {
    exec(
        c,
        "INSERT INTO claim_occurrence (claim_id, run_id, claim_text, content_hash, claim_type, claim_confidence, verification_status, family_id, query_context, support_count, contradiction_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        vec![
            sv(&a.str("claim_id")?),
            sv(&a.str("run_id")?),
            sv(&a.str("claim_text")?),
            osv(a.opt_str("content_hash")?.as_deref()),
            osv(a.opt_str("claim_type")?.as_deref()),
            fv(a.opt_f64("claim_confidence")?),
            osv(a.opt_str("verification_status")?.as_deref()),
            osv(a.opt_str("family_id")?.as_deref()),
            osv(a.opt_str("query_context")?.as_deref()),
            iv(a.opt_i64("support_count")?.unwrap_or(0)),
            iv(a.opt_i64("contradiction_count")?.unwrap_or(0)),
        ],
    )?;
    Ok(Value::Null)
}

pub fn get_or_create_resource(c: &Connection, a: &A) -> R<Value> {
    let observed_at = dt_or_now(a.get("observed_at"))?;
    let uri = a.opt_str("canonical_uri")?;
    let uri_hash = uri.as_deref().filter(|u| !u.is_empty()).map(sha256_hex);
    if let Some(h) = &uri_hash {
        if let Some(r) = row(c, "SELECT resource_id FROM source_resource WHERE uri_hash=?", vec![sv(h)])? {
            return Ok(r["resource_id"].clone());
        }
    }
    let id = exec(
        c,
        "INSERT INTO source_resource (resource_type, canonical_uri, uri_hash, node_id, validator_id, model_id, first_observed_at) VALUES (?,?,?,?,?,?,?)",
        vec![sv(&a.str("resource_type")?), osv(uri.as_deref()), osv(uri_hash.as_deref()), osv(a.opt_str("node_id")?.as_deref()), osv(a.opt_str("validator_id")?.as_deref()), osv(a.opt_str("model_id")?.as_deref()), sv(&observed_at)],
    )?
    .1;
    Ok(json!(id))
}

pub fn find_observation_id_for_replay(c: &Connection, resource_id: i64, origin_run_id: Option<&str>) -> R<Value> {
    let Some(run) = origin_run_id.filter(|r| !r.is_empty()) else {
        return Ok(Value::Null);
    };
    Ok(row(c, "SELECT observation_id FROM source_observation WHERE resource_id=? AND run_id=? ORDER BY observation_id ASC LIMIT 1", vec![iv(resource_id), sv(run)])?.map(|r| r["observation_id"].clone()).unwrap_or(Value::Null))
}

pub fn record_source_observation(c: &Connection, a: &A) -> R<Value> {
    if let Some(r) = a.opt_str("rejection_reason")? {
        if !REJECTION_REASONS.contains(&r.as_str()) {
            return Err(format!("ValueError: rejection_reason '{r}' not in controlled vocabulary"));
        }
    }
    let observed_at = dt_or_now(a.get("observed_at"))?;
    // `retrieval_claim_id or None`: пустая строка тоже NULL
    let retrieval_claim_id = a.opt_str("retrieval_claim_id")?.filter(|s| !s.is_empty());
    let id = exec(
        c,
        "INSERT INTO source_observation (resource_id, run_id, observation_route, origin_observation_id, observed_at, source_class, quality_score, content_excerpt, rejection_reason, evidence_id, source_title, retrieval_query, retrieval_rank, relevance_to_query, authority, traceability, primaryness, is_meta_pipeline_output, is_subject_matter_evidence, source_cluster_id, origin_source_cluster_id, retrieval_claim_id, route_side, subject_entities, fact_candidates, supports_query_aspect) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        vec![
            iv(a.i64("resource_id")?),
            sv(&a.str("run_id")?),
            sv(&a.str("observation_route")?),
            oiv(a.opt_i64("origin_observation_id")?),
            sv(&observed_at),
            osv(a.opt_str("source_class")?.as_deref()),
            fv(a.opt_f64("quality_score")?),
            osv(a.opt_str("content_excerpt")?.as_deref()),
            osv(a.opt_str("rejection_reason")?.as_deref()),
            osv(a.opt_str("evidence_id")?.as_deref()),
            osv(a.opt_str("source_title")?.as_deref()),
            osv(a.opt_str("retrieval_query")?.as_deref()),
            oiv(a.opt_i64("retrieval_rank")?),
            fv(a.opt_f64("relevance_to_query")?),
            fv(a.opt_f64("authority")?),
            fv(a.opt_f64("traceability")?),
            fv(a.opt_f64("primaryness")?),
            bv(a.bool_or("is_meta_pipeline_output", false)),
            bv(a.bool_or("is_subject_matter_evidence", true)),
            osv(a.opt_str("source_cluster_id")?.as_deref()),
            osv(a.opt_str("origin_source_cluster_id")?.as_deref()),
            osv(retrieval_claim_id.as_deref()),
            osv(a.opt_str("route_side")?.as_deref()),
            jv(a.get("subject_entities")),
            jv(a.get("fact_candidates")),
            jv(a.get("supports_query_aspect")),
        ],
    )?
    .1;
    Ok(json!(id))
}

const SOURCE_OBSERVATION_JSON: [&str; 3] = ["subject_entities", "fact_candidates", "supports_query_aspect"];

pub fn get_source_observation(c: &Connection, observation_id: i64) -> R<Value> {
    match row(c, "SELECT * FROM source_observation WHERE observation_id=?", vec![iv(observation_id)])? {
        Some(mut r) => {
            decode_json_cols(&mut r, &SOURCE_OBSERVATION_JSON)?;
            Ok(Value::Object(r))
        }
        None => Ok(Value::Null),
    }
}

pub fn get_resource(c: &Connection, resource_id: i64) -> R<Option<Row>> {
    row(c, "SELECT * FROM source_resource WHERE resource_id=?", vec![iv(resource_id)])
}

pub fn find_claim_occurrences_by_content_hash(c: &Connection, content_hash: &str, limit: Option<i64>, exclude_run_id: Option<&str>) -> R<Value> {
    let mut sql = "SELECT co.*, vr.started_at AS occurrence_observed_at FROM claim_occurrence co JOIN verification_run vr ON vr.run_id = co.run_id WHERE co.content_hash=?".to_string();
    let mut args = vec![sv(content_hash)];
    if let Some(x) = exclude_run_id.filter(|x| !x.is_empty()) {
        sql += " AND co.run_id != ?";
        args.push(sv(x));
    }
    sql += " ORDER BY vr.started_at DESC";
    if let Some(l) = limit {
        sql += " LIMIT ?";
        args.push(iv(l));
    }
    Ok(json!(rows(c, &sql, args)?))
}

pub fn find_claim_occurrences_by_family(c: &Connection, family_id: &str) -> R<Value> {
    Ok(json!(rows(c, "SELECT co.*, vr.started_at AS occurrence_observed_at FROM claim_occurrence co JOIN verification_run vr ON vr.run_id = co.run_id WHERE co.family_id=? ORDER BY vr.started_at DESC", vec![sv(family_id)])?))
}

/// Идёт по `origin_observation_id` до корня; `(маршрут, run_id, observed_at, source_cluster_id)` корня либо четыре null.
fn resolve_origin_chain(c: &Connection, observation_id: Option<i64>, max_hops: usize) -> R<[Value; 4]> {
    let none = || [Value::Null, Value::Null, Value::Null, Value::Null];
    let Some(mut current) = observation_id else {
        return Ok(none());
    };
    let mut last: Option<Row> = None;
    for _ in 0..max_hops {
        let r = row(c, "SELECT observation_id, run_id, observation_route, observed_at, origin_observation_id, source_cluster_id FROM source_observation WHERE observation_id=?", vec![iv(current)])?;
        let Some(r) = r else {
            return Ok(none());
        };
        if r["origin_observation_id"].is_null() {
            return Ok([r["observation_route"].clone(), r["run_id"].clone(), r["observed_at"].clone(), r["source_cluster_id"].clone()]);
        }
        current = r["origin_observation_id"].as_i64().unwrap_or(0);
        last = Some(r);
    }
    let r = last.expect("max_hops > 0");
    Ok([r["observation_route"].clone(), r["run_id"].clone(), r["observed_at"].clone(), r["source_cluster_id"].clone()])
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|x| x != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `x or default` Python.
fn or(v: Option<&Value>, default: Value) -> Value {
    match v {
        Some(x) if truthy(x) => x.clone(),
        _ => default,
    }
}

pub fn list_evidence_for_claim(c: &Connection, claim_id: &str) -> R<Value> {
    let found = rows(
        c,
        "SELECT er.relation, er.directness, er.evidence_eligible, er.evidence_role, so.* FROM evidence_relation er JOIN source_observation so ON so.observation_id = er.observation_id WHERE er.claim_id=?",
        vec![sv(claim_id)],
    )?;
    let mut results = Vec::new();
    for mut r in found {
        decode_json_cols(&mut r, &SOURCE_OBSERVATION_JSON)?;
        let resource = get_resource(c, r["resource_id"].as_i64().unwrap_or(0))?;
        let [origin_route, origin_run_id, origin_observed_at, origin_cluster_id] = resolve_origin_chain(c, r["observation_id"].as_i64(), 10)?;
        let g = |k: &str| r.get(k);
        let rsrc = |k: &str| resource.as_ref().map(|x| x[k].clone()).unwrap_or(Value::Null);
        results.push(json!({
            "evidence_id": or(g("evidence_id"), json!("")),
            "relation": r["relation"],
            "directness": g("directness").cloned().unwrap_or(Value::Null),
            "evidence_eligible": g("evidence_eligible").map(truthy).unwrap_or(false),
            "evidence_role": g("evidence_role").cloned().unwrap_or(Value::Null),
            "source_type": if resource.is_some() { rsrc("resource_type") } else { json!("web") },
            "source_uri": rsrc("canonical_uri"),
            "source_title": or(g("source_title"), json!("")),
            "content_excerpt": or(g("content_excerpt"), json!("")),
            "relevance_to_query": or(g("relevance_to_query"), json!(0.0)),
            "quality_score": or(g("quality_score"), json!(0.0)),
            "source_class": or(g("source_class"), json!("unknown")),
            "authority": or(g("authority"), json!(0.0)),
            "traceability": or(g("traceability"), json!(0.0)),
            "primaryness": or(g("primaryness"), json!(0.0)),
            "is_meta_pipeline_output": g("is_meta_pipeline_output").map(truthy).unwrap_or(false),
            "is_subject_matter_evidence": g("is_subject_matter_evidence").map(truthy).unwrap_or(true),
            "source_cluster_id": g("source_cluster_id").cloned().unwrap_or(Value::Null),
            "retrieval_claim_id": or(g("retrieval_claim_id"), json!("")),
            "route_side": or(g("route_side"), json!("")),
            "route": r["observation_route"],
            "observed_at": g("observed_at").cloned().unwrap_or(Value::Null),
            "from_memory": r["observation_route"] == json!("local_memory"),
            "origin_route": origin_route,
            "origin_trace_id": origin_run_id,
            "origin_observed_at": origin_observed_at,
            "origin_source_cluster_id": origin_cluster_id,
            "node_id": rsrc("node_id"),
            "validator_id": rsrc("validator_id"),
            "model_id": rsrc("model_id"),
            "subject_entities": or(g("subject_entities"), json!([])),
            "fact_candidates": or(g("fact_candidates"), json!([])),
            "supports_query_aspect": or(g("supports_query_aspect"), json!([])),
        }));
    }
    Ok(json!(results))
}

/// Диспетчер группы: `Ok(None)` — имя не из этой группы.
pub fn dispatch(c: &Connection, name: &str, a: &A) -> R<Option<Value>> {
    Ok(Some(match name {
        "resolve_question" => resolve_question(c, &a.str("raw_text")?, a.opt_str("anonymized_text")?.as_deref(), a.get("asked_at"), a.opt_str("session_id")?.as_deref())?,
        "start_run" => start_run(c, a)?,
        "complete_run" => complete_run(c, a)?,
        "fail_run" => fail_run(c, a)?,
        "reconcile_stale_running_runs" => reconcile_stale_running_runs(c, a.opt_i64("older_than_seconds")?.unwrap_or(3600))?,
        "record_answer_version" => record_answer_version(c, a.i64("question_id")?, &a.str("answer_text")?, &a.str("run_id")?, a.get("created_at"))?,
        "record_answer_assessment" => record_answer_assessment(c, a)?,
        "get_or_create_claim_family" => get_or_create_claim_family(c, a)?,
        "link_family_member" => link_family_member(c, a)?,
        "list_claim_families_by_domain" => list_claim_families_by_domain(c, &a.str("domain")?)?,
        "get_claim_family" => get_claim_family(c, &a.str("family_id")?)?,
        "record_claim_occurrence" => record_claim_occurrence(c, a)?,
        "get_or_create_resource" => get_or_create_resource(c, a)?,
        "find_observation_id_for_replay" => find_observation_id_for_replay(c, a.i64("resource_id")?, a.opt_str("origin_run_id")?.as_deref())?,
        "record_source_observation" => record_source_observation(c, a)?,
        "get_source_observation" => get_source_observation(c, a.i64("observation_id")?)?,
        "get_resource" => get_resource(c, a.i64("resource_id")?)?.map(Value::Object).unwrap_or(Value::Null),
        "find_claim_occurrences_by_content_hash" => find_claim_occurrences_by_content_hash(c, &a.str("content_hash")?, a.opt_i64("limit")?, a.opt_str("exclude_run_id")?.as_deref())?,
        "find_claim_occurrences_by_family" => find_claim_occurrences_by_family(c, &a.str("family_id")?)?,
        "list_evidence_for_claim" => list_evidence_for_claim(c, &a.str("claim_id")?)?,
        _ => return Ok(None),
    }))
}
