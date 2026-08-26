# YANDI — Текущее состояние (2026-05-14)

Снапшот того, что реализовано, что сломано, что не начато.  
Роадмап и приоритеты — в `YANDI_ROADMAP.md`.

---

## Чат и файлы

**Статус: работает**

- P2P чат (ChatMessage 0xA0) — доставка, история, уведомления браузера ✅
- Отправка файлов чанками (700 байт) через `/api/files/send-chunk` ✅
- Получатель собирает файл в `downloads/`, отправитель ждёт FileComplete ACK ✅
- Буфер приёма исправлен: 1024 → 65536 байт (`p2p/transport.rs:389`) ✅
- `/api/files/status` — проверка доступности файла на принимающей стороне ✅
- `watchAttachmentReady()` — JS-retry до 5 попыток при недоставке ✅

**Известные ограничения:**
- Нет unit-тестов доставки файлов (запланировано Q2)
- Максимум файла: 200 MB (browser-side ограничение)

---

## Голосовые звонки

**Статус: реализовано, требует полевого теста**

### Сигнальная цепочка (исправлена)

```
Звонящий                          Принимающий
────────                          ─────────────
startCall(peerId)
  → getUserMedia()
  → openSignalingWS(peerId)
  → POST /api/media/call/start
    → VoiceCallRequest P2P ──────→ main.rs signal loop
                                    → store_incoming_call()
  ← polling /api/media/incoming-call (3s)
  ← showIncomingCallModal()
  ← [пользователь: Принять]
  ← acceptCall(callId, fromShortId)
    ← getUserMedia()
    ← openSignalingWS(fromShortId)
    ← POST /api/media/call/{id}/accept
      ← VoiceCallAccept P2P ────→ WS → "call-accept"
createPeerConnection()
createAndSendOffer()
  → offer via WS/VoiceData P2P ──→ handleOffer()
                                    → createAnswer()
                                    ← answer via WS
handleAnswer()
ICE exchange (обе стороны)
WebRTC connected ✅
```

### Что реализовано

| Компонент | Файл | Статус |
|-----------|------|--------|
| P2P пакеты VoiceCall* | `p2p/packet.rs` | ✅ |
| Очередь входящих `incoming_calls` | `web/server.rs` AppState | ✅ |
| API start/accept/reject/end | `web/media_api.rs` | ✅ |
| VoiceCallRequest в main.rs signal loop | `main.rs` | ✅ |
| WebSocket signaling (VoiceData forwarding) | `web/media_api.rs` | ✅ |
| Polling + модалка входящего звонка | `ui/voice-call.js` | ✅ |
| WebRTC offer/answer/ICE | `ui/voice-call.js` | ✅ |
| Звук входящего: `calling.mp3` (loop) | `ui/voice-call.js` | ✅ |
| Звук исходящего: `call_out.mp3` (loop) | `ui/voice-call.js` | ✅ |
| DND-режим (🔕): звуки выкл, модалка остаётся | `ui/app.js` + `voice-call.js` | ✅ |
| Кнопка 📞 в хедере чата | `ui/chat.html` | ✅ |
| `window.myDisplayName` из профиля | `ui/chat.html` | ✅ |
| Unit-тесты | — | ❌ не написаны |

**Известные ограничения:**
- Только STUN (Google). При симметричном NAT с обеих сторон соединение может не установиться — нужен TURN
- Тестировалось только архитектурно; полевого теста между двумя реальными нодами не было

---

## Видеозвонки

**Статус: частично реализовано, нерабочее**

| Компонент | Статус | Проблема |
|-----------|--------|----------|
| P2P пакеты VideoCall* (0xC0–0xC3) | ✅ определены | не обрабатываются в main.rs |
| `video-call.js` WebRTC-скелет | 🔧 есть | timing bug: offer до accept |
| Очередь входящих видеозвонков | ❌ нет | аналог `incoming_calls` не создан |
| API `/api/media/video-call/*` | ❌ нет | эндпоинты отсутствуют |
| Polling входящих видеозвонков | ❌ нет | нет в `video-call.js` |
| Звуки видеозвонка | ❌ нет | |
| UI видео-превью в модалке | ❌ нет | |

Для починки нужен объём работы ~= V1 + V2 голосовых звонков.

---

## Транспорт и прокси

**Статус: работает, показатели из стресс-теста 2026-05-11**

| Метрика | Значение |
|---------|----------|
| Throughput через YANDI proxy | **78 Mbps** (против 9.8 Mbps прямого ISP-шейпа) |
| 8 параллельных загрузок | 78 Mbps суммарно, распределение равномерное |
| 20 параллельных загрузок | 78 Mbps суммарно, 90% success |
| UDP drops (после sysctl) | 0 |
| RSS ноды | 224 MB |
| Latency под нагрузкой | +647ms bufferbloat (известная проблема) |

**Конфигурация prod-хоста (обязательно):**
```bash
sudo sysctl -w net.core.rmem_max=67108864
sudo sysctl -w net.core.wmem_max=67108864
```

**Транспортный стек:**
- `netlayer::P2PTransport` (wagon FEC dual-path) — прокси/роутинг/circuits, порты 9000/9999
- `yandi::p2p::P2PTransport` (simple UDP dual-path) — чат/файлы/звонки, порты 9998/9001
- Dual-path: каждый пакет дублируется по path0 + path1, дедупликация через `packet_cache`

---

## AI-mesh

**Статус: не начат**

Все компоненты (yandi-rpc, AI-inference, DHT knowledge base) — в `FUTURE_IDEAS.md` и `YANDI_ROADMAP.md`.

---

## Файлы плана и состояния

| Файл | Содержание |
|------|-----------|
| `YANDI_ROADMAP.md` | Роадмап с приоритетами (P1/P2/P3) |
| `CURRENT_STATE.md` | Этот файл — снапшот состояния |
| `COMM_IMPROVEMENT_PLAN.md` | Детальный план F1–A1 итераций |
| `COMM_IMPROVEMENT_STATUS.md` | Чеклист выполнения F1–V3 |
| `NETWORK_EVOLUTION_PLAN.md` | Hardening Cycle (закрыт) |
| `FUTURE_IDEAS.md` | Расширенный бэклог: транспорт + AI-mesh |
| `YANDI_VISION.md` | Стратегическое видение проекта |
