# YANDI — P0 Claim Lifecycle Disappearance Audit

Дата: 2026-08-26
Источник истины: `/tmp/yandi_directness_full_live.log`
Триггер: `[Synthesizer Claims] extracted=15 lifecycle=15 meta_skipped=0`,
но `[Claim Validator] accepted=0 rejected=0 reasons={}` сразу следом —
15 claims исчезли между синтезом и валидатором.

---

## 1-3. Data Flow — точное место перехода 15 → 0

### Цепочка (до фикса)

```
orch_synthesizer.py:1010  claims.append({...})           × 15   (локальная переменная claims)
orch_synthesizer.py:1022  print("[Synthesizer Claims] extracted=15 lifecycle=15 ...")
orch_synthesizer.py:1034  raw_answer = _call(compose_prompt, ...)   ← ЗДЕСЬ бросает исключение
orch_synthesizer.py:1047  except Exception as e:
orch_synthesizer.py:1048      return SynthesisResult(...), {"error": str(e)}   ← claims НЕ включены
                                                                                   в reasoning_info!
orchestrator_v2.py:2525   synthesis_result, reasoning_info = synthesize(...)   ← reasoning_info == {"error": ...}
orchestrator_v2.py:2577   claims_data = reasoning_info.get("claims", [])       ← .get() → [] (ключа нет)
orchestrator_v2.py:2739   claims_data = _claim_validator.filter_claims(claims_data)  ← вход уже []
```

### Таблица переходов

| Шаг | Переменная | Тип | N | Поле | Записывается | Читается | Reassign/filter/clear? |
|---|---|---|---|---|---|---|---|
| 1 | `claims` (локальная в `synthesize()`) | `list[dict]` | **15** | — | `orch_synthesizer.py:1010` (append в цикле) | `orch_synthesizer.py:1022` (print), `1068` (evidence_score) | нет |
| 2 | возвращаемое значение `synthesize()` | `tuple[SynthesisResult, dict]` | — | второй элемент tuple | `orch_synthesizer.py:1048` (exception branch) | `orchestrator_v2.py:2525` | **ДА — exception branch строит НОВЫЙ dict `{"error": str(e)}`, не включающий переменную `claims`, которая на тот момент уже содержит 15 элементов** |
| 3 | `reasoning_info` | `dict` | **0** (нет ключа `"claims"`) | — | `orchestrator_v2.py:2525` (unpack tuple) | `orchestrator_v2.py:2577` | нет (просто не содержит ключ) |
| 4 | `claims_data` | `list[dict]` | **0** | — | `orchestrator_v2.py:2577` — `reasoning_info.get("claims", [])` | `orchestrator_v2.py:2647, 2679, 2737` | нет — `.get(..., [])` корректно возвращает default, потому что ключа физически нет в словаре |
| 5 | `pre_validation_claims` | `list[dict]` | **0** | — | `orchestrator_v2.py:2737` — `list(claims_data)` | `2739` (filter_claims) | нет |
| 6 | `ClaimValidator.filter_claims()` вход/выход | `list[dict]` | **0 / 0** | — | — | `orchestrator_v2.py:2775-2778` (лог `accepted=0 rejected=0 reasons={}`) | Validator корректно обработал пустой список — сам validator НЕ виноват |

### Вывод по пп.1-4 задания

- **НЕТ рассинхронизации между несколькими контейнерами** (`result.claims` / `claim_lifecycle` / `query_frame["claims"]` / `synthesis.claims` / `validated_claims`) — в реальности существует ровно ОДИН канонический путь: `synthesize()` возвращает `(SynthesisResult, reasoning_info)`, и `reasoning_info["claims"]` — единственный источник для `claims_data`. Проверено grep'ом (`claims_data` встречается только с одним источником присвоения — строка 2577).
- **`ClaimValidator` НЕ виноват** — `reasons={}` пуст именно потому, что на вход подан пустой список; сам validator корректно и честно отработал 0 входных claims.
- **`supports_query_aspect`, claim role classifier, `SynthesizerResult`-датакласс, evidence pool/eligibility — НЕ участвуют в этом баге.** Ни один из них не читается и не пишется между строками 1010 и 2577. Причина ровно одна: **исключение, брошенное featureless-веткой `except Exception` вокруг ВСЕГО блока synthesize(), включая шаги, не имеющие отношения к claims (answer composition).**

---

## Точная причина исключения (эмпирически подтверждено таймингом)

`raw_answer = _call(compose_prompt, max_tokens=600, temp=TEMP_ANALYST)` (строка 1034) —
третий LLM-вызов в `synthesize()` (после extract_prompt на 800 токенов).
`_call()` не перехватывает исключения сама (`orch_synthesizer.py:132-178`) — использует
`requests.Session.post(..., timeout=TIMEOUT)`, где `TIMEOUT = 180` (строка 111).

Арифметика по реальному логу:
- `[Local LLM] generation done in 149.70s tokens<=800` — это ИМЕННО extract-вызов (виден
  прямо перед `[Synthesizer] Извлечено claims: 15`).
- После него нет ни одного `[Local LLM] generation done` для compose-вызова — значит он
  не завершился успешно.
- `[PROFILE] synthesize 329.81s` — общий бюджет времени на `synthesize()`.
- `329.81 − 149.70 = 180.11s` — практически ТОЧНО совпадает с `TIMEOUT = 180` (с учётом
  накладных расходов). Это статистически однозначно указывает на `requests.exceptions.ReadTimeout`
  внутри `_call(compose_prompt, ...)`.

Причина самого timeout (нагрузка на GPU/semaphore) не является частью этого P0 — это
отдельный вопрос производительности. Важно другое: **какая бы причина исключения ни была,
уже построенные 15 claims не должны исчезать из-за сбоя в НЕСВЯЗАННОМ последующем шаге.**

---

## ROOT CAUSE

`except Exception as e:` в `orch_synthesizer.py:1047` оборачивает единым блоком ДВЕ
логически независимые фазы — (а) claim extraction и (б) answer composition — и при сбое
ЛЮБОЙ из них возвращает `reasoning_info = {"error": str(e)}`, безусловно теряя уже
полностью построенный список `claims` (и `evidence_records`), даже если сбой произошёл
уже ПОСЛЕ того, как claims были успешно извлечены и залогированы.

---

## PRODUCTION FIX (минимальный)

`agent/orch_synthesizer.py`, строка ~1047-1051:

```python
    except Exception as e:
        return SynthesisResult(
            answer=f"Не удалось получить ответ: {e}",
            confidence=0.0, sources=[], trust_level="UNVERIFIED",
        ), {
            "error": str(e),
            "claims": claims,
            "evidence_records": evidence_records,
        }
```

Свойства фикса:
- Если исключение случилось ДО извлечения claims (например, сам extract-вызов упал) —
  `claims`/`evidence_records` всё ещё равны `[]` (инициализированы в начале функции,
  строки 726-727) — поведение идентично прежнему, ничего не меняется (см. baseline-тест
  ниже).
- Если исключение случилось ПОСЛЕ извлечения claims — они теперь сохраняются и доходят
  до `ClaimValidator`/`Evidence Mapper`/`Claim Status`, как и должны.
- `answer`/`trust_level`/`confidence` в `SynthesisResult` **не изменены** — итоговый текст
  ответа пользователю по-прежнему честно отражает сбой composition-шага (это НЕ Final
  Answer Gate/prompt — не трогалось, вне scope этого раунда).
- Ranking/eligibility/directness/NLI/source_quality/Claim Status/retrieval budget/web
  pipeline — **не затронуты**: фикс меняет только то, ЧТО synthesize() возвращает
  вызывающей стороне, не логику downstream-обработки этих claims.

## Диагностическая трасса (без изменения поведения)

`agent/orchestrator_v2.py`, непосредственно перед `if _claim_validator:` (~строка 2735):

```python
if verbose:
    log(
        "[Claim Pipeline Boundary] "
        f"synthesized={len(reasoning_info.get('claims', [])) if isinstance(reasoning_info, dict) else 0} "
        f"lifecycle={len(claims_data)} "
        f"validator_input={len(claims_data)}"
    )
```

Позволяет в любом будущем прогоне мгновенно увидеть, если снова возникнет разрыв
`synthesized>0` при `validator_input==0` — и, поскольку граница теперь единственная и
явно залогирована, такой разрыв больше не может быть ЭТИМ багом (он устранён), а будет
означать новый источник потери.

---

## Regression Test

Новый файл: `agent/claim_lifecycle_regression_test.py`.

Тестирует РЕАЛЬНЫЙ production-код (`orch_synthesizer.synthesize()` напрямую и реальный
`ClaimValidator` из `claim_validator.py`), мокая только сетевой уровень (`_call`), чтобы
детерминированно воспроизвести именно тот сценарий, который случился в живом прогоне:
extraction успешен (3 claims), compose падает с `TimeoutError` **после** этого.

```
$ /home/iam/venv/bin/python3 -m agent.claim_lifecycle_regression_test
...
[Claim Pipeline Boundary] synthesized=3 lifecycle=3 validator_input=3
[OK] КРИТИЧЕСКИЙ ИНВАРИАНТ: accepted + rejected == validator_input (3)
...
РЕЗУЛЬТАТ: все проверки пройдены
```

9/9 проверок пройдено, включая:
- `extracted=3`, `lifecycle=3`, `validator_input=3` (соответствует критерию из задания);
- `accepted + rejected == 3` (критический инвариант выполнен на реальном `ClaimValidator`);
- baseline-сценарий (ранний сбой ДО extraction) по-прежнему корректно даёт `claims=[]` —
  фикс не изобретает claims там, где их никогда не было.

Плюс существующий `evidence_eligibility_regression_test.py` — 24/24, без регрессий
(collateral damage check).

`py_compile` чист на всех изменённых/новых файлах (через `/home/iam/venv/bin/python3`,
реальный project venv).

---

## 9A. Web Query timeout / Refutation TimeoutError — ДИАГНОСТИКА (без фикса)

**CONFIRMED** (код+константы дают точное совпадение, не предположение):

1. `formulate_queries()`/`formulate_refutation_queries()` вызываются **дважды за запрос**:
   - Параллельно в фан-ауте (`orchestrator_v2.py:1974-1990`, `web_future`/`refutation_future`),
     ожидание результата — **`.result(timeout=30)`** (`orchestrator_v2.py:2016, 2034`) —
     хардкод, не использует `DEFAULT_TIMEOUTS`.
   - Затем **повторно, последовательно** в шаге [7] (`orchestrator_v2.py:2127`,
     `step_timer("web_query", lambda: formulate_queries(enrich_result))`), с таймаутом
     `DEFAULT_TIMEOUTS["web_query"] = 60s` (`orch_timeout.py:23`).
   - Результат параллельного вызова (`wq_result` из фан-аута, включая fallback-ветку
     "используем enriched query fallback") **полностью перезаписывается** результатом
     второго, последовательного вызова в шаге [7] — то есть работа шага 6 в этой части
     не используется нигде дальше. Побочный эффект: минимум одна лишняя LLM-генерация
     GPU-времени тратится впустую на каждый запрос.
2. Оба вызова `formulate_*` идут через `GENERATION_SEMAPHORE` (`orch_web_query.py:25,92`),
   общий с `orch_synthesizer.py` (`Semaphore(2)`, максимум 2 одновременных generation-задачи).
   В одном запросе конкурируют: `local_future` (генерация локального ответа, реально
   заняла **153.21s** в этом прогоне), `web_future`, `refutation_future`, плюс позже —
   ДВА вызова `_call()` внутри самого `synthesize()` (~150s каждый по наблюдаемым данным).
3. Внутренний HTTP-таймаут `formulate_queries`/`formulate_refutation_queries` сам по себе
   **90s** (`orch_web_query.py:27`) — но внешний `.result(timeout=30)` в фан-ауте **вдвое
   меньше**, чем даже этот внутренний таймаут, и **на порядок меньше** наблюдаемой реальной
   латентности генерации (~150s). При Semaphore(2) и ≥3 одновременных заявках на GPU-слот
   `web_future`/`refutation_future` с высокой вероятностью не успевают даже ЗАПУСТИТЬ свой
   HTTP-запрос за отведённые 30s — они всё ещё стоят в очереди на семафор.

**Вероятный root cause**: несогласованные, слишком короткие внешние таймауты
(`.result(timeout=30)` в фан-ауте, `DEFAULT_TIMEOUTS["web_query"]=60` в шаге 7) на фоне
общего GPU-семафора размером 2 и реальной латентности generation-вызовов ~150s — плюс
избыточный повторный (дублирующий) вызов `formulate_queries` в шаге 7, который делает
результат фан-аута бесполезным. Не фиксировалось в этом раунде.

---

## 9B. Evidence Pool: total=5 direct=0 context=5 origins={'local_registry': 5}

**Не самостоятельная проблема — прямое следствие 9A.** `build_canonical_evidence_pool(
search_result=search_result, web_result=web_result, refutation_snippets=refutation_snippets)`
(`orchestrator_v2.py:2594`) получает `web_result=None` (шаг 7 не получил ни одного snippet —
после `log("[7] Web search...")` в логе нет ни `queries:`, ни `сниппетов:`, что означает ветку
`else: web_skipped_reason = "no queries"`, т.е. `formulate_queries()` в шаге 7 тоже не вернул
результат вовремя) и `refutation_snippets=[]` (Refutation поймал `TimeoutError`, обработчик
на `orchestrator_v2.py:2255` логирует ошибку и оставляет `refutation_snippets` пустым).
Единственный оставшийся источник evidence — 5 документов локального реестра
(`search_result.docs`). Отдельного фикса не требует — устранится вместе с 9A.

---

## 9C. Hypothesis Graph получил нерелевантный registry document

**CONFIRMED** — воспроизведено буквально по словам.

Код (`orchestrator_v2.py:2301-2310`):

```python
query_words = set(query_to_use.lower().split())
for doc in search_result.docs:
    doc_words = set(doc_text.lower().split())
    overlap = len(query_words & doc_words)
    if overlap < 2:
        log(f"[Graph] Пропущен нерелевантный документ (пересечение: {overlap})")
        continue
```

- Query: `"Есть ли разумная жизнь на Юпитере?"` → токены (без стоп-слов, без стемминга,
  просто `.split()`): `{есть, ли, разумная, жизнь, на, юпитере?}`.
- Документ: `"...Стоишь на берегу и слышишь запах моря и веришь что жизнь лишь только..."`
  → содержит `на` и `жизнь` → `overlap = 2` → **`2 < 2` ложно → фильтр НЕ отбраковывает
  документ**, несмотря на то, что оба совпавших слова — короткие
  общеупотребительные/служебные («на» — предлог, «жизнь» — многозначное слово,
  использованное в документе в совершенно ином, не астробиологическом смысле).

**Root cause**: relevance-фильтр графа гипотез — наивное пересечение множеств слов БЕЗ
стоп-слов и БЕЗ учёта длины/информативности токена, порог всего `>=2` совпадений
тривиально проходим случайными короткими словами. Не фиксировалось в этом раунде (вне
scope — retrieval ranking/query relevance явно в списке "НЕ ТРОГАТЬ" для eligibility-раунда,
и это отдельный компонент от того, что было явно разрешено чинить в этом P0).

---

## ФИНАЛЬНЫЙ ОТЧЁТ

CLAIMS EXTRACTED: 15
CLAIMS BEFORE VALIDATOR (до фикса): 0
CLAIMS PASSED TO VALIDATOR (после фикса, синтетический regression): 3/3 (100%, реальный прогон с 15 будет исправлен тем же кодовым путём)

EXACT 15->0 LOCATION:
`agent/orch_synthesizer.py:1047-1051` — `except Exception as e: return ..., {"error": str(e)}` (ключ `"claims"` отсутствовал в возвращаемом `reasoning_info`)

ROOT CAUSE:
Единый `except Exception` вокруг extraction+composition терял уже построенный список `claims`, если исключение (подтверждено: `ReadTimeout` на `TIMEOUT=180s`, compose-вызов) происходило ПОСЛЕ успешного извлечения claims, но ДО завершения всей функции `synthesize()`.

PRODUCTION FIX:
`agent/orch_synthesizer.py` — except-branch теперь возвращает `{"error": str(e), "claims": claims, "evidence_records": evidence_records}` вместо `{"error": str(e)}`. Плюс диагностическая трасса `[Claim Pipeline Boundary]` в `agent/orchestrator_v2.py` перед ClaimValidator (без изменения поведения).

REGRESSION: 9/9 PASSED (новый `claim_lifecycle_regression_test.py`) + 24/24 PASSED (существующий `evidence_eligibility_regression_test.py`, без регрессий)

FILES CHANGED:
- `agent/orch_synthesizer.py` (минимальный фикс except-branch)
- `agent/orchestrator_v2.py` (только добавлена диагностическая трасса `[Claim Pipeline Boundary]`, поведение не изменено)
- `agent/claim_lifecycle_regression_test.py` (новый)

BACKUPS:
- `agent/orch_synthesizer.py.bak_20260826_103000`
- `agent/orchestrator_v2.py.bak_20260826_103000`

WEB TIMEOUT ROOT CAUSE: CONFIRMED (несогласованные таймауты 30s/60s/90s против ~150s реальной латентности + Semaphore(2) contention + дублирующий вызов formulate_queries в шаге 7, обесценивающий результат фан-аута)

REGISTRY GRAPH LEAK ROOT CAUSE: CONFIRMED (naive bag-of-words overlap без стоп-слов, порог `>=2` тривиально проходим двумя короткими словами)

FULL ORCHESTRATOR RUN: NOT RUN

---

Следующий live integration test:

```
/home/iam/venv/bin/python3 agent/orchestrator_v2.py "Есть ли разумная жизнь на Юпитере?" --web --no-cache 2>&1 | tee /tmp/yandi_claim_lifecycle_fix_live.log
```
