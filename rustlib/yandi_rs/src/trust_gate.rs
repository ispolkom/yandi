//! Перенос ядра agent/orchestrator/epistemic/trust_gate.py — вычисление итоговой метки доверия (trust label):
//! таблица рангов `_TRUST_ORDER`, `_apply_trust_cap`, `_calculate_delta_factors` и «решение» из
//! `apply_epistemic_trust_adjustment` (метка + причины по классификации, лимиту, домену/проверяемости, покрытию,
//! поддержке evidence и уверенности убеждений). Запись в `trace` (trust, learning rules) остаётся в Python.
//! Вызывается на КАЖДЫЙ ответ (writeback.py, pipeline.py, canonical_trust.py — тот же _TRUST_ORDER/_apply_trust_cap).
//!
//! Fidelity:
//! * Таблица рангов сгенерирована ИЗ Python (trust_data.rs, gen_trust_data.py); неизвестная метка = ранг 0 (как
//!   `_TRUST_ORDER.get(label, 0)`) — именно это и есть «баг WEAKLY_SUPPORTED» из истории модуля, сохранено.
//! * `round(x, 3)` Python — корректно округляющий алгоритм; здесь `format!("{:.3}")` + разбор (как в срезе 5);
//!   `min(1.0, max(0.0, c))`/`max(-0.5, min(0.5, t))` — питоновские min/max (при NaN: `max(0.0, nan) = 0.0`).
//! * `f"{x:.2f}"` — `format!("{:.2}")`; проверено на «ничьих» (0.125, 0.375 …) parity-тестом.
//! * Порядок применения правил и `elif` между порогами покрытия/поддержки — как в оригинале; список причин
//!   возвращается ПОЛНЫМ (Python берёт `[:4]`).
//! * Средняя уверенность убеждений считается в Python (доступ к объектам/исключения `try/except Exception`)
//!   и приходит сюда числом или None.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_TRUST_GATE_ENGINE=rust (см. agent/orchestrator/epistemic/trust_gate.py).

use crate::trust_data::TRUST_ORDER;
#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::PyDict;

pub fn order(label: &str) -> i64 {
    TRUST_ORDER.iter().find(|(k, _)| *k == label).map(|(_, v)| *v).unwrap_or(0)
}

/// `_apply_trust_cap`
pub fn apply_trust_cap(current: &str, cap: &str) -> String {
    if order(current) > order(cap) {
        cap.to_string()
    } else {
        current.to_string()
    }
}

fn py_min(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}
fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// Python `round(x, 3)` (для float).
fn py_round3(x: f64) -> f64 {
    format!("{x:.3}").parse::<f64>().unwrap_or(x)
}

pub struct Delta {
    pub total: f64,
    pub verification_weight: f64,
    pub confidence_factor: f64,
    pub source_quality: f64,
    pub consensus_factor: f64,
}

/// `_calculate_delta_factors`
pub fn calculate_delta_factors(verdict: &str, confidence: f64, has_sources: bool, agreement: f64, total_nodes: f64) -> Delta {
    let verification_weight = match verdict {
        "VERIFIED" => 0.5,
        "PARTIALLY_VERIFIED" => 0.2,
        "CONFLICT" => -0.2,
        "REJECTED" => -0.5,
        "TIMEOUT" => -0.1,
        _ => 0.0,
    };
    let confidence_factor = py_min(1.0, py_max(0.0, confidence));
    let source_quality = if has_sources { 1.0 } else { 0.7 };
    let consensus_factor = if total_nodes > 0.0 {
        let ratio = agreement / total_nodes;
        0.5 + 0.5 * ratio
    } else {
        0.7
    };
    let total = verification_weight * confidence_factor * source_quality * consensus_factor;
    let total = py_round3(py_max(-0.5, py_min(0.5, total)));
    Delta {
        total,
        verification_weight: py_round3(verification_weight),
        confidence_factor: py_round3(confidence_factor),
        source_quality: py_round3(source_quality),
        consensus_factor: py_round3(consensus_factor),
    }
}

const PROTECTED: &[&str] = &["RELIGIOUS_CLAIM", "METAPHYSICAL_UNTESTABLE", "VALUE_FRAMEWORK", "BOUNDARY_QUESTION", "ONTOLOGICAL_INQUIRY"];

/// Решение `apply_epistemic_trust_adjustment` без записи в trace: (метка, полный список причин).
#[allow(clippy::too_many_arguments)]
pub fn compute_trust_label(
    is_subjective: bool,
    epistemic_trust_label: &str,
    domain: &str,
    testability: &str,
    max_trust_cap: &str,
    is_science_as_model: bool,
    has_entity: bool,
    final_claim_coverage: f64,
    support_grounding: f64,
    avg_belief_conf: Option<f64>,
) -> (String, Vec<String>) {
    let mut label = "UNVERIFIED".to_string();
    let mut reasons: Vec<String> = Vec::new();

    if !is_subjective && epistemic_trust_label != "PARTIALLY_SUPPORTED" && epistemic_trust_label != "UNVERIFIED" {
        label = epistemic_trust_label.to_string();
        reasons.push(format!("эпистемическая классификация: {domain} ({testability})"));
    }

    if !is_subjective {
        let cap_label = max_trust_cap;
        if label != cap_label {
            let old = label.clone();
            label = apply_trust_cap(&label, cap_label);
            if old != label {
                reasons.push(format!("trust понижен с {old} до {label} (cap={cap_label})"));
            }
        }
    }

    let high = |l: &str| matches!(l, "VERIFIED" | "STRONGLY_SUPPORTED" | "EMPIRICALLY_SUPPORTED");

    if !is_subjective && (testability == "interpretive" || testability == "non_falsifiable") {
        if high(&label) {
            label = "PARTIALLY_SUPPORTED".to_string();
            reasons.push("интерпретативный вопрос не может быть STRONGLY_SUPPORTED".to_string());
        }
        reasons.push(format!("ответ дан в рамках {testability} перспективы"));
    }

    if !is_subjective && matches!(domain, "axiological" | "normative" | "philosophical") && high(&label) {
        label = "VALUE_FRAMEWORK".to_string();
        reasons.push("ценностный вопрос не имеет единственного правильного ответа".to_string());
    }

    if !is_subjective && domain == "media_interpretation" && !has_entity {
        reasons.push("фильм не идентифицирован".to_string());
        if label == "STRONGLY_SUPPORTED" || label == "SUPPORTED" {
            label = "PARTIALLY_SUPPORTED".to_string();
        }
    }

    if !is_subjective && is_science_as_model && (label == "STRONGLY_SUPPORTED" || label == "VERIFIED") {
        label = "SUPPORTED".to_string();
        reasons.push("научное утверждение — это модель, а не истина".to_string());
    }

    // FINAL CLAIM COVERAGE TRUST GATE
    if final_claim_coverage < 0.50 {
        reasons.push(format!("низкое покрытие фактических утверждений финального ответа ({final_claim_coverage:.2})"));
        if !PROTECTED.contains(&label.as_str()) {
            label = "UNVERIFIED".to_string();
        }
    } else if final_claim_coverage < 0.80 {
        reasons.push(format!("частичное покрытие фактических утверждений финального ответа ({final_claim_coverage:.2})"));
        if matches!(label.as_str(), "VERIFIED" | "STRONGLY_SUPPORTED" | "EMPIRICALLY_SUPPORTED" | "SUPPORTED") {
            label = "PARTIALLY_SUPPORTED".to_string();
        }
    }

    // EVIDENCE SUPPORT TRUST GATE
    if support_grounding < 0.3 {
        reasons.push("слабое покрытие claims прямыми поддерживающими evidence".to_string());
        if !PROTECTED.contains(&label.as_str()) {
            label = "UNVERIFIED".to_string();
        }
    } else if support_grounding < 0.6 {
        reasons.push("частичное покрытие claims прямыми поддерживающими evidence".to_string());
        if high(&label) {
            label = "PARTIALLY_SUPPORTED".to_string();
        }
    }

    if let Some(avg) = avg_belief_conf {
        if avg < 0.5 && (label == "STRONGLY_SUPPORTED" || label == "SUPPORTED") {
            label = "PARTIALLY_SUPPORTED".to_string();
            reasons.push(format!("средняя уверенность убеждений {avg:.2}"));
        }
    }
    (label, reasons)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "apply_trust_cap")]
fn py_apply_trust_cap(current: &str, cap: &str) -> String {
    apply_trust_cap(current, cap)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "calculate_delta_factors")]
fn py_calculate_delta_factors<'py>(
    py: Python<'py>,
    verdict: &str,
    confidence: f64,
    has_sources: bool,
    agreement: f64,
    total_nodes: f64,
) -> PyResult<Bound<'py, PyDict>> {
    let d = calculate_delta_factors(verdict, confidence, has_sources, agreement, total_nodes);
    let out = PyDict::new_bound(py);
    out.set_item("total", d.total)?;
    out.set_item("verification_weight", d.verification_weight)?;
    out.set_item("confidence_factor", d.confidence_factor)?;
    out.set_item("source_quality", d.source_quality)?;
    out.set_item("consensus_factor", d.consensus_factor)?;
    Ok(out)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "compute_trust_label")]
#[allow(clippy::too_many_arguments)]
fn py_compute_trust_label(
    is_subjective: bool,
    epistemic_trust_label: &str,
    domain: &str,
    testability: &str,
    max_trust_cap: &str,
    is_science_as_model: bool,
    has_entity: bool,
    final_claim_coverage: f64,
    support_grounding: f64,
    avg_belief_conf: Option<f64>,
) -> (String, Vec<String>) {
    compute_trust_label(
        is_subjective, epistemic_trust_label, domain, testability, max_trust_cap, is_science_as_model, has_entity,
        final_claim_coverage, support_grounding, avg_belief_conf,
    )
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "trust_order")]
fn py_trust_order(label: &str) -> i64 {
    order(label)
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_apply_trust_cap, m)?)?;
    m.add_function(wrap_pyfunction!(py_calculate_delta_factors, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_trust_label, m)?)?;
    m.add_function(wrap_pyfunction!(py_trust_order, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cap_lowers_only() {
        assert_eq!(apply_trust_cap("STRONGLY_SUPPORTED", "PARTIALLY_SUPPORTED"), "PARTIALLY_SUPPORTED");
        assert_eq!(apply_trust_cap("UNVERIFIED", "WEAKLY_SUPPORTED"), "UNVERIFIED");
        assert_eq!(apply_trust_cap("WEAKLY_SUPPORTED", "UNVERIFIED"), "UNVERIFIED");
        assert_eq!(apply_trust_cap("NOPE", "NOPE2"), "NOPE"); // оба неизвестны — ранг 0, не выше капа
    }

    #[test]
    fn delta_factors_basics() {
        let d = calculate_delta_factors("VERIFIED", 0.8, true, 2.0, 3.0);
        assert!((d.total - 0.5 * 0.8 * 1.0 * (0.5 + 0.5 * (2.0 / 3.0))).abs() < 5e-4);
        assert_eq!(calculate_delta_factors("REJECTED", 2.0, false, 0.0, 0.0).total, -0.245);
    }

    #[test]
    fn label_pipeline_and_reasons() {
        let (l, r) = compute_trust_label(false, "STRONGLY_SUPPORTED", "scientific", "empirical", "SUPPORTED", false, true, 0.9, 0.9, None);
        assert_eq!(l, "SUPPORTED");
        assert!(r[0].starts_with("эпистемическая классификация") && r[1].contains("trust понижен"));
        let (l2, _) = compute_trust_label(false, "VERIFIED", "x", "y", "VERIFIED", false, true, 0.4, 0.9, None);
        assert_eq!(l2, "UNVERIFIED");
        let (l3, r3) = compute_trust_label(true, "VERIFIED", "x", "y", "VERIFIED", false, true, 0.9, 0.9, Some(0.4));
        assert_eq!(l3, "UNVERIFIED"); // субъективный ответ: метка остаётся UNVERIFIED
        assert!(r3.is_empty());
    }
}
