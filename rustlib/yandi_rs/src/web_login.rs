//! Перенос части pet/web_login.py — Sessions (сессии входа) и Throttle (замедление подбора
//! пароля). Чисто в памяти; время — через `_clock`, ПЕРЕСТАВЛЯЕМЫЙ Python-вызываемый объект
//! (не спрятанная в Rust функция): pet/pet_web_login_regression_test.py делает именно
//! `wl.sessions._clock = Clock()` ПОСЛЕ создания объекта, чтобы гонять время в тестах без
//! реального ожидания — если бы `_clock` здесь был обычным Rust-полем без обратного вызова в
//! Python, это перестало бы работать и был бы уже не перенос, а другое поведение.
//!
//! Что НЕ перенесено (осталось в Python, pet/web_login.py): маршруты FastAPI, сборка cookie,
//! вызов утилиты yandi-keys, ASGI-прослойка WebLoginMiddleware — HTTP/ASGI-специфика, не сюда.
//! Классы Sessions/Throttle (определения) тоже остаются в Python — их сабклассит
//! pet_web_login_regression_test.py для собственных тестовых дублей (NoThrottle, KeepsSessions);
//! Rust-версия — это то, что может встать ВМЕСТО экземпляра-одиночки (`sessions`/`throttle` в
//! pet/web_login.py), не замена самим классам.
//!
//! Статус (2026-09-23): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_LOGIN_ENGINE=rust (см. pet/web_login.py).

use pyo3::prelude::*;
use rand::rngs::OsRng;
use rand::RngCore;
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

// pet/web_login.py:: SESSION_SECS, REMEMBER_SECS, FREE_ATTEMPTS, BACKOFF_BASE, BACKOFF_MAX
const SESSION_SECS: i64 = 12 * 3600;
const REMEMBER_SECS: i64 = 30 * 24 * 3600;
const FREE_ATTEMPTS: i64 = 3;
const BACKOFF_BASE: i64 = 2;
const BACKOFF_MAX: i64 = 300;

fn read_clock(py: Python<'_>, clock: &Py<PyAny>) -> PyResult<f64> {
    clock.call0(py)?.extract::<f64>(py)
}

/// pet/web_login.py: `secrets.token_urlsafe(32)` — nbytes случайных байт из CSPRNG,
/// base64url без паддинга. Значения токенов НЕ должны и не могут совпадать между Python и
/// Rust (оба честно случайны) — сверяется контрактом (parity-тест), а не байт в байт.
fn token_urlsafe(nbytes: usize) -> String {
    use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
    let mut buf = vec![0u8; nbytes];
    OsRng.fill_bytes(&mut buf);
    URL_SAFE_NO_PAD.encode(buf)
}

#[allow(dead_code)]
fn system_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// pet/web_login.py::Sessions
#[pyclass]
pub struct Sessions {
    #[pyo3(get, set, name = "_clock")]
    clock: Py<PyAny>,
    items: HashMap<String, f64>,
}

#[pymethods]
impl Sessions {
    #[new]
    #[pyo3(signature = (clock=None))]
    fn new(py: Python<'_>, clock: Option<Py<PyAny>>) -> PyResult<Self> {
        let clock = match clock {
            Some(c) => c,
            None => py.import_bound("time")?.getattr("time")?.unbind(),
        };
        Ok(Self { clock, items: HashMap::new() })
    }

    fn create(&mut self, py: Python<'_>, remember: bool) -> PyResult<(String, i64)> {
        let token = token_urlsafe(32);
        let secs = if remember { REMEMBER_SECS } else { SESSION_SECS };
        let now = read_clock(py, &self.clock)?;
        self.items.insert(token.clone(), now + secs as f64);
        self.purge(py)?;
        Ok((token, secs))
    }

    #[pyo3(signature = (token=None))]
    fn valid(&mut self, py: Python<'_>, token: Option<String>) -> PyResult<bool> {
        let token = match token {
            Some(t) if !t.is_empty() => t,
            _ => return Ok(false),
        };
        let expires = match self.items.get(&token) {
            Some(e) => *e,
            None => return Ok(false),
        };
        let now = read_clock(py, &self.clock)?;
        if expires <= now {
            self.items.remove(&token);
            return Ok(false);
        }
        Ok(true)
    }

    #[pyo3(signature = (token=None))]
    fn end(&mut self, token: Option<String>) {
        self.items.remove(&token.unwrap_or_default());
    }

    fn clear(&mut self) {
        self.items.clear();
    }

    fn _purge(&mut self, py: Python<'_>) -> PyResult<()> {
        self.purge(py)
    }
}

impl Sessions {
    fn purge(&mut self, py: Python<'_>) -> PyResult<()> {
        let now = read_clock(py, &self.clock)?;
        self.items.retain(|_, expires| *expires > now);
        Ok(())
    }
}

/// pet/web_login.py::Throttle
#[pyclass]
pub struct Throttle {
    #[pyo3(get, set, name = "_clock")]
    clock: Py<PyAny>,
    #[pyo3(get, set)]
    failures: i64,
    #[pyo3(get, set)]
    last: f64,
}

#[pymethods]
impl Throttle {
    #[new]
    #[pyo3(signature = (clock=None))]
    fn new(py: Python<'_>, clock: Option<Py<PyAny>>) -> PyResult<Self> {
        let clock = match clock {
            Some(c) => c,
            None => py.import_bound("time")?.getattr("time")?.unbind(),
        };
        Ok(Self { clock, failures: 0, last: 0.0 })
    }

    #[staticmethod]
    fn backoff_after(failures: i64) -> i64 {
        if failures <= FREE_ATTEMPTS {
            return 0;
        }
        let shift = (failures - FREE_ATTEMPTS - 1).clamp(0, 31);
        (BACKOFF_BASE * (1i64 << shift)).min(BACKOFF_MAX)
    }

    fn remaining(&self, py: Python<'_>) -> PyResult<i64> {
        let backoff = Self::backoff_after(self.failures);
        if backoff == 0 {
            return Ok(0);
        }
        let now = read_clock(py, &self.clock)?;
        let wait = backoff as f64 - (now - self.last);
        Ok(if wait > 0.0 { (wait as i64) + 1 } else { 0 })
    }

    fn failed(&mut self, py: Python<'_>) -> PyResult<()> {
        self.failures += 1;
        self.last = read_clock(py, &self.clock)?;
        Ok(())
    }

    fn succeeded(&mut self) {
        self.failures = 0;
    }
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Sessions>()?;
    m.add_class::<Throttle>()?;
    Ok(())
}

// ── Юнит-тесты чистой логики (backoff_after — единственная часть без часов/Python) ──────────
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_matches_python_formula() {
        // FREE_ATTEMPTS=3: 0 до и включая 3 неудач; затем 2,4,8,...,300 (потолок)
        assert_eq!(Throttle::backoff_after(0), 0);
        assert_eq!(Throttle::backoff_after(3), 0);
        assert_eq!(Throttle::backoff_after(4), 2);
        assert_eq!(Throttle::backoff_after(5), 4);
        assert_eq!(Throttle::backoff_after(6), 8);
        assert_eq!(Throttle::backoff_after(7), 16);
        assert_eq!(Throttle::backoff_after(8), 32);
        assert_eq!(Throttle::backoff_after(9), 64);
        assert_eq!(Throttle::backoff_after(10), 128);
        assert_eq!(Throttle::backoff_after(11), 256);
        assert_eq!(Throttle::backoff_after(12), 300); // потолок (было бы 512)
        assert_eq!(Throttle::backoff_after(1000), 300); // далеко за потолком, не переполняется
    }

    #[test]
    fn token_urlsafe_length_and_alphabet() {
        let t = token_urlsafe(32);
        // 32 случайных байта в base64url-без-паддинга: ceil(32*8/6) = 43 символа
        assert_eq!(t.len(), 43);
        assert!(t.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'));
        let t2 = token_urlsafe(32);
        assert_ne!(t, t2, "два вызова не должны дать одно и то же (иначе не CSPRNG)");
    }
}
