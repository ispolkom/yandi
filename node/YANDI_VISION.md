# YANDI — Vision & Positioning

**Last revised:** 2026-05-11

---

## Что такое YANDI

**Распределённая личная инфраструктура.** Не один продукт, а **слойный стек**, который заменяет привычные облачные сервисы и одновременно даёт фундамент для нового класса приложений — federated AI с локальными моделями и собственными данными.

Краткая формула: **«self-sovereign AI-mesh»** — peer-to-peer сеть с собственным транспортом, шифрованием, идентичностью, и application-слоями (мессенджер, файлы, голос, видео, AI-RPC), всё под контролем пользователя, без центральных серверов.

---

## Конкурентный landscape — почему это новый класс продукта

Каждый отдельный слой имеет аналоги:

| Слой | Аналоги | Что YANDI делает иначе |
|---|---|---|
| P2P-mesh transport | Yggdrasil, cjdns | + identity / role-based, + jurisdiction routing, + redundancy-first FEC |
| VPN | WireGuard, Tailscale | + bypass throttling (FEC + multipath), + не требует central control |
| Anonymity | Tor | + private community (не публичный пул), + multi-purpose не только web |
| Federated messenger | Matrix, XMPP | + собственный transport, + без HTTP-зависимости, + без серверной федерации |
| P2P messenger | Briar, Session | + multi-anchor pair'инг, + circuit-routing, + AI-ready |
| Voice/Video | Skype, Discord, Jitsi | + поверх собственного transport, + privacy by architecture |
| Local LLM | Ollama, llama.cpp | + federation между нодами (отсутствует в Ollama) |
| AI-API | OpenAI, Anthropic | + локальные модели + федерированный retrieval, no cloud |

**Никто не покрывает всё это в одной коробке.** Это и есть market gap.

---

## Слойная модель

```
┌──────────────────────────────────────────────────────────────┐
│ Apps:  Messenger | Files | Voice/Video | AI-RPC | Local LLM  │
├──────────────────────────────────────────────────────────────┤
│ Federation Discovery (DHT-based + jurisdiction-aware)        │
├──────────────────────────────────────────────────────────────┤
│ Circuit / Multi-hop / Onion encryption                       │
├──────────────────────────────────────────────────────────────┤
│ Identity / Pairing / Session resume                          │
├──────────────────────────────────────────────────────────────┤
│ Wagon-transport (redundancy-FEC) | WS-over-TLS              │
├──────────────────────────────────────────────────────────────┤
│ UDP / TCP socket + OS network                                │
└──────────────────────────────────────────────────────────────┘
```

---

## Текущее состояние (2026-05-11)

### ✅ Реализовано и работает

- **Transport (wagon).** Path0 + Path1 dual-path с dedup. 60K wagon. Rollback от мая 2026 вернул historic throughput.
- **Identity.** Ed25519 (signing) + X25519 (encryption) keypair. Self-certifying node-name.
- **WS-over-TLS** (mobile↔anchor) с fingerprint pinning.
- **Multi-hop circuits** (0xB0..0xB3) + jurisdiction TLV в Hello.
- **Onion encryption** (Iter 5) — chacha20poly1305, 1024B cells.
- **Pairing.** QR-payload + session resume + paired_clients/paired_anchors store. Hardening cycle добавил session-key resume.
- **HTTP proxy + SOCKS5 proxy** через peer.
- **Messenger** (CommPacket, ChatManager) — UI готов, group chat есть.
- **File transfer** через FileTransferManager — работает, есть отдельные issues (см. backlog).
- **mDNS local discovery.**
- **DHT (Kademlia)** — store/find_node, hot-cache.
- **Web UI** на localhost:9999 (axum).

### 🛠 В работе / частично

- **Voice/Video** — webrtc-rs зависимости подключены, media-session manager есть, signaling в процессе.
- **Telescoping ECDH** в onion-handshake — wire codec + caps bit готовы (Step 7 of Hardening), реальный flow в `wrap_onion_forward_chain` ещё на deterministic ключах.
- **Stream-reassembly partial-emit** — full-train semantics всё ещё, partial-emit запланирован (FUTURE_IDEAS §4).

### ❌ Запланировано (приоритет высокий)

- **AI-RPC.** Spec + reference impl. Foundation для inter-node AI coordination.
- **Local AI integration.** Ollama-style local LLM, expose через YANDI как RPC. Mobile/client'ы делают inference на anchor'е.
- **DHT-shared knowledge DB.** Каждая нода держит свой кусок (PDF, books, training data). DHT-разметка `topic_hash → node_list`. Federated retrieval.
- **Performance-Enhancing Proxy (PEP).** Buffered streaming relay для видео (см. FUTURE_IDEAS §1).
- **Reputation/Sybil** (P3) — anchor uptime score, traffic-volume score.

---

## Стратегические приоритеты

### Iter 6 (next): **AI-RPC foundation**

Без этого YANDI = ещё один децентрализованный мессенджер. С этим — **уникальная платформа**.

Минимальный scope:
1. **`yandi-fetch`** API — client запрашивает HTTP-ресурс через anchor'а (anchor сам делает HTTPS, отдаёт body). Foundation для всего что не browser-based. См. FUTURE_IDEAS §10.
2. **`yandi-rpc` spec** — call-pattern для запросов «`peer P, выполни Q, верни R`». Идентификация peer'а, payload, signing, rate-limiting.
3. **AI-RPC reference** — простой endpoint: «`peer X, инференс на model M, prompt P, верни completion`». Сначала proxy-pattern (anchor → Ollama localhost → ответ обратно). Это даёт работающий demo на 1 anchor.

### Iter 7: **Local LLM + federation**

1. **Ollama-style integration.** Anchor запускает локально Ollama (или llama.cpp server). Yandi exposes как `yandi-rpc` endpoint.
2. **Model discovery через DHT.** Регистрация: «у меня есть model llama3.1:70b». Discovery: «кто умеет gpt-class инференс?».
3. **Auth and quota.** Anchor решает кто может его GPU использовать.

### Iter 8: **DHT-shared knowledge base**

1. **Chunk storage.** Анкор держит коллекцию документов (PDF, markdown, datasets). Каждый chunk имеет `topic_hash`.
2. **DHT-index.** `topic_hash → list of nodes хранящих chunk`.
3. **Federated retrieval.** AI-агент делает запрос «найди chunks по topic X» — DHT-lookup → fetch chunks с нескольких peer'ов параллельно.
4. **RAG-like inference.** Совместная работа: model на anchor A + knowledge chunks с anchor B+C → ответ.

### Iter 9: **Voice/Video polish**

- Reliable media session establishment.
- Multi-party calls.
- E2E через onion для group calls.

### Iter 10+: **Reputation, governance, sybil-resistance**

Когда сеть > 100 узлов и публикация — приходит время threat-model укрепить.

---

## Use-cases по приоритетам

1. **Small private federation.** 5–50 узлов доверяющих друг другу. Мессенджер + files + groupcall + AI-RPC между ними. Никакого облака. **Это сегодняшний MVP-сценарий.**

2. **Bypass throttling / DPI.** Жители стран с активным шейпингом провайдеров и DPI. YANDI как «overlay-internet» с redundant FEC-transport. **Уже работает.**

3. **AI cooperatives.** Группа researchers / engineers shareing GPU-time и knowledge bases. Каждый запускает Ollama локально, federation координирует. **Iter 7-8 target.**

4. **Sovereign personal cloud.** Один человек — несколько устройств (desktop, laptop, phone, NAS). Свой anchor дома, все mobile-клиенты pair'ются. Заменяет iCloud/Google. **Iter 6-7 target.**

5. **Mesh для отключённых регионов.** В offline-местах несколько anchor'ов через любой канал (LoRa-bridge, satellite) образуют локальную AI-сеть. **Iter 10+ aspiration.**

---

## Не-цели (явно)

- **Не пытаемся быть mainstream-VPN.** WireGuard выиграет в простоте и kernel-mode performance.
- **Не пытаемся быть Tor-конкурентом по anonymity.** Tor шире, k-anonymity выше.
- **Не пытаемся быть HTTP/3 / generic-internet.** QUIC лучше и стандартизирован.
- **Не закрытое решение.** Code open, threat-model documented, no marketing-driven trust.

---

## Что нужно от пользователя/community

1. **Hosting anchor'а** — узел с public IP, 24/7, желательно в jurisdiction free от censorship.
2. **Doverие peer'ам** — это **private federation**, не публичный пул. Pair'инг через QR — это deliberate trust step.
3. **Терпение** — пока сеть малая, hit-rate cache'й низкий, exit-jurisdiction может быть ограничен.

---

## Метрики «здоровья» проекта

Хочется отслеживать:

- Размер сети (anchor'ов / mobile-клиентов).
- Среднее uptime anchor'а.
- Throughput (Mbps p50/p95 через proxy).
- Failure rate соединений (% потерянных trains).
- AI-RPC adoption (когда появится).
- Количество forks / contributors (когда станет PR-friendly).

---

## Ссылки

- `NETWORK_EVOLUTION_PLAN.md` — детальный технический roadmap.
- `NETWORK_EVOLUTION_STATUS.md` — текущее состояние реализации.
- `FUTURE_IDEAS.md` — backlog архитектурных идей.
- `DEPLOYMENT.md` — production deployment guide.
- `YANDI_TRANSPORT_PLAN.md`, `YANDI_TRANSPORT_UPDATES.md` — историческая часть про transport-уровень.
