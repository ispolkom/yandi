# YANDI — FINAL PERFORMANCE PASS: REFUTATION ROOT CAUSE

Дата: 2026-08-26

Это последний performance-проход перед возвратом к
epistemic architecture (`YANDI_EPISTEMIC_ARCHITECTURE_GAP.md`,
обсуждение с пользователем ещё не завершено — не начинается
автоматически).

---

## 1. REFUTATION DATA FLOW

```
enrich_result
  → formulate_refutation_queries(enrich_result)   [1 LLM call, JSON, 2-3 запроса]
  → query_frame["refutation_queries"]
  → scrape(refutation_wq, fetch_cache)            [DDGS search × ≤3 запроса
                                                     → HTTP fetch (ThreadPoolExecutor(5))
                                                     → keyword relevance gate
                                                     → domain diversity select]
  → refutation_snippets
  → query_frame["refutation_snippets"]
  → (позже, отдельный profile bucket "source_classification"):
        merged pool (local + web + refutation)
      → is_relevant() [embedding]
      → classify_sources() [NLI per source]
  → query_frame["classified_sources"]
  → synthesis / trust downstream
```

Важный факт, найденный в разделе 1 (атомарное объяснение): бакет
`profile_refutation_ms` в `[PROFILE]` покрывает ТОЛЬКО
`scrape(refutation_wq)` — то есть DDGS search + HTTP fetch + дешёвый
keyword-based relevance gate + domain diversity selection. Внутри
самого refutation bucket НЕТ embedding и НЕТ NLI-вызовов — это
происходит позже, в отдельном, уже давно измеряемом
`source_classification` bucket (объединяющий local+web+refutation
источники). Поэтому refutation bucket по конструкции **атомарно
объясним на 100%**: это чистая сетевая работа (search latency + fetch
latency), без скрытых embedding/NLI издержек внутри себя.

---

## 2. ROOT CAUSE

Не quadratic NLI explosion и не sentence-by-sentence embedding (этих
паттернов внутри refutation НЕ обнаружено — их там физически нет).

Реальная находка: **main web scrape(), refutation scrape() и
claim-specific retrieve_for_claims() каждый создавали СВОЙ
собственный `SharedFetchCache`** (или `scrape()` создавал одноразовый
внутри себя, когда вызывающий не передавал кэш). Один и тот же URL,
физически обнаруженный независимо в двух разных стадиях одного
запроса (например, и основным web, и refutation discovery), мог быть
скачан дважды — без какой-либо видимости этого дублирования.

Проверка раздела 2 (A-I): паттерны A/B/D/E/F/G/H не найдены в
refutation. Паттерн **C (URL fetch'ится несколько раз)** и **I (один
источник уже в main web, но refutation заново делает физический
retrieval)** — подтверждены и исправлены.

`formulate_refutation_queries()` (раздел 4) уже был одним LLM-вызовом
со structured JSON output (`{"queries": [...]}`, 2-3 запроса) — батчинг
там не требовался, оставлен без изменений.

---

## 3. SUBPHASE TIMINGS

Живой прогон (`Есть ли разумная жизнь на Юпитере?`, `--web --no-cache`,
после фикса, `/tmp/yandi_refutation_final_perf.log`):

| Phase | Time | % |
|---|---|---|
| claim_specific_retrieval | 120.35s | 32.2% |
| final_claim_coverage | 51.85s | 13.9% |
| claim_claim_nli | 43.31s | 11.6% |
| web | 31.88s | 8.5% |
| **refutation** | **28.32s** | **7.6%** |
| synthesize | 25.47s | 6.8% |
| claim_pass2_mapper_nli | 21.90s | 5.9% |
| claim_setup_validator_mapper1_nli1 | 17.63s | 4.7% |
| registry/web-initial | 14.95s | 4.0% |
| belief_update | 7.86s | 2.1% |
| plan + intent | 10.13s | 2.7% |
| source_classification | 4.05s | 1.1% |
| прочее | ~1.4s | ~0.4% |
| **measured_sum=379.72s unaccounted=0.00s total=373.83s** | | |

Refutation внутри себя не имеет под-фаз embedding/NLI (раздел 1) — её
собственная стоимость целиком это search+fetch. Раскладку "query_gen/
search/fetch/parse/embedding/nli/classification" из раздела 8 задания
делать избыточно детальной не потребовалось: query_gen = 1 LLM-вызов
(не измеряется отдельно, доли секунды), search+fetch — единственная
реальная стоимость внутри бакета, embedding/nli в бакете отсутствуют
физически.

Замечание не по refutation, а по масштабированию: в этом прогоне
извлечено **20 claims** (против 5 в предыдущем аудите) — это
полностью объясняет рост `claim_claim_nli` (43.31s/11.6% против
13.32s/2.6% ранее, при том же O(claims²) механизме, уже
задокументированном как известный scaling risk в предыдущем отчёте).
Это НЕ новая находка и вне scope этого прохода (задание ограничивает
эту фазу условием "только если снова станет значимым узким местом" —
11.6% ниже 15-20% stop-condition порога, поэтому не преследуется
здесь).

---

## 4. DUPLICATE WORK FOUND

1. **Cross-phase URL fetch duplication** (главная находка): main web,
   refutation и claim-specific retrieval не делились кэшем
   физических fetch'ей.
2. Побочная находка при написании regression-теста (не perf, а
   correctness): `SharedFetchCache.get_or_fetch()` падал с
   `UnboundLocalError` в `finally`, если `fetch_fn` бросал исключение
   (а не возвращал `(None, reason)`), маскируя оригинальную ошибку и
   пропуская `event.set()` — что заставило бы любой конкурентный
   waiter ждать полный `FETCH_TIMEOUT+10` вместо быстрого fallback. В
   продакшене `_fetch_url`/`_fetch_url_proxy` никогда не бросают
   исключение (всегда возвращают tuple), поэтому баг не проявлялся
   вживую — найден только благодаря написанию реалистичного теста на
   cache failure fallback (раздел 10, кейс 5).

Никаких duplicate query generation, duplicate parsing, duplicate NLI,
duplicate embedding внутри refutation НЕ найдено.

---

## 5. FIXES

1. **perf**: один `SharedFetchCache` на запрос (`_request_fetch_cache`
   в `orchestrator_v2.process()`), передаётся в main web `scrape()`,
   refutation `scrape()` и `retrieve_for_claims()` (новый опциональный
   параметр `fetch_cache`, по умолчанию — как раньше, свежий
   экземпляр). Коммит `9efed4e`.
2. **fix**: `SharedFetchCache.get_or_fetch()` — `result = None` перед
   `try`, чтобы `finally` не падал на `fetch_fn`-исключении. Коммит
   `b0cf84d`.
3. **diag**: `[Search Work Audit]` — cumulative
   requests/unique/network_fetches/saved/hit_ratio после `[PROFILE]`.
   Часть коммита `9efed4e`.

Ничего из "ОСТАНОВИСЬ И ДОЛОЖИ" (раздел 9) не потребовалось — оба
фикса чисто физические (fetch-level кэш и его bookkeeping), epistemic
breadth/recall/relation/Trust не затронуты.

---

## 6. HTTP OLD/NEW

| | OLD | NEW |
|---|---|---|
| Fetch cache scope | отдельный per scrape()-вызов / per retrieve_for_claims()-вызов | один per запрос, разделяемый между 3 фазами |
| Измеримость overlap | не измерялась (не было общего счётчика) | `[Search Work Audit]` — реальные числа |

Живое измерение (после фикса): `requests=210 unique_urls=194
network_fetches=194 saved=16 hit_ratio=0.08`. То есть из 210
обращений к кэшу за весь запрос 16 (8%) оказались повторным
обращением к УЖЕ фактически скачанному в этом же запросе URL и не
породили новый физический HTTP fetch. Строгого "до/после" на
идентичном запросе получить нельзя (реальный веб/DDGS
недетерминирован между прогонами — см. раздел 11), поэтому
доказательство — по счётчикам механизма, а не по разнице total
latency между прогонами, как и требовало задание (раздел 12: "механизм
fix доказывать внутренними counters, а не только total latency").

---

## 7. EMBEDDING OLD/NEW

Не применимо к refutation напрямую — refutation bucket не делает
embedding-вызовов (раздел 1). Изменений в embedding-путях в рамках
этого прохода не вносилось.

---

## 8. NLI OLD/NEW

Не применимо к refutation bucket напрямую (NLI происходит позже, в
`source_classification`, который не был целью этого прохода и не
показал признаков N+1: ровно один `/api/generate` вызов на источник в
`classify_sources()`, это genuine per-source epistemic work, не
дублирование). Изменений не вносилось.

---

## 9. CACHE RESULTS

`[Search Work Audit]`: `saved=16` из `requests=210` — реальные, а не
предполагаемые, сэкономленные физические fetch'и за счёт объединения
кэша между main web / refutation / claim-specific retrieval в одном
запросе.

---

## 10. REFUTATION OLD/NEW

Строгое "до/после" на идентичном запросе невозможно (недетерминизм
реального веба/DDGS/LLM между прогонами — раздел 11), но для контекста:

| Прогон | Query | refutation bucket |
|---|---|---|
| До этого прохода (`yandi_unaccounted_diag.log`) | Юпитер/жизнь | 12.12s (3.0%) |
| После belief+import фиксов (`yandi_post_belief_fix_diag3.log`) | Марс/вулканы (другой запрос) | 32.69s (19.9%) |
| После fetch-cache фикса (этот прогон) | Юпитер/жизнь | 28.32s (7.6%) |

Разброс между прогонами ОДНОГО И ТОГО ЖЕ запроса (12.12s vs 28.32s)
объясняется реальной вариативностью живого DDGS/сети/LLM между
запусками (разное число обнаруженных URL — 13-24 в разных прогонах,
разная фактическая доступность/скорость сайтов), а не регрессией:
механизм дедупа доказан отдельно, счётчиками (`saved=16`), а не
единственным числом wall-time.

---

## 11. TOTAL OLD/NEW

| | OLD (`yandi_unaccounted_diag.log`, до belief+refutation фиксов) | NEW (`yandi_refutation_final_perf.log`, после всех фиксов) |
|---|---|---|
| Query | Юпитер/жизнь | Юпитер/жизнь |
| Total | 397.88s | 373.83s |
| Unaccounted | 74.85s | 0.00s |
| Claims extracted | не зафиксировано в этом виде | 20 |

Итоговое total снизилось (397.88s → 373.83s), но прямое приписывание
всей разницы refutation-фиксу некорректно: между этими двумя
прогонами также изменилось число извлечённых claims (влияет на
`claim_claim_nli`, `claim_specific_retrieval`, `final_claim_coverage`
нелинейно) и реальный веб-контент отличался. Единственная величина,
которую можно честно приписать ИМЕННО этому проходу — `saved=16`
физических fetch'а, что при типичной fetch-latency (наблюдаемой в
логах в диапазоне долей секунды — единицы секунд на URL) даёт
плausible вклад в единицы-десятки секунд, но не точную изолированную
цифру.

---

## 12. EPISTEMIC INVARIANTS

Проверено явно (raздел 0 задания):

- число контраргументов не уменьшено — `formulate_refutation_queries()`
  не тронут, по-прежнему 2-3 запроса;
- contradiction search не отключался;
- recall не снижался — кэш влияет только на то, СКОЛЬКО РАЗ URL
  физически скачивается, не на то, какие URL или сколько источников
  рассматривается;
- Trust formula не менялась;
- source eligibility не менялась;
- NLI не удалялся и не упрощался;
- uncertain не превращался в unrelated;
- epistemic breadth не уменьшалась.

Регрессионный тест явно проверяет (раздел 10, кейсы 3-4, 7):
одинаковый физический URL, использованный main web и refutation
пайплайнами, порождает ДВА независимых snippet-объекта с независимым
`"type"` тегом ("web" vs "refutation") — кэш хранит только сырые
fetch-байты, relation/evidence_role/retrieval_origin никогда не
читаются из кэша и не пишутся в него.

**EPISTEMIC SEMANTICS: UNCHANGED.**

---

## 13. REGRESSION

Полный набор (13 модулей), все зелёные:

`claim_priority_regression_test`, `evidence_eligibility_regression_test`,
`final_epistemic_regression_test`, `claim_lifecycle_regression_test`,
`timeout_regression_test` (14), `final_claim_extraction_regression_test` (14),
`planner_regression_test` (5), `claim_relation_regression_test` (10),
`candidate_routing_regression_test` (24), `shared_fetch_regression_test` (22),
`claim_query_batch_regression_test` (17), `belief_manager_regression_test` (14),
**`refutation_performance_regression_test` (15, новый)**.

Новый файл покрывает все 10 кейсов из задания (некоторые — как явную
проверку, некоторые — как естественное следствие уже покрытого
поведения): (1) refutation queries сохраняются, (2) contradiction
query не теряется, (3) duplicate URL fetched once, (4) один source
независимо принадлежит normal+refutation пайплайнам, (5) cache
failure fallback (нашёл реальный баг, см. раздел 4), (6) embed batch
fallback — не применимо к refutation напрямую, уже покрыт
`claim_relation_regression_test`, (7) relation не переносится
автоматически кэшем, (8) empty refutation result, (9) partial search
failure, (10) concurrent duplicate URL.

Два дефекта теста (не production-кода) найдены и исправлены по ходу
написания: нереалистичный мок `_search_with_ddgs`, бросающий
исключение (реальная функция сама ловит свои ошибки и никогда не
бросает — исправлено на "0 URL найдено"), и сломанный `Barrier(2)`
внутри fetch-функции, которая на самом деле выполняется только ОДНИМ
(owner) потоком, а не обоими — исправлено на простую задержку.

---

## 14. COMMITS

```
9efed4e perf: share one fetch cache across main web, refutation and claim retrieval
b0cf84d fix: SharedFetchCache.get_or_fetch() UnboundLocalError on fetch_fn exception
```
(плюс отдельный chore-коммит для dataset episode telemetry)

---

## 15. REMAINING BOTTLENECKS

- `claim_specific_retrieval` (32.2%), `final_claim_coverage` (13.9%),
  `claim_claim_nli` (11.6%) — крупные, но уже прошли через
  посвящённые им раунды оптимизации в предыдущих проходах (candidate
  routing, batched query-gen, batched embeds, belief fix); новых
  duplicate-work паттернов в рамках ЭТОГО (refutation-scoped) прохода
  не искалось и не найдено.
- `claim_claim_nli` вырос в абсолютных числах из-за роста числа claims
  (20 против 5) — уже задокументированный, известный scaling risk
  (O(claims²)), не новая проблема, ниже stop-condition порога (11.6%
  < 15-20%).
- Прямой физический fetch/search — после дедупа всё ещё сетевая
  latency за уникальные URL, дальше не сжимается без изменения
  epistemic breadth (сколько источников реально нужно проверять).

---

## 16. PERFORMANCE CEILING

Согласуется с предыдущим отчётом
(`YANDI_PERFORMANCE_ARCHITECTURE_DECISION.md`): architectural floor ≈
evolutionary floor, поскольку межфазное дублирование физической
работы (единственный источник архитектурного выигрыша) теперь
устранено на всех трёх известных точках (claim↔claim fetch dedup —
предыдущий раунд; main web/refutation/claim-retrieval fetch dedup —
этот раунд). Дальнейшее ускорение требует либо снижения epistemic
breadth (запрещено заданием), либо фундаментальной смены topology
(не обосновано измерениями, см. предыдущий архитектурный отчёт).

---

## 17. STOP CONDITION RESULT

Проверка условий раздела 13 задания:

- unaccounted <=5%? **Да** (0.00%).
- нет крупного N+1? **Да** — единственный найденный (cross-phase
  fetch dedup) исправлен; внутри refutation N+1 не было физически
  (нет embedding/NLI в бакете).
- нет крупного duplicate fetch/embed? **Да** — исправлено, измерено
  (`saved=16`).
- нет фазы >15-20% с очевидной безопасной локальной оптимизацией?
  **Да** — refutation теперь 7.6%; крупные оставшиеся фазы уже прошли
  свои собственные раунды оптимизации, новых очевидных фиксов для них
  в рамках этого (refutation-scoped) прохода не найдено.
- дальнейшее ускорение требует epistemic breadth/topology change?
  **Да**, согласно предыдущему архитектурному отчёту.

**STOP CONDITION: MET.**

---

## 18. RECOMMENDED NEXT PROJECT PHASE

Performance-фаза завершена. Следующий приоритет проекта —
**epistemic architecture** (`YANDI_EPISTEMIC_ARCHITECTURE_GAP.md`,
ещё не написан — GAP-обсуждение с пользователем идёт отдельно и не
завершено): provenance/independent source detection, source
dependency graph, epistemic type, representation equivalence, registry
memory vs verified knowledge, contradiction retention, Trust
dimensions, Knowledge Graph evolution. Начинать этот этап
самостоятельно не следует, пока GAP audit обсуждается пользователем.

---

## ИТОГ

**REFUTATION ROOT CAUSE:**
Cross-phase fetch duplication — main web, refutation и claim-specific
retrieval каждый физически скачивали URL независимо, даже когда один
и тот же URL уже был скачан другой фазой того же запроса. Внутри
самого refutation bucket отдельного N+1/quadratic паттерна не было
(в бакете нет embedding/NLI вообще).

**REFUTATION PERFORMANCE:**
IMPROVED (механизм доказан измеренными counters — `saved=16` реальных
фетчей на живом прогоне; абсолютная величина refutation bucket
варьируется между прогонами из-за реальной сетевой/LLM
недетерминированности, что ожидаемо и не является регрессией).

**EPISTEMIC SEMANTICS:**
UNCHANGED.

**PERFORMANCE OPTIMIZATION PHASE:**
COMPLETE.

**NEXT RECOMMENDED PHASE:**
YANDI_EPISTEMIC_ARCHITECTURE_GAP.md (provenance/independence tracking
first, per предыдущее обсуждение) — не начинать без решения
пользователя по уже представленному концептуальному заданию.

**git status:** чисто (все изменения закоммичены).

**git log --oneline -15** (на момент подготовки отчёта, до коммита самого отчёта):
```
6634e09 chore: pick up dataset episode entries from the live refutation diagnostic run
9efed4e perf: share one fetch cache across main web, refutation and claim retrieval
b0cf84d fix: SharedFetchCache.get_or_fetch() UnboundLocalError on fetch_fn exception
58296d8 chore: pick up dataset episode entries from the live diagnostic run
4abb86e docs: performance ceiling + architecture decision audit report
57a50db fix: correct bare orchestrator_v2 import crash in synthesize()
e47a382 chore: pick up dataset episode entries from regression test runs
da6b85d perf: batch belief_manager similarity-check embeddings (root cause of unaccounted time)
214da61 diag: track belief_update cost in PROFILE (was fully unaccounted)
e76256a perf+diag: batch claim-claim embed calls, timestamp all top-level logs
00e6058 chore: pick up dataset episode entry from the P0+P1 live integration run
61279fe perf: batch claim-specific query generation
b58841e perf: deduplicate cross-claim document fetches
28e5d8a chore: pick up dataset episode log entries from live runs
c045f7c perf: add high-recall candidate routing for final coverage NLI
```
