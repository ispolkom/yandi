# YANDI — Security Audit Plan

**Дата:** 2026-05-14  
**Статус проверки:** `SECURITY_AUDIT_STATUS.md`  
**Аудитор:** Claude Sonnet 4.6 (первый проход)

---

## Контекст угроз

YANDI — P2P-сеть для людей и AI-агентов. Модель угроз:
- **Сетевой атакующий** — перехватывает UDP-трафик между нодами
- **Вредоносная нода** — подключается к сети и притворяется легитимным пиром
- **Скомпрометированный пир** — легитимный узел захвачен; пытается имперсонировать других
- **AI-агент** — может стать стороной атаки или мишенью

---

## Итоги аудита — обзор

| ID | Уязвимость | Критичность | Файл |
|----|-----------|-------------|------|
| SEC-01 | P2P пакеты без шифрования в `send_packet_dual_path` | **CRITICAL** | `p2p/transport.rs:341` |
| SEC-02 | Sender в P2P пакете self-reported, без подписи | **CRITICAL** | `p2p/packet.rs`, `p2p/transport.rs` |
| SEC-03 | Hello handshake: x25519_public не подписан Ed25519 ключом | **CRITICAL** | `p2p/hello.rs` |
| SEC-04 | Приватные ключи хранятся в plaintext JSON | **CRITICAL** | `core/identity.rs:211-247` |
| SEC-05 | JS-инъекция в onclick через call_id / from_short_id | **HIGH** | `ui/voice-call.js:108,111` |
| SEC-06 | display_name из сети без sanitize в JS onclick | **HIGH** | `ui/voice-call.js`, `ui/video-call.js` |
| SEC-07 | Path traversal в `/api/files/content/{file_id}/{filename}` | **HIGH** | `web/server.rs` |
| SEC-08 | `sanitize_filename` обходится через unicode / double-dot | **HIGH** | `communication/file_transfer.rs:61` |
| SEC-09 | `static mut LAST_RECV` без синхронизации (UB) | **MEDIUM** | `p2p/transport.rs:394` |
| SEC-10 | Bootstrap адреса принимаются без верификации | **MEDIUM** | `netlayer/bootstrap.rs` |
| SEC-11 | Полный путь к приватному ключу логируется в stdout | **LOW** | `core/identity.rs:245,264` |
| SEC-12 | Checkpoint файлы файлопередачи — plaintext на диске | **LOW** | `communication/file_transfer.rs:23` |

---

## SEC-01: P2P пакеты не шифруются

### Суть
`send_packet_dual_path` отправляет пакет **как есть**. Флаг `encrypted: bool` в структуре пакета — декоративный, он меняет бит в заголовке, но сам payload не шифруется. ChatMessage, VoiceCallRequest, FileChunk — всё идёт в открытом виде по UDP.

`send_encrypted()` (строка 309) правильно шифрует через `p2p_encryption`, но **нигде не вызывается** для прикладных пакетов — только для ключевого обмена.

### Доказательство
```rust
// transport.rs:341 — send_packet_dual_path
let bytes0 = packet.to_bytes();  // payload plaintext
self.data_send_socket.send_to(&bytes0, &peer_addr).await?;  // raw UDP
```

### Исправление

**Вариант A (быстрый):** Внутри `send_packet_dual_path` после сборки `bytes` — шифровать AES-256-GCM сессионным ключом peer'а из `p2p_encryption`:
```rust
pub async fn send_packet_dual_path(&self, peer_id: HashId, mut packet: P2PPacket) -> Result<(), String> {
    let peer_addr = { ... }; // как сейчас
    
    // Шифровать payload если есть сессионный ключ
    let enc = self.p2p_encryption.lock().await;
    if let Some(session) = enc.get_session(&peer_id) {
        let encrypted_payload = session.encrypt(&packet.payload)?;
        packet.payload = encrypted_payload;
        packet.encrypted = true;
    }
    drop(enc);
    
    // ... отправка как сейчас
}
```

**Вариант B (правильный, требует рефакторинга):** Использовать `send_encrypted()` для всех прикладных пакетов. `send_encrypted` уже делает ECDH + AES-256-GCM через `EncryptionManager`.

**Приоритет:** реализовать Вариант A немедленно, затем Вариант B.

---

## SEC-02: Sender self-reported — нет аутентификации отправителя

### Суть
Поле `sender: HashId` в P2PPacket заполняется отправителем самостоятельно. Получатель доверяет этому значению и использует его для роутинга, хранения сообщений, идентификации звонящего. Любая нода может поставить чужой `node_id` в поле `sender` и притвориться другим пользователем.

```rust
// packet.rs — sender берётся из заголовка пакета без проверки
let sender = HashId(sender_bytes);  // self-reported
```

```rust
// main.rs:577 — sender из пакета используется напрямую
let from_short_id = hex::encode(&peer_id.0[..8]);  // peer_id из пакета
```

### Исправление
Каждый прикладной пакет должен содержать **Ed25519 подпись** над (packet_type || sender || packet_id || payload). Получатель проверяет подпись через публичный Ed25519 ключ отправителя, полученный при Hello handshake.

**Добавить в P2PPacket:**
```rust
pub struct P2PPacket {
    // ... существующие поля ...
    pub signature: Option<[u8; 64]>,  // Ed25519 подпись
}
```

**При отправке (в transport.rs):**
```rust
let signing_key = self.signing_key.clone();
let msg = packet.signable_bytes();  // type + sender + packet_id + payload
let signature = signing_key.sign(&msg);
packet.signature = Some(signature.to_bytes());
```

**При приёме:**
```rust
if let Some(sig_bytes) = &packet.signature {
    let peer_pubkey = self.get_peer_ed25519_pubkey(&packet.sender).await?;
    let signature = ed25519_dalek::Signature::from_bytes(sig_bytes);
    peer_pubkey.verify(&packet.signable_bytes(), &signature)
        .map_err(|_| "Invalid packet signature")?;
}
```

---

## SEC-03: Hello handshake без подписи

### Суть
`P2PHelloPacket` содержит `node_id` и `x25519_public`, но эта связка **не подписана**. Атакующий может:
1. Перехватить легитимный Hello
2. Отправить фиктивный Hello с чужим `node_id` и своим `x25519_public`
3. Стать MitM между двумя нодами — они будут думать что общаются друг с другом, а ключ обмена будет с атакующим

### Исправление
Добавить Ed25519 подпись в Hello:
```rust
pub struct P2PHelloPacket {
    // ... существующие поля ...
    pub ed25519_public: [u8; 32],   // Ed25519 верификационный ключ
    pub signature: [u8; 64],        // Sign(node_id || x25519_public || nonce || timestamp)
}
```

При генерации:
```rust
let msg = [node_id.as_ref(), x25519_public.as_ref(), &nonce.to_le_bytes(), &timestamp.to_le_bytes()].concat();
let signature = signing_key.sign(&msg);
```

При верификации:
```rust
let vk = ed25519_dalek::VerifyingKey::from_bytes(&hello.ed25519_public)?;
// Проверить что ed25519_public соответствует node_id (node_id = Hash(ed25519_public))
let expected_node_id = HashId(sha2::Sha256::digest(&hello.ed25519_public).into());
if expected_node_id != hello.node_id { return Err("node_id mismatch"); }
// Проверить подпись
vk.verify(&msg, &Signature::from_bytes(&hello.signature))?;
```

Это закрывает MitM и спуфинг одновременно, так как `node_id = Hash(ed25519_public)` — детерминированная связь.

---

## SEC-04: Приватные ключи в plaintext JSON

### Суть
`save_to_file` сериализует `private_key: [u8; 32]` и `signing_private_key: [u8; 32]` в обычный JSON. Файл защищён только правами 0600. Если атакующий получит доступ к диску (backup, swap, /proc/[pid]/mem) — ключи скомпрометированы.

### Исправление
Зашифровать ключи перед сохранением через password-based key derivation:

```rust
use argon2::Argon2;
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};
use rand::RngCore;

pub fn save_to_file_encrypted(&self, port: u16, password: &[u8]) -> Result<PathBuf, String> {
    let mut salt = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut salt);
    
    // Derive key via Argon2id
    let mut key = [0u8; 32];
    Argon2::default().hash_password_into(password, &salt, &mut key)
        .map_err(|e| e.to_string())?;
    
    // Encrypt identity bytes
    let cipher = Aes256Gcm::new_from_slice(&key).unwrap();
    let mut nonce_bytes = [0u8; 12];
    rand::rngs::OsRng.fill_bytes(&mut nonce_bytes);
    let nonce = Nonce::from_slice(&nonce_bytes);
    
    let plaintext = /* serialize private keys */;
    let ciphertext = cipher.encrypt(nonce, plaintext.as_ref()).unwrap();
    
    // Save: { salt, nonce, ciphertext, public_keys (не секретны) }
}
```

**Краткосрочно (если пароль нежелателен):** Использовать machine-bound ключ (на основе `/etc/machine-id` + путь к файлу) для защиты от копирования, не от локального root доступа.

---

## SEC-05 + SEC-06: JS-инъекция в модалке входящего звонка

### Суть
В `voice-call.js` и `video-call.js` значения `call_id` и `from_short_id` вставляются напрямую в HTML строку onclick-атрибута:

```javascript
// voice-call.js:108 — УЯЗВИМО
onclick="window.voiceCallManager.acceptCall('${callInfo.call_id}', '${callInfo.from_short_id}')"
```

Если атакующая нода отправит `call_id = "x'); alert(document.cookie); //"` — выполнится произвольный JS.

`display_name` правильно escapeHTML в тексте, но `call_id` и `from_short_id` не escapeHTML.

### Исправление

**Подход 1 (правильный):** Использовать data-атрибуты вместо onclick:
```javascript
showIncomingCallModal(callInfo) {
    // Сохраняем данные в модальнике, не в onclick
    this.pendingCallInfo = callInfo;  // { call_id, from_short_id }
    this.incomingModal.innerHTML = `
        ...
        <button class="btn btn-success" id="acceptCallBtn">✅ Принять</button>
        <button class="btn btn-danger" id="rejectCallBtn">❌ Отклонить</button>
    `;
    // Привязываем обработчики через JS (без inline onclick)
    document.getElementById('acceptCallBtn').addEventListener('click', () => {
        this.acceptCall(this.pendingCallInfo.call_id, this.pendingCallInfo.from_short_id);
    });
    document.getElementById('rejectCallBtn').addEventListener('click', () => {
        this.rejectCall(this.pendingCallInfo.call_id);
    });
}
```

**Подход 2 (минимальный):** escapeHTML для call_id и from_short_id перед вставкой в onclick. Это не полностью безопасно для onclick (нужно ещё escapeJS), лучше Подход 1.

---

## SEC-07 + SEC-08: Path Traversal в файловом API

### Суть
`/api/files/content/{file_id}/{filename}` — `filename` может содержать `../../../etc/passwd`. `sanitize_filename` в `file_transfer.rs` заменяет `/\:*?"<>|` но не обрабатывает:
- Unicode-нормализацию (e.g. `%2F` через URL-decode)
- Null bytes
- Windows-reserved имена (NUL, CON, PRN)
- Trailing dots/spaces

### Исправление
```rust
pub fn sanitize_filename(name: &str) -> String {
    // Убрать всё кроме ASCII alphanumeric + safe symbols
    let sanitized: String = name.chars()
        .map(|c| if c.is_ascii_alphanumeric() || "._- ".contains(c) { c } else { '_' })
        .collect();
    
    // Убрать path separators после URL-decode
    let sanitized = sanitized.replace("..", "_");
    
    // Ограничить длину
    let sanitized = &sanitized[..sanitized.len().min(255)];
    
    // Не должен начинаться с точки (скрытый файл Unix)
    let sanitized = if sanitized.starts_with('.') { format!("_{}", sanitized) } else { sanitized.to_string() };
    
    if sanitized.is_empty() { "file".to_string() } else { sanitized }
}
```

И в хендлере добавить явную проверку что путь не вышел за пределы `downloads/`:
```rust
let canonical = std::fs::canonicalize(&full_path)?;
let downloads_dir = std::fs::canonicalize("downloads/")?;
if !canonical.starts_with(&downloads_dir) {
    return Err(StatusCode::FORBIDDEN);
}
```

---

## SEC-09: `static mut LAST_RECV` без синхронизации

### Суть
```rust
// transport.rs:394
static mut LAST_RECV: u64 = 0;
...
unsafe {
    if LAST_RECV != 0 && now - LAST_RECV > 1000 { ... }
    LAST_RECV = now;
}
```
Это UB в Rust при доступе из нескольких потоков. Хотя receive_loop работает в одном task, futures могут быть переведены между потоками в tokio runtime.

### Исправление
Заменить на `std::sync::atomic::AtomicU64`:
```rust
static LAST_RECV: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
let prev = LAST_RECV.swap(now, std::sync::atomic::Ordering::Relaxed);
if prev != 0 && now - prev > 1000 {
    println!("[P2P] ⚠️ Gap in receive: {} ms", now - prev);
}
```

---

## SEC-10: Bootstrap адреса не верифицированы

### Суть
Bootstrap-ноды задаются в конфиге (`bootstrap_nodes`). При компрометации конфига или MITM подстановке атакующий может подсунуть вредоносный bootstrap — нода подключится к нему первой, что позволит атакующему контролировать peer discovery.

### Исправление
- Хардкодировать fingerprint (SHA-256 pubkey) bootstrap-нод в коде и верифицировать при подключении
- Или подписывать конфиг bootstrap-нод Ed25519 ключом оператора сети

---

## Порядок исправлений (критический путь)

```
Неделя 1 — Немедленно:
├── SEC-05/06: Убрать JS-инъекцию в модалках (30 мин)
├── SEC-08: Hardened sanitize_filename + canonicalize проверка (1 час)
└── SEC-09: static mut → AtomicU64 (15 мин)

Неделя 2 — Безопасность P2P:
├── SEC-03: Подписи в Hello handshake (2-3 дня)
├── SEC-01: Шифрование в send_packet_dual_path (1-2 дня)
└── SEC-02: Ed25519 подпись в каждом пакете (2-3 дня)

Неделя 3-4 — Хранение ключей:
└── SEC-04: Argon2 + AES-GCM для identity файла (1-2 дня)

Фоновая задача:
└── SEC-07: Canonicalize проверка в file API (1 час)
└── SEC-10: Bootstrap fingerprint (1 день)
└── SEC-11/12: Убрать пути из логов, зашифровать checkpoints
```

---

## Что уже сделано правильно

- Ed25519 ключи **существуют** в `NodeIdentity` — основа для подписей есть
- X25519 Diffie-Hellman **реализован** в `encryption_manager.rs` — основа для шифрования есть
- AES-256-GCM с anti-replay nonce **реализован** — нужно только подключить
- `escapeHTML` в UI **есть** и применяется для текстового контента
- Файлы identity **права 0600** — минимальная OS-level защита есть
- `seen_nonces` в Session — anti-replay заготовка есть

Всё критически нужное уже написано — нужно **соединить провода**.
