//! Точный порт Python `json.loads(str)` (C-сканер CPython 3.11, Lib/json + Modules/_json.c) —
//! вместо `serde_json`, который принимает и отвергает ДРУГОЕ:
//!   * Python принимает литералы `NaN`, `Infinity`, `-Infinity`, число `1e999` (= inf), одиночные
//!     суррогаты `"\ud800"`; serde_json — нет (=> ветка "malformed JSON", тогда как в Python данные
//!     разбираются и модель получает ok=True);
//!   * тексты ошибок: Python — `Expecting value: line 1 column 13 (char 12)`, serde — `expected value
//!     at line 1 column 13` (мы эти тексты кладём в диагностическое поле результата);
//!   * пробелы JSON — только ' ', '\t', '\n', '\r' (в обоих), но позиции ошибок считаются по ЧАСТЯМ
//!     СИМВОЛОВ Python-строки (code points), не байтам.
//! Проверяется дифференциальным фаззингом против настоящего `json.loads`
//! (agent/rust_python_text_semantics_parity_test.py): значения и ТОЧНЫЕ тексты ошибок.
//!
//! Сознательные, задокументированные отличия (экзотика):
//!   * одиночный суррогат в строке заменяется на U+FFFD (Rust `String` не может его хранить);
//!   * целое, не помещающееся в f64 (>~1.8e308), хранится как inf — Python при `float(int)` бросает
//!     OverflowError; вызывающий код (message_intensity) поднимает то же исключение;
//!   * глубокая вложенность: Python бросает RecursionError; здесь глубже MAX_DEPTH — `Escalate`
//!     (вызывающий поднимает RecursionError), вместо переполнения стека и падения процесса;
//!   * целые >4300 цифр (ValueError в Python 3.11.x) не воспроизводятся.

use std::fmt;

pub const MAX_DEPTH: usize = 500;

#[derive(Debug, Clone)]
pub enum PyJson {
    Null,
    Bool(bool),
    /// Python int: нужны только «ноль ли» (truthiness) и значение как float(int).
    Int { zero: bool, as_f64: f64 },
    Float(f64),
    Str(String),
    List(Vec<PyJson>),
    /// dict: ключи уникальны; повтор ключа заменяет значение (как Python dict).
    Dict(Vec<(String, PyJson)>),
}

impl PyJson {
    pub fn get(&self, key: &str) -> Option<&PyJson> {
        match self {
            PyJson::Dict(items) => items.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn is_dict(&self) -> bool {
        matches!(self, PyJson::Dict(_))
    }
    pub fn is_true(&self) -> bool {
        matches!(self, PyJson::Bool(true))
    }
}

/// Не JSONDecodeError, а то, что в Python пролетело бы мимо `except json.JSONDecodeError`.
#[derive(Debug, PartialEq, Clone, Copy)]
pub enum Escalate {
    Recursion,
}

#[derive(Debug, Clone)]
pub enum LoadsError {
    /// Уже отформатированный `str(JSONDecodeError)`.
    Decode(String),
    Escalate(Escalate),
}

impl fmt::Display for LoadsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LoadsError::Decode(m) => write!(f, "{m}"),
            LoadsError::Escalate(_) => write!(f, "escalated"),
        }
    }
}

enum Fail {
    /// StopIteration(idx) -> "Expecting value"
    Stop(usize),
    Err(&'static str, usize),
    Escalate(Escalate),
}

type R<T> = Result<T, Fail>;

struct Scanner<'a> {
    s: &'a [char],
}

fn is_ws(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\r')
}

impl<'a> Scanner<'a> {
    fn len(&self) -> usize {
        self.s.len()
    }

    fn skip_ws(&self, mut idx: usize) -> usize {
        while idx < self.len() && is_ws(self.s[idx]) {
            idx += 1;
        }
        idx
    }

    fn starts_with(&self, idx: usize, lit: &str) -> bool {
        let n = lit.chars().count();
        idx + n <= self.len() && self.s[idx..idx + n].iter().copied().eq(lit.chars())
    }

    fn scan_once(&self, idx: usize, depth: usize) -> R<(PyJson, usize)> {
        if idx >= self.len() {
            return Err(Fail::Stop(idx));
        }
        match self.s[idx] {
            '"' => {
                let (st, end) = self.scanstring(idx + 1)?;
                Ok((PyJson::Str(st), end))
            }
            '{' => self.parse_object(idx + 1, depth + 1),
            '[' => self.parse_array(idx + 1, depth + 1),
            'n' if self.starts_with(idx, "null") => Ok((PyJson::Null, idx + 4)),
            't' if self.starts_with(idx, "true") => Ok((PyJson::Bool(true), idx + 4)),
            'f' if self.starts_with(idx, "false") => Ok((PyJson::Bool(false), idx + 5)),
            'N' if self.starts_with(idx, "NaN") => Ok((PyJson::Float(f64::NAN), idx + 3)),
            'I' if self.starts_with(idx, "Infinity") => Ok((PyJson::Float(f64::INFINITY), idx + 8)),
            '-' if self.starts_with(idx, "-Infinity") => Ok((PyJson::Float(f64::NEG_INFINITY), idx + 9)),
            _ => self.match_number(idx),
        }
    }

    fn is_digit(&self, i: usize) -> bool {
        i < self.len() && self.s[i].is_ascii_digit()
    }

    /// _match_number_unicode (только ASCII-цифры, как C-сканер).
    fn match_number(&self, start: usize) -> R<(PyJson, usize)> {
        let end_idx = self.len() as isize - 1;
        let mut idx = start;
        let mut is_float = false;
        if self.s[idx] == '-' {
            idx += 1;
            if idx as isize > end_idx {
                return Err(Fail::Stop(start));
            }
        }
        let c = self.s[idx];
        if ('1'..='9').contains(&c) {
            idx += 1;
            while idx as isize <= end_idx && self.is_digit(idx) {
                idx += 1;
            }
        } else if c == '0' {
            idx += 1;
        } else {
            return Err(Fail::Stop(start));
        }
        if (idx as isize) < end_idx && self.s[idx] == '.' && self.is_digit(idx + 1) {
            is_float = true;
            idx += 2;
            while idx as isize <= end_idx && self.is_digit(idx) {
                idx += 1;
            }
        }
        if (idx as isize) < end_idx && (self.s[idx] == 'e' || self.s[idx] == 'E') {
            let e_start = idx;
            idx += 1;
            if (idx as isize) < end_idx && (self.s[idx] == '-' || self.s[idx] == '+') {
                idx += 1;
            }
            while idx as isize <= end_idx && self.is_digit(idx) {
                idx += 1;
            }
            if self.s[idx - 1].is_ascii_digit() {
                is_float = true;
            } else {
                idx = e_start;
            }
        }
        let numstr: String = self.s[start..idx].iter().collect();
        if is_float {
            // float(numstr): корректно округлённый разбор; переполнение -> inf (как Python float('1e999'))
            let v: f64 = numstr.parse().unwrap_or(f64::NAN);
            Ok((PyJson::Float(v), idx))
        } else {
            let zero = numstr.trim_start_matches('-').chars().all(|c| c == '0');
            let v: f64 = if zero { 0.0 } else { numstr.parse().unwrap_or(f64::NAN) };
            Ok((PyJson::Int { zero, as_f64: v }, idx))
        }
    }

    /// scanstring_unicode (strict=True). `end` — индекс сразу после открывающей кавычки.
    fn scanstring(&self, mut end: usize) -> R<(String, usize)> {
        let begin = end - 1;
        let len = self.len();
        let mut out = String::new();
        loop {
            let mut next = end;
            let mut c = '\0';
            while next < len {
                c = self.s[next];
                if c == '"' || c == '\\' {
                    break;
                }
                if (c as u32) <= 0x1f {
                    return Err(Fail::Err("Invalid control character at", next));
                }
                next += 1;
            }
            if next >= len || !(c == '"' || c == '\\') {
                return Err(Fail::Err("Unterminated string starting at", begin));
            }
            if next != end {
                out.extend(self.s[end..next].iter());
            }
            next += 1;
            if c == '"' {
                end = next;
                break;
            }
            if next == len {
                return Err(Fail::Err("Unterminated string starting at", begin));
            }
            let esc = self.s[next];
            let decoded: char;
            if esc != 'u' {
                end = next + 1;
                decoded = match esc {
                    '"' => '"',
                    '\\' => '\\',
                    '/' => '/',
                    'b' => '\u{8}',
                    'f' => '\u{c}',
                    'n' => '\n',
                    'r' => '\r',
                    't' => '\t',
                    _ => return Err(Fail::Err("Invalid \\escape", end - 2)),
                };
            } else {
                next += 1;
                end = next + 4;
                if end >= len {
                    return Err(Fail::Err("Invalid \\uXXXX escape", next - 1));
                }
                let mut cp: u32 = 0;
                while next < end {
                    let d = self.s[next].to_digit(16).ok_or(Fail::Err("Invalid \\uXXXX escape", end - 5))?;
                    cp = (cp << 4) | d;
                    next += 1;
                }
                // Суррогатная пара
                if (0xD800..=0xDBFF).contains(&cp) && end + 6 < len {
                    let a = self.s[next];
                    next += 1;
                    if a == '\\' {
                        let b = self.s[next];
                        next += 1;
                        if b == 'u' {
                            let mut c2: u32 = 0;
                            end += 6;
                            while next < end {
                                let d = self.s[next].to_digit(16).ok_or(Fail::Err("Invalid \\uXXXX escape", end - 5))?;
                                c2 = (c2 << 4) | d;
                                next += 1;
                            }
                            if (0xDC00..=0xDFFF).contains(&c2) {
                                cp = 0x10000 + (((cp - 0xD800) << 10) | (c2 - 0xDC00));
                            } else {
                                end -= 6;
                            }
                        }
                    }
                }
                // одиночный суррогат Rust String хранить не может -> U+FFFD (см. заметки вверху файла)
                decoded = char::from_u32(cp).unwrap_or('\u{FFFD}');
            }
            out.push(decoded);
        }
        Ok((out, end))
    }

    fn parse_object(&self, mut idx: usize, depth: usize) -> R<(PyJson, usize)> {
        if depth > MAX_DEPTH {
            return Err(Fail::Escalate(Escalate::Recursion));
        }
        let len = self.len();
        let mut items: Vec<(String, PyJson)> = Vec::new();
        idx = self.skip_ws(idx);
        if !(idx < len && self.s[idx] == '}') {
            loop {
                if !(idx < len && self.s[idx] == '"') {
                    return Err(Fail::Err("Expecting property name enclosed in double quotes", idx));
                }
                let (key, after_key) = self.scanstring(idx + 1)?;
                idx = self.skip_ws(after_key);
                if !(idx < len && self.s[idx] == ':') {
                    return Err(Fail::Err("Expecting ':' delimiter", idx));
                }
                idx = self.skip_ws(idx + 1);
                let (val, after_val) = self.scan_once(idx, depth)?;
                if let Some(slot) = items.iter_mut().find(|(k, _)| *k == key) {
                    slot.1 = val;
                } else {
                    items.push((key, val));
                }
                idx = self.skip_ws(after_val);
                if idx < len && self.s[idx] == '}' {
                    break;
                }
                if !(idx < len && self.s[idx] == ',') {
                    return Err(Fail::Err("Expecting ',' delimiter", idx));
                }
                idx = self.skip_ws(idx + 1);
            }
        }
        Ok((PyJson::Dict(items), idx + 1))
    }

    fn parse_array(&self, mut idx: usize, depth: usize) -> R<(PyJson, usize)> {
        if depth > MAX_DEPTH {
            return Err(Fail::Escalate(Escalate::Recursion));
        }
        let len = self.len();
        let mut items = Vec::new();
        idx = self.skip_ws(idx);
        if !(idx < len && self.s[idx] == ']') {
            loop {
                let (val, after_val) = self.scan_once(idx, depth)?;
                items.push(val);
                idx = self.skip_ws(after_val);
                if idx < len && self.s[idx] == ']' {
                    break;
                }
                if !(idx < len && self.s[idx] == ',') {
                    return Err(Fail::Err("Expecting ',' delimiter", idx));
                }
                idx = self.skip_ws(idx + 1);
            }
        }
        Ok((PyJson::List(items), idx + 1))
    }
}

fn format_error(chars: &[char], msg: &str, pos: usize) -> String {
    let upto = &chars[..pos.min(chars.len())];
    let lineno = upto.iter().filter(|&&c| c == '\n').count() + 1;
    let colno = match upto.iter().rposition(|&c| c == '\n') {
        Some(nl) => pos - nl,
        None => pos + 1,
    };
    format!("{msg}: line {lineno} column {colno} (char {pos})")
}

/// json.loads(s)
pub fn loads(s: &str) -> Result<PyJson, LoadsError> {
    let chars: Vec<char> = s.chars().collect();
    if chars.first() == Some(&'\u{feff}') {
        return Err(LoadsError::Decode(format_error(&chars, "Unexpected UTF-8 BOM (decode using utf-8-sig)", 0)));
    }
    let sc = Scanner { s: &chars };
    let start = sc.skip_ws(0);
    let fail = |f: Fail| match f {
        Fail::Stop(i) => LoadsError::Decode(format_error(&chars, "Expecting value", i)),
        Fail::Err(m, p) => LoadsError::Decode(format_error(&chars, m, p)),
        Fail::Escalate(e) => LoadsError::Escalate(e),
    };
    let (value, end) = sc.scan_once(start, 0).map_err(fail)?;
    let end = sc.skip_ws(end);
    if end != chars.len() {
        return Err(LoadsError::Decode(format_error(&chars, "Extra data", end)));
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn err_text(s: &str) -> String {
        match loads(s) {
            Err(LoadsError::Decode(m)) => m,
            other => panic!("ожидалась ошибка, получено {other:?}"),
        }
    }

    #[test]
    fn python_literals_and_numbers() {
        assert!(matches!(loads("NaN").unwrap(), PyJson::Float(f) if f.is_nan()));
        assert!(matches!(loads("-Infinity").unwrap(), PyJson::Float(f) if f == f64::NEG_INFINITY));
        assert!(matches!(loads("1e999").unwrap(), PyJson::Float(f) if f == f64::INFINITY));
        assert!(matches!(loads("-0").unwrap(), PyJson::Int { zero: true, .. }));
        assert!(matches!(loads("[1, 2.5, \"x\", null, true]").unwrap(), PyJson::List(v) if v.len() == 5));
    }

    #[test]
    fn python_error_texts() {
        assert_eq!(err_text("{'a': 1}"), "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)");
        assert_eq!(err_text("{\"a\": 1,}"), "Expecting property name enclosed in double quotes: line 1 column 9 (char 8)");
        assert_eq!(err_text("[1,]"), "Expecting value: line 1 column 4 (char 3)");
        assert_eq!(err_text("{\"a\" 1}"), "Expecting ':' delimiter: line 1 column 6 (char 5)");
        assert_eq!(err_text("{\"a\": 1 \"b\": 2}"), "Expecting ',' delimiter: line 1 column 9 (char 8)");
        assert_eq!(err_text("{\"a\": 1} x"), "Extra data: line 1 column 10 (char 9)");
        assert_eq!(err_text("\"abc"), "Unterminated string starting at: line 1 column 1 (char 0)");
        assert_eq!(err_text("{\n\"a\": tru}"), "Expecting value: line 2 column 6 (char 7)");
    }

    #[test]
    fn duplicate_keys_last_wins() {
        let v = loads("{\"a\": 1, \"a\": 2}").unwrap();
        assert!(matches!(v.get("a"), Some(PyJson::Int { as_f64, .. }) if *as_f64 == 2.0));
    }

    #[test]
    fn deep_nesting_escalates_not_crashes() {
        let deep = "[".repeat(100_000);
        assert!(matches!(loads(&deep), Err(LoadsError::Escalate(Escalate::Recursion))));
    }

    #[test]
    fn surrogate_pair_and_lone() {
        assert!(matches!(loads("\"\\ud83d\\ude00\"").unwrap(), PyJson::Str(s) if s == "😀"));
        assert!(matches!(loads("\"\\ud800\"").unwrap(), PyJson::Str(s) if s == "\u{FFFD}"));
    }
}
