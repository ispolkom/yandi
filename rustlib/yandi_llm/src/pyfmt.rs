//! Python-совместимое строковое представление JSON-значений: `str(x)` / `repr(x)` для None/bool/int/float/str/list/dict.
//! Нужно там, где оригинал вставляет произвольные значения схемы в f-строки (`f"от {spec['minimum']} до {spec['maximum']}"`).

use serde_json::Value;
use yandi_rs::py_text::py_repr_str;

/// `repr(float)` как в CPython (кратчайшее круговое представление, правила выбора формы `float_repr_style = 'short'`).
pub fn py_float_repr(f: f64) -> String {
    if f.is_nan() {
        return "nan".to_string();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf".to_string() } else { "-inf".to_string() };
    }
    if f == 0.0 {
        return if f.is_sign_negative() { "-0.0".to_string() } else { "0.0".to_string() };
    }
    // `{:e}` печатает кратчайшие цифры: "d.ddde-7" / "de5"
    let sci = format!("{:e}", f.abs());
    let (mant, exp) = sci.split_once('e').expect("формат {:e}");
    let exp10: i32 = exp.parse().expect("порядок");
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let nd = digits.len() as i32;
    let decpt = exp10 + 1; // значение = 0.DIGITS × 10^decpt
    let sign = if f < 0.0 { "-" } else { "" };
    if decpt <= -4 || decpt > 16 {
        let m = if nd == 1 { digits.clone() } else { format!("{}.{}", &digits[..1], &digits[1..]) };
        let e = decpt - 1;
        let es = if e < 0 { format!("-{:02}", -e) } else { format!("+{:02}", e) };
        format!("{sign}{m}e{es}")
    } else if decpt <= 0 {
        format!("{sign}0.{}{}", "0".repeat((-decpt) as usize), digits)
    } else if decpt >= nd {
        format!("{sign}{}{}.0", digits, "0".repeat((decpt - nd) as usize))
    } else {
        format!("{sign}{}.{}", &digits[..decpt as usize], &digits[decpt as usize..])
    }
}

pub fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.to_string()
            } else if let Some(u) = n.as_u64() {
                u.to_string()
            } else {
                py_float_repr(n.as_f64().unwrap_or(f64::NAN))
            }
        }
        Value::String(s) => py_repr_str(s),
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter().map(|(k, v)| format!("{}: {}", py_repr_str(k), py_repr(v))).collect::<Vec<_>>().join(", ")
        ),
    }
}

/// `str(x)`: строка — как есть, остальное — repr.
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => py_repr(other),
    }
}

/// `bool(x)` для JSON-значения.
pub fn py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i != 0
            } else if let Some(u) = n.as_u64() {
                u != 0
            } else {
                n.as_f64().map(|f| f != 0.0).unwrap_or(true)
            }
        }
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_repr_matches_cpython() {
        for (f, want) in [
            (1.0, "1.0"), (0.5, "0.5"), (100.0, "100.0"), (1e15, "1000000000000000.0"), (1e16, "1e+16"), (1.5e16, "1.5e+16"),
            (0.0001, "0.0001"), (0.00001, "1e-05"), (1.2345e-7, "1.2345e-07"), (123456.789, "123456.789"), (-2.5, "-2.5"), (0.1, "0.1"),
            (1e22, "1e+22"), (1e-300, "1e-300"), (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"), (0.3, "0.3"), (2.0, "2.0"),
        ] {
            assert_eq!(py_float_repr(f), want, "{f:e}");
        }
    }

    #[test]
    fn repr_of_values() {
        let v: Value = serde_json::from_str(r#"{"a":[1,2.5,null,true,"x'y"],"b":{}}"#).unwrap();
        assert_eq!(py_repr(&v), r#"{'a': [1, 2.5, None, True, "x'y"], 'b': {}}"#);
    }
}
