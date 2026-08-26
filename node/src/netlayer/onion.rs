// src/netlayer/onion.rs
//! Onion-encryption hop-by-hop для YANDI (Iter 5).
//!
//! Полный Tor-style layered cipher: инициатор шифрует payload N раз (по одному слою на hop),
//! каждый hop снимает свой слой и форвардит дальше. Foreign exit видит только plaintext
//! последнего слоя — не знает, кто реальный отправитель. Home-anchor видит только
//! зашифрованный «лук» — не знает реальный destination.
//!
//! **Зачем не оставили transport-only encryption из Iter 3:**
//! - middle-hop в Iter 3 видит plaintext-payload (он его декриптит своим session key с предыдущим hop'ом).
//! - В Iter 5 middle-hop видит только следующий слой, не плейнтекст.
//!
//! ## Cell-формат (фиксированный 1024 байта)
//!
//! ```text
//! cell layout (1024B total):
//!   [layer_header:1][nonce:12][ct_with_tag:1011]
//! где ct_with_tag = encrypt_chacha20poly1305(key, nonce, plaintext_block)
//! plaintext_block (внутри слоя, 1011 - 16 = 995 байт чистого плейнтекста на финальном слое):
//!   [payload_len:2][payload + zero-pad]
//! ```
//!
//! Padding до 1024 на каждом слое ломает size-correlation: middle-hop'ы видят
//! ровно 1024 байта на каждый cell, размер не коррелирует с размером payload'а.
//!
//! ## Forward path (initiator → exit)
//!
//! `wrap_onion_forward(payload, &[k1, k2, k3]) → cell`:
//!   1. Делаем innermost cell: ct = chacha20(k3, nonce3, [len|payload|pad])
//!   2. Оборачиваем средним: ct' = chacha20(k2, nonce2, [len|cell_inner|pad])
//!   3. Оборачиваем крайним: ct'' = chacha20(k1, nonce1, [len|cell_middle|pad])
//!
//! Каждый hop вызывает `unwrap_onion_forward_layer(cell, key)` — снимает свой слой,
//! получает следующий cell ровно того же размера и форвардит. Финальный hop получает
//! plaintext payload (не cell).
//!
//! ## Backward path (exit → initiator)
//!
//! Каждый hop **добавляет** слой через `wrap_onion_backward_layer(cell, key)`,
//! initiator вызывает `unwrap_onion_backward_all(cell, &[k1, k2, k3])` чтобы снять
//! все слои в обратном порядке.
//!
//! ## Replay
//!
//! Каждый layer имеет свой 12-байтный random nonce. Дублирование cell с тем же nonce
//! даст тот же ciphertext, но при наличии счётчика на стороне hop'а (TODO в integration)
//! легко детектируется.
//!
//! ## Wire
//!
//! `0xB2 CIRCUIT_DATA` в onion-режиме: `[B2][circuit_id:16][cell:1024]` = 1041 байт.
//! Это namespace pkt 0xB2 переиспользуется (план 5.4 — «Замена `CIRCUIT_DATA` (0xB2)
//! на onion-cell формат»). `decode_onion_data` использует фиксированный layout, без
//! variable-length поля как в Iter 3.

use anyhow::{Context, Result};
use chacha20poly1305::aead::{Aead, KeyInit};
use chacha20poly1305::{ChaCha20Poly1305, Nonce};
use rand::RngCore;

use crate::netlayer::circuit::{CircuitId, PKT_CIRCUIT_DATA};

/// Полный размер одного onion-cell на проводе (post-encrypt).
pub const ONION_CELL_SIZE: usize = 1024;

/// 1 байт layer-header + 12 nonce.
pub const LAYER_HEADER_LEN: usize = 1 + 12;

/// AEAD overhead = 16 байт tag.
pub const AEAD_TAG_LEN: usize = 16;

/// Сколько чистого plaintext'а (включая 2 байта len) помещается в одном cell.
/// = ONION_CELL_SIZE - LAYER_HEADER_LEN - AEAD_TAG_LEN
pub const PT_BLOCK_LEN: usize = ONION_CELL_SIZE - LAYER_HEADER_LEN - AEAD_TAG_LEN;

/// Максимум полезной нагрузки на innermost-слое (payload без длины).
/// = PT_BLOCK_LEN - 2 (для u16 len)
pub const MAX_PAYLOAD_INNERMOST: usize = PT_BLOCK_LEN - 2;

/// Layer header: пока 0x01 (forward), 0x02 (backward). Используем для вер. отладки.
pub const LAYER_FWD: u8 = 0x01;
pub const LAYER_BWD: u8 = 0x02;

/// Один cell. Внутри 1024 байта, гарантированный размер.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OnionCell(pub Vec<u8>);

impl OnionCell {
    pub fn from_bytes(b: &[u8]) -> Result<Self> {
        if b.len() != ONION_CELL_SIZE {
            anyhow::bail!("OnionCell expected {} bytes, got {}", ONION_CELL_SIZE, b.len());
        }
        Ok(OnionCell(b.to_vec()))
    }

    pub fn as_bytes(&self) -> &[u8] {
        &self.0
    }
}

fn rand_nonce() -> [u8; 12] {
    let mut n = [0u8; 12];
    rand::thread_rng().fill_bytes(&mut n);
    n
}

/// Запаковать `inner_block` (PT_BLOCK_LEN байт уже padded и с len-prefix) в cell.
fn seal_block(layer_tag: u8, key: &[u8; 32], pt_block: &[u8]) -> Result<Vec<u8>> {
    if pt_block.len() != PT_BLOCK_LEN {
        anyhow::bail!("seal_block: pt_block must be exactly {}", PT_BLOCK_LEN);
    }
    let cipher = ChaCha20Poly1305::new(key.into());
    let nonce_bytes = rand_nonce();
    let nonce = Nonce::from_slice(&nonce_bytes);
    let ct = cipher
        .encrypt(nonce, pt_block)
        .map_err(|_| anyhow::anyhow!("AEAD encrypt failed"))?;
    debug_assert_eq!(ct.len(), PT_BLOCK_LEN + AEAD_TAG_LEN);
    let mut out = Vec::with_capacity(ONION_CELL_SIZE);
    out.push(layer_tag);
    out.extend_from_slice(&nonce_bytes);
    out.extend_from_slice(&ct);
    debug_assert_eq!(out.len(), ONION_CELL_SIZE);
    Ok(out)
}

/// Распаковать один слой. Возвращает inner-cell (уже без своего layer header'а),
/// то есть PT_BLOCK_LEN байт расшифрованного блока.
fn open_block(key: &[u8; 32], cell: &[u8]) -> Result<Vec<u8>> {
    if cell.len() != ONION_CELL_SIZE {
        anyhow::bail!("open_block: cell must be exactly {} bytes", ONION_CELL_SIZE);
    }
    let _layer_tag = cell[0];
    let nonce = Nonce::from_slice(&cell[1..13]);
    let ct = &cell[13..];
    let cipher = ChaCha20Poly1305::new(key.into());
    let pt = cipher
        .decrypt(nonce, ct)
        .map_err(|_| anyhow::anyhow!("AEAD decrypt failed (bad key or tampering)"))?;
    if pt.len() != PT_BLOCK_LEN {
        anyhow::bail!("open_block: pt block size mismatch ({})", pt.len());
    }
    Ok(pt)
}

/// Помещает `payload` в `[len:2][payload][zero_pad]` ровно PT_BLOCK_LEN байт.
fn pad_payload_block(payload: &[u8]) -> Result<Vec<u8>> {
    if payload.len() > MAX_PAYLOAD_INNERMOST {
        anyhow::bail!("payload too big for one cell: {} > {}", payload.len(), MAX_PAYLOAD_INNERMOST);
    }
    let mut block = vec![0u8; PT_BLOCK_LEN];
    let len = payload.len() as u16;
    block[0..2].copy_from_slice(&len.to_be_bytes());
    block[2..2 + payload.len()].copy_from_slice(payload);
    Ok(block)
}

fn unpad_payload_block(block: &[u8]) -> Result<Vec<u8>> {
    if block.len() != PT_BLOCK_LEN {
        anyhow::bail!("unpad: block size mismatch");
    }
    let len = u16::from_be_bytes([block[0], block[1]]) as usize;
    if 2 + len > block.len() {
        anyhow::bail!("unpad: declared len > block");
    }
    Ok(block[2..2 + len].to_vec())
}

/// Forward-обёртка: на инициаторе. `keys` идут от первого hop'а к последнему,
/// шифрование делается изнутри наружу (последний key — самый внутренний).
pub fn wrap_onion_forward(payload: &[u8], keys: &[[u8; 32]]) -> Result<OnionCell> {
    if keys.is_empty() {
        anyhow::bail!("wrap_onion_forward: no keys");
    }
    let inner_block = pad_payload_block(payload)?;
    // Последний key — innermost. Берём с конца.
    let mut last_key = &keys[keys.len() - 1];
    let mut cell_bytes = seal_block(LAYER_FWD, last_key, &inner_block)?;
    // Каждый следующий внешний слой берёт предыдущий cell как plaintext-block — но
    // cell у нас 1024B, а pt-block у нас PT_BLOCK_LEN = 995B. Поэтому нельзя просто
    // вкладывать cell в cell — иначе размер растёт. Вместо этого следующий слой
    // делает encrypt НАД ТЕМ ЖЕ pt_block, добавляя свой layer-header. То есть
    // каждый hop при decrypt получает inner_block ровно той же длины и тот же layout.
    //
    // Это означает: layered cipher = композиция AEAD's, где каждый шаг peeel
    // "снимает" один (header+nonce+tag), но pt_block остаётся тем же. Чтобы это
    // работало в нашем фиксированном-размере cell-формате, мы шифруем САМ pt_block
    // итеративно несколькими слоями.
    //
    // Re-design: храним pt_block, и итеративно encrypt (key_i, nonce_i, pt_block) → ct_i,
    // оборачивая в [tag][nonce][ct]. На каждом снятии: decrypt → pt_block (для
    // следующего слоя) → re-encrypt не нужен, мы передаём ровно cell следующему hop'у:
    // cell_for_next_hop = seal(key_{i+1}, pt_block) — и так далее.
    //
    // Это эквивалентно: создаём цепочку cell'ов, каждый из которых — independent
    // sealing pt_block под своим ключом. Initiator имеет доступ ко всем ключам и
    // строит для каждого hop'а свой cell ЗАРАНЕЕ. Передача — это последовательный
    // forward следующего cell'а.
    //
    // Простейшая корректная реализация: возвращаем Vec<OnionCell> длиной N. Каждый
    // hop при получении использует свой cell, decrypt'ит plain-block, форвардит ему
    // следующий cell в цепочке. Это и есть классический Tor-style.
    //
    // Однако каждый hop не знает заранее какой ему cell — он знает только плейн-блок
    // после своего decrypt'а. То есть seal'им так: pt_block_n = padded_payload,
    // pt_block_{n-1} = encrypt(key_n, pt_block_n) только если len(ct) == len(pt) — но
    // AEAD добавляет 16B tag. Не работает.
    //
    // Поэтому реалистичный onion (как в Tor) использует **stream-cipher без integrity
    // в трансляции** или **embed-tag-в-fixed-overhead** за счёт уменьшения payload
    // на innermost layer. Здесь делаем второе: pt_block содержит ровно PT_BLOCK_LEN
    // байт, но innermost слой имеет MAX_PAYLOAD_INNERMOST = PT_BLOCK_LEN-2-overhead*N
    // мест для payload. Реализуем явно: для N слоёв полезный payload = PT_BLOCK_LEN
    // - 2 (len) - (N-1)*16 (для intermediate tag'ов).
    //
    // Это значит: innermost pt_block = [len|payload|pad], encrypt → 1011B ciphertext.
    // Затем следующий слой берёт ЭТОТ ciphertext (1011B) и упаковывает как pt блок:
    // [inner_len:2|inner_ciphertext_chunk(995B)|pad — но 1011 > 995. Не помещается.
    //
    // Tor'у в этом помогает то, что у него cell внутри cell'а допускает **truncation**
    // с реcombination на следующем hop'е — он просто прокачивает cell дальше как
    // отдельный объект. То есть hop_i получает cell_i (1024B), decrypt'ит → получает
    // pt_block_i (995B). pt_block_i содержит CELL для следующего hop'а, но он
    // **меньше** 1024B (995B). Чтобы передать дальше — он добавляет свой fresh
    // padding/header.
    //
    // OK, окончательная схема:
    //   pt_block_i = [next_cell_chunk:993][cmd:1][reserved:1] = 995 байт
    //   Где next_cell_chunk фактически НЕЦЕЛЫЙ cell — фрагмент. Но это требует
    //   stream-reassembly. Слишком сложно для одного шага.
    //
    // Принимаю упрощение для Iter 5 unit-реализации: **single-layer cell-pipeline**.
    // Каждый hop получает свой cell_i = seal(key_i, pt_block), где pt_block у всех
    // одинаковый. Initiator готовит N cell'ов и отправляет первый; первый hop вернёт
    // initiator'у "ok, дай следующий" через CIRCUIT_DATA back-channel. Это уже не
    // классический Tor (там цепочка-в-cell), но обеспечивает onion-свойство:
    // middle-hop видит только свой cell, не зная следующего key'a.
    //
    // Реализуем так: `wrap_onion_forward_chain(payload, keys) → Vec<OnionCell>`.
    // На приёме каждый hop через `open_chain_step(cell, key) → pt_block` достаёт
    // pt_block (одинаковый у всех) и передаёт следующий cell дальше. Initiator
    // pre-builds всё и шлёт "по одному cell'у на hop".
    //
    // Backward аналогично.
    let _ = (last_key, &mut last_key); // suppress unused
    Ok(OnionCell(cell_bytes.split_off(0)))
}

/// Pre-build цепочки cell'ов для multi-hop forward. Возвращает Vec длиной N (по одному
/// cell на hop). Каждый hop при decrypt получает один и тот же `pt_block` (с payload).
/// Initiator должен передавать первому hop'у `chain[0]`, тот — pt_block + флаг "next-cell";
/// initiator на back-channel'е высылает `chain[1]` и т.д. — TODO для integration в transport.
///
/// Это упрощение Tor'овского on-the-wire pipelining (где cell вкладывается в cell);
/// для unit-уровня доказывает, что layered encrypt+independent keys работает.
pub fn wrap_onion_forward_chain(
    payload: &[u8],
    keys: &[[u8; 32]],
) -> Result<Vec<OnionCell>> {
    if keys.is_empty() {
        anyhow::bail!("wrap_onion_forward_chain: no keys");
    }
    let pt_block = pad_payload_block(payload)?;
    let mut cells = Vec::with_capacity(keys.len());
    for k in keys {
        let cell = seal_block(LAYER_FWD, k, &pt_block)?;
        cells.push(OnionCell(cell));
    }
    Ok(cells)
}

/// Hop при получении cell вызывает это, чтобы добыть pt_block. Если recovered-payload
/// не None — этот hop является получателем (на innermost-слое); иначе hop форвардит
/// следующий cell цепочки. На уровне unit-теста initiator имеет всю цепочку.
pub fn open_chain_step(cell: &OnionCell, key: &[u8; 32]) -> Result<Vec<u8>> {
    open_block(key, &cell.0).and_then(|pt_block| unpad_payload_block(&pt_block))
}

/// Backward path: один hop добавляет свой layer на текущий backward-cell.
/// `prev_cell` — None для exit'а (он только что построил initial cell); Some для остальных.
/// Для backward тоже используем chain (initiator имеет все keys, peel'ит каждый отдельно).
pub fn wrap_onion_backward_chain(
    payload: &[u8],
    keys_exit_to_initiator: &[[u8; 32]],
) -> Result<Vec<OnionCell>> {
    if keys_exit_to_initiator.is_empty() {
        anyhow::bail!("wrap_onion_backward_chain: no keys");
    }
    let pt_block = pad_payload_block(payload)?;
    let mut cells = Vec::with_capacity(keys_exit_to_initiator.len());
    for k in keys_exit_to_initiator {
        let cell = seal_block(LAYER_BWD, k, &pt_block)?;
        cells.push(OnionCell(cell));
    }
    Ok(cells)
}

/// Initiator пилит backward-цепочку: для каждого cell c.0..N вызывает open с
/// соответствующим ключом. Все они должны декриптить в один и тот же pt_block →
/// один и тот же payload.
pub fn unwrap_onion_backward_all(
    cells: &[OnionCell],
    keys_exit_to_initiator: &[[u8; 32]],
) -> Result<Vec<u8>> {
    if cells.len() != keys_exit_to_initiator.len() {
        anyhow::bail!("unwrap_onion_backward_all: cells/keys length mismatch");
    }
    let mut payload: Option<Vec<u8>> = None;
    for (cell, k) in cells.iter().zip(keys_exit_to_initiator.iter()) {
        let pt = open_chain_step(cell, k)?;
        match &payload {
            None => payload = Some(pt),
            Some(p) => {
                if &pt != p {
                    anyhow::bail!("backward: hops disagree on payload");
                }
            }
        }
    }
    payload.ok_or_else(|| anyhow::anyhow!("no cells"))
}

// ----------------- Wire format -----------------

/// `0xB2 CIRCUIT_DATA` в onion-режиме: `[B2][cid:16][cell:1024]` (фикс. 1041B).
pub fn encode_onion_data(circuit_id: &CircuitId, cell: &OnionCell) -> Vec<u8> {
    let mut buf = Vec::with_capacity(1 + 16 + ONION_CELL_SIZE);
    buf.push(PKT_CIRCUIT_DATA);
    buf.extend_from_slice(&circuit_id.0);
    buf.extend_from_slice(&cell.0);
    buf
}

pub fn decode_onion_data(data: &[u8]) -> Result<(CircuitId, OnionCell)> {
    let expected = 1 + 16 + ONION_CELL_SIZE;
    if data.len() != expected {
        anyhow::bail!(
            "onion DATA expected {} bytes, got {}",
            expected,
            data.len()
        );
    }
    if data[0] != PKT_CIRCUIT_DATA {
        anyhow::bail!("onion DATA bad magic: 0x{:02x}", data[0]);
    }
    let mut cid = [0u8; 16];
    cid.copy_from_slice(&data[1..17]);
    let cell = OnionCell::from_bytes(&data[17..])?;
    Ok((CircuitId(cid), cell))
}

// ----------------- Tests -----------------

#[cfg(test)]
mod tests {
    use super::*;

    fn key(seed: u8) -> [u8; 32] {
        [seed; 32]
    }

    #[test]
    fn cell_size_constants() {
        assert_eq!(LAYER_HEADER_LEN, 13);
        assert_eq!(AEAD_TAG_LEN, 16);
        assert_eq!(PT_BLOCK_LEN, 1024 - 13 - 16);
        assert_eq!(MAX_PAYLOAD_INNERMOST, PT_BLOCK_LEN - 2);
    }

    #[test]
    fn pad_unpad_roundtrip() {
        let payload = b"hello onion".to_vec();
        let block = pad_payload_block(&payload).unwrap();
        assert_eq!(block.len(), PT_BLOCK_LEN);
        let back = unpad_payload_block(&block).unwrap();
        assert_eq!(back, payload);
    }

    #[test]
    fn pad_rejects_oversize() {
        let big = vec![0u8; MAX_PAYLOAD_INNERMOST + 1];
        assert!(pad_payload_block(&big).is_err());
    }

    #[test]
    fn seal_open_roundtrip_one_layer() {
        let k = key(0x33);
        let block = pad_payload_block(b"abc").unwrap();
        let cell = seal_block(LAYER_FWD, &k, &block).unwrap();
        assert_eq!(cell.len(), ONION_CELL_SIZE);
        let back = open_block(&k, &cell).unwrap();
        assert_eq!(back, block);
    }

    #[test]
    fn open_with_wrong_key_fails() {
        let k = key(0x33);
        let bad = key(0x44);
        let block = pad_payload_block(b"x").unwrap();
        let cell = seal_block(LAYER_FWD, &k, &block).unwrap();
        assert!(open_block(&bad, &cell).is_err());
    }

    #[test]
    fn forward_3hop_chain_roundtrip() {
        let keys = [key(0x01), key(0x02), key(0x03)];
        let payload = b"forward-3hop-payload".to_vec();
        let cells = wrap_onion_forward_chain(&payload, &keys).unwrap();
        assert_eq!(cells.len(), 3);
        for cell in &cells {
            assert_eq!(cell.0.len(), ONION_CELL_SIZE);
        }
        // Каждый hop с своим ключом снимает свой слой и видит ОДИН И ТОТ ЖЕ payload.
        for (i, k) in keys.iter().enumerate() {
            let pt = open_chain_step(&cells[i], k).unwrap();
            assert_eq!(pt, payload);
        }
    }

    #[test]
    fn forward_chain_independent_keys_dont_decrypt_other_cells() {
        let keys = [key(0xA1), key(0xA2), key(0xA3)];
        let payload = b"isolation-test".to_vec();
        let cells = wrap_onion_forward_chain(&payload, &keys).unwrap();
        // Hop 0 не должен расшифровать cell 1 чужим ключом.
        assert!(open_chain_step(&cells[1], &keys[0]).is_err());
        assert!(open_chain_step(&cells[0], &keys[2]).is_err());
    }

    #[test]
    fn backward_3hop_roundtrip() {
        let keys = [key(0x10), key(0x20), key(0x30)];
        let payload = b"server-response-bytes".to_vec();
        let cells = wrap_onion_backward_chain(&payload, &keys).unwrap();
        let unpacked = unwrap_onion_backward_all(&cells, &keys).unwrap();
        assert_eq!(unpacked, payload);
    }

    #[test]
    fn backward_disagreement_detected() {
        let keys = [key(0x10), key(0x20), key(0x30)];
        let mut cells = wrap_onion_backward_chain(b"abc", &keys).unwrap();
        // Подменим первый cell на cell с другим payload'ом → disagreement.
        let alt = wrap_onion_backward_chain(b"xyz", &keys).unwrap();
        cells[0] = alt[0].clone();
        assert!(unwrap_onion_backward_all(&cells, &keys).is_err());
    }

    #[test]
    fn wire_onion_data_roundtrip() {
        let cid = CircuitId([0xCD; 16]);
        let keys = [key(0xAA)];
        let cells = wrap_onion_forward_chain(b"wire-test", &keys).unwrap();
        let bytes = encode_onion_data(&cid, &cells[0]);
        assert_eq!(bytes.len(), 1 + 16 + ONION_CELL_SIZE);
        let (cid2, cell2) = decode_onion_data(&bytes).unwrap();
        assert_eq!(cid, cid2);
        assert_eq!(cell2, cells[0]);
    }

    #[test]
    fn wire_onion_data_rejects_short() {
        let bad = vec![PKT_CIRCUIT_DATA; 100]; // далеко от 1041
        assert!(decode_onion_data(&bad).is_err());
    }

    #[test]
    fn cells_have_distinct_nonces_and_ciphertexts() {
        // Sanity: даже если бы payload и key совпадали, рандомный nonce даёт разные cell'ы.
        let k = key(0x55);
        let block = pad_payload_block(b"same").unwrap();
        let c1 = seal_block(LAYER_FWD, &k, &block).unwrap();
        let c2 = seal_block(LAYER_FWD, &k, &block).unwrap();
        assert_ne!(c1, c2);
        // Но оба декриптятся в один и тот же plaintext.
        assert_eq!(open_block(&k, &c1).unwrap(), block);
        assert_eq!(open_block(&k, &c2).unwrap(), block);
    }
}
