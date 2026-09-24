//! Транслятор паттернов Python `re` (для str) в крейт `regex`, ТОЧНЫЙ там, где эти движки различаются
//! не по алгоритму, а по определениям символов. Сравнение с Python — массовый дифференциальный
//! фаззинг (agent/rust_unicode_fuzz_parity_test.py).
//!
//! Что переписывается (всё остальное копируется как есть):
//!  * `\s`  -> `[\s\x1c-\x1f]`  (Python-пробелы шире на U+001C..1F);
//!  * `\w`  -> класс из таблицы Python-`\w` (`isalnum()` или `_`, py_word_table.rs);
//!  * `\b`  -> граница слова по Python-`\w`. Крейт `regex` не умеет lookaround, поэтому граница
//!    выражается «съедающим» соседним символом: ведущая `\b` (после начала/`(`/`|`) — `(?:^|[^W])`,
//!    замыкающая — `(?:$|[^W])`. Это ТОЧНО для проверок «есть ли совпадение» (is_match/search) при условии,
//!    что атом рядом с `\b` состоит из словесных символов (у нас всегда так; проверяется фаззингом);
//!    для позиционных вызовов (find_iter, captures по тексту рядом с границей) НЕ использовать;
//!  * `$`   -> `(?:\n?$)`  (Python `$` без MULTILINE совпадает и перед завершающим `\n`);
//!  * `(?i)` (IGNORECASE) -> убирается; каждый литерал/символ класса раскрывается в класс всех символов,
//!    которые Python считает равными при IGNORECASE (py_icase_table.rs: `_sre.unicode_tolower` +
//!    `re._casefix._EXTRA_CASES`) — например 'i' ~ 'İ' ~ 'ı', 'k' ~ K(Kelvin), 's' ~ 'ſ';
//!  * `(?x)` (verbose) — пробелы/комментарии вне класса сохраняются (крейт понимает их так же), флаг `x` оставлен.
//! Не поддерживается (паника при компиляции, чтобы не пропустить молча): `\S \W \B \D`, lookaround,
//! обратные ссылки, `(?m)`.

use crate::py_icase_table::PY_ICASE_GROUPS;
use crate::py_word_table::PY_WORD_RANGES;
use once_cell::sync::Lazy;
use regex::Regex;
use std::collections::HashMap;
use std::fmt::Write;

static ICASE_MAP: Lazy<HashMap<u32, &'static [u32]>> = Lazy::new(|| {
    let mut m = HashMap::new();
    for g in PY_ICASE_GROUPS.iter() {
        for &c in g.iter() {
            m.insert(c, *g);
        }
    }
    m
});

/// Тело класса «словесные символы Python» (без скобок): `\x{30}-\x{39}\x{41}-\x{5A}...`
static WORD_BODY: Lazy<String> = Lazy::new(|| {
    let mut s = String::new();
    for &(lo, hi) in PY_WORD_RANGES.iter() {
        if lo == hi {
            let _ = write!(s, "\\x{{{lo:X}}}");
        } else {
            let _ = write!(s, "\\x{{{lo:X}}}-\\x{{{hi:X}}}");
        }
    }
    s
});

#[derive(PartialEq, Clone, Copy)]
enum Prev {
    Start,
    Open,
    Alt,
    Other,
}

fn hex(c: char) -> String {
    format!("\\x{{{:X}}}", c as u32)
}

/// Символ (литерал) -> его представление: при IGNORECASE и наличии группы — класс `[...]`.
fn literal(c: char, icase: bool, out: &mut String) {
    if icase {
        if let Some(g) = ICASE_MAP.get(&(c as u32)) {
            out.push('[');
            for &m in g.iter() {
                let _ = write!(out, "\\x{{{m:X}}}");
            }
            out.push(']');
            return;
        }
    }
    if c.is_ascii_alphanumeric() || (c as u32) > 0x7f || c == '_' {
        out.push(c);
    } else {
        // ASCII-пунктуация/пробел/управляющие — в hex, чтобы ни один не стал метасимволом
        out.push_str(&hex(c));
    }
}

fn translate_class(chars: &[char], start: usize, icase: bool, out: &mut String) -> usize {
    // chars[start] == '['
    let mut i = start + 1;
    out.push('[');
    if i < chars.len() && chars[i] == '^' {
        out.push('^');
        i += 1;
    }
    if i < chars.len() && chars[i] == ']' {
        panic!("ведущий ']' в классе не поддерживается py_regex");
    }
    let mut extra: Vec<u32> = Vec::new();
    let add_extra = |c: u32, extra: &mut Vec<u32>| {
        if icase {
            if let Some(g) = ICASE_MAP.get(&c) {
                for &m in g.iter() {
                    if !extra.contains(&m) {
                        extra.push(m);
                    }
                }
            }
        }
    };
    while i < chars.len() && chars[i] != ']' {
        let c = chars[i];
        // одиночный символ (возможно экранированный)
        let single: Option<(char, usize)> = if c == '\\' {
            let n = chars[i + 1];
            match n {
                's' => {
                    out.push_str(r"\s\x1c-\x1f");
                    i += 2;
                    continue;
                }
                'w' => {
                    out.push_str(&WORD_BODY);
                    i += 2;
                    continue;
                }
                'd' => {
                    out.push_str(r"\d");
                    i += 2;
                    continue;
                }
                'S' | 'W' | 'D' | 'b' | 'B' => panic!("\\{n} в классе не поддерживается py_regex"),
                'n' => Some(('\n', 2)),
                't' => Some(('\t', 2)),
                'r' => Some(('\r', 2)),
                _ => Some((n, 2)),
            }
        } else {
            Some((c, 1))
        };
        let (lo, adv) = single.unwrap();
        i += adv;
        // диапазон lo-hi ?
        if i + 1 < chars.len() && chars[i] == '-' && chars[i + 1] != ']' {
            let hi_c = if chars[i + 1] == '\\' {
                i += 3;
                chars[i - 1]
            } else {
                i += 2;
                chars[i - 1]
            };
            let (l, h) = (lo as u32, hi_c as u32);
            assert!(l <= h, "обратный диапазон в классе");
            let _ = write!(out, "\\x{{{l:X}}}-\\x{{{h:X}}}");
            if icase {
                assert!(h - l <= 4096, "слишком широкий диапазон при IGNORECASE");
                for cp in l..=h {
                    add_extra(cp, &mut extra);
                }
            }
        } else {
            let _ = write!(out, "\\x{{{:X}}}", lo as u32);
            add_extra(lo as u32, &mut extra);
        }
    }
    for m in extra {
        let _ = write!(out, "\\x{{{m:X}}}");
    }
    out.push(']');
    i + 1
}

/// Переписывает паттерн Python `re` (см. заметки вверху файла).
pub fn translate(pattern: &str) -> String {
    let chars: Vec<char> = pattern.chars().collect();
    let mut out = String::with_capacity(pattern.len() * 2);
    let mut i = 0;
    let mut icase = false;
    let mut verbose = false;
    let mut prev = Prev::Start;

    // ведущие флаги вида (?i) (?s) (?x) (?is)
    if chars.len() >= 4 && chars[0] == '(' && chars[1] == '?' {
        let mut j = 2;
        while j < chars.len() && "imsx".contains(chars[j]) {
            j += 1;
        }
        if j > 2 && j < chars.len() && chars[j] == ')' {
            for &f in &chars[2..j] {
                match f {
                    'i' => icase = true,
                    'x' => verbose = true,
                    's' => {}
                    _ => panic!("флаг {f} не поддерживается py_regex"),
                }
            }
            let keep: String = chars[2..j].iter().filter(|&&f| f == 's' || f == 'x').collect();
            if !keep.is_empty() {
                let _ = write!(out, "(?{keep})");
            }
            i = j + 1;
        }
    }

    while i < chars.len() {
        let c = chars[i];
        if verbose {
            if c.is_whitespace() {
                out.push(c);
                i += 1;
                continue;
            }
            if c == '#' {
                while i < chars.len() && chars[i] != '\n' {
                    out.push(chars[i]);
                    i += 1;
                }
                continue;
            }
        }
        match c {
            '\\' => {
                let n = chars[i + 1];
                i += 2;
                match n {
                    's' => out.push_str(r"[\s\x1c-\x1f]"),
                    'w' => {
                        out.push('[');
                        out.push_str(&WORD_BODY);
                        out.push(']');
                    }
                    'b' => {
                        let leading = matches!(prev, Prev::Start | Prev::Open | Prev::Alt);
                        out.push_str(if leading { "(?:^|[^" } else { "(?:$|[^" });
                        out.push_str(&WORD_BODY);
                        out.push_str("])");
                    }
                    'd' => out.push_str(r"\d"),
                    'S' | 'W' | 'B' | 'D' => panic!("\\{n} не поддерживается py_regex: {pattern}"),
                    'x' => {
                        // \xHH -> символ
                        let h: String = chars[i..i + 2].iter().collect();
                        let cp = u32::from_str_radix(&h, 16).expect("\\xHH");
                        i += 2;
                        literal(char::from_u32(cp).unwrap(), icase, &mut out);
                    }
                    'n' => out.push_str("\\n"),
                    't' => out.push_str("\\t"),
                    'r' => out.push_str("\\r"),
                    _ => literal(n, icase, &mut out), // экранированная пунктуация/буква -> литерал
                }
                prev = Prev::Other;
            }
            '[' => {
                i = translate_class(&chars, i, icase, &mut out);
                prev = Prev::Other;
            }
            '(' => {
                if i + 1 < chars.len() && chars[i + 1] == '?' {
                    if i + 2 < chars.len() && chars[i + 2] == ':' {
                        out.push_str("(?:");
                        i += 3;
                    } else if i + 2 < chars.len() && chars[i + 2] == 'P' {
                        while i < chars.len() && chars[i] != '>' {
                            out.push(chars[i]);
                            i += 1;
                        }
                        out.push('>');
                        i += 1;
                    } else {
                        panic!("группа (?{}…) не поддерживается py_regex: {pattern}", chars.get(i + 2).copied().unwrap_or('?'));
                    }
                } else {
                    out.push('(');
                    i += 1;
                }
                prev = Prev::Open;
            }
            '|' => {
                out.push('|');
                i += 1;
                prev = Prev::Alt;
            }
            ')' | '^' | '.' | '*' | '+' | '?' => {
                out.push(c);
                i += 1;
                prev = Prev::Other;
            }
            '$' => {
                out.push_str(r"(?:\n?$)");
                i += 1;
                prev = Prev::Other;
            }
            '{' => {
                // квантификатор {m}, {m,}, {m,n} — как есть; иначе литерал
                let mut j = i + 1;
                while j < chars.len() && (chars[j].is_ascii_digit() || chars[j] == ',') {
                    j += 1;
                }
                if j < chars.len() && chars[j] == '}' && j > i + 1 {
                    out.extend(chars[i..=j].iter());
                    i = j + 1;
                } else {
                    out.push_str("\\{");
                    i += 1;
                }
                prev = Prev::Other;
            }
            '}' => {
                out.push_str("\\}");
                i += 1;
                prev = Prev::Other;
            }
            _ => {
                literal(c, icase, &mut out);
                i += 1;
                prev = Prev::Other;
            }
        }
    }
    out
}

/// Regex с точной питоновской семантикой для is_match/search (см. ограничения в заметках вверху).
pub fn compile(pattern: &str) -> Regex {
    let t = translate(pattern);
    regex::RegexBuilder::new(&t)
        .size_limit(64 * 1024 * 1024)
        .build()
        .unwrap_or_else(|e| panic!("паттерн должен быть валиден: {pattern}\n-> {t}\n{e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn word_boundary_uses_python_word() {
        let r = compile(r"\bты\b");
        assert!(r.is_match("ты\u{301} тут")); // U+0301 не слово в Python -> граница есть
        assert!(!r.is_match("путы"));
        assert!(!r.is_match("ты2"));
        assert!(r.is_match("эй,ты!"));
        assert!(r.is_match("ты"));
    }

    #[test]
    fn ignorecase_is_pythons() {
        let r = compile(r"(?i)yandi");
        assert!(r.is_match("YANDı")); // ı ~ i при re.I
        assert!(r.is_match("YANDİ")); // İ ~ i
        assert!(compile(r"(?i)k").is_match("\u{212a}")); // Kelvin ~ k
        assert!(compile(r"(?i)s").is_match("ſ"));
        assert!(!compile(r"(?i)ß").is_match("SS")); // Python: НЕ равны
        assert!(compile(r"(?i)[а-я]+").is_match("ПРИВЕТ"));
    }

    #[test]
    fn dollar_before_trailing_newline() {
        let r = compile(r"x$");
        assert!(r.is_match("x\n"));
        assert!(r.is_match("x"));
        assert!(!r.is_match("x\n\n"));
        assert!(!r.is_match("x\ny"));
    }

    #[test]
    fn space_class_and_verbose() {
        assert!(compile(r"a\sb").is_match("a\u{1c}b"));
        assert!(compile(r"[_\s.-]x").is_match("\u{1f}x"));
        assert!(compile("(?x) a \\s+ b # comment").is_match("a  b"));
    }

    #[test]
    fn quantifier_and_alternation_boundaries() {
        let r = compile(r"(?i)\b(вызывает|вызвал[а-я]*|causes?)\b");
        assert!(r.is_match("Это вызывает проблемы"));
        assert!(r.is_match("CAUSES x"));
        assert!(!r.is_match("причиной вызывается"));
        assert!(!r.is_match("вызывает\u{0663}")); // цифра — слово
    }
}
