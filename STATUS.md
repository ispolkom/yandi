# STATUS — QueryFrame integration plan
_Дата: 2026-05-18 | Основа: рекомендации Claude + DeepSeek_

---

## Что сделано (фундамент)

- [x] `orch_query_framer.py` — разбивает запрос на матрицу: object, action, constraints, missing, search_queries, clarifying_question
- [x] `decide_policy()` — детерминированная, 10/10 тестов, safe_domain через подстроку
- [x] `_auto_cq()` — fallback уточняющий вопрос если LLM не дал
- [x] `chat_orch.py` — ветка ask_first/safe_general, не запускает оркестратор
- [x] Буфер inet 180с + `_inet_collect_responses()`

---

## ПЛАН (текущая сессия)

### Шаг 1 — search_queries → оркестратор  [КРИТИЧНО]
**Проблема:** QueryFrame генерирует `search_queries` контекстные (с missing-слотами),
но `orchestrator_v2.py` игнорирует их и вызывает `formulate_queries()` заново от нуля.
**Изменения:**
- `orch_schemas.py`: добавлено `search_queries: list[str] = []` в `OrchestratorRequest`
- `chat_orch.py`: передаёт `frame.search_queries` в `OrchestratorRequest`
- `orchestrator_v2.py`: если `request.search_queries` есть — пропускает `formulate_queries()`
**Статус:** [x] DONE ✅

---

### Шаг 2 — cq в ответ (мягкое уточнение)  [Claude]
**Проблема:** `clarifying_question` генерировалась, но терялась для `answer_with_assumptions`.
**Изменения:**
- `chat_orch.py`: если `frame.answer_policy in ("answer_with_assumptions","answer_direct")`
  и есть `clarifying_question` и `missing` — добавляет `💬 *cq*` в конец ответа
**Статус:** [x] DONE ✅

---

### Шаг 3 — Redis frame logging (авто-датасет)  [Claude]
**Проблема:** QueryFrame живёт только в памяти — нет трассировки решений, нет датасета.
**Изменения:**
- `chat_orch.py`: `await r.setex(f"council:frame:{msg_id}", 3600, json.dumps(frame.to_dict()))`
  TTL 1 час. Ключ: `council:frame:{msg_id}`
**Статус:** [x] DONE ✅

---

### Шаг 4 — frame в meta knowledge.jsonl  [GPT]
**Проблема:** При сохранении в реестр (`📌 Запомнить`) frame не писался в meta.
**Изменения:**
- `chat_orch.py` `/api/orchestrator/remember`: достаёт frame из Redis по `council:frame:{msg_id}`,
  передаёт как `meta.query_frame` в `write_knowledge()`
**Статус:** [x] DONE ✅

---

## Итог сессии
Все 4 шага реализованы. Сервер запущен на порту 9010.

**Что теперь работает:**
- Поиск в интернете использует умные запросы из QueryFrame (с missing-слотами) вместо generic
- Для answer_with_assumptions ответ завершается уточняющим вопросом (`💬 *...*`)
- Каждый QueryFrame пишется в Redis `council:frame:{msg_id}` TTL 3600 — авто-датасет
- При `📌 Запомнить` в knowledge.jsonl пишется полный frame в `meta.query_frame`

**Следующие возможные шаги:**
- Endpoint `/api/frame/export` — выгрузка датасета из Redis за период
- Добавить `trust_penalty: true` в синтезатор при наличии `missing`

---

## Фикс веб-скрапера (2026-05-18)

**Проблема:** когда trafilatura не могла извлечь текст со страницы (JS-сайт, paywall),
скрапер молча падал на DDG-сниппет — кэшированный фрагмент из поиска, потенциально устаревший.

**Изменения в `agent/orch_web_scraper.py`:**
- [x] Убран fallback `raw = item.get("body", "")` → теперь `return None` если trafilatura пустая
- [x] `MAX_CHARS` 2000 → 3500 (хорошие страницы больше не обрезаются)
- [x] `_clean_text` порог 40 → 20 символов (сохраняем bullet-points и короткие факты)
- [x] `line.isupper()` теперь с защитой `len(line) > 6` (аббревиатуры типа "API", "DDG" не удаляются)

---

## Orch AI Validator — изолированный канал DeepSeek (2026-05-18)

**Redis-ключи (префикс `orch:ai:`):**
- `orch:ai:queue` — очередь задач
- `orch:ai:result:{task_id}` — результат (TTL 10 мин)
- `orch:ai:log` — лог валидаций для датасета (500 записей)

**Файлы:**
- [x] `agent/orch_ai_validator.py` — push / get / parse / log
- [x] `pet/council_chat_server.py` — `/api/ext/orch/poll` + `/api/ext/orch/result` + `/api/orch/ai/log`
- [x] `pet/extension/background.js` — `pollOrch()` второй изолированный цикл
- [x] `pet/chat_orch.py` — `_bg_validate()` использует orch:ai канал

**Поток:**
```
Ответ пользователю (preliminary)
  → push orch:ai:queue
  → Firefox extension polling /api/ext/orch/poll
  → DeepSeek (та же вкладка, тот же прокси)
  → /api/ext/orch/result → parse → trust апдейт
  → log в orch:ai:log (будущий датасет)
```
