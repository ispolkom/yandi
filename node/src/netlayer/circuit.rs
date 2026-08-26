// src/netlayer/circuit.rs
//! Multi-hop circuit для YANDI (Iter 3).
//!
//! Circuit — это упорядоченный путь через несколько anchor-узлов:
//! `client → hop1 → hop2 → … → exit`.
//! Используется в `multi-hop по умолчанию: mobile → home anchor → foreign anchor → интернет`.
//!
//! **Что делает этот модуль (Iter 3, transport-only):**
//! - Структуры `Circuit`, `Hop`, `CircuitState`.
//! - `CircuitManager` — реестр живых circuit'ов на ноде.
//! - Wire format пакетов 0xB0..0xB3 (build/extend/data/close) — encode/decode.
//! - Per-hop key derivation (HKDF поверх существующего session key с peer'ом).
//!
//! **Чего здесь НЕТ (намеренно отложено):**
//! - Telescoping handshake и layered onion encryption — это Iter 5.
//!   В Iter 3 каждый hop обрабатывает CIRCUIT_DATA через свой существующий session key с
//!   соседями (transport-only encryption, как сказано в плане 3.6).
//! - Реальный forward через `P2PTransport` — bridge будет в `transport.rs` отдельно.
//! - DHT-фильтр `find_anchors_by_jurisdiction` (план 3.3) — отложен на интеграцию с DHT.
//! - HTTP/SOCKS5 proxy через circuit (план 3.7) — переключается отдельно после ядра.
//!
//! Wire format (big-endian, без alignment):
//! ```text
//! 0xB0 BUILD:   [B0][circuit_id:16][hop_count:1][hop_id_0:32][hop_id_1:32]…
//!               Тот, кто получает BUILD — первый hop, далее уже EXTEND.
//! 0xB1 EXTEND:  [B1][circuit_id:16][next_hop_id:32]
//! 0xB2 DATA:    [B2][circuit_id:16][direction:1][payload_len:2][payload…]
//!               direction: 0 — forward (client→exit), 1 — backward (exit→client).
//! 0xB3 CLOSE:   [B3][circuit_id:16][reason:1]
//! ```

use crate::util::HashId;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::Mutex;

pub const PKT_CIRCUIT_BUILD: u8 = 0xB0;
pub const PKT_CIRCUIT_EXTEND: u8 = 0xB1;
pub const PKT_CIRCUIT_DATA: u8 = 0xB2;
pub const PKT_CIRCUIT_CLOSE: u8 = 0xB3;
/// 🆕 Hardening Step 7: EXTEND_REPLY — hop отвечает X25519 pubkey'ем чтобы
/// инициатор смог посчитать ECDH-shared secret для layer-key'а.
pub const PKT_CIRCUIT_EXTEND_REPLY: u8 = 0xB4;

/// 16-байтовый идентификатор circuit'а. Случайный, выбирается инициатором.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct CircuitId(pub [u8; 16]);

impl CircuitId {
    pub fn random() -> Self {
        let mut id = [0u8; 16];
        getrandom_fill(&mut id);
        CircuitId(id)
    }

    /// Нулевой CircuitId. Hardening Step 4: используется как «не привязан»
    /// в delivery-канале когда конкретный circuit неизвестен потребителю.
    pub fn zero() -> Self {
        CircuitId([0u8; 16])
    }

    pub fn from_bytes(b: &[u8]) -> Option<Self> {
        if b.len() < 16 {
            return None;
        }
        let mut id = [0u8; 16];
        id.copy_from_slice(&b[..16]);
        Some(CircuitId(id))
    }
}

fn getrandom_fill(buf: &mut [u8]) {
    use rand::RngCore;
    let mut rng = rand::thread_rng();
    rng.fill_bytes(buf);
}

/// Состояние circuit'а в его lifecycle.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CircuitState {
    /// Создан клиентом, ещё не выслан BUILD.
    New,
    /// BUILD отправлен, ожидаем подтверждения от первого hop'а (extends в процессе).
    Building,
    /// Все hop'ы добавлены, циркуит готов передавать DATA.
    Ready,
    /// Получен CLOSE или истёк TTL.
    Closed,
}

/// Описание одного hop'а в circuit'е.
#[derive(Clone, Debug)]
pub struct Hop {
    pub peer_id: HashId,
    /// Per-hop key derivation: SHA-256(session_key_with_peer || circuit_id || hop_index).
    /// На Iter 3 не используется напрямую (transport-only), но заводится для подготовки к Iter 5.
    pub derived_key: [u8; 32],
}

/// Один circuit в реестре. Хранит порядок hop'ов и состояние.
#[derive(Clone, Debug)]
pub struct Circuit {
    pub id: CircuitId,
    pub hops: Vec<Hop>,
    pub state: CircuitState,
    /// UNIX seconds создания.
    pub created_at: u64,
    /// TTL в секундах. Closed при превышении.
    pub ttl_secs: u64,
    /// Если эта нода — middle-hop (relay в circuit'е), то это id предыдущей и следующей пары
    /// для backward/forward маршрутизации DATA.
    pub upstream: Option<HashId>,
    pub downstream: Option<HashId>,
    /// 🧅 Step 5: onion-mode флаг. Если true — `CIRCUIT_DATA` для этого circuit'а
    /// упакован в фикс. 1024B onion-cell (см. `crate::netlayer::onion`). Если false —
    /// Iter 3 variable-length DATA layout. Default false для совместимости.
    pub onion_mode: bool,
}

impl Circuit {
    pub fn new_initiator(hop_ids: Vec<HashId>, ttl_secs: u64) -> Self {
        let id = CircuitId::random();
        let hops = hop_ids
            .into_iter()
            .enumerate()
            .map(|(idx, peer_id)| Hop {
                peer_id,
                derived_key: derive_hop_key(&id, idx as u8, &peer_id),
            })
            .collect();
        Self {
            id,
            hops,
            state: CircuitState::New,
            created_at: now_secs(),
            ttl_secs,
            upstream: None,
            downstream: None,
            onion_mode: false,
        }
    }

    /// Включить onion-mode на инициаторе. Для middle-hop'ов это автоматически
    /// определяется по входному cell-формату (1041B = onion).
    pub fn with_onion_mode(mut self) -> Self {
        self.onion_mode = true;
        self
    }

    /// На relay-ноде: создаём запись о пролетающем circuit'е.
    pub fn new_relay(id: CircuitId, upstream: HashId, downstream: HashId, ttl_secs: u64) -> Self {
        Self {
            id,
            hops: Vec::new(),
            state: CircuitState::Ready,
            created_at: now_secs(),
            ttl_secs,
            upstream: Some(upstream),
            downstream: Some(downstream),
            onion_mode: false,
        }
    }

    pub fn is_expired(&self) -> bool {
        now_secs().saturating_sub(self.created_at) > self.ttl_secs
    }

    /// hop_index 0 = первый peer, 1 = второй и т.д. Возвращает None если индекс вне диапазона.
    pub fn hop_at(&self, idx: usize) -> Option<&Hop> {
        self.hops.get(idx)
    }

    pub fn last_hop(&self) -> Option<&Hop> {
        self.hops.last()
    }
}

/// Простая HKDF-подобная производная: blake3(b"yandi-circuit-v1" || circuit_id || hop_idx || peer_id).
/// На Iter 3 это болванка для Iter 5 onion-encryption (там это станет HKDF над DH-shared-secret).
pub fn derive_hop_key(circuit_id: &CircuitId, hop_idx: u8, peer_id: &HashId) -> [u8; 32] {
    let mut hasher = blake3::Hasher::new();
    hasher.update(b"yandi-circuit-v1");
    hasher.update(&circuit_id.0);
    hasher.update(&[hop_idx]);
    hasher.update(&peer_id.0);
    *hasher.finalize().as_bytes()
}

/// 🆕 Hardening Step 7: HKDF-derived hop-key из ECDH-shared-secret.
/// Используется в telescoping handshake'е: после X25519-обмена между инициатором
/// и каждым hop'ом, обе стороны вычисляют одинаковый layer-key через HKDF.
///
/// info string: `"yandi-circuit-telescope-v1" || circuit_id || hop_idx`.
pub fn derive_hop_key_ecdh(shared_secret: &[u8; 32], circuit_id: &CircuitId, hop_idx: u8) -> [u8; 32] {
    use sha2::Sha256;
    use hkdf::Hkdf;
    let mut info = Vec::with_capacity(26 + 16 + 1);
    info.extend_from_slice(b"yandi-circuit-telescope-v1");
    info.extend_from_slice(&circuit_id.0);
    info.push(hop_idx);
    let hk = Hkdf::<Sha256>::new(None, shared_secret);
    let mut out = [0u8; 32];
    hk.expand(&info, &mut out).expect("hkdf expand");
    out
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

// --------------- Wire encode/decode ---------------

/// Закодировать BUILD-пакет. Без 0xB0 prefix'а (он добавляется dispatcher'ом, как у других).
pub fn encode_build(c: &Circuit) -> Vec<u8> {
    let mut buf = Vec::with_capacity(1 + 16 + 1 + c.hops.len() * 32);
    buf.push(PKT_CIRCUIT_BUILD);
    buf.extend_from_slice(&c.id.0);
    buf.push(c.hops.len().min(255) as u8);
    for h in &c.hops {
        buf.extend_from_slice(&h.peer_id.0);
    }
    buf
}

/// Раскодировать BUILD: вернуть circuit_id и список hop_id'ов.
pub fn decode_build(data: &[u8]) -> Result<(CircuitId, Vec<HashId>), String> {
    if data.len() < 1 + 16 + 1 {
        return Err("BUILD too short".into());
    }
    if data[0] != PKT_CIRCUIT_BUILD {
        return Err(format!("BUILD bad magic: 0x{:02x}", data[0]));
    }
    let circuit_id = CircuitId::from_bytes(&data[1..17]).ok_or_else(|| "bad circuit_id".to_string())?;
    let hop_count = data[17] as usize;
    if data.len() < 1 + 16 + 1 + hop_count * 32 {
        return Err("BUILD truncated at hops".into());
    }
    let mut hops = Vec::with_capacity(hop_count);
    for i in 0..hop_count {
        let off = 18 + i * 32;
        let mut id = [0u8; 32];
        id.copy_from_slice(&data[off..off + 32]);
        hops.push(HashId(id));
    }
    Ok((circuit_id, hops))
}

pub fn encode_extend(circuit_id: &CircuitId, next_hop: &HashId) -> Vec<u8> {
    let mut buf = Vec::with_capacity(1 + 16 + 32);
    buf.push(PKT_CIRCUIT_EXTEND);
    buf.extend_from_slice(&circuit_id.0);
    buf.extend_from_slice(&next_hop.0);
    buf
}

/// 🆕 Hardening Step 7: EXTEND с ephemeral X25519 pubkey'ем инициатора (32B).
/// Когда caps_bits & TELESCOPING_HANDSHAKE — hop ожидает этот вариант и отвечает
/// `PKT_CIRCUIT_EXTEND_REPLY` со своим pubkey'ем. Старый decode_extend остаётся
/// для backward-compat: если payload длиннее — это v2 с pubkey'ем.
pub fn encode_extend_v2(circuit_id: &CircuitId, next_hop: &HashId, initiator_x25519: &[u8; 32]) -> Vec<u8> {
    let mut buf = Vec::with_capacity(1 + 16 + 32 + 32);
    buf.push(PKT_CIRCUIT_EXTEND);
    buf.extend_from_slice(&circuit_id.0);
    buf.extend_from_slice(&next_hop.0);
    buf.extend_from_slice(initiator_x25519);
    buf
}

/// v2-aware decode: возвращает (circuit_id, next_hop, optional initiator_x25519).
pub fn decode_extend_v2(data: &[u8]) -> Result<(CircuitId, HashId, Option<[u8; 32]>), String> {
    if data.len() < 1 + 16 + 32 {
        return Err("EXTEND too short".into());
    }
    if data[0] != PKT_CIRCUIT_EXTEND {
        return Err(format!("EXTEND bad magic: 0x{:02x}", data[0]));
    }
    let circuit_id = CircuitId::from_bytes(&data[1..17]).ok_or("bad circuit_id")?;
    let mut next = [0u8; 32];
    next.copy_from_slice(&data[17..49]);
    let pub_opt = if data.len() >= 1 + 16 + 32 + 32 {
        let mut pk = [0u8; 32];
        pk.copy_from_slice(&data[49..81]);
        Some(pk)
    } else {
        None
    };
    Ok((circuit_id, HashId(next), pub_opt))
}

/// 🆕 Hardening Step 7: encode EXTEND_REPLY = `[B4][cid:16][hop_x25519:32]`.
pub fn encode_extend_reply(circuit_id: &CircuitId, hop_x25519: &[u8; 32]) -> Vec<u8> {
    let mut buf = Vec::with_capacity(1 + 16 + 32);
    buf.push(PKT_CIRCUIT_EXTEND_REPLY);
    buf.extend_from_slice(&circuit_id.0);
    buf.extend_from_slice(hop_x25519);
    buf
}

pub fn decode_extend_reply(data: &[u8]) -> Result<(CircuitId, [u8; 32]), String> {
    if data.len() < 1 + 16 + 32 {
        return Err("EXTEND_REPLY too short".into());
    }
    if data[0] != PKT_CIRCUIT_EXTEND_REPLY {
        return Err(format!("EXTEND_REPLY bad magic: 0x{:02x}", data[0]));
    }
    let circuit_id = CircuitId::from_bytes(&data[1..17]).ok_or("bad circuit_id")?;
    let mut hop = [0u8; 32];
    hop.copy_from_slice(&data[17..49]);
    Ok((circuit_id, hop))
}

pub fn decode_extend(data: &[u8]) -> Result<(CircuitId, HashId), String> {
    if data.len() < 1 + 16 + 32 {
        return Err("EXTEND too short".into());
    }
    if data[0] != PKT_CIRCUIT_EXTEND {
        return Err(format!("EXTEND bad magic: 0x{:02x}", data[0]));
    }
    let circuit_id = CircuitId::from_bytes(&data[1..17]).ok_or("bad circuit_id")?;
    let mut next = [0u8; 32];
    next.copy_from_slice(&data[17..49]);
    Ok((circuit_id, HashId(next)))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CircuitDirection {
    Forward = 0,
    Backward = 1,
}

impl CircuitDirection {
    pub fn from_byte(b: u8) -> Option<Self> {
        match b {
            0 => Some(Self::Forward),
            1 => Some(Self::Backward),
            _ => None,
        }
    }
}

pub fn encode_data(circuit_id: &CircuitId, dir: CircuitDirection, payload: &[u8]) -> Vec<u8> {
    let plen = payload.len().min(u16::MAX as usize);
    let mut buf = Vec::with_capacity(1 + 16 + 1 + 2 + plen);
    buf.push(PKT_CIRCUIT_DATA);
    buf.extend_from_slice(&circuit_id.0);
    buf.push(dir as u8);
    buf.extend_from_slice(&(plen as u16).to_be_bytes());
    buf.extend_from_slice(&payload[..plen]);
    buf
}

pub fn decode_data(data: &[u8]) -> Result<(CircuitId, CircuitDirection, Vec<u8>), String> {
    if data.len() < 1 + 16 + 1 + 2 {
        return Err("DATA too short".into());
    }
    if data[0] != PKT_CIRCUIT_DATA {
        return Err(format!("DATA bad magic: 0x{:02x}", data[0]));
    }
    let circuit_id = CircuitId::from_bytes(&data[1..17]).ok_or("bad circuit_id")?;
    let dir = CircuitDirection::from_byte(data[17]).ok_or("bad direction")?;
    let plen = u16::from_be_bytes([data[18], data[19]]) as usize;
    if data.len() < 20 + plen {
        return Err("DATA truncated payload".into());
    }
    let payload = data[20..20 + plen].to_vec();
    Ok((circuit_id, dir, payload))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CloseReason {
    Normal = 0,
    Timeout = 1,
    HopUnreachable = 2,
    Protocol = 3,
}

impl CloseReason {
    pub fn from_byte(b: u8) -> Self {
        match b {
            0 => Self::Normal,
            1 => Self::Timeout,
            2 => Self::HopUnreachable,
            _ => Self::Protocol,
        }
    }
}

pub fn encode_close(circuit_id: &CircuitId, reason: CloseReason) -> Vec<u8> {
    let mut buf = Vec::with_capacity(1 + 16 + 1);
    buf.push(PKT_CIRCUIT_CLOSE);
    buf.extend_from_slice(&circuit_id.0);
    buf.push(reason as u8);
    buf
}

pub fn decode_close(data: &[u8]) -> Result<(CircuitId, CloseReason), String> {
    if data.len() < 1 + 16 + 1 {
        return Err("CLOSE too short".into());
    }
    if data[0] != PKT_CIRCUIT_CLOSE {
        return Err(format!("CLOSE bad magic: 0x{:02x}", data[0]));
    }
    let circuit_id = CircuitId::from_bytes(&data[1..17]).ok_or("bad circuit_id")?;
    let reason = CloseReason::from_byte(data[17]);
    Ok((circuit_id, reason))
}

/// Что верхний уровень должен сделать после `process_circuit_packet`.
#[derive(Debug, Clone)]
pub enum CircuitAction {
    /// Circuit зарегистрирован/обновлён, никаких отправок не нужно.
    Established,
    /// Closed (получен 0xB3 на терминальной ноде).
    Closed,
    /// Передать `packet` peer'у `target` (encrypt + send_encrypted).
    Forward { target: HashId, packet: Vec<u8> },
    /// Доставить payload приложению (мы — endpoint в этом направлении).
    Deliver { payload: Vec<u8>, dir: CircuitDirection },
}

// --------------- Manager ---------------

/// Реестр circuit'ов на ноде. Один и тот же объект используют:
/// - инициатор (хранит свои собственные circuit'ы),
/// - middle-hop'ы (хранят upstream/downstream pair'ы),
/// - exit-нода (хранит circuit'ы где она последний hop).
pub struct CircuitManager {
    inner: Arc<Mutex<HashMap<CircuitId, Circuit>>>,
}

impl Default for CircuitManager {
    fn default() -> Self {
        Self::new()
    }
}

impl CircuitManager {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub async fn insert(&self, c: Circuit) {
        self.inner.lock().await.insert(c.id, c);
    }

    pub async fn get(&self, id: &CircuitId) -> Option<Circuit> {
        self.inner.lock().await.get(id).cloned()
    }

    pub async fn remove(&self, id: &CircuitId) -> Option<Circuit> {
        self.inner.lock().await.remove(id)
    }

    /// Возвращает количество circuit'ов в реестре.
    pub async fn len(&self) -> usize {
        self.inner.lock().await.len()
    }

    pub async fn is_empty(&self) -> bool {
        self.inner.lock().await.is_empty()
    }

    /// Очистить TTL-протухшие circuit'ы. Возвращает число удалённых.
    pub async fn gc_expired(&self) -> usize {
        let mut g = self.inner.lock().await;
        let before = g.len();
        g.retain(|_, c| !c.is_expired());
        before - g.len()
    }

    pub async fn set_state(&self, id: &CircuitId, state: CircuitState) {
        if let Some(c) = self.inner.lock().await.get_mut(id) {
            c.state = state;
        }
    }
}

// --------------- Tests ---------------

#[cfg(test)]
mod tests {
    use super::*;

    fn id32(seed: u8) -> HashId {
        HashId([seed; 32])
    }

    #[test]
    fn circuit_id_zero_constant() {
        // Hardening Step 4: CircuitId::zero() — заглушка для случаев когда delivery channel
        // не знает конкретный cid (нынешний handle_circuit_action::Deliver не несёт его).
        let z = CircuitId::zero();
        assert_eq!(z.0, [0u8; 16]);
        // random должен отличаться (с overwhelming вероятностью)
        let r = CircuitId::random();
        assert_ne!(r.0, [0u8; 16]);
    }

    #[test]
    fn telescoping_extend_v2_roundtrip_with_pubkey() {
        // Hardening Step 7: EXTEND v2 содержит ephemeral X25519 pubkey инициатора.
        let cid = CircuitId([0xABu8; 16]);
        let next = id32(0xCD);
        let pubkey = [0x77u8; 32];
        let bytes = encode_extend_v2(&cid, &next, &pubkey);
        let (cid2, next2, pk) = decode_extend_v2(&bytes).unwrap();
        assert_eq!(cid2, cid);
        assert_eq!(next2, next);
        assert_eq!(pk, Some(pubkey));
    }

    #[test]
    fn telescoping_extend_v2_backward_compat() {
        // V1 EXTEND без pubkey'а — decoder возвращает None для опционального поля.
        let cid = CircuitId([0xEFu8; 16]);
        let next = id32(0x12);
        let v1 = encode_extend(&cid, &next);
        let (cid2, next2, pk) = decode_extend_v2(&v1).unwrap();
        assert_eq!(cid2, cid);
        assert_eq!(next2, next);
        assert_eq!(pk, None);
    }

    #[test]
    fn telescoping_extend_reply_roundtrip() {
        let cid = CircuitId([0x10u8; 16]);
        let hop_pub = [0x42u8; 32];
        let bytes = encode_extend_reply(&cid, &hop_pub);
        let (cid2, hp) = decode_extend_reply(&bytes).unwrap();
        assert_eq!(cid2, cid);
        assert_eq!(hp, hop_pub);
    }

    #[test]
    fn telescoping_hkdf_derive_stable_and_unique() {
        // derive_hop_key_ecdh должен быть стабилен и различаться по hop_idx / circuit.
        let shared = [0x33u8; 32];
        let cid1 = CircuitId([1u8; 16]);
        let cid2 = CircuitId([2u8; 16]);
        let k_a = derive_hop_key_ecdh(&shared, &cid1, 0);
        let k_b = derive_hop_key_ecdh(&shared, &cid1, 1);
        let k_c = derive_hop_key_ecdh(&shared, &cid2, 0);
        assert_ne!(k_a, k_b);
        assert_ne!(k_a, k_c);
        // Стабильность
        assert_eq!(k_a, derive_hop_key_ecdh(&shared, &cid1, 0));
    }

    #[test]
    fn telescoping_caps_bit_is_0x0800() {
        // Sanity: TELESCOPING_HANDSHAKE = 0x0800, не конфликтует с другими.
        use crate::netlayer::packet::hello_caps;
        assert_eq!(hello_caps::TELESCOPING_HANDSHAKE, 0x0800);
        // не пересекается с другими известными битами
        let known = hello_caps::SUPERBOOT | hello_caps::RELAY | hello_caps::TUNNEL
            | hello_caps::DHT | hello_caps::GATEWAY | hello_caps::NAT_TRAVERSAL
            | hello_caps::MESH | hello_caps::ENCRYPTED | hello_caps::MOBILE
            | hello_caps::ANCHOR | hello_caps::INTRODUCER;
        assert_eq!(known & hello_caps::TELESCOPING_HANDSHAKE, 0);
    }

    #[test]
    fn circuit_initiator_hop_ordering() {
        let c = Circuit::new_initiator(vec![id32(0xAA), id32(0xBB), id32(0xCC)], 600);
        assert_eq!(c.hops.len(), 3);
        assert_eq!(c.hops[0].peer_id, id32(0xAA));
        assert_eq!(c.hops[2].peer_id, id32(0xCC));
        assert_eq!(c.last_hop().unwrap().peer_id, id32(0xCC));
        assert!(matches!(c.state, CircuitState::New));
    }

    #[test]
    fn derived_keys_are_unique_per_hop_and_circuit() {
        let cid = CircuitId([1u8; 16]);
        let cid2 = CircuitId([2u8; 16]);
        let p = id32(0x10);
        let k1 = derive_hop_key(&cid, 0, &p);
        let k2 = derive_hop_key(&cid, 1, &p);
        let k3 = derive_hop_key(&cid2, 0, &p);
        assert_ne!(k1, k2);
        assert_ne!(k1, k3);
        // Стабильность: тот же вход → тот же ключ.
        assert_eq!(k1, derive_hop_key(&cid, 0, &p));
    }

    #[test]
    fn build_packet_roundtrip() {
        let c = Circuit::new_initiator(vec![id32(0x01), id32(0x02), id32(0x03), id32(0x04)], 600);
        let bytes = encode_build(&c);
        let (cid, hops) = decode_build(&bytes).unwrap();
        assert_eq!(cid, c.id);
        assert_eq!(hops.len(), 4);
        assert_eq!(hops[0], id32(0x01));
        assert_eq!(hops[3], id32(0x04));
    }

    #[test]
    fn extend_packet_roundtrip() {
        let cid = CircuitId([0xAB; 16]);
        let next = id32(0x77);
        let bytes = encode_extend(&cid, &next);
        let (cid2, n2) = decode_extend(&bytes).unwrap();
        assert_eq!(cid, cid2);
        assert_eq!(n2, next);
    }

    #[test]
    fn data_packet_roundtrip_both_directions() {
        let cid = CircuitId([0xCD; 16]);
        let payload = b"hello-circuit".to_vec();
        for dir in [CircuitDirection::Forward, CircuitDirection::Backward] {
            let bytes = encode_data(&cid, dir, &payload);
            let (cid2, dir2, p2) = decode_data(&bytes).unwrap();
            assert_eq!(cid, cid2);
            assert_eq!(dir, dir2);
            assert_eq!(p2, payload);
        }
    }

    #[test]
    fn close_packet_roundtrip() {
        let cid = CircuitId([0xEE; 16]);
        let bytes = encode_close(&cid, CloseReason::Timeout);
        let (cid2, reason) = decode_close(&bytes).unwrap();
        assert_eq!(cid, cid2);
        assert_eq!(reason, CloseReason::Timeout);
    }

    #[test]
    fn malformed_packets_rejected() {
        assert!(decode_build(&[0xB0, 0]).is_err());
        assert!(decode_extend(&[0xB1, 1, 2, 3]).is_err());
        // bad magic byte
        assert!(decode_data(&[0x00; 32]).is_err());
        // direction OOR
        let mut bad = vec![PKT_CIRCUIT_DATA];
        bad.extend_from_slice(&[0u8; 16]);
        bad.push(0xFF); // direction
        bad.extend_from_slice(&[0u8, 0u8]); // len 0
        assert!(decode_data(&bad).is_err());
    }

    #[tokio::test]
    async fn manager_insert_get_remove() {
        let mgr = CircuitManager::new();
        let c = Circuit::new_initiator(vec![id32(0x10), id32(0x20)], 600);
        let id = c.id;
        mgr.insert(c).await;
        assert_eq!(mgr.len().await, 1);
        let got = mgr.get(&id).await.unwrap();
        assert_eq!(got.hops.len(), 2);
        let removed = mgr.remove(&id).await.unwrap();
        assert_eq!(removed.id, id);
        assert!(mgr.is_empty().await);
    }

    #[tokio::test]
    async fn manager_gc_expired() {
        let mgr = CircuitManager::new();
        let mut c = Circuit::new_initiator(vec![id32(0xAA)], 1);
        c.created_at = c.created_at.saturating_sub(60); // протухший
        mgr.insert(c).await;
        let cleared = mgr.gc_expired().await;
        assert_eq!(cleared, 1);
        assert!(mgr.is_empty().await);
    }
}
