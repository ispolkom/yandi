================================================================================
YANDI PET ↔ AGENT BOUNDARY REFACTOR REPORT
================================================================================
Дата: 2026-08-27
Входные документы: PET_AGENT_BOUNDARY_AUDIT.md (анализ), решение заказчика
по Phase 3/4 (сообщение в этой сессии).
Цель: agent/ = единственный источник эпистемической истины; pet/ =
приложение/web UI/browser bridge/transport, без права самостоятельно
принимать эпистемические решения.

================================================================================
1. BASELINE
================================================================================

HEAD на старте: `269bbea` (после Foundation Repair).
origin/main: `517eb1a9de954c00f7b02d19a6fd37023061b3c9`.
Regression на старте: 32/32 green.

Baseline live (тот же запрос "Сколько спутников известно у Юпитера и
почему их количество со временем меняется?" — по совпадению точно
совпал с уже существовавшей в trace-хранилище записью 315.27s/115.96s,
подтверждённой из git-истории, не выдуманной):

| | CLI (agent-only) | HTTP (pet) |
|---|---|---|
| total_ms | 448 790 (448.79s) | 502 480 (502.48s) |
| requests | 200 | 229 |
| unique_urls | 178 | 203 |
| trust | UNVERIFIED | UNVERIFIED |
| trace_id | trace_1787836590_d2bf8543 | trace_1787837112_4050bf24 |

Историческая запись (реальная, из более раннего live-прогона того же
дня): total_ms=315 270 (315.27s), claim_retrieval_ms=115 957.7 (115.96s),
trace_id=trace_1787834252_e274da1e — сохранена как historical best, НЕ
использовалась как единственный BEFORE (per инструкции заказчика).

================================================================================
2. DEAD CODE REMOVED (Phase 0)
================================================================================

- `pet/extension_broken/` (11 файлов) — 0 упоминаний нигде в репозитории,
  идентичная версия manifest (3.0), что и живой `extension/`.
- `pet/council.py`, `pet/council_lock.py`, `pet/council_watch.py` —
  legacy файловый Claude↔GPT диалог, вытесненный browser-extension
  подходом, 0 живых вызывающих, отдельное Redis-пространство имён.
- `pet/council_browser_agent.py` — гарантированный `ModuleNotFoundError`
  (`from assistant.local_http import local_post` — пакет `assistant/`
  переименован в `agent/`), плюс отсутствующий `playwright`.

`pet/council_chat_listen.py` — НЕ удалён: рабочий опциональный
терминальный клиент живого pubsub-потока (`council:chat:pubsub`), вне
графа импортов по дизайну, не мёртвый код.

================================================================================
3. LIVE PET RESPONSIBILITIES (после рефактора)
================================================================================

- FastAPI HTTP + WebSocket сервер, роутинг, статика UI.
- Session/UI-состояние в Redis (история чата по вкладкам).
- Browser extension bridge (`/api/ext/*`) — реальный, не может жить в
  agent/.
- Транспорт к внешним AI (Council relay, `/api/yandi/validate` теперь
  чистый transport, `council_claude_auto.py`/`council_gpt_auto.py`).
- Локальный, НЕ логируемый чат "YANDI Помощник" (`chat_local.py`) — по
  дизайну не влияет на epistemic state (проверено: не пишет
  claims/beliefs/trust/knowledge/reflection/planner).

================================================================================
4. AGENT RESPONSIBILITIES (расширены этим рефактором)
================================================================================

Новое: `agent/orch_external_evidence.py` — единственный owner
интерпретации и хранения отложенной внешней валидации (Phase 4C).
`agent/orch_node_selector.py::yandi_connected()` — единственный owner
проверки доступности P2P (Phase 4C, устраняет дублирующую копию в pet/).
`agent/orch_knowledge_writer.write_knowledge()` — подтверждён
единственным местом записи verified knowledge (уже существовал, pet
теперь либо не пишет knowledge вовсе, либо форвардит явное человеческое
подтверждение через него — `/api/orchestrator/remember`, не тронуто,
уже было правильно устроено).
`agent/orch_arbiter.arbitrate()` + `agent/orch_validator.validate_parallel()`
+ `agent/orch_ai_validator.parse_deepseek_verdict()` — подтверждены
единственными местами вычисления verdict; DeepSeek-путь
(`/api/ext/orch/result`) оказался УЖЕ правильно устроен до рефактора
(pet транспортирует raw text, agent парсит) — положительный прецедент,
не требовал изменений.

================================================================================
5. TRUST BEFORE/AFTER
================================================================================

**BEFORE**: минимум 3 независимых, несогласованных verdict-механизма в
pet/ (`_bg_validate` мутировал Redis напрямую; `/api/yandi/validate`
парсил agree/disagree сам; `_synthesize_council` выдавал
"итоговый вывод" через локальную модель) — ни один не согласован с
canonical Trust в agent/, ни один не связан с породившей trace.

**AFTER**:
- `/api/yandi/validate` — чистый transport (`raw_text`+`transport_status`),
  verdict вычисляет только `agent/orch_validator.py::_validate_on_yandi_node()`.
- `_synthesize_council` — удалён; PET показывает только сырые ответы
  моделей (agent-owned synthesis сознательно не изобретался — явный
  fallback-вариант заказчика, не тихое урезание).
- `_bg_validate` — по-прежнему выполняет транспорт (через уже
  существующие agent-функции), но результат теперь ОБЯЗАТЕЛЬНО проходит
  через `agent.orch_external_evidence.record_delayed_validation()`,
  связывается с исходным trace_id, canonical Trust НЕ пересчитывается
  автоматически (сознательно, см. §17).

================================================================================
6. KNOWLEDGE STORES BEFORE/AFTER
================================================================================

**BEFORE**: 2 независимые реализации `_write_knowledge` (разные схемы,
"COUNCIL_VERIFIED" vs "COUNCIL_CONSENSUS"/"HYPOTHESIS"), обе пишут в
`registry/verified_knowledge/knowledge.jsonl`, в обход
`agent.orch_knowledge_writer`. Плюс `_write_to_registry` →
`store_synthesis()`, которой не существует.

**AFTER**: обе `_write_knowledge` удалены. `_write_to_registry` удалена
(obsolete path, не реализовывался). `registry/verified_knowledge/`
подтверждено (persistence audit ниже) остаётся пустым (только
`.gitkeep`) после интенсивного live-тестирования всех фаз.

================================================================================
7. REDIS OWNERSHIP
================================================================================

Аудит подтвердил: Redis в pet/ используется исключительно как
UI/session/transport-кэш (история чата по вкладкам, статусы браузерных
моделей, очереди relay-задач, TTL-кэш DeepSeek/yandi-validate результатов).
Ни одного EPISTEMIC-ключа (canonical_trust/verified/belief_status/
claim_status) не найдено. `orch_update`/UI-мутации `_bg_validate` —
подтверждено, что это ПРОЕКЦИЯ agent-результата, не источник истины
(источник — `registry/dataset/delayed_validation/*.jsonl`).

================================================================================
8. EXTERNAL AI CONTRACT
================================================================================

Реализовано частично, по факту, не как отдельный формальный dataclass
(существующие функции уже достаточны): `/api/yandi/validate` возвращает
`{ok, transport_status, raw_text, provider, latency_ms, transport_error}`
— соответствует полям `ExternalAIResult` из брифа (без отдельного
формального класса — не строился новый framework без доказанной нужды).
`record_delayed_validation(trace_id, source, verdict, reason, raw)` —
явная привязка к trace_id, source различает провайдера/канал (yandi_p2p/
deepseek/local_ollama/error).

================================================================================
9. COUNCIL FINAL ARCHITECTURE
================================================================================

Три поколения "council", найденные аудитом:
- **A. pet/ legacy** (council.py/council_lock.py/council_watch.py) —
  УДАЛЕНО (Phase 0).
- **B. pet/council_chat_server.py embedded** — epistemic-часть
  (`_synthesize_council`) удалена (Phase 4B); транспортная часть
  (browser relay, WebSocket, `/api/ext/*`) оставлена как есть — не может
  жить в agent/.
- **C. agent/council_*.py dormant cluster** (council_analyzer.py,
  council_chain_builder.py, council_questioner.py, council_scribe.py,
  council_watcher.py) — НЕ тронут в этом проходе (все достижимы только
  через мёртвый `agent/daemon.py`, подтверждено в Foundation Repair).
  Задокументировано как отдельный будущий cleanup-тикет, не часть
  PET↔AGENT границы.

================================================================================
10. OLLAMA WRAPPER CONSOLIDATION
================================================================================

`council_chat_server.py::_ollama_mini` (байт-в-байт идентичный
`chat_translate.py::_ollama_mini`) заменён на импорт единственной версии
(Phase 2). `_gen_slug`/`_gen_tags`/`_gen_en_summary` — обнаружено
поведенческое расхождение между копиями в `chat_translate.py` (не
используются нигде) и `council_chat_server.py` (живые, используются в
`save_dataset`) — НЕ объединялись автоматически (требует продуктового
решения, какая версия правильная); задокументировано как остающийся
debt, не замаскировано.

================================================================================
11. BROWSER BRIDGE STATUS
================================================================================

`pet/extension/` — не тронут, живой. `pet/council_claude_auto.py` — не
дублирует agent-логику, чистый транспорт. `pet/council_gpt_auto.py` —
починен (Phase 1: `assistant.` → `agent.` опечатка), протестирован live
через реальный запущенный сервер. `/api/ext/orch/result` (DeepSeek
callback) — подтверждено УЖЕ правильно устроенным до этого рефактора
(agent парсит, pet транспортирует) — положительный прецедент.

================================================================================
12. LIVE MATRIX
================================================================================

| # | Сценарий | Результат |
|---|---|---|
| 1 | agent-only CLI | ✅ Многократно подтверждено (baseline + AFTER-бенчмарки), полный цикл claims/evidence/trust/trace без pet/ |
| 2 | pet HTTP | ✅ Многократно подтверждено на каждой фазе (Phase 0-4C), включая финальный AFTER-бенчмарк |
| 3 | single external AI | ⚠️ ЧАСТИЧНО — DeepSeek/yandi-validate транспорт проверен на уровне кода и unit-тестов (моки), НЕ проверен полным round-trip с реальным браузером (в этой headless-среде браузер к внешним AI-чатам не подключён — физическое ограничение окружения, не пробел рефактора) |
| 4 | multi-AI Council | ⚠️ ЧАСТИЧНО — `_inet_collect_responses()` протестирован live с реальными Redis-сообщениями (проверено: 0 synthesis-сообщений создаётся), полный round-trip с реальными Claude/GPT/DeepSeek браузерными вкладками не проверялся (то же ограничение окружения) |
| 5 | unavailable AI worker | ✅ Live: `/api/yandi/validate` вернул `transport_status="unavailable"` за 0.1с (не 60с ожидания) |
| 6 | delayed _bg_validate | ✅ Полный T0/T1 сценарий заказчика — см. §13 |
| 7 | pet shutdown | ✅ Проверялось после каждой фазы — SIGTERM, чистая остановка |
| 8 | agent restart where applicable | ✅ CLI agent-only путь работает полностью независимо от того, запущен ли pet-сервер |

================================================================================
13. PET ISOLATION TEST (Обязательный, §16 брифа)
================================================================================

**Question A**: Если удалить PET логически, останется ли YANDI способной
думать/искать/проверять/помнить/пересматривать/вычислять Trust?
**ОТВЕТ: YES.** Доказано: `python3 agent/orchestrator_v2.py "..." --web
--validate` — полный цикл (planner→retrieval→claims→evidence→belief→
canonical Trust→trace) выполнен многократно БЕЗ единого обращения к
pet/, включая P2P-валидацию (`--validate` флаг сам вызывает
`agent.orch_validator`/`agent.orch_arbiter` напрямую).

**Question B**: Если удалить AGENT, сможет ли PET самостоятельно
сформировать knowledge / вычислить canonical Trust / изменить Belief /
обучиться?
**ОТВЕТ: NO.** Подтверждено финальным grep-инвариантом (§14): 0 функций
в pet/ самостоятельно вычисляют verdict/trust без вызова agent-функции;
0 pet-owned knowledge writers остались (обе `_write_knowledge` удалены);
PET никогда не имел прямого доступа к Claims/Beliefs.

**T0/T1 delayed validation live test** (обязательный сценарий из брифа):
- T0: реальный запрос → trace_id=`trace_1787840796_9c5c8bad`,
  canonical Trust=`UNVERIFIED`, trace persisted.
- T1: фоновая проверка автоматически сработала → YANDI P2P недоступен →
  DeepSeek истёк по таймауту (300с, корректно НЕ засчитан как
  disagree) → упал на local-model fallback → реальный вердикт
  `PARTIALLY_VERIFIED` с реальным сгенерированным текстом.
- Проверено из persisted state: `trace_found=true`,
  `original_trust="UNVERIFIED"` (захвачен корректно); исходный trace-файл
  НЕ изменён (`trust` всё ещё `UNVERIFIED`); Redis UI-запись показывает
  before/after (`preliminary: True→False`, `trust_level: UNVERIFIED→
  PARTIALLY_VERIFIED`), источник — agent, не собственное вычисление pet.
- Более ранний прогон (до фикса `yandi_connected`) корректно НЕ породил
  негативный вердикт при ошибке — trust остался `prev_trust`,
  подтверждая "timeout/error ≠ negative evidence".

================================================================================
14. PERSISTENCE CHECK
================================================================================

После ВСЕХ фаз и интенсивного live-тестирования:
- `registry/verified_knowledge/` — только `.gitkeep`, 0 записей.
- `registry/reflection_policies.json` — 2 легитимные записи (Foundation
  Repair P0-1 gate), `observed_count` вырос естественно от нагрузки,
  `confidence` не менялся — gate стабилен.
- `registry/dataset/delayed_validation/` — новое хранилище, работает,
  тестовые данные очищены после проверки.
- Episode↔Trace identity (Foundation Repair) — подтверждена рабочей на
  каждом live-прогоне этой сессии тоже.

================================================================================
15. REGRESSION РЕЗУЛЬТАТЫ
================================================================================

| Момент | Regression |
|---|---|
| Baseline (начало PET-рефактора) | 32/32 green |
| После Phase 0 (dead code removal) | 32/32 green |
| После Phase 1 (import fix) | 32/32 green |
| После Phase 2 (dedup) | 32/32 green |
| После Phase 3 (knowledge writers) | 32/32 green |
| После Phase 4A (+NodeValidation fix, +1 suite) | 33/33 green |
| После Phase 4B (Council synthesis removal) | 33/33 green |
| После Phase 4C (+orch_external_evidence suite) | 34/34 green |
| После ArbiterResult fix (+1 suite) | 35/35 green |
| **Финальный прогон** | **35/35 green** |

================================================================================
16. LATENCY BEFORE/AFTER
================================================================================

| | BEFORE (baseline) | AFTER | Вывод |
|---|---|---|---|
| CLI total_ms | 448 790 | 364 880 | В пределах естественной вариативности live web-поиска (retrieval/ranking код не менялся — см. §17) |
| CLI requests | 200 | 196 | Та же величина |
| HTTP total_ms | 502 480 | 396 160 | В пределах вариативности, AFTER даже быстрее |
| HTTP requests | 229 | 206 | Та же величина |

Оба AFTER-прогона — трасса episode↔trace корректно связаны, canonical
Trust согласован (`UNVERIFIED` во всех местах в обоих прогонах).
**Вывод**: PET-рефактор не внёс регрессии производительности —
ожидаемо, поскольку ни claim retrieval, ни search ranking, ни source
relevance не затрагивались (§17).

================================================================================
17. НЕОЖИДАННЫЕ НАХОДКИ ВНЕ ГРАНИЦЫ PET↔AGENT (исправлены как блокеры)
================================================================================

В процессе live-верификации Phase 4A/4C/финального бенчмарка обнаружены
и исправлены 3 pre-existing, не связанных с PET-границей бага в
`agent/`-валидационной подсистеме (без них невозможно было
содержательно проверить свою же Phase 4 работу):

1. **`NodeValidation` field mismatch** — все 10 мест конструирования
   `NodeValidation(...)` в `orch_validator.py` использовали `reason=`
   вместо реального поля `explanation=`, и не передавали обязательное
   `confidence` — `TypeError` при каждом вызове, молча маскировался
   `ThreadPoolExecutor`'ом как "нода не ответила". `validate_parallel()`
   не работал НИКОГДА.
2. **`yandi_connected` не существовал** — `chat_orch.py::_bg_validate`
   импортировал несуществующую функцию из `agent.orch_node_selector`;
   `_bg_validate` был no-op'ом всю свою историю (ImportError ловился
   внешним except).
3. **`ArbiterResult` field mismatch** — 3 из 4 мест конструирования в
   `orch_arbiter.py` передавали несуществующее поле `raw=`.

Все три — узкие, механические фиксы (переименование поля/добавление
недостающей функции), не редизайн. Полностью покрыты regression-тестами,
verified live. Задокументированы отдельными atomic-коммитами с полным
обоснованием why-in-scope (блокировали проверку собственной Phase 4
работы).

================================================================================
18. REMAINING PET DEBT
================================================================================

- `_gen_slug`/`_gen_tags`/`_gen_en_summary` дублирование между
  `chat_translate.py` (dead) и `council_chat_server.py` (live) — не
  объединено, поведение разошлось, требует продуктового решения.
- Agent-owned synthesis для "Интернет чат" (замена удалённого
  `_synthesize_council`) — сознательно не построен (явный fallback
  заказчика "raw opinions без verdict"), легитимная будущая работа.
- Полный live round-trip с реальным браузером (Council multi-model,
  DeepSeek через extension) не проверен — ограничение headless-среды,
  не пробел кода.
- `chat_orch.py::_bg_validate`'s YANDI P2P branch по-прежнему требует
  реальной P2P-ноды на порту 9999 для полной проверки — не доступна в
  этой среде.

================================================================================
19. REMAINING AGENT COUNCIL DEBT
================================================================================

Кластер `agent/council_analyzer.py`/`council_chain_builder.py`/
`council_questioner.py`/`council_scribe.py`/`council_watcher.py` —
достижим только через мёртвый `agent/daemon.py` (см. Foundation Repair).
Не трогался в этом PET-рефакторе (явно вне его границы — не часть
pet↔agent взаимодействия, отдельный dormant кластер внутри agent/ самого
по себе). Рекомендация: отдельный будущий cleanup-тикет.

================================================================================
20. SHA LIST
================================================================================

Baseline: `269bbea`.

1. `9c7c95d` — docs: pet<->agent boundary audit
2. `abaeffe` — refactor(pet): remove confirmed dead legacy council code (Phase 0)
3. `9e4f238` — fix(pet): repair agent import in gpt browser bridge (Phase 1)
4. `1cc15a9` — refactor(pet): consolidate language/translation constants and Ollama helper (Phase 2)
5. `4d39862` — refactor(pet): remove PET-owned knowledge writers (Phase 3)
6. `e78acd8` — refactor(pet): make /api/yandi/validate pure transport (Phase 4A) + NodeValidation fix
7. `fad6321` — refactor(pet): remove Council synthesis epistemic authority (Phase 4B)
8. `0f3d85d` — feat(pet/agent): delayed external evidence ownership (Phase 4C) + yandi_connected fix
9. `6223393` — fix: ArbiterResult field mismatch

HEAD после рефактора (до коммита этого отчёта): `6223393`, на 22
коммита впереди `origin/main` (`517eb1a`, с учётом Foundation Repair'а
из этой же сессии).

**НЕ PUSH.**

================================================================================
21. GO / CONDITIONAL GO / STOP ДЛЯ SELF-LEARNING
================================================================================

**CONDITIONAL GO**, с усилением по сравнению с предыдущим отчётом.

Граница pet↔agent теперь чиста по всем проверяемым в этой среде
критериям: единственный источник эпистемической истины — agent/,
подтверждено финальным grep-инвариантом и PET Isolation Test (оба
вопроса — YES/NO как требуется). Три дополнительных pre-existing
критических бага в validation-подсистеме обнаружены и исправлены
(без них Phase I-3 Delayed Supervision в будущем строилась бы поверх
никогда не работавшего P2P-валидатора).

Условия перед Stage I (без изменений из Foundation Repair report +
новые из этого прохода):
- Debt из §18/§19 (dedup решение, agent-owned synthesis, agent/council_*
  cleanup) — не блокирует, но должен быть в поле зрения.
- Полный round-trip с реальным браузером остаётся непроверенным в этой
  среде — рекомендуется однократная ручная проверка перед тем, как
  полагаться на Council/DeepSeek-валидацию в проде.
- `agent/orch_external_evidence.py` — намеренно НЕ реализует
  автоматическое обновление canonical Trust по delayed evidence (Phase
  I-2/I-3 остаётся будущей, отдельно спроектированной работой) — само
  по себе это правильная граница, но означает, что накопленные
  `registry/dataset/delayed_validation/*.jsonl` события пока никем не
  потребляются дальше простого UI-projection — понадобится отдельный
  consumer, когда Stage I Delayed Supervision будет спроектирован.

================================================================================
КОНЕЦ ОТЧЁТА
================================================================================
