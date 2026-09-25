//! Обиды, ёмкость прощения, личность — перенос группы `repositories.py`. Слова человека (описание/контекст обиды) запечатываются защитой полей.
use rusqlite::Connection;
use serde_json::{json, Value};
use yandi_rs::py_text::py_repr_str;

use super::field_protection as fp;
use super::*;

const PERSONALITY_JSON: [&str; 5] = ["traits", "goals", "principles", "limitations", "preferences"];

fn key_of(id: &str) -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("id".into(), json!(id));
    m
}

fn decode_context(r: &mut Row) -> R<()> {
    decode_json_cols(r, &["context"])
}

pub fn get_personality(c: &Connection) -> R<Value> {
    match row(c, "SELECT * FROM personality WHERE id=1", vec![])? {
        Some(mut r) => {
            decode_json_cols(&mut r, &PERSONALITY_JSON)?;
            Ok(Value::Object(r))
        }
        None => Ok(Value::Null),
    }
}

pub fn dispatch(c: &Connection, name: &str, a: &A) -> R<Option<Value>> {
    Ok(Some(match name {
        "record_grievance" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let id = a.str("grievance_id")?;
            let key = key_of(&id);
            let description = fp::seal(c, "grievance", "description", &key, Some(&a.str("description")?))?;
            let ctx_json = match a.get("context") {
                None | Some(Value::Null) => None,
                Some(v) => Some(pydumps(v)),
            };
            let context = fp::seal(c, "grievance", "context", &key, ctx_json.as_deref())?;
            exec(
                c,
                "INSERT INTO grievance (id, user_id, event_type, description, severity, status, context, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'registered', ?, ?, ?)",
                vec![sv(&id), sv(&a.str("user_id")?), sv(&a.str("event_type")?), osv(description.as_deref()), fv(a.opt_f64("severity")?), osv(context.as_deref()), sv(&created_at), sv(&created_at)],
            )?;
            Value::Null
        }
        "get_grievance" => match row(c, "SELECT * FROM grievance WHERE id=?", vec![sv(&a.str("grievance_id")?)])? {
            Some(mut r) => {
                fp::open_row(c, "grievance", &mut r, None)?;
                decode_context(&mut r)?;
                Value::Object(r)
            }
            None => Value::Null,
        },
        "find_similar_open_grievance" => {
            let prefix = cut_chars(&a.str("description")?, 20);
            let cands = rows(c, "SELECT * FROM grievance WHERE user_id=? AND status != 'forgiven' ORDER BY created_at DESC, rowid DESC", vec![sv(&a.str("user_id")?)])?;
            let mut found = Value::Null;
            for mut r in cands {
                fp::open_row(c, "grievance", &mut r, None)?;
                if cut_chars(r["description"].as_str().unwrap_or(""), 20) == prefix {
                    found = Value::Object(r);
                    break;
                }
            }
            found
        }
        "bump_grievance" => {
            let ts = dt_or_now(a.get("timestamp"))?;
            exec(c, "UPDATE grievance SET severity=?, status='registered', apology_sincerity=0.0, apology_at=NULL, understood_at=NULL, updated_at=? WHERE id=?", vec![fv(a.opt_f64("new_severity")?), sv(&ts), sv(&a.str("grievance_id")?)])?;
            Value::Null
        }
        "update_grievance_status" => {
            let ts = dt_or_now(a.get("timestamp"))?;
            let d = |k: &str| -> R<Sql> { Ok(dt_opt(a.get(k))?.map(|s| Sql::Text(s)).unwrap_or(Sql::Null)) };
            exec(
                c,
                "UPDATE grievance SET status=?, updated_at=?, apology_sincerity=COALESCE(?, apology_sincerity), apology_at=COALESCE(?, apology_at), understood_at=COALESCE(?, understood_at), forgiven_at=COALESCE(?, forgiven_at) WHERE id=?",
                vec![sv(&a.str("status")?), sv(&ts), fv(a.opt_f64("apology_sincerity")?), d("apology_at")?, d("understood_at")?, d("forgiven_at")?, sv(&a.str("grievance_id")?)],
            )?;
            Value::Null
        }
        "list_active_grievances" => {
            let mut rs = rows(c, "SELECT * FROM grievance WHERE user_id=? AND status NOT IN ('forgiven', 'unforgiven') ORDER BY created_at ASC, rowid ASC", vec![sv(&a.str("user_id")?)])?;
            fp::open_rows(c, "grievance", &mut rs, None)?;
            for r in rs.iter_mut() {
                decode_context(r)?;
            }
            json!(rs)
        }
        "list_recent_resolved_grievances" => {
            let mut rs = rows(c, "SELECT * FROM grievance WHERE user_id=? AND status IN ('forgiven', 'unforgiven') ORDER BY updated_at DESC, rowid DESC LIMIT ?", vec![sv(&a.str("user_id")?), iv(a.opt_i64("limit")?.unwrap_or(20))])?;
            fp::open_rows(c, "grievance", &mut rs, None)?;
            json!(rs)
        }
        "count_grievances_by_status" => json!(row(c, "SELECT COUNT(*) AS c FROM grievance WHERE user_id=? AND status=?", vec![sv(&a.str("user_id")?), sv(&a.str("status")?)])?.map(|r| r["c"].clone()).unwrap_or(json!(0))),
        "get_forgiveness_capacity" => {
            let uid = a.str("user_id")?;
            match row(c, "SELECT * FROM forgiveness_capacity WHERE user_id=?", vec![sv(&uid)])? {
                Some(r) => Value::Object(r),
                None => json!({"user_id": uid, "capacity": 50.0, "last_forgiveness": null, "updated_at": null}),
            }
        }
        "set_forgiveness_capacity" => {
            let ts = dt_or_now(a.get("timestamp"))?;
            let lf = dt_opt(a.get("last_forgiveness"))?;
            exec(
                c,
                "INSERT INTO forgiveness_capacity (user_id, capacity, last_forgiveness, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET capacity=excluded.capacity, last_forgiveness=COALESCE(excluded.last_forgiveness, last_forgiveness), updated_at=excluded.updated_at",
                vec![sv(&a.str("user_id")?), fv(a.opt_f64("capacity")?), osv(lf.as_deref()), sv(&ts)],
            )?;
            Value::Null
        }
        "get_personality" => get_personality(c)?,
        "get_or_create_personality" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            let js = |k: &str| -> Sql { jv(a.get(k)) };
            exec(
                c,
                "INSERT OR IGNORE INTO personality (id, name, version, traits, goals, principles, limitations, preferences, created_at, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                vec![sv(&a.str("name")?), sv(&a.str("version")?), js("traits"), js("goals"), js("principles"), js("limitations"), js("preferences"), sv(&created_at), sv(&created_at)],
            )?;
            get_personality(c)?
        }
        "update_personality_lists" => {
            let mut sets = Vec::new();
            let mut args = Vec::new();
            for col in ["traits", "goals", "principles", "limitations"] {
                if let Some(v) = a.get(col).filter(|v| !v.is_null()) {
                    sets.push(format!("{col}=?"));
                    args.push(jv(Some(v)));
                }
            }
            if !sets.is_empty() {
                sets.push("updated_at=?".into());
                args.push(sv(&dt_or_now(a.get("updated_at"))?));
                exec(c, &format!("UPDATE personality SET {} WHERE id=1", sets.join(", ")), args)?;
            }
            Value::Null
        }
        "increment_personality_counter" => {
            let counter = a.str("counter")?;
            if !["total_cycles", "total_decisions", "total_learnings"].contains(&counter.as_str()) {
                return Err(format!("ValueError: unknown personality counter: {}", py_repr_str(&counter)));
            }
            let ts = dt_or_now(a.get("updated_at"))?;
            exec(c, &format!("UPDATE personality SET {counter} = {counter} + 1, updated_at=? WHERE id=1"), vec![sv(&ts)])?;
            Value::Null
        }
        "record_personality_change" => {
            let created_at = dt_or_now(a.get("created_at"))?;
            json!(exec(c, "INSERT INTO personality_change (what_changed, reason, created_at) VALUES (?, ?, ?)", vec![sv(&a.str("what_changed")?), osv(a.opt_str("reason")?.as_deref()), sv(&created_at)])?.1)
        }
        "count_personality_changes" => json!(row(c, "SELECT COUNT(*) AS c FROM personality_change", vec![])?.map(|r| r["c"].clone()).unwrap_or(json!(0))),
        _ => return Ok(None),
    }))
}
