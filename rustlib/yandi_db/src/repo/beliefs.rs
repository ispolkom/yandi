//! Убеждения (belief), история их оценок, события перепроверки — перенос группы `repositories.py`.
use rusqlite::Connection;
use serde_json::{json, Value};

use super::*;

const BELIEF_JSON: [&str; 3] = ["evidence_for", "evidence_against", "claim_ids"];

fn decode(mut r: Row) -> R<Value> {
    decode_json_cols(&mut r, &BELIEF_JSON)?;
    Ok(Value::Object(r))
}

fn decode_all(rs: Vec<Row>) -> R<Value> {
    Ok(json!(rs.into_iter().map(decode).collect::<R<Vec<_>>>()?))
}

/// `round(float(x or 0), 2)` Python (по точному двоичному значению, ничьи к чётному — как `{:.2}` Rust).
fn round2(x: Option<f64>) -> Value {
    let v = x.unwrap_or(0.0);
    json!(format!("{v:.2}").parse::<f64>().unwrap_or(v))
}

pub fn dispatch(c: &Connection, name: &str, a: &A) -> R<Option<Value>> {
    Ok(Some(match name {
        "upsert_belief" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let updated_at = dt_or_now(a.get("updated_at"))?;
            exec(
                c,
                "INSERT INTO belief (belief_id, topic, statement, confidence, status, evidence_for, evidence_against, claim_ids, prior, likelihood, contradiction_score, decay_factor, superseded_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) \
                 ON CONFLICT(belief_id) DO UPDATE SET statement=excluded.statement, confidence=excluded.confidence, status=excluded.status, evidence_for=excluded.evidence_for, evidence_against=excluded.evidence_against, claim_ids=excluded.claim_ids, prior=excluded.prior, likelihood=excluded.likelihood, contradiction_score=excluded.contradiction_score, decay_factor=excluded.decay_factor, superseded_by=excluded.superseded_by, updated_at=excluded.updated_at",
                vec![
                    sv(&a.str("belief_id")?),
                    sv(&a.str("topic")?),
                    sv(&a.str("statement")?),
                    fv(a.opt_f64("confidence")?),
                    sv(&a.opt_str("status")?.unwrap_or_else(|| "active".into())),
                    jv(a.get("evidence_for")),
                    jv(a.get("evidence_against")),
                    jv(a.get("claim_ids")),
                    fv(Some(a.opt_f64("prior")?.unwrap_or(0.5))),
                    fv(Some(a.opt_f64("likelihood")?.unwrap_or(0.5))),
                    fv(Some(a.opt_f64("contradiction_score")?.unwrap_or(0.0))),
                    fv(Some(a.opt_f64("decay_factor")?.unwrap_or(0.95))),
                    osv(a.opt_str("superseded_by")?.as_deref()),
                    sv(&created_at),
                    sv(&updated_at),
                ],
            )?;
            Value::Null
        }
        "get_belief" => match row(c, "SELECT * FROM belief WHERE belief_id=?", vec![sv(&a.str("belief_id")?)])? {
            Some(r) => decode(r)?,
            None => Value::Null,
        },
        "list_beliefs_by_topic" => {
            let statuses: Vec<String> = match a.get("statuses") {
                Some(Value::Array(x)) if !x.is_empty() => x.iter().map(|v| v.as_str().unwrap_or("").to_string()).collect(),
                _ => vec!["active".into(), "revised".into()],
            };
            let sql = format!("SELECT * FROM belief WHERE topic=? AND status IN ({}) ORDER BY created_at ASC, belief_id ASC", vec!["?"; statuses.len()].join(", "));
            let mut args = vec![sv(&a.str("topic")?)];
            args.extend(statuses.iter().map(|s| sv(s)));
            decode_all(rows(c, &sql, args)?)?
        }
        "list_belief_history" => json!(rows(c, "SELECT * FROM belief_assessment_history WHERE belief_id=? ORDER BY created_at ASC, history_id ASC", vec![sv(&a.str("belief_id")?)])?),
        "list_all_beliefs" => decode_all(rows(c, "SELECT * FROM belief ORDER BY belief_id", vec![])?)?,
        "list_active_beliefs" => decode_all(rows(c, "SELECT * FROM belief WHERE status='active' ORDER BY created_at ASC, belief_id ASC", vec![])?)?,
        "list_contradictory_beliefs" => decode_all(rows(c, "SELECT * FROM belief WHERE contradiction_score >= ? ORDER BY contradiction_score DESC, belief_id ASC", vec![Sql::Real(a.opt_f64("min_score")?.unwrap_or(0.5))])?)?,
        "get_belief_stats" => {
            let t = row(c, "SELECT COUNT(*) AS total, SUM(status='active') AS active, SUM(status='revised') AS revised, SUM(status='superseded') AS superseded FROM belief", vec![])?.unwrap_or_default();
            // AVG(FLOAT) в MySQL считается по ТОЧНЫМ значениям f32 (не по 6-значным десятичным, которые видит клиент)
            let avg = |col: &str| -> R<Option<f64>> {
                let vals: Vec<f64> = rows(c, &format!("SELECT {col} AS v FROM belief WHERE {col} IS NOT NULL"), vec![])?.iter().filter_map(|r| r["v"].as_f64()).map(|x| (x as f32) as f64).collect();
                Ok(if vals.is_empty() { None } else { Some(vals.iter().sum::<f64>() / vals.len() as f64) })
            };
            let (avg_confidence, avg_contradiction) = (avg("confidence")?, avg("contradiction_score")?);
            let topics = rows(c, "SELECT topic, COUNT(*) AS c FROM belief GROUP BY topic", vec![])?;
            let contra = row(c, "SELECT COUNT(*) AS c FROM belief WHERE contradiction_score >= 0.5", vec![])?.unwrap_or_default();
            let n = |r: &Row, k: &str| r.get(k).and_then(|v| v.as_i64()).unwrap_or(0);
            let mut tm = Map::new();
            for r in &topics {
                tm.insert(r["topic"].as_str().unwrap_or("").to_string(), r["c"].clone());
            }
            json!({
                "total": n(&t, "total"), "active": n(&t, "active"), "revised": n(&t, "revised"), "superseded": n(&t, "superseded"),
                "contradictory": n(&contra, "c"), "topics": Value::Object(tm),
                "avg_confidence": round2(avg_confidence),
                "avg_contradiction": round2(avg_contradiction),
            })
        }
        "record_belief_assessment" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let reason = a.opt_str("reason")?.map(|s| cut_chars(&s, 255)).filter(|s| !s.is_empty());
            let id = exec(
                c,
                "INSERT INTO belief_assessment_history (belief_id, run_id, old_confidence, new_confidence, reason, change_type, created_at) VALUES (?,?,?,?,?,?,?)",
                vec![sv(&a.str("belief_id")?), osv(a.opt_str("run_id")?.as_deref()), fv(a.opt_f64("old_confidence")?), fv(a.opt_f64("new_confidence")?), osv(reason.as_deref()), sv(&a.str("change_type")?), sv(&created_at)],
            )?
            .1;
            json!(id)
        }
        "record_recheck_event" => {
            let started_at = dt_or_now(a.get("started_at"))?;
            let id = exec(
                c,
                "INSERT INTO recheck_event (family_id, run_id, trigger_reason, started_at, outcome, reason) VALUES (?,?,?,?,?,?)",
                vec![sv(&a.str("family_id")?), osv(a.opt_str("run_id")?.as_deref()), osv(a.opt_str("trigger_reason")?.as_deref()), sv(&started_at), sv(&a.str("outcome")?), osv(a.opt_str("reason")?.as_deref())],
            )?
            .1;
            json!(id)
        }
        _ => return Ok(None),
    }))
}
