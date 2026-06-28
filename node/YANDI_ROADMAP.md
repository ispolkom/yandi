# YANDI — Roadmap

**Обновлено:** 2026-05-14  
**Статус пройденных этапов:** `CURRENT_STATE.md`

---

## Легенда

| Символ | Смысл |
|--------|-------|
| ✅ | Реализовано, работает |
| 🔧 | Частично реализовано / сломано |
| ⬜ | Запланировано, не начато |
| 🔬 | Требует исследования |
| ❌ | Отменено / не актуально |

---

## Блок 1 — Связь (Communication)

### Чат и файлы

| ID | Задача | Статус | Приоритет |
|----|--------|--------|-----------|
| F1 | Исправить буфер приёма P2P (1024 → 65536 байт) | ✅ | — |
| F2 | Эндпоинт `/api/files/status` + JS-retry при недоставке | ✅ | — |
| F3 | Unit-тесты доставки файлов | ⬜ | P2 |
| S1 | Звук `icq.mp3` при новом входящем сообщении (без document.hidden) | ✅ | — |
| S2 | Звук `icq-online.mp3` при переходе контакта offline→online | ✅ | — |

### Голосовые звонки

| ID | Задача | Статус | Приоритет |
|----|--------|--------|-----------|
| V1 | Бэкенд: VoiceCallRequest P2P, очередь входящих, API accept/reject/end | ✅ | — |
| V2 | Фронтенд: polling входящих, WebRTC timing fix, кнопка в хедере чата | ✅ | — |
| V3 | Звуки: `calling.mp3` (входящий), `call_out.mp3` (исходящий), DND-режим | ✅ | — |
| V4 | Unit-тесты: очередь входящих звонков, voice packet format | ⬜ | P1 |
| V5 | TURN-сервер для NAT traversal (сейчас только STUN) | ⬜ | P2 |

### Видеозвонки

| ID | Задача | Статус | Приоритет |
|----|--------|--------|-----------|
| VD1 | P2P пакеты VideoCallRequest/Accept/End/Reject (0xC0–0xC3) | ✅ (определены) | — |
| VD2 | `video-call.js` — WebRTC-скелет с видео | 🔧 (timing bug, нет polling) | — |
| VD3 | Бэкенд: очередь входящих видеозвонков, API по аналогии с голосом | ✅ | — |
| VD4 | Фронтенд: исправить timing (offer только после accept), polling входящих | ✅ | — |
| VD5 | Звуки видеозвонка + DND | ✅ | — |
| VD6 | UI: видео-превью в модалке (local + remote) | ✅ | — |

### Нативное аудио (CLI)

| ID | Задача | Статус | Приоритет |
|----|--------|--------|-----------|
| A1 | Native audio via cpal + Opus (P2P без браузера) | ⬜ | P3 |

---

## Блок 2 — Транспорт и сеть

| ID | Задача | Статус | Приоритет |
|----|--------|--------|-----------|
| T1 | Adaptive jitter между path0 / path1 (анти-burst-loss) | ⬜ | P2 |
| T2 | Multi-circuit parallel fetch (swarming, Range-requests) | ⬜ | P2 |
| T3 | Streaming reassembly partial-emit (убрать HoL blocking) | ⬜ | P2 |
| T4 | Triple-path при loss > порога | ⬜ | P3 |
| T5 | Mask-mode infrastructure (порты 443/UDP, TLS mimicry) | ⬜ | P3 |
| T6 | Dynamic wagon-size (distribution-mimicking) | ⬜ | P3 |
| T7 | Multi-anchor active-active fetch | ⬜ | P3 |

---

## Блок 3 — AI-mesh

| ID | Задача | Статус | Приоритет |
|----|--------|--------|-----------|
| AI1 | yandi-rpc spec + wire-протокол | ⬜ | P1 |
| AI2 | yandi-fetch CLI / SDK | ⬜ | P1 |
| AI3 | AI-inference proxy (Ollama / llama.cpp через yandi-rpc) | ⬜ | P2 |
| AI4 | LLM model discovery via DHT | ⬜ | P2 |
| AI5 | DHT-shared knowledge base (federated RAG) | ⬜ | P3 |
| AI6 | Sovereign personal cloud (single-user, QR pairing) | ⬜ | P2 |

---

## Блок 4 — Качество и надёжность

| ID | Задача | Статус | Приоритет |
|----|--------|--------|-----------|
| Q1 | Unit-тесты voice signaling (V4) | ⬜ | P1 |
| Q2 | Integration-тест P2P file delivery end-to-end | ⬜ | P1 |
| Q3 | Стресс-тест голосового WebRTC (2 ноды локально) | ⬜ | P1 |
| Q4 | Мониторинг: метрики звонков (длительность, drops, ICE-fail) | ⬜ | P2 |
| Q5 | Bufferbloat mitigation (+647ms под нагрузкой) | ⬜ | P2 |

---

## Критический путь (приоритет P1)

```
[V4 тесты голоса]
        │
        ▼
[VD3 + VD4 — видеозвонки бэк+фронт]
        │
        ▼
[VD5 + VD6 — звуки и UI видео]
        │
        ▼
[Q2 + Q3 интеграционные тесты]
        │
        ▼
[AI1 yandi-rpc] ──→ [AI2 yandi-fetch] ──→ [AI3 inference]
                                                  │
                                             [AI4 discovery]
                                                  │
                                             [AI5 federated RAG]
```

---

## История версий роадмапа

| Дата | Изменение |
|------|-----------|
| 2026-05-14 | Создан по итогам реализации F1–F3, V1–V3 |
| 2026-05-14 | S1, S2 — звуки онлайн/сообщений; VD3–VD6 — видеозвонки полностью |
