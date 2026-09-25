//! Контекст вызова: соединение с базой, часы и источник случайных идентификаторов. В бою — настоящее время и случайные числа; в тестах — заданные.
use std::cell::RefCell;

use serde_json::{json, Value};
use yandi_db::rusqlite::Connection;

use crate::R;

pub struct Ctx<'a> {
    pub conn: &'a Connection,
    /// Секунды Unix (UTC) «сейчас» с дробной частью; None — системные часы.
    pub fixed_now: Option<f64>,
    /// Заранее заданные значения «uuid4().hex» (по очереди); пусто — случайные.
    ids: RefCell<Vec<String>>,
}

impl<'a> Ctx<'a> {
    pub fn new(conn: &'a Connection) -> Self {
        Ctx { conn, fixed_now: None, ids: RefCell::new(vec![]) }
    }

    pub fn with_clock(mut self, now: f64) -> Self {
        self.fixed_now = Some(now);
        self
    }

    pub fn with_ids(self, ids: Vec<String>) -> Self {
        *self.ids.borrow_mut() = ids;
        self
    }

    pub fn now_secs(&self) -> f64 {
        self.fixed_now.unwrap_or_else(|| std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs_f64())
    }

    /// `uuid.uuid4().hex`
    pub fn uuid_hex(&self) -> String {
        let mut q = self.ids.borrow_mut();
        if !q.is_empty() {
            return q.remove(0);
        }
        use rand::RngCore;
        let mut b = [0u8; 16];
        rand::rngs::OsRng.fill_bytes(&mut b);
        b[6] = (b[6] & 0x0f) | 0x40;
        b[8] = (b[8] & 0x3f) | 0x80;
        b.iter().map(|x| format!("{x:02x}")).collect()
    }

    /// Вызов репозитория БД: `repo("имя", json!({...}))`.
    pub fn repo(&self, name: &str, args: Value) -> R<Value> {
        yandi_db::repo::call(self.conn, name, &args)
    }

    /// Текущее время как значение для репозитория (секунды Unix — репозиторий сам округлит до секунды, как MySQL).
    pub fn now_value(&self) -> Value {
        json!(self.now_secs())
    }
}

/// Значение времени из строки БД → секунды Unix.
pub fn dt_secs(v: &Value) -> Option<f64> {
    v.as_str().and_then(yandi_db::repo::parse_dt_secs)
}
