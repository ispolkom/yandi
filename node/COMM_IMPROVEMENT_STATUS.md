# YANDI Comm Improvement Status

**Last updated:** 2026-05-14  
**Plan:** `COMM_IMPROVEMENT_PLAN.md`

---

| Iter | Subject                         | Status    |
|------|---------------------------------|-----------|
| F1   | Fix receive buffer (1024→65536) | ✅ done   |
| F2   | File delivery status endpoint   | ✅ done   |
| V1   | Voice call signaling backend    | ✅ done   |
| V2   | Voice call frontend             | ✅ done   |
| A1   | Native audio P2P (cpal+Opus)    | ⬜ P2     |

---

## Iter F1 details

- [x] `src/p2p/transport.rs` — буфер 1024 → 65536
- [x] `cargo check` — OK

## Iter F2 details

- [x] `src/web/server.rs` — `GET /api/files/status/{file_id}/{filename}` (api_files_status handler)
- [x] `src/web/ui/chat.html` — watchAttachmentReady() polling при 404 на вложение
- [x] `cargo check` — OK

## Iter V1 details

- [x] `src/web/server.rs` — `incoming_calls: Arc<Mutex<HashMap<String, IncomingCallInfo>>>` в AppState
- [x] `src/web/media_api.rs` — `start_call` отправляет VoiceCallRequest P2P, возвращает call_id
- [x] `src/main.rs` — VoiceCallRequest в media_signal_rx loop → store_incoming_call()
- [x] `src/web/media_api.rs` — `GET /api/media/incoming-call`
- [x] `src/web/media_api.rs` — `POST /api/media/call/{call_id}/accept` (отправляет VoiceCallAccept P2P)
- [x] `src/web/media_api.rs` — `POST /api/media/call/{call_id}/reject` (отправляет VoiceCallReject P2P)
- [x] `src/web/media_api.rs` — `POST /api/media/call/end` (отправляет VoiceCallEnd P2P)
- [x] `cargo check` — OK
- [ ] Тесты: incoming call queue + voice packet format (TODO)

## Iter V2 details

- [x] `src/web/ui/voice-call.js` — incoming call polling (checkIncomingCalls / showIncomingCallModal)
- [x] `src/web/ui/voice-call.js` — acceptCall / rejectCall с полным flow
- [x] `src/web/ui/voice-call.js` — `call-accept` signal: caller отправляет SDP offer после accept
- [x] `src/web/ui/chat.html` — кнопка 📞 в chat header (headerCallBtn)
- [x] `src/web/ui/chat.html` — window.myDisplayName устанавливается из профиля
- [x] `cargo check` — OK

## Pending / Next

- Tests for V1: `test_incoming_call_queue`, `test_voice_packet_format` (Rust unit tests)
- Iter A1 (P2): Native CLI audio via cpal+Opus — future work
