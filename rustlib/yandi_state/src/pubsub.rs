//! Публикация/подписка внутри процесса. `publish` возвращает число получателей КАК В REDIS: подписчики канала + подписчики по шаблонам,
//! совпавшим с каналом (один клиент, подписанный и на канал, и на шаблон, получает ДВА сообщения и считается дважды).

use std::collections::HashMap;
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};

use crate::glob::glob_match;

/// Сообщение, доставленное подписчику: (вид, шаблон-или-пусто, канал, данные).
#[derive(Debug, Clone, PartialEq)]
pub struct Message {
    pub kind: &'static str, // "message" | "pmessage"
    pub pattern: Option<Vec<u8>>,
    pub channel: Vec<u8>,
    pub data: Vec<u8>,
}

type Tx = Sender<Message>;

#[derive(Default)]
pub struct Hub {
    channels: HashMap<Vec<u8>, Vec<(u64, Tx)>>,
    patterns: HashMap<Vec<u8>, Vec<(u64, Tx)>>,
    next_id: u64,
}

pub struct Subscription {
    pub id: u64,
    pub rx: Receiver<Message>,
    hub: Arc<Mutex<Hub>>,
    tx: Tx,
}

impl Hub {
    pub fn new() -> Arc<Mutex<Hub>> {
        Arc::new(Mutex::new(Hub::default()))
    }
}

/// Новый подписчик (без каналов; каналы/шаблоны добавляются `subscribe`/`psubscribe`).
pub fn new_subscription(hub: &Arc<Mutex<Hub>>) -> Subscription {
    let (tx, rx) = channel();
    let mut h = hub.lock().unwrap();
    h.next_id += 1;
    Subscription { id: h.next_id, rx, hub: hub.clone(), tx }
}

impl Subscription {
    pub fn subscribe(&self, channel: &[u8]) {
        let mut h = self.hub.lock().unwrap();
        let v = h.channels.entry(channel.to_vec()).or_default();
        if !v.iter().any(|(id, _)| *id == self.id) {
            v.push((self.id, self.tx.clone()));
        }
    }
    pub fn psubscribe(&self, pattern: &[u8]) {
        let mut h = self.hub.lock().unwrap();
        let v = h.patterns.entry(pattern.to_vec()).or_default();
        if !v.iter().any(|(id, _)| *id == self.id) {
            v.push((self.id, self.tx.clone()));
        }
    }
    pub fn unsubscribe(&self, channel: &[u8]) {
        let mut h = self.hub.lock().unwrap();
        if let Some(v) = h.channels.get_mut(channel) {
            v.retain(|(id, _)| *id != self.id);
            if v.is_empty() {
                h.channels.remove(channel);
            }
        }
    }
    pub fn punsubscribe(&self, pattern: &[u8]) {
        let mut h = self.hub.lock().unwrap();
        if let Some(v) = h.patterns.get_mut(pattern) {
            v.retain(|(id, _)| *id != self.id);
            if v.is_empty() {
                h.patterns.remove(pattern);
            }
        }
    }
    /// Следующее сообщение без ожидания.
    pub fn try_recv(&self) -> Option<Message> {
        self.rx.try_recv().ok()
    }
}

impl Drop for Subscription {
    fn drop(&mut self) {
        let mut h = self.hub.lock().unwrap();
        for v in h.channels.values_mut() {
            v.retain(|(id, _)| *id != self.id);
        }
        h.channels.retain(|_, v| !v.is_empty());
        for v in h.patterns.values_mut() {
            v.retain(|(id, _)| *id != self.id);
        }
        h.patterns.retain(|_, v| !v.is_empty());
    }
}

/// `PUBLISH`: число получателей (канальные + шаблонные).
pub fn publish(hub: &Arc<Mutex<Hub>>, channel: &[u8], data: &[u8]) -> i64 {
    let h = hub.lock().unwrap();
    let mut n = 0i64;
    if let Some(v) = h.channels.get(channel) {
        for (_, tx) in v {
            if tx.send(Message { kind: "message", pattern: None, channel: channel.to_vec(), data: data.to_vec() }).is_ok() {
                n += 1;
            }
        }
    }
    for (pat, v) in h.patterns.iter() {
        if glob_match(pat, channel) {
            for (_, tx) in v {
                if tx.send(Message { kind: "pmessage", pattern: Some(pat.clone()), channel: channel.to_vec(), data: data.to_vec() }).is_ok() {
                    n += 1;
                }
            }
        }
    }
    n
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts() {
        let hub = Hub::new();
        let a = new_subscription(&hub);
        a.subscribe(b"c");
        a.psubscribe(b"c*");
        assert_eq!(publish(&hub, b"c", b"x"), 2);
        assert_eq!(publish(&hub, b"d", b"x"), 0);
        assert_eq!(a.try_recv().unwrap().kind, "message");
        assert_eq!(a.try_recv().unwrap().kind, "pmessage");
        drop(a);
        assert_eq!(publish(&hub, b"c", b"x"), 0);
    }
}
