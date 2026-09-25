//! Трассы запусков, отложенная проверка, связи доказательств, ошибки, журнал решений, наблюдения ИИ — перенос второй группы `repositories.py`.
use rusqlite::Connection;
use serde_json::{json, Value};

use super::*;

const TRACE_JSON: [&str; 8] = ["execution", "reasoning", "cost", "epistemic", "outcome", "learning", "confidence_evolution", "rejected_claims"];

fn opt_or_none(s: Option<String>) -> Option<String> {
    s.filter(|x| !x.is_empty())
}

pub fn dispatch(c: &Connection, name: &str, a: &A) -> R<Option<Value>> {
    Ok(Some(match name {
        "record_trace_record" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let mut args = vec![sv(&a.str("run_id")?)];
            for k in TRACE_JSON {
                args.push(jv(a.get(k)));
            }
            args.push(iv(a.opt_i64("claims_filtered_count")?.unwrap_or(0)));
            args.push(iv(a.opt_i64("claims_rejected_count")?.unwrap_or(0)));
            args.push(sv(&created_at));
            exec(c, "INSERT INTO trace_record (run_id, execution, reasoning, cost, epistemic, outcome, learning, confidence_evolution, rejected_claims, claims_filtered_count, claims_rejected_count, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", args)?;
            Value::Null
        }
        "get_trace_record" => match row(c, "SELECT * FROM trace_record WHERE run_id=?", vec![sv(&a.str("run_id")?)])? {
            Some(mut r) => {
                decode_json_cols(&mut r, &TRACE_JSON)?;
                Value::Object(r)
            }
            None => Value::Null,
        },
        "record_delayed_validation_event" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let run_id = opt_or_none(a.opt_str("run_id")?);
            let reason = a.opt_str("reason")?.map(|s| cut_chars(&s, 500)).filter(|s| !s.is_empty());
            let raw = a.opt_str("raw")?.map(|s| cut_chars(&s, 2000)).filter(|s| !s.is_empty());
            exec(
                c,
                "INSERT INTO delayed_validation_event (event_id, run_id, trace_found, original_trust, source, verdict, reason, raw, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                vec![sv(&a.str("event_id")?), osv(run_id.as_deref()), bv(a.bool_or("trace_found", false)), osv(a.opt_str("original_trust")?.as_deref()), sv(&a.str("source")?), sv(&a.str("verdict")?), osv(reason.as_deref()), osv(raw.as_deref()), sv(&created_at)],
            )?;
            Value::Null
        }
        "list_delayed_validation_events" => {
            let mut r = rows(c, "SELECT * FROM delayed_validation_event WHERE run_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?", vec![sv(&a.str("run_id")?), iv(a.opt_i64("limit")?.unwrap_or(30))])?;
            r.reverse();
            json!(r)
        }
        "record_evidence_relation" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let id = exec(
                c,
                "INSERT INTO evidence_relation (claim_id, observation_id, relation, directness, evidence_eligible, evidence_role, counted_via, created_at) VALUES (?,?,?,?,?,?,?,?)",
                vec![sv(&a.str("claim_id")?), iv(a.i64("observation_id")?), sv(&a.str("relation")?), fv(a.opt_f64("directness")?), bv(a.bool_or("evidence_eligible", false)), osv(a.opt_str("evidence_role")?.as_deref()), osv(a.opt_str("counted_via")?.as_deref()), sv(&created_at)],
            )?
            .1;
            json!(id)
        }
        "record_run_error" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let msg = cut_chars(&a.opt_str("short_message")?.unwrap_or_default(), 500);
            exec(c, "INSERT INTO run_error (run_id, failed_stage, error_class, short_message, created_at) VALUES (?,?,?,?,?)", vec![sv(&a.str("run_id")?), sv(&a.str("failed_stage")?), sv(&a.str("error_class")?), sv(&msg), sv(&created_at)])?;
            Value::Null
        }
        "record_decision_event" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            exec(
                c,
                "INSERT INTO decision_event (event_id, run_id, event_type, entity_type, entity_id, verdict, domain, confidence, delta, delta_factors, reason, meta, parent_event_id, duration_ms, policy_snapshot, policy_version, orchestrator_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                vec![
                    sv(&a.str("event_id")?),
                    sv(&a.str("run_id")?),
                    sv(&a.str("event_type")?),
                    sv(&a.str("entity_type")?),
                    sv(&a.str("entity_id")?),
                    osv(a.opt_str("verdict")?.as_deref()),
                    osv(a.opt_str("domain")?.as_deref()),
                    fv(a.opt_f64("confidence")?),
                    fv(a.opt_f64("delta")?),
                    jv(a.get("delta_factors")),
                    osv(a.opt_str("reason")?.as_deref()),
                    jv(a.get("meta")),
                    osv(a.opt_str("parent_event_id")?.as_deref()),
                    oiv(a.opt_i64("duration_ms")?),
                    jv(a.get("policy_snapshot")),
                    osv(a.opt_str("policy_version")?.as_deref()),
                    osv(a.opt_str("orchestrator_version")?.as_deref()),
                    sv(&created_at),
                ],
            )?;
            Value::Null
        }
        "get_decision_trace" => {
            let mut r = rows(c, "SELECT * FROM decision_event WHERE run_id=? ORDER BY created_at ASC, event_id ASC", vec![sv(&a.str("run_id")?)])?;
            for x in r.iter_mut() {
                decode_json_cols(x, &["delta_factors", "meta", "policy_snapshot"])?;
            }
            json!(r)
        }
        "record_ai_observation" => {
            let observed_at = dt_or_now(a.get("observed_at"))?;
            let d = |k: &str, def: &str| -> R<String> { Ok(a.opt_str(k)?.unwrap_or_else(|| def.to_string())) };
            let id = exec(
                c,
                "INSERT INTO ai_observation (provider, model_id, run_id, prompt_identity, answer_excerpt, provenance_mode_reported, live_search_used_reported, provenance_parse_status, observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                vec![sv(&a.str("provider")?), sv(&a.str("model_id")?), osv(a.opt_str("run_id")?.as_deref()), osv(a.opt_str("prompt_identity")?.as_deref()), osv(a.opt_str("answer_excerpt")?.as_deref()), sv(&d("provenance_mode_reported", "UNKNOWN")?), sv(&d("live_search_used_reported", "UNKNOWN")?), sv(&d("provenance_parse_status", "missing")?), sv(&observed_at)],
            )?
            .1;
            json!(id)
        }
        "record_ai_reported_source" => {
            let id = exec(c, "INSERT INTO ai_reported_source (ai_observation_id, ordinal, reported_name, reported_uri) VALUES (?,?,?,?)", vec![iv(a.i64("ai_observation_id")?), oiv(a.opt_i64("ordinal")?), osv(a.opt_str("reported_name")?.as_deref()), osv(a.opt_str("reported_uri")?.as_deref())])?.1;
            json!(id)
        }
        "get_ai_observations_for_run" => {
            let mut obs = rows(c, "SELECT ai_observation_id, provider, model_id, run_id, prompt_identity, answer_excerpt, provenance_mode_reported, live_search_used_reported, provenance_parse_status, observed_at FROM ai_observation WHERE run_id=? ORDER BY ai_observation_id", vec![sv(&a.str("run_id")?)])?;
            for o in obs.iter_mut() {
                let srcs = rows(c, "SELECT ordinal, reported_name, reported_uri FROM ai_reported_source WHERE ai_observation_id=? ORDER BY ordinal, ai_reported_source_id", vec![iv(o["ai_observation_id"].as_i64().unwrap_or(0))])?;
                o.insert("reported_sources".into(), json!(srcs));
            }
            json!(obs)
        }
        _ => return Ok(None),
    }))
}
