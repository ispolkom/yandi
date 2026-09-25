//! API чтения «локальной памяти»: текущий ответ, история, объяснение ответа, запуски, источники, сравнение запусков.
use rusqlite::Connection;
use serde_json::{json, Value};

use super::*;

fn get_sources_for_run(c: &Connection, run_id: &str) -> R<Vec<Row>> {
    rows(c, "SELECT so.*, sr.canonical_uri, sr.resource_type FROM source_observation so JOIN source_resource sr ON sr.resource_id = so.resource_id WHERE so.run_id = ? ORDER BY so.observed_at, so.observation_id", vec![sv(run_id)])
}

/// Отношения для (запуск, ресурс) — множество строк, отсортированное.
fn relations(c: &Connection, run_id: &str, resource_id: i64) -> R<Vec<String>> {
    let mut v: Vec<String> = rows(c, "SELECT er.relation FROM evidence_relation er JOIN source_observation so ON so.observation_id = er.observation_id WHERE so.run_id=? AND so.resource_id=?", vec![sv(run_id), iv(resource_id)])?
        .into_iter()
        .map(|r| r["relation"].as_str().unwrap_or("").to_string())
        .collect();
    v.sort();
    v.dedup();
    Ok(v)
}

/// Словарь Python `{resource_id: строка}`: порядок — по первой вставке, значение — последнее.
fn by_resource(rs: Vec<Row>) -> Vec<(i64, Row)> {
    let mut out: Vec<(i64, Row)> = Vec::new();
    for r in rs {
        let k = r["resource_id"].as_i64().unwrap_or(0);
        match out.iter_mut().find(|(x, _)| *x == k) {
            Some(slot) => slot.1 = r,
            None => out.push((k, r)),
        }
    }
    out
}

pub fn dispatch(c: &Connection, name: &str, a: &A) -> R<Option<Value>> {
    Ok(Some(match name {
        "get_current_answer" => row(c, "SELECT av.*, aa.canonical_trust, aa.diverged, aa.created_at AS assessed_at FROM answer_version av LEFT JOIN answer_assessment aa ON aa.answer_id = av.answer_id WHERE av.question_id = ? ORDER BY av.version_number DESC, aa.created_at DESC, aa.assessment_id DESC LIMIT 1", vec![iv(a.i64("question_id")?)])?.map(Value::Object).unwrap_or(Value::Null),
        "get_answer_history" => {
            let mut versions = rows(c, "SELECT answer_id, version_number, answer_text, created_at, created_by_run_id FROM answer_version WHERE question_id=? ORDER BY version_number", vec![iv(a.i64("question_id")?)])?;
            for v in versions.iter_mut() {
                let asm = rows(c, "SELECT assessment_id, run_id, canonical_trust, diverged, created_at FROM answer_assessment WHERE answer_id=? ORDER BY created_at, assessment_id", vec![iv(v["answer_id"].as_i64().unwrap_or(0))])?;
                v.insert("assessments".into(), json!(asm));
            }
            json!(versions)
        }
        "explain_answer" => {
            let answer_id = a.i64("answer_id")?;
            let Some(answer) = row(c, "SELECT * FROM answer_version WHERE answer_id=?", vec![iv(answer_id)])? else {
                return Ok(Some(json!({})));
            };
            let assessment = row(c, "SELECT * FROM answer_assessment WHERE answer_id=? ORDER BY created_at DESC, assessment_id DESC LIMIT 1", vec![iv(answer_id)])?;
            let run_id = answer["created_by_run_id"].as_str().unwrap_or("").to_string();
            let run = row(c, "SELECT * FROM verification_run WHERE run_id=?", vec![sv(&run_id)])?;
            let mut claims = rows(c, "SELECT * FROM claim_occurrence WHERE run_id=? ORDER BY rowid", vec![sv(&run_id)])?;
            for claim in claims.iter_mut() {
                let ev = rows(
                    c,
                    "SELECT er.*, so.resource_id, so.observation_route, so.observed_at, so.content_excerpt, sr.canonical_uri, sr.resource_type FROM evidence_relation er JOIN source_observation so ON so.observation_id = er.observation_id JOIN source_resource sr ON sr.resource_id = so.resource_id WHERE er.claim_id = ? ORDER BY er.rowid",
                    vec![sv(claim["claim_id"].as_str().unwrap_or(""))],
                )?;
                claim.insert("evidence".into(), json!(ev));
            }
            json!({"answer": answer, "assessment": assessment, "run": run, "claims": claims})
        }
        "get_verification_runs" => json!(rows(c, "SELECT vr.* FROM verification_run vr JOIN question_occurrence qo ON qo.occurrence_id = vr.occurrence_id WHERE qo.question_id = ? ORDER BY vr.started_at, vr.rowid", vec![iv(a.i64("question_id")?)])?),
        "get_sources_for_run" => json!(get_sources_for_run(c, &a.str("run_id")?)?),
        "get_claim_history" => json!(rows(c, "SELECT co.* FROM claim_occurrence co JOIN family_member fm ON fm.claim_id = co.claim_id WHERE fm.family_id = ? ORDER BY co.run_id, co.rowid", vec![sv(&a.str("family_id")?)])?),
        "get_route_history" => json!(rows(c, "SELECT * FROM source_observation WHERE resource_id=? ORDER BY observed_at, observation_id", vec![iv(a.i64("resource_id")?)])?),
        "get_last_checked" => row(c, "SELECT MAX(vr.started_at) AS last_checked FROM verification_run vr JOIN question_occurrence qo ON qo.occurrence_id = vr.occurrence_id WHERE qo.question_id = ?", vec![iv(a.i64("question_id")?)])?.map(|r| r["last_checked"].clone()).unwrap_or(Value::Null),
        "compare_runs" => {
            let (ra, rb) = (a.str("run_id_a")?, a.str("run_id_b")?);
            let sa = by_resource(get_sources_for_run(c, &ra)?);
            let sb = by_resource(get_sources_for_run(c, &rb)?);
            let added: Vec<&Row> = sb.iter().filter(|(k, _)| !sa.iter().any(|(x, _)| x == k)).map(|(_, r)| r).collect();
            let lost: Vec<&Row> = sa.iter().filter(|(k, _)| !sb.iter().any(|(x, _)| x == k)).map(|(_, r)| r).collect();
            let mut common: Vec<i64> = sa.iter().map(|(k, _)| *k).filter(|k| sb.iter().any(|(x, _)| x == k)).collect();
            common.sort();
            let mut changed = Vec::new();
            for rid in common {
                let (before, after) = (relations(c, &ra, rid)?, relations(c, &rb, rid)?);
                if before != after {
                    changed.push(json!({"resource_id": rid, "before": before, "after": after}));
                }
            }
            json!({"added": added, "lost": lost, "changed": changed})
        }
        _ => return Ok(None),
    }))
}
