================================================================================
YANDI SELF-LEARNING FOUNDATION REPAIR REPORT
================================================================================
Дата: 2026-08-27
Скоуп: /home/iam/yandi/agent/ ТОЛЬКО.
Входной документ: YANDI_SELF_LEARNING_RECONCILIATION_AUDIT.md (Phase I-0).
Метод: root cause → baseline → minimal fix → targeted regression → full
regression → LIVE production run → inspect persisted result → atomic commit,
для каждого независимого исправления.

================================================================================
0. OLLAMA STATUS / ROOT CAUSE (почему предыдущий Phase I-0 не смог live-run)
================================================================================

**Ollama всё это время была полностью здорова.** Не было ни падения сервиса,
ни проблем с GPU/драйвером.

Две отдельные, независимые причины кажущейся недоступности:

1. **Диагностика была искажена прокси.** В окружении сессии установлены
   `http_proxy`/`https_proxy` (используются для внешних web-запросов),
   без `NO_PROXY`/`--noproxy` исключения для localhost. `curl http://127.0.0.1:11434/...`
   без явного `--noproxy '*'` заворачивался во внешний прокси
   (`45.147.182.91:8000`), который не может достучаться до loopback-адреса →
   `curl: (52) Empty reply from server`. Сам `ollama list`/`ollama ps` работали
   мгновенно (Go-клиент не наследует эти переменные так же). Проверено: с
   `--noproxy '*'` `/api/tags` и `/api/generate` отвечают корректно и быстро
   (генерация — 489ms). Production-код YANDI (`orch_synthesizer.py:115`,
   `_session.trust_env = False`) уже defensively отключает это наследование
   для своих Ollama-запросов — сам продакшн не был бы затронут этим багом.
   `start.sh` уже использует `--noproxy '127.0.0.1,localhost'` в своей
   health-check — автор уже знал про эту особенность окружения.

2. **Продакшн-процесс YANDI просто не был запущен** в среде предыдущего
   аудита (`pgrep -af council_chat_server` — пусто). Без поднятого HTTP-сервера
   live-run невозможен независимо от состояния Ollama.

**Production-модель определена из кода, не из roadmap**: `agent/orch_config.py:30`
`MODEL = "heretic:q8"` (не `qwen3:14b`, как можно было бы предположить) —
`ollama ps` подтвердил, что именно эта модель загружена (100% GPU, 9GB).

**Live-run подтверждён**: сервер поднят (`start.sh`), реальный запрос через
`/api/orchestrator/ask` дошёл до ответа (HTTP 200), trace и dataset episode
сохранены. Единственная особенность окружения: реальный HTTP-путь
(`pet/chat_orch.py:164`) всегда передаёт `enable_validation=True`, что
запускает `send_to_deepseek()` → `get_result(timeout=120)`
(`agent/ai_validator_redis.py`) — ожидание ответа через Redis-мост к
Firefox-расширению, которого в headless-сессии нет. Это НЕ баг — пайплайн
корректно восстанавливается после истечения таймаута и возвращает валидный
ответ (проверено: HTTP 200 за ~100-140s). Каждый live-run в этом отчёте
занимал ~20с-150с именно по этой причине.

================================================================================
1. ИСПРАВЛЕННЫЕ P0/P1 (с SHA каждого atomic commit)
================================================================================

Baseline перед началом: HEAD `27f1d9a` (после Phase I-0 аудита), regression
29/29, рабочее дерево чисто (кроме фонового dataset-append).

--------------------------------------------------------------------------------
P0-1. reflection → policy → planner self-reinforcement — commit `18a529d`
--------------------------------------------------------------------------------

**Root cause (доказано глубже, чем в аудите)**: помимо описанного в аудите
неконтролируемого `confidence += 0.1` при повторении, обнаружен более
серьёзный баг — `ReflectionLoop.get_policies()` возвращал
`self.active_policies` ПО ССЫЛКЕ. `orch_planner.py::_get_reflection_policies()`
дописывал в этот же список ad-hoc lesson-объекты для локального
использования, случайно мутируя живой список политик. При следующем
`_save_policies()` эта контаминация утекала на диск. На реальных данных:
382 записи в `registry/reflection_policies.json`, из них только 2
легитимные (`type: "behavioral"`), 380 — утёкший мусор без поля `rule`,
растущий примерно на 1-2 записи за каждый запрос, без dedup.

**Fix**:
- `orch_planner.py`: `list(reflection.get_policies())` — копия вместо
  мутации живого списка.
- `reflection_loop.py::_apply_policy_to_planner`: повторение правила больше
  НЕ увеличивает confidence безусловно — растёт только `observed_count`.
  Новая policy получает `status="observed"` и не влияет на planner, пока не
  накопит `_MIN_OBSERVATIONS_TO_ACTIVATE=3` независимых повторений (→
  `status="active"`). Confidence фиксируется на момент создания.
- `orch_planner.py::_apply_reflection_policies`/`_should_skip_internet`:
  пропускают policy со `status="observed"`; legacy-записи без поля `status`
  не затронуты (grandfathered).
- Одноразовая очистка `registry/reflection_policies.json` (не в git):
  382 → 2 записи, 2 легитимные мигрированы на новую схему.

**Проверено live**: до фикса — рост файла с 372 до 382 записей за несколько
запросов в рамках этой же сессии. После фикса — множественные реальные
HTTP-запросы через `/api/orchestrator/ask`, файл стабильно держит 2 записи
(`observed_count` растёт: 3→4→...→8 на момент финальной проверки,
`confidence` не меняется).

--------------------------------------------------------------------------------
P0-2. Canonical Trust для dataset/experience/outcome — commit `eecdebe`
--------------------------------------------------------------------------------

**Root cause**: canonical Trust вычисляется один раз, поздно
(`writeback.py`, единственная точка cutover). До этой точки несколько
consumers сохраняли pre-cutover значение и никогда не сверялись заново:
`trace.outcome.trust_label` (встроен в тот же persisted Trace, что и
`trace.trust`), `experience_memory.add_experience()`, `dataset_builder.record_episode()`
(буквально файл `agent/dataset/episodes_*.jsonl`, на который будет опираться
будущий ExperienceRecord). Восстановлен из git-истории удалённый отчёт
(`git show 7637887:YANDI_EPISTEMIC_TRUST_CONSOLIDATION_REPORT.md`, раздел
9 "Legacy paths remaining") — это было ИЗВЕСТНОЕ, намеренно отложенное
предыдущей фазой ограничение, не новый баг.

**Fix**:
- Canonical trust вычисляется один раз сразу после reflection-downgrade
  (последняя мутация strand 1) и используется в `experience_memory`/
  `dataset_builder`. `self_model`/`memory`/`reflection` намеренно
  продолжают видеть pre-cutover значение — reflection's собственный
  downgrade сам является входом в strand 1, поэтому обязан выполняться
  до канонизации (циклическая зависимость, не баг).
- На исходном cutover (`writeback.py:~654-668`) добавлен патч
  `trace.outcome.trust_label` на canonical значение — ДО
  `tracer.save_trace()`, то есть попадает в persisted файл.
- `archive_query()` сознательно НЕ тронут: вызывается безусловно, до
  условного V3-блока (self_model/memory/reflection/experience/dataset) —
  перенос сделал бы безусловный вызов условным, что выходит за рамки
  минимального фикса. Задокументировано как остающийся debt, не
  замаскировано.
- Добавлены целевые regression-проверки в
  `epistemic_canonical_trust_shadow_regression_test.py` (structural,
  source-inspection style, консистентный с существующими проверками).

**Проверено live**: реальный HTTP-запрос → `trace.trust`,
`trace.outcome.trust_label`, `episode.trust` — все три совпадают
(`UNVERIFIED`), включая финальную сводную проверку в конце этого отчёта.

--------------------------------------------------------------------------------
FOUNDATION. Episode ↔ Trace identity — commit `461ea2e`
--------------------------------------------------------------------------------

**Root cause**: `agent/dataset/episodes_*.jsonl` не содержал ни своего id,
ни ссылки на породивший trace. Единственный способ сопоставления —
timestamp proximity, доказанно ненадёжный (расхождение ~309с на реальном
примере в аудите).

**Fix** (переиспользована существующая identity, не создана параллельная):
- `dataset_builder.py::record_episode()`: id теперь генерируется ДО записи
  и встраивается как `episode_id` (раньше вычислялся ПОСЛЕ записи и
  никогда не сохранялся).
- `writeback.py`: уже существующий в scope `trace_id` передаётся в
  `record_episode()` как `"trace_id"`.
- Добавлен `agent/dataset_builder_regression_test.py` (изолированная temp-
  директория, не трогает реальный dataset).

**Проверено live**: реальный запрос → `episode.trace_id` буквально
совпадает с `trace.trace_id` того же запроса (`trace_1787830569_85acbd6f`).
Подтверждено повторно в финальной сводной проверке.

--------------------------------------------------------------------------------
P1. orch_dataset.py — сломанный trace→SFT фильтр — commit `f17c98a`
--------------------------------------------------------------------------------

**Root cause: schema разошлась катастрофически, а не миска-либрован порог.**
`_filter`/`_dedup`/`_to_chatml` были написаны под схему
(`task`/`skill`/`model`/`messages`/`result`/`outcome`-как-строка/`quality`/
`ts`/`task_type`), которую реальный producer (`agent/orch_tracer.py`)
никогда не писал. Дополнительно обнаружено: `stats()`/`review()` вызывали
`OrchestratorTracer().stats()`/`.tail()` — `OrchestratorTracer` оказался
**no-op заглушкой** (`def trace(*a, **kw): pass`, без единого другого
метода) — оба CLI-сабкоманды падали с `AttributeError` безусловно, независимо
от данных. Эмпирически подтверждено до фикса: `stats` → traceback,
`export` → 0 raw... нет, 440 raw → 0 filtered → 0 deduped.

**Fix** (под реальную схему, проверенную на живых трассах):
- `_filter`: trust-label (`STRONGLY_SUPPORTED`/`PARTIALLY_SUPPORTED`) +
  минимальная длина `outcome.final_answer`.
- **Сознательно НЕ добавлен обратно числовой quality-гейт.** Измерено на
  реальных данных: `outcome.trust_score` (сырая confidence синтезатора)
  ни разу не превысил 0.4 даже для `STRONGLY_SUPPORTED` трасс;
  `outcome.coverage_ratio` — это захардкоженная заглушка
  (`writeback.py: 0.5 if len(answer)>100 else 0.0`), а не реальное
  вычисление покрытия — принимает только 2 значения. Ни одно поле не
  является честным независимым quality-сигналом. Гейтить на любом из них
  означало бы либо снова получить 0 результатов, либо подобрать
  произвольный заниженный порог без обоснования — ровно то, что ТЗ
  запретило ("не ослаблять фильтр просто ради ненулевого результата").
  Trust-label — уже существующий, канонический epistemic quality-сигнал
  проекта — оставлен единственным гейтом.
- `_dedup`: ключ на `query` (было: несуществующий `task`).
- `_to_chatml`: реальный 2-turn `messages` (было: всегда `[]`), метаданные
  из реальных `outcome`/`epistemic` полей.
- `export()`'s per-skill разбивка перегруппирована по `epistemic.domain`
  (было: несуществующий `skill`, всегда `"general"`); ничего вне модуля не
  читает `exec_<skill>.jsonl` по имени — переименование безопасно.
- `stats()`/`review()`: вычисляются напрямую из реальных файлов вместо
  мёртвой заглушки; `exec_search`/`exec_analysis` targets честно
  показывают `have=0` (skill-измерение отсутствует в реальных данных,
  а не выдумана произвольная привязка).
- `_safe_outcome()` guard: защита от non-dict `outcome` (legacy/malformed
  записи) — падало на `.get()`.

**Измерено, до/после (`export` на реальных 440 трассах)**:
| | ДО | ПОСЛЕ |
|---|---|---|
| raw traces | 440 | 440 |
| filtered | 0 | 86 |
| deduped | 0 | 53 |
| SFT-файлы | не создавались | `orch_train.jsonl` (53 строки) + 5 файлов по доменам |
| `stats`/`review` | AttributeError (крах) | работают, реальные метрики |

`success_rate` (доля STRONGLY/PARTIALLY_SUPPORTED) = 0.195 по всем 440
трассам — низкая, но это честное отражение текущего состояния прод-
трафика (много UNVERIFIED без интернет-проверки в тестовых прогонах), не
артефакт фильтра.

**Проверено live**: реально запущенные `orch_dataset.py stats/review/export`
против настоящей `registry/dataset/orch_traces/`, выходные файлы
проинспектированы вручную — валидные query/answer пары, корректные
trust/domain/trace_id.

--------------------------------------------------------------------------------
Item 6. Аудит двух finetune pipeline — ДИАГНОСТИКА, код не менялся
--------------------------------------------------------------------------------

| | `agent/finetune.py` (`FinetunePipeline`) | `agent/orch_finetune.py` |
|---|---|---|
| Корпус | HF-экспорт council-чата (character/personality banter) | `registry/dataset/orch_sft/orch_train.jsonl` (эпистемические трассы оркестратора, теперь рабочий после фикса выше) |
| Целевые модели | qwen3-0.6b/1.7b/4b, llama3-8b | qwen3-7b (executor) / qwen3-14b (orchestrator) |
| Вызывающий | `agent/daemon.py:586-587` — но **сам daemon.py не запускается** в проде (нет systemd/cron, отсутствует `reader/config.yaml`) | Нет программных вызывающих — только упоминание в print-подсказке (`orch_node_bootstrap.py:165`), чисто-ручной CLI |
| Формат чтения | `registry/dataset/final/*_hf.jsonl` (session_id/role/content/topic) | `registry/dataset/orch_sft/orch_train.jsonl` (messages/quality) |
| Формат производства | `registry/finetune/sft/{train,val}_*.jsonl` (ChatML) | `registry/dataset/orch_finetune/train_{7b,14b}.jsonl` |
| Scaffolding | run tracking (`status()`), `eval()` (A/B по длине ответа — грубо), `promote()` (ставит флаг + `DatasetVersionManager`/`KnowledgeGraph` — **не переключает реальную serving-модель**, нет настоящего gate) | Только `prepare_dataset()`/`train()`/`eval_ab()` (print-сравнение) — нет run tracking, нет promote, нет versioning |
| Автозапуск | Нет (только вручную через Redis `council:daemon:control`) | Нет (только вручную через CLI) |
| unsloth/trl доступны в этом окружении | Нет (`ModuleNotFoundError`) | Нет (`ModuleNotFoundError`) |

**Вердикт: НЕ конкурирующие реализации по назначению** (разные корпуса,
разные целевые модели) — совпадают только механикой (unsloth+TRL LoRA).
Оба сегодня manual-only, ни один не автозапускается (соответствует
инварианту "не автообучать по ночам").

**Рекомендация: KEEP обе** для своих доменов. Ни удалять, ни объединять
логику пайплайнов сейчас нельзя (разные корпуса, разные модели — слияние
разрушило бы разделение доменов, которое сам roadmap требует). **MERGE
LATER — только shared plumbing**, если/когда Stage II будет одобрен: run
tracking / promote-с-реальным-gate / dataset versioning уже есть в
`finetune.py`, но с фиктивным gate; `orch_finetune.py` было бы разумно
переиспользовать эту инфраструктуру (не корпус-специфичную логику), а не
писать её в третий раз. Не сделано сейчас — Stage II явно вне скоупа
этого ТЗ.

--------------------------------------------------------------------------------
Item 7. orch_metrics.jsonl flush-on-shutdown — commit `3cd6849` (ЧАСТИЧНЫЙ FIX)
--------------------------------------------------------------------------------

**Root cause подтверждён**: `record()` буферизует в памяти, flush каждые
10 событий, без единого shutdown-хука. Файл не обновлялся с 2026-07-15
несмотря на активный прод-трафик.

**Применённый локальный fix**: `atexit.register(flush)` в
`orch_monitoring.py`. Корректен и покрыт regression-тестом (реальный
subprocess, пишет 3 события < порога, штатно завершается, все 3 на
диске — было бы 0).

**КРИТИЧЕСКИ ВАЖНО, обнаружено верификацией, а не предположено**: прямая
изолированная репродукция (минимальное FastAPI+uvicorn приложение с тем же
`atexit.register(flush)`, `kill -TERM` процесса) **доказала, что этот
atexit НЕ срабатывает** при graceful SIGTERM-остановке реального
production-сервера (`pet/council_chat_server.py`, `uvicorn.run(...,
reload=False)`). Путь graceful shutdown в uvicorn не проходит через
триггер atexit-хендлеров в этой конфигурации. Подтверждено повторно на
самом `pet/council_chat_server.py` (запуск, live-запрос, `kill -TERM` PID,
пауза, файл метрик не изменился).

**Согласно инструкции этого ТЗ** ("Если требует архитектурного изменения
— STOP по этой подзадаче и задокументировать"): полноценный fix требует
явного shutdown-хука в `pet/council_chat_server.py` (например, FastAPI
`@app.on_event("shutdown")`, вызывающий `flush()`) — это файл вне `agent/`,
вне аудированного скоупа этого ТЗ (пользователь явно ограничил: "речь
пока идёт о /home/iam/yandi/agent/"). **STOP на этой подзадаче.**
Применённый atexit-фикс оставлен (безвреден, реально чинит CLI/скриптовый
сценарий использования модуля), но явно и честно задокументирован в коде
и в regression-тесте как НЕ решающий production-путь — не замаскировано.

================================================================================
2. REGRESSION РЕЗУЛЬТАТЫ
================================================================================

| Момент | Regression |
|---|---|
| Baseline (после Phase I-0, до Foundation Repair) | 29/29 green |
| После P0-1 | 29/29 green |
| После P0-2 (+2 targeted-теста в существующем файле) | 29/29 green |
| После episode↔trace identity (+1 новый файл, 6 тестов) | 30/30 green |
| После orch_dataset.py (+1 новый файл, 10 тестов) | 31/31 green |
| После orch_monitoring.py (+1 новый файл, 6 тестов) | 32/32 green |
| **Финальный прогон** | **32/32 green** |

Все regression-тесты запускались через `/home/iam/venv/bin/python3` с
`PYTHONPATH=/home/iam/yandi` (venv, не system python3 — там отсутствуют
зависимости вроде numpy).

================================================================================
3. LIVE MATRIX (реальные HTTP-запросы через /api/orchestrator/ask)
================================================================================

За время Foundation Repair выполнено 9 реальных live-запросов через
поднятый `pet/council_chat_server.py` (порты 9011-9020, каждый раз с
чистым окружением без унаследованных proxy-переменных), включая финальный
сводный прогон. Один запрос попал в ветку уточнения (clarification) —
корректное поведение пайплайна, не баг. Все остальные вернули HTTP 200 с
валидным ответом, персистентной trace и dataset episode.

Финальная сводная проверка (после ВСЕХ фиксов, один запрос "Опиши
химический состав атмосферы Венеры максимально точно"):
- `trace.trust` == `trace.outcome.trust_label` == `episode.trust` ==
  `UNVERIFIED` (согласованность canonical Trust, P0-2).
- `episode.trace_id` == `trace.trace_id` == `trace_1787831769_9e2adc3f`
  (episode↔trace identity, Foundation item 3).
- `registry/reflection_policies.json`: 2 записи (не 382), `observed_count`
  растёт, `confidence` не меняется (P0-1 gate работает под реальной
  нагрузкой).

================================================================================
4. DATASET BEFORE/AFTER
================================================================================

См. §1 P1-раздел выше (orch_dataset.py): 440 raw → 0 → 86 filtered → 0 →
53 deduped, до/после фикса схемы. Дополнительно: episodes_*.jsonl теперь
несёт `episode_id`+`trace_id` на каждой новой записи (обратная
совместимость сохранена — старые записи без этих полей не сломаны, читаются
как раньше).

================================================================================
5. ОСТАЮЩИЙСЯ DEBT (не исправлено в этом проходе, явно, не замаскировано)
================================================================================

- **orch_metrics.jsonl, production shutdown path** (см. Item 7) — требует
  правки `pet/council_chat_server.py`, вне скоупа `agent/`. Нужно отдельное
  разрешение на выход за пределы `agent/`, либо явный отдельный тикет.
- **archive_query()** (`registry/knowledge/*.db`) продолжает получать
  pre-canonical Trust — сознательно не тронут (см. P0-2), задокументировано.
- **Naming collisions** из аудита (Reflection×4, Policy, Trust, Validator×4,
  Hypothesis, Experience/KnowledgeGraph) — не переименовывались (не входило
  в Foundation Repair scope, это дизайн-решение для будущего Stage I).
- **Дублирующиеся finetune pipelines** — оставлены как есть (KEEP,
  диагностика в §1 item 6), MERGE LATER — не сейчас.
- **Storage discipline** (unbounded rewrite-whole-file для beliefs.json/
  disagreements.json/episodic_memory.json/transport_memory.json) — не
  трогалось, вне скоупа этого прохода (P2 в исходном аудите).
- **`agent/evidence_kind.py`** — мёртв и сломан (NameError при импорте) —
  не трогался (P2, cleanup-only, безопасно игнорировать).
- **Six confirmed dead files + empty stub `agent/orchestrator/registry/`**
  из аудита — не трогались, не входило в Foundation Repair scope.
- Мелкий NEEDS VERIFICATION из аудита (writer `episodes_*.jsonl` конкретной
  функцией) — теперь известен: `dataset_builder.record_episode()`,
  вызывается из `writeback.py` (обнаружено в ходе P0-2/identity фиксов).

================================================================================
6. GIT
================================================================================

Baseline: `27f1d9a` (после Phase I-0).

Atomic-коммиты Foundation Repair (в хронологическом порядке):
1. `18a529d` — fix: gate reflection→policy self-reinforcement and stop
   policy-list aliasing leak (P0-1)
2. `eecdebe` — fix: dataset/experience/trace-outcome consumers use
   canonical Trust (P0-2)
3. `461ea2e` — fix: link dataset episodes to their producing trace by id
   (Foundation item 3)
4. `f17c98a` — fix: repair orch_dataset.py trace→SFT pipeline against
   the real trace schema (P1)
5. `3cd6849` — fix(partial): flush orch_metrics.jsonl buffer on process
   exit; production uvicorn path left as documented debt (item 7)

HEAD после Foundation Repair (до коммита этого отчёта): `3cd6849`, на 6
коммитов впереди `origin/main` (`517eb1a`).

`git status` перед написанием отчёта: чисто, кроме фонового
`agent/dataset/episodes_20260827.jsonl` (естественный прирост от
собственных live-verification запросов этого прохода — не код).

**НЕ PUSH.**

================================================================================
7. ГОТОВНОСТЬ К STAGE I
================================================================================

**GO** (с учётом задокументированного debt из §5).

Обоснование смены с CONDITIONAL GO (Phase I-0) на GO:
- Оба P0 из аудита устранены и живо verified: reflection→policy loop
  теперь имеет безопасный gate (наблюдение сохранено, но применение к
  planner требует накопленного повторения — не полноценный
  PolicyHypothesis/Shadow/Experiment, но уже не самоусиливающийся
  безусловный loop); canonical Trust теперь согласован across
  trace/outcome/dataset/experience.
- Episode↔Trace identity — фундамент для будущего `ExperienceRecord`
  восстановлен: reference, не дублирование, как требовал roadmap.
- P1 dataset pipeline — рабочий, с честной (не раздутой) метрикой
  готовности (53 качественных примера из 440 сырых трасс — новый,
  реальный baseline для Stage II readiness assessment, всё ещё далеко
  от 500 target, но БАЗА для измерения прогресса теперь существует).
- Единственный оставшийся нерешённый P1 (orch_metrics.jsonl production
  shutdown path) не блокирует Stage I: он касается performance/latency
  метрик, не самого self-learning loop, и явно задокументирован, а не
  скрыт.
- Regression 32/32, live verification на каждом шаге, ничего не
  замаскировано.

Следующий шаг — НЕ входит в это ТЗ: отдельное разрешение и отдельное ТЗ
на реализацию `agent/self_learning/` (PolicyHypothesis/Shadow/
ExperimentEngine), придерживаясь минимальной архитектуры, предложенной в
YANDI_SELF_LEARNING_RECONCILIATION_AUDIT.md §22 (HYBRID: `policy_hypothesis.py`
+ `shadow.py` + `experiment.py`, всё остальное — EXTEND существующих
owners, не дублировать).

================================================================================
КОНЕЦ ОТЧЁТА
================================================================================
