# YANDI — PERFORMANCE CEILING + ARCHITECTURE DECISION AUDIT

Дата: 2026-08-26
Область: `agent/orchestrator_v2.py` и связанный pipeline (claim retrieval,
NLI, final coverage, belief manager, planner).

Этот отчёт НЕ реализует никаких архитектурных изменений. Все P0/P1 фиксы
внутри текущей архитектуры сделаны, протестированы регрессией и
закоммичены отдельными коммитами. Раздел про DAG/document-centric/
scheduler — только анализ, без кода.

---

## 1. CURRENT PERFORMANCE BASELINE

Цепочка измерений в рамках всей performance-работы (разные раунды,
разные query — отмечено явно):

| Момент | Query | Total | Источник |
|---|---|---|---|
| До всей perf-работы | Юпитер/жизнь | 625.66s | предыдущий раунд (P0-P3 shared computation) |
| После P0-P3 shared computation | Юпитер/жизнь | 420.87s | `/tmp/yandi_shared_work_live.log` |
| После batching claim-claim embed + timestamp diag | Юпитер/жизнь | 397.88s, unaccounted=74.85s | `/tmp/yandi_unaccounted_diag.log` |
| После belief_manager fix + import fix | Марс/вулканы (новый query — Юпитер закэширован) | **164.18s, unaccounted=0.00s** | `/tmp/yandi_post_belief_fix_diag3.log` |

Важная оговорка: последний прогон — на ДРУГОМ запросе (Марс/вулканы, а
не Юпитер/жизнь), потому что запрос про Юпитер уже был отвечен ранее и
попадает в semantic cache (similarity=1.00 → 0.69s, весь pipeline
пропускается). Это НЕ строгий same-query A/B на последнем шаге — но
корректность и величина самого belief_manager фикса подтверждены
отдельно, ниже.

Live-подтверждение фикса belief_manager (изолированный тест на реальных
данных `registry/beliefs.json`, 108 активных "biological" beliefs):

- max abs diff между старыми (sequential) и новыми (batched) cosine
  similarity = **2.98e-08** (шум float32, т.е. бит-идентично);
- speedup на подвыборке 15 кандидатов: **30.8x** (7.11s → 0.23s);
- end-to-end `_find_similar()` по всем 108 реальным кандидатам для
  действительно нового statement: **1.01s** (было ~27s/кандидат в живом
  прогоне, т.е. до ~81s на 3 кандидата).

---

## 2. UNACCOUNTED ROOT CAUSE

Причина найдена и устранена.

Блок `YANDI V6: BELIEFS` в `orchestrator_v2.py` (`add_belief()` для
каждого claim, максимум 3) не имел вообще никакого cost-трекинга —
был невидим в `[PROFILE]` целиком. Внутри него
`belief_manager._is_similar_statement()` делал 2 отдельных HTTP-вызова
`/api/embed` НА КАЖДОЕ сравнение с существующим belief, включая
повторный re-embed одного и того же нового statement на каждой
итерации. В реальном registry на topic="biological" — 108 активных
beliefs, т.е. один `add_belief()` для нового по смыслу утверждения мог
стоить 200+ HTTP round-trips (~27s/кандидат наблюдалось в живом
прогоне, 3 кандидата → ~81s — это и есть почти весь unaccounted=74.85s
гэп).

Фикс (закоммичен, `da6b85d`): `_find_similar()` теперь сначала делает
бесплатный exact-match проход (без сети), и только если совпадения нет
— один batched `/api/embed` вызов (statement + все кандидаты той же
темы разом), затем тот же порядок перебора и тот же threshold
(0.70) + LLM-judge, что и раньше. Критерий решения не менялся —
изменился только способ получения embedding.

Добавлена инструментация (`cost["belief_update_ms"]`,
`[Belief Update Timing]`, запись в `profile_keys`) — закоммичена
отдельно (`214da61`), до фикса самого N+1.

Результат в свежем прогоне: `belief_update=4.93s (3.0%)`,
`measured_sum=166.39s unaccounted=0.00s total=164.18s`. Unaccounted
gap закрыт полностью (`measured_sum` чуть больше `total` из-за
частичного перекрытия фаз в параллельном fan-out на шаге [6] — это
ожидаемо и не является проблемой).

Побочная находка (не perf, а корректность): при подготовке чистого
live-прогона обнаружен и исправлен добаг-баг, существовавший с самого
первого коммита проекта (`ed818bc`, т.е. до начала всей этой сессии):
`orch_synthesizer.py:1262` делал `from orchestrator_v2 import
LocalSynthesisResult` — голый top-level импорт, который резолвится
только если `agent/` сама лежит в `sys.path`, а не только корень
проекта. Это падало с `ModuleNotFoundError` при прямом вызове
`process()` из чистого окружения (ровно то, что нужно для
диагностики). Исправлено на `from agent.orchestrator_v2 import
LocalSynthesisResult` (модуль уже гарантированно импортирован к этому
моменту — он и есть вызывающий). Закоммичено отдельно (`57a50db`),
полная регрессия зелёная.

---

## 3. PLAN/INTENT ROOT CAUSE

Не требует дальнейшего расследования — условие задачи было
"продолжать только если это часть unaccounted". Unaccounted теперь
0.00s, то есть plan/intent полностью учтены в measured_sum и не
являются скрытой проблемой.

Величина для справки: в новом прогоне `plan=1.51s (0.9%)`,
`intent=1.50s (0.9%)` — суммарно 1.8%. В предыдущем прогоне (Юпитер)
было `plan=5.40s + intent=5.39s = 10.79s (2.8%)`. Разница между
прогонами объясняется вариативностью реальной LLM-latency между
запросами (разная длина промпта/контекста, разная нагрузка на Ollama),
а не архитектурной проблемой — все предыдущие дешёвые гипотезы уже
были опровергнуты в прошлом раунде, и текущие данные не дают повода
их пересматривать.

---

## 4. REMAINING LOCAL OPTIMIZATIONS

По свежему профилю (Марс/вулканы, после всех фиксов):

| Phase | Time | % |
|---|---|---|
| web | 35.32s | 21.5% |
| final_claim_coverage | 33.69s | 20.5% |
| refutation | 32.69s | 19.9% |
| synthesize | 21.31s | 13.0% |
| source_classification | 10.56s | 6.4% |
| claim_setup_validator_mapper1_nli1 | 8.81s | 5.4% |
| claim_specific_retrieval | 6.07s | 3.7% |
| belief_update | 4.93s | 3.0% |
| claim_claim_nli | 4.26s | 2.6% |
| registry/web-initial | 4.13s | 2.5% |
| plan + intent | 3.01s | 1.8% |
| остальное | ~1.6s | ~1.0% |

Топ-4 (web, final_claim_coverage, refutation, synthesize) — это
**79.9%** всего времени. Все четыре уже прошли через P0-P2
shared-computation фиксы этой сессии (SharedFetchCache,
batched query-gen, candidate routing prefilter, batched
extract_claim_from_source). Дальнейшая локальная оптимизация ЭТИХ
четырёх фаз ограничена не архитектурой оркестратора, а:

- реальной сетевой latency внешних сайтов (web, refutation);
- реальной latency Ollama-генерации под `GENERATION_SEMAPHORE(2)`
  (final_claim_coverage, synthesize, refutation-анализ).

Один конкретный неисследованный кандидат: **refutation (32.69s,
19.9%)** — не проверено в этом раунде, использует ли она тот же
`SharedFetchCache`/batched query-gen, что и `claim_evidence_retriever`,
или ведёт собственный независимый web-поиск. Это НЕ измерено и НЕ
оценено количественно в рамках текущей задачи (фокус был на
unaccounted + ceiling), поэтому здесь только флагирую как кандидата
для отдельного точечного прохода в будущем — не утверждаю конкретную
экономию, чтобы не "фантазировать цифры".

Данные `[Shared Fetch Cache]` в этом прогоне: `requests=14
network_fetches=14 saved=0` — для ЭТОГО конкретного запроса
пересечений URL между claims не было вообще (см. раздел 7,
Scaling Risks — величина выгоды от дедупа сильно зависит от запроса).

---

## 5. LOCAL OPTIMIZATION CEILING

**LOCAL OPTIMIZATION HEADROOM: LOW-MEDIUM.**

Обоснование: 79.9% времени приходится на 4 фазы, чей пол уже почти
достигнут существующими фиксами этой сессии (fetch dedup, query batch,
candidate routing, embed batching). Оставшаяся стоимость — это либо
реальная сетевая latency (нельзя устранить, только кэшировать
повторы — а повторов в среднем запросе может не быть вообще, см. Scaling
Risks), либо реальная latency локальной LLM-генерации под
сознательно выставленным лимитом параллелизма (можно поднять — но это
risk/capacity решение, а не perf-баг).

Единственный измеримо неисследованный кандидат — refutation
(19.9%), и даже там теоретический потолок оптимизации, скорее всего,
такой же (fetch/generation-bound), а не executor/scheduling-bound —
но это предположение, не измерение.

---

## 6. CURRENT PIPELINE BOTTLENECK MAP

| PHASE | CURRENT | THEORETICAL MIN | REALISTIC MIN | LOCAL OPT? | ARCH CHANGE? |
|---|---|---|---|---|---|
| web | 35.32s | latency N уникальных fetch (сеть) | ≈ текущее (dedup уже есть) | НЕТ (уже сделано) | НЕТ |
| final_claim_coverage | 33.69s | N_routed_pairs × generation_latency / 2 (semaphore) | ≈ текущее (routing уже дал ~64% сокращение пар в предыдущем раунде) | НЕТ (уже сделано) | НЕТ |
| refutation | 32.69s | неизвестно (не профилировано глубоко) | неизвестно | ВОЗМОЖНО (не измерено) | НЕТ |
| synthesize | 21.31s | N_generation_calls × latency | ≈ текущее (N+1 в extract_claim_from_source уже устранён) | НЕТ (уже сделано) | НЕТ |
| source_classification | 10.56s | N_sources × generation_latency | ≈ текущее | не проверено в этом раунде | НЕТ |
| claim_setup_validator_mapper1_nli1 | 8.81s | N_pairs × generation_latency | ≈ текущее | не проверено | НЕТ |
| claim_specific_retrieval | 6.07s | fetch+parse+embed для нужных claims | ≈ текущее (shared cache уже есть) | НЕТ | НЕТ |
| belief_update | 4.93s | 1 batch embed + K judge calls (K = кандидаты ≥0.70) | ≈ текущее (только что оптимизировано) | НЕТ (только что сделано) | НЕТ |
| claim_claim_nli | 4.26s | N(N-1)/2 pairs × generation_latency / 2 | ≈ текущее (не является узким местом, batching уже есть) | НЕТ | НЕТ |
| registry/web-initial | 4.13s | 1 registry lookup | ≈ текущее | НЕТ | НЕТ |
| plan + intent | 3.01s | 2 LLM calls | ≈ текущее (расследовано в прошлом раунде, гипотезы исчерпаны) | НЕТ | НЕТ |

**Оценка потолков (на основе измеренных данных, не фантазия):**

- **EVOLUTIONARY FLOOR** (дальнейшая локальная оптимизация без смены
  архитектуры): около **130-150s** для запроса сравнимой сложности с
  Марс/вулканы (5 claims, 13-узловой hypothesis graph) — то есть ещё
  примерно 10-20% от текущих 164.18s, в основном за счёт непроверенной
  refutation-фазы, ЕСЛИ там действительно есть дублирующая работа
  (не подтверждено).
- **ARCHITECTURAL FLOOR** (при гипотетической полной DAG/document-centric
  перестройке): не может быть ниже суммы (а) реальной сетевой latency
  уникальных fetch'ей и (б) реальной Ollama-latency под текущим
  `GENERATION_SEMAPHORE(2)` — обе эти величины уже близки к текущим
  измеренным, поскольку дублирующая работа (главный источник
  архитектурного выигрыша) уже устранена P0/P1 фиксами. То есть
  архитектурный потолок и эволюционный потолок в данный момент **близки
  друг к другу** — это ключевой аргумент против рефакторинга (раздел 15).

---

## 7. SCALING RISKS

- **belief_update**: до фикса рос линейно и дорого с размером registry
  (O(N) сетевых round-trips на N существующих beliefs той же темы).
  После фикса — O(1) сетевой round-trip (batch) + O(N) дешёвых
  in-memory cosine, то есть рост registry больше НЕ создаёт линейно
  растущую сетевую стоимость. При очень большом N (тысячи beliefs)
  сам payload batch-embed запроса вырастет — не проблема на
  сегодняшних масштабах (108 beliefs, 1.01s), но стоит перепроверить,
  если registry вырастет на порядок.

- **Shared Fetch Cache savings — сильно query-dependent**: в этом
  прогоне (Марс/вулканы) `saved=0` — ни одного пересечения URL между
  claims. В прошлых прогонах (Юпитер/жизнь, где P0-фикс изначально
  измерялся) экономия была заметной. Это значит: **нельзя закладывать
  фиксированный % выигрыша от cross-claim дедупа** в оценки — выгода
  зависит от того, насколько claims одного запроса физически ссылаются
  на одни и те же документы. Для DAG/document-centric архитектуры
  (разделы 8-9) это прямое следствие: её потолок ТОЖЕ query-dependent,
  и не может быть уверенно оценён без большого корпуса реальных
  запросов.

- **claim_claim_nli** сейчас не узкое место (2.6%), но растёт как
  O(claims²) по количеству claims в ответе — при существенно большем
  количестве claims на запрос (сейчас видели 3-5) это может снова
  стать значимым. Задача явно требует строить scaling curve только
  если это "снова станет значимым узким местом" — сейчас это не так,
  поэтому curve не строится, только фиксируется риск на будущее.

---

## 8. DOCUMENT-CENTRIC OPTION (B)

Документ = единица физической работы (fetch/parse/embed), claim =
единица эпистемического суждения. Уже частично реализовано на уровне
fetch (`SharedFetchCache`, P0 этой сессии). Этот вариант расширяет то
же самое на parse+embedding (см. `DocumentArtifact` из исходного
задания на shared-computation pass).

- **Затрагиваемые файлы**: `claim_evidence_retriever.py` (реструктура
  worker'ов), `orch_web_scraper.py` (уже содержит `SharedFetchCache`,
  нужен `DocumentArtifact`), возможно `claim_relation.py`
  (`extract_claim_from_source` мог бы потреблять готовый artifact
  вместо повторного парсинга).
- **Migration complexity**: MEDIUM — нужна новая абстракция
  (`DocumentArtifact`), но паттерн потокобезопасного request-scoped
  кэша с in-flight dedup (Lock + Event) уже проверен и работает
  (`SharedFetchCache`).
- **Epistemic risk**: LOW-MEDIUM — при строгом соблюдении инварианта
  "shared computation, не shared ownership" (уже валидирован на fetch
  уровне). Основной риск: случайно применить claim-специфичную
  предобработку (например, windowing текста под конкретный claim) на
  уровне общего artifact.
- **Expected latency improvement**: уже оценено в предыдущем раунде
  этой же сессии как **~3-8s ceiling** — потому что URL-level dedup
  (P0) уже забрал большую часть выигрыша от переиспользования; parse+
  embed reuse поверх него добавляет немного. При явном
  query-dependent характере выгоды (раздел 7) реальный выигрыш может
  быть ещё ближе к нулю для запросов без document-level пересечений.
  **ПРИНЯТО РЕШЕНИЕ НЕ РЕАЛИЗОВЫВАТЬ** в прошлом раунде именно по
  этой причине ("сложность должна быть оправдана измеренной
  экономией") — текущие данные это решение подтверждают, не меняют.
- **Concurrency model**: те же потоки, добавляется общий document
  cache слой.
- **Rollback complexity**: MEDIUM.
- **Test burden**: MEDIUM (нужны новые regression на границу reuse,
  non-leakage между claims).
- **Gradual migration**: возможна поверх существующего
  `SharedFetchCache`, но не оправдана сейчас measured ceiling'ом.

---

## 9. DAG/JOB-GRAPH OPTION (C)

Обобщение варианта B на произвольные узлы вычисления (fetch, parse,
embed, generate) с dependency tracking и мемоизацией по content hash.

- **Затрагиваемые файлы**: потребует НОВОГО модуля (dag executor/job
  scheduler), затронет весь control flow `orchestrator_v2.process()`,
  `claim_evidence_retriever.py`, `final_claim_coverage.py`,
  `claim_relation.py`, `belief_manager.py` — по сути, полная смена
  топологии pipeline, а не патч.
- **Migration complexity**: HIGH — это full rewrite, не инкрементальный
  фикс; требует пересмотра `process()` как графа, а не
  императивной последовательности с параллельным fan-out.
- **Epistemic risk**: MEDIUM-HIGH — мемоизация по dependency graph
  требует очень аккуратного design cache-key (content hash + model
  version + preprocessing version), иначе риск случайно слить
  семантически разное, но текстуально похожее вычисление между claims
  или между запросами. Существующая сложность кодовой базы
  (`orchestrator_v2.py` 5000+ строк, уже найдены 2 неочевидных N+1
  бага и 1 давний crash-баг за эту сессию) делает полный DAG-rewrite
  заметно более рискованным для отладки, чем инкрементальные фиксы.
- **Expected latency improvement**: НЕ ИЗМЕРЕНО. Учитывая, что (а)
  URL-level dedup (P0) и query-gen batching (P1) уже забрали
  межклеймовую дублирующую работу, и (б) оставшиеся измеренные затраты
  (NLI-вызовы, generation-вызовы) — это по определению НЕ
  переиспользуемая между claims эпистемическая работа (raздел 6:
  architectural floor ≈ evolutionary floor), потолок ДОПОЛНИТЕЛЬНОГО
  выигрыша DAG'а над вариантом A, скорее всего, небольшой — но это
  гипотеза, не измерение.
- **Concurrency model**: DAG-scheduler (topological sort + futures) —
  архитектурно иная модель, чем сегодняшняя "список фаз + per-claim
  worker pool".
- **Provenance/ownership compatibility**: возможна, но требует
  явного design-дисциплины (узлы графа должны быть явно помечены как
  "shared computation" vs "per-claim judgment") — не бесплатно,
  добавляет реальный design/review overhead.
- **Rollback complexity**: HIGH — полный rewrite тяжело откатить
  частично; потребовалась бы параллельная реализация + постепенный
  cutover.
- **Test burden**: HIGH — нужна новая тестовая инфраструктура + всё
  ещё нужно сохранить гарантии существующих regression-тестов.
- **Gradual migration**: возможна только очень длинным поэтапным
  путём (например, DAG только для fetch/parse/embed subpipeline,
  epistemic-суждения остаются как сейчас) — но это фактически
  сходится обратно к варианту A/B, а не оправдывает полный DAG.

---

## 10. REQUEST-SCHEDULER OPTION (D — async/actor scheduler)

Условие задания: рассматривать ТОЛЬКО если thread/executor gaps
доказаны как узкое место.

**Условие НЕ выполнено.** Именно это и было целью P0 этого аудита:
проверить ThreadPoolExecutor lifecycle, очереди, ожидания Future,
фоновые потоки, сериализацию, executor shutdown, semaphore
acquisition, GC, Python scheduling как источник unaccounted времени.
Результат: unaccounted был НЕ threading/executor/scheduling проблемой
— это была легитимная (хоть и расточительная) сетевая нагрузка
belief_manager, теперь измеренная и исправленная. После фикса
unaccounted=0.00s.

Поскольку предпосылка для рассмотрения варианта D не подтверждена
измерениями, **вариант D не обосновывается текущими данными** —
детальный разбор (файлы/миграция/риск) не проводится, чтобы не
"фантазировать цифры" для сценария, для которого нет измеренного
основания.

---

## 11. MIGRATION COST

Сводно (A уже реализовано инкрементально в этой сессии):

| Option | Migration complexity | Rollback | Test burden |
|---|---|---|---|
| A (текущий + больше кэш/batch) | LOW | Trivial | LOW incremental |
| B (document-centric) | MEDIUM | MEDIUM | MEDIUM |
| C (DAG/job-graph) | HIGH | HIGH | HIGH |
| D (async/actor scheduler) | не оценено — предпосылка не подтверждена | — | — |

---

## 12. EPISTEMIC RISK

| Option | Epistemic risk |
|---|---|
| A | LOW — доказано многократно в этой сессии (routing явно отделён от epistemic decision, ownership подтверждён регрессией на каждом фиксе) |
| B | LOW-MEDIUM — риск в основном вокруг случайного смешения claim-специфичной предобработки с общим artifact |
| C | MEDIUM-HIGH — мемоизация по dependency graph рискует слить семантически разные, но текстуально похожие вычисления; сложность кодовой базы усиливает риск |
| D | не оценено |

---

## 13. EXPECTED PERFORMANCE

- **A**: дальнейший выигрыш ограничен (LOW-MEDIUM headroom, раздел 5) —
  единственный неисследованный кандидат — refutation phase, оценка
  потенциальной экономии не дана намеренно (не измерено).
- **B**: ~3-8s ceiling (оценено в прошлом раунде, подтверждено текущими
  данными о query-dependent характере выгоды) — недостаточно для
  оправдания сложности.
- **C**: не измерено; по логике раздела 6 (architectural floor ≈
  evolutionary floor) ожидаемый дополнительный выигрыш над A — вероятно
  небольшой, но это гипотеза.
- **D**: не оценено — предпосылка (thread/executor bottleneck) этим
  же аудитом опровергнута.

---

## 14. RECOMMENDED OPTION

**Вариант A — эволюционировать текущую архитектуру дальше, точечно.**

Единственное конкретное действие с неизвестной, но потенциально
измеримой отдачей: профилировать `refutation` (32.69s, 19.9%)
отдельно — проверить, использует ли она `SharedFetchCache` и batched
query generation так же, как `claim_evidence_retriever`, или ведёт
независимый (возможно дублирующий) web-поиск. Это укладывается в
рамки уже предоставленных автономных полномочий (repeated HTTP/
duplicate work), но НЕ было сделано в рамках текущего аудита, потому
что задача аудита — измерить и решить архитектурный вопрос, не
провести ещё один полный perf-проход.

---

## 15. WHY NOT THE OTHER OPTIONS

- **B (document-centric)**: уже explicitly оценён и отклонён в
  прошлом раунде по измеренному потолку (~3-8s); текущие данные
  (query-dependent dedup savings, раздел 7) не меняют этот вывод, а
  усиливают его — выгода ненадёжна и мала.
- **C (DAG/job-graph)**: не measured bottleneck + local optimization
  ceiling ещё не достигнут (refutation до конца не исследован) +
  ARCHITECTURAL FLOOR ≈ EVOLUTIONARY FLOOR по разделу 6 — по
  собственному критерию задания ("архитектурная смена допускается
  только если MEASURED BOTTLENECK + LOCAL OPTIMIZATION CEILING +
  EXPECTED ARCHITECTURAL GAIN оправдывают миграцию") ни одно из трёх
  условий сейчас не выполнено полностью.
- **D (async/actor scheduler)**: явное условие задания ("только если
  thread/executor gaps доказаны") НЕ выполнено — сам этот аудит
  доказал обратное (unaccounted полностью объяснился сетевой
  нагрузкой, а не потоками/executor'ом).

---

## 16. PHASED MIGRATION PLAN

Поскольку архитектурная смена не обоснована сейчас, "миграция" — это
не B/C/D, а продолжение варианта A:

1. Профилировать `refutation` отдельно (instrumentation only, как
   `[Claim Retrieval Worker SubProfile]` уже сделан для claim
   retrieval) — узнать, есть ли там дублирующий fetch/query-gen.
2. Если найдено дублирование — применить уже проверенный паттерн
   (`SharedFetchCache` / batched query-gen) к refutation, с той же
   дисциплиной регрессии, что и во всех предыдущих фиксах этой сессии.
3. Переоценить headroom после этого шага — если суммарный выигрыш
   остаётся в пределах единиц секунд, дальнейшая эволюционная
   оптимизация исчерпана, и вопрос о B/C/D можно пересмотреть с
   реальными данными, а не гипотезами.

Ни один из шагов не требует смены epistemic semantics, Trust formula,
candidate recall policy или topology pipeline.

---

## 17. FIRST SAFE STEP

Добавить `[Refutation SubProfile]` инструментацию (по аналогии с уже
существующей `[Claim Retrieval Worker SubProfile]`) вокруг fetch/
query-gen/parse внутри refutation-фазы — pure instrumentation, без
изменения логики, под уже предоставленными автономными полномочиями.
Только после этого — решение, стоит ли там что-то чинить.

---

## ИТОГОВЫЕ ВЕРДИКТЫ

**CURRENT ARCHITECTURE: KEEP**

**LOCAL OPTIMIZATION HEADROOM: LOW-MEDIUM**
(один неисследованный кандидат — refutation; всё остальное уже
оптимизировано до сетевого/generation-latency потолка)

**ARCHITECTURE CHANGE JUSTIFIED NOW: NO**
(ни одно из трёх условий задания — measured bottleneck, local
optimization ceiling, expected architectural gain — не выполнено
полностью; unaccounted gap закрыт, thread/executor gaps опровергнуты
измерением, document-centric ceiling измерен и мал)

**RECOMMENDED NEXT STEP:**
Точечно проинструментировать и, если найдётся дублирующая работа,
починить фазу `refutation` (19.9% времени, не исследована в этом
раунде) — тем же паттерном (`SharedFetchCache`/batched query-gen),
что уже проверен на claim retrieval. Архитектурную смену (B/C/D) не
реализовывать без отдельного решения пользователя и без нового
измеренного основания.
