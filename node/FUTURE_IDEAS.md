# YANDI — Future Ideas Backlog

**Date:** 2026-05-11
**Updated:** 2026-05-11 — added AI-RPC / Local LLM / DHT-shared DB as core direction (post-vision discussion).

**Context:** записаны в ходе обсуждения transport throughput'а, архитектурных альтернатив для шлюза и **позиционирования YANDI как self-sovereign AI-mesh** (см. `YANDI_VISION.md`). Не входят в Hardening Cycle. Каждая идея — отдельная P1/P2/P3 итерация.

---

## 1. Buffered Streaming Relay (PEP-mode) — главная

**Что:** шлюз становится **stream terminator + buffer**. Сам открывает HTTPS к target, тянет на максимум канала, формирует rolling-manifest батчей (по 1–2 МБ). Client тянет batches по id через wagon-transport, собирает обратно, отдаёт browser'у через локальный proxy.

**Зачем:**
- **Throttling immunity.** YouTube видит шлюз как 300Mbps клиента, шейпинг провайдера применяется только к wagon-трафику client↔шлюз, не к YouTube fingerprint.
- **Pre-buffer** на 30 секунд видео → "Buffering…" не доходит до player'а на просадках.
- **Bandwidth aggregation.** Шлюз делает параллельные Range-requests к target (aria2-style), client получает агрегированную скорость.
- **Anti-DPI radical.** NL↔YouTube — обычный HTTPS из дата-центра. Client↔NL — обфусцированный wagon-трафик. Корреляция почти невозможна.

**Главный technical blocker — HTTPS termination.** Сейчас browser→прокси через CONNECT даёт шлюзу только raw-TLS-bytes. Чтобы "смотреть сам", TLS должен терминироваться на шлюзе. Варианты:

- **A. MitM с собственным Yandi-CA.** Browser/system устанавливает Yandi root cert. Шлюз генерит fake-cert для youtube.com on-the-fly. Работает как mitmproxy/Squid SslBump. Жертва — user-trust step.
- **B. Custom yandi-aware client** (mobile-app, browser extension). Свой fetch-API `yandi.fetch(url)`. Шлюз сам делает HTTPS. Подходит для mobile / AI-RPC / federation, не для Chrome.

**Когда:** когда появится own-client (mobile-app или extension) или federation сценарий с AI-RPC.

**Риски/гочи:**
- Bandwidth waste при dropped session — нужен мгновенный cancel upstream при закрытии client'ом.
- Storage quota per-client (500MB × N клиентов) — без quota DoS-возможность.
- State machine на gateway (partial buffers, manifest, TTL, GC) — это уровень mini-CDN, не "10 строк кода".
- Не для realtime: chat/IM/voice → pass-through transport, не buffered.

**Литературное название:** PEP (Performance-Enhancing Proxy). Известно из спутникового интернета.

---

## 2. Cache-by-URL on gateway (BitTorrent-style)

**Что:** шлюз кэширует ответы `make_http_request` по URL'у с TTL/LRU. Реестр manifest'ов, дисковый storage. Повторные запросы к тому же URL — отдача из кэша.

**Зачем:**
- **Multi-user federation:** при N клиентах с overlapping requests — cache hit rate растёт линейно. Classic CDN economics.
- **Resumable downloads:** chunks с известным id'ом → docacha после disconnect.
- **Swarming:** client может тянуть chunks через несколько circuit'ов параллельно.

**Условия работы:**
- Только для **plain-HTTP path** (`make_http_request`). Шлюз сам делает HTTPS к target — видит URL и body. Cache естественен.
- Для CONNECT-tunnel'ов не применимо (см. §1 — нужна termination).

**Когда:** когда есть ≥10 client'ов с overlap'ом запросов И есть data по hit-rate для оценки ROI. Иначе чистый overhead.

**Риски:**
- Cache coherence (HTTP invalidation, ETag, Vary-headers) — отдельная rabbit hole.
- Privacy regression: шлюз помнит запросы клиента на диске, forensic artifact.
- Disk I/O + GC — отдельная подсистема надёжности.

---

## 3. Multi-circuit Parallel Fetch (Swarming без cache)

**Что:** client запрашивает один большой ресурс параллельно через 2–4 circuit'а с HTTP Range-headers. Каждый circuit заканчивается своим anchor'ом (или одним, но через разные пути). Шлюз делает Range-fetches к upstream независимо.

**Зачем:** аналог BitTorrent swarming, но stateless на шлюзе. Прирост скорости в 2–4× на крупных загрузках. Защищает от congestion на одном пути.

**Условия:** работает с любым transport-mode'ом (plain HTTP или CONNECT). Логика на client'е.

**Когда:** **first step из этой семьи** — простая в реализации, не требует stateful'а на шлюзе. Вписывается в "redundancy"-философию.

**Риски:** минимальные. Можно делать в Iter 6.

---

## 4. Streaming Reassembly с partial-emit

**Что:** receiver не ждёт full train. Как только wagon `n` собран из path0 ИЛИ path1 — он эмитится вверх немедленно. Лишний клон dedupится и дропается. **Конвейер сборки**, не **конвейер ожидания**.

**Зачем:** убирает head-of-line blocking. Один потерянный wagon (оба пути не дошли) не блокирует поток. Дотягивать его можно постфактум без блокировки.

**Что сейчас:** `Train::add_wagon` в `protocol/train.rs` (одинаковый между backup и текущей версией) **ждёт пока `total_received == total_wagons`**, потом emit. Backup-эталон тоже работал на этой модели — там было быстро благодаря 60K wagon × 128 in-flight, train целиком приезжал за миллисекунды.

**Когда:** Iter 6/P2. Сначала откатить wagon-size + dual-path (текущий патч). Убедиться что throughput вернулся. Потом partial-emit как доп. оптимизация.

**Риски:** новый cursor-state в Train. Reassembly semantics усложняется: out-of-order delivery вверх. Текущий receiver просто emit'ит весь train целиком — partial-emit это другая API в сторону consumer'а (нужен streaming reader, не один blob).

---

## 5. Dynamic wagon-size distribution-mimicking

**Что:** wagon-size не фиксированный 60K, а варьируется по распределению, имитирующему популярный протокол. Например QUIC-mode: модальный ~1350B, длинный tail к 60K. Per-wagon size, не per-train.

**Зачем:**
- DPI flow-classifier тогда видит "QUIC-like flow", не "redundant streaming".
- Train: `[8K, 60K, 12K, 60K, 4K, ...]` — никаких regular pattern для signature-matching.

**Условия активации:** триггерится **внешним сигналом** ("mask-mode on"), а не постоянно. Окно "DPI ещё не классифицировала YANDI" пока открыто — маска лежит в кармане.

**Когда:** P2/P3, когда сеть выросла или появился внешний сигнал классификации.

**Риски:** мелкие wagon'ы = больше overhead per byte. Если 50/50 mix — теряем ~30% throughput на headers. Хочется ~10% маленьких / 90% больших.

---

## 6. Adaptive jitter между клонами

**Что:** path0 и path1 шлются не синхронно, а с задержкой 5–80мс между ними. Спред адаптивный: при низком loss — почти синхронно, при высоком — растягиваем.

**Зачем:**
1. **Anti-burst-loss decorrelation.** Burst-loss длится 20–200мс. Синхронные клоны попадают в один burst и оба теряются. Jitter 50мс выводит path1 в окно ВНЕ burst'а → **квадратичное** снижение burst-loss probability.
2. **Anti-DPI.** Сейчас DPI легко видит "пакет X байт + через ~0мс ещё X байт = redundant". Jitter ломает корреляцию.

**Когда:** можно прямо сейчас как дополнение к rollback'нутому dual-path. Дёшево, превентивно ослабляет statistical-anomaly detectors.

**Риски:** на чистом канале — лёгкий рост latency-to-completion. Незначительно.

---

## 7. Triple-path при критической нагрузке

**Что:** при observed loss > X% — третий клон (path2). Динамически. Дёшево когда не нужно, спасает когда нужно.

**Зачем:** твоя оригинальная модель — `2-of-2 survivors`. При loss 30% оба пути просядают вместе с заметной вероятностью. Третий клон — `1-of-3 survivors`, устойчивость выше.

**Когда:** Iter 7+. После rollback'а wagon+dual-path надо измерить — реально ли где-то loss > 30%. Если редко — оверкилл.

**Риски:** **3× bandwidth** на один payload. Для bytes/sec плана это серьёзно. Включать только опционально.

---

## 8. Mask-mode infrastructure

Совокупность анти-DPI мер, лежащих "в кармане" пока окно классификации открыто:

- **Port hygiene:** 443/UDP-443 (QUIC standard) вместо 8443. Hardening Step 1 уже даёт configurable bind.
- **TLS ClientHello mimicry:** выглядеть как Chrome QUIC handshake (utls в Rust есть).
- **Wagon-size distribution-mimicking** (§5).
- **Adaptive jitter** (§6) активный постоянно.
- **Cipher-suite hygiene:** не давать DPI уникальный fingerprint в TLS handshake.

**Триггер активации:** "loss > threshold ИЛИ external signal (новость / форум упоминание)". Пока триггер не сработал — на throughput не влияем.

**Когда:** инфраструктуру закладывать в Iter 6, активацию опционально per-deployment.

---

## 9. Per-anchor active-active multi-fetch

**Что:** client тянет один ресурс **одновременно** через RU + NL anchor'ов. Кто первый принёс — wins.

**Зачем:** application-уровневая избыточность поверх transport-избыточности. На один ресурс — два независимых пути от entry до exit.

**Когда:** связано с §3 (multi-circuit). Можно сделать вместе.

**Риски:** удвоение bandwidth на ресурс. Только для критичных ресурсов (initial page load, manifest). Не для bulk.

---

## 10. yandi-fetch CLI / SDK

**Что:** не browser-через-прокси, а собственный API на client'е: `yandi-fetch <url> [--cache] [--parallel N] [--anchor=NL]`. Шлюз делает HTTPS сам.

**Зачем:**
- Открывает все возможности §1 (buffered streaming), §2 (cache), §3 (swarming), §9 (multi-anchor) **без MitM**.
- Натуральный transport для AI-RPC, federation запросов, RSS-fetch, downloads.
- Foundation для mobile-app и browser-extension.

**Когда:** очень рано — это базовый API для всего что не-browser. Iter 6 кандидат.

**Риски:** нужен design протокола `yandi-fetch over wagon transport`. Не сложно, но нужно сразу делать с расчётом на §1-§3.

---

---

# Section II — AI-mesh layer (core direction после vision-discussion)

Не транспортные оптимизации, а **новый класс функциональности** который делает YANDI уникальным.

## 11. yandi-rpc — spec и foundation

**Что:** call-pattern для запросов между peer'ами: `peer P, выполни Q, верни R`. Не обычный HTTP — встроенный в transport-слой YANDI'а с identity-aware authentication.

**Минимальный wire-протокол:**
```
[YANDIRPC1][caller_node_id:32][callee_node_id:32]
[nonce:16][method_str_len:1][method_str][payload_len:4][payload]
[caller_signature:64]   // Ed25519 over всё выше
```

**Ответ симметричный:** `[YANDIRPC1][callee][caller][request_nonce][status:1][payload_len][payload][callee_signature]`.

**Семантика:** request-response, идемпотентный, поверх circuit'а. Если callee не отвечает за TTL — caller знает (это **не fire-and-forget** как chat'е).

**Зачем:** foundation для §12 (AI-inference), §13 (local LLM federation), §14 (DHT knowledge retrieval). Без этого AI-кооперация невозможна.

**Когда:** Iter 6. Это **next foundational step** после Hardening.

**Риски:** ABI-стабильность. Если в первой версии что-то отрезают — все потребители ломаются. Делать с расчётом на extension.

---

## 12. AI-inference proxy / yandi-ai-rpc

**Что:** один из методов §11: `peer X, инференс на model M, prompt P, верни completion`. Anchor с GPU держит локально Ollama / llama.cpp server, exposes как `yandi-rpc` endpoint. Mobile/client'ы не имеющие GPU делают federated inference.

**Минимальный API:**
```
method: "ai.complete"
payload: { model: "llama3.1:70b", prompt: "...", max_tokens: 500, ... }
response: { completion: "...", tokens_used: N, latency_ms: M }
```

**Зачем:**
- **Foundation use-case YANDI.** Это единственный сценарий где YANDI не "ещё один X", а уникальный — federated inference без облака.
- Mobile/слабые-устройства могут пользоваться 70B-моделями через anchor с RTX 4090.
- В private community один человек шарит GPU-time с группой.

**Когда:** Iter 7. После §11 — это очень короткий путь к demo.

**Риски:**
- Quota / rate-limit / abuse. Anchor должен решать кто может его GPU использовать.
- Streaming-completion (token-by-token) — нужен streaming-extension к §11, не блокирующий request-response.
- Privacy: anchor видит prompt'ы. Это проблема ровно как с OpenAI, **но** под полным контролем владельца anchor'а.

---

## 13. Local LLM federation: model discovery via DHT

**Что:** анкоры регистрируют в DHT какие модели они умеют запускать: `model_hash(name+quantization) → node_list`. Discovery: «найди peer'а который умеет gemma3:27b».

**Зачем:**
- Without discovery, mobile должен жёстко знать "use anchor X для llama:70b". С discovery — динамически выбирается ближайший / самый свободный.
- Foundation для **AI swarming**: один большой prompt разносится на 3 anchor'а параллельно.

**Когда:** Iter 7-8 параллельно с §12.

**Риски:** False advertisement (anchor говорит что умеет model но обрабатывает медленно). Reputation (P3) — после.

---

## 14. DHT-shared knowledge base (federated RAG)

**Что:** каждый anchor держит свою коллекцию документов (PDF, books, training data, scraped web). Каждый chunk имеет `topic_hash`. DHT-индекс `topic_hash → list of nodes`. AI-агент делает запрос «найди chunks по topic X» — DHT-lookup → fetch chunks с нескольких peer'ов параллельно → передаёт в context inference.

**Зачем:**
- **RAG без облака.** OpenAI и co предлагают «загрузи свои документы в наш облачный store». В YANDI документы остаются у владельца, AI-агент тянет on-demand.
- **Knowledge division of labor.** Один anchor — биология, другой — юриспруденция, третий — code. Federated retrieval собирает релевантное.
- **Censorship-resistant.** Документ удалили с YouTube → если он был sharded по 3 anchor'ам YANDI'а — он доступен.

**Минимальный stack:**
1. **Chunk storage** — per-anchor SQLite или ёмкий FS.
2. **Embedding index** — local FAISS / hnswlib над chunks.
3. **DHT publish** — `topic_hash → (anchor_id, chunk_count)`.
4. **`yandi-rpc` methods:** `kb.search(query) → list of (chunk_id, score)`, `kb.fetch(chunk_id) → text`.

**Когда:** Iter 8. После §11-§13.

**Риски:**
- **Indexing cost.** Embedding всей коллекции — RAM/compute-heavy.
- **Permissions.** Не все документы public. Нужны access-lists per-chunk.
- **Trust-aware retrieval.** AI-агент должен знать который peer trustworthy.

---

## 15. Sovereign personal cloud

**Что:** для одного пользователя с несколькими устройствами (desktop, laptop, phone, NAS). Свой anchor дома, все mobile-клиенты pair'ются через QR. Заменяет iCloud/Google Drive для **personal data**.

**Условия работы:**
- Anchor 24/7 дома (NAS / Raspberry Pi 5 / mini-PC).
- Public IP или dyndns через bootstrap.
- Pairing — однократный QR-scan.

**Использует:**
- §11 yandi-rpc — для sync операций.
- §14 DHT-knowledge — для bookmarks/notes-search.
- §12 если есть GPU на anchor'е.

**Когда:** Iter 6-7. Это самый простой "personal MVP".

**Риски:** один anchor = single point. Решается федерацией с другом / second anchor у родственника.

---

## Резюме приоритизации

### Transport / network layer

| # | Идея | Затраты | Профит | Когда |
|---|---|---|---|---|
| 6 | Adaptive jitter между клонами | Низкие | Средний (anti-burst + anti-DPI) | Сейчас как дополнение к rollback |
| 3 | Multi-circuit swarming | Низкие | Большой на больших файлах | Iter 6 после §11 |
| 4 | Streaming reassembly partial-emit | Средние | Средний (HoL blocking) | Iter 6/7 |
| 9 | Multi-anchor active-active | Низкие | Средний | Iter 7 |
| 1 | Buffered Streaming Relay (PEP) | Высокие | **Большой** для видео | Iter 8+ когда есть §11 |
| 2 | Cache-by-URL | Высокие | Большой при federation | Iter 9+, когда multi-user |
| 8 | Mask-mode infrastructure | Средние | Превентивно (анти-DPI) | Iter 7-8 |
| 5 | Distribution-mimicking wagon | Средние | Анти-DPI | Часть §8 |
| 7 | Triple-path | Низкие | Только при высоком loss | Iter 7+ |
| 10 | yandi-fetch CLI / SDK | Средние | Foundation для §11 | **Iter 6 (next)** |

### AI-mesh layer (core direction)

| # | Идея | Затраты | Профит | Когда |
|---|---|---|---|---|
| 11 | **yandi-rpc spec + foundation** | Средние | **Foundation для §12-§15** | **Iter 6 (next)** |
| 15 | Sovereign personal cloud (single-user) | Низкие | Personal MVP | Iter 6 после §11 |
| 12 | AI-inference proxy / yandi-ai-rpc | Средние | **Уникальный use-case YANDI** | Iter 7 |
| 13 | LLM model discovery via DHT | Низкие | AI swarming foundation | Iter 7-8 |
| 14 | DHT-shared knowledge base (federated RAG) | Высокие | **Censorship-resistant RAG** | Iter 8 |

### Critical path

```
[Hardening (done)]
        ↓
[§11 yandi-rpc] ─→ [§10 yandi-fetch] ─→ [§3 multi-circuit swarming]
        ↓                 ↓
[§15 personal cloud]  [§1 PEP / §2 cache]
        ↓
[§12 AI-inference] ─→ [§13 model discovery]
        ↓
[§14 DHT knowledge base] ──→ federated RAG
```

---

## Окно DPI-классификации (стратегический контекст)

Сейчас YANDI-трафик не классифицирован — слишком малая сеть, нет training-set'а. Это **временное окно преимущества**. Закрывается на одном из:

- Сеть > тысячи узлов → попадание в академический dataset.
- Публикация на форумах / в новостях.
- Государственный DPI помечает "unknown encrypted UDP с redundant patterns" как "изучить".

Пока окно открыто — throughput важнее обфускации. Когда закроется — переключаем "маску в кармане" (§8) триггером.

---

---

## Production deployment: required sysctl (2026-05-11)

**Найдено стресс-тестом 2026-05-11:** default kernel'ный `net.core.rmem_max = 212992` (208 KB) silently капает наш `SO_RCVBUF=4MB`. Реальный буфер становится ~416 KB = всего ~7 wagon'ов 60K в очереди. Под bursty-шейпингом провайдера → UDP-drops в ядре → train timeout → eviction → "белый экран" в browser'е.

**Permanent fix (на каждом хосте, обе ноды federation):**

```bash
# Одноразово:
sudo sysctl -w net.core.rmem_max=67108864
sudo sysctl -w net.core.wmem_max=67108864

# Persistent:
cat <<EOF | sudo tee /etc/sysctl.d/99-yandi.conf
net.core.rmem_max=67108864
net.core.wmem_max=67108864
EOF
sudo sysctl -p /etc/sysctl.d/99-yandi.conf
```

**Что это даёт:**
- До: 20 параллельных downloads → 6 обрывов, drops растут.
- После: 20 параллельных downloads → 2 обрыва (upstream-side close, не наш bug), **drops = 0**.

**В код добавлено** (transport.rs `with_handlers`): startup-warning если `rmem_max < 4MB`. Не блокирует запуск, только пишет в stderr с инструкцией.

## Стресс-тест 2026-05-11 — фиксация performance baseline

| Тест | Результат |
|---|---|
| Direct (без YANDI) | 9.8 Mbps (ISP-шейп) |
| Через YANDI proxy → NL | **78 Mbps** (6.5× обход шейпа) |
| 4 публичных speedtest avg | **78 Mbps** down / варьируется up |
| 8 параллельных × 30 МБ | 78 Mbps суммарно, делится ровно |
| 20 параллельных × 25 МБ | 78 Mbps суммарно, 90% success |
| UDP drops (после sysctl) | 0 |
| RSS ноды | 224 MB |
| Threads | 10 |
| ping under load | 100ms baseline, +647ms bufferbloat |

**Известный bufferbloat (+647ms latency-under-load)** — отдельный suspect для §6 (adaptive jitter pacing). Запланировано.

---

## Ссылки на текущее состояние

- `NETWORK_EVOLUTION_PLAN.md` — Hardening Cycle (9 steps, закрыт).
- `NETWORK_EVOLUTION_STATUS.md` — детали реализованного.
- `YANDI_TRANSPORT_PLAN.md` / `YANDI_TRANSPORT_UPDATES.md` — исторический контекст по transport-уровню.
