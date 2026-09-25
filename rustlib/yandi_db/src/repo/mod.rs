//! Репозитории: функции чтения/записи над схемой — перенос `agent/db/sql/repositories.py`. Каждая функция принимает открытое соединение
//! (транзакциями владеет вызывающий), возвращает строки как JSON-объекты (`Row`) — так же, как Python отдаёт словари.
//!
//! Известные отличия от MySQL (документируются в README):
//! * DATETIME хранится ISO-текстом `YYYY-MM-DD HH:MM:SS`; дробные секунды округляются к целой секунде, как это делает MySQL DATETIME(0).
//! * FLOAT в MySQL — одинарная точность, клиенту отдаётся с 6 значащими цифрами: при записи значение приводится к тому, что прочитал бы Python (0.7 остаётся 0.7).
//! * Текстовые сравнения точные (BINARY); MySQL по умолчанию сравнивал без учёта регистра и диакритики.
use rusqlite::types::{Value as Sql, ValueRef};
use rusqlite::{params_from_iter, Connection};
use serde_json::{Map, Value};

pub mod epistemic;
pub mod beliefs;
pub mod field_protection;
pub mod relationship;
pub mod evidence;
pub mod views;

pub type Row = Map<String, Value>;
pub type R<T> = Result<T, String>;

pub(crate) fn err<T: std::fmt::Display>(x: T) -> String {
    x.to_string()
}

// ---------------------------------------------------------------- SQL-помощники

pub(crate) fn rows(c: &Connection, sql: &str, args: Vec<Sql>) -> R<Vec<Row>> {
    let mut st = c.prepare(sql).map_err(err)?;
    let names: Vec<String> = st.column_names().iter().map(|s| s.to_string()).collect();
    let mut q = st.query(params_from_iter(args)).map_err(err)?;
    let mut out = Vec::new();
    while let Some(r) = q.next().map_err(err)? {
        let mut m = Row::new();
        for (i, n) in names.iter().enumerate() {
            let v = match r.get_ref(i).map_err(err)? {
                ValueRef::Null => Value::Null,
                ValueRef::Integer(x) => Value::from(x),
                ValueRef::Real(x) => serde_json::Number::from_f64(x).map(Value::Number).unwrap_or(Value::Null),
                ValueRef::Text(t) => Value::String(String::from_utf8_lossy(t).into_owned()),
                ValueRef::Blob(b) => Value::String(b.iter().map(|x| format!("{x:02x}")).collect()),
            };
            m.insert(n.clone(), v);
        }
        out.push(m);
    }
    Ok(out)
}

pub(crate) fn row(c: &Connection, sql: &str, args: Vec<Sql>) -> R<Option<Row>> {
    Ok(rows(c, sql, args)?.into_iter().next())
}

/// Выполнить изменяющий запрос: `(число затронутых строк, последний rowid)`.
pub(crate) fn exec(c: &Connection, sql: &str, args: Vec<Sql>) -> R<(usize, i64)> {
    let n = c.prepare(sql).map_err(err)?.execute(params_from_iter(args)).map_err(err)?;
    Ok((n, c.last_insert_rowid()))
}

// ---------------------------------------------------------------- значения

pub(crate) fn sv(x: &str) -> Sql {
    Sql::Text(x.to_string())
}
pub(crate) fn osv(x: Option<&str>) -> Sql {
    x.map(sv).unwrap_or(Sql::Null)
}
pub(crate) fn iv(x: i64) -> Sql {
    Sql::Integer(x)
}
pub(crate) fn oiv(x: Option<i64>) -> Sql {
    x.map(Sql::Integer).unwrap_or(Sql::Null)
}
/// FLOAT MySQL = f32, а по текстовому протоколу клиенту отдаётся `%.6g` (6 значащих цифр): Python видел именно это число (0.7, а не 0.699999988…).
/// Повторяем: значение приводится к f32 и округляется до 6 значащих цифр — то, что прочитал бы Python.
pub(crate) fn fv(x: Option<f64>) -> Sql {
    x.map(|v| {
        let f = (v as f32) as f64;
        if !f.is_finite() {
            return Sql::Real(f);
        }
        Sql::Real(format!("{f:.5e}").parse::<f64>().unwrap_or(f))
    })
    .unwrap_or(Sql::Null)
}
pub(crate) fn bv(x: bool) -> Sql {
    Sql::Integer(x as i64)
}
/// JSON-столбец: `json.dumps(...)` (или NULL).
pub(crate) fn jv(x: Option<&Value>) -> Sql {
    match x {
        None | Some(Value::Null) => Sql::Null,
        Some(v) => Sql::Text(py_json_dumps(v)),
    }
}

/// Текст JSON-столбца ТАК, как его отдаёт MySQL (нормализованный тип JSON): ключи объектов отсортированы (сначала короче, затем по байтам), разделители `, ` и `: `,
/// не-ASCII символы как есть (не `\\uXXXX`). Так читатели «сырого» текста столбца видят то же, что и раньше.
pub(crate) fn py_json_dumps(v: &Value) -> String {
    fn go(v: &Value, out: &mut String) {
        match v {
            Value::Null => out.push_str("null"),
            Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Value::Number(n) => out.push_str(&n.to_string()),
            Value::String(s) => {
                out.push('"');
                for ch in s.chars() {
                    match ch {
                        '"' => out.push_str("\\\""),
                        '\\' => out.push_str("\\\\"),
                        '\n' => out.push_str("\\n"),
                        '\r' => out.push_str("\\r"),
                        '\t' => out.push_str("\\t"),
                        '\u{8}' => out.push_str("\\b"),
                        '\u{c}' => out.push_str("\\f"),
                        c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                        c => out.push(c),
                    }
                }
                out.push('"');
            }
            Value::Array(a) => {
                out.push('[');
                for (i, x) in a.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    go(x, out);
                }
                out.push(']');
            }
            Value::Object(m) => {
                let mut keys: Vec<&String> = m.keys().collect();
                keys.sort_by(|a, b| a.len().cmp(&b.len()).then_with(|| a.as_bytes().cmp(b.as_bytes())));
                out.push('{');
                for (i, k) in keys.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    go(&Value::String((*k).clone()), out);
                    out.push_str(": ");
                    go(&m[*k], out);
                }
                out.push('}');
            }
        }
    }
    let mut s = String::new();
    go(v, &mut s);
    s
}

/// Разобрать JSON-столбцы строки (как `_decode_*_json` в Python: только если значение — строка).
pub(crate) fn decode_json_cols(r: &mut Row, cols: &[&str]) -> R<()> {
    for c in cols {
        if let Some(Value::String(s)) = r.get(*c) {
            let v: Value = serde_json::from_str(s).map_err(err)?;
            r.insert((*c).to_string(), v);
        }
    }
    Ok(())
}

// ---------------------------------------------------------------- время

/// (год, месяц, день) по числу дней от 1970-01-01 (алгоритм Говарда Хиннанта).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

pub(crate) fn fmt_epoch_secs(secs: i64) -> String {
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    let (y, m, d) = civil_from_days(days);
    format!("{y:04}-{m:02}-{d:02} {:02}:{:02}:{:02}", rem / 3600, (rem % 3600) / 60, rem % 60)
}

/// Unix-время (с дробной частью) → текст DATETIME(0) как у MySQL: сначала микросекунды (round-half-even, как `utcfromtimestamp`), затем округление к секунде.
pub(crate) fn epoch_to_datetime_text(t: f64) -> String {
    let secs = t.floor();
    let frac = t - secs;
    let mut micro = (frac * 1_000_000.0).round_ties_even() as i64;
    let mut s = secs as i64;
    if micro >= 1_000_000 {
        micro -= 1_000_000;
        s += 1;
    }
    fmt_epoch_secs(if micro >= 500_000 { s + 1 } else { s })
}

pub(crate) fn now_text() -> String {
    let d = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    epoch_to_datetime_text(d.as_secs_f64())
}

/// `_coerce_datetime(x) or _now()`: None/пусто → сейчас; число → Unix-время; строка — как есть (разбирает и округляет MySQL; здесь нормализуем).
pub(crate) fn dt_or_now(v: Option<&Value>) -> R<String> {
    Ok(match v {
        None | Some(Value::Null) => now_text(),
        Some(Value::Number(n)) => epoch_to_datetime_text(n.as_f64().unwrap_or(0.0)),
        Some(Value::String(s)) => normalize_dt_string(s),
        Some(other) => return Err(format!("недопустимое значение времени: {other}")),
    })
}

/// `YYYY-MM-DD[ T]HH:MM:SS[.дробь]` → `YYYY-MM-DD HH:MM:SS` с округлением дроби (MySQL DATETIME(0)); прочее остаётся как есть.
pub(crate) fn normalize_dt_string(s: &str) -> String {
    let b = s.as_bytes();
    let ok = b.len() >= 19 && b[4] == b'-' && b[7] == b'-' && (b[10] == b' ' || b[10] == b'T') && b[13] == b':' && b[16] == b':';
    if !ok {
        return s.to_string();
    }
    let num = |a: usize, z: usize| s[a..z].parse::<i64>().ok();
    let (Some(y), Some(mo), Some(d), Some(h), Some(mi), Some(se)) = (num(0, 4), num(5, 7), num(8, 10), num(11, 13), num(14, 16), num(17, 19)) else {
        return s.to_string();
    };
    let round_up = b.len() > 20 && b[19] == b'.' && b[20] >= b'5';
    if !round_up {
        return format!("{y:04}-{mo:02}-{d:02} {h:02}:{mi:02}:{se:02}");
    }
    // +1 секунда через эпоху
    let days = days_from_civil(y, mo as u32, d as u32);
    fmt_epoch_secs(days * 86_400 + h * 3600 + mi * 60 + se + 1)
}

fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y.rem_euclid(400);
    let mp = (if m > 2 { m - 3 } else { m + 9 }) as i64;
    let doy = (153 * mp + 2) / 5 + d as i64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

pub(crate) fn sha256_hex(text: &str) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(text.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

// ---------------------------------------------------------------- аргументы вызова (kwargs)

pub struct A<'a>(pub &'a Map<String, Value>);

impl<'a> A<'a> {
    pub fn get(&self, k: &str) -> Option<&'a Value> {
        self.0.get(k)
    }
    pub fn str(&self, k: &str) -> R<String> {
        match self.0.get(k) {
            Some(Value::String(s)) => Ok(s.clone()),
            other => Err(format!("аргумент {k}: ожидалась строка, получено {other:?}")),
        }
    }
    pub fn opt_str(&self, k: &str) -> R<Option<String>> {
        match self.0.get(k) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(s)) => Ok(Some(s.clone())),
            other => Err(format!("аргумент {k}: ожидалась строка, получено {other:?}")),
        }
    }
    pub fn i64(&self, k: &str) -> R<i64> {
        self.opt_i64(k)?.ok_or_else(|| format!("аргумент {k} обязателен"))
    }
    pub fn opt_i64(&self, k: &str) -> R<Option<i64>> {
        match self.0.get(k) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::Number(n)) if n.is_i64() || n.is_u64() => Ok(n.as_i64()),
            Some(Value::Bool(b)) => Ok(Some(*b as i64)),
            other => Err(format!("аргумент {k}: ожидалось целое, получено {other:?}")),
        }
    }
    pub fn opt_f64(&self, k: &str) -> R<Option<f64>> {
        match self.0.get(k) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::Number(n)) => Ok(n.as_f64()),
            Some(Value::Bool(b)) => Ok(Some(*b as i64 as f64)),
            other => Err(format!("аргумент {k}: ожидалось число, получено {other:?}")),
        }
    }
    pub fn bool_or(&self, k: &str, default: bool) -> bool {
        match self.0.get(k) {
            None | Some(Value::Null) => default,
            Some(Value::Bool(b)) => *b,
            Some(Value::Number(n)) => n.as_f64().map(|x| x != 0.0).unwrap_or(default),
            Some(Value::String(s)) => !s.is_empty(),
            Some(_) => true,
        }
    }
}

// ---------------------------------------------------------------- диспетчер

/// Вызов репозитория по имени (для моста дифференциальных тестов и, позднее, для вызывающих без Python). Результат — JSON.
pub fn call(c: &Connection, name: &str, args: &Value) -> R<Value> {
    let empty = Map::new();
    let a = A(args.as_object().unwrap_or(&empty));
    if let Some(v) = epistemic::dispatch(c, name, &a)? {
        return Ok(v);
    }
    if let Some(v) = evidence::dispatch(c, name, &a)? {
        return Ok(v);
    }
    if let Some(v) = beliefs::dispatch(c, name, &a)? {
        return Ok(v);
    }
    if let Some(v) = views::dispatch(c, name, &a)? {
        return Ok(v);
    }
    if let Some(v) = relationship::dispatch(c, name, &a)? {
        return Ok(v);
    }
    Err(format!("неизвестная функция {name}"))
}

/// Произвольный запрос для инструментов сверки (только чтение).
pub fn rows_pub(c: &Connection, sql: &str) -> R<Vec<Row>> {
    rows(c, sql, vec![])
}

/// Первые `n` символов (Python-срез `s[:n]` работает по символам, не по байтам).
pub(crate) fn cut_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// `json.dumps(x)` Python (ensure_ascii=True, разделители `, ` и `: `, порядок ключей как вставлен) — для TEXT-столбцов, где Python хранит JSON строкой.
pub(crate) fn pydumps(v: &Value) -> String {
    fn go(v: &Value, out: &mut String) {
        match v {
            Value::Null => out.push_str("null"),
            Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Value::Number(n) => out.push_str(&n.to_string()),
            Value::String(s) => {
                out.push('"');
                for ch in s.chars() {
                    match ch {
                        '"' => out.push_str("\\\""),
                        '\\' => out.push_str("\\\\"),
                        '\n' => out.push_str("\\n"),
                        '\r' => out.push_str("\\r"),
                        '\t' => out.push_str("\\t"),
                        '\u{8}' => out.push_str("\\b"),
                        '\u{c}' => out.push_str("\\f"),
                        c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                            let mut b = [0u16; 2];
                            for u in c.encode_utf16(&mut b) {
                                out.push_str(&format!("\\u{:04x}", u));
                            }
                        }
                        c => out.push(c),
                    }
                }
                out.push('"');
            }
            Value::Array(a) => {
                out.push('[');
                for (i, x) in a.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    go(x, out);
                }
                out.push(']');
            }
            Value::Object(m) => {
                out.push('{');
                for (i, (k, x)) in m.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    go(&Value::String(k.clone()), out);
                    out.push_str(": ");
                    go(x, out);
                }
                out.push('}');
            }
        }
    }
    let mut s = String::new();
    go(v, &mut s);
    s
}

/// `_coerce_datetime(x)` без подстановки «сейчас»: None остаётся None.
pub(crate) fn dt_opt(v: Option<&Value>) -> R<Option<String>> {
    Ok(match v {
        None | Some(Value::Null) => None,
        other => Some(dt_or_now(other)?),
    })
}
