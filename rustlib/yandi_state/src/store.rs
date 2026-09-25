//! Хранилище: строки (байты) с TTL, списки, множества. Один `Mutex` — команды атомарны, как в однопоточном Redis.
//! Истечение срока — ленивое (при обращении) плюс очистка при `KEYS`/`SCAN`/`DBSIZE`; время — настенные часы в миллисекундах, как в Redis (срок хранится абсолютным).
//! Порядок проверок аргументов — как в Redis (для каждой команды: что проверяется раньше — число, ключ или тип), тексты ошибок — Redis 7.0.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::glob::glob_match;
use crate::pubsub::{self, Hub, Subscription};
use crate::reply::{Reply, NOT_INT, SYNTAX, WRONGTYPE};

#[derive(Debug, Clone)]
enum Value {
    Str(Vec<u8>),
    List(VecDeque<Vec<u8>>),
    Set(HashSet<Vec<u8>>),
}

#[derive(Debug, Clone)]
struct Entry {
    value: Value,
    /// абсолютное время истечения, unix-миллисекунды (как в Redis)
    expire_at: Option<i64>,
}

#[derive(Default)]
struct Db {
    map: HashMap<Vec<u8>, Entry>,
}

pub struct Store {
    db: Mutex<Db>,
    hub: Arc<Mutex<Hub>>,
}

impl Default for Store {
    fn default() -> Self {
        Store::new()
    }
}

fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

fn parse_i64(b: &[u8]) -> Option<i64> {
    // Redis `string2ll`: без пробелов, без '+', без ведущих нулей (кроме "0"), диапазон i64
    let s = std::str::from_utf8(b).ok()?;
    if s.is_empty() || s.len() > 20 {
        return None;
    }
    let digits = s.strip_prefix('-').unwrap_or(s);
    if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    if digits.len() > 1 && digits.starts_with('0') {
        return None;
    }
    if s == "-0" {
        return None;
    }
    s.parse::<i64>().ok()
}

fn upper(b: &[u8]) -> String {
    String::from_utf8_lossy(b).to_ascii_uppercase()
}

fn wrong_args(cmd: &str) -> Reply {
    Reply::Error(format!("ERR wrong number of arguments for '{}' command", cmd.to_ascii_lowercase()))
}

impl Db {
    fn expired(&self, e: &Entry) -> bool {
        // как `keyIsExpired` в Redis: истёк, когда now СТРОГО больше срока
        e.expire_at.map(|t| now_ms() > t).unwrap_or(false)
    }

    /// Удалить просроченный ключ (ленивое истечение).
    fn purge(&mut self, key: &[u8]) {
        if let Some(e) = self.map.get(key) {
            if self.expired(e) {
                self.map.remove(key);
            }
        }
    }

    fn purge_all(&mut self) {
        let now = now_ms();
        self.map.retain(|_, e| e.expire_at.map(|t| now <= t).unwrap_or(true));
    }

    fn get(&mut self, key: &[u8]) -> Option<&mut Entry> {
        self.purge(key);
        self.map.get_mut(key)
    }
}

/// Оставшееся время жизни в миллисекундах (>= 0); None — без срока.
fn ttl_ms(e: &Entry) -> Option<i64> {
    e.expire_at.map(|t| (t - now_ms()).max(0))
}

/// Абсолютный срок из относительного значения; None — переполнение (Redis: `invalid expire time`).
fn expire_from(unit_ms: bool, n: i64) -> Option<i64> {
    let ms = if unit_ms { n } else { n.checked_mul(1000)? };
    now_ms().checked_add(ms)
}

/// Часть аргумента до первого NUL (Redis печатает аргументы в сообщении об ошибке как C-строки), \r и \n → пробел.
fn c_arg(b: &[u8], max: usize) -> String {
    let end = b.iter().position(|c| *c == 0).unwrap_or(b.len());
    let cut = &b[..end.min(max)];
    String::from_utf8_lossy(cut).replace(['\r', '\n'], " ")
}

impl Store {
    pub fn new() -> Self {
        Store { db: Mutex::new(Db::default()), hub: Hub::new() }
    }

    pub fn new_subscription(&self) -> Subscription {
        pubsub::new_subscription(&self.hub)
    }

    /// Выполнить команду (`argv[0]` — имя, регистр не важен).
    pub fn execute(&self, argv: &[Vec<u8>]) -> Reply {
        if argv.is_empty() {
            return Reply::err("ERR empty command");
        }
        let cmd = upper(&argv[0]);
        let a = &argv[1..];
        if cmd == "PUBLISH" {
            return if a.len() != 2 { wrong_args(&cmd) } else { Reply::Int(pubsub::publish(&self.hub, &a[0], &a[1])) };
        }
        let mut db = self.db.lock().unwrap();
        match cmd.as_str() {
            "PING" => match a.len() {
                0 => Reply::Status("PONG".into()),
                1 => Reply::bulk(&a[0]),
                _ => wrong_args(&cmd),
            },
            "GET" => cmd_get(&mut db, a, &cmd),
            "SET" => cmd_set(&mut db, a),
            "SETEX" | "PSETEX" => cmd_setex(&mut db, a, &cmd),
            "SETNX" => {
                if a.len() != 2 {
                    return wrong_args(&cmd);
                }
                if db.get(&a[0]).is_some() {
                    Reply::Int(0)
                } else {
                    db.map.insert(a[0].clone(), Entry { value: Value::Str(a[1].clone()), expire_at: None });
                    Reply::Int(1)
                }
            }
            "GETSET" => {
                if a.len() != 2 {
                    return wrong_args(&cmd);
                }
                let old = match db.get(&a[0]) {
                    None => Reply::Nil,
                    Some(Entry { value: Value::Str(v), .. }) => Reply::Bulk(v.clone()),
                    Some(_) => return Reply::err(WRONGTYPE),
                };
                db.map.insert(a[0].clone(), Entry { value: Value::Str(a[1].clone()), expire_at: None });
                old
            }
            "MGET" => {
                if a.is_empty() {
                    return wrong_args(&cmd);
                }
                Reply::Array(
                    a.iter()
                        .map(|k| match db.get(k) {
                            Some(Entry { value: Value::Str(v), .. }) => Reply::Bulk(v.clone()),
                            _ => Reply::Nil,
                        })
                        .collect(),
                )
            }
            "MSET" => {
                if a.is_empty() || a.len() % 2 != 0 {
                    return wrong_args(&cmd);
                }
                for kv in a.chunks(2) {
                    db.map.insert(kv[0].clone(), Entry { value: Value::Str(kv[1].clone()), expire_at: None });
                }
                Reply::ok()
            }
            "DEL" | "UNLINK" => {
                if a.is_empty() {
                    return wrong_args(&cmd);
                }
                let mut n = 0;
                for k in a {
                    db.purge(k);
                    if db.map.remove(k).is_some() {
                        n += 1;
                    }
                }
                Reply::Int(n)
            }
            "EXISTS" => {
                if a.is_empty() {
                    return wrong_args(&cmd);
                }
                Reply::Int(a.iter().filter(|k| db.get(k).is_some()).count() as i64)
            }
            "EXPIRE" | "PEXPIRE" => cmd_expire(&mut db, a, &cmd),
            "TTL" | "PTTL" => {
                if a.len() != 1 {
                    return wrong_args(&cmd);
                }
                match db.get(&a[0]) {
                    None => Reply::Int(-2),
                    Some(e) => match ttl_ms(e) {
                        None => Reply::Int(-1),
                        Some(ms) => Reply::Int(if cmd == "PTTL" { ms } else { (ms + 500) / 1000 }),
                    },
                }
            }
            "PERSIST" => {
                if a.len() != 1 {
                    return wrong_args(&cmd);
                }
                match db.get(&a[0]) {
                    Some(e) if e.expire_at.is_some() => {
                        e.expire_at = None;
                        Reply::Int(1)
                    }
                    _ => Reply::Int(0),
                }
            }
            "TYPE" => {
                if a.len() != 1 {
                    return wrong_args(&cmd);
                }
                Reply::Status(
                    match db.get(&a[0]) {
                        None => "none",
                        Some(Entry { value: Value::Str(_), .. }) => "string",
                        Some(Entry { value: Value::List(_), .. }) => "list",
                        Some(Entry { value: Value::Set(_), .. }) => "set",
                    }
                    .into(),
                )
            }
            "INCR" | "DECR" | "INCRBY" | "DECRBY" => cmd_incr(&mut db, a, &cmd),
            "APPEND" => {
                if a.len() != 2 {
                    return wrong_args(&cmd);
                }
                match db.get(&a[0]) {
                    None => {
                        let n = a[1].len() as i64;
                        db.map.insert(a[0].clone(), Entry { value: Value::Str(a[1].clone()), expire_at: None });
                        Reply::Int(n)
                    }
                    Some(Entry { value: Value::Str(v), .. }) => {
                        v.extend_from_slice(&a[1]);
                        Reply::Int(v.len() as i64)
                    }
                    Some(_) => Reply::err(WRONGTYPE),
                }
            }
            "STRLEN" => {
                if a.len() != 1 {
                    return wrong_args(&cmd);
                }
                match db.get(&a[0]) {
                    None => Reply::Int(0),
                    Some(Entry { value: Value::Str(v), .. }) => Reply::Int(v.len() as i64),
                    Some(_) => Reply::err(WRONGTYPE),
                }
            }
            "LPUSH" | "RPUSH" | "LPUSHX" | "RPUSHX" => cmd_push(&mut db, a, &cmd),
            "LPOP" | "RPOP" => cmd_pop(&mut db, a, &cmd),
            "LLEN" => {
                if a.len() != 1 {
                    return wrong_args(&cmd);
                }
                match db.get(&a[0]) {
                    None => Reply::Int(0),
                    Some(Entry { value: Value::List(l), .. }) => Reply::Int(l.len() as i64),
                    Some(_) => Reply::err(WRONGTYPE),
                }
            }
            "LRANGE" => cmd_lrange(&mut db, a, &cmd),
            "LTRIM" => cmd_ltrim(&mut db, a, &cmd),
            "LINDEX" => {
                if a.len() != 2 {
                    return wrong_args(&cmd);
                }
                // порядок как в Redis: ключ (нет → nil) → тип → разбор индекса
                match db.get(&a[0]) {
                    None => Reply::Nil,
                    Some(Entry { value: Value::List(l), .. }) => {
                        let idx = match parse_i64(&a[1]) {
                            Some(i) => i,
                            None => return Reply::err(NOT_INT),
                        };
                        let n = l.len() as i64;
                        let i = if idx < 0 { idx + n } else { idx };
                        if i < 0 || i >= n {
                            Reply::Nil
                        } else {
                            Reply::Bulk(l[i as usize].clone())
                        }
                    }
                    Some(_) => Reply::err(WRONGTYPE),
                }
            }
            "LSET" => {
                if a.len() != 3 {
                    return wrong_args(&cmd);
                }
                // ключ (нет → `no such key`) → тип → разбор индекса
                match db.get(&a[0]) {
                    None => Reply::err("ERR no such key"),
                    Some(Entry { value: Value::List(l), .. }) => {
                        let idx = match parse_i64(&a[1]) {
                            Some(i) => i,
                            None => return Reply::err(NOT_INT),
                        };
                        let n = l.len() as i64;
                        let i = if idx < 0 { idx + n } else { idx };
                        if i < 0 || i >= n {
                            Reply::err("ERR index out of range")
                        } else {
                            l[i as usize] = a[2].clone();
                            Reply::ok()
                        }
                    }
                    Some(_) => Reply::err(WRONGTYPE),
                }
            }
            "LREM" => cmd_lrem(&mut db, a, &cmd),
            "LINSERT" => cmd_linsert(&mut db, a, &cmd),
            "SADD" => {
                if a.len() < 2 {
                    return wrong_args(&cmd);
                }
                let key = a[0].clone();
                let cur = db.get(&key);
                match cur {
                    Some(Entry { value: Value::Set(s), .. }) => Reply::Int(a[1..].iter().filter(|m| s.insert((*m).clone())).count() as i64),
                    Some(_) => Reply::err(WRONGTYPE),
                    None => {
                        let mut s = HashSet::new();
                        let n = a[1..].iter().filter(|m| s.insert((*m).clone())).count() as i64;
                        db.map.insert(key, Entry { value: Value::Set(s), expire_at: None });
                        Reply::Int(n)
                    }
                }
            }
            "SREM" => {
                if a.len() < 2 {
                    return wrong_args(&cmd);
                }
                let key = a[0].clone();
                let (n, empty) = match db.get(&key) {
                    None => return Reply::Int(0),
                    Some(Entry { value: Value::Set(s), .. }) => (a[1..].iter().filter(|m| s.remove(*m)).count() as i64, s.is_empty()),
                    Some(_) => return Reply::err(WRONGTYPE),
                };
                if empty {
                    db.map.remove(&key);
                }
                Reply::Int(n)
            }
            "SCARD" => {
                if a.len() != 1 {
                    return wrong_args(&cmd);
                }
                match db.get(&a[0]) {
                    None => Reply::Int(0),
                    Some(Entry { value: Value::Set(s), .. }) => Reply::Int(s.len() as i64),
                    Some(_) => Reply::err(WRONGTYPE),
                }
            }
            "SISMEMBER" => {
                if a.len() != 2 {
                    return wrong_args(&cmd);
                }
                match db.get(&a[0]) {
                    None => Reply::Int(0),
                    Some(Entry { value: Value::Set(s), .. }) => Reply::Int(s.contains(&a[1]) as i64),
                    Some(_) => Reply::err(WRONGTYPE),
                }
            }
            "SMEMBERS" => {
                if a.len() != 1 {
                    return wrong_args(&cmd);
                }
                match db.get(&a[0]) {
                    None => Reply::Array(vec![]),
                    Some(Entry { value: Value::Set(s), .. }) => Reply::Array(s.iter().map(|m| Reply::Bulk(m.clone())).collect()),
                    Some(_) => Reply::err(WRONGTYPE),
                }
            }
            "KEYS" => {
                if a.len() != 1 {
                    return wrong_args(&cmd);
                }
                db.purge_all();
                Reply::Array(db.map.keys().filter(|k| glob_match(&a[0], k)).map(|k| Reply::Bulk(k.clone())).collect())
            }
            "SCAN" => cmd_scan(&mut db, a, &cmd),
            "RENAME" => {
                if a.len() != 2 {
                    return wrong_args(&cmd);
                }
                db.purge(&a[0]);
                match db.map.remove(&a[0]) {
                    None => Reply::err("ERR no such key"),
                    Some(e) => {
                        db.purge(&a[1]);
                        db.map.insert(a[1].clone(), e);
                        Reply::ok()
                    }
                }
            }
            "DBSIZE" => {
                if !a.is_empty() {
                    return wrong_args(&cmd);
                }
                db.purge_all();
                Reply::Int(db.map.len() as i64)
            }
            "FLUSHDB" | "FLUSHALL" => {
                db.map.clear();
                Reply::ok()
            }
            _ => {
                // как `rejectCommand`: имя ≤ 128 байт; аргументы печатаются C-строками (до первого NUL), пока набрано < 128 символов
                let mut args = String::new();
                for x in a.iter() {
                    if args.len() >= 128 {
                        break;
                    }
                    args.push_str(&format!("'{}' ", c_arg(x, 128 - args.len())));
                }
                Reply::Error(format!("ERR unknown command '{}', with args beginning with: {}", c_arg(&argv[0], 128), args))
            }
        }
    }
}

fn cmd_get(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    if a.len() != 1 {
        return wrong_args(cmd);
    }
    match db.get(&a[0]) {
        None => Reply::Nil,
        Some(Entry { value: Value::Str(v), .. }) => Reply::Bulk(v.clone()),
        Some(_) => Reply::err(WRONGTYPE),
    }
}

enum Exp {
    Rel { ms: bool, n: i64 },
    Abs { ms: bool, n: i64 },
}

#[derive(Clone, Copy, PartialEq)]
enum ExpKind {
    Ex,
    Px,
    ExAt,
    PxAt,
}

fn cmd_set(db: &mut Db, a: &[Vec<u8>]) -> Reply {
    if a.len() < 2 {
        return wrong_args("SET");
    }
    let (mut nx, mut xx, mut get, mut keepttl) = (false, false, false, false);
    // как `parseExtendedStringArgumentsOrReply`: сначала разбираются ВСЕ опции (ошибка синтаксиса раньше ошибки числа), повтор одной и той же
    // EX/PX/EXAT/PXAT допустим (побеждает последнее значение), смесь разных или с KEEPTTL — синтаксическая ошибка; число разбирается ПОСЛЕ цикла
    let mut kind: Option<ExpKind> = None;
    let mut expire_arg: Option<&Vec<u8>> = None;
    let mut i = 2;
    while i < a.len() {
        let opt = upper(&a[i]);
        let next = i + 1 < a.len();
        match opt.as_str() {
            "NX" if !xx => nx = true,
            "XX" if !nx => xx = true,
            "GET" => get = true,
            "KEEPTTL" if kind.is_none() => keepttl = true,
            "EX" if !keepttl && matches!(kind, None | Some(ExpKind::Ex)) && next => {
                kind = Some(ExpKind::Ex);
                expire_arg = Some(&a[i + 1]);
                i += 1;
            }
            "PX" if !keepttl && matches!(kind, None | Some(ExpKind::Px)) && next => {
                kind = Some(ExpKind::Px);
                expire_arg = Some(&a[i + 1]);
                i += 1;
            }
            "EXAT" if !keepttl && matches!(kind, None | Some(ExpKind::ExAt)) && next => {
                kind = Some(ExpKind::ExAt);
                expire_arg = Some(&a[i + 1]);
                i += 1;
            }
            "PXAT" if !keepttl && matches!(kind, None | Some(ExpKind::PxAt)) && next => {
                kind = Some(ExpKind::PxAt);
                expire_arg = Some(&a[i + 1]);
                i += 1;
            }
            _ => return Reply::err(SYNTAX),
        }
        i += 1;
    }
    let expire: Option<Exp> = match (kind, expire_arg) {
        (Some(k), Some(arg)) => {
            let n = match parse_i64(arg) {
                Some(n) => n,
                None => return Reply::err(NOT_INT),
            };
            let ms = matches!(k, ExpKind::Px | ExpKind::PxAt);
            Some(if matches!(k, ExpKind::ExAt | ExpKind::PxAt) { Exp::Abs { ms, n } } else { Exp::Rel { ms, n } })
        }
        _ => None,
    };
    let invalid = || Reply::err("ERR invalid expire time in 'set' command");
    let when: Option<i64> = match &expire {
        None => None,
        Some(Exp::Rel { ms, n }) => {
            if *n <= 0 {
                return invalid();
            }
            match expire_from(*ms, *n) {
                Some(w) => Some(w),
                None => return invalid(),
            }
        }
        Some(Exp::Abs { ms, n }) => {
            if *n <= 0 {
                return invalid();
            }
            match if *ms { Some(*n) } else { n.checked_mul(1000) } {
                Some(w) => Some(w),
                None => return invalid(),
            }
        }
    };
    let key = a[0].clone();
    // GET: старое значение (и WRONGTYPE) до записи
    let old: Option<Vec<u8>> = if get {
        match db.get(&key) {
            None => None,
            Some(Entry { value: Value::Str(v), .. }) => Some(v.clone()),
            Some(_) => return Reply::err(WRONGTYPE),
        }
    } else {
        None
    };
    let exists = db.get(&key).is_some();
    if (nx && exists) || (xx && !exists) {
        return if get { old.map(Reply::Bulk).unwrap_or(Reply::Nil) } else { Reply::Nil };
    }
    let keep = if keepttl { db.get(&key).and_then(|e| e.expire_at) } else { None };
    let expire_at = if when.is_some() { when } else { keep };
    if let Some(w) = expire_at {
        if matches!(expire, Some(Exp::Abs { .. })) && w <= now_ms() {
            // срок уже в прошлом: значение записывается и тут же истекает (ключа не остаётся)
            db.map.remove(&key);
            return if get { old.map(Reply::Bulk).unwrap_or(Reply::Nil) } else { Reply::ok() };
        }
    }
    db.map.insert(key, Entry { value: Value::Str(a[1].clone()), expire_at });
    if get {
        old.map(Reply::Bulk).unwrap_or(Reply::Nil)
    } else {
        Reply::ok()
    }
}

fn cmd_setex(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    if a.len() != 3 {
        return wrong_args(cmd);
    }
    let n = match parse_i64(&a[1]) {
        Some(n) => n,
        None => return Reply::err(NOT_INT),
    };
    if n <= 0 {
        return Reply::Error(format!("ERR invalid expire time in '{}' command", cmd.to_ascii_lowercase()));
    }
    match expire_from(cmd == "PSETEX", n) {
        Some(w) => {
            db.map.insert(a[0].clone(), Entry { value: Value::Str(a[2].clone()), expire_at: Some(w) });
            Reply::ok()
        }
        None => Reply::Error(format!("ERR invalid expire time in '{}' command", cmd.to_ascii_lowercase())),
    }
}

fn cmd_expire(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    if a.len() < 2 {
        return wrong_args(cmd);
    }
    let n = match parse_i64(&a[1]) {
        Some(n) => n,
        None => return Reply::err(NOT_INT),
    };
    let (mut nx, mut xx, mut gt, mut lt) = (false, false, false, false);
    for o in &a[2..] {
        match upper(o).as_str() {
            "NX" => nx = true,
            "XX" => xx = true,
            "GT" => gt = true,
            "LT" => lt = true,
            other => return Reply::Error(format!("ERR Unsupported option {}", other)),
        }
    }
    if nx && (xx || gt || lt) {
        return Reply::err("ERR NX and XX, GT or LT options at the same time are not compatible");
    }
    if gt && lt {
        return Reply::err("ERR GT and LT options at the same time are not compatible");
    }
    let unit_ms = cmd == "PEXPIRE";
    let invalid = || Reply::Error(format!("ERR invalid expire time in '{}' command", cmd.to_ascii_lowercase()));
    let when = match expire_from(unit_ms, n) {
        Some(w) => w,
        None => return invalid(),
    };
    let key = a[0].clone();
    let cur: Option<i64> = match db.get(&key) {
        None => return Reply::Int(0),
        Some(e) => e.expire_at,
    };
    if nx && cur.is_some() {
        return Reply::Int(0);
    }
    if xx && cur.is_none() {
        return Reply::Int(0);
    }
    // сравнение по абсолютным миллисекундам; ключ без срока считается «бесконечным»
    if gt && !cur.map(|c| when > c).unwrap_or(false) {
        return Reply::Int(0);
    }
    if lt && !cur.map(|c| when < c).unwrap_or(true) {
        return Reply::Int(0);
    }
    if when <= now_ms() {
        db.map.remove(&key);
        return Reply::Int(1);
    }
    if let Some(e) = db.get(&key) {
        e.expire_at = Some(when);
    }
    Reply::Int(1)
}

fn cmd_incr(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    let (by_arg, sign): (bool, i64) = match cmd {
        "INCR" => (false, 1),
        "DECR" => (false, -1),
        "INCRBY" => (true, 1),
        _ => (true, -1),
    };
    if a.len() != if by_arg { 2 } else { 1 } {
        return wrong_args(cmd);
    }
    let by = if by_arg {
        match parse_i64(&a[1]) {
            Some(n) => n,
            None => return Reply::err(NOT_INT),
        }
    } else {
        1
    };
    let key = a[0].clone();
    let cur = match db.get(&key) {
        None => 0,
        Some(Entry { value: Value::Str(v), .. }) => match parse_i64(v) {
            Some(n) => n,
            None => return Reply::err(NOT_INT),
        },
        Some(_) => return Reply::err(WRONGTYPE),
    };
    let delta = if sign < 0 {
        match by.checked_neg() {
            Some(d) => d,
            None => return Reply::err("ERR decrement would overflow"),
        }
    } else {
        by
    };
    let next = match cur.checked_add(delta) {
        Some(n) => n,
        None => return Reply::err("ERR increment or decrement would overflow"),
    };
    match db.map.get_mut(&key) {
        Some(e) => e.value = Value::Str(next.to_string().into_bytes()),
        None => {
            db.map.insert(key, Entry { value: Value::Str(next.to_string().into_bytes()), expire_at: None });
        }
    }
    Reply::Int(next)
}

fn cmd_push(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    if a.len() < 2 {
        return wrong_args(cmd);
    }
    let left = cmd.starts_with('L');
    let only_existing = cmd.ends_with('X');
    let key = a[0].clone();
    match db.get(&key) {
        Some(Entry { value: Value::List(l), .. }) => {
            for v in &a[1..] {
                if left {
                    l.push_front(v.clone());
                } else {
                    l.push_back(v.clone());
                }
            }
            Reply::Int(l.len() as i64)
        }
        Some(_) => Reply::err(WRONGTYPE),
        None => {
            if only_existing {
                return Reply::Int(0);
            }
            let mut l = VecDeque::new();
            for v in &a[1..] {
                if left {
                    l.push_front(v.clone());
                } else {
                    l.push_back(v.clone());
                }
            }
            let n = l.len() as i64;
            db.map.insert(key, Entry { value: Value::List(l), expire_at: None });
            Reply::Int(n)
        }
    }
}

fn cmd_pop(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    if a.is_empty() || a.len() > 2 {
        return wrong_args(cmd);
    }
    let left = cmd == "LPOP";
    let count: Option<i64> = if a.len() == 2 {
        match parse_i64(&a[1]) {
            Some(n) if n >= 0 => Some(n),
            _ => return Reply::err("ERR value is out of range, must be positive"),
        }
    } else {
        None
    };
    let key = a[0].clone();
    let (reply, empty) = match db.get(&key) {
        None => return Reply::Nil,
        Some(Entry { value: Value::List(l), .. }) => {
            let take = |l: &mut VecDeque<Vec<u8>>| if left { l.pop_front() } else { l.pop_back() };
            match count {
                None => (take(l).map(Reply::Bulk).unwrap_or(Reply::Nil), l.is_empty()),
                Some(c) => {
                    let mut out = Vec::new();
                    for _ in 0..c {
                        match take(l) {
                            Some(v) => out.push(Reply::Bulk(v)),
                            None => break,
                        }
                    }
                    (Reply::Array(out), l.is_empty())
                }
            }
        }
        Some(_) => return Reply::err(WRONGTYPE),
    };
    if empty {
        db.map.remove(&key);
    }
    reply
}

/// Нормализация диапазона [start, stop] по длине; None — пусто.
fn range_bounds(start: i64, stop: i64, n: i64) -> Option<(usize, usize)> {
    let mut s = if start < 0 { start + n } else { start };
    let mut e = if stop < 0 { stop + n } else { stop };
    if s < 0 {
        s = 0;
    }
    if s > e || s >= n {
        return None;
    }
    if e >= n {
        e = n - 1;
    }
    Some((s as usize, e as usize))
}

fn cmd_lrange(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    if a.len() != 3 {
        return wrong_args(cmd);
    }
    let (s, e) = match (parse_i64(&a[1]), parse_i64(&a[2])) {
        (Some(s), Some(e)) => (s, e),
        _ => return Reply::err(NOT_INT),
    };
    match db.get(&a[0]) {
        None => Reply::Array(vec![]),
        Some(Entry { value: Value::List(l), .. }) => match range_bounds(s, e, l.len() as i64) {
            None => Reply::Array(vec![]),
            Some((s, e)) => Reply::Array(l.iter().skip(s).take(e - s + 1).map(|v| Reply::Bulk(v.clone())).collect()),
        },
        Some(_) => Reply::err(WRONGTYPE),
    }
}

fn cmd_ltrim(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    if a.len() != 3 {
        return wrong_args(cmd);
    }
    let (s, e) = match (parse_i64(&a[1]), parse_i64(&a[2])) {
        (Some(s), Some(e)) => (s, e),
        _ => return Reply::err(NOT_INT),
    };
    let key = a[0].clone();
    let empty = match db.get(&key) {
        None => return Reply::ok(),
        Some(Entry { value: Value::List(l), .. }) => {
            match range_bounds(s, e, l.len() as i64) {
                None => l.clear(),
                Some((s, e)) => {
                    let kept: VecDeque<Vec<u8>> = l.iter().skip(s).take(e - s + 1).cloned().collect();
                    *l = kept;
                }
            }
            l.is_empty()
        }
        Some(_) => return Reply::err(WRONGTYPE),
    };
    if empty {
        db.map.remove(&key);
    }
    Reply::ok()
}

fn cmd_lrem(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    if a.len() != 3 {
        return wrong_args(cmd);
    }
    let count = match parse_i64(&a[1]) {
        Some(n) => n,
        None => return Reply::err(NOT_INT),
    };
    let key = a[0].clone();
    let (removed, empty) = match db.get(&key) {
        None => return Reply::Int(0),
        Some(Entry { value: Value::List(l), .. }) => {
            let mut removed = 0i64;
            let limit = if count == 0 { i64::MAX } else { count.abs() };
            if count >= 0 {
                let mut out = VecDeque::new();
                for v in l.iter() {
                    if *v == a[2] && removed < limit {
                        removed += 1;
                    } else {
                        out.push_back(v.clone());
                    }
                }
                *l = out;
            } else {
                let mut out: Vec<Vec<u8>> = Vec::new();
                for v in l.iter().rev() {
                    if *v == a[2] && removed < limit {
                        removed += 1;
                    } else {
                        out.push(v.clone());
                    }
                }
                out.reverse();
                *l = out.into();
            }
            (removed, l.is_empty())
        }
        Some(_) => return Reply::err(WRONGTYPE),
    };
    if empty {
        db.map.remove(&key);
    }
    Reply::Int(removed)
}

fn cmd_linsert(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    if a.len() != 4 {
        return wrong_args(cmd);
    }
    let before = match upper(&a[1]).as_str() {
        "BEFORE" => true,
        "AFTER" => false,
        _ => return Reply::err(SYNTAX),
    };
    match db.get(&a[0]) {
        None => Reply::Int(0),
        Some(Entry { value: Value::List(l), .. }) => match l.iter().position(|v| *v == a[2]) {
            None => Reply::Int(-1),
            Some(p) => {
                l.insert(if before { p } else { p + 1 }, a[3].clone());
                Reply::Int(l.len() as i64)
            }
        },
        Some(_) => Reply::err(WRONGTYPE),
    }
}

/// `parseScanCursorOrReply`: строка C (до первого NUL), пустая → 0; первый символ — пробельный → ошибка; далее `strtoul` (знак, цифры) до конца, переполнение → ошибка;
/// отрицательное число оборачивается в беззнаковое (огромный курсор).
fn parse_cursor(b: &[u8]) -> Option<u64> {
    let end = b.iter().position(|c| *c == 0).unwrap_or(b.len());
    let s = &b[..end];
    if s.is_empty() {
        return Some(0);
    }
    if s[0].is_ascii_whitespace() || s[0] == 0x0b {
        return None;
    }
    let (neg, digits) = match s[0] {
        b'-' => (true, &s[1..]),
        b'+' => (false, &s[1..]),
        _ => (false, s),
    };
    if digits.is_empty() || !digits.iter().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let v: u128 = std::str::from_utf8(digits).ok()?.parse().ok()?;
    if v > u64::MAX as u128 {
        return None;
    }
    let v = v as u64;
    Some(if neg { v.wrapping_neg() } else { v })
}

fn cmd_scan(db: &mut Db, a: &[Vec<u8>], cmd: &str) -> Reply {
    if a.is_empty() {
        return wrong_args(cmd);
    }
    let cursor = match parse_cursor(&a[0]) {
        Some(c) => c,
        None => return Reply::err("ERR invalid cursor"),
    };
    let mut pattern: Option<Vec<u8>> = None;
    let mut i = 1;
    while i < a.len() {
        match upper(&a[i]).as_str() {
            "MATCH" if i + 1 < a.len() => {
                pattern = Some(a[i + 1].clone());
                i += 2;
            }
            "COUNT" if i + 1 < a.len() => {
                match parse_i64(&a[i + 1]) {
                    Some(n) if n >= 1 => {}
                    Some(_) => return Reply::err(SYNTAX),
                    None => return Reply::err(NOT_INT),
                }
                i += 2;
            }
            "TYPE" if i + 1 < a.len() => i += 2,
            _ => return Reply::err(SYNTAX),
        }
    }
    db.purge_all();
    // любой курсор — весь набор за один шаг с курсором 0 (COUNT — лишь подсказка, полный обход допустим; у Redis на маленьких таблицах курсор тоже
    // «схлопывается» и отдаёт всё). Вызывающий, дошедший до курсора 0, обход завершил.
    let _ = cursor;
    let keys: Vec<Reply> = db.map.keys().filter(|k| pattern.as_ref().map(|p| glob_match(p, k)).unwrap_or(true)).map(|k| Reply::Bulk(k.clone())).collect();
    Reply::Array(vec![Reply::bulk(b"0"), Reply::Array(keys)])
}
