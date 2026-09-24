//! Перенос agent/source_clustering.py::assign_source_clusters (ядро — попарное сравнение
//! источников и union-find) вместе с сигналами схожести из agent/source_independence_prototype.py,
//! которые оно использует: title_similarity (difflib.SequenceMatcher.ratio по канонизированным
//! заголовкам) и content_fingerprint_similarity (Жаккар по 5-словным шинглам).
//!
//! ЭТО БОЕВОЙ ГОРЯЧИЙ ПУТЬ: assign_source_clusters вызывается из orchestrator/claims/lifecycle.py
//! и async_pipeline.py на КАЖДЫЙ набор evidence, и делает O(n²) попарных сравнений, каждое из
//! которых в Python заново канонизирует, токенизирует и гоняет SequenceMatcher. Здесь канонизация
//! и шинглы считаются ОДИН раз на источник (функции чистые — результат тот же), пары сравниваются
//! уже по готовому.
//!
//! Fidelity — конкретные места, где наивный перенос был бы тихо неверен:
//!
//! 1. `difflib.SequenceMatcher(None, a, b).ratio()` — Ratcliff–Obershelp с ЭВРИСТИКОЙ "autojunk":
//!    если len(b) >= 200, символы, встречающиеся в b чаще `len(b)//100 + 1` раз, считаются
//!    "популярными" и УДАЛЯЮТСЯ из индекса b2j (но потом подхватываются циклами расширения
//!    совпадения). Порт воспроизводит это точно, включая порядок выбора самого раннего лучшего
//!    совпадения (строгое `k > bestsize`). Проверяется дифференциальным фаззингом против настоящего
//!    difflib (см. agent/source_clustering_rust_parity_test.py), включая длинные строки (>=200).
//!    isjunk всегда None => множество junk пусто => "junk-подхват" (последние два цикла
//!    find_longest_match) — заведомо холостой и здесь опущен.
//! 2. Regex `[\w']+` в Python для str: `\w` = `ch.isalnum() or ch == '_'` (CPython sre). Крейт
//!    `regex` понимает `\w` иначе (напр. '²', комбинирующие знаки) — поэтому здесь НЕ regex, а
//!    ручная токенизация по таблице диапазонов, сгенерированной из самого Python
//!    (rustlib/gen_py_word_table.py -> py_word_table.rs). Таблица проверяется по ВСЕМ 1 114 112
//!    кодовым точкам в parity-тесте.
//! 3. Длины/индексы — по СИМВОЛАМ (Vec<char>), не байтам, как Python-строки.
//! 4. Порядок обхода пар i<j и правило union-find (`parent[rb] = ra`, сжатие путей "делением
//!    пополам") совпадают с Python — от них зависит, КАКОЙ элемент станет корнем кластера, а корень
//!    даёт `source_cluster_id = "sc_<root evidence_id>"`.
//! 5. Python-версия ловит исключения сравнения и "проваливается открыто" (пара считается непохожей).
//!    В Rust исключений в чистой логике нет; нестроковые/ломаные (одиночные суррогаты) входы
//!    отсекает PyO3 (TypeError/UnicodeError) — Python-обёртка тогда просто выполняет исходный
//!    Python-путь для этого вызова.
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_SOURCE_CLUSTERING_ENGINE=rust (см. agent/source_clustering.py).

use pyo3::prelude::*;
use std::collections::{HashMap, HashSet};

use crate::claim_identity::canonicalize_claim_text;
use crate::py_word_table::PY_WORD_RANGES;

const TITLE_SIM_THRESHOLD: f64 = 0.55;
const CONTENT_FINGERPRINT_THRESHOLD: f64 = 0.25;

/// Python `ch.isalnum() or ch == '_'` — то, что `re` считает `\w` для str.
pub fn is_py_word_char(c: char) -> bool {
    let cp = c as u32;
    PY_WORD_RANGES
        .binary_search_by(|&(lo, hi)| {
            if cp < lo {
                std::cmp::Ordering::Greater
            } else if cp > hi {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// re.findall(r"[\w']+", text)
fn tokenize(text: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut cur = String::new();
    for c in text.chars() {
        if is_py_word_char(c) || c == '\'' {
            cur.push(c);
        } else if !cur.is_empty() {
            words.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        words.push(cur);
    }
    words
}

/// agent/source_independence_prototype.py::_shingles (n=5)
fn shingles(text: &str) -> HashSet<String> {
    const N: usize = 5;
    let words = tokenize(&canonicalize_claim_text(text));
    let mut out = HashSet::new();
    if words.len() < N {
        if !words.is_empty() {
            out.insert(words.join(" "));
        }
        return out;
    }
    for w in words.windows(N) {
        out.insert(w.join(" "));
    }
    out
}

fn jaccard(sa: &HashSet<String>, sb: &HashSet<String>) -> f64 {
    if sa.is_empty() || sb.is_empty() {
        return 0.0;
    }
    let inter = sa.intersection(sb).count();
    let union = sa.len() + sb.len() - inter;
    if union == 0 {
        0.0
    } else {
        inter as f64 / union as f64
    }
}

/// difflib.SequenceMatcher.find_longest_match при isjunk=None (см. заметку 1 вверху файла).
fn find_longest_match(
    a: &[char],
    b: &[char],
    b2j: &HashMap<char, Vec<usize>>,
    alo: usize,
    ahi: usize,
    blo: usize,
    bhi: usize,
) -> (usize, usize, usize) {
    let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
    let mut j2len: HashMap<usize, usize> = HashMap::new();
    for i in alo..ahi {
        let mut newj2len: HashMap<usize, usize> = HashMap::new();
        if let Some(js) = b2j.get(&a[i]) {
            for &j in js {
                if j < blo {
                    continue;
                }
                if j >= bhi {
                    break;
                }
                let prev = if j > 0 { j2len.get(&(j - 1)).copied().unwrap_or(0) } else { 0 };
                let k = prev + 1;
                newj2len.insert(j, k);
                if k > bestsize {
                    besti = i + 1 - k;
                    bestj = j + 1 - k;
                    bestsize = k;
                }
            }
        }
        j2len = newj2len;
    }
    // Расширение лучшего совпадения не-junk элементами в обе стороны (junk-множество пусто, поэтому
    // проверки `not isbjunk` всегда истинны). Именно здесь подхватываются "популярные" символы,
    // выброшенные autojunk из b2j.
    while besti > alo && bestj > blo && a[besti - 1] == b[bestj - 1] {
        besti -= 1;
        bestj -= 1;
        bestsize += 1;
    }
    while besti + bestsize < ahi && bestj + bestsize < bhi && a[besti + bestsize] == b[bestj + bestsize] {
        bestsize += 1;
    }
    (besti, bestj, bestsize)
}

/// difflib.SequenceMatcher(None, a, b).ratio()
pub fn sequence_ratio(a: &[char], b: &[char]) -> f64 {
    let length = a.len() + b.len();
    if length == 0 {
        return 1.0;
    }

    // __chain_b: индекс позиций каждого символа b + autojunk "популярных" при len(b) >= 200.
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (i, &ch) in b.iter().enumerate() {
        b2j.entry(ch).or_default().push(i);
    }
    let n = b.len();
    if n >= 200 {
        let ntest = n / 100 + 1;
        b2j.retain(|_, idxs| idxs.len() <= ntest);
    }

    // get_matching_blocks: сумма размеров блоков (порядок/склейка соседних блоков на сумму не влияют,
    // но стек LIFO сохранён как в оригинале).
    let mut matches = 0usize;
    let mut queue = vec![(0usize, a.len(), 0usize, b.len())];
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = find_longest_match(a, b, &b2j, alo, ahi, blo, bhi);
        if k > 0 {
            matches += k;
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    2.0 * matches as f64 / length as f64
}

/// agent/source_independence_prototype.py::title_similarity
pub fn title_similarity(title_a: &str, title_b: &str) -> f64 {
    let a: Vec<char> = canonicalize_claim_text(title_a).chars().collect();
    let b: Vec<char> = canonicalize_claim_text(title_b).chars().collect();
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    sequence_ratio(&a, &b)
}

/// agent/source_independence_prototype.py::content_fingerprint_similarity
pub fn content_fingerprint_similarity(text_a: &str, text_b: &str) -> f64 {
    jaccard(&shingles(text_a), &shingles(text_b))
}

/// agent/source_clustering.py::_similar (без ветки except — см. заметку 5)
pub fn similar(title_a: &str, title_b: &str, content_a: &str, content_b: &str) -> bool {
    title_similarity(title_a, title_b) >= TITLE_SIM_THRESHOLD
        || content_fingerprint_similarity(content_a, content_b) >= CONTENT_FINGERPRINT_THRESHOLD
}

struct Uf {
    parent: Vec<usize>,
}

impl Uf {
    fn find(&mut self, mut k: usize) -> usize {
        while self.parent[k] != k {
            self.parent[k] = self.parent[self.parent[k]];
            k = self.parent[k];
        }
        k
    }
    fn union(&mut self, a: usize, b: usize) {
        let (ra, rb) = (self.find(a), self.find(b));
        if ra != rb {
            self.parent[rb] = ra;
        }
    }
}

/// Ядро assign_source_clusters: для каждого элемента — индекс корня его кластера.
pub fn cluster_roots(titles: &[String], contents: &[String]) -> Vec<usize> {
    let n = titles.len();
    // Всё, что зависит только от одного источника, считается ОДИН раз (в Python — на каждую пару).
    let canon_titles: Vec<Vec<char>> = titles.iter().map(|t| canonicalize_claim_text(t).chars().collect()).collect();
    let shingle_sets: Vec<HashSet<String>> = contents.iter().map(|c| shingles(c)).collect();

    let mut uf = Uf { parent: (0..n).collect() };
    for i in 0..n {
        for j in (i + 1)..n {
            if uf.find(i) == uf.find(j) {
                continue;
            }
            let t_sim = if canon_titles[i].is_empty() || canon_titles[j].is_empty() {
                0.0
            } else {
                sequence_ratio(&canon_titles[i], &canon_titles[j])
            };
            let is_similar = t_sim >= TITLE_SIM_THRESHOLD
                || jaccard(&shingle_sets[i], &shingle_sets[j]) >= CONTENT_FINGERPRINT_THRESHOLD;
            if is_similar {
                uf.union(i, j);
            }
        }
    }
    (0..n).map(|i| uf.find(i)).collect()
}

// ── PyO3-обвязка ────────────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(name = "is_word_char")]
fn py_is_word_char(codepoint: u32) -> bool {
    char::from_u32(codepoint).map(is_py_word_char).unwrap_or(false)
}

#[pyfunction]
#[pyo3(name = "sequence_ratio")]
fn py_sequence_ratio(a: &str, b: &str) -> f64 {
    let (a, b): (Vec<char>, Vec<char>) = (a.chars().collect(), b.chars().collect());
    sequence_ratio(&a, &b)
}

#[pyfunction]
#[pyo3(name = "title_similarity")]
fn py_title_similarity(a: &str, b: &str) -> f64 {
    title_similarity(a, b)
}

#[pyfunction]
#[pyo3(name = "content_fingerprint_similarity")]
fn py_content_fingerprint_similarity(a: &str, b: &str) -> f64 {
    content_fingerprint_similarity(a, b)
}

#[pyfunction]
#[pyo3(name = "similar")]
fn py_similar(title_a: &str, title_b: &str, content_a: &str, content_b: &str) -> bool {
    similar(title_a, title_b, content_a, content_b)
}

#[pyfunction]
#[pyo3(name = "cluster_roots")]
fn py_cluster_roots(titles: Vec<String>, contents: Vec<String>) -> PyResult<Vec<usize>> {
    if titles.len() != contents.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("titles и contents должны быть одной длины"));
    }
    Ok(cluster_roots(&titles, &contents))
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_is_word_char, m)?)?;
    m.add_function(wrap_pyfunction!(py_sequence_ratio, m)?)?;
    m.add_function(wrap_pyfunction!(py_title_similarity, m)?)?;
    m.add_function(wrap_pyfunction!(py_content_fingerprint_similarity, m)?)?;
    m.add_function(wrap_pyfunction!(py_similar, m)?)?;
    m.add_function(wrap_pyfunction!(py_cluster_roots, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chars(s: &str) -> Vec<char> {
        s.chars().collect()
    }

    #[test]
    fn ratio_known_difflib_values() {
        // Значения из документации difflib: SequenceMatcher(None, "abcd", "bcde").ratio() == 0.75
        assert_eq!(sequence_ratio(&chars("abcd"), &chars("bcde")), 0.75);
        assert_eq!(sequence_ratio(&chars(""), &chars("")), 1.0);
        assert_eq!(sequence_ratio(&chars("abc"), &chars("")), 0.0);
        assert_eq!(sequence_ratio(&chars("abc"), &chars("abc")), 1.0);
    }

    #[test]
    fn word_char_table_basics() {
        assert!(is_py_word_char('a') && is_py_word_char('Я') && is_py_word_char('_') && is_py_word_char('5'));
        assert!(is_py_word_char('²')); // Python isalnum: True (крейт regex \w — нет)
        assert!(!is_py_word_char(' ') && !is_py_word_char('-') && !is_py_word_char('\''));
    }

    #[test]
    fn tokenize_keeps_apostrophe() {
        assert_eq!(tokenize("don't stop-me, Привет_мир!"), vec!["don't", "stop", "me", "Привет_мир"]);
    }

    #[test]
    fn short_text_single_shingle() {
        assert_eq!(shingles("один два три").len(), 1);
        assert_eq!(shingles("").len(), 0);
        assert_eq!(shingles("a b c d e f").len(), 2);
    }

    #[test]
    fn clusters_syndicated_copies() {
        let titles = vec![
            "Ученые нашли новую экзопланету".to_string(),
            "Учёные нашли новую экзопланету!".to_string(),
            "Совсем другая новость про футбол".to_string(),
        ];
        let contents = vec![String::new(), String::new(), String::new()];
        let roots = cluster_roots(&titles, &contents);
        assert_eq!(roots[0], roots[1]);
        assert_ne!(roots[0], roots[2]);
        assert_eq!(roots[2], 2);
    }
}
