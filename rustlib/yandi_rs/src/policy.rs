//! Перенос чистого ядра agent/policy.py — политика безопасности: сканер секретов (`SecretScanner.scan_text`),
//! проверка shell-команд по block/allow-спискам (`PolicyEngine.check_shell`) и проверка хоста по allowlist сети
//! (`check_network`). Вызывается из agent/skills.py и agent/daemon.py (одобрение команд). Файловый ввод-вывод
//! (`scan_file`/`scan_dir`), аудит-лог и конфиг остаются в Python. Паттерны и списки СГЕНЕРИРОВАНЫ ИЗ Python
//! (policy_data.rs, gen_policy_data.py): в модуле безопасности опечатка = пропущенный секрет.
//!
//! Fidelity:
//! * `pos` — индекс СИМВОЛА (Python `m.start()`), а не байта; `match` — `matched[:80]` по символам.
//! * Порядок находок: паттерны в порядке списка, внутри — слева направо (`finditer`, без перекрытий).
//! * Паттерны с `\b` (sk-, AKIA, hf_) написаны вручную: важны позиции/границы совпадения, а «съедающая» граница
//!   py_regex позиции бы исказила. Логика: ведущая `\b` — левее НЕ словесный символ (Python-`\w`); хвост — максимальная
//!   серия ASCII-символов класса (для AKIA — ровно 16), правее НЕ словесный символ; откат к короткой серии не помогает
//!   (следующий символ был бы словесным), поэтому «максимальная серия» точна.
//! * Остальные паттерны — py_regex: точные `\s`, IGNORECASE (i≈ı≈İ, s≈ſ, k≈K…), классы.
//! * Белый список: `w.lower() in matched.lower()`; безопасные хосты для proxy_creds — регистрозависимо (`h in matched`).
//! * check_shell: `cmd.strip()`; block-лист по подстроке (ПЕРВЫЙ найденный в порядке списка попадает в причину);
//!   allow-лист по `startswith` (без границы слова: "lsblk" проходит по "ls"); check_network: равенство или `.endswith("." + allowed)`.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_POLICY_ENGINE=rust (см. agent/policy.py).

use crate::py_text::PyLowerExt;
use crate::policy_data::*;
use crate::py_text::py_strip;
use crate::source_clustering::is_py_word_char;
use once_cell::sync::Lazy;
#[cfg(feature = "python")]
use pyo3::prelude::*;
use regex::Regex;

static COMPILED: Lazy<Vec<Option<Regex>>> = Lazy::new(|| {
    SECRET_PATTERNS
        .iter()
        .map(|(name, pat)| if matches!(*name, "sk_token" | "aws_access" | "hf_token") { None } else { Some(crate::py_text::py_regex(pat)) })
        .collect()
});

fn is_ascii_alnum(c: char) -> bool {
    c.is_ascii_alphanumeric()
}
fn is_upper_or_digit(c: char) -> bool {
    c.is_ascii_uppercase() || c.is_ascii_digit()
}

/// finditer для `\b<prefix>[класс]{min,}\b` (exact=None) или `\b<prefix>[класс]{n}\b` (exact=Some(n)) — [(начало, конец) в символах].
fn manual_find(chars: &[char], prefix: &str, class: fn(char) -> bool, min: usize, exact: Option<usize>) -> Vec<(usize, usize)> {
    let pre: Vec<char> = prefix.chars().collect();
    let n = chars.len();
    let mut out = Vec::new();
    let mut i = 0;
    while i + pre.len() <= n {
        let lead_ok = i == 0 || !is_py_word_char(chars[i - 1]);
        if lead_ok && chars[i..i + pre.len()] == pre[..] {
            let mut j = i + pre.len();
            let mut cnt = 0usize;
            while j < n && class(chars[j]) {
                j += 1;
                cnt += 1;
                if Some(cnt) == exact {
                    break;
                }
            }
            let len_ok = match exact {
                Some(e) => cnt == e,
                None => cnt >= min,
            };
            let tail_ok = j >= n || !is_py_word_char(chars[j]);
            if len_ok && tail_ok {
                out.push((i, j));
                i = j;
                continue;
            }
        }
        i += 1;
    }
    out
}

pub struct Finding {
    pub kind: &'static str,
    pub matched: String,
    pub pos: usize,
}

fn passes_filters(kind: &str, matched: &str) -> bool {
    let lower = matched.py_lowercase();
    if SECRET_WHITELIST.iter().any(|w| lower.contains(&w.py_lowercase())) {
        return false;
    }
    if kind == "proxy_creds" && SAFE_HOSTS.iter().any(|h| matched.contains(h)) {
        return false;
    }
    true
}

/// `SecretScanner.scan_text` (без поля source)
pub fn scan_text(text: &str) -> Vec<Finding> {
    let chars: Vec<char> = text.chars().collect();
    let mut out = Vec::new();
    for ((kind, _pat), re) in SECRET_PATTERNS.iter().zip(COMPILED.iter()) {
        let spans: Vec<(usize, String)> = match re {
            Some(r) => r.find_iter(text).map(|m| (text[..m.start()].chars().count(), m.as_str().to_string())).collect(),
            None => {
                let found = match *kind {
                    "sk_token" => manual_find(&chars, "sk-", is_ascii_alnum, 20, None),
                    "hf_token" => manual_find(&chars, "hf_", is_ascii_alnum, 30, None),
                    _ => manual_find(&chars, "AKIA", is_upper_or_digit, 0, Some(16)),
                };
                found.into_iter().map(|(s, e)| (s, chars[s..e].iter().collect::<String>())).collect()
            }
        };
        for (pos, matched) in spans {
            if !passes_filters(kind, &matched) {
                continue;
            }
            out.push(Finding { kind, matched: matched.chars().take(80).collect(), pos });
        }
    }
    out
}

/// `PolicyEngine.check_shell` -> (allowed, reason, cmd)
pub fn check_shell(cmd: &str) -> (bool, String, String) {
    let cmd = py_strip(cmd).to_string();
    for bad in SHELL_BLOCKLIST {
        if cmd.contains(bad) {
            return (false, format!("blocklist: {bad}"), cmd);
        }
    }
    for ok in SHELL_ALLOWLIST {
        if cmd.starts_with(ok) {
            return (true, "allowlist".to_string(), cmd);
        }
    }
    (false, "not in allowlist".to_string(), cmd)
}

/// `PolicyEngine.check_network`
pub fn check_network(host: &str) -> bool {
    NETWORK_ALLOW.iter().any(|a| host == *a || host.ends_with(&format!(".{a}")))
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "scan_text")]
fn py_scan_text(text: &str) -> Vec<(String, String, usize)> {
    scan_text(text).into_iter().map(|f| (f.kind.to_string(), f.matched, f.pos)).collect()
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "check_shell")]
fn py_check_shell(cmd: &str) -> (bool, String, String) {
    check_shell(cmd)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "check_network")]
fn py_check_network(host: &str) -> bool {
    check_network(host)
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_scan_text, m)?)?;
    m.add_function(wrap_pyfunction!(py_check_shell, m)?)?;
    m.add_function(wrap_pyfunction!(py_check_network, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_typical_secrets() {
        let t = "key: sk-abcdefghijklmnopqrst1234 и AKIAABCDEFGHIJKLMNOP плюс password = \"hunter2secret\"";
        let kinds: Vec<&str> = scan_text(t).iter().map(|f| f.kind).collect();
        assert!(kinds.contains(&"sk_token") && kinds.contains(&"aws_access") && kinds.contains(&"password"));
    }

    #[test]
    fn whitelist_and_boundaries() {
        assert!(scan_text("api_key = your_token_here_xxxxxxxxxxxx").is_empty()); // whitelist
        assert!(scan_text("xsk-abcdefghijklmnopqrst1234").is_empty()); // нет левой границы
        assert!(scan_text("sk-abcdefghijklmnopqrst1234_").is_empty()); // справа `_` — словесный
        assert!(scan_text("AKIAABCDEFGHIJKLMNOPQ").is_empty()); // 17 символов — правой границы нет
    }

    #[test]
    fn position_in_chars_not_bytes() {
        let f = scan_text("Привет sk-abcdefghijklmnopqrst1234");
        assert_eq!(f[0].pos, 7);
    }

    #[test]
    fn shell_and_network() {
        assert_eq!(check_shell("  rm -rf /  ").1, "blocklist: rm -rf");
        assert_eq!(check_shell("lsblk").1, "allowlist"); // startswith без границы слова — как в Python
        assert_eq!(check_shell("whoami").1, "not in allowlist");
        assert!(check_network("api.openai.com") && check_network("x.api.openai.com") && !check_network("evilapi.openai.com.evil"));
    }
}
