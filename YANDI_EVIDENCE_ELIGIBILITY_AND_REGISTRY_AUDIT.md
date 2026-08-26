# YANDI — Evidence Eligibility & Registry Provenance Audit

Дата: 2026-08-26
Триггер: живой прогон `orchestrator_v2.py` («Есть ли разумная жизнь на Юпитере?»)
с корректным claim ranking (TOP-4 = CORE/CORE/DIRECT/CORE), но
`supported=0/unverified=19` в финальном Claim Status.

Scope этого раунда: **eligibility / directness / registry provenance /
final coverage parser / claim-claim NLI cost audit**. Полный список
"НЕ ТРОГАТЬ" соблюдён (claim role classifier, absence markers, query
relevance ranking, query generation, subject anchors, ClaimValidator,
Final Answer Gate, Trust formula, worker count, MAX_CLAIMS,
proxy/browser routing, transport memory — ни один из этих файлов/блоков
в этом раунде не редактировался).

---

## P0-A / P0-B. `source_quality.py` — математический аудит

### A1. Формула

```
quality_score = authority*0.45 + traceability*0.35 + primaryness*0.20
```

Для `source_class="unknown"` (когда домен не входит ни в один список
приоритетов) `evidence_eligible` требует:

```
quality_score >= 0.70  AND  authority >= 0.50  AND  traceability >= 0.70
```

### A2. Максимально достижимый `quality_score` по классам

| source_class | authority | primaryness | traceability (max) | max quality_score | eligible? |
|---|---|---|---|---|---|
| primary (`.gov`) | 0.90 | 0.95 | 1.00 | **0.945** | ДА |
| scientific (`.edu`/known) | 0.85 | 0.80 | 1.00 | **0.883** | ДА |
| reference (wikipedia/britannica) | 0.75 | 0.45 | 1.00 | **0.778** | ДА (порог для reference ниже) |
| unknown, есть host | 0.50 | 0.40 | 1.00 (эмпирически ≤1.0, реально ~0.6-0.8 для типового текста) | **≤0.655** | **НЕТ — математически недостижимо** (0.655 < 0.70) |
| unknown, нет host (local registry) | 0.25 | 0.20 | 1.00 | **≤0.3625** | **НЕТ, ещё дальше от порога** |
| blocked (`forum`/`social`/`news`/`popular_article`/`speculative`/`blog_opinion`/`generated_pipeline`) | — | — | — | — | **НЕТ, всегда** (hard block независимо от score) |

**Вывод A**: авторитетный (authority) путь к eligibility физически
непроходим для любого источника с `source_class="unknown"`, при ЛЮБОМ
качестве текста/структуры. Это не пограничный случай — это
математическая невозможность, встроенная в константы порога.

### B. Где именно решается `evidence_role=context` / `eligible=False`

`evaluate_source_quality()` в `source_quality.py`:
1. `_classify_source(url, title)` — присваивает `source_class` по спискам
   доменов/эвристикам (НЕ читает содержимое passage).
2. По `source_class` берутся authority/primaryness priors (тоже не
   зависят от passage).
3. `traceability` — единственный сигнал, частично зависящий от текста
   (структура/длина/наличие дат), но ограничен `<=1.0` и не может сам
   компенсировать низкий authority.
4. `evidence_role` = `direct` только если `source_class in
   {primary, scientific, reference}` — т.е. **тоже функция исключительно
   от source_class**, не от того, отвечает ли конкретный passage на
   конкретный claim.

**Подтверждённый диагноз**: SOURCE AUTHORITY (кто говорит) и EVIDENCE
DIRECTNESS (отвечает ли именно этот текст на именно этот claim) были
слиты в один сигнал (`quality_score`/`source_class`), у которого нет
входа "насколько passage релевантен claim'у". Мэппер (`claim_evidence_mapper.py`)
считает cosine similarity между claim и passage (`all_scores`,
`[Mapper Score]`), но это число использовалось только для выбора
топ-2 кандидатов на маппинг — **оно нигде не участвовало в
eligibility/role decision**. Это и есть корневая причина
`supported=0` при корректном ranking: даже claim с правильно
подобранным, семантически точным evidence с unknown-домена не мог
физически преодолеть authority-порог.

---

## P0-C. Claim Status Truth Table (после фикса)

`_counts_toward_status(rel)` — единственная точка входа в
`supports_count`/`contradicts_count` (заменяет прежний
`evidence_role=="direct" and evidence_eligible is True`).

| evidence_role | evidence_eligible | source_class | retrieval_origin | directness | relation (NLI) | counted? | via |
|---|---|---|---|---|---|---|---|
| direct | True | primary/scientific/reference | web | любая | supports | ДА | authority |
| direct | True | primary/scientific/reference | web | любая | unrelated | ДА (счётчик решает relation-фильтр ниже, не eligibility) | authority |
| context | False | unknown | web | ≥0.60 | supports | **ДА (НОВОЕ)** | directness |
| context | False | unknown | web | <0.60 | supports | НЕТ | — |
| context | False | forum/social/news/... (blocked) | web | ≥0.60 | supports | **НЕТ** (authority-блок доминирует) | — |
| context | False | unknown | **local_registry** | даже 0.95 | supports | **НЕТ** (явное исключение реестра) | — |
| direct | True | primary/scientific/reference | web | 0.0 | contradicts | ДА (в contradicts_count, не supports) | authority |

Инвариант сохранён: `eligible=False` + низкий directness ⇒ никогда не
считается. Новый путь строго ỳже старого: добавляет **ровно один**
дополнительный случай (unknown/не-blocked/не-registry + directness ≥ 0.60),
не ослабляет ни одно существующее условие.

---

## P0-D. Причинная трассировка TOP-4 claims последнего живого прогона

Последний прогон (TOP-4 = CORE/CORE/DIRECT/CORE, `supported=0/unverified=19`)
выполнялся **до** появления сигнала `directness` — в его логах этого
числа физически нет (функция `evaluate_evidence_directness()` и
логирование `[Evidence Eligibility]` появились только в этом раунде).
Поэтому реальную таблицu "directness per evidence" для ТОГО прогона
восстановить нельзя — это не пробел в анализе, а честная граница
данных.

То, что можно восстановить из прежних раундов (`[Claim Retrieval
Priority]`, `[Pass2 Trace]`, `evidence_role`/`eligible` в тех логах):

| claim (топ-4, роль) | evidence найдено | evidence_role (старое) | eligible (старое) | supports до фикса | ожидаемый эффект после фикса |
|---|---|---|---|---|---|
| #1 CORE | 2-3 web (unknown-домены) + 1 registry | context | False (все) | 0 | часть могла перейти в supported, если directness≥0.60 |
| #2 CORE | 1-2 web (unknown) | context | False | 0 | то же |
| #3 DIRECT | 1 web (unknown) + 1 wiki-подобный (если попал) | context/direct смешанно | зависело от классификации | 0 либо частично | если был reference-класс — уже должен был считаться и раньше; если unknown — потенциальный кандидат на directness |
| #4 CORE | 1-2 web (unknown) + registry | context | False | 0 | то же |

**Явный вывод**: точную числовую трассировку по этому конкретному
прогону нельзя закрыть без нового живого запуска с уже внедрённым
`[Evidence Eligibility]`/`[Claim Support Decision]` логированием — это
и есть рекомендованный NEXT LIVE TEST в конце отчёта.

---

## P0-E. Registry Provenance — что представляет запись реестра

Прочитан `agent/orch_registry_search.py::_extract_docs_from_file()`
(строки 1-110) и подтверждено grep'ом по `evidence_pool.py`:

- Источник данных реестра: `registry/dataset/{model_sessions,final,orch_traces}`
  — это **буквальные записи прошлых Q&A-сессий самой модели** (JSONL).
- `_extract_docs_from_file()` присваивает `"trust_level": "UNVERIFIED"`
  **безусловно, для 100% записей**, без какого-либо иного provenance-поля.
- `evidence_pool.py`: записи реестра получают `source_type="local"`,
  `retrieval_origin="local_registry"`, URL отсутствует.

**Ответ на P0-E (варианты A-F)**: **A — unverified model memory**.
Это не кэш верифицированных фактов (C), не epistemic trace с
провенансом (D), не запись с собственной proof-chain (E) — это
непроверенный "что модель сказала в прошлый раз", технически
неотличимый от собственной галлюцинации при повторном использовании
без независимой проверки.

**Меры защиты, принятые в этом раунде**:
1. `_counts_toward_status()` явно исключает `retrieval_origin ==
   "local_registry"` из НОВОГО directness-пути — реестр не может стать
   eligible через directness.
2. Старый authority-путь и так недостижим для реестра (§A2, quality
   ≤0.3625) — двойная защита, не единая точка отказа.
3. Никакого нового "доверия по умолчанию" реестру не добавлено.

---

## P0-F. Реализованный минимальный фикс: разделение Authority и Directness

### Дизайн (не переиспользует существующие пороги произвольно)

Добавлена **независимая ось** `evidence_directness` — cosine similarity
между текстом claim и текстом конкретного passage (embeddings через
уже используемую в проекте модель `embeddinggemma:latest`, тот же
endpoint, что и claim-claim prefilter).

Порог `DIRECTNESS_SUPPORT_THRESHOLD = 0.60` — **не новое произвольное
число**: это точно тот же порог, который `claim_relation.py::classify_relation()`
уже использует для своего embedding-fallback SUPPORTS-решения
(переиспользование существующей калибровки, не изобретение новой).

### Композитный гейт (замена прежнего единственного условия)

```python
def _counts_toward_status(rel):
    if rel.get("evidence_role") == "direct" and rel.get("evidence_eligible") is True:
        return True, "authority"
    if (
        rel.get("source_class") not in HARD_BLOCKED_SOURCE_CLASSES
        and rel.get("retrieval_origin") != "local_registry"
        and float(rel.get("directness", 0.0) or 0.0) >= DIRECTNESS_SUPPORT_THRESHOLD
    ):
        return True, "directness"
    return False, None
```

Свойства:
- Старое поведение (authority-путь) **не изменено ни на бит** — это
  строго `OR`, не замена.
- Blocked-классы (`forum`/`social`/`news`/`popular_article`/`speculative`/
  `blog_opinion`/`generated_pipeline`) остаются заблокированы **даже
  при directness=0.99** — авторитетность источника всё ещё имеет право
  вето, что и требовалось ("не давать local registry доверие
  автоматически" по аналогии).
- Реестр явно исключён (см. P0-E).

### Изменённые файлы

- `agent/source_quality.py` — добавлена `evaluate_evidence_directness(claim_text, passage_text) -> float`, graceful fallback `0.0` при недоступном Ollama/ошибке.
- `agent/orchestrator_v2.py`:
  - импорт `evaluate_evidence_directness`;
  - `_run_claim_evidence_batch()`: вычисление `directness` на каждую (claim, evidence) пару кандидатов, новое поле `directness` в `candidate_sources`/`evidence_relations`, новый лог `[Evidence Eligibility]`;
  - Claim Status: `_counts_toward_status()` + новый лог `[Claim Support Decision]` вместо прежнего инлайн list comprehension.

---

## P1. Обобщаемая политика для unknown-доменов

P0-F **и есть** ответ на P1: вместо жёсткого списка доменов (что
пользователь явно запретил), используется существующий,
domain-агностичный сигнал — семантическая близость claim↔passage,
уже посчитанная в проекте тем же embedding-механизмом, что и
claim-claim prefilter. Никакой новый домен нигде не захардкожен.
Любой неизвестный сайт теперь может стать источником support/contradict,
если и только если его конкретный текст (а не репутация домена)
семантически отвечает на claim — это ровно то поведение, которое
требовалось ("сигналы title/URL/DOI/publisher/passage структура", из
которых наиболее дёшево и надёжно доступен именно passage-контент
через embeddings; DOI/publisher-парсинг для произвольных доменов не
существует в проекте и потребовал бы новой инфраструктуры, что
выходит за рамки минимального фикса).

---

## P1-B. Аудит: несправедлив ли барьер для negative/absence claims

**Не редактировалось** — только аудит, как и предписано.

До этого раунда absence-claims (напр. "разумная жизнь не обнаружена")
были вынуждены находить evidence с authority-путём, чтобы засчитаться,
— то есть тем же барьером, что и любой позитивный claim. Проблема была
не в самих absence-маркерах (те уже исправлены в предыдущих раундах —
`_NEGATION_GAP`, existential-negation), а именно в том, что evidence,
подтверждающее отсутствие чего-либо, почти всегда приходит с
нейтральных/энциклопедических/новостных источников (Wikipedia — ОК,
но обзорные научно-популярные статьи — `popular_article`, блокируется
жёстко) или generic web-страниц (`unknown`, недостижимый порог).

**Результат после P0-F**: барьер для absence-claims снят ровно
настолько же, насколько и для позитивных claims — новый directness-путь
не делает различий между "supports X происходит" и "supports X не
происходит", решение остаётся за NLI (`relation`), не за eligibility.
Тест 10a/10b в `evidence_eligibility_regression_test.py` подтверждает:
низкая directness по-прежнему не спасает absence-claim (не введено
скрытой поблажки), высокая directness + NLI `supports` — теперь
может засчитаться (раньше не могло никогда).

**Вывод P1-B**: да, барьер был объективно завышен для ЛЮБОГО claim с
unknown-domain evidence (не специфично для absence), и это тот же
корневой P0 баг — отдельного фикса для negative claims не требуется.

---

## P1-C. Final Claim Coverage — `parse_error raw_len=1442`

### Root cause

`_extract_json()` в `final_claim_coverage.py` пыталась распарсить
сырой текст напрямую и через один `re.search(r"\{.*\}")` — ломалось на:
markdown code fence (```json ... ```), prose-текст перед JSON, и
висячие запятые перед `}`/`]`.

### Фикс

- Сначала пробуем извлечь fenced-блок (` ```json ... ``` `), затем сырой текст.
- Если прямой `json.loads` не сработал — ищем `{...}` через regex, пробуем как есть, затем с удалёнными висячими запятыми (`,\s*([\]}])`  → `\1`).
- Новый `_format_hint(text)` — **не дампит** тело ответа, только классифицирует: `has_code_fence` / `prose_before_json` / `no_brace_found` / `trailing_comma_suspected` / `unstripped_think_tag` / `empty` / `unknown_format_issue`.
- Все 4 сайта логирования (`call_error`, 2×`parse_error`, generic exception) переведены на единый формат:
  `[Final Claim Extraction] status=... raw_len=... format_hint=... [error=...]`.

Проверено на 5 сценариях (fenced JSON, prose+JSON без fence, trailing
comma, валидный `{"claims": []}`, неразбираемый мусор) — все 5 прошли
(см. §Regression, тесты 9a-9e).

---

## P2. Аудит: квадратичная стоимость Claim↔Claim NLI (только измерение, без фикса)

Код прочитан целиком (`orchestrator_v2.py:3860-4140`).

### Механизм

1. `total_pairs = n*(n-1)/2` — теоретическое число пар (19 claims → 171).
2. Считаются embeddings один раз на claim (линейная стоимость, не NLI).
3. Строится `candidate_pair_keys` как **объединение (OR)** двух условий:
   - `similarity >= CLAIM_CONFLICT_SIM_THRESHOLD` (0.30) — глобальный порог;
   - пара входит в top-`CLAIM_CONFLICT_TOP_K` (3) ближайших соседей **любого** из двух claims.
4. Только `candidate_pair_keys` идут в дорогой batch LLM NLI.
5. При сбое embeddings — **fail-open**: берутся ВСЕ пары (корректность важнее стоимости).

Существующий лог `[Claim↔Claim Prefilter] claims=... total_pairs=...
candidates=... skipped=... threshold=0.30 top_k=3 semantic=...` уже
даёт точные числа на каждый прогон (наблюдение подтверждено grep,
строки ~4126-4137).

### Почему префильтр слабо режет пары

Условие — **OR**, не **AND**: даже если top-k не выбрал пару, она всё
равно попадёт в NLI, если cosine similarity ≥ 0.30. Для claims,
сгенерированных из ОДНОГО ответа на ОДИН вопрос (все claims — про
Юпитер), большинство пар тематически близки и превышают 0.30 cosine
почти автоматически. Из данных предыдущих раундов (10 claims, 45
теоретических пар): `skipped=6`, `candidates=39` — то есть отсеяно
всего ~13% пар. При росте claims с 10 → 19 задержка бакета
`claim_claim_nli` в PROFILE выросла кратно сильнее, чем линейно —
согласуется с тем, что и число NLI-пар растёт почти как n².

### Вывод P2

**CLAIM_CLAIM_NLI QUADRATIC COST: PARTIAL.** Префильтр существует и
технически не пропускает 100% пар, но при тематически однородном
наборе claims (типичный случай — все claims из одного ответа на один
вопрос) его эффективное сокращение мало (~13% в наблюдаемых данных),
поэтому фактическая стоимость остаётся близкой к квадратичной, а не
подавляется до линейной/O(n·k). Порог 0.30 слишком низкий, чтобы
отсеивать тематически смежные, но логически независимые claims —
но менять его в этом раунде запрещено ("audit + concrete proposal",
не фикс).

---

## P2-B. Предложение: двухфазный семантический бюджет (только архитектура, без реализации)

**Идея**: не менять prefilter-логику, а менять **какие claims вообще
участвуют** в claim-claim NLI, используя УЖЕ существующую classification
(`CORE`/`DIRECT_DECISION_EVIDENCE`/`EXPLANATORY`/`BACKGROUND` из
claim role classifier, который в этом раунде трогать нельзя, но
можно ЧИТАТЬ его вывод).

**Фаза 1** — обязательная: все пары внутри `{CORE, DIRECT_DECISION_EVIDENCE}`
проверяются на конфликт всегда (это claims, которые реально решают
финальный ответ — именно они должны быть железно непротиворечивы).

**Фаза 2** — условная: `EXPLANATORY`/`BACKGROUND` claims сравниваются
между собой и с фазой 1 **только если** после фазы 1 остались
`unverified`/`contradicted` claims в топе, ЛИБО если общий claim count
превышает некий бюджет (например, >12 claims) — иначе фаза 2
пропускается целиком, логируется явно (`[Claim↔Claim Budget] phase2_skipped
reason=...`), без "тихого" урезания.

**Почему это не нарушает "не трогать role classifier"**: используется
только как read-only вход для решения "сравнивать или нет", сам
classifier не модифицируется.

**Ожидаемый эффект**: для типичного прогона (19 claims, из которых
обычно 4-6 CORE/DIRECT) число обязательных пар падает с ~171 до
~15-30 в фазе 1, с опциональным расширением только при необходимости
— потенциально кратное (не на 13%, а в разы) сокращение LLM-вызовов
для claim_claim_nli. **Не реализовано в этом раунде** (P2-B — proposal
only, по явному указанию).

---

## Regression Tests

Файл: `agent/evidence_eligibility_regression_test.py` (новый).

```
$ python3 -m agent.evidence_eligibility_regression_test
...
РЕЗУЛЬТАТ: все проверки пройдены
```

23/23 проверки пройдены (source_quality math table ×7, composite gate
×8, final coverage parser ×5, negative-claim directness ×2, graceful
degradation ×1). Плюс 1 документационный пункт (case 6 — registry
provenance architecture gap, намеренно не тест, а зафиксированное
ограничение).

`python3 -m py_compile` — чисто на всех изменённых файлах:
`source_quality.py`, `orchestrator_v2.py`, `final_claim_coverage.py`,
`evidence_eligibility_regression_test.py`.

---

## FILES CHANGED

- `agent/source_quality.py` — добавлена `evaluate_evidence_directness()`.
- `agent/orchestrator_v2.py` — импорт directness-функции; вычисление и логирование `directness`/`[Evidence Eligibility]` в `_run_claim_evidence_batch()`; замена Claim Status gate на композитный `_counts_toward_status()` + `[Claim Support Decision]`.
- `agent/final_claim_coverage.py` — устойчивый `_extract_json()` (fence/prose/trailing comma); новый `_format_hint()`; единый формат `[Final Claim Extraction]`.
- `agent/evidence_eligibility_regression_test.py` — новый файл, 23 проверки.

## BACKUPS

- `agent/source_quality.py.bak_20260826_092840`
- `agent/orchestrator_v2.py.bak_20260826_092840`
- `agent/final_claim_coverage.py.bak_20260826_092840`

## FULL ORCHESTRATOR RUN: NOT RUN

---

## ИТОГОВЫЙ БЛОК ОТВЕТОВ

- **SOURCE QUALITY / DIRECTNESS COUPLED**: YES (было подтверждено — единая формула `quality_score`/`source_class` не имела входа для passage-claim релевантности; теперь разделены)
- **UNKNOWN DOMAIN CAN BECOME ELIGIBLE**: YES (через новый directness-путь, если ≥0.60 и NLI подтверждает relation; authority-путь по-прежнему недостижим)
- **LOCAL REGISTRY CAN BECOME ELIGIBLE**: NO (явно исключён из directness-пути; authority-путь математически недостижим)
- **REGISTRY PROVENANCE PRESERVED**: PARTIAL (provenance как таковой не существует в схеме — есть только безусловный `UNVERIFIED`; "preserved" в смысле "не улучшен искусственно, трактуется как unverified" — да; но структурного provenance-поля для будущей дифференциации нет — это архитектурный пробел, не наша правка)
- **ELIGIBILITY ROOT CAUSE FOUND**: YES (математически недостижимый порог для unknown/local source_class, конфляция authority и directness)
- **CLAIM STATUS SUPPORT GATE VERIFIED**: YES (23/23 regression, включая инвариант "низкая directness всё ещё не считается")
- **CORE CLAIM SUPPORT BLOCKER**: устранён для случаев unknown-domain + семантически точный passage (via directness); НЕ устранён для случаев, где evidence физически не содержит ответа на claim (директный тест 4/7 подтверждает — это ожидаемо и корректно)
- **DIRECT CLAIM SUPPORT BLOCKER**: тот же корневой блокер, что и CORE — фикс общий, не специфичен для роли claim
- **NEGATIVE CLAIM EVIDENCE ISSUE**: PARTIAL (барьер был объективно завышен, но не специфично для negative claims — устранён тем же общим P0-F фиксом; NLI-логика самих absence-маркеров не менялась, как и требовалось)
- **FINAL COVERAGE PARSE ROOT CAUSE**: FOUND & FIXED (markdown fence / prose-prefix / trailing comma не обрабатывались; исправлено, добавлена `format_hint`-диагностика без дампа тела)
- **CLAIM_CLAIM_NLI QUADRATIC COST CONFIRMED**: PARTIAL (префильтр существует, но OR-логика с низким порогом 0.30 слабо режет тематически однородные claims — ~13% сокращение на наблюдаемых данных, не фундаментальное решение)
- **SEMANTIC TWO-PHASE BUDGET RECOMMENDED**: YES (архитектура предложена в P2-B, НЕ реализована в этом раунде по прямому указанию)
- **REGRESSION TESTS**: 23/23 PASSED
- **FILES CHANGED**: `agent/source_quality.py`, `agent/orchestrator_v2.py`, `agent/final_claim_coverage.py`, `agent/evidence_eligibility_regression_test.py` (новый)
- **BACKUPS**: `agent/source_quality.py.bak_20260826_092840`, `agent/orchestrator_v2.py.bak_20260826_092840`, `agent/final_claim_coverage.py.bak_20260826_092840`
- **FULL ORCHESTRATOR RUN**: NOT RUN
- **NEXT LIVE TEST**: `python3 agent/orchestrator_v2.py "Есть ли разумная жизнь на Юпитере?"`
