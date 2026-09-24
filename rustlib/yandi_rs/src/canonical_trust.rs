//! Перенос agent/orchestrator/epistemic/canonical_trust.py::compute_canonical_trust — сведение двух «нитей»
//! доверия (метка синтезатора и метка trust-gate) в одну каноническую: при доступности обеих берётся более
//! строгая по общей таблице рангов (та же `_TRUST_ORDER`/`_apply_trust_cap`, что в trust_gate). Вызывается на
//! каждый ответ. Логирование (`log(...)` при verbose) остаётся в Python: Rust возвращает значения.
//!
//! Fidelity: «пустая» метка = None ИЛИ пустая строка (`not x`); ветки `neither_available` /
//! `synthesizer_only` / `trust_gate_only` возвращаются РАНЬШЕ сравнения и без лога; неизвестная метка = ранг 0;
//! `diverged` в итоговом dict = `final != gate` (а не `canonical != final`) — как в оригинале.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_CANONICAL_TRUST_ENGINE=rust (см. agent/orchestrator/epistemic/canonical_trust.py).

use crate::trust_gate::{apply_trust_cap, order};
#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::PyDict;

pub struct Canonical {
    pub canonical_trust: String,
    pub diverged: bool,
    pub stricter_strand: &'static str,
    pub reason: String,
}

fn empty(x: &Option<String>) -> bool {
    x.as_deref().map_or(true, |s| s.is_empty())
}

/// compute_canonical_trust (без лога)
pub fn compute(final_trust: Option<String>, gate: Option<String>) -> Canonical {
    if empty(&final_trust) && empty(&gate) {
        return Canonical {
            canonical_trust: "UNVERIFIED".to_string(),
            diverged: false,
            stricter_strand: "neither_available",
            reason: "neither strand produced a value".to_string(),
        };
    }
    if empty(&gate) {
        return Canonical {
            canonical_trust: final_trust.unwrap_or_default(),
            diverged: false,
            stricter_strand: "synthesizer_only",
            reason: "trust_gate strand unavailable (e.g. synthesis timed out)".to_string(),
        };
    }
    if empty(&final_trust) {
        return Canonical {
            canonical_trust: gate.unwrap_or_default(),
            diverged: false,
            stricter_strand: "trust_gate_only",
            reason: "synthesizer strand unavailable".to_string(),
        };
    }
    let (f, g) = (final_trust.unwrap(), gate.unwrap());
    let canonical = apply_trust_cap(&f, &g);
    let (of, og) = (order(&f), order(&g));
    let stricter = if og < of {
        "trust_gate"
    } else if of < og {
        "synthesizer"
    } else {
        "equal"
    };
    let reason = format!("synthesizer_strand={f} trust_gate_strand={g} -> canonical={canonical}");
    Canonical { canonical_trust: canonical, diverged: f != g, stricter_strand: stricter, reason }
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "compute_canonical_trust", signature = (final_trust=None, gate=None))]
fn py_compute<'py>(py: Python<'py>, final_trust: Option<String>, gate: Option<String>) -> PyResult<Bound<'py, PyDict>> {
    let c = compute(final_trust, gate);
    let d = PyDict::new_bound(py);
    d.set_item("canonical_trust", c.canonical_trust)?;
    d.set_item("diverged", c.diverged)?;
    d.set_item("stricter_strand", c.stricter_strand)?;
    d.set_item("reason", c.reason)?;
    Ok(d)
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_compute, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn branches() {
        assert_eq!(compute(None, None).stricter_strand, "neither_available");
        assert_eq!(compute(Some(String::new()), None).canonical_trust, "UNVERIFIED");
        assert_eq!(compute(Some("SUPPORTED".into()), None).stricter_strand, "synthesizer_only");
        assert_eq!(compute(None, Some("VERIFIED".into())).stricter_strand, "trust_gate_only");
        let c = compute(Some("STRONGLY_SUPPORTED".into()), Some("PARTIALLY_SUPPORTED".into()));
        assert_eq!((c.canonical_trust.as_str(), c.stricter_strand, c.diverged), ("PARTIALLY_SUPPORTED", "trust_gate", true));
        let e = compute(Some("SUPPORTED".into()), Some("VERIFIED".into())); // ранги равны (4 == 4)
        assert_eq!((e.stricter_strand, e.diverged, e.canonical_trust.as_str()), ("equal", true, "SUPPORTED"));
    }
}
