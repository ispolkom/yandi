// src/netlayer/onion_stream.rs
//! 🆕 Hardening Step 8: stream-reassembly buffer для classical Tor cell-в-cell.
//!
//! Цель — заменить «инициатор pre-builds N independent cells» (текущая упрощённая
//! схема, см. `wrap_onion_forward_chain` в `onion.rs`) на потоковую модель, где
//! полезная нагрузка любой длины фрагментируется на cell-фрагменты, каждый из
//! которых имеет (stream_id, seq, last_flag). Hop собирает фрагменты в буфере
//! и доставляет потребителю когда увидел `last_flag = true`.
//!
//! Это модуль scaffolding'а: интеграция в `wrap_onion_forward` — следующая итерация
//! (требует переосмыслить layout `pt_block` чтобы оставить место под фрагменты).
//! Сам буфер готов к использованию и покрыт unit-тестами.

use std::collections::HashMap;
use std::time::{Duration, Instant};

/// 4-байтовый stream-id. Назначается инициатором.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct StreamId(pub u32);

#[derive(Clone, Debug)]
pub struct CellFragment {
    pub stream_id: StreamId,
    pub seq: u32,
    pub last: bool,
    pub payload: Vec<u8>,
}

impl CellFragment {
    /// Wire layout: `[stream_id:4][seq:4][last:1][len:2][payload:len]`.
    pub fn encode(&self) -> Vec<u8> {
        let mut buf = Vec::with_capacity(4 + 4 + 1 + 2 + self.payload.len());
        buf.extend_from_slice(&self.stream_id.0.to_be_bytes());
        buf.extend_from_slice(&self.seq.to_be_bytes());
        buf.push(if self.last { 1 } else { 0 });
        let len = self.payload.len().min(u16::MAX as usize) as u16;
        buf.extend_from_slice(&len.to_be_bytes());
        buf.extend_from_slice(&self.payload[..len as usize]);
        buf
    }

    pub fn decode(data: &[u8]) -> Option<Self> {
        if data.len() < 4 + 4 + 1 + 2 {
            return None;
        }
        let mut sid = [0u8; 4];
        sid.copy_from_slice(&data[0..4]);
        let stream_id = StreamId(u32::from_be_bytes(sid));
        let mut seq_b = [0u8; 4];
        seq_b.copy_from_slice(&data[4..8]);
        let seq = u32::from_be_bytes(seq_b);
        let last = data[8] != 0;
        let mut len_b = [0u8; 2];
        len_b.copy_from_slice(&data[9..11]);
        let len = u16::from_be_bytes(len_b) as usize;
        if data.len() < 11 + len {
            return None;
        }
        let payload = data[11..11 + len].to_vec();
        Some(Self { stream_id, seq, last, payload })
    }
}

#[derive(Debug)]
struct StreamState {
    /// seq → payload. HashMap чтобы обрабатывать out-of-order.
    fragments: HashMap<u32, Vec<u8>>,
    /// Последний seq (если уже видели last_flag).
    final_seq: Option<u32>,
    /// Когда видели последнюю активность.
    last_activity: Instant,
}

impl Default for StreamState {
    fn default() -> Self {
        Self {
            fragments: HashMap::new(),
            final_seq: None,
            last_activity: Instant::now(),
        }
    }
}

/// Hardening Step 8: per-hop буфер для сборки потоков из cell-фрагментов.
/// `max_stream_buffer` — потолок памяти на стрим (защита от amplification).
/// `idle_timeout` — старые стримы выкидываются при `gc`.
#[derive(Debug)]
pub struct StreamReassemblyBuffer {
    streams: HashMap<StreamId, StreamState>,
    max_stream_buffer: usize,
    idle_timeout: Duration,
}

impl StreamReassemblyBuffer {
    pub fn new() -> Self {
        Self {
            streams: HashMap::new(),
            max_stream_buffer: 16 * 1024 * 1024, // 16 MB / поток
            idle_timeout: Duration::from_secs(120),
        }
    }

    pub fn with_limits(max_stream_buffer: usize, idle_timeout: Duration) -> Self {
        Self {
            streams: HashMap::new(),
            max_stream_buffer,
            idle_timeout,
        }
    }

    /// Добавить фрагмент. Возвращает Some(reassembled-bytes) когда стрим закрыт
    /// (last_flag = true) И все seq'и от 0 до final_seq собраны.
    pub fn add(&mut self, frag: CellFragment) -> Option<Vec<u8>> {
        let state = self.streams.entry(frag.stream_id).or_default();
        state.last_activity = Instant::now();
        if frag.last {
            state.final_seq = Some(frag.seq);
        }
        // Защита от amplification: если уже превысили — игнорируем.
        let already: usize = state.fragments.values().map(|v| v.len()).sum();
        if already + frag.payload.len() > self.max_stream_buffer {
            self.streams.remove(&frag.stream_id);
            return None;
        }
        state.fragments.insert(frag.seq, frag.payload);

        if let Some(final_seq) = state.final_seq {
            // Проверяем что seq 0..=final_seq все собраны.
            if state.fragments.len() as u32 != final_seq + 1 {
                return None;
            }
            // Собираем по порядку.
            let mut out = Vec::new();
            for s in 0..=final_seq {
                match state.fragments.get(&s) {
                    Some(p) => out.extend_from_slice(p),
                    None => return None,
                }
            }
            // Стрим завершён — удаляем.
            self.streams.remove(&frag.stream_id);
            return Some(out);
        }
        None
    }

    /// Сколько стримов сейчас в работе.
    pub fn active_streams(&self) -> usize {
        self.streams.len()
    }

    /// Удалить стримы старше idle_timeout. Возвращает кол-во удалённых.
    pub fn gc(&mut self) -> usize {
        let now = Instant::now();
        let timeout = self.idle_timeout;
        let before = self.streams.len();
        self.streams.retain(|_, s| now.duration_since(s.last_activity) <= timeout);
        before - self.streams.len()
    }
}

impl Default for StreamReassemblyBuffer {
    fn default() -> Self { Self::new() }
}

// ---------------- Tests ----------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fragment_codec_roundtrip() {
        let f = CellFragment {
            stream_id: StreamId(0xABCD_1234),
            seq: 7,
            last: true,
            payload: vec![1u8, 2, 3, 4, 5],
        };
        let bytes = f.encode();
        let f2 = CellFragment::decode(&bytes).unwrap();
        assert_eq!(f2.stream_id, f.stream_id);
        assert_eq!(f2.seq, f.seq);
        assert_eq!(f2.last, f.last);
        assert_eq!(f2.payload, f.payload);
    }

    #[test]
    fn reassembly_in_order() {
        let mut buf = StreamReassemblyBuffer::new();
        let sid = StreamId(1);
        assert!(buf.add(CellFragment { stream_id: sid, seq: 0, last: false, payload: b"hello-".to_vec() }).is_none());
        assert!(buf.add(CellFragment { stream_id: sid, seq: 1, last: false, payload: b"world-".to_vec() }).is_none());
        let out = buf.add(CellFragment { stream_id: sid, seq: 2, last: true, payload: b"end".to_vec() });
        assert_eq!(out.as_deref(), Some(b"hello-world-end".as_slice()));
        // Закрытый стрим удалён
        assert_eq!(buf.active_streams(), 0);
    }

    #[test]
    fn reassembly_out_of_order() {
        let mut buf = StreamReassemblyBuffer::new();
        let sid = StreamId(2);
        assert!(buf.add(CellFragment { stream_id: sid, seq: 2, last: true, payload: b"!".to_vec() }).is_none());
        assert!(buf.add(CellFragment { stream_id: sid, seq: 0, last: false, payload: b"hi".to_vec() }).is_none());
        let out = buf.add(CellFragment { stream_id: sid, seq: 1, last: false, payload: b"-there".to_vec() });
        assert_eq!(out.as_deref(), Some(b"hi-there!".as_slice()));
    }

    #[test]
    fn reassembly_with_gaps_pending() {
        // Без middle-фрагмента не должно отдавать.
        let mut buf = StreamReassemblyBuffer::new();
        let sid = StreamId(3);
        assert!(buf.add(CellFragment { stream_id: sid, seq: 0, last: false, payload: b"a".to_vec() }).is_none());
        assert!(buf.add(CellFragment { stream_id: sid, seq: 2, last: true, payload: b"c".to_vec() }).is_none());
        // seq=1 не пришёл — assembly не должен отдать
        assert_eq!(buf.active_streams(), 1);
    }

    #[test]
    fn amplification_protection() {
        // Превышение max_stream_buffer выкидывает стрим.
        let mut buf = StreamReassemblyBuffer::with_limits(10, Duration::from_secs(60));
        let sid = StreamId(4);
        assert!(buf.add(CellFragment { stream_id: sid, seq: 0, last: false, payload: vec![0u8; 6] }).is_none());
        assert!(buf.add(CellFragment { stream_id: sid, seq: 1, last: false, payload: vec![0u8; 6] }).is_none());
        assert_eq!(buf.active_streams(), 0);
    }

    #[test]
    fn gc_removes_idle_streams() {
        let mut buf = StreamReassemblyBuffer::with_limits(1024, Duration::from_millis(10));
        buf.add(CellFragment { stream_id: StreamId(1), seq: 0, last: false, payload: b"x".to_vec() });
        std::thread::sleep(Duration::from_millis(30));
        assert_eq!(buf.gc(), 1);
        assert_eq!(buf.active_streams(), 0);
    }
}
