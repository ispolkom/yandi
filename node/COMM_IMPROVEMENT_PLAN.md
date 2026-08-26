# YANDI Comm Improvement Plan — File Delivery & Voice

**Created:** 2026-05-14  
**Source of truth for:** file transfer reliability + voice/audio calling  
**Current status file:** `COMM_IMPROVEMENT_STATUS.md`

---

## Scope

Проект уже имеет:
- Чат: работает ✅
- Прокси (HTTP+SOCKS5): работает ✅  
- Файловая передача: реализована, но есть надёжность-проблемы ⚠️
- Голосовые звонки: сигнализация WebRTC реализована частично, нет входящего вызова ❌

Два транспорта:
- `netlayer::transport` (wagon FEC dual-path) — для прокси и routing
- `p2p::P2PTransport` (UDP dual-path, порты 9998/9001) — для чата, файлов, голоса

---

## Iter F1 — Fix receive buffer (P0, ~10 мин)

**Проблема:** `receive_loop` в `src/p2p/transport.rs` использует буфер 1024 байт при заявленном MTU 65536. Для файловых чанков (~792 байт) хватает, но это fragile и может резать большие пакеты (длинные текстовые сообщения, сообщения с metadata).

**Фикс:**
- `src/p2p/transport.rs:389` — изменить `vec![0u8; 1024]` → `vec![0u8; 65536]`

**Тест:** `cargo check`

---

## Iter F2 — File delivery status endpoint (P1, ~30 мин)

**Проблема:** Получатель не знает, дошёл ли файл (нет HTTP endpoint для проверки).  
Когда chat-сообщение с `file_ref` приходит, браузер сразу пытается открыть `/api/files/content/{file_id}/{filename}`. Если P2P transfer ещё не завершён (race condition) — 404.

**Фикс:**
- Добавить `GET /api/files/status/{file_id}/{filename}` → `{exists: bool, ready: bool}`
- Добавить retry-логику в JS: при 404 на файл — ретрай 3 раза через 1/2/3с
- Добавить `X-Transfer-Status` header в `api_files_content` (200 = ok, 202 = still_transferring, 404 = not_found)

**Тест:** unit-тест для `resolve_local_file_path`

---

## Iter V1 — Voice call signaling backend (P1, ~2 часа)

### Архитектура

```
Caller browser  →  /api/media/call/start  →  VoiceCallRequest P2P  →  Callee server
                                                                      ↓
Callee browser  ←  /api/media/incoming-call (polling)  ←  incoming_calls queue
                                                                      
Callee browser  →  /api/media/call/accept  →  VoiceCallAccept P2P  →  Caller server
                                                                      ↓
Caller browser  ←  WS signal (call-accept)  ←  media_signal_bus
Caller browser  →  WS: SDP offer  →  VoiceData P2P  →  Callee server
                                                      ↓
Callee browser  ←  WS signal (offer)  ←  media_signal_bus
Callee browser  →  WS: SDP answer  →  VoiceData P2P  →  Caller server
                                                      ↓
Caller browser  ←  WS signal (answer)
ICE candidates: same VoiceData P2P path (bidirectional)
WebRTC audio: browser-to-browser via ICE/STUN
```

### Signaling message format (JSON text payload)

All signaling messages (VoiceCallRequest, VoiceCallAccept, VoiceCallEnd, VoiceCallReject, VoiceData)
are JSON text payloads forwarded through `media_signal_bus`.

| Packet type       | JSON payload                                             |
|-------------------|----------------------------------------------------------|
| VoiceCallRequest  | `{"type":"call-request","call_id":"...","display_name":"...","from_short_id":"..."}`  |
| VoiceCallAccept   | `{"type":"call-accept","call_id":"..."}`                |
| VoiceCallReject   | `{"type":"call-reject","call_id":"..."}`                |
| VoiceCallEnd      | `{"type":"hangup","call_id":"..."}`                     |
| VoiceData         | `{"type":"offer"/"answer"/"ice-candidate", ...}`        |

### Backend changes

1. `AppState` — добавить `incoming_calls: Arc<Mutex<HashMap<String, IncomingCallInfo>>>`  
   где ключ = call_id, значение = `{call_id, from_short_id, from_display_name, received_at}`

2. `start_call` API:  
   - Принять `peer_id`, `display_name`  
   - Сгенерировать `call_id`  
   - Послать `VoiceCallRequest` P2P пакет с JSON payload  
   - Вернуть `{call_id, status: "ringing"}`

3. Обработка `VoiceCallRequest` в `media_signal_rx` loop (main.rs):  
   - Распарсить JSON payload  
   - Добавить в `incoming_calls`

4. `GET /api/media/incoming-call` → вернуть oldest pending call или `null`

5. `POST /api/media/call/accept/{call_id}`:  
   - Послать `VoiceCallAccept` P2P пакет  
   - Удалить из `incoming_calls`  
   - Вернуть OK

6. `POST /api/media/call/reject/{call_id}`:  
   - Послать `VoiceCallReject` P2P пакет  
   - Удалить из `incoming_calls`  
   - Вернуть OK

7. `POST /api/media/call/end` → послать `VoiceCallEnd` P2P + закрыть состояние

### Тесты

- `test_incoming_call_queue` — добавить/извлечь call из очереди
- `test_voice_packet_format` — JSON encode/decode VoiceCallRequest

---

## Iter V2 — Voice call frontend (P1, ~2 часа)

### voice-call.js изменения

1. `startPollingIncomingCalls()` — poll `/api/media/incoming-call` каждые 3с  
2. Показать incoming call modal при ответе сервера  
3. `acceptCall(callInfo)`:  
   - `getUserMedia({audio: true})`  
   - Открыть WS к `/api/media/ws/{callInfo.from_short_id}`  
   - POST `/api/media/call/accept/{call_id}`  
   - Ждать WS signal `call-accept` (от caller)  
   - Когда caller шлёт offer → `handleOffer` → create answer → send via WS
4. `rejectCall(callInfo)`: POST reject
5. `handleSignaling` — добавить обработку `call-accept`: caller создаёт offer после accept

### chat.html изменения

- Кнопка вызова 📞 в chat header рядом с именем контакта
- Incoming call notification banner (вверху экрана)

---

## Iter A1 — Native audio P2P (P2, в следующем цикле)

Текущая реализация: WebRTC через браузер (внешние STUN серверы Google).  
Будущая: опциональный native Opus/cpal P2P audio через VoiceData пакеты.

- `src/media/audio_capture.rs` — уже есть cpal AudioCapture/AudioPlayback  
- `src/media/codecs/opus.rs` — Opus codec  
- Нужна интеграция: capture → encode → VoiceData P2P → decode → playback  
- Полезно для CLI-режима (без браузера)

---

## Wire format — занятые байты (обновление)

```
0xA0-0xAF: Chat (ChatMessage, ChatAck, ChatRead, ChatTyping, ChatDeleteMessage)
0xB0: VoiceCallRequest
0xB1: VoiceCallAccept
0xB2: VoiceCallEnd
0xB3: VoiceCallReject
0xB4: VoiceData (SDP offer/answer/ICE candidate + future native audio)
0xC0: VideoCallRequest
0xC1: VideoCallAccept
0xC2: VideoCallEnd
0xC3: VideoCallReject
0xC4: VideoData
0xD0: FileTransferStart
0xD1: FileChunk
0xD2: FileTransferEnd
0xD3: FileTransferCancel
0xD4: FileMissing
0xD5: FileComplete
```

Netlayer circuit bytes (отдельный namespace, не конфликтует):
```
0xB0-0xB3: PKT_CIRCUIT_BUILD/EXTEND/DATA/CLOSE
0xB4: PKT_CIRCUIT_EXTEND_REPLY (telescoping DH, Hardening Step 7)
```

---

## Known limitations (после реализации)

- WebRTC voice требует внешний STUN (Google stun.l.google.com) для cross-NAT  
- Нет TURN relay для symmetric NAT  
- Video calling — endpoint'ы есть, имплементация не начата  
- Native CLI audio (cpal) — P2 приоритет
