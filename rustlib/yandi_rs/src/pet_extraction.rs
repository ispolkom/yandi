//! Перенос чистых помощников pet/event_extraction.py, pet/fact_extraction.py, pet/commitment_verification.py
//! (срез 30, 2026-09-24): то, что бежит на КАЖДОЕ сообщение пользователя вокруг вызова модели:
//!   * `segment_words`      — `\S+` по сообщению, позиции в СИМВОЛАХ (не байтах); пробельность как у Python (U+001C..1F, U+0085…);
//!   * `json_text`          — подготовка текста ответа модели к `json.loads` (снятие ограждения ```), сам `json.loads` остаётся в Python;
//!   * `validate_candidate` — event_extraction._validate_candidate (проверка кандидата-события от модели);
//!   * `validate_fact`      — fact_extraction._validate (проверка кандидата-факта);
//!   * `looks_secret`       — fact_extraction.looks_secret: «форма» пароля/ключа (лукахеды регулярки написаны вручную);
//!   * `inside_quotation`   — commitment_verification.inside_quotation.
//! Проверки типов строгие: значение приходит из json.loads, поэтому «родные» типы (None/bool/int/float/str/list/dict) разбираются здесь,
//! а любой посторонний тип → функция возвращает None и Python-обёртка выполняет исходный код (ровно как раньше).
//! Константы (типы событий, классы фактов…) передаются из Python при каждом вызове — единственный источник правды остаётся в Python.

#[cfg(feature = "python")]
use pyo3::exceptions::PyKeyError;
#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::py_text::{is_py_space, py_decimal_value, py_strip};

// ---------- чистое ядро ----------

/// (слово, старт, конец) в символах.
pub fn segment_words(message: &str) -> Vec<(String, usize, usize)> {
    let mut out = Vec::new();
    let mut start: Option<usize> = None;
    let mut buf = String::new();
    let mut idx = 0usize;
    for c in message.chars() {
        if is_py_space(c) {
            if let Some(s) = start.take() {
                out.push((std::mem::take(&mut buf), s, idx));
            }
        } else {
            if start.is_none() {
                start = Some(idx);
            }
            buf.push(c);
        }
        idx += 1;
    }
    if let Some(s) = start {
        out.push((buf, s, idx));
    }
    out
}

/// Текст для json.loads: как в `_json_object` до вызова json.loads (raw уже str).
pub fn json_text(raw: &str) -> String {
    let text = py_strip(raw);
    if text.starts_with("```") {
        let t = text.trim_matches('`');
        let t = match t.find('{') {
            Some(i) => &t[i..],
            None => t,
        };
        let end = match t.rfind('}') {
            Some(i) => i + 1,
            None => 0,
        };
        return t[..end].to_string();
    }
    text.to_string()
}

fn secret_class(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '+' | '/' | '=')
}

/// `(?=[C]*\d)(?=[C]*[A-Za-z])[C]{20,}` | `-----BEGIN [A-Z ]*KEY-----` — поиск существования.
pub fn looks_secret(text: &str) -> bool {
    let ch: Vec<char> = text.chars().collect();
    let n = ch.len();
    // альтернатива 1: достаточно проверить начало каждого максимального прогона (условия монотонны по старту)
    let mut i = 0;
    while i < n {
        if !secret_class(ch[i]) {
            i += 1;
            continue;
        }
        let s = i;
        while i < n && secret_class(ch[i]) {
            i += 1;
        }
        let e = i; // конец прогона: ch[e] (если есть) НЕ из класса
        if e - s >= 20 {
            let letter = ch[s..e].iter().any(|c| c.is_ascii_alphabetic());
            let digit = ch[s..e].iter().any(|c| c.is_ascii_digit()) || (e < n && py_decimal_value(ch[e]).is_some());
            if letter && digit {
                return true;
            }
        }
    }
    // альтернатива 2
    let pre: Vec<char> = "-----BEGIN ".chars().collect();
    let key: Vec<char> = "KEY-----".chars().collect();
    if n >= pre.len() {
        for a in 0..=(n - pre.len()) {
            if ch[a..a + pre.len()] != pre[..] {
                continue;
            }
            let p = a + pre.len();
            let mut q = p;
            loop {
                if q + key.len() <= n && ch[q..q + key.len()] == key[..] {
                    return true;
                }
                if q < n && (ch[q].is_ascii_uppercase() || ch[q] == ' ') {
                    q += 1;
                } else {
                    break;
                }
            }
        }
    }
    false
}

pub fn inside_quotation(message: &str, position: usize) -> bool {
    let mut stack: Vec<char> = Vec::new();
    for c in message.chars().take(position) {
        if stack.last() == Some(&c) {
            stack.pop();
        } else if let Some(close) = match c {
            '«' => Some('»'),
            '„' => Some('“'),
            '“' => Some('”'),
            _ => None,
        } {
            stack.push(close);
        } else if c == '"' {
            stack.push('"');
        }
    }
    !stack.is_empty()
}

// ---------- разбор «родных» типов из json.loads ----------

#[cfg(feature = "python")]
pub(crate) enum V<'py> {
    None,
    Bool,
    Int(Bound<'py, PyAny>),
    Float(f64),
    Str(String),
    List(Bound<'py, PyList>),
    Dict(Bound<'py, PyDict>),
}

#[cfg(feature = "python")]
/// None = посторонний тип (подкласс, кортеж, Decimal…) → вызывающий откатывается на Python.
/// Строка с одиноким суррогатом даёт Err(UnicodeEncodeError) — обёртка тоже откатывается.
pub(crate) fn classify<'py>(o: &Bound<'py, PyAny>) -> PyResult<Option<V<'py>>> {
    if o.is_none() {
        return Ok(Some(V::None));
    }
    if o.is_exact_instance_of::<PyBool>() {
        return Ok(Some(V::Bool));
    }
    if o.is_exact_instance_of::<PyInt>() {
        return Ok(Some(V::Int(o.clone())));
    }
    if o.is_exact_instance_of::<PyFloat>() {
        return Ok(Some(V::Float(o.extract::<f64>()?)));
    }
    if o.is_exact_instance_of::<PyString>() {
        return Ok(Some(V::Str(o.extract::<String>()?)));
    }
    if o.is_exact_instance_of::<PyList>() {
        return Ok(Some(V::List(o.downcast::<PyList>()?.clone())));
    }
    if o.is_exact_instance_of::<PyDict>() {
        return Ok(Some(V::Dict(o.downcast::<PyDict>()?.clone())));
    }
    Ok(None)
}

#[cfg(feature = "python")]
/// Значение целого: Some(i64) либо None для огромного (тогда любое сравнение с диапазоном слов — «вне диапазона»).
pub(crate) fn small_int(o: &Bound<'_, PyAny>) -> Option<i64> {
    o.extract::<i64>().ok()
}


/// Разбор `span`: Ok(Ok((first,last))) — два целых; Ok(Err(())) — «не два целых»; Err(fallback).
enum SpanKind {
    TwoInts(Option<i64>, Option<i64>), // None = огромное целое
    NotTwoInts,
}

#[cfg(feature = "python")]
fn parse_span(span: Option<Bound<'_, PyAny>>) -> PyResult<Option<SpanKind>> {
    let span = match span {
        None => return Ok(Some(SpanKind::NotTwoInts)),
        Some(s) => s,
    };
    match classify(&span)? {
        None => Ok(None),
        Some(V::List(l)) => {
            if l.len() != 2 {
                // Python: isinstance list и len==2 — иначе «не два целых»; элементы при len≠2 не смотрятся
                return Ok(Some(SpanKind::NotTwoInts));
            }
            let mut vals = [None, None];
            let mut all_int = true;
            for (k, x) in l.iter().enumerate() {
                match classify(&x)? {
                    None => return Ok(None),
                    Some(V::Int(i)) => vals[k] = small_int(&i),
                    Some(_) => all_int = false,
                }
            }
            if all_int {
                Ok(Some(SpanKind::TwoInts(vals[0], vals[1])))
            } else {
                Ok(Some(SpanKind::NotTwoInts))
            }
        }
        Some(_) => Ok(Some(SpanKind::NotTwoInts)),
    }
}

/// `0 <= first <= last < nwords`; огромное целое всегда «вне диапазона».
fn span_in_range(a: Option<i64>, b: Option<i64>, nwords: usize) -> Option<(i64, i64)> {
    match (a, b) {
        (Some(first), Some(last)) if 0 <= first && first <= last && (last as i128) < nwords as i128 => Some((first, last)),
        _ => None,
    }
}

#[cfg(feature = "python")]
/// `_in_unit_range` + `float()`: Some(значение) если int/float (не bool) в 0..1.
fn unit_value(o: Option<Bound<'_, PyAny>>) -> PyResult<Result<Option<f64>, ()>> {
    // Ok(Ok(Some(v))) годно; Ok(Ok(None)) не годно; Ok(Err(())) посторонний тип → откат
    let o = match o {
        None => return Ok(Ok(None)),
        Some(o) => o,
    };
    match classify(&o)? {
        None => Ok(Err(())),
        Some(V::Float(f)) => Ok(Ok(if (0.0..=1.0).contains(&f) { Some(f) } else { None })),
        Some(V::Int(i)) => Ok(Ok(match small_int(&i) {
            Some(0) => Some(0.0),
            Some(1) => Some(1.0),
            _ => None,
        })),
        Some(_) => Ok(Ok(None)),
    }
}

#[cfg(feature = "python")]
fn get<'py>(d: &Bound<'py, PyDict>, key: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    d.get_item(key)
}

#[cfg(feature = "python")]
/// str_in: Some(true/false) либо None → откат. Значения не-строки никогда не равны строке из кортежа.
fn str_in(o: &Option<Bound<'_, PyAny>>, allowed: &[String]) -> PyResult<Option<bool>> {
    let o = match o {
        None => return Ok(Some(false)), // dict.get → None → не входит
        Some(o) => o,
    };
    match classify(o)? {
        None => Ok(None),
        Some(V::Str(s)) => Ok(Some(allowed.iter().any(|a| *a == s))),
        Some(_) => Ok(Some(false)),
    }
}

// ---------- PyO3 ----------

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "segment_words")]
fn py_segment_words(message: &str) -> Vec<(String, usize, usize)> {
    segment_words(message)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "json_text")]
fn py_json_text(raw: &str) -> String {
    json_text(raw)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "looks_secret")]
fn py_looks_secret(text: &str) -> bool {
    looks_secret(text)
}

#[cfg(feature = "python")]
#[pyfunction]
#[pyo3(name = "inside_quotation")]
fn py_inside_quotation(message: &str, position: usize) -> bool {
    inside_quotation(message, position)
}

#[cfg(feature = "python")]
/// event_extraction._validate_candidate. Возвращает None, если пришёл посторонний тип (тогда выполняется Python).
#[pyfunction]
#[pyo3(name = "validate_candidate")]
fn py_validate_candidate(
    py: Python<'_>, item: &Bound<'_, PyAny>, nwords: usize, event_types: Vec<String>, insult: &str, apology: &str, max_span_words: i64,
) -> PyResult<Option<PyObject>> {
    let fail = |reason: &str| -> PyResult<Option<PyObject>> { Ok(Some((py.None(), reason).into_py(py))) };
    let d = match classify(item)? {
        None => return Ok(None),
        Some(V::Dict(d)) => d,
        Some(_) => return fail("candidate is not an object"),
    };
    let kind_o = get(&d, "type")?;
    match str_in(&kind_o, &event_types)? {
        None => return Ok(None),
        Some(false) => return fail("unknown event type"),
        Some(true) => {}
    }
    let kind = match classify(kind_o.as_ref().unwrap())? {
        Some(V::Str(s)) => s,
        _ => return Ok(None),
    };
    let (a, b) = match parse_span(get(&d, "span")?)? {
        None => return Ok(None),
        Some(SpanKind::NotTwoInts) => return fail("span is not two integers"),
        Some(SpanKind::TwoInts(a, b)) => (a, b),
    };
    let (first, last) = match span_in_range(a, b, nwords) {
        None => return fail("span reference out of range"),
        Some(x) => x,
    };
    if last - first + 1 > max_span_words {
        return fail("span too long to be one event");
    }
    let (mut severity, mut sincerity) = (0.0f64, 0.0f64);
    if kind == insult {
        match unit_value(get(&d, "severity")?)? {
            Err(()) => return Ok(None),
            Ok(None) => return fail("insult without a severity in 0..1"),
            Ok(Some(v)) => severity = v,
        }
    }
    if kind == apology {
        match unit_value(get(&d, "sincerity")?)? {
            Err(()) => return Ok(None),
            Ok(None) => return fail("apology without a sincerity in 0..1"),
            Ok(Some(v)) => sincerity = v,
        }
    }
    let cand = PyTuple::new_bound(py, [kind.into_py(py), first.into_py(py), last.into_py(py), severity.into_py(py), sincerity.into_py(py)]);
    Ok(Some((cand, "").into_py(py)))
}

#[cfg(feature = "python")]
/// fact_extraction._validate. Возвращает None, если пришёл посторонний тип.
#[pyfunction]
#[pyo3(name = "validate_fact")]
#[allow(clippy::too_many_arguments)]
fn py_validate_fact(
    py: Python<'_>, item: &Bound<'_, PyAny>, nwords: usize, known: &Bound<'_, PyAny>, fact_classes: Vec<String>, secret_class: &str,
    polarities: Vec<String>, times: Vec<String>, relations: Vec<String>, max_span_words: i64, max_known: usize, statement_min: usize,
    statement_max: usize,
) -> PyResult<Option<PyObject>> {
    let fail = |reason: &str| -> PyResult<Option<PyObject>> { Ok(Some((py.None(), reason).into_py(py))) };
    let d = match classify(item)? {
        None => return Ok(None),
        Some(V::Dict(d)) => d,
        Some(_) => return fail("candidate is not an object"),
    };
    let (a, b) = match parse_span(get(&d, "span")?)? {
        None => return Ok(None),
        Some(SpanKind::NotTwoInts) => return fail("span is not two integers"),
        Some(SpanKind::TwoInts(a, b)) => (a, b),
    };
    let (first, last) = match span_in_range(a, b, nwords) {
        None => return fail("span reference out of range"),
        Some(x) => x,
    };
    if last - first + 1 > max_span_words {
        return fail("span too long to be one fact");
    }
    let class_o = get(&d, "class")?;
    if let Some(o) = &class_o {
        match classify(o)? {
            None => return Ok(None),
            Some(V::Str(s)) if s == secret_class => return fail("the extractor itself marked it a secret"),
            _ => {}
        }
    }
    match str_in(&class_o, &fact_classes)? {
        None => return Ok(None),
        Some(false) => return fail("unknown fact class"),
        Some(true) => {}
    }
    let stmt_o = get(&d, "statement")?;
    let statement: String = match &stmt_o {
        None => return fail("statement missing or malformed"),
        Some(o) => match classify(o)? {
            None => return Ok(None),
            Some(V::Str(s)) => s,
            Some(_) => return fail("statement missing or malformed"),
        },
    };
    let stripped = py_strip(&statement);
    let slen = stripped.chars().count();
    if !(statement_min <= slen && slen <= statement_max && !statement.contains('\n')) {
        return fail("statement missing or malformed");
    }
    let pol_o = get(&d, "polarity")?;
    let pol_in = match str_in(&pol_o, &polarities)? {
        None => return Ok(None),
        Some(v) => v,
    };
    if !pol_in {
        return fail("polarity or time missing");
    }
    let time_o = get(&d, "time")?;
    match str_in(&time_o, &times)? {
        None => return Ok(None),
        Some(false) => return fail("polarity or time missing"),
        Some(true) => {}
    }
    match get(&d, "stability")? {
        None => return fail("not a stable fact"),
        Some(o) => match classify(&o)? {
            None => return Ok(None),
            Some(V::Str(s)) if s == "stable" => {}
            Some(_) => return fail("not a stable fact"),
        },
    }
    // relation: dict.get("relation", "none")
    let rel_o = get(&d, "relation")?;
    let mut relation: String = match &rel_o {
        None => "none".to_string(),
        Some(o) => match classify(o)? {
            None => return Ok(None),
            Some(V::Str(s)) if relations.iter().any(|r| *r == s) => s,
            Some(_) => return fail("unknown relation"),
        },
    };
    let mut target: Option<PyObject> = None;
    if relation != "none" {
        let mut resolved = false;
        if let Some(idx_o) = get(&d, "target")? {
            match classify(&idx_o)? {
                None => return Ok(None),
                Some(V::Int(i)) => {
                    // known: точный list, иначе откат
                    let kl = match classify(known)? {
                        Some(V::List(l)) => l,
                        _ => return Ok(None),
                    };
                    let bound = kl.len().min(max_known);
                    if let Some(idx) = small_int(&i) {
                        if idx >= 0 && (idx as u128) < bound as u128 {
                            let entry = kl.get_item(idx as usize)?;
                            let ed = match classify(&entry)? {
                                Some(V::Dict(ed)) => ed,
                                _ => return Ok(None),
                            };
                            match ed.get_item("fact_id")? {
                                Some(v) => target = Some(v.unbind()),
                                None => return Err(PyKeyError::new_err("fact_id")),
                            }
                            resolved = true;
                        }
                    }
                }
                Some(_) => {}
            }
        }
        if !resolved {
            if relation == "same" {
                relation = "none".to_string();
            } else {
                return fail("relation target is not one of the known facts");
            }
        }
    }
    let out = PyDict::new_bound(py);
    out.set_item("class", class_o.unwrap())?;
    out.set_item("statement", stripped)?;
    out.set_item("polarity", pol_o.unwrap())?;
    out.set_item("time", time_o.unwrap())?;
    out.set_item("first", first)?;
    out.set_item("last", last)?;
    out.set_item("relation", relation)?;
    out.set_item("target", target)?;
    Ok(Some((out, "").into_py(py)))
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_segment_words, m)?)?;
    m.add_function(wrap_pyfunction!(py_json_text, m)?)?;
    m.add_function(wrap_pyfunction!(py_looks_secret, m)?)?;
    m.add_function(wrap_pyfunction!(py_inside_quotation, m)?)?;
    m.add_function(wrap_pyfunction!(py_validate_candidate, m)?)?;
    m.add_function(wrap_pyfunction!(py_validate_fact, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn words() {
        let w = segment_words("  привет мир\u{1c}x\u{a0}y ");
        assert_eq!(w.iter().map(|x| x.0.as_str()).collect::<Vec<_>>(), vec!["привет", "мир", "x", "y"]);
        assert_eq!((w[0].1, w[0].2), (2, 8));
        assert_eq!((w[1].1, w[1].2), (9, 12));
    }

    #[test]
    fn fence() {
        assert_eq!(json_text("  {\"a\":1} "), "{\"a\":1}");
        assert_eq!(json_text("```json\n{\"a\":1}\n```"), "{\"a\":1}");
        assert_eq!(json_text("```no braces```"), "");
    }

    #[test]
    fn secrets() {
        assert!(looks_secret("sk1234567890abcdefghij"));
        assert!(!looks_secret("abcdefghijklmnopqrstuvwxyz")); // нет цифры
        assert!(!looks_secret("12345678901234567890")); // нет буквы
        assert!(looks_secret("-----BEGIN RSA PRIVATE KEY-----"));
        assert!(!looks_secret("короткий текст"));
        assert!(looks_secret("abcdefghijklmnopqrst\u{663}")); // Unicode-цифра сразу за прогоном закрывает lookahead \d
    }

    #[test]
    fn quotes() {
        assert!(inside_quotation("он сказал «привет", 12));
        assert!(!inside_quotation("он сказал «привет» ок", 20));
        assert!(inside_quotation("say \"hi", 100));
    }
}
