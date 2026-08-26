# YANDI Final Epistemic Audit and Fix

Работал как единый проход: audit → root cause → fix → offline tests → report. Полный orchestrator не запускался — все находки основаны на реальном runtime-логе последнего прогона (TOTAL=550.23s, query "Есть ли разумная жизнь на Юпитере?"), предоставленном пользователем, и на чтении/тестировании кода.

## 1. Executive Summary

Обнаружены и исправлены три независимых P0-бага, все подтверждённые кодом, не догадкой:

1. **Final Answer Gate не существовал.** `synthesis_result.answer` (текст, который видит пользователь) генерируется `compose_prompt` **до** того, как Claim Status вообще вычислен. Всё, что происходит после (`Claim Status Gate`), трогало только `trust_level`/`confidence` — никогда сам текст. Ответ был архитектурно свободен придумывать что угодно, независимо от результатов проверки.
2. **`coverage=1.00` при `factual=0` был vacuous truth bug.** `extract_final_claims()` схлопывала LLM call error, malformed JSON и "модель реально не нашла claims" в один и тот же `[]` → `evaluate_final_claim_coverage()` трактовала это как идеальное покрытие.
3. **Novel claim leakage был невидим** — механизм для его обнаружения (`uncovered_claims`) уже существовал внутри `final_claim_coverage.py`, но никогда не логировался отдельно как "утечка".

Дополнительно подтверждена (не пофикшена, т.к. явно запрещено трогать) причина, почему дорогой retrieval (277.59s, 50.4% latency) почти не создаёт usable support: `source_quality.py`'s domain whitelist делает `eligible=True` практически недостижимым для любого источника вне ~10 захардкоженных доменов — это уже было установлено в самом первом аудите этой сессии и вновь подтверждено этим прогоном.

## 2. Backups Created

| Файл | Backup |
|---|---|
| `agent/final_claim_coverage.py` | `final_claim_coverage.py.bak_20260826_084503` |
| `agent/orchestrator_v2.py` | `orchestrator_v2.py.bak_20260826_084503` |
| `agent/claim_evidence_retriever.py` | `claim_evidence_retriever.py.bak_20260826_084503` |

(Новых файлов, для которых нужен был backup, не создавалось — `agent/final_epistemic_regression_test.py` создан впервые.)

## 3. Files Changed

- `agent/final_claim_coverage.py` — P0-B fix.
- `agent/orchestrator_v2.py` — P0-A fix + P0-C diagnostic.
- `agent/claim_evidence_retriever.py` — P1 query-generation visibility.
- `agent/final_epistemic_regression_test.py` — новый offline regression suite.

## 4. Last Runtime Evidence

```
TOTAL: 550.23s
[PROFILE] claim_specific_retrieval   277.59s   50.4%
[Claim Status] supported=0 disputed=0 contradicted=0 unverified=10 rejected=0
[Grounding] semantic=1.00 epistemic=0.00 support=0.00
[Final Claim Coverage] factual=0 covered=0 uncovered=0 coverage=1.00
```
Финальный ответ при этом содержал развёрнутую новую гипотезу ("хемосинтетической", "коллективной", "распределённой по объёму атмосферы") — ни одно из этих утверждений не входило в `claims_data` и не проходило lifecycle.

## 5. Final Answer Data Flow (точная цепочка)

```
orchestrator_v2.py: synthesize(enrich_result, ...)     ~line 2507
    ↓ (ВНУТРИ synthesize(), orch_synthesizer.py)
    local_answer уже сгенерирован РАНЬШЕ (generate_local_answer, до synthesize())
    _EXTRACT_PROMPT извлекает claims ИЗ local_answer     (для epistemic lifecycle)
    compose_prompt формирует ФИНАЛЬНЫЙ ТЕКСТ            <- synthesis_result.answer
    (compose_prompt НЕ видит claim_status — его ещё нет)
    ↓
orchestrator_v2.py: claims_data validation/mapping/retrieval/NLI    ~line 2600-3460
    (работает НАД claims, извлечёнными из local_answer, НЕ над
     compose_prompt'ным финальным текстом — они разные генерации)
    ↓
orchestrator_v2.py: Claim Status Gate                    ~line 4560-4730
    (ДО фикса: трогал только trust_level/confidence)
    ↓
orchestrator_v2.py: [10] Optimistic respond
    responder.respond(synthesis_result)  -> orch_optimistic.py
    ↓
orch_optimistic.py: format_preliminary()
    lines.append(synthesis.answer)   <- ВЕРБАТИМ, без LLM, без правок
    ↓
banner добавляется (orchestrator_v2.py:5110-5128) — ТОЖЕ не зависит
от Claim Status, только от epistemic_result.is_science_as_model/
domain/testability (вычислен ЕЩЁ РАНЬШЕ, до web/claims)
```

## 6. Final Answer Gate Root Cause

**Подтверждено чтением кода, не предположено.**

1. Последний момент, когда Claim Status уже известен: `orchestrator_v2.py:3458` (`claim["verification_status"] = new_status`, внутри цикла по `claims_data`), финализируется агрегатами в Claim Status Gate (`~4585-4716`).
2. Final answer формируется в `orch_synthesizer.py::synthesize()`, вызванном на `orchestrator_v2.py:~2507` — **задолго до** (1).
3. Responder (`orch_optimistic.py::format_preliminary`) получает `synthesis_result.answer` — тот самый текст из (2), без единой правки, кроме добавления badge/banner (тоже не связанных с Claim Status).
4. Responder НЕ видит supported/unverified/contradicted/disputed claims списком — только `synthesis.trust_level` (строка-метка) и `synthesis.sources`.
5. Да — до фикса Claim Status Gate ДО фикса менял `synthesis_result.answer` только в двух крайних случаях (`total_claims==0`, `все claims rejected`) — в НАШЕМ точном сценарии (`verified=0, supported=0, unverified=10`) текст оставался нетронутым.
6. Новые factual claims получают возможность появиться именно в `compose_prompt` (orch_synthesizer.py) — потому что этот prompt пишет полноценный, самостоятельный текст с нуля (структура "НАБЛЮДЕНИЕ/ГИПОТЕЗА/ПОДДЕРЖКА/ОГРАНИЧЕНИЯ/АЛЬТЕРНАТИВЫ"), а не собирается из уже верифицированных claims.

## 7. Final Epistemic Contract

**Изучена существующая архитектура сначала** — `claims_data`/`Claim Status Gate` уже вычисляют РОВНО те агрегаты, что просил P0-A (`verified_claims`, `supported_claims`, `disputed_claims`, `contradicted_claims`, `unverified_claims`, `rejected_claims`, `total_claims`) — это уже canonical объект, изобретать новый не потребовалось.

**Старое:** Gate читает эти агрегаты и правит только `trust_level`/`confidence`.

**Новое:** Gate **дополнительно** правит `synthesis_result.answer` в двух конкретно доказанных опасных случаях:
```python
# Случай A: доминируют contradicted claims, ни одного supported/verified.
if not synthesis_result.answer.startswith("⚠️ ВАЖНО:"):
    synthesis_result.answer = _contradiction_notice + "\n" + synthesis_result.answer

# Случай B: verified=0 И supported=0 (наш точный баг-сценарий).
if not synthesis_result.answer.startswith("⚠️ ВАЖНО:"):
    synthesis_result.answer = _unsupported_notice + "\n" + synthesis_result.answer
```
Текст НЕ удаляется и не переписывается заново (никакого нового multi-pass verifier per sentence, как и требовалось) — он **предваряется** явным, невозможным-не-заметить маркером прямо в теле ответа, а не только в trust-бейдже. Case C (`verified=0`, но `supported>0`) сознательно НЕ маркируется тем же способом — только `confidence` снижается, как и раньше, чтобы не портить частично поддержанные ответы (доказано тестом 3b).

Не реализовано (сознательно, по инструкции "не строить многопроходный verifier"): построчная проверка КАЖДОГО предложения ответа на предмет "это supported claim / это уже помеченная гипотеза / это novel unsupported assertion". Это было бы архитектурно более полным решением, но требует либо LLM-прохода по всему тексту, либо тесной интеграции compose_prompt с claim lifecycle (переупорядочивание всего пайплайна) — за рамками одного прохода.

## 8. Novel Claim Leakage

**Как возникало:** `compose_prompt` пишет текст независимо от `claims_data` (см. §6) — любое предложение в финальном ответе, не совпадающее ни с одним pipeline claim, есть "novel" по построению.

**Как теперь ограничено:** не ограничено (запрет) — **сделано видимым**. `final_claim_coverage.py` УЖЕ вычисляет `covered_claims`/`uncovered_claims` (extract claims из финального текста → сравнить с pipeline claims → NLI-based semantic identity). Добавлен `[Final Claim Leakage] extracted=N known=N novel=N speculative=N` сразу после существующего `[Final Claim Coverage]` лога (`orchestrator_v2.py`) — переиспользует уже вычисленные данные, никакой новой extraction machinery.

## 9. Final Claim Coverage Root Cause

**Точно найдено, не предположено.** `extract_final_claims()`:
```python
try:
    raw = _call_ollama(prompt)          # (1) может бросить исключение
    data = _extract_json(raw)           # (2) при неудачном парсинге -> {}
    for item in data.get("claims", []): # (3) на {} или {"other_key":...} -> []
        ...
    return result
except Exception as exc:
    return []
```
Все три пути — (1) исключение, (2) parse failure, (3) валидный JSON без нужного формата — **и** легитимный случай "модель реально вернула `{"claims": []}`" — давали **один и тот же** `[]`. `evaluate_final_claim_coverage()`:
```python
if not factual_claims:
    coverage_score = 1.0   # БЕЗУСЛОВНО
```
`extract=13.65s` в логе (не мгновенный сбой — вызов явно выполнился) делает наиболее вероятным сценарий (2)/(3): модель ответила, но не в ожидаемом JSON-формате (вероятно, из-за markdown-таблиц/заголовков в самом финальном ответе, которые модель извлечения не ожидала) — это НЕ доказано напрямую (raw response не сохранился в логе), но механизм бага доказан кодом безусловно.

## 10. Coverage Fix (включая 0/0 поведение)

`extract_final_claims()` теперь возвращает `(claims, status)`, где `status ∈ {"ok", "call_error", "parse_error"}`. `evaluate_final_claim_coverage()` при `factual_claims == []`:

| status | длина answer | coverage_score | coverage_status |
|---|---|---|---|
| `call_error`/`parse_error` | любая | **0.0** (было 1.0) | `extraction_error` |
| `ok` | > 200 симв. | **0.0** (было 1.0) | `suspicious_empty` |
| `ok` | ≤ 200 симв. | 1.0 (не изменилось) | `no_factual_content` |

Trust-формула НЕ переписана — она по-прежнему читает числовой `final_claim_coverage_score` (`orchestrator_v2.py:4355/4371/4442`, ветки `<0.50`/`<0.80`) без изменений; изменилось только КАКОЕ число туда попадает при технической ошибке extraction. `coverage_status` — новое поле, чисто диагностическое, ничего не решает само по себе.

## 11. Claim-Specific Retrieval Resolution (для CORE и DIRECT claim отдельно)

### A. CORE: "По имеющейся информации разумная жизнь на Юпитере не обнаружена."
- Query generation: `contextual_claim_text` включает полный `query_context` ("Есть ли разумная жизнь на Юпитере?") + сам claim → subject anchor `['юпитере','юпитер','jupiter']` присутствует на входе (подтверждено тестом §12 ниже). Реальные СГЕНЕРИРОВАННЫЕ query-строки в этом прогоне не логировались вообще (баг наблюдаемости, исправлен в этом же проходе — §12).
- Retrieval worker: `records=1, time=70.20s` — только 1 НОВАЯ запись из собственного PASS2-поиска этого claim; второй linked evidence (`ev_44ca4ee6`) пришёл из PASS1 (глобальный пул).
- Subject Gate: обе найденные evidence (cyclowiki.org, o-kosmose.ru) прошли (эти источники реально про Юпитер).
- source_class=`unknown` (ни один домен не в whitelist `source_quality.py`) → `role=context`, `quality=0.655`, `eligible=False`.
- NLI: `relation=uncertain` для обеих — источники являются ОБЩИМИ обзорными статьями про "жизнь на Юпитере" (историю гипотез Сагана и т.д.), не содержат прямого утверждения "жизнь НЕ обнаружена" в конкретных терминах, которые NLI распознал бы как `supports`.
- Claim Status: `unverified` — по ДВУМ независимым причинам одновременно (eligible=False делает relation неважным даже если бы relation=supports; и relation сам по себе uncertain).

### B. DIRECT_DECISION_EVIDENCE: "Ни один телескоп или космический аппарат не зафиксировал ни одного сигнала или артефакта на Юпитере."
- Retrieval worker: `records=0, time=83.29s` — **ноль** полезных записей за 83 секунды.
- Subject Gate: **6 источников отклонены** (Wikipedia Biosignature, NASA exoplanets, Sagan archive.org, headlines4.com, labroots.com, fiveable.me) — ни один не содержит "юпитере/юпитер/jupiter" в title/url/passage. Все они про экзопланеты/биосигнатуры/K2-18b в общем — НЕ про Юпитер конкретно.
- 2 источника ПРОШЛИ subject gate (space.com/19915-milky-way-galaxy.html по passage-совпадению, scihub101.com/ganymede по title-совпадению) — но затем отклонены `reject semantic_irrelevant` (не про detection telescope/spacecraft findings конкретно).
- Итог: 0 evidence вообще → `unverified` тривиально (считать нечего).

## 12. Search Query / Subject Preservation

**Root cause НЕ ДОКАЗАН до конца (честно, не гадаю).** Код гарантирует, что subject anchor присутствует на **входе** в `formulate_claim_evidence_queries()` (`contextual_claim_text` содержит "Юпитере" — тест §P1 в regression suite подтверждает это структурно). Но реальные СГЕНЕРИРОВАННЫЕ строки запроса нигде не логировались — единственный видимый симптом это то, ЧТО discovery возвращал (exoplanets/K2-18b/biosignatures/Ganymede/Sagan book вместо Jupiter-специфичных страниц), что КОСВЕННО подтверждает гипотезу пользователя, но не доказывает её напрямую (могла быть и проблема на стороне поисковика/скрапера, а не только формулировки запроса).

**Исправлено (наблюдаемость, не логика):** добавлен `[Claim Retrieval Query] claim_id=... anchors=[...] queries=[...]` сразу после генерации запросов (`claim_evidence_retriever.py`). Следующий live-run покажет РЕАЛЬНЫЕ сгенерированные строки для `cl_6bc5be75`-подобных claims — это либо подтвердит потерю subject в LLM-генерации, либо опровергнет её (может оказаться, что запросы корректны, а проблема в скрапере/поисковике). Query construction/subject restoration логика САМА **не изменена** — только сделана видимой.

## 13. Evidence Eligibility Table

| claim | URL | subject_gate | semantic_relevance | source_class | quality | role | eligible | NLI | counted? | reason |
|---|---|---|---|---|---|---|---|---|---|---|
| CORE | cyclowiki.org/Жизнь_на_Юпитере | pass (PASS1 global) | relevant | unknown | 0.655 | context | False | uncertain | NO | role≠direct |
| CORE | o-kosmose.ru/...zhizn-na-yupitere | pass (PASS1 global) | relevant | unknown | 0.655 | context | False | uncertain | NO | role≠direct |
| DIRECT | wikipedia.org/Biosignature | **reject** | — | — | — | — | — | NO | subject_gate: no Jupiter anchor |
| DIRECT | science.nasa.gov/exoplanets | **reject** | — | — | — | — | — | NO | subject_gate: no Jupiter anchor |
| DIRECT | space.com/19915-milky-way-galaxy | pass | **reject** | — | — | — | — | NO | semantic_irrelevant |
| DIRECT | scihub101.com/ganymede | pass | **reject** | — | — | — | — | NO | semantic_irrelevant |

Ни thresholds, ни subject gate не менялись — таблица построена на реальных логах, root cause уже был известен из первого аудита этой сессии (математический потолок `quality_score≈0.655` для `class=unknown`, ниже порога `0.70`).

## 14. Negative / Absence Claim Evidence Semantics

**Подтверждено логом, не внедрена новая философия.** Для CORE claim ("жизнь не обнаружена") оба найденных источника — ОБЩИЕ обзорные статьи о теме, не содержащие прямой фразы-детекции. Текущий Claim Status требует `role=direct AND eligible=True AND relation∈{supports,contradicts}` — тот же критерий, что и для позитивного факта типа "атмосфера содержит водород". Structural mismatch реален: absence-of-observation claim эпистемически труднее подтвердить ОДНОЙ цитатой, потому что "никто не сообщал о обнаружении X" обычно не публикуется как отдельный факт (в отличие от позитивного открытия). Архитектурная альтернатива (survey-based / mission-scope evidence semantics для negative claims) — НЕ реализована в этом проходе, только зафиксирована как обоснованное направление для отдельной задачи. Изменение NLI/Claim Status философии явно запрещено инструкцией.

## 15. Registry Evidence Role

`source_quality.py::evaluate_source_quality()` не имеет НИКАКОЙ специальной ветки для `source_type="local"` — local registry документы проходят ТУ ЖЕ classify-by-domain логику, что и web. Поскольку у local документов обычно нет URL, `_classify_source()` берёт ветку "нет host" → `authority=0.25, primaryness=0.20` — **хуже**, чем у анонимного `unknown` web-домена с URL (`0.50/0.40`). Максимальный достижимый `quality_score` для local без URL ≈ 0.33 (доказано тестом §10 regression suite) — заведомо ниже любого порога eligibility.

**REGISTRY CAN SUPPORT CLAIM STATUS: NO.**

Причина — не намеренный архитектурный выбор ("не доверять внутреннему знанию"), а побочный эффект того, что `source_quality.py` целиком построен вокруг domain/URL classification без отдельного пути для internal/trusted-by-construction источников. Не исправлено — `source_quality thresholds` явно в списке "не трогать".

## 16. Claim↔Claim Semantics

Проверено кодом: `claim["verification_status"]` финализируется на `orchestrator_v2.py:3458`, Claim↔Claim NLI блок (`~3714-4210`) запускается СТРОГО ПОСЛЕ. Единственный потребитель Claim↔Claim relations — `_disagreement_engine.challenge(...)` (`orchestrator_v2.py:4107`), часть V6 personality/disagreement подсистемы (реакция персонажа на противоречия), не участвует в перезаписи `claims_data`/Claim Status Gate.

**CLAIM-CLAIM SELF-SUPPORT LOOP EXISTS: NO.** Подтверждено кодом — предположение пользователя ("вероятно, правильно") верно.

## 17. PASS2 Latency Anatomy

`claims=8 workers=3 wall=274.56s worker_sum=648.52s worker_max=106.86s`, `batch_return=14 direct=3 context=11 eligible=3`:

| claim | time | records | note |
|---|---|---|---|
| cl_84631540 (CORE) | 70.20s | 1 | |
| **cl_6bc5be75 (DIRECT)** | **83.29s** | **0** | **ZERO-USEFUL WORKER** — топовый по приоритету claim, ноль пользы |
| cl_41219688 (background: H/He) | 98.03s | 3 | |
| cl_3f86e388 (explanatory) | 32.61s | 1 | |
| cl_3264929e (explanatory) | 84.41s | 3 | |
| cl_c74154c1 (background: no surface) | 78.35s | 2 | |
| cl_6be0c14a (explanatory: artificial structures) | 94.76s | 2 | |
| cl_30267448 (background: extreme atmosphere) | 106.86s | 2 | |

**TIME PER USEFUL EVIDENCE:** 274.56s wall / 14 records-returned ≈ 19.6s/record; но только 3 из 14 оказались `eligible=True` → 274.56s / 3 eligible ≈ **91.5s на потенциально полезную единицу evidence**.

**ZERO-USEFUL WORKERS: 1 из 8** (12.5%), и это именно самый приоритетный (DIRECT_DECISION_EVIDENCE) claim — латентность НЕ коррелирует с полезностью в эту сторону.

## 18. Adaptive Semantic Budget Proposal (архитектурный план, НЕ реализован)

Наблюдение подтверждено: после CORE/DIRECT (ранги 1-2) идут 6 background/explanatory jobs (ранги 3-8), которые получают retrieval budget НЕЗАВИСИМО от того, разрешился ли CORE/DIRECT. Направление (не реализовано, требует отдельного прохода с собственными offline-тестами):

- После завершения retrieval workers для CORE/DIRECT claims (обычно первые в очереди по приоритету), проверить `_claim_has_effective_evidence()` (уже существует, `orchestrator_v2.py:~3067`) для них конкретно.
- Если CORE/DIRECT НЕ разрешились (как в этом прогоне) — это сигнал не для "меньше работать", а для "искать иначе" (см. §14) — сокращение background budget здесь не решило бы correctness-проблему, только скрыло бы её более быстрым, но столь же неверифицированным ответом.
- Если CORE/DIRECT разрешились быстро — оставшиеся worker slots можно перераспределить на explanatory/background НЕ тратя лишний latency budget заранее зарезервированный под них.
- НЕ предлагается тупо резать `MAX_CLAIMS` — семантический budget policy, завязанный на `role` (уже вычисляется, §B прошлого прохода), а не на позицию в списке.

## 19. Observability Added

- `[Final Claim Leakage] extracted=N known=N novel=N speculative=N` — новое.
- `[Claim Retrieval Query] claim_id=... anchors=[...] queries=[...]` — новое.
- `[Final Coverage Timing] ... extract_status=... coverage_status=...` — расширено существующее.
- `[Final Claim Coverage] ... status=...` — расширено существующее.
- **Уже существовало и не дублировалось** (проверено): `[Claim Status Gate] verified=N supported=N disputed=N contradicted=N candidate=N unverified=N rejected=N total=N` — это и есть Final Epistemic Contract diagnostic, запрошенный в задаче; `[Claim Status] claim=... supports=N contradicts=N secondary=N context=N` — это и есть Claim Resolution Trace per-claim. Оба уже полностью удовлетворяют требованиям observability без изменений.

## 20. Offline Regression Suite

Новый файл `agent/final_epistemic_regression_test.py`, реально выполнен целиком в этой среде (не изолированная симуляция для этой части — только §12/9 использует extraction чистых функций из-за bs4-ограничения sandbox):

```
python3 -m agent.final_epistemic_regression_test
РЕЗУЛЬТАТ: все проверки пройдены
```
15/15 проверок: P0-A (7 кейсов: unsupported-notice, contradicted-notice, healthy-untouched, partial-untouched, идемпотентность), P0-B/C (6 кейсов: факты>0, genuine-empty, call_error, parse_error, partial-coverage, novel-uncovered), P1 (2 кейса: anchor в claim, anchor во входе в query formulation), P3 (2 кейса: local registry не может стать eligible, role=context).

`agent/claim_priority_regression_test.py` (из прошлых проходов) — логика подтверждена НЕ затронутой (изолированный тест `_classify_claim_role` после P1-правки даёт те же CORE/DIRECT_DECISION_EVIDENCE результаты); полный `python3 -m` запуск в этой sandbox по-прежнему падает на отсутствующем `bs4` — то же самое (не новое) ограничение окружения, что и во всех предыдущих проходах этой сессии.

## 21. py_compile

```
python3 -m py_compile agent/final_claim_coverage.py agent/orchestrator_v2.py \
  agent/claim_evidence_retriever.py agent/final_epistemic_regression_test.py
FINAL_ALL_OK
```

## 22. Explicitly Unchanged Systems

claim role classifier, absence markers, retrieval priority weights, embedding weight, specificity weight, ClaimValidator meta-паттерны, Subject Gate thresholds, source_quality thresholds, evidence eligibility thresholds, NLI labels/prompt, Trust-формула (числа/пороги), worker count, `MAX_CLAIMS`, scraper proxy/browser routing, transport memory, registry data — ни один не тронут. Изменения ограничены: текст financial answer (только в 2 доказанных опасных случаях), статус extraction в final_claim_coverage, и три новых diagnostic print.

## 23. Remaining Risks

- P0-A эвристика (`startswith("⚠️ ВАЖНО:")`) предполагает, что banner (§5, `orchestrator_v2.py:5127`) добавляется ПОСЛЕ Claim Status Gate — что верно по текущему коду (banner на line ~5110, gate на ~4585), но если порядок когда-нибудь поменяется, idempotency-проверка может не сработать корректно. Низкий риск, легко тестируется regression suite'ом при следующих изменениях.
- P0-B length threshold (200 символов) для "suspicious_empty" — эвристика, не строго обоснованное число (аналогично RELEVANCE_WEIGHT=8.0 из прошлых проходов) — калибровка по мере накопления реальных данных.
- §12 (query subject loss) остаётся частично недоказанным — только наблюдаемость добавлена, root cause требует следующего live-run для окончательного вывода.
- §18 (adaptive budget) — только план, не реализация; если будет запрошено — отдельный проход с собственными offline-тестами.

## 24. ONE Full Integration Command

```bash
cd /home/iam/yandi
python3 -m agent.final_epistemic_regression_test && \
python3 agent/orchestrator_v2.py \
  "Есть ли разумная жизнь на Юпитере?" \
  --web --no-cache 2>&1 | \
tee /tmp/yandi_final_epistemic_integration.log | \
grep -E \
'Synthesizer Claims]|Claim Validator]|Claim Retrieval Priority]|Claim Retrieval Query]|Claim Retrieval Select]|Claim Retrieval Timing|Claim Status]|Claim Status Gate|Final Claim Coverage]|Final Claim Leakage]|Final Coverage Timing|YANDI PIPELINE WALL-CLOCK PROFILE|\[PROFILE\]|PROFILE BOTTLENECK|ВАЖНО:|Готово за|Latency:'
```

---

# SUMMARY

FINAL ANSWER GATE BUG FOUND: **YES**
FINAL ANSWER GATE FIXED: **PARTIAL** (два доказанных опасных случая покрыты — contradicted-dominant и supported=0/verified=0; общий "responder не может утверждать unsupported facts" инвариант НЕ гарантирован для всех промежуточных состояний без полного sentence-level verifier, что явно не строилось по инструкции)
UNVERIFIED CLAIMS CAN STILL BECOME ASSERTED FACTS: **PARTIAL** (в 2 доказанных worst-case сценариях — нет, текст маркируется; в промежуточных случаях типа "verified=0, supported>0" — да, текст не маркируется, только confidence снижается, сознательный компромисс)
NOVEL CLAIM LEAKAGE DETECTED: **YES**
NOVEL CLAIM LEAKAGE FIXED: **PARTIAL** (видимость есть — `[Final Claim Leakage]`; предотвращения появления novel claims в тексте нет, только диагностика)
FINAL COVERAGE ROOT CAUSE FOUND: **YES**
FINAL COVERAGE 0/0 PERFECT BUG FIXED: **YES**
FINAL FACTUAL EXTRACTION WORKS OFFLINE: **YES** (подтверждено 6 mock-сценариями, реально выполненными)
CORE CLAIM RETRIEVAL ROOT CAUSE FOUND: **YES** (source_quality unknown-domain cap + генерические источники без прямой detection-фразы)
DIRECT CLAIM RETRIEVAL ROOT CAUSE FOUND: **PARTIAL** (subject_gate/semantic_irrelevant механизм точно прослежен; первопричина ПОЧЕМУ discovery возвращает не-Jupiter-специфичные страницы не доказана до конца — добавлена observability для следующего прогона)
SUBJECT LOST DURING QUERY GENERATION: **UNKNOWN** (вход в generation подтверждённо сохраняет subject; выход — генерируемые LLM строки — не логировался раньше, теперь логируется, ответ будет в следующем прогоне)
ELIGIBILITY BLOCKS SUPPORT: **YES** (математически доказано ещё в первом аудите сессии, вновь подтверждено этим прогоном)
REGISTRY CAN SUPPORT CLAIM STATUS: **NO** (доказано формулой + offline тестом)
CLAIM-CLAIM SELF-SUPPORT LOOP EXISTS: **NO** (доказано порядком выполнения кода)
CLAIM_SPECIFIC_RETRIEVAL LATENCY ROOT CAUSE: не единая причина — 1 из 8 workers (12.5%) полностью бесполезен (0 records за 83s), остальные дают низкий eligible-yield (3 из 14 записей); сама latency НЕ коррелирует с полезностью
ADAPTIVE BUDGET RECOMMENDED: **PARTIAL** (архитектурное направление предложено §18, не реализовано — сначала нужно решить correctness §14/§15, иначе adaptive stop просто быстрее давал бы такой же неверифицированный ответ)
REGRESSION TESTS: **15/15 PASSED**
FILES CHANGED: `agent/final_claim_coverage.py`, `agent/orchestrator_v2.py`, `agent/claim_evidence_retriever.py`, `agent/final_epistemic_regression_test.py` (новый)
BACKUPS: `final_claim_coverage.py.bak_20260826_084503`, `orchestrator_v2.py.bak_20260826_084503`, `claim_evidence_retriever.py.bak_20260826_084503`
FULL ORCHESTRATOR RUN: NOT RUN
NEXT FULL TEST: см. §24 выше
