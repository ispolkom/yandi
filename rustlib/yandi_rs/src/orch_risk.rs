//! Перенос agent/orch_risk.py::assess_risk — Risk Engine оркестратора (hardcoded ключевые слова,
//! без LLM): по тексту вопроса решает уровень риска, обязателен ли арбитраж, какой моделью
//! валидировать и сколько нод нужно. Вызывается на КАЖДЫЙ запрос оркестратора (pipeline.py,
//! orch_planner.py, orch_validator.py, pet/chat_orch.py).
//!
//! Fidelity:
//! * `len(query) > 300` в Python считает СИМВОЛЫ (code points); здесь `.chars().count()`, не
//!   `.len()` (байты) — для кириллицы (2 байта/символ) байтовый счёт срабатывал бы уже на 151
//!   символах. Шестой раз подряд эта ловушка (срезы 3/6/11/12/13/14).
//! * Порядок проверок: critical → high → medium → low, первое совпадение выигрывает.
//! * Ключевые слова — ПОДСТРОКИ (не целые слова): "суд" совпадёт внутри "судьба", "иск" внутри
//!   "искусство" — так и в Python, сохранено намеренно (это поведение оригинала, не баг переноса).
//! * Нестроковый вход: Python падает AttributeError (`None.lower()`), Rust-обёртка — TypeError
//!   (PyO3 не принимает None как &str). Оба — падение; реальные вызовы всегда передают str.
//!
//! Возвращает кортеж (risk_level, mandatory_arbitrage, validator_model, nodes_required); настоящий
//! датакласс `RiskResult` строит Python-обёртка (он используется как тип в других местах).
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_ORCH_RISK_ENGINE=rust (см. agent/orch_risk.py).

use crate::py_text::PyLowerExt;
#[cfg(feature = "python")]
use pyo3::prelude::*;

const CRITICAL_KW: &[&str] = &[
    "медицин", "лечени", "диагноз", "болезн", "симптом", "лекарств", "дозировк",
    "юридич", "закон", "суд", "договор", "право", "иск", "штраф",
    "финансов", "инвестиц", "кредит", "налог", "банкрот",
    "безопасност", "взлом", "уязвимост", "exploit",
];
const HIGH_KW: &[&str] = &[
    "хирург", "операци", "вакцин", "антибиотик",
    "наркотик", "алкогол",
    "завещани", "наследств", "арест",
    "криптовалют", "биткоин", "торговл",
];
const MEDIUM_KW: &[&str] = &[
    "совет", "рекоменд", "стоит ли", "как лучше",
    "политик", "религи", "спорн",
];

pub struct Risk {
    pub risk_level: &'static str,
    pub mandatory_arbitrage: bool,
    pub validator_model: &'static str,
    pub nodes_required: i64,
}

/// agent/orch_risk.py::assess_risk
pub fn assess_risk(query: &str) -> Risk {
    let q = query.py_lowercase();

    if CRITICAL_KW.iter().any(|kw| q.contains(kw)) {
        return Risk { risk_level: "critical", mandatory_arbitrage: true, validator_model: "14b", nodes_required: 3 };
    }
    if HIGH_KW.iter().any(|kw| q.contains(kw)) {
        return Risk { risk_level: "high", mandatory_arbitrage: false, validator_model: "14b", nodes_required: 3 };
    }
    if MEDIUM_KW.iter().any(|kw| q.contains(kw)) || query.chars().count() > 300 {
        return Risk { risk_level: "medium", mandatory_arbitrage: false, validator_model: "7b", nodes_required: 2 };
    }
    Risk { risk_level: "low", mandatory_arbitrage: false, validator_model: "7b", nodes_required: 1 }
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "assess_risk")]
fn py_assess_risk(query: &str) -> (String, bool, String, i64) {
    let r = assess_risk(query);
    (r.risk_level.to_string(), r.mandatory_arbitrage, r.validator_model.to_string(), r.nodes_required)
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_assess_risk, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn levels_from_module_demo() {
        assert_eq!(assess_risk("Как лечить кашель?").risk_level, "low"); // "лечить" != "лечени"
        assert_eq!(assess_risk("Как расторгнуть договор аренды?").risk_level, "critical");
        assert_eq!(assess_risk("Как настроить DHT в P2P-сети?").risk_level, "low");
        assert_eq!(assess_risk("Стоит ли вкладывать деньги в биткоин?").risk_level, "high");
        assert_eq!(assess_risk("Привет, как дела?").risk_level, "low");
    }

    #[test]
    fn critical_beats_high() {
        // "вакцин" (high) + "медицин" (critical) → critical выигрывает
        let r = assess_risk("Медицинская вакцинация");
        assert_eq!(r.risk_level, "critical");
        assert!(r.mandatory_arbitrage);
        assert_eq!(r.nodes_required, 3);
    }

    #[test]
    fn substring_semantics_preserved() {
        assert_eq!(assess_risk("судьба").risk_level, "critical"); // "суд" внутри слова
    }

    #[test]
    fn length_counts_chars_not_bytes() {
        // 200 кириллических символов = 400 байт: символьная длина <= 300 → low,
        // байтовая (400) > 300 сработала бы неверно.
        assert_eq!(assess_risk(&"а".repeat(200)).risk_level, "low");
        assert_eq!(assess_risk(&"а".repeat(300)).risk_level, "low");
        assert_eq!(assess_risk(&"а".repeat(301)).risk_level, "medium");
    }
}
