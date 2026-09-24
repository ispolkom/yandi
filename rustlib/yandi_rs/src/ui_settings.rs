//! Перенос `validate` из pet/ui_settings.py (срез 34, 2026-09-24): проверка документа настроек вкладки «YANDI», пришедшего от формы
//! (какая модель отвечает, какие советуют). Это шлюз ввода веб-интерфейса: неизвестное поле и ключ API — ошибка, а не молчаливая правка.
//! Файл, права и атомарная запись (`load`/`save`) остаются в Python.
//!
//! Вход — объект из json.loads: разбираются только «родные» типы (None/bool/int/float/str/list/dict); любой посторонний тип → None
//! (вызывающий выполняет исходный Python-код). Ошибка возвращается как `(False, текст)` — исключение `SettingsError` бросает Python.
//! Порядок проверок и тексты сообщений — как в оригинале. ЕДИНСТВЕННОЕ отличие: если не заполнено НЕСКОЛЬКО выбранных блоков, оригинал
//! перебирает `set` строк (порядок зависит от хэш-рандомизации и не определён) и называет случайный; здесь порядок детерминирован
//! (сначала голос, затем советники local→remote→api). Тест принимает любое из допустимых сообщений.

#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::PyDict;

use crate::pet_extraction::{classify, V};
use crate::py_text::{is_py_space, py_strip};

const KINDS: [&str; 3] = ["local", "remote", "api"];
const SERVICES: [&str; 3] = ["openai", "anthropic", "other"];
const MAX_PATH: usize = 1024;

enum Out {
    Fallback,
    Err(String),
}

impl From<String> for Out {
    fn from(s: String) -> Out {
        Out::Err(s)
    }
}

#[cfg(feature = "python")]
/// Значение `doc.get(key) or {}` → (dict | ошибка-тип). Возвращает Some(dict) если объект, None если «не dict».
fn get_obj_or_empty<'py>(d: &Bound<'py, PyDict>, key: &str) -> Result<Result<Option<Bound<'py, PyDict>>, ()>, Out> {
    // Ok(Ok(Some(d))) — dict; Ok(Ok(None)) — falsy → пустой dict; Ok(Err(())) — truthy не-dict
    let v = match d.get_item(key).map_err(|_| Out::Fallback)? {
        None => return Ok(Ok(None)),
        Some(v) => v,
    };
    match classify(&v).map_err(|_| Out::Fallback)? {
        None => Err(Out::Fallback),
        Some(V::Dict(x)) => {
            if x.len() == 0 {
                Ok(Ok(None))
            } else {
                Ok(Ok(Some(x)))
            }
        }
        Some(V::List(l)) => {
            if l.len() == 0 {
                Ok(Ok(None))
            } else {
                Ok(Err(()))
            }
        }
        Some(V::Str(s)) => {
            if s.is_empty() {
                Ok(Ok(None))
            } else {
                Ok(Err(()))
            }
        }
        Some(V::Int(i)) => {
            if i.is_truthy().map_err(|_| Out::Fallback)? {
                Ok(Err(()))
            } else {
                Ok(Ok(None))
            }
        }
        Some(V::Float(f)) => {
            if f != 0.0 {
                Ok(Err(())) // NaN тоже истинно
            } else {
                Ok(Ok(None))
            }
        }
        Some(V::None) => Ok(Ok(None)),
        Some(V::Bool) => {
            let b: bool = v.extract().map_err(|_| Out::Fallback)?;
            if b {
                Ok(Err(()))
            } else {
                Ok(Ok(None))
            }
        }
    }
}

#[cfg(feature = "python")]
/// `_text(value, name, limit)`.
fn text(v: Option<Bound<'_, PyAny>>, name: &str, limit: usize) -> Result<String, Out> {
    let v = match v {
        None => return Ok(String::new()),
        Some(v) => v,
    };
    match classify(&v).map_err(|_| Out::Fallback)? {
        None => Err(Out::Fallback),
        Some(V::None) => Ok(String::new()),
        Some(V::Str(s)) => {
            let t = py_strip(&s);
            if t.chars().count() > limit {
                return Err(Out::Err(format!("{name}: слишком длинное значение")));
            }
            Ok(t.to_string())
        }
        Some(_) => Err(Out::Err(format!("{name}: нужна строка"))),
    }
}

fn addr_ok(a: &str) -> bool {
    let rest = if let Some(r) = a.strip_prefix("https://") {
        r
    } else if let Some(r) = a.strip_prefix("http://") {
        r
    } else {
        return false;
    };
    let mut it = rest.chars();
    match it.next() {
        None => return false,
        Some(c) => {
            if is_py_space(c) || matches!(c, '/' | '$' | '.' | '?' | '#') {
                return false;
            }
        }
    }
    it.all(|c| !is_py_space(c))
}

fn model_ok(m: &str) -> bool {
    let mut it = m.chars();
    match it.next() {
        Some(c) if c.is_ascii_alphanumeric() => {}
        _ => return false,
    }
    let mut n = 1;
    for c in it {
        n += 1;
        if !(c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | ':' | '/' | '@' | '+' | '-')) {
            return false;
        }
    }
    n <= 128
}

#[cfg(feature = "python")]
/// Строка из `doc.get(key, default)` для сравнения с перечнем: Ok(Some(s)) — строка; Ok(None) — не строка (не равна ничему из перечня).
fn str_or_other(v: Option<Bound<'_, PyAny>>, default: &str) -> Result<Option<String>, Out> {
    match v {
        None => Ok(Some(default.to_string())),
        Some(o) => match classify(&o).map_err(|_| Out::Fallback)? {
            None => Err(Out::Fallback),
            Some(V::Str(s)) => Ok(Some(s)),
            Some(_) => Ok(None),
        },
    }
}

#[cfg(feature = "python")]
fn run<'py>(py: Python<'py>, doc: &Bound<'py, PyAny>) -> Result<Bound<'py, PyDict>, Out> {
    let d = match classify(doc).map_err(|_| Out::Fallback)? {
        None => return Err(Out::Fallback),
        Some(V::Dict(d)) => d,
        Some(_) => return Err(Out::Err("ожидался JSON-объект".to_string())),
    };
    // неизвестные поля (сортировка по кодовым точкам)
    let mut unknown: Vec<String> = Vec::new();
    for (k, _) in d.iter() {
        match classify(&k).map_err(|_| Out::Fallback)? {
            Some(V::Str(s)) => {
                if !["voice", "advisors", "local", "remote", "api"].contains(&s.as_str()) {
                    unknown.push(s);
                }
            }
            _ => return Err(Out::Fallback),
        }
    }
    if !unknown.is_empty() {
        unknown.sort();
        return Err(Out::Err(format!("неизвестные поля: {}", unknown.join(", "))));
    }
    let api_in = match get_obj_or_empty(&d, "api")? {
        Err(()) => return Err(Out::Err("api: нужен объект".to_string())),
        Ok(x) => x,
    };
    if let Some(a) = &api_in {
        for k in ["key", "api_key", "token", "secret"] {
            if a.contains(k).map_err(|_| Out::Fallback)? {
                return Err(Out::Err("ключ API здесь не сохраняется (хранение ключа ещё не подключено)".to_string()));
            }
        }
    }
    // voice
    let voice = match str_or_other(d.get_item("voice").map_err(|_| Out::Fallback)?, "")? {
        Some(s) if s.is_empty() || KINDS.contains(&s.as_str()) => s,
        _ => return Err(Out::Err("voice: local, remote или api".to_string())),
    };
    // advisors
    let adv_bad = || Out::Err("advisors: список из local / remote / api".to_string());
    let advisors_v = d.get_item("advisors").map_err(|_| Out::Fallback)?;
    let mut advisors: Vec<String> = Vec::new();
    match advisors_v {
        None => {}
        Some(o) => match classify(&o).map_err(|_| Out::Fallback)? {
            None => return Err(Out::Fallback),
            Some(V::List(l)) => {
                let mut bad = false;
                for a in l.iter() {
                    match classify(&a).map_err(|_| Out::Fallback)? {
                        None => return Err(Out::Fallback),
                        Some(V::Str(s)) => {
                            if KINDS.contains(&s.as_str()) {
                                advisors.push(s);
                            } else {
                                bad = true;
                            }
                        }
                        Some(_) => bad = true,
                    }
                }
                if bad {
                    return Err(adv_bad());
                }
            }
            Some(_) => return Err(adv_bad()),
        },
    }
    let advisors_sorted: Vec<&str> = KINDS.iter().copied().filter(|k| advisors.iter().any(|a| a == k)).collect();
    // local / remote
    let local = get_obj_or_empty(&d, "local")?;
    let remote = get_obj_or_empty(&d, "remote")?;
    let (local, remote) = match (local, remote) {
        (Ok(l), Ok(r)) => (l, r),
        _ => return Err(Out::Err("local и remote: нужны объекты".to_string())),
    };
    let get = |o: &Option<Bound<'py, PyDict>>, k: &str| -> Result<Option<Bound<'py, PyAny>>, Out> {
        match o {
            None => Ok(None),
            Some(x) => x.get_item(k).map_err(|_| Out::Fallback),
        }
    };
    let path = text(get(&local, "path")?, "local.path", MAX_PATH)?;
    let address = text(get(&remote, "address")?, "remote.address", 512)?;
    if !address.is_empty() && !addr_ok(&address) {
        return Err(Out::Err("remote.address: адрес вида http://хост:порт".to_string()));
    }
    // оригинал строит кортеж из ОБОИХ `_text(...)` ДО проверок регулярками: ошибка типа/длины api.model опережает ошибку формата remote.model
    let remote_model = text(get(&remote, "model")?, "remote.model", 128)?;
    let api_model = text(get(&api_in, "model")?, "api.model", 128)?;
    if !remote_model.is_empty() && !model_ok(&remote_model) {
        return Err(Out::Err("remote.model: имя модели (буквы, цифры и . _ : / @ + -)".to_string()));
    }
    if !api_model.is_empty() && !model_ok(&api_model) {
        return Err(Out::Err("api.model: имя модели (буквы, цифры и . _ : / @ + -)".to_string()));
    }
    let service = match str_or_other(get(&api_in, "service")?, "openai")? {
        Some(s) if SERVICES.contains(&s.as_str()) => s,
        _ => return Err(Out::Err("api.service: openai, anthropic или other".to_string())),
    };
    // голос и советники
    if voice.is_empty() {
        return Err(Out::Err("выберите Голос: тот, кто будет отвечать".to_string()));
    }
    let filled = |kind: &str| -> bool {
        match kind {
            "local" => !path.is_empty(),
            "remote" => !address.is_empty() && !remote_model.is_empty(),
            _ => !api_model.is_empty(),
        }
    };
    let mut to_check: Vec<&str> = vec![voice.as_str()];
    for k in advisors_sorted.iter() {
        if !to_check.contains(k) {
            to_check.push(k);
        }
    }
    for kind in to_check {
        if !filled(kind) {
            let name = match kind {
                "local" => "Локальная",
                "remote" => "Удалённая",
                _ => "API",
            };
            return Err(Out::Err(format!("блок «{name}» выбран, но не заполнен")));
        }
    }
    // результат — как defaults() с подставленными значениями, тот же порядок ключей
    let out = PyDict::new_bound(py);
    out.set_item("saved", false).map_err(|_| Out::Fallback)?;
    out.set_item("voice", voice).map_err(|_| Out::Fallback)?;
    out.set_item("advisors", advisors_sorted).map_err(|_| Out::Fallback)?;
    let l = PyDict::new_bound(py);
    l.set_item("path", path).map_err(|_| Out::Fallback)?;
    out.set_item("local", l).map_err(|_| Out::Fallback)?;
    let r = PyDict::new_bound(py);
    r.set_item("address", address).map_err(|_| Out::Fallback)?;
    r.set_item("model", remote_model).map_err(|_| Out::Fallback)?;
    out.set_item("remote", r).map_err(|_| Out::Fallback)?;
    let a = PyDict::new_bound(py);
    a.set_item("service", service).map_err(|_| Out::Fallback)?;
    a.set_item("model", api_model).map_err(|_| Out::Fallback)?;
    out.set_item("api", a).map_err(|_| Out::Fallback)?;
    Ok(out)
}

#[cfg(feature = "python")]
/// `(True, dict)` — годный документ; `(False, текст)` — SettingsError; None — посторонний тип, выполнить Python.
#[pyfunction]
#[pyo3(name = "validate")]
fn py_validate(py: Python<'_>, doc: &Bound<'_, PyAny>) -> PyResult<Option<PyObject>> {
    Ok(match run(py, doc) {
        Ok(d) => Some((true, d).into_py(py)),
        Err(Out::Err(m)) => Some((false, m).into_py(py)),
        Err(Out::Fallback) => None,
    })
}

#[cfg(feature = "python")]
pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_validate, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn address() {
        assert!(addr_ok("http://192.168.1.5:8080"));
        assert!(addr_ok("https://h"));
        assert!(!addr_ok("http://"));
        assert!(!addr_ok("http:///x"));
        assert!(!addr_ok("ftp://x"));
        assert!(!addr_ok("http://a b"));
        assert!(!addr_ok("http://.x"));
    }

    #[test]
    fn model() {
        assert!(model_ok("qwen-14b"));
        assert!(model_ok("a/b:c@d+e.f_g"));
        assert!(!model_ok("-x"));
        assert!(!model_ok("é"));
        assert!(model_ok(&"a".repeat(128)));
        assert!(!model_ok(&"a".repeat(129)));
    }
}
