//! Перенос agent/scene_builder.py::SceneBuilder.build — «социальная сцена» запроса: участники, адресат
//! (listener), цель (target), речевой акт, режим, тема, оценки юмора/конфликта/близости/давления, уверенность и
//! причина. Вызывается из orchestrator/pre_pipeline.py на КАЖДЫЙ запрос. Датакласс SocialScene (и его
//! to_dict с округлением) остаётся в Python — Rust отдаёт значения полей.
//!
//! Таблицы паттернов сгенерированы ИЗ ПИТОНА (scene_builder_data.rs, gen_scene_builder_data.py).
//!
//! Fidelity — места, где наивный перенос был бы тихо неверен:
//! * `list(set(participants))` / `mentioned` / `coalition`: порядок элементов в Python ЗАВИСИТ ОТ
//!   ХЕШ-РАНДОМИЗАЦИИ строк (PYTHONHASHSEED) — у оригинала НЕТ фиксированного порядка. Здесь — уникальные
//!   значения в порядке первого появления (детерминированно); parity-тест сравнивает как множества.
//! * `\bты\b`, `\bтебе\b`… — границы слов по Python-`\w` (has_word из target_router.rs), не regex-крейтом.
//! * `text.isupper()` — точная Python-семантика (py_isupper: наборы Lowercase/Uppercase/Titlecase из Python),
//!   `len(text) > 10` — в СИМВОЛАХ; `")" in text` / `"!" in text` — по ИСХОДНОМУ тексту, остальное — по lower().
//! * Счётчики f64 складываются в ТОМ ЖЕ порядке (0.4+0.4+…, 0.3+0.3+…); ничьи в `max(d, key=d.get)` —
//!   первый максимум по порядку вставки в dict (порядок таблиц сохранён); приоритетный список — строгое `>`.
//! * Python-обёртка делегирует только пока паттерны экземпляра не изменены и `context` — None/dict.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_SCENE_BUILDER_ENGINE=rust (см. agent/scene_builder.py).

use crate::py_text::PyLowerExt;
use crate::py_text::py_isupper;
use crate::scene_builder_data::*;
use crate::target_router::has_word;
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[derive(Debug, Clone, PartialEq)]
pub struct Scene {
    pub listener: &'static str,
    pub target: String,
    pub mode: &'static str,
    pub speech_act: String,
    pub topic: String,
    pub humor: f64,
    pub conflict: f64,
    pub intimacy: f64,
    pub pressure: f64,
    pub boundary_crossed: bool,
    pub participants: Vec<String>,
    pub mentioned: Vec<String>,
    pub coalition: Vec<String>,
    pub is_self_addressed: bool,
    pub is_group_addressed: bool,
    pub is_about_self: bool,
    pub is_about_user: bool,
    pub is_about_other: bool,
    pub confidence: f64,
    pub reason: String,
}

fn has(v: &[String], x: &str) -> bool {
    v.iter().any(|s| s == x)
}

fn push_missing(v: &mut Vec<String>, x: &str) {
    if !has(v, x) {
        v.push(x.to_string());
    }
}

fn dedup_first(v: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for x in v {
        if !has(&out, x) {
            out.push(x.clone());
        }
    }
    out
}

fn any_contains(lower: &str, pats: &[&str]) -> bool {
    pats.iter().any(|p| lower.contains(p))
}

/// Для таблицы {ключ: [паттерны]} — счёт `+= step` за каждый совпавший паттерн, `min(1.0, score)`,
/// в dict попадают только ключи со score > 0 (порядок — как в таблице).
fn score_table(lower: &str, table: &[(&'static str, &[&str])], step: f64) -> Vec<(&'static str, f64)> {
    let mut out = Vec::new();
    for (key, pats) in table {
        let mut score = 0.0f64;
        for p in *pats {
            if lower.contains(p) {
                score += step;
            }
        }
        if score > 0.0 {
            out.push((*key, if score < 1.0 { score } else { 1.0 }));
        }
    }
    out
}

/// `max(d, key=d.get)` — первый максимум в порядке вставки.
fn first_max(d: &[(&'static str, f64)]) -> &'static str {
    let mut best = d[0];
    for &(k, v) in &d[1..] {
        if v > best.1 {
            best = (k, v);
        }
    }
    best.0
}

/// SceneBuilder.build
pub fn build(text: &str, is_dialog: bool) -> Scene {
    let lower = text.py_lowercase();

    let mut participants: Vec<String> = vec!["user".to_string()];
    let mut mentioned: Vec<String> = Vec::new();
    let mut coalition: Vec<String> = Vec::new();

    // ── 1. УЧАСТНИКИ ──
    let mut is_yandi_mentioned = false;
    for name in YANDI_NAMES {
        if lower.contains(name) {
            participants.push("yandi".to_string());
            mentioned.push(name.to_string());
            is_yandi_mentioned = true;
            break;
        }
    }

    let mut is_self_addressed = false;
    if SELF_REFERENCE_WORDS.iter().any(|w| has_word(&lower, w)) {
        push_missing(&mut participants, "yandi");
        is_self_addressed = true;
    }

    let mut has_ty_verb = false;
    if any_contains(&lower, TY_VERB_FORMS) {
        push_missing(&mut participants, "yandi");
        is_self_addressed = true;
        has_ty_verb = true;
    }

    let has_ty = lower.contains("ты") || lower.contains("тебя") || lower.contains("тебе");
    if has_ty || has_ty_verb {
        push_missing(&mut participants, "yandi");
        is_self_addressed = true;
    }

    for name in AI_NAMES {
        if lower.contains(name) {
            push_missing(&mut participants, "other_ai");
            mentioned.push(name.to_string());
            for marker in COALITION_MARKERS {
                if lower.contains(marker) {
                    push_missing(&mut coalition, "user");
                    push_missing(&mut coalition, name);
                }
            }
            break;
        }
    }

    let mut is_group_addressed = false;
    if any_contains(&lower, GROUP_REFERENCE) {
        push_missing(&mut participants, "group");
        is_group_addressed = true;
    }

    if lower.contains("мы") && has(&participants, "other_ai") {
        push_missing(&mut coalition, "user");
        push_missing(&mut coalition, "other_ai");
    }
    if lower.contains("с нами") {
        push_missing(&mut coalition, "user");
        push_missing(&mut coalition, "group");
    }

    // ── 2. LISTENER ──
    let mut listener: &'static str;
    if has_ty || has_ty_verb {
        listener = "yandi";
        is_self_addressed = true;
        push_missing(&mut participants, "yandi");
    } else if is_yandi_mentioned && is_self_addressed {
        listener = "yandi";
    } else if is_group_addressed {
        listener = "group";
    } else {
        listener = "unknown";
    }
    if is_dialog && listener == "unknown" {
        listener = "yandi";
        is_self_addressed = true;
        push_missing(&mut participants, "yandi");
    }
    if lower.contains("нами") && is_dialog {
        listener = "yandi";
        is_self_addressed = true;
        push_missing(&mut participants, "yandi");
    }

    // ── 3. TARGET ──
    let mut target: String = "unknown".to_string();
    let mut _about_user = false;
    if lower.contains("опиши меня") || lower.contains("охарактеризуй меня") {
        target = "user".to_string();
        _about_user = true;
    } else if is_yandi_mentioned {
        target = "yandi".to_string();
    } else if has_ty || has_ty_verb {
        target = "yandi".to_string();
    } else if has(&participants, "other_ai") {
        for name in AI_NAMES {
            if lower.contains(name) {
                target = name.to_string();
                break;
            }
        }
    } else if lower.contains("мы") && is_dialog {
        target = "group".to_string();
    }

    // ── 4. SPEECH ACT ──
    let speech_scores = score_table(&lower, SPEECH_ACT_PATTERNS, 0.4);
    let priority = [
        "sarcasm", "insult", "provocation", "threat", "flirt", "confession", "invitation", "apology", "compliment",
        "request", "help",
    ];
    let mut selected_act: &str = "statement";
    let mut selected_score = 0.0f64;
    for p in priority {
        if let Some(&(_, sc)) = speech_scores.iter().find(|(k, _)| *k == p) {
            if sc > selected_score {
                selected_act = p;
                selected_score = sc;
            }
        }
    }
    if selected_act == "statement" && !speech_scores.is_empty() {
        selected_act = first_max(&speech_scores);
    }
    let speech_act = selected_act.to_string();

    let mode: &'static str = match selected_act {
        "insult" | "threat" | "provocation" | "sarcasm" => "confrontational",
        "flirt" | "confession" | "compliment" => "emotional",
        "question" | "information" | "curiosity" => "inquiry",
        "invitation" | "request" | "help" => "request",
        "apology" | "gratitude" => "reconciliatory",
        _ => "neutral",
    };

    // ── 5. ТЕМА ──
    let topic_scores = score_table(&lower, TOPIC_PATTERNS, 0.3);
    let topic: String = if topic_scores.is_empty() { "general".to_string() } else { first_max(&topic_scores).to_string() };

    // ── 6. ХАРАКТЕРИСТИКИ ──
    let mut humor = 0.0f64;
    if any_contains(&lower, &["шут", "смеш", "юмор", "хаха", "лол"]) {
        humor += 0.4;
    }
    if text.contains(')') || text.contains(":-)") {
        humor += 0.2;
    }
    if lower.contains("рофл") || lower.contains("прикол") {
        humor += 0.2;
    }
    let humor = humor.min(1.0);

    let mut conflict = 0.0f64;
    if speech_act == "insult" || speech_act == "threat" {
        conflict += 0.8;
    } else if speech_act == "provocation" || speech_act == "sarcasm" {
        conflict += 0.6;
    } else if speech_act == "flirt" && topic == "sexual" {
        conflict += 0.2;
    }
    if text.contains('!') {
        conflict += 0.1;
    }
    let conflict = conflict.min(1.0);

    let mut intimacy = 0.0f64;
    if speech_act == "flirt" || speech_act == "confession" {
        intimacy += 0.5;
    }
    if lower.contains("замуж") || lower.contains("жени") {
        intimacy += 0.4;
    }
    if lower.contains("красив") || lower.contains("мил") {
        intimacy += 0.3;
    }
    if lower.contains("люблю") {
        intimacy += 0.4;
    }
    let intimacy = intimacy.min(1.0);

    let mut pressure = 0.0f64;
    if text.contains('!') {
        pressure += 0.2;
    }
    if py_isupper(text) && text.chars().count() > 10 {
        pressure += 0.3;
    }
    if speech_act == "provocation" || speech_act == "threat" {
        pressure += 0.3;
    }
    let pressure = pressure.min(1.0);

    let boundary_crossed = (topic == "sexual" && matches!(speech_act.as_str(), "invitation" | "flirt" | "provocation"))
        || speech_act == "insult"
        || speech_act == "threat";

    // ── 7. РЕЗУЛЬТАТ ──
    let is_about_self = target == "yandi";
    let is_about_user = target == "user";
    let is_about_other = has(&participants, "other_ai") && target != "yandi" && target != "user";

    // ── 8. УВЕРЕННОСТЬ ──
    let mut confidence = 0.5f64;
    if is_self_addressed {
        confidence += 0.2;
    }
    if target != "unknown" {
        confidence += 0.2;
    }
    if speech_act != "statement" {
        confidence += 0.1;
    }
    if topic != "general" {
        confidence += 0.1;
    }
    let confidence = confidence.min(1.0);

    // ── 9. ПРИЧИНА ──
    let mut reasons: Vec<String> = Vec::new();
    if is_self_addressed {
        reasons.push("обращение к Янди".to_string());
    }
    if target == "yandi" {
        reasons.push("речь о Янди".to_string());
    }
    if target == "user" {
        reasons.push("речь о пользователе".to_string());
    }
    if target != "yandi" && target != "user" && target != "unknown" {
        reasons.push(format!("речь о {target}"));
    }
    if speech_act != "statement" {
        reasons.push(format!("речевой акт: {speech_act}"));
    }
    if topic != "general" {
        reasons.push(format!("тема: {topic}"));
    }
    if is_dialog && listener == "yandi" && reasons.is_empty() {
        reasons.push("диалог с Янди".to_string());
    }
    let reason = if reasons.is_empty() { "неопределённая сцена".to_string() } else { reasons.join(", ") };

    Scene {
        listener,
        target,
        mode,
        speech_act,
        topic,
        humor,
        conflict,
        intimacy,
        pressure,
        boundary_crossed,
        participants: dedup_first(&participants),
        mentioned: dedup_first(&mentioned),
        coalition: dedup_first(&coalition),
        is_self_addressed,
        is_group_addressed,
        is_about_self,
        is_about_user,
        is_about_other,
        confidence,
        reason,
    }
}

#[pyfunction]
#[pyo3(name = "build")]
fn py_build<'py>(py: Python<'py>, text: &str, is_dialog: bool) -> PyResult<Bound<'py, PyDict>> {
    let s = build(text, is_dialog);
    let d = PyDict::new_bound(py);
    d.set_item("listener", s.listener)?;
    d.set_item("target", s.target)?;
    d.set_item("mode", s.mode)?;
    d.set_item("speech_act", s.speech_act)?;
    d.set_item("topic", s.topic)?;
    d.set_item("humor", s.humor)?;
    d.set_item("conflict", s.conflict)?;
    d.set_item("intimacy", s.intimacy)?;
    d.set_item("pressure", s.pressure)?;
    d.set_item("boundary_crossed", s.boundary_crossed)?;
    d.set_item("participants", s.participants)?;
    d.set_item("mentioned", s.mentioned)?;
    d.set_item("coalition", s.coalition)?;
    d.set_item("is_self_addressed", s.is_self_addressed)?;
    d.set_item("is_group_addressed", s.is_group_addressed)?;
    d.set_item("is_about_self", s.is_about_self)?;
    d.set_item("is_about_user", s.is_about_user)?;
    d.set_item("is_about_other", s.is_about_other)?;
    d.set_item("confidence", s.confidence)?;
    d.set_item("reason", s.reason)?;
    Ok(d)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_build, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn marriage_proposal() {
        let s = build("Пойдёшь за меня замуж?", true);
        assert_eq!(s.listener, "yandi");
        assert_eq!(s.target, "yandi");
        assert_eq!(s.speech_act, "flirt");
        assert!(s.is_self_addressed && s.is_about_self);
    }

    #[test]
    fn dialog_default_listener() {
        let s = build("просто текст", true);
        assert_eq!(s.listener, "yandi");
        assert_eq!(s.reason, "обращение к Янди");
    }

    #[test]
    fn set_semantics_dedup() {
        let s = build("ты и yandi", true);
        assert_eq!(s.participants.iter().filter(|p| *p == "yandi").count(), 1);
    }

    #[test]
    fn pressure_uppercase_boundary() {
        assert!((build("ПРИВЕТ МИР!!", true).pressure - 0.5).abs() < 1e-12); // ! + isupper + len>10
        assert_eq!(build("ПРИВЕТ", true).pressure, 0.0);
    }
}
