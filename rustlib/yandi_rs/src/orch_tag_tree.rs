//! Перенос чистого ядра agent/orch_tag_tree.py (срез 31, 2026-09-24): токенизация запроса для классификации тегов
//! (`_tokenize`), LSH-бакет токена (`_lsh_bucket`) и нормализованная энтропия Шеннона по бакетам (`lsh_entropy`, а также
//! пересчёт энтропии узла по гистограмме внутри `TagTree.update`).
//! Деревом, файлом tree_file, временем и JSON-сохранением по-прежнему занимается Python.
//!
//! Точность: `.lower()` = `py_text::py_lower` (таблица из самого Python, сверено по КАЖДОЙ кодовой точке в parity-тесте; `str::to_lowercase` знает более новый Unicode и расходится на 8 символах); `\b[а-яёa-z]{3,}\b` — граница слова
//! по питоновскому `\w` (таблица py_word_table): токен = максимальный прогон [а-яёa-z], у которого сосед слева и справа не `\w`;
//! `round(x, 4)` — через форматирование `{:.4}` (корректное округление, как в CPython); логарифм — тот же libm, что у `math.log`.

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use std::collections::HashSet;

use crate::py_text::py_lower;
use crate::source_clustering::is_py_word_char;

/// Копия `_STOPWORDS` из Python; parity-тест сверяет её с оригиналом (и обёртка не делегирует, если множество в Python изменили).
pub const STOPWORDS: [&str; 17] = [
    "как", "что", "где", "когда", "почему", "зачем", "можно", "нужно", "это", "есть", "быть", "мне", "мой", "моя", "для", "при", "без",
];
const STOPWORDS_EN: [&str; 8] = ["the", "is", "are", "how", "what", "where", "why", "can"];

static STOP: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    let mut s: HashSet<&'static str> = STOPWORDS.iter().copied().collect();
    s.extend(STOPWORDS_EN.iter().copied());
    s.insert("do");
    s
});

fn in_class(c: char) -> bool {
    c.is_ascii_lowercase() || ('а'..='я').contains(&c) || c == 'ё'
}

/// `re.findall(r'\b[а-яёa-z]{3,}\b', text.lower())` без стоп-слов.
pub fn tokenize(text: &str) -> Vec<String> {
    let lower = py_lower(text);
    let ch: Vec<char> = lower.chars().collect();
    let n = ch.len();
    let mut out = Vec::new();
    let mut i = 0;
    while i < n {
        if !in_class(ch[i]) {
            i += 1;
            continue;
        }
        let s = i;
        while i < n && in_class(ch[i]) {
            i += 1;
        }
        let e = i;
        let left_ok = s == 0 || !is_py_word_char(ch[s - 1]);
        let right_ok = e == n || !is_py_word_char(ch[e]);
        if e - s >= 3 && left_ok && right_ok {
            let tok: String = ch[s..e].iter().collect();
            if !STOP.contains(tok.as_str()) {
                out.push(tok);
            }
        }
    }
    out
}

pub fn lsh_bucket(token: &str, n_buckets: u64) -> u64 {
    let n = n_buckets as u128;
    let mut h: u128 = 0;
    for c in token.chars() {
        h = (h * 31 + c as u128) % n;
    }
    h as u64
}

fn py_round4(x: f64) -> f64 {
    format!("{x:.4}").parse::<f64>().unwrap_or(x)
}

/// Энтропия по гистограмме; None, если сумма 0 (вызывающий решает, что тогда: 0.0 или «не менять»).
pub fn entropy_from_counts(counts: &[u64], n_buckets: usize) -> Option<f64> {
    let total: u64 = counts.iter().sum();
    if total == 0 {
        return None;
    }
    let log_n = (n_buckets as f64).ln();
    let mut entropy = 0.0f64;
    for &c in counts {
        if c > 0 {
            let p = c as f64 / total as f64;
            entropy -= p * p.ln();
        }
    }
    Some(py_round4(entropy / log_n))
}

pub fn lsh_entropy(queries: &[String], n_buckets: usize) -> f64 {
    if queries.is_empty() {
        return 0.0;
    }
    let mut counts = vec![0u64; n_buckets];
    for q in queries {
        for t in tokenize(q) {
            counts[lsh_bucket(&t, n_buckets as u64) as usize] += 1;
        }
    }
    entropy_from_counts(&counts, n_buckets).unwrap_or(0.0)
}

#[pyfunction]
#[pyo3(name = "tokenize")]
fn py_tokenize(text: &str) -> Vec<String> {
    tokenize(text)
}

#[pyfunction]
#[pyo3(name = "lsh_bucket")]
fn py_lsh_bucket(token: &str, n_buckets: u64) -> u64 {
    lsh_bucket(token, n_buckets)
}

#[pyfunction]
#[pyo3(name = "lsh_entropy")]
fn py_lsh_entropy(queries: Vec<String>, n_buckets: usize) -> f64 {
    lsh_entropy(&queries, n_buckets)
}

/// Энтропия узла по его гистограмме (`TagTree.update`): None — сумма 0, энтропия узла не меняется.
#[pyfunction]
#[pyo3(name = "entropy_from_hist")]
fn py_entropy_from_hist(counts: Vec<u64>) -> Option<f64> {
    let n = counts.len();
    entropy_from_counts(&counts, n)
}

#[pyfunction]
#[pyo3(name = "stopwords")]
fn py_stopwords() -> Vec<String> {
    STOP.iter().map(|s| s.to_string()).collect()
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_tokenize, m)?)?;
    m.add_function(wrap_pyfunction!(py_lsh_bucket, m)?)?;
    m.add_function(wrap_pyfunction!(py_lsh_entropy, m)?)?;
    m.add_function(wrap_pyfunction!(py_entropy_from_hist, m)?)?;
    m.add_function(wrap_pyfunction!(py_stopwords, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokens() {
        assert_eq!(tokenize("Как настроить Python-сервер? the api"), vec!["настроить", "python", "сервер", "api"]);
        assert_eq!(tokenize("ab abc abc1 _abc abc_ ёжик"), vec!["abc", "ёжик"]);
    }

    #[test]
    fn bucket() {
        assert_eq!(lsh_bucket("abc", 32), ((97u64 * 31 + 98) * 31 + 99) % 32);
    }

    #[test]
    fn entropy() {
        assert_eq!(lsh_entropy(&[], 32), 0.0);
        assert_eq!(lsh_entropy(&["привет привет".to_string()], 32), 0.0);
        assert!(entropy_from_counts(&[0; 32], 32).is_none());
        let e = entropy_from_counts(&[1; 32], 32).unwrap();
        assert!((e - 1.0).abs() < 1e-9);
    }
}
