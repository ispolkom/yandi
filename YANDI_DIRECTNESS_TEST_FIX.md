# YANDI — Directness Fallback Test Fix

Дата: 2026-08-26
Триггер: fail-fast regression `[FAIL] directness возвращает 0.0 (не падает)
при недоступном Ollama — got 0.5500500202178955`, блокировавший старт
полного интеграционного прогона.

Scope: только диагностика + правка **теста**. Production-код
(`evaluate_evidence_directness`, composite gate, пороги, registry
exclusion) — **не изменён**, потому что бага в нём не найдено.

---

## ROOT CAUSE

Комбинация двух факторов:

1. **Тест был некорректным (сценарий D).** Строка, помеченная
   "Directness graceful degradation (без Ollama)", **никогда не
   отключала и не мокала Ollama** — она просто вызывала
   `evaluate_evidence_directness("тестовый claim", "тестовый passage")`
   напрямую и предполагала, что вернётся `0.0`. Тест был
   nondeterministic: его результат полностью зависел от того, доступен
   ли в момент запуска Ollama-демон и интерпретатор с `numpy`, а не от
   какого-либо явного условия в самом тесте.

2. **В моей диагностической sandbox-среде (`/usr/bin/python3`) этот
   тест раньше "проходил" случайно, по неверной причине.** Там нет
   пакета `numpy` (`ModuleNotFoundError: No module named 'numpy'`)
   — исключение перехватывалось тем же `except Exception:` внутри
   `evaluate_evidence_directness()`, что и `ConnectionError`, и функция
   возвращала `0.0`. Это выглядело как "graceful fallback при
   недоступном Ollama", а на самом деле было fallback-ом из-за
   отсутствующей зависимости в интерпретаторе, никак не связанной с
   доступностью Ollama.

В реальном project venv (`/home/iam/venv`, тот же, которым пользуется
живой прогон orchestrator) `numpy`/`requests`/`bs4` установлены,
Ollama реально запущен (`ollama ps` → `embeddinggemma:latest` в
памяти), и `evaluate_evidence_directness()` корректно достучался до
`127.0.0.1:11434/api/embed`, посчитал реальный embedding и вернул
настоящий cosine similarity — **это правильное поведение**, не
регрессия.

---

## OLLAMA ACTUALLY AVAILABLE: YES

Подтверждено напрямую:

```
$ ollama ps
NAME                     ID              SIZE      PROCESSOR    CONTEXT    UNTIL
embeddinggemma:latest    85462619ee72    680 MB    100% CPU     2048       ...
```

и прямым HTTP-пробом через `/home/iam/venv/bin/python3` с точно теми
же параметрами сессии, что использует production-код
(`session.trust_env = False`, тот же URL, та же модель):

```
status 200, embedding len 768
```

Дополнительно обнаружена причина, почему НАИВНЫЙ `requests.post()` без
`trust_env=False` в этой машине падает с `ProxyError` (окружение
экспортирует `http_proxy`/`https_proxy` на непроходной для loopback
адрес `45.147.182.91:8000`) — но это не задевает production-код: он
явно ставит `session.trust_env = False`, обходя системный прокси и
подключаясь к `127.0.0.1:11434` напрямую. Это преднамеренное и
корректное поведение, а не случайность.

## EMBEDDING ENDPOINT: `http://127.0.0.1:11434/api/embed`, модель `embeddinggemma:latest`, HTTP-клиент `requests.Session()` с `trust_env=False`, `timeout=15`.

## WHY VALUE WAS 0.55005:

Это настоящий cosine similarity между embeddings строк
`"тестовый claim"` и `"тестовый passage"` (обе — generic заглушки,
у которых общее слово "тестовый" и в целом умеренно близкая
семантика/структура для короткой русской фразы). Значение
~0.55 — правдоподобный средний cosine similarity для двух коротких,
частично похожих, но по смыслу разных фраз; воспроизведено детерминированно
через `/home/iam/venv/bin/python3` (`got 0.5500500202178955`, побитово
совпадает с тем, что прислал пользователь). Никакой ошибки в подсчёте
нет.

## PRODUCTION BUG: NO

`evaluate_evidence_directness()` работает ровно так, как
спроектировано: реальный embedding endpoint → реальный cosine
similarity, exception (любой, включая недоступность сети/модели) →
`0.0`. Ни цепочка вызовов, ни fallback-логика не менялись и не
требуют изменений.

## TEST BUG: YES

Тест утверждал, что тестирует "недоступность Ollama", но не создавал
этого условия никаким детерминированным способом — assertion
полагался на случайное состояние машины (наличие/отсутствие `numpy`,
доступность Ollama-демона). Это ровно тот case, который
пользователь предсказал в п.3 задания ("если никак — тест
nondeterministic").

---

## Исправление теста

Файл: `agent/evidence_eligibility_regression_test.py`
(backup: `agent/evidence_eligibility_regression_test.py.bak_20260826_101500`).

1. Прежний недетерминированный check заменён на явный
   `unittest.mock.patch("requests.Session.post", side_effect=ConnectionError(...))`
   — гарантированно форсирует exception внутри `_embed()` независимо от
   реального состояния Ollama/наличия numpy/сетевых условий машины.
   Проверяется: `evaluate_evidence_directness(...) == 0.0` и что
   исключение **не** проходит наружу.
2. Добавлен отдельный **live embedding sanity check** (не gate, не
   регрессия с жёстким порогом): сравнивает
   `directness(claim, passage_direct)` vs
   `directness(claim, passage_unrelated)` на примерах из задания
   (Юпитер/разумная жизнь vs Марс/температура). Если Ollama в моменте
   недоступен — оба вызова корректно деградируют до `0.0`, и sanity
   check **пропускается** (`[SKIP]`), не считается провалом теста.
   Если Ollama доступен — проверяется только относительный порядок
   (`direct > unrelated`), без фиксации конкретного числового порога.
3. Никакой рефакторинг production-кода / dependency boundary не
   потребовался — `unittest.mock.patch` на уровне `requests.Session.post`
   оказался достаточным без изменения `source_quality.py`.

---

## FALLBACK PATH DETERMINISTICALLY TESTED: YES

Через `/home/iam/venv/bin/python3` (реальный project venv, тот же,
которым пользуется live-прогон):

```
[OK] directness возвращает 0.0 (не падает) при принудительно
     недоступном Ollama (мокнутый ConnectionError)
```

## LIVE EMBEDDING SANITY: PASS

```
[OK] live sanity: directness(direct passage) > directness(unrelated passage)
     direct=0.8490 unrelated=0.2263
```

(Числа получены через реальный Ollama endpoint в project venv;
подтверждают, что directness действительно отличает семантически
релевантный passage от нерелевантного — не просто шум.)

---

## REGRESSION: 24/24 PASSED

(Ранее было 23 проверки; добавлена одна новая — live sanity check,
которая в реальном venv теперь реально выполняется, а не пропускается.)

Запуск через реальный project venv (обязательно, не системный
`/usr/bin/python3`, у которого отсутствует `numpy`):

```
$ /home/iam/venv/bin/python3 -m py_compile \
    agent/source_quality.py \
    agent/evidence_eligibility_regression_test.py
# без вывода — OK

$ /home/iam/venv/bin/python3 -m agent.evidence_eligibility_regression_test
...
РЕЗУЛЬТАТ: все проверки пройдены
```

**Побочное наблюдение методологии (не требует действия сейчас, но
важно на будущее):** во всех предыдущих раундах этой сессии offline
тесты запускались через `/usr/bin/python3` (системный, без `bs4`/
`numpy`), что было обосновано как "sandbox limitation". Обнаружено,
что в проекте есть полноценный venv `/home/iam/venv` со всеми
зависимостями (`numpy`, `requests`, `bs4`) — тот же, которым
пользуется реальный live-прогон orchestrator. Для будущих раундов
регрессионные тесты стоит запускать через
`/home/iam/venv/bin/python3`, а не системный интерпретатор — это
устраняет целый класс ложных "graceful degradation" срабатываний,
как в этом случае.

---

## FILES CHANGED:

- `agent/evidence_eligibility_regression_test.py` — недетерминированный
  Ollama-fallback check заменён на мокнутый (`ConnectionError`);
  добавлен отдельный live embedding sanity check (skip-если-недоступно).

`agent/source_quality.py` — **не изменён** (production-бага не найдено).

## BACKUPS:

- `agent/evidence_eligibility_regression_test.py.bak_20260826_101500`

## FULL ORCHESTRATOR RUN: NOT RUN
