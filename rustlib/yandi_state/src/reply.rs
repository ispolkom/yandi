//! Ответ команды — как разобранный RESP: nil, целое, bulk-строка (байты), простая строка (`OK`, `PONG`, тип), массив, ошибка.

#[derive(Debug, Clone, PartialEq)]
pub enum Reply {
    Nil,
    Int(i64),
    Bulk(Vec<u8>),
    Status(String),
    Array(Vec<Reply>),
    Error(String),
}

impl Reply {
    pub fn ok() -> Reply {
        Reply::Status("OK".to_string())
    }
    pub fn err(msg: &str) -> Reply {
        Reply::Error(msg.to_string())
    }
    pub fn bulk(b: &[u8]) -> Reply {
        Reply::Bulk(b.to_vec())
    }
}

pub const WRONGTYPE: &str = "WRONGTYPE Operation against a key holding the wrong kind of value";
pub const NOT_INT: &str = "ERR value is not an integer or out of range";
pub const SYNTAX: &str = "ERR syntax error";
