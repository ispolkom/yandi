//! Перенос охранного шлюза agent/tools/tool_shell.py::_allowed — можно ли агенту выполнить shell-команду: сначала
//! запрещённые паттерны (rm, mv, cp, wget, curl, chmod, sudo, kill…, метасимволы `|&;`$`, `..`), затем белый список
//! разрешённых начал (`^ls`, `^python(\d[\d.]*)?`, `^cargo\s(test|…)` …). Выполнение (`subprocess`) остаётся в Python.
//! Паттерны СГЕНЕРИРОВАНЫ ИЗ Python (tool_shell_data.rs, gen_tool_shell_data.py).
//!
//! Fidelity: `cmd.strip()` (питоновские пробелы, U+001C..1F); `AGENT_SHELL_FULL` (любое непустое значение — истина,
//! даже "0") -> разрешено ВСЁ; `AGENT_SHELL_NET` (непусто) снимает запрет только с `\bcurl\b`/`\bwget\b`; все запреты
//! проверяются ДО белого списка; `\b`, `\s`, `\d` (Unicode-цифры: «python٣ x» подходит под `^python(\d[\d.]*)?`) — через
//! точный py_regex (только is_match). Флаги окружения читает Python-обёртка и передаёт булевыми значениями.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_TOOL_SHELL_ENGINE=rust (см. agent/tools/tool_shell.py).

use crate::py_text::py_strip;
use crate::tool_shell_data::{BANNED, WHITELIST};
use once_cell::sync::Lazy;
#[cfg(feature = "python")]
use pyo3::prelude::*;
use regex::Regex;

static BANNED_RE: Lazy<Vec<Regex>> = Lazy::new(|| BANNED.iter().map(|p| crate::py_text::py_regex(p)).collect());
static WHITELIST_RE: Lazy<Vec<Regex>> = Lazy::new(|| WHITELIST.iter().map(|p| crate::py_text::py_regex(p)).collect());

/// `_allowed(cmd)` при заданных флагах окружения
pub fn allowed(cmd: &str, full: bool, net: bool) -> bool {
    let cmd = py_strip(cmd);
    if full {
        return true;
    }
    for (pat, re) in BANNED.iter().zip(BANNED_RE.iter()) {
        if net && (*pat == r"\bcurl\b" || *pat == r"\bwget\b") {
            continue;
        }
        if re.is_match(cmd) {
            return false;
        }
    }
    WHITELIST_RE.iter().any(|re| re.is_match(cmd))
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "allowed")]
fn py_allowed(cmd: &str, full: bool, net: bool) -> bool {
    allowed(cmd, full, net)
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_allowed, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn typical() {
        assert!(allowed("ls -la", false, false));
        assert!(allowed("pwd", false, false));
        assert!(!allowed("rm -rf x", false, false));
        assert!(!allowed("ls | wc", false, false)); // пайп запрещён
        assert!(!allowed("cat ../secret", false, false));
        assert!(!allowed("whoami", false, false)); // не в белом списке
        assert!(allowed("cargo test --lib", false, false));
        assert!(!allowed("cargo run", false, false));
    }

    #[test]
    fn flags() {
        assert!(allowed("rm -rf /", true, false)); // FULL — всё
        assert!(!allowed("curl x", false, false));
        assert!(!allowed("curl x", false, true)); // NET снимает запрет, но curl не в белом списке
        assert!(allowed("python3 x.py", false, false));
        assert!(allowed("python\u{663} x", false, false)); // Unicode-цифра под \d
    }
}
