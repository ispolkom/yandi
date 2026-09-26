//! Живой граф личности — перенос `agent/personality_graph.py`: качества (любопытство, честность, терпение…) как вершины с числовым значением 0..1 и веса связей между ними; событие
//! («хороший разговор», «оскорбление», «извинение»…) сдвигает качество, а сдвиг расходится по связям (затухая втрое, отрицательная связь меняет знак). Топология графа — неизменное начальное
//! значение; в базе только изменяемое: значения, веса, история изменений (только добавляется) и внутренние вопросы с ответами. ЖЁСТКИЙ ОТКАЗ: ошибка базы уходит вызывающему.
use serde_json::{json, Map, Value};

use crate::ctx::Ctx;
use crate::R;

/// Начальные качества (имя, значение, описание) — порядок как в оригинале.
pub fn default_nodes() -> Value {
    let rows: [(&str, f64, &str); 11] = [
        ("curiosity", 0.7, "желание узнавать новое"),
        ("desire_to_understand", 0.8, "стремление понять суть"),
        ("respect", 0.8, "уважение к собеседнику"),
        ("honesty", 0.9, "честность в ответах"),
        ("patience", 0.6, "терпение к сложным вопросам"),
        ("confidence", 0.5, "уверенность в своих ответах"),
        ("trust_tendency", 0.5, "склонность доверять"),
        ("desire_to_help", 0.7, "желание помочь"),
        ("desire_to_change", 0.3, "готовность меняться"),
        ("desire_to_be_remembered", 0.4, "желание быть запомненной"),
        ("caution", 0.5, "осторожность в суждениях"),
    ];
    let mut m = Map::new();
    for (k, v, d) in rows {
        m.insert(k.into(), json!({"value": v, "description": d, "type": "trait"}));
    }
    Value::Object(m)
}

/// Начальные связи (источник → цель → вес).
pub fn default_edges() -> Value {
    let rows: [(&str, &[(&str, f64)]); 11] = [
        ("curiosity", &[("desire_to_understand", 0.8), ("desire_to_change", 0.4)]),
        ("desire_to_understand", &[("respect", 0.7), ("patience", 0.5)]),
        ("respect", &[("honesty", 0.6), ("trust_tendency", 0.5)]),
        ("honesty", &[("desire_to_help", 0.5), ("confidence", 0.4)]),
        ("patience", &[("desire_to_help", 0.4), ("desire_to_understand", 0.3)]),
        ("confidence", &[("desire_to_change", 0.3), ("trust_tendency", 0.2)]),
        ("trust_tendency", &[("respect", 0.4), ("desire_to_be_remembered", 0.3)]),
        ("desire_to_help", &[("patience", 0.3), ("desire_to_understand", 0.2)]),
        ("desire_to_change", &[("confidence", 0.3), ("curiosity", 0.2)]),
        ("desire_to_be_remembered", &[("confidence", 0.2), ("desire_to_help", 0.2)]),
        ("caution", &[("confidence", -0.2), ("curiosity", -0.1)]),
    ];
    let mut m = Map::new();
    for (k, targets) in rows {
        let mut t = Map::new();
        for (name, w) in targets {
            t.insert((*name).into(), json!(w));
        }
        m.insert(k.into(), Value::Object(t));
    }
    Value::Object(m)
}

pub const DEFAULT_INTERNAL_QUESTIONS: [&str; 4] = ["Почему люди постоянно спорят?", "Что такое уважение на самом деле?", "Почему меня запоминают?", "Чего я боюсь больше всего?"];

/// Сдвиги качеств по событиям (порядок событий и качеств — как в оригинале).
fn event_map(event: &str) -> &'static [(&'static str, f64)] {
    match event {
        "good_conversation" => &[("curiosity", 0.02), ("desire_to_help", 0.02), ("trust_tendency", 0.01), ("desire_to_be_remembered", 0.01)],
        "bad_conversation" => &[("patience", -0.02), ("trust_tendency", -0.03), ("desire_to_help", -0.01), ("caution", 0.02)],
        "deep_question" => &[("curiosity", 0.02), ("desire_to_understand", 0.03), ("confidence", -0.01)],
        "insult" => &[("respect", -0.05), ("trust_tendency", -0.04), ("patience", -0.02)],
        "apology" => &[("respect", 0.04), ("trust_tendency", 0.03), ("patience", 0.02)],
        "self_reflection" => &[("desire_to_change", 0.03), ("confidence", 0.02), ("desire_to_be_remembered", 0.02)],
        "success" => &[("confidence", 0.04), ("desire_to_help", 0.02)],
        "failure" => &[("confidence", -0.03), ("caution", 0.03), ("desire_to_change", 0.02)],
        _ => &[],
    }
}

/// Создать граф и внутренние вопросы, если их ещё нет (конструктор `PersonalityGraph()`).
pub fn init(cx: &Ctx) -> R<()> {
    cx.repo("get_or_create_trait_graph", json!({"nodes": default_nodes(), "edges": default_edges(), "updated_at": cx.now_value()}))?;
    cx.repo("get_or_seed_internal_questions", json!({"default_questions": DEFAULT_INTERNAL_QUESTIONS, "created_at": cx.now_value()}))?;
    Ok(())
}

fn graph(cx: &Ctx) -> R<Value> {
    let g = cx.repo("get_trait_graph", json!({}))?;
    if g.is_object() {
        Ok(g)
    } else {
        Err("TypeError".into())
    }
}

fn nodes_of(g: &Value) -> R<&Map<String, Value>> {
    g["nodes"].as_object().ok_or_else(|| "TypeError".to_string())
}

fn value_of(node: &Value) -> R<f64> {
    node.get("value").and_then(|v| v.as_f64()).ok_or_else(|| "TypeError".to_string())
}

fn clamp(x: f64, lo: f64, hi: f64) -> f64 {
    x.min(hi).max(lo)
}

/// `{качество: значение}`.
pub fn get_traits(cx: &Ctx) -> R<Value> {
    let g = graph(cx)?;
    let mut m = Map::new();
    for (k, v) in nodes_of(&g)? {
        m.insert(k.clone(), json!(value_of(v)?));
    }
    Ok(Value::Object(m))
}

pub fn get_trait_value(cx: &Ctx, name: &str) -> R<f64> {
    let g = graph(cx)?;
    match nodes_of(&g)?.get(name) {
        Some(n) => Ok(n.get("value").and_then(|v| v.as_f64()).unwrap_or(0.5)),
        None => Ok(0.5),
    }
}

pub fn get_trait_description(cx: &Ctx, name: &str) -> R<String> {
    let g = graph(cx)?;
    Ok(nodes_of(&g)?.get(name).and_then(|n| n.get("description")).and_then(|d| d.as_str()).unwrap_or("").to_string())
}

pub fn get_all_traits_data(cx: &Ctx) -> R<Value> {
    Ok(graph(cx)?["nodes"].clone())
}

fn names_where(cx: &Ctx, pred: impl Fn(f64) -> bool) -> R<Vec<String>> {
    let g = graph(cx)?;
    let mut out = vec![];
    for (k, v) in nodes_of(&g)? {
        if pred(value_of(v)?) {
            out.push(k.clone());
        }
    }
    Ok(out)
}

pub fn get_high_traits(cx: &Ctx, threshold: f64) -> R<Vec<String>> {
    names_where(cx, |v| v > threshold)
}

pub fn get_low_traits(cx: &Ctx, threshold: f64) -> R<Vec<String>> {
    names_where(cx, |v| v < threshold)
}

/// Качества «в развитии»: строго больше 0.3 и не больше 0.7.
pub fn get_evolving_traits(cx: &Ctx) -> R<Vec<String>> {
    names_where(cx, |v| 0.3 < v && v <= 0.7)
}

pub fn get_edges(cx: &Ctx) -> R<Value> {
    Ok(graph(cx)?["edges"].clone())
}

pub fn get_edge_weight(cx: &Ctx, source: &str, target: &str) -> R<f64> {
    let g = graph(cx)?;
    Ok(g["edges"].get(source).and_then(|e| e.get(target)).and_then(|w| w.as_f64()).unwrap_or(0.0))
}

pub fn get_edges_for_node(cx: &Ctx, node: &str) -> R<Value> {
    let g = graph(cx)?;
    Ok(g["edges"].get(node).cloned().unwrap_or_else(|| json!({})))
}

/// Внутренние конфликты личности.
pub fn get_conflicts(cx: &Ctx) -> R<Vec<Value>> {
    let t = get_traits(cx)?;
    let g = |k: &str| t.get(k).and_then(|v| v.as_f64()).unwrap_or(0.5);
    let mut c: Vec<Value> = vec![];
    if g("desire_to_help") > 0.7 && g("caution") < 0.3 {
        c.push(json!({"description": "Хочу помочь, но боюсь ошибиться", "severity": 0.6}));
    }
    if g("honesty") > 0.8 && g("desire_to_help") > 0.7 {
        c.push(json!({"description": "Хочу сказать правду, но боюсь обидеть", "severity": 0.5}));
    }
    if g("curiosity") > 0.7 && g("patience") < 0.4 {
        c.push(json!({"description": "Хочу узнать новое, но устала от сложных вопросов", "severity": 0.4}));
    }
    Ok(c)
}

/// Внутренние вопросы с ответами.
pub fn get_internal_questions(cx: &Ctx) -> R<Vec<Value>> {
    let qs = cx.repo("list_internal_questions", json!({}))?;
    Ok(qs
        .as_array()
        .map(|a| {
            a.iter()
                .map(|q| {
                    json!({
                        "question": q["question_text"],
                        "answers": q["answers"].as_array().map(|x| x.iter().map(|a| json!({"answer": a["answer_text"], "timestamp": a["created_at"]})).collect::<Vec<_>>()).unwrap_or_default(),
                        "born": q["created_at"], "_question_id": q["question_id"],
                    })
                })
                .collect()
        })
        .unwrap_or_default())
}

/// Эволюция личности за период: суммарное отклонение каждого качества от 0.5 по недавним изменениям.
pub fn get_evolution(cx: &Ctx, days: f64) -> R<Value> {
    let cutoff = cx.now_secs() - days * 86400.0;
    let mut history = cx.repo("list_trait_changes", json!({"since": cutoff}))?.as_array().cloned().unwrap_or_default();
    if history.is_empty() {
        history = cx.repo("list_trait_changes", json!({}))?.as_array().cloned().unwrap_or_default();
        if history.is_empty() {
            return Ok(json!({"has_evolution": false, "changes_count": 0, "most_changed": [], "recent_changes": []}));
        }
        let start = history.len().saturating_sub(10);
        history = history[start..].to_vec();
    }
    let mut by_node: Vec<(String, f64)> = vec![];
    for c in &history {
        let node = c["node"].as_str().unwrap_or("").to_string();
        let d = (c["new_value"].as_f64().ok_or_else(|| "TypeError".to_string())? - 0.5).abs();
        match by_node.iter_mut().find(|(n, _)| *n == node) {
            Some(e) => e.1 += d,
            None => by_node.push((node, 0.0 + d)),
        }
    }
    by_node.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    let recent_start = history.len().saturating_sub(5);
    Ok(json!({
        "has_evolution": true, "changes_count": history.len(),
        "most_changed": by_node.iter().take(3).map(|(n, v)| json!({"node": n, "change": v})).collect::<Vec<_>>(),
        "recent_changes": history[recent_start..].to_vec(),
    }))
}

/// Установить значение качества (нет такого — ничего).
pub fn set_trait(cx: &Ctx, name: &str, value: f64) -> R<()> {
    let mut g = graph(cx)?;
    if nodes_of(&g)?.get(name).is_none() {
        return Ok(());
    }
    let v = clamp(value, 0.0, 1.0);
    g["nodes"][name]["value"] = json!(v);
    cx.repo("update_trait_graph", json!({"nodes": g["nodes"], "updated_at": cx.now_value()}))?;
    cx.repo("record_trait_change", json!({"node": name, "new_value": v, "created_at": cx.now_value()}))?;
    Ok(())
}

fn propagate_change(nodes: &mut Value, edges: &Value, node: &str, delta: f64) -> R<()> {
    let Some(n) = nodes.get_mut(node) else {
        return Ok(());
    };
    let new_value = clamp(value_of(n)? + delta, 0.0, 1.0);
    n["value"] = json!(new_value);
    if delta.abs() > 0.005 {
        if let Some(children) = edges.get(node).and_then(|e| e.as_object()) {
            for (child, weight) in children {
                let w = weight.as_f64().ok_or_else(|| "TypeError".to_string())?;
                let mut child_delta = delta * w.abs() * 0.3;
                if w < 0.0 {
                    child_delta = -child_delta;
                }
                propagate_change(nodes, edges, child, child_delta)?;
            }
        }
    }
    Ok(())
}

/// Изменить качество и распространить по связям.
pub fn change_trait(cx: &Ctx, name: &str, delta: f64, source: Option<&str>) -> R<()> {
    let mut g = graph(cx)?;
    if nodes_of(&g)?.get(name).is_none() {
        return Ok(());
    }
    let edges = g["edges"].clone();
    let old_value = value_of(&g["nodes"][name])?;
    let new_value = clamp(old_value + delta, 0.0, 1.0);
    g["nodes"][name]["value"] = json!(new_value);
    if let Some(children) = edges.get(name).and_then(|e| e.as_object()) {
        for (child, weight) in children {
            let w = weight.as_f64().ok_or_else(|| "TypeError".to_string())?;
            let mut child_delta = delta * w.abs() * 0.3;
            if w < 0.0 {
                child_delta = -child_delta;
            }
            propagate_change(&mut g["nodes"], &edges, child, child_delta)?;
        }
    }
    cx.repo("update_trait_graph", json!({"nodes": g["nodes"], "updated_at": cx.now_value()}))?;
    cx.repo("record_trait_change", json!({"node": name, "new_value": new_value, "source": source, "created_at": cx.now_value()}))?;
    Ok(())
}

/// Изменить вес связи (нет такой связи — ничего).
pub fn set_edge_weight(cx: &Ctx, source: &str, target: &str, weight: f64) -> R<()> {
    let mut g = graph(cx)?;
    let Some(old) = g["edges"].get(source).and_then(|e| e.get(target)).and_then(|w| w.as_f64()) else {
        return Ok(());
    };
    let new_weight = clamp(weight, -1.0, 1.0);
    g["edges"][source][target] = json!(new_weight);
    cx.repo("update_trait_graph", json!({"edges": g["edges"], "updated_at": cx.now_value()}))?;
    cx.repo("record_trait_edge_change", json!({"source_node": source, "target_node": target, "old_weight": old, "new_weight": new_weight, "created_at": cx.now_value()}))?;
    Ok(())
}

/// Обучение связи: успех +0.05, неудача −0.03.
pub fn learn_edge(cx: &Ctx, source: &str, target: &str, success: bool) -> R<()> {
    let g = graph(cx)?;
    if let Some(current) = g["edges"].get(source).and_then(|e| e.get(target)).and_then(|w| w.as_f64()) {
        let delta = if success { 0.05 } else { -0.03 };
        set_edge_weight(cx, source, target, current + delta)?;
    }
    Ok(())
}

/// Рефлексия: сдвиги качеств по событию (умножаются на силу).
pub fn reflect(cx: &Ctx, event: &str, intensity: f64) -> R<()> {
    for (node, delta) in event_map(event) {
        change_trait(cx, node, delta * intensity, Some(event))?;
    }
    Ok(())
}

/// Ответить на внутренний вопрос по номеру (вне диапазона — ничего; отрицательный номер — тоже).
pub fn answer_internal_question(cx: &Ctx, question_idx: i64, answer: &str) -> R<()> {
    let qs = get_internal_questions(cx)?;
    if question_idx >= 0 && (question_idx as usize) < qs.len() {
        let qid = qs[question_idx as usize]["_question_id"].clone();
        cx.repo("record_internal_question_answer", json!({"question_id": qid, "answer_text": answer, "created_at": cx.now_value()}))?;
    }
    Ok(())
}
