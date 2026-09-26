//! Построитель графа гипотез — перенос `agent/hypothesis_builder.py` вместе со структурами `agent/hypothesis_graph.py` (в виде словарей):
//! наблюдения (L1) → следствия (L2, только для причинных вопросов) → гипотезы по маркерам (L3) → школы (L4).
//! Идентификаторы берутся из `Ctx::uuid_hex` (первые 8 знаков), порядок расхода тот же, что у оригинала. Отладочную печать оригинала (`[DEBUG ...]`) не переносим.
use std::collections::HashSet;

use serde_json::{json, Value};
use yandi_rs::py_text::{py_lower, py_split_whitespace};

use crate::ctx::Ctx;

fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn clen(s: &str) -> usize {
    s.chars().count()
}

fn id8(cx: &Ctx, prefix: &str) -> String {
    format!("{prefix}_{}", cut(&cx.uuid_hex(), 8))
}

/// `re.sub(r'\s+', ' ', text).strip()`
pub fn normalize_text(text: &str) -> String {
    py_split_whitespace(text).collect::<Vec<_>>().join(" ")
}

/// `re.split(r'[.!?]+', text)`, части короче 16 знаков (после `strip`) отбрасываются.
pub fn extract_sentences(text: &str) -> Vec<String> {
    text.split(|c| c == '.' || c == '!' || c == '?')
        .map(|s| yandi_rs::py_text::py_strip(s).to_string())
        .filter(|s| clen(s) > 15)
        .collect()
}

fn relevance_score(sentence: &str, query: &str) -> f64 {
    let q: HashSet<String> = py_split_whitespace(&py_lower(query)).map(String::from).collect();
    let s: HashSet<String> = py_split_whitespace(&py_lower(sentence)).map(String::from).collect();
    if q.is_empty() {
        return 0.5;
    }
    q.intersection(&s).count() as f64 / q.len() as f64
}

fn obs(id: String, text: String, source_ref: &str, confidence: f64) -> Value {
    json!({"id": id, "text": text, "source_ref": source_ref, "confidence": confidence, "is_direct_quote": false, "raw_quote": Value::Null})
}

/// L1: наблюдения из текста.
pub fn extract_observations(cx: &Ctx, text: &str, source_ref: &str, query: &str) -> Vec<Value> {
    const EVALUATIVE: [&str; 10] = ["зависть", "ревность", "гордость", "справедливость", "несправедливость", "вина", "наказание", "ненависть", "предательство", "верность"];
    let mut out = vec![];
    for sent in extract_sentences(text) {
        let sent = normalize_text(&sent);
        let n = clen(&sent);
        if n > 15 && n < 800 {
            let lower = py_lower(&sent);
            let evaluative = EVALUATIVE.iter().filter(|w| lower.contains(**w)).count();
            if evaluative <= 2 {
                if !query.is_empty() && relevance_score(&sent, query) < 0.03 {
                    continue;
                }
                out.push(obs(id8(cx, "obs"), sent, source_ref, 0.9));
            }
        }
    }
    if out.is_empty() && clen(text) > 50 {
        out.push(obs(id8(cx, "obs"), cut(text, 200), source_ref, 0.7));
    }
    out
}

fn obs_text(o: &Value) -> String {
    py_lower(o["text"].as_str().unwrap_or(""))
}

/// L2: логические следствия — только если вопрос причинный.
pub fn build_inferences(cx: &Ctx, observations: &[Value], question: &str) -> Vec<Value> {
    const CAUSAL: [&str; 8] = ["почему", "зачем", "из-за", "причина", "следствие", "после", "затем", "тогда"];
    let q = py_lower(question);
    if !CAUSAL.iter().any(|k| q.contains(k)) {
        return vec![];
    }
    let mut out = vec![];
    let anger = |t: &str| t.contains("гнев") || t.contains("гневался") || t.contains("разгневался");
    if observations.iter().any(|o| anger(&obs_text(o))) {
        let based: Vec<Value> = observations.iter().filter(|o| anger(&obs_text(o))).map(|o| o["id"].clone()).collect();
        out.push(json!({"id": id8(cx, "inf"), "text": "Упоминается сильное отрицательное эмоциональное состояние (гнев).", "based_on": based, "confidence": 0.9}));
    }
    let seq = |t: &str| ["после", "затем", "тогда"].iter().any(|w| t.contains(w));
    if observations.iter().any(|o| seq(&obs_text(o))) {
        let based: Vec<Value> = observations.iter().filter(|o| seq(&obs_text(o))).map(|o| o["id"].clone()).collect();
        out.push(json!({"id": id8(cx, "inf"), "text": "Между событиями существует временная и вероятная причинная связь.", "based_on": based, "confidence": 0.7}));
    }
    out
}

/// Категория гипотезы по ключевым словам.
pub fn classify_hypothesis(text: &str) -> &'static str {
    let l = py_lower(text);
    if l.contains("конфликт") || l.contains("антагонизм") || l.contains("война") {
        "Конфликт"
    } else if l.contains("сотрудничеств") || l.contains("взаимодейств") || l.contains("диалог") {
        "Сотрудничество"
    } else if l.contains("независимость") || l.contains("независим") || l.contains("автоном") {
        "Независимость"
    } else if l.contains("интеграция") || l.contains("синтез") || l.contains("единство") {
        "Интеграция"
    } else {
        "Общая точка зрения"
    }
}

/// L3: гипотезы из текстов; не больше пяти (идентификаторы при этом расходуются на все найденные).
pub fn extract_hypotheses_from_texts(cx: &Ctx, texts: &[String], _query: &str) -> Vec<Value> {
    const MARKERS: [&str; 23] = [
        "согласно", "по мнению", "исследователи считают", "некоторые учёные полагают", "предполагается", "выдвигается гипотеза", "есть точка зрения", "распространено мнение", "традиционно считается",
        "в литературе выделяют", "существует подход", "один из подходов", "другая интерпретация", "альтернативная точка зрения", "конфликт", "сотрудничество", "независимость", "диалог", "интеграция",
        "тезис", "концепция", "модель", "парадигма",
    ];
    let markers = &MARKERS;
    let all_text = py_lower(&texts.join(" "));
    let mut raw: Vec<String> = vec![];
    for text in texts {
        for sent in extract_sentences(text) {
            let sl = py_lower(&sent);
            for m in markers {
                if sl.contains(m) {
                    let h = normalize_text(&sent);
                    if clen(&h) > 30 {
                        raw.push(h);
                        break;
                    }
                }
            }
        }
    }
    if raw.is_empty() {
        raw = vec![if all_text.contains("наука") && all_text.contains("религия") {
            "Наука и религия — разные способы познания, но могут пересекаться.".to_string()
        } else if all_text.contains("коммунизм") || all_text.contains("демократия") {
            "Политические идеологии могут приобретать квазирелигиозные черты.".to_string()
        } else {
            "В предоставленных текстах содержатся различные точки зрения.".to_string()
        }];
    }
    let mut hyps: Vec<Value> = vec![];
    for h in raw {
        let description = if clen(&h) > 200 { format!("{}...", cut(&h, 200)) } else { h.clone() };
        let name = classify_hypothesis(&h);
        let key = cut(&h, 50);
        let count = texts.iter().filter(|t| t.contains(&key)).count();
        let text_support = if count > 0 { (count as f64 / 3.0).min(1.0) } else { 0.2 };
        hyps.push(json!({
            "id": id8(cx, "hyp"), "name": name, "description": description,
            "support": {"text_support": text_support, "tradition_support": 0.3, "science_support": 0.3},
            "explains": [], "not_explains": [], "competitors": [], "assumptions": [], "confirm_if": [], "refute_if": [], "origin": "извлечено из текстов",
        }));
    }
    hyps.truncate(5);
    hyps
}

fn tradition(id: String, name: &str, description: String, figures: Vec<String>) -> Value {
    json!({"id": id, "name": name, "description": description, "preferred_hypotheses": [], "key_figures": figures, "representative_works": []})
}

/// L4: школы, найденные по именам учёных; если никого нет — две стандартные.
pub fn extract_traditions_from_texts(cx: &Ctx, texts: &[String]) -> Vec<Value> {
    let all_text = texts.join(" ");
    let scholars: [(&str, &[&str]); 4] = [
        ("Конфликт", &["Докинз", "Уайт", "Фейнман", "Крик", "Эткинс", "Гинзбург"]),
        ("Сотрудничество", &["Брук", "Фернгрен", "Маска"]),
        ("Независимость", &["Гулд", "Доукинс"]),
        ("Диалог", &["Полани", "Барбур"]),
    ];
    let mut out = vec![];
    for (dir, names) in scholars {
        let found: Vec<String> = names.iter().filter(|n| all_text.contains(**n)).map(|n| n.to_string()).collect();
        if !found.is_empty() {
            let d = format!("Представлена учёными: {}", found.join(", "));
            out.push(tradition(id8(cx, "trad"), dir, d, found));
        }
    }
    if out.is_empty() {
        out = vec![
            tradition("trad_academic".into(), "Академическая традиция", "Основана на критическом анализе источников и научных методах.".into(), vec![]),
            tradition("trad_philosophical".into(), "Философская традиция", "Рассмотрение вопроса через призму философских категорий.".into(), vec![]),
        ];
    }
    out
}

/// Весь граф: `HypothesisGraph.to_dict()` для вопроса и текстов. Источников может быть меньше текстов — тогда лишние тексты не читаются (как `zip`).
pub fn build_hypothesis_graph(cx: &Ctx, question: &str, texts: &[String], source_refs: Option<&[String]>) -> Value {
    let default_refs: Vec<String>;
    let refs: &[String] = match source_refs {
        Some(r) => r,
        None => {
            default_refs = vec!["unknown".to_string(); texts.len()];
            &default_refs
        }
    };
    let mut all_obs: Vec<Value> = vec![];
    for (text, r) in texts.iter().zip(refs.iter()) {
        all_obs.extend(extract_observations(cx, text, r, question));
    }
    let shown: Vec<Value> = all_obs.iter().take(10).cloned().collect();
    let inferences = build_inferences(cx, &all_obs, question);
    let hypotheses = extract_hypotheses_from_texts(cx, texts, question);
    let traditions = extract_traditions_from_texts(cx, texts);
    json!({"question": question, "observations": shown, "inferences": inferences, "hypotheses": hypotheses, "traditions": traditions})
}
