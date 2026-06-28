# YANDI Network Evolution Plan — Hardening Cycle

**Last revision:** 2026-05-11
**Owner:** YANDI core dev (solo)
**Mode:** persistent plan, do not edit completed sections, append addenda

---

## 0. Контекст

Два предыдущих цикла закрыты:
- **MVP-цикл (Iter T → Iter 5):** ядро — TLS+WS, multi-hop circuit, jurisdiction TLV, pairing, onion encryption. 40/40 unit-тестов.
- **Integration cycle (6 шагов):** dispatch_decrypted_wagon helper, WS-pump dispatch, 0xB0..0xB3 hook, 0xC0/0xC1 hook, onion-mode switch, auto-reconnect watchdog.

Этот цикл — **Hardening**: подобрать всё что было отмечено в полке деферредов и довести до production-готовности.

**Жёсткое правило:** не ломать работающий чат/прокси. После каждого шага — `cargo check` + relevant unit-тесты должны проходить. Если ломается — откатываем шаг.

---

## 1. Roadmap по приоритету

### P0 — критично для prod-deploy (без этого реальной mobile↔anchor связки нет)

#### Step 1 (P0) — Configurable WS-bind

**Goal:** WS-server сейчас hard-coded на `0.0.0.0:8443`. Должен браться из конфига или CLI.

**Steps:**
- 1.1. Добавить в `~/.yandi/config.toml` секцию `[ws]` с полем `bind = "0.0.0.0:443"` (default 8443).
- 1.2. CLI override `--ws-bind <addr>` имеет приоритет над config.
- 1.3. Заменить hard-coded `let bind: SocketAddr = "0.0.0.0:8443".parse().unwrap()` на чтение значения.
- 1.4. На случай permission-denied bind на 443 (без root) — graceful fallback на 8443 с warning.

**Tests:**
- Unit: parse config с `[ws] bind = ...`.
- Manual: `yandi --ws-bind 0.0.0.0:9443` — slstrings showing bound port.

**Risk:** ничего критичного — это конфигурация.

---

#### Step 2 (P0) — Session-key resume в EncryptionManager

**Goal:** сейчас 0xC1 RESUME_ACK не отправляется потому что после resume у anchor нет session-key для encryption (X25519-derived). Нужно сохранять/восстанавливать session-key одновременно с resume_secret.

**Steps:**
- 2.1. Расширить `SessionToken` или ввести `SessionKeyEnvelope { token: SessionToken, peer_pubkey: [u8;32], session_key: [u8;32] }` хранимый в `paired_clients.json` (encrypted-at-rest при необходимости — простой подход: 0o600 + plain JSON в первом приближении).
- 2.2. В `EncryptionManager` добавить метод `restore_session(peer_id, session_key)` который вставляет ключ в внутренний state без ECDH-handshake.
- 2.3. На anchor'е в `handle_resume_packet` после успешного verify — вызвать `restore_session` и тогда отправить 0xC1 ACK через `send_encrypted` (теперь работает).
- 2.4. На mobile в `connect_to_anchor_ws` — flow: если в `paired_anchors` есть session_token и не expired — отправить 0xC0 RESUME _до_ Hello; ждать 0xC1 ACK; если ok — пропустить Hello-handshake; если не ok — стандартный Hello flow.

**Tests:**
- Unit: `EncryptionManager::restore_session` вставляет ключ, последующий `encrypt(peer)` работает.
- Unit: round-trip сценарий store→restore→encrypt→decrypt.
- Integration manual: anchor + mobile с paired pair → mobile рестарт → resume без Hello.

**Risk:** Encryption-manager — критичная часть. Изменения должны быть additive, не трогать existing encrypt/decrypt path.

---

#### Step 3 (P0) — QR pairing flow в Web UI

**Goal:** для новых mobile-клиентов нужен механизм pair'инга. Anchor показывает QR, mobile сканит, payload приземляется в `paired_anchors.json`. SessionToken issue.

**Steps:**
- 3.1. Web UI endpoint `GET /pair/qr` — генерит `PairingPayload`, рендерит QR-PNG (новый dep: `qrcode = "0.14"`).
- 3.2. Web UI endpoint `POST /pair/issue` — anchor вызывает `paired_clients.issue(client_pubkey_hex, ttl)` и возвращает `SessionToken`. Защита: проверка origin / pre-shared CSRF token чтобы случайный посторонний не мог issue'ить.
- 3.3. Mobile-сторона: CLI команда или web-UI `import-pairing <qr-string-or-png>` — парсит QR, кладёт в `paired_anchors.json`.
- 3.4. После pair'инга mobile делает первое подключение через WS, получает session_token (через 0xC2 SESSION_ISSUE — новый wire-байт; либо через web-UI после auth).

**Tests:**
- Unit: QR-PNG render не паникует (smoke).
- Unit: parsing QR string → `PairingPayload` (уже есть `pairing_payload_qr_roundtrip`).
- Manual: открыть `/pair/qr` в браузере, отсканить телефоном (проверка картинкой).

**Risk:** QR-генерация сама по себе несложная, но web-UI auth модель — нужна базовая защита от случайных HTTP-запросов.

---

### P1 — функциональность (циркуит-работа реальная)

#### Step 4 (P1) — Proxy/SOCKS5 over circuit

**Goal:** `HttpProxyClient` и `Socks5ProxyServer` сейчас шлют трафик через `transport.send_encrypted(exit_peer, ...)` напрямую. Должны использовать circuit (`build_circuit_onion` → `send_circuit_data_onion`).

**Steps:**
- 4.1. Добавить в proxy/SOCKS5 интерфейс мутабельную ссылку на `CircuitId` (и опционально `transport: Arc<P2PTransport>`).
- 4.2. Изменить `forward to gateway`: вместо `send_encrypted(gateway_id, payload)` — `transport.send_circuit_data_onion(circuit_id, payload)`.
- 4.3. На стороне exit (CircuitAction::Deliver) — payload pipe'ится в существующий ProxyGateway flow (HTTP fetch / SOCKS5 connect).
- 4.4. Backward path: ответ exit'а → `send_circuit_data_onion` от exit'а на initiator'е → CircuitAction::Deliver → канал на client → response browser'у.

**Tests:**
- Unit: симулятор 3-process — initiator-anchor1-anchor2 — HTTP fetch результат через circuit совпадает с direct.
- Manual: `socks5 <id>` пускает трафик через 2-hop circuit, проверка через `curl --socks5 ...`.

**Risk:** реальная intergration с существующим Proxy/SOCKS5-кодом — это работа уровня одной итерации; может потянуть refactor в `proxy.rs`/`socks5.rs`.

---

#### Step 5 (P1) — DHT find_anchors_by_jurisdiction (полная выборка)

**Goal:** сейчас `find_anchors_by_jurisdiction` ходит только по локальной peer-table. Должна делать DHT-запрос: «дай anchor'ов country=DE» через Kademlia find_node-подобный flow.

**Steps:**
- 5.1. В DHT (Kademlia) ввести индекс `jurisdiction → Vec<NodeId>` — keep at root level. Каждый anchor при announce'е обновляет.
- 5.2. Новый RPC `FIND_BY_JURISDICTION { country }` → возвращает `Vec<PeerInfo>` от queried-нод.
- 5.3. Helper в transport: сначала локальная peer-table, потом DHT-запрос если результата мало (<3).
- 5.4. Cache-слой: TTL 5 минут на DHT-результат, чтобы не флудить запросами.

**Tests:**
- Unit: DHT mock с 5 anchor'ами различных стран; find_by_jurisdiction("US") → только US.
- Integration: 3 ноды разных стран; find_by("DE") возвращает корректный set.

**Risk:** DHT-код — отдельный модуль, не самый простой для рефактора.

---

#### Step 6 (P1) — CLI флаги селекшен policy

**Goal:** `--exit-jurisdiction`, `--my-jurisdiction`, `--anchor-store`, `--ws-bind` — все как обычные argv-флаги.

**Steps:**
- 6.1. `--exit-jurisdiction XX` — при построении circuit'а инициатор фильтрует exit-кандидатов по country.
- 6.2. `--my-jurisdiction XX` — синоним к существующему `--jurisdiction` (alias). Оставляем оба для backward-compat.
- 6.3. `--anchor-store <path>` — override path к paired_anchors.json (полезно для тестов).
- 6.4. `--ws-bind <addr>` — уже в Step 1.

**Tests:**
- Unit: argv-парсер каждого флага.
- Manual: запуск со всеми флагами одновременно — нет конфликтов.

**Risk:** минимальный.

---

### P2 — R&D / технический долг (не блокирует prod)

#### Step 7 (P2) — Telescoping DH-handshake over wire

**Goal:** circuit-keys сейчас pre-shared (deterministic от `derive_hop_key(cid, idx, peer_id)`). Должен быть DH-handshake между initiator и каждым hop'ом через предыдущий канал — каждый hop узнаёт layer-key только в момент EXTEND.

**Steps:**
- 7.1. Расширить wire 0xB1 EXTEND: добавить X25519 ephemeral pubkey инициатора (32 байта).
- 7.2. Hop при получении EXTEND отвечает 0xB1' (или новый 0xB4) с своим X25519 pubkey'ом.
- 7.3. Initiator делает ECDH с pubkey'ем каждого hop'а → derived layer-key через HKDF.
- 7.4. Заменить `derive_hop_key` (deterministic) на ECDH-derived в onion-flow.

**Tests:**
- Unit: handshake round-trip между initiator и mock-hop'ом.
- Unit: layer-key стабильно одинаков между сторонами для того же handshake.

**Risk:** существенное расширение wire-формата. Backward-compat: вводим caps-bit `TELESCOPING_HANDSHAKE` чтобы старые ноды не падали.

---

#### Step 8 (P2) — Cell-в-cell classical Tor pipelining

**Goal:** текущая chain-схема (initiator pre-builds N independent cells) — упрощение. Classical Tor вкладывает cell в cell с stream-reassembly. Реализовать.

**Steps:**
- 8.1. Cell layout пересмотр: внутри pt_block оставить место для следующего cell-fragment'а.
- 8.2. Stream-reassembly buffer на каждом hop'е.
- 8.3. Тестирование on real 3-hop.

**Risk:** значительный crypto-рефактор. Должно быть с unit-тестами, иначе threat-model гарантии не сохранятся.

---

#### Step 9 (P2) — починка 3 pre-existing baseline-failure'ов

**Goal:** `adaptive::test_mode_switching`, `broadcast::test_broadcast_rate_limit`, `encryption::test_encryption_decryption` падают до моих изменений. Разобраться и починить или ignore'нуть.

**Steps:**
- 9.1. Прочитать каждый, понять причину.
- 9.2. Если test bitrot — fix; если behavior change — обновить assertions.

**Risk:** низкий, локальные правки.

---

### P3 — Beyond MVP (Iter 6+, на дальние циклы)

(Не входят в этот цикл, но **держим в полке**)

- **Anchor reputation / Sybil protection**: uptime score, traffic-volume score, web-of-trust среди anchor'ов
- **Bandwidth accounting**: peer'ы платят за трафик в локальной валюте
- **Client-side DHT для мобилок** (если/когда понадобится децентрализованное discovery без anchor'а)
- **Hidden services**: домашний anchor может публиковать локальный сервис под yandi-адресом
- **AI-RPC API** поверх circuit'ов для AI-to-AI обмена (главный долгосрочный use-case)

---

## 2. Test Strategy

Каждый шаг — `cargo check` + `cargo test --lib <module>` зелёные **до** перехода к следующему.
3 pre-existing failure'а — baseline до Step 9.

---

## 3. Полка / Deferred Backlog (НЕ ТЕРЯТЬ)

### 3.1. Не вошло в Hardening Cycle
- WS-server bind — Step 1 закрывает.
- 0xC1 ACK over encrypted — Step 2 закрывает.
- QR pairing — Step 3 закрывает.
- Proxy/SOCKS5 over circuit — Step 4 закрывает.
- DHT find_by_jurisdiction — Step 5 закрывает.
- Selection policy CLI — Step 6 закрывает.
- Telescoping DH — Step 7 закрывает.
- Cell-в-cell — Step 8 закрывает.
- Baseline failures — Step 9 закрывает.

### 3.2. Известные ограничения текущих steps (фиксируется при реализации каждого)

#### По факту реализации (Hardening 2026-05-11):
- **Step 1** ✅ fall-back на 8443 при bind-fail логируется warn'ом. CLI override > config > default.
- **Step 2** ✅ Wire 0xC0 RESUME расширен node_id (32B) для plaintext pre-Hello flow. WS-server теперь принимает Hello или RESUME первым. Mobile pre-restore session_key из PairedAnchorStore.
- **Step 3** ✅ qrcode = "0.14" features=["svg"] — генерим SVG, не PNG (image-dep избегаем). Origin/host check на /pair/issue.
- **Step 4** ⚠ exit-side Deliver→ProxyGateway автоматический parsing payload→ProxyRequest НЕ активирован по умолчанию — payload идёт в `circuit_delivery_tx`, ProxyGateway-парсинг — следующая задача. Initiator-side полностью работает (через `set_circuit_route`).
- **Step 5** ⚠ DHT RPC `FIND_BY_JURISDICTION` (план §5.2) НЕ реализован. Текущая реализация — local root-level индекс `JurisdictionIndex`, обновляется при приёме Hello-ов с jurisdiction TLV. Хватает для small-scale federation, полная DHT-выборка отложена.
- **Step 6** ✅ Все 4 флага работают; `--my-jurisdiction` — alias `--jurisdiction`.
- **Step 7** ⚠ Wire-codec и `derive_hop_key_ecdh` готовы и протестированы; caps bit 0x0800 определён. `wrap_onion_forward_chain` всё ещё использует deterministic ключи. Initiator должен где-то хранить per-circuit hop_x25519 чтобы перейти на ECDH — отдельная задача.
- **Step 8** ⚠ Только scaffolding: `StreamReassemblyBuffer`, `CellFragment` codec. Интеграция в `wrap_onion_forward` (фрагменты в pt_block) требует пересмотра cell-layout — отдельный step.
- **Step 9** ✅ Все 3 baseline-failure'а зелёные.

### 3.3. Iter 6+ (P3)
См. § P3 выше.

### 3.4. Wire-format extensions table (running)

| Бит / Байт | Назначение | Cycle | Status |
|---|---|---|---|
| (предыдущие) | см. предыдущий план | T/NAT/F/2..5/Integration | ✅ |
| pkt 0xC0 RESUME v2 (с node_id) | mobile resume'ит сессию plaintext до Hello | Hardening Step 2 | ✅ |
| pkt 0xC2 SESSION_ISSUE | anchor выдаёт session_token при первом pair'е | Hardening Step 3 | ✅ |
| pkt 0xB4 EXTEND_REPLY | hop отвечает X25519 pubkey'ем | Hardening Step 7 | ✅ codec |
| caps 0x0800 TELESCOPING_HANDSHAKE | поддержка ECDH-circuit'ов | Hardening Step 7 | ✅ |
| Свободно: 0x90, 0xC3+, 0xD0+ | резерв | — | — |

---

## 4. Stop / pivot conditions

- Если работающий чат/прокси сломался → откат, обновить STATUS, переосмыслить.
- Тест показал что архитектурное предположение неверно → addendum в этом плане, пересогласовать step.
- Зависимость не подтянулась → ищем замену (например, `qrcode` crate vs. inline-PPM).

---

## 5. Pre-existing failures (baseline до Step 9)

- `netlayer::adaptive::tests::test_mode_switching` — `Balanced != Performance`
- `netlayer::broadcast::tests::test_broadcast_rate_limit`
- `netlayer::encryption::tests::test_encryption_decryption`
- `socket_manager.rs:83`: `rotate(_, _)` 2 args вместо 3

Не блокируют шаги P0/P1.

---

## 6. Foundation done (предыдущие циклы, контекст)

### MVP Cycle (Iter T → Iter 5, 2026-05-10)
| Iter | Wire / Файлы | Tests |
|---|---|---|
| Iter T (Адаптивный transport) | wagon 1200B, rate adapt, dynamic clones, AdaptiveController | unit |
| Iter NAT (Hole-punch + relay + NAT-PMP) | 0xA0..0xA5, NAT-PMP RFC 6886, EIM/EDM | unit |
| Iter F (Foundation: NodeRole) | caps 0x100..0x400, `--lite`/`--anchor`/`--mobile` | существующие |
| Iter 2 (WS-over-TLS) | tls_cert.rs, ws_transport.rs (8443), bridge, --anchor-url/--anchor-fp | 5/5 |
| Iter 3 (Multi-hop circuit + jurisdiction) | 0xB0..0xB3, jurisdiction TLV, circuit.rs, process_circuit_packet | 12/12 |
| Iter 4 (Pairing + session resume) | 0xC0/0xC1, pairing.rs (PairingPayload/SessionToken/HMAC, JSON-stores) | 11/11 |
| Iter 5 (Onion encryption) | onion.rs (1024B cell, layered chacha20poly1305) | 12/12 |

### Integration Cycle (6 steps, 2026-05-10)
| Step | Subject | Status |
|---|---|---|
| 1 | extract `dispatch_decrypted_wagon` | ✅ |
| 2 | WS-pump dispatch | ✅ |
| 3 | circuit packets (0xB0..0xB3) hook | ✅ |
| 4 | resume packets (0xC0/0xC1) hook | ✅ |
| 5 | onion-mode switch на circuit'е | ✅ |
| 6 | auto-reconnect и failover | ✅ |

**Итого 40/40 unit-тестов из MVP + integration зелёные. Сборка clean. 58 netlayer-тестов pass + 3 baseline-fail.**
