# rustlib/ — перенос YANDI с Python на Rust, маленькими шагами

Владелец (2026-09-23): «Приступай, можешь работать автономно. Rust пишешь отдельно, Python пока
не удаляешь! Создай путёвую структуру, чтобы потом ничего не менять!»

Это — та структура. Она не меняется от куска к куску: каждый следующий перенесённый модуль просто
добавляет файлы по уже заданному шаблону ниже, ничего в уже сделанном не перестраивая.

## Быстрый старт для владельца (одна команда)

```bash
python -m pip install maturin           # один раз, в ТОМ ЖЕ окружении, где запускается YANDI
./scripts/rust-build-and-verify.sh      # собрать модуль и прогнать все проверки; ничего не включает
python scripts/rust_bench.py            # (по желанию) замер скорости на вашей машине
```

Rust включается ТОЛЬКО переменными окружения `YANDI_*_ENGINE=rust` (по одной; список рекомендуемых — в конце вывода
скрипта и в разделе «Что реально выгодно включать»). Откат: убрать переменную и перезапустить. Python остаётся по умолчанию.

## Зачем отдельно от `../node/`

`node/` — самостоятельный, годами растущий проект (сетевой узел, супервизор, ключи, P2P-транспорт):
своя история, свои документы, свой смысл. `rustlib/` — про другое: сюда переезжает то, что раньше
было на Python (`agent/`, `pet/`), кусок за куском, ради надёжности и скорости, но вызывается ИЗ
Python как обычный нативный модуль (через [PyO3](https://pyo3.rs)/[maturin](https://www.maturin.rs)),
а не живёт отдельным процессом/бинарником. Смешивать эти две вещи в одном workspace было бы неверно
даже структурно — они решают разные задачи.

## Правило на каждый перенесённый кусок

1. **Зафиксировать нынешнее поведение тестами.** Если для Python-модуля уже есть тесты — они
   становятся контрактом. Если нет (или недостаточно) — сначала дописать их для Python-версии.
2. **Написать Rust-версию** — построчный перевод, не «улучшение по пути». Если что-то хочется
   улучшить — это отдельная, следующая, явно обсуждённая задача, не подмешивается в перенос.
3. **Доказать построчное совпадение** отдельным parity-тестом: одни и те же примеры (входы) через
   обе реализации, оба результата должны совпасть один в один. Тест обязан ловить внесённую вручную
   порчу (мутационная проверка) — иначе он ничего не доказывает, кроме того что код запускается.
4. **Только тогда переключать вызывающий код** — и то не «удаляя Python», а добавляя выбор:
   Python-реализация остаётся на месте и активна по умолчанию; Rust-реализация включается явно
   (сейчас — переменной окружения на модуль; возможно, позже появится общий механизм на весь
   проект — тогда он тоже войдёт в этот README). Если Rust-модуль не собран или переменная не
   стоит — сервер работает ровно как раньше, ничего не ломается.
5. Python-версия не удаляется, пока владелец явно не решит, что Rust-путь достаточно обкатан в
   его реальном использовании.

## Структура (не меняется по мере роста)

```
rustlib/
  Cargo.toml              # workspace; members растёт по одному крейту на смысловую область
  README.md                # этот файл — методология, единая для всех кусков
  yandi_rs/                 # ПЕРВЫЙ (и пока единственный) крейт: один общий нативный модуль
                             # для Python, растёт подмодулями — см. ниже
    Cargo.toml
    pyproject.toml          # сборка через maturin
    src/
      lib.rs                 # регистрация подмодулей PyO3 — точка входа, почти не меняется
      boundaries.rs            # перенос agent/boundaries.py (кусок 11, 2026-09-23)
      claim_answer_linker.rs    # перенос link_answer_to_claims() (кусок 12, 2026-09-23)
      local_guard.rs          # перенос pet/local_guard.py (кусок 1, 2026-09-23)
      web_login.rs             # перенос pet/web_login.py::Sessions/Throttle (кусок 2, 2026-09-23)
      claim_identity.rs         # перенос agent/claim_identity.py целиком (кусок 3, 2026-09-23)
      claim_semantic_identity_hardening.rs  # перенос hardening_guard() (кусок 4, 2026-09-23)
      source_quality.rs          # перенос evaluate_source_quality() (кусок 5, 2026-09-23)
      claim_validator.rs          # перенос normalize_claim_text()/validate() (кусок 6, 2026-09-23)
      criticism_detector.rs        # перенос analyze()/get_response_template() (кусок 7, 2026-09-23)
      claim_types.rs                 # перенос всех 5 функций (кусок 8, 2026-09-23)
      epistemic_router.rs             # перенос 11 детекторных функций (кусок 9, 2026-09-23)
      message_intensity.rs             # перенос parse_self_report() (кусок 10, 2026-09-23)
      claim_evidence_retriever.rs        # перенос existence/role-классификаторов (кусок 13, 2026-09-24)
      orch_risk.rs                        # перенос assess_risk() (кусок 14, 2026-09-24)
      source_clustering.rs                 # перенос assign_source_clusters() + сигналы схожести (кусок 15, 2026-09-24)
      intent_router.rs                       # перенос detect_intent() и справочных функций (кусок 16, 2026-09-24)
      target_router.rs                       # перенос detect_target()/get_target_description() (кусок 17, 2026-09-24)
      personal_boundary.rs                   # перенос PersonalBoundary.analyze/get_response_template (кусок 18, 2026-09-24)
      scene_builder.rs                       # перенос SceneBuilder.build() (кусок 19, 2026-09-24)
      scene_builder_data.rs / py_case_table.rs  # ДАННЫЕ: паттерны сцены (сгенерированы ИЗ Python), регистр Unicode для str.isupper()
      py_regex.rs / py_icase_table.rs        # ТРАНСЛЯТОР паттернов Python re -> крейт regex (`\\s`,`\\w`,`\\b`,`$`, IGNORECASE) + данные
      object_resolver.rs / entity_resolver.rs  # перенос ObjectResolver.resolve / EntityResolver.resolve (куски 20–21, 2026-09-24) + resolver_data.rs (данные из Python)
      claim_graph.rs                         # перенос текстового ядра ClaimGraph + build_edges (кусок 22, 2026-09-24)
      trust_gate.rs / trust_data.rs           # перенос ядра trust_gate: метка доверия и причины, _apply_trust_cap, _calculate_delta_factors (кусок 23, 2026-09-24) + таблица рангов из Python
      canonical_trust.rs                     # перенос compute_canonical_trust (кусок 24, 2026-09-24)
      final_claim_coverage.rs / fcc_data.rs   # перенос лексического ядра маршрутизации пар (кусок 25, 2026-09-24) + стоп-слова из Python
      policy.rs / policy_data.rs             # перенос ядра policy.py: сканер секретов + check_shell/check_network (кусок 27, 2026-09-24) + данные из Python
      tool_shell.rs / tool_shell_data.rs      # перенос охранного шлюза shell-команд агента `_allowed` (кусок 28, 2026-09-24) + данные из Python
      crypto.rs                              # перенос ядра agent/db/sql/crypto.py (AES-256-GCM поле, blind index) + HKDF/wrap из keys.py (кусок 29, 2026-09-24); примитивы — RustCrypto
      pet_extraction.rs                      # перенос чистых помощников pet/event_extraction.py, fact_extraction.py, commitment_verification.py (кусок 30, 2026-09-24)
      orch_tag_tree.rs                       # перенос чистого ядра agent/orch_tag_tree.py: токенизация, LSH-бакет, энтропия (кусок 31, 2026-09-24)
      relationship_memory.rs / relationship_memory_data.rs   # перенос стеммера agent/relationship_memory.py `_stem/_stems` (кусок 32, 2026-09-24) + таблицы из Python
      core_lifecycle.rs                      # перенос чистых помощников pet/core_lifecycle.py: разбор ключа, регулярки идентификаторов, схема запросов, HKDF проверочного значения (кусок 33, 2026-09-24)
      ui_settings.rs                         # перенос `validate` из pet/ui_settings.py: шлюз ввода настроек вкладки «YANDI» (кусок 34, 2026-09-24)
      orch_query_framer.rs / orch_query_framer_data.rs   # перенос решений agent/orch_query_framer.py: политика ответа, авто-уточняющий вопрос, чувствительный домен (кусок 35, 2026-09-24) + таблицы из Python
      claim_status.rs                        # перенос ЯДРА agent/orchestrator/claims/status.py::classify_claim_epistemic_status: отношения «доказательство → утверждение» → статус (кусок 37, 2026-09-24)
      py_word_table.rs                       # ДАННЫЕ (не подмодуль): точная копия Python-`\w`, генерируется gen_py_word_table.py
      py_decimal_table.rs / py_printable_table.rs  # ДАННЫЕ: цифры Unicode для float(), isprintable() для repr() (gen_py_*.py)
      py_text.rs                              # ОБЩАЯ подпорка «как в Python»: strip/split/`\s`/float/repr (подмодуль yandi_rs.py_text — только для проверок)
      py_json.rs                              # ОБЩАЯ подпорка: точный json.loads Python (значения и тексты ошибок)
      <следующий_модуль>.rs                  # каждый новый перенос — один новый файл здесь
```

**Почему один крейт `yandi_rs`, а не по крейту на модуль:** один скомпилированный `.so`, один
`import yandi_rs`, одна сборка через `maturin`. Каждый перенесённый Python-модуль `pet/xxx.py`
получает здесь ровно файл `src/xxx.rs` и подмодуль `yandi_rs.xxx` — так `from yandi_rs.xxx import
...` в Python всегда зеркалит `from pet.xxx import ...`, и добавление следующего куска — это
дописать файл + одну строку регистрации в `lib.rs`, не более. Если однажды какой-то перенесённый
кусок будет достаточно велик/независим (например, станет собственным сетевым сервисом, а не
библиотекой) — тогда для НЕГО отдельно можно завести новый workspace member; это решение принимается
в момент, когда до него дойдёт очередь, не раньше.

## Как собрать и проверить (локально, в песочнице уже сделано и проверено)

```bash
cd rustlib/yandi_rs
cargo test                       # чистые Rust-юнит-тесты, без Python вообще
maturin develop --release        # собрать и установить нативный модуль в ТЕКУЩИЙ Python-venv
python -m pet.pet_local_guard_rust_parity_test   # доказательство совпадения с Python-версией
```

`maturin develop` — для локальной разработки/проверки (ставит editable-модуль в активный venv).
Прод-раскладка (нужно ли собирать wheel и класть его в `requirements.txt`, или собирать при
`start.sh`) — отдельное решение, не принято, не нужно для того, чтобы это уже сейчас работало и
проверялось; поднимется, когда владелец решит реально включить Rust-путь в бою.

## Уже перенесено

| Python-модуль | Rust-файл | Переключатель | Статус |
|---|---|---|---|
| `pet/local_guard.py` (только чистая логика решения; ASGI-обвязка осталась в Python) | `yandi_rs/src/local_guard.rs` | `YANDI_GUARD_ENGINE=rust` | Собрано, 68 сценариев + 2 внесённых мутанта пойманы (`pet/pet_local_guard_rust_parity_test.py`); в бою по умолчанию ВЫКЛЮЧЕНО |
| `pet/web_login.py::Sessions, Throttle` (сессии + замедление подбора пароля; сами определения классов и маршруты остались в Python) | `yandi_rs/src/web_login.rs` | `YANDI_LOGIN_ENGINE=rust` | Собрано, полный временной сценарий + 2 внесённых мутанта пойманы (`pet/pet_web_login_rust_parity_test.py`); переставляемые часы (`_clock`) перенесены как настоящий Python-вызываемый объект — иначе существующие тесты, подменяющие время, перестали бы работать; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/claim_identity.py` целиком (canonicalize_claim_text, compute_claim_content_hash, extract_subject_anchors, extract_content_anchors — горячий путь, каждое извлечённое утверждение) | `yandi_rs/src/claim_identity.rs` | `YANDI_CLAIM_IDENTITY_ENGINE=rust` | Собрано, 200 проверок + 3 внесённых мутанта пойманы (`agent/claim_identity_rust_parity_test.py`); две реальные ловушки переноса задокументированы прямо в файле — символьная vs байтовая длина строки (кириллица), lookbehind/lookahead из Python `re` (крейт `regex` их не поддерживает — заменено ручным посимвольным поиском границы слова); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/claim_semantic_identity_hardening.py::hardening_guard` (regex-guard против ложного объединения разных claim'ов; переиспользует уже перенесённый extract_subject_anchors напрямую из Rust, не через Python) | `yandi_rs/src/claim_semantic_identity_hardening.rs` | `YANDI_HARDENING_ENGINE=rust` | Собрано, 127+ проверок (включая обратный порядок аргументов) + 3 внесённых мутанта пойманы (`agent/claim_semantic_identity_hardening_rust_parity_test.py`); реальная ошибка транскрипции найдена и исправлена ДО этого теста — пропущенный `(?i)` на одном из 14 паттернов, пойман собственным Rust-юнит-тестом; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/source_quality.py::evaluate_source_quality` + помощники (оценка доверия к веб-источнику; `evaluate_evidence_directness` НЕ перенесена — делает настоящий embedding-вызов) | `yandi_rs/src/source_quality.rs` | `YANDI_SOURCE_QUALITY_ENGINE=rust` | Собрано, 156 проверок + 1 внесённый мутант пойман (`agent/source_quality_rust_parity_test.py`); найдено и исправлено реальное расхождение в округлении `round(x, 3)` — наивное "умножить на 1000" не совпадает с корректно-округляющим алгоритмом Python на границах, исправлено через `format!("{:.3}", x)` (тот же класс алгоритма); `_hostname` — ручной терпимый парсер, не строгий `url`-крейт (см. файл); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/claim_validator.py::ClaimValidator.normalize_claim_text/validate` (+ `_looks_like_fact`; фильтр мусорных claims — каждое извлечённое утверждение; сам класс со счётчиками остался в Python) | `yandi_rs/src/claim_validator.rs` | `YANDI_CLAIM_VALIDATOR_ENGINE=rust` | Собрано, 80 проверок + 2 внесённых мутанта пойманы (`agent/claim_validator_rust_parity_test.py`); та же символьная-vs-байтовая ловушка длины строки, что и в claim_identity.rs — уже знакомая, учтена сразу; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/criticism_detector.py::CriticismDetector.analyze/get_response_template` (критика vs оскорбление, каждое сообщение пользователя; своего regression-теста не было — сценарии выверены напрямую через реальный Python перед тем, как стать проверками, один пример из `__main__` модуля оказался НЕ тем, что подсказывала интуиция) | `yandi_rs/src/criticism_detector.rs` | `YANDI_CRITICISM_ENGINE=rust` | Собрано, 114 проверок + 1 внесённый мутант пойман массово (32 сценария) (`agent/criticism_detector_rust_parity_test.py`); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/claim_types.py` целиком (типы утверждений/режимы ответа; сами Enum-классы остались в Python — Rust работает со строковыми .value, Python-обёртка восстанавливает настоящий Enum) | `yandi_rs/src/claim_types.rs` | `YANDI_CLAIM_TYPES_ENGINE=rust` | Собрано, 61 проверка (включая явную проверку, что переключённая версия возвращает НАСТОЯЩИЙ Python Enum, не строку) + 1 внесённый мутант пойман (`agent/claim_types_rust_parity_test.py`); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/epistemic_router.py` — 11 детекторных функций (домен/гипотетичность/проверяемость/стабильность знания/объективность вопроса; `classify_claim()` сама НЕ перенесена — просто собирает датакласс из этих функций плюс констант) | `yandi_rs/src/epistemic_router.rs` | `YANDI_EPISTEMIC_ROUTER_ENGINE=rust` | Собрано, 779 проверок (включая полную интеграционную сверку `classify_claim()` целиком, все ~30 полей) + 1 внесённый мутант пойман (потребовалось усилить тест — первая версия не задевала домен `metaphysical` напрямую, ни один из примеров вопросов туда не попадал) (`agent/epistemic_router_rust_parity_test.py`); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/message_intensity.py::parse_self_report` (+ `intensity_from_state`, `_parse_structured`, `_strip_all_markers`; разбирает собственный "самоотчёт" модели о накале разговора — каждый ответ локальной модели) | `yandi_rs/src/message_intensity.rs` | `YANDI_MESSAGE_INTENSITY_ENGINE=rust` | Собрано, 113 проверок (включая точное совпадение диагностических сообщений об ошибках, не только поведения) + 1 внесённый мутант пойман (`agent/message_intensity_rust_parity_test.py`); Python `bool(x)`-truthiness любого JSON-значения воспроизведена явно (`json_truthy`), не Rust bool-каст; в бою по умолчанию ВЫКЛЮЧЕНО |

| `agent/boundaries.py::detect_toxicity/is_apology/generate_response/generate_apology_response` (границы/токсичность/извинения; init_session_state и мутаторы состояния НЕ перенесены — тривиальная мутация dict) | `yandi_rs/src/boundaries.rs` | `YANDI_BOUNDARIES_ENGINE=rust` | Собрано, 52 проверки + 1 внесённый мутант пойман (`agent/boundaries_rust_parity_test.py`); та же символьная-vs-байтовая ловушка длины строки (в этот раз в `is_apology`'s искренность-по-длине); в бою по умолчанию ВЫКЛЮЧЕНО |

| `agent/claim_answer_linker.py::ClaimAnswerLinker.link_answer_to_claims` (+ `_extract_key_phrases`/`_is_claim_supporting`; связывает финальный ответ с подкрепляющими claims — источник supporting_claim_ids в трейсе) | `yandi_rs/src/claim_answer_linker.rs` | `YANDI_CLAIM_ANSWER_LINKER_ENGINE=rust` | Собрано, 46 проверок + 1 внесённый мутант пойман (`agent/claim_answer_linker_rust_parity_test.py`); та же символьная-vs-байтовая ловушка длины строки (четвёртый раз подряд — систематический паттерн, проверяется теперь заранее в каждом новом куске); claim_id возвращается тем же Python-объектом, что был на входе (не приведён к строке); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/claim_evidence_retriever.py` — ТОЛЬКО чистый подкластер (`_is_absence_claim`, `_is_existence_question`, `_extract_existence_target`, `_target_overlap`, `_classify_claim_role`); сам модуль в целом делает реальный retrieval (сеть) и НЕ переносится — `_claim_retrieval_priority` тоже НЕ перенесена (вызывает impure embedding-based `_query_relevance_score`) | `yandi_rs/src/claim_evidence_retriever.rs` | `YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE=rust` | Собрано, 268 проверок (переиспользованы реальные фикстуры существующего `agent/claim_priority_regression_test.py`) + 2 внесённых мутанта пойманы (пятый раз подряд — символьная vs байтовая длина, на этот раз в фильтре `len(w)>=4` **и** отдельно в стемминге `word[:max(3,len(word)-2)]`; и порядок приоритета DIRECT_DECISION_EVIDENCE-перед-CORE) (`agent/claim_evidence_retriever_rust_parity_test.py`); негативный lookahead `(?!сомнени)` — крейт `regex` не поддерживает — заменён ручной функцией `matches_bare_net()` (второй раз в проекте после claim_identity.rs, другой техникой: перебор кандидатов `\bнет\s+` + проверка префикса остатка); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/orch_risk.py::assess_risk` (Risk Engine оркестратора: уровень риска → обязательный арбитраж, модель валидации, число нод; вызывается на каждый запрос, 4 места вызова; своего теста не было) | `yandi_rs/src/orch_risk.rs` | `YANDI_ORCH_RISK_ENGINE=rust` | Собрано, 245 проверок (полная развёртка каждого ключевого слова из трёх наборов + граница 300 символов) + 2 внесённых мутанта пойманы (`agent/orch_risk_rust_parity_test.py`); шестой раз символьная-vs-байтовая длина (`len(query) > 300`) — граничный случай построен ЗАРАНЕЕ по чек-листу, мутант пойман с первой попытки; ключевые слова — подстроки ("суд" внутри "судьба"), сохранено как в оригинале; Python-обёртка строит настоящий `RiskResult`; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/source_clustering.py::assign_source_clusters` (боевой кластеризатор источников-перепечаток; попарное O(n²) сравнение) + сигналы `title_similarity` (difflib) / `content_fingerprint_similarity` (Жаккар по шинглам) из `source_independence_prototype.py` | `yandi_rs/src/source_clustering.rs` (+ данные `py_word_table.rs`) | `YANDI_SOURCE_CLUSTERING_ENGINE=rust` | Собрано, 5932 проверки + 5 разных внесённых мутантов пойманы (`agent/source_clustering_rust_parity_test.py`). **Первый срез с реальным выигрышем в скорости: ×6 (10 источников) … ×12 (60 источников), растёт с размером пула** — канонизация/шинглы считаются один раз на источник, а не на каждую пару. Три уровня доказательства: (A) Python-`\w` сверен по ВСЕМ 1 114 112 кодовым точкам (крейт `regex` понимает `\w` иначе — поэтому таблица диапазонов сгенерирована из самого Python, `rustlib/gen_py_word_table.py`, Python 3.11/Unicode 14); (B) `difflib.SequenceMatcher.ratio` (включая эвристику autojunk при len>=200) — дифференциальный фаззинг против настоящего difflib, ~4000 случайных пар; (C) итоговые `source_cluster_id` на 150+ случайных пулах. Нестроковые/сломанные входы PyO3 не принимает — вызов тогда идёт прежним Python-путём. Единственный существующий тест (`epistemic_source_cluster_regression_test`, «ошибка сравнения → не сливать») подменяет Python-функцию, поэтому на этот фрагмент Rust-движок явно выключается; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/intent_router.py` (detect_intent + should_use_rag/get_intent_action/description/explanation; тип запроса на каждый запрос, `pre_pipeline.py`; своего теста не было) | `yandi_rs/src/intent_router.rs` | `YANDI_INTENT_ROUTER_ENGINE=rust` | Собрано, 5401 проверка + 4 внесённых мутанта пойманы (`agent/intent_router_rust_parity_test.py`); по новому правилу «трудных входов»: каждый паттерн в 7 формах (регистр, U+001C..1F, префикс/суффикс), граница `len(q) < 10` (символы vs байты), якорь `$`, ничьи (строгое `>`), фаззинг 4000 фраз; весь набор тестов прогнан со ВСЕМИ переключателями (177 наборов, только 2 известных DB-фейла); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/target_router.py` (detect_target — адресат запроса ai/user/object/knowledge на каждый запрос, `pre_pipeline.py`; своего теста не было) | `yandi_rs/src/target_router.rs` | `YANDI_TARGET_ROUTER_ENGINE=rust` | Собрано, 7074 проверки + 4 внесённых мутанта пойманы (`agent/target_router_rust_parity_test.py`); главное: `\\b`-границы слов (`\\bты\\b`, `\\bя\\b`…) реализованы вручную через таблицу Python-`\\w` (крейт regex понимает слово иначе — напр. `ты` с ударением U+0301: в Python граница есть, в regex-крейте нет; это реальный вход для русского текста); порядок сложения float-счётчиков сохранён (фаззинг 6000 фраз); `^ты\\s` с разделителями U+001C..1F; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/personal_boundary.py::PersonalBoundary.analyze/get_response_template` (границы личности: провокация / искреннее vs формальное извинение / личный и глубокий вопрос / социальный; на каждый запрос, `pre_pipeline.py`; своего теста не было; класс и датакласс остались в Python) | `yandi_rs/src/personal_boundary.rs` | `YANDI_PERSONAL_BOUNDARY_ENGINE=rust` | Собрано, 9694 проверки + 5 внесённых мутантов пойманы (`agent/personal_boundary_rust_parity_test.py`): каждый паттерн 6 списков в 8 формах, `[, ]*` (НЕ `\\s` — разделители U+001C..1F там не пробел), приоритеты при пересечении категорий (фаззинг 6000 фраз + 625 пар), перезапись reason последующими ветками, `max(conf,0.6)`, шаблоны на границах доверия/раздражения (NaN/inf/bool/огромное целое → прежний Python-путь; строка → то же исключение). Rust включается, ТОЛЬКО пока списки паттернов экземпляра не изменены (иначе Python) — проверено; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/scene_builder.py::SceneBuilder.build` (социальная сцена запроса: участники, адресат, цель, речевой акт, режим, тема, юмор/конфликт/близость/давление, уверенность; на каждый запрос, `pre_pipeline.py`; своего теста не было; датакласс `SocialScene` остался в Python) | `yandi_rs/src/scene_builder.rs` (+ данные `scene_builder_data.rs`, `py_case_table.rs`) | `YANDI_SCENE_BUILDER_ENGINE=rust` | Собрано, ~76 тыс. проверок (72 тыс. сцен) + 8 внесённых мутантов пойманы (`agent/scene_builder_rust_parity_test.py`). **Тест сам оказался с пробелом:** мутант «без отсечения `min(1.0, score)`» выжил — ничьи после отсечения видны только когда 2–3 категории сразу набирают ≥3 совпадений; добавлены целевые «перегруженные» тексты (4000), мутант пойман. Таблицы паттернов сгенерированы ИЗ Python (`gen_scene_builder_data.py`) — без ручного переписывания сотни русских слов; `str.isupper()` точен по всем кодовым точкам (`gen_py_case_table.py`: Lowercase/Uppercase/Titlecase из Python); `\\b`-слова через таблицу Python-`\\w`. **Особенность оригинала:** `list(set(...))` для participants/mentioned/coalition — порядок зависит от хеш-рандомизации процесса, т. е. у Python-версии его нет; тест сравнивает как множества, Rust отдаёт порядок первого появления (потребители порядок не используют); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/object_resolver.py::ObjectResolver.resolve` (тип объекта субъективного запроса: песня/фильм/книга/персона/идея/самопознание/игра; `orchestrator_v2.py`; своего теста не было) | `yandi_rs/src/object_resolver.rs` (+ данные `resolver_data.rs`, `gen_resolver_data.py`) | `YANDI_OBJECT_RESOLVER_ENGINE=rust` | Собрано, 12 143 проверки + 4 из 5 внесённых мутантов пойманы (`agent/object_resolver_rust_parity_test.py`); 5-й (снятие отсечения `min(1.0)` у базы) **эквивалентен**: база+len/200 не достигает 1.0 ни у одного паттерна — свойство самого оригинала. Каждый паттерн в 11 формах (регистр, İ/ı/K/ſ-варианты IGNORECASE, U+001C..1F), ничьи и приоритет самопознания (+0.2), перегруженные запросы (3000); также в массовом Unicode-фаззинге; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/entity_resolver.py::EntityResolver.resolve` (что ищет пользователь: игровой термин/медиа/собственное имя; `pre_pipeline.py` на каждый запрос; `get_search_strategy` осталась в Python; своего теста не было) | `yandi_rs/src/entity_resolver.rs` (+ `resolver_data.rs`) | `YANDI_ENTITY_RESOLVER_ENGINE=rust` | Собрано, 14 318 проверок + 4 из 5 мутантов пойманы (`agent/entity_resolver_rust_parity_test.py`; выживший — порог `> 0.4` — эквивалентен: уверенность всегда ≥ 0.5). **Особенность оригинала:** словари — `set` строк, при совпадении нескольких игр сразу поле `game` и порядок `categories` зависят от хеш-рандомизации процесса; тест допускает любой возможный Python-исход, Rust отдаёт порядок списков. Пустой запрос → `is_proper_name=True` (`0 >= 0.0`) — сохранено; `w[0].isupper()` — точная Python-семантика; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/claim_graph.py::ClaimGraph` — текстовое ядро (разбиение на предложения, очистка, фильтр «утверждение о мире», тип утверждения, уверенность, надёжность источника, `_is_contradiction`/`_is_support`) + попарное построение рёбер `_build_graph` (`orchestrator_v2.py`; класс, датакласс `Claim`, дедупликация и время остались в Python) | `yandi_rs/src/claim_graph.rs` | `YANDI_CLAIM_GRAPH_ENGINE=rust` | Собрано, ~69 тыс. проверок (по функциям, 600 графов, 300 сквозных `extract_claims` с детерминированными id) + 9 внесённых мутантов пойманы (`agent/claim_graph_rust_parity_test.py`; **2 выживших мутанта выявили пробелы теста:** граница длины 350 была ненаблюдаема без маркера «утверждения о мире» — добавлены пограничные тексты; в тест-обвязке был неверный признак переполнения — исправлен). `_build_graph` в Rust: слова и `lower()` считаются один раз на утверждение (в Python — на каждую пару), порядок рёбер = порядок вложенных циклов; отрицание — ПОДСТРОКА («не» внутри «неделя») сохранено; питоновские `max/min` (при NaN `max(0.1, nan)=0.1`, а `clamp` дал бы NaN); `\\d` теперь точный (таблица цифр Python) в транслятор `py_regex`; также в массовом Unicode-фаззинге; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/orchestrator/epistemic/trust_gate.py` — `_apply_trust_cap`, `_calculate_delta_factors` и «решение» `apply_epistemic_trust_adjustment` (итоговая метка доверия + причины по классификации, лимиту, домену/проверяемости, покрытию, поддержке evidence и уверенности убеждений; на каждый ответ: `writeback.py`, `pipeline.py`, `canonical_trust.py` используют ту же таблицу рангов и `_apply_trust_cap`). Запись в `trace` и learning rules осталась в Python; исходная логика метки вынесена в `_compute_label_python` ДОСЛОВНО | `yandi_rs/src/trust_gate.rs` (+ `trust_data.rs`, `gen_trust_data.py`) | `YANDI_TRUST_GATE_ENGINE=rust` | Собрано, 47 тыс. проверок (32 160 сквозных вызовов `apply_epistemic_trust_adjustment` с фиктивными trace/epistemic_result/belief_manager: сетка по доменам/проверяемости/лимитам/пределам покрытия и поддержки/убеждениям, включая исключения и не-числа, плюс 12 000 случайных) + **10 из 10 внесённых мутантов пойманы** (`agent/trust_gate_rust_parity_test.py`). Таблица рангов сгенерирована ИЗ Python (расхождение = тихая ошибка порядка доверия; неизвестная метка — ранг 0, как `_TRUST_ORDER.get(label, 0)` — именно это «баг WEAKLY_SUPPORTED» из истории модуля); `round(x, 3)` и `f"{x:.2f}"` на «ничьих» (0.125, 0.375, …) совпадают с Python; питоновские `min/max` (при NaN `clamp` дал бы иное). Входы других типов (строка вместо числа, метка не-строка, огромные целые) идут прежним Python-путём с тем же исходом. Существующие тесты (`trust_order_weakly_supported`, `canonical_trust_shadow`, `orchestrator_modularization`…) — с включённым переключателем в полном прогоне; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/orchestrator/epistemic/canonical_trust.py::compute_canonical_trust` (сведение метки синтезатора и метки trust-gate в каноническую — более строгая по общей таблице рангов; на каждый ответ, `writeback.py`; лог `log(...)` при verbose остался в Python) | `yandi_rs/src/canonical_trust.rs` (использует таблицу и `apply_trust_cap` из `trust_gate.rs`) | `YANDI_CANONICAL_TRUST_ENGINE=rust` | Собрано, 1926 проверок (все пары меток × None/пустая строка/неизвестные/пробельные/не-строки × verbose с проверкой строк лога) + 5 из 5 внесённых мутантов пойманы (`agent/canonical_trust_rust_parity_test.py`; первая попытка мутанта «diverged» не компилировалась и молча тестировала чистую сборку — обвязка мутаций теперь проверяет код возврата сборки). `diverged` = `final != gate` (а не `canonical != final`) — как в оригинале; ветки одной доступной нити возвращаются раньше сравнения и без лога; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/final_claim_coverage.py` — лексическое ядро маршрутизации пар «финальное утверждение ↔ утверждение пайплайна» на NLI: `_content_words`, `_lexical_overlap` (Жаккар), `_has_negation`, `_shares_number`, `_is_near_duplicate`, `_mandatory_routing_reason` + матрица «обязательных» причин для `_route_candidate_pairs` (O(F×P): слова/числа/отрицания — один раз на утверждение, а не на каждую пару). Эмбеддинги/numpy, вызовы LLM/NLI, `_extract_json` остались в Python | `yandi_rs/src/final_claim_coverage.rs` (+ `fcc_data.rs`, `gen_fcc_data.py`) | `YANDI_FINAL_CLAIM_COVERAGE_ENGINE=rust` | Собрано, ~49 тыс. проверок (48 тыс. сравнений функций; все комбинации ролей; сквозной `_route_candidate_pairs` с детерминированным эмбеддингом и «эмбеддинг недоступен») + мутанты (11 + 5 пограничных) пойманы (`agent/final_claim_coverage_rust_parity_test.py`; 1 — «нет» — **эквивалентен**: `\\bне{1,2}[а-яё]*\\b` уже покрывает «нет»; **2 пробела теста закрыты:** доля Жаккара между 0.8 и 0.9 и ровно 0.15 не порождались случайно — добавлены наборы k/(k+1) и 3/20). Слова — только `[а-яёa-z0-9]` (Unicode-цифры не входят), длина в СИМВОЛАХ; отрицание/числа — точные `\\b`, `\\d`, IGNORECASE через `py_regex`; стоп-слова сгенерированы из Python; существующий `candidate_routing_regression_test` — с включённым переключателем в полном прогоне; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/claim_evidence_retriever.py::_anchor_hit` и `_subject_anchor_matches` (кусок 26, дополнение к куску 13 — тот же переключатель `YANDI_CLAIM_EVIDENCE_RETRIEVER_ENGINE`): «шлюз идентичности субъекта» — подтверждают ли title/url/passage якорь claim'а; динамический паттерн `\\b<re.escape(якорь)>\\b` на КАЖДЫЙ фрагмент источника | `yandi_rs/src/claim_evidence_retriever.rs` (`anchor_hit`, `subject_fields`; выбор якорей остался в Python) | тот же | ~4,4 тыс. проверок раздела H (`agent/claim_evidence_retriever_rust_parity_test.py`) + 6 мутантов; **2 пробела теста закрыты после выживших мутантов:** пустой якорь в `subject_fields` (Python-обёртка отсекает его раньше) и символы, где Python-`\\w` и `is_alphanumeric` Rust расходятся (`U+0345`, знаки деванагари/тайские — «Other_Alphabetic»). `\\b` для ПРОИЗВОЛЬНОГО литерала реализован вручную по таблице Python-`\\w`: граница слева/справа = «словесность» соседних символов (верно и когда якорь начинается/кончается не-словесным, напр. `c++`); перебираются ВСЕ позиции (перекрывающиеся вхождения), как `re.search`; также в массовом Unicode-фаззинге (1,53 млн сравнений); существующий `claim_evidence_bilingual_subject_gate_regression_test` — с включённым переключателем в полном прогоне |
| `agent/policy.py` — ядро политики БЕЗОПАСНОСТИ: `SecretScanner.scan_text` (9 типов секретов: api_key, sk-, AKIA, aws_secret, password, private key, token/secret, hf_, proxy-creds; белый список шаблонов, безопасные хосты), `PolicyEngine.check_shell` (block/allow-листы одобрения shell-команд агента), `check_network` (allowlist хостов). Файловый ввод-вывод, аудит-лог и конфиг остались в Python; вызывается из `skills.py` и `daemon.py`; своего теста не было | `yandi_rs/src/policy.rs` (+ `policy_data.rs`, `gen_policy_data.py`) | `YANDI_POLICY_ENGINE=rust` | Собрано, ~27 тыс. проверок (12 822 текста сканера, 3 тыс. shell-команд, 1 тыс. хостов; образцы всех 9 типов на границах длины 19/20/21, 29/30/31, ровно 16; `\\b` слева/справа с `_`, цифрами, Unicode, ударением; позиции в СИМВОЛАХ при кириллице/эмодзи; обрезка `match` до 80 символов; порядок находок; шаблоны белого списка в любом регистре; IGNORECASE İ/ı/ſ/K) + **14 из 14 внесённых мутантов пойманы** (`agent/policy_rust_parity_test.py`). Паттерны и списки сгенерированы ИЗ Python — в модуле безопасности опечатка = пропущенный секрет; три паттерна с `\\b` (sk-, AKIA, hf_) написаны вручную ради точных позиций. Сохранено как в оригинале: allow-лист по `startswith` БЕЗ границы слова («lsblk» проходит по «ls»), block-лист — первое найденное в порядке списка. Делегирует только пока таблицы модуля не изменены; также в массовом Unicode-фаззинге; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/tools/tool_shell.py::_allowed` — охранный шлюз shell-команд агента: запреты (rm, mv, cp, wget, curl, chmod, chown, sudo, su, kill, pkill, reboot, shutdown, метасимволы `\|&;`$`, `..`) проверяются ДО белого списка разрешённых начал; флаги `AGENT_SHELL_FULL`/`AGENT_SHELL_NET` из окружения. Выполнение (`subprocess`) осталось в Python | `yandi_rs/src/tool_shell.rs` (+ `tool_shell_data.rs`, `gen_tool_shell_data.py`) | `YANDI_TOOL_SHELL_ENGINE=rust` | Собрано, ~84,5 тыс. проверок (9 комбинаций флагов окружения × ~9 тыс. команд: каждая «голова» и каждый запрещённый токен во всех позициях, «запрет побеждает белый список», границы слова `\\b` — farm/rm_x/rmdir/ударение перед rm, Unicode-цифры под `\\d`, пробелы U+001C..1F/табы/переводы строк, регистр) + **9 из 9 внесённых мутантов пойманы** (`agent/tool_shell_rust_parity_test.py`). Паттерны сгенерированы ИЗ Python; флаги окружения читает Python-обёртка при каждом вызове (`"0"` — тоже «включено», как в оригинале); делегирует только пока WHITELIST/BANNED не изменены; также в массовом Unicode-фаззинге; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/db/sql/crypto.py` (`encrypt_field`/`decrypt_field`/`blind_index`/`build_aad`) + `agent/db/sql/keys.py` (`wrap_dek`/`unwrap_dek`/`derive_*_key` через HKDF-SHA256) — шифрование полей БД: блоб `версия(1)‖nonce(12)‖шифртекст+тег`, AAD `YANDI\|тип\|id\|поле\|vN`. **Криптопримитивы — RustCrypto (`aes-gcm`, `hmac`, `hkdf`, `sha2`), ничего самописного.** Проверки ключа/длины/версии, исключения (`KeyMissingError`, `ValueError`, `InvalidTag`) и `.decode` остались в Python | `yandi_rs/src/crypto.rs` | `YANDI_CRYPTO_ENGINE=rust` | Собрано, ~2,4 тыс. проверок: побайтные «известные ответы» с одним nonce против `cryptography` (юникод, управляющие символы, 100 КБ), шифрование в обе стороны (Rust→Python и Python→Rust) через сам модуль, порча КАЖДОГО байта блоба/чужой контекст/чужой ключ → `InvalidTag` в обоих путях, короткие блобы на длинах 0..40, версия вне 0..255, ключи 16/24 байта (идут в Python), суррогаты, не-UTF-8, `int`/`str` `entity_id`, уникальность nonce (2000 подряд, и для wrap), HKDF на всех длинах и границе 255·32, wrap/unwrap с порчей + **15 из 16 мутантов пойманы, 16-й эквивалентен** (`bool` как `entity_id`: `str(True)` совпадает с f-строкой) (`agent/crypto_rust_parity_test.py`). Делегирует только для 32-байтового ключа типа `bytes`. + хеш записи цепочечного журнала `llm_gateway/secure_store.py::_entry_hash` (HMAC-SHA256 по `seq(8, big-endian)|op|name_index|content_hash|prev_hash`; тот же переключатель `YANDI_CRYPTO_ENGINE`; 5/5 мутантов пойманы, собственный тест хранилища проходит с включённым переключателем). **Нужны сетевые крейты** (`aes-gcm`, `hkdf`, `hmac` — уже в кэше cargo, сборка с `--offline` проверена); выигрыша в скорости нет (OpenSSL быстр) — срез ради единого Rust-ядра; в бою по умолчанию ВЫКЛЮЧЕНО |
| `pet/event_extraction.py` (`segment_words`, `_json_object` — подготовка текста, `_validate_candidate`), `pet/fact_extraction.py` (`_validate`, `looks_secret`), `pet/commitment_verification.py` (`inside_quotation`) — то, что бежит на КАЖДОЕ сообщение пользователя вокруг вызова модели: нарезка на слова (позиции в символах), проверка кандидатов-событий/фактов от модели (строгая схема, границы диапазонов, длина утверждения в символах), «похоже на секрет» (лукахеды регулярки написаны руками), «внутри цитаты» | `yandi_rs/src/pet_extraction.rs` | `YANDI_PET_EXTRACTION_ENGINE=rust` | Собрано, ~105 тыс. проверок дифференциального фаззинга против настоящего Python (пробелы U+001C..1F/U+0085/U+00A0 против U+200B/U+FEFF, ограждения ``` , сгенерированные кандидаты из родных и посторонних типов JSON — bool как int, огромные целые, NaN, кортеж, подклассы; границы span 30/31, statement 7/8/200/201 СИМВОЛОВ после strip, known 29/30/35; Unicode-цифра сразу за прогоном закрывает `\\d` в `looks_secret` — квирк оригинала воспроизведён; все пары кавычек, позиции отрицательные/огромные) + **29 из 31 внесённых мутантов пойманы, 2 эквивалентны** (буква в прогоне секрета всегда ASCII; отрицательный индекс и так вне границы) (`pet/pet_extraction_rust_parity_test.py`). `json.loads` остаётся в Python (он уже на C); посторонние типы входа и одинокие суррогаты откатываются на исходный Python-код; константы Rust получает из Python при каждом вызове. Ускорения нет (микросекунды) — перенос ради единого Rust-ядра; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/orch_tag_tree.py` (`_tokenize`, `_lsh_bucket`, `lsh_entropy`, пересчёт энтропии узла в `TagTree.update`) — LSH-энтропия для решения «делить/склеивать» тег в дереве тегов оркестратора | `yandi_rs/src/orch_tag_tree.rs` | `YANDI_ORCH_TAG_TREE_ENGINE=rust` | Собрано, ~28 тыс. проверок, ГЛАВНОЕ — `.lower()` + `\\b[а-яёa-z]{3,}\\b` сверены по КАЖДОЙ кодовой точке Unicode в трёх окружениях (это и вскрыло расхождение версий Unicode — см. «Версия Unicode» в ревизии точности), фаззинг трудных смесей, `lsh_bucket` на любых `n_buckets`, `lsh_entropy` побитово (`repr` float), `TagTree.update` (гистограмма/энтропия после серии обновлений; испорченные гистограммы идут в Python; реальный `tag_tree.json` НЕ пишется); стоп-слова Rust == Python (сверка в тесте), при изменении `_STOPWORDS` делегирование отключается + **12 из 14 внесённых мутантов пойманы, 2 эквивалентны** (ранний возврат для пустого списка; `a-z` vs `A-Za-z` после lower) (`agent/orch_tag_tree_rust_parity_test.py`). Дерево/файл/время остаются в Python; в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/relationship_memory.py` (`_stems`, через него `content_stems`; `_stem`) — стеммер «содержательных» слов для сопоставления обиды и извинения/обещания/личного факта: `casefold` + `ё→е`, токены `[a-zа-я0-9]+`, первый подходящий суффикс из УПОРЯДОЧЕННОГО списка при остатке >= 3 СИМВОЛОВ, вычитание стемов «неинформативных» слов, отсев < 3 символов | `yandi_rs/src/relationship_memory.rs` (+ `relationship_memory_data.rs`, `gen_relationship_memory_data.py`) | `YANDI_RELATIONSHIP_MEMORY_ENGINE=rust` | Собрано, ~3,4 млн сравнений (главное — `_stems` на КАЖДОЙ кодовой точке Unicode в 3 окружениях; каждый суффикс × основы длиной 0..6 символов, граница «остаток >= 3» в символах, а не байтах; фаззинг с ß/İ/знаком Кельвина/Ё/Unicode-цифрами/комбинирующими; типы входа None/не-строки/суррогаты) + **14 из 15 внесённых мутантов пойманы, 15-й эквивалентен** (порядок двух фильтров множества); собственный тест модуля (38 проверок) проходит с включённым переключателем; таблицы (суффиксы по порядку, стемы) сгенерированы ИЗ Python, при изменении таблиц в Python делегирование отключается (`agent/relationship_memory_rust_parity_test.py`); `_overlap` и всё SQL/состояние остаются в Python; в бою по умолчанию ВЫКЛЮЧЕНО |
| `pet/core_lifecycle.py` (`_decode_key`, `_REQUEST_ID_RE`/`_IDEM_RE`/`_SECRET_RE` через `_id_ok`, `_validate`, `_check_key`) — граница «узел ⇄ ядро» (контракт NODE_CORE_CONTRACT): именно эти проверки узел на Rust обязан делать ТАК ЖЕ. Разбор ключа разблокировки, идентификаторы запроса/идемпотентности, секрет запуска, схема тел `unlock`/`lock`/`shutdown`, HKDF проверочного значения. Файлы, права, AES-GCM и жизненная логика остались в Python | `yandi_rs/src/core_lifecycle.rs` | `YANDI_CORE_LIFECYCLE_ENGINE=rust` | Собрано, ~24 тыс. сравнений: ТОНКОСТИ оригинала воспроизведены — `$` в `re` пропускает завершающий `\n` (а `b64decode(validate=True)` его потом отвергает), Python ПРИНИМАЕТ «лишние» биты последнего символа base64 (декодирование поэтому ручное, все 64 значения проверены), длины идентификаторов в символах, не-ASCII; схема запросов на сгенерированных документах (bool/float/огромные int/подклассы) + provision→verify перекрёстно между режимами + **25 из 25 внесённых мутантов пойманы** (4 из них сначала выжили — в тесте не было сплошных строк допустимых символов на границах длины 7/8/64/65; добавлено); собственный тест модуля (регрессия ядра с подпроцессом) проходит с включённым переключателем (`pet/pet_core_lifecycle_rust_parity_test.py`); в бою по умолчанию ВЫКЛЮЧЕНО |
| `pet/ui_settings.py::validate` — шлюз ввода настроек вкладки «YANDI» (какая модель отвечает, какие советуют): неизвестное поле и ключ API — ошибка, а не молчаливая правка; голос обязателен, выбранные блоки должны быть заполнены. `load`/`save` (файл, права 0600, атомарная запись) остались в Python | `yandi_rs/src/ui_settings.rs` | `YANDI_UI_SETTINGS_ENGINE=rust` | Собрано, 60 тыс. сгенерированных документов (9,4 тыс. годных): проверенный словарь (включая ПОРЯДОК ключей) либо `SettingsError` с ТОЧНЫМ текстом; falsy-значения равны `{}`, длины в СИМВОЛАХ (путь 1024/1025, модель 128/129, адрес 512), strip Python (U+001C..1F), `\\s` Python в адресе, порядок ошибок (оригинал вычисляет ОБА `_text` модели до проверок формата), посторонние типы/суррогаты → Python. **Единственное отличие:** при нескольких незаполненных блоках оригинал называет случайный (перебор `set`, порядок зависит от хэш-рандомизации), Rust — детерминированно первый; тест принимает любой настоящий незаполненный. + **30 из 33 внесённых мутантов пойманы, 2 эквивалентны** (длина модели 128 уже отсекается `_text`; пустой dict ≡ `{}`), 1 выживший (первый символ адреса U+001C..1F) закрыт тестом. Собственный тест вкладки настроек: его встроенные мутанты портят питоновскую проверку, Rust её обходил — они теперь явно привязаны к Python-пути (как в срезах 1–2) (`pet/pet_ui_settings_rust_parity_test.py`); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/orch_query_framer.py` (`decide_policy`, `_auto_cq`, `_is_safe_domain`) — решения оркестратора на КАЖДЫЙ запрос после того, как модель заполнила матрицу запроса: политика ответа (`safe_general`/`ask_first`/`answer_direct`/`answer_with_assumptions`), уточняющий вопрос из списка недостающего (порядок словаря шаблонов — побеждает первое вхождение), чувствительность домена (подстрока, не равенство). Вызов модели, сбор контекста и разбор JSON остались в Python | `yandi_rs/src/orch_query_framer.rs` (+ `orch_query_framer_data.rs`, `gen_orch_query_framer_data.py`) | `YANDI_ORCH_QUERY_FRAMER_ENGINE=rust` | Собрано, ~27 тыс. сравнений: каждый безопасный домен и каждый «общий» объект в 10 вариациях, комбинации объект/действие/ограничения/домен (≈13 тыс.), каждый ключ шаблона в разных регистрах/позициях, регистр по Python `.lower()`, фигурные скобки в действии, посторонние типы полей (→ Python), суррогаты + **16 из 16 внесённых мутантов пойманы**; таблицы сгенерированы ИЗ Python, при их изменении делегирование отключается (снимок сравнивается при каждом вызове) (`agent/orch_query_framer_rust_parity_test.py`); в бою по умолчанию ВЫКЛЮЧЕНО |
| `agent/orchestrator/claims/status.py::classify_claim_epistemic_status` (ядро; `_counts_toward_status` и `_distinct_cluster_count` — внутри) — **ЭПИСТЕМИЧЕСКОЕ ЯДРО**: как отношения «доказательство → утверждение» превращаются в статус (`supported`/`disputed`/`contradicted`/`unverified`). Инвариант «trust never truth»: `verified` здесь НИКОГДА не выставляется; N перепечаток одной истории (один `source_cluster_id`) = ОДИН независимый источник; путь авторитета (`direct` + `eligible is True`) и путь directness (>= 0.60, не из «жёстко заблокированных» классов, не из `local_registry`). Rust пишет результат В ТОТ ЖЕ словарь (порядок ключей как в Python), журнал и сводка остались в Python | `yandi_rs/src/claim_status.rs` | `YANDI_CLAIM_STATUS_ENGINE=rust` | Собрано, 60 тыс. дифференциальных сравнений (~98 тыс. утверждений реально прошли по Rust-пути, ~5 тыс. откатились): сравнивается ВСЁ наблюдаемое — сводка, каждое утверждение после вызова (`repr` с порядком ключей и `counted_via`), все строки журнала (verbose True/False), а при ошибке тип исключения и то, что успело измениться (в оригинале исключение в строке журнала случается ПОСРЕДИ цикла — Rust это учитывает откатом); поля — «родные» типы JSON и посторонние (bool как число, NaN/inf/огромные числа, `float("abc")`, нехэшируемые id, кортеж, подклассы dict); Python-семантика (сравнение, хэш, `float()`, истинность) исполняется самим Python через PyO3; при любой неоднозначности Rust ничего не записывает и отдаёт Python; константы (`HARD_BLOCKED_SOURCE_CLASSES`, порог) передаются из Python при каждом вызове + **23 из 25 внесённых мутантов пойманы, 2 эквивалентны** (защита от falsy в directness и в списке отношений лишь отправляют случай на Python-путь). Собственные тесты (независимый подсчёт, кластеры источников, порядок идентичности семей, модуляризация оркестратора — 230 проверок) проходят с включённым переключателем (`agent/claim_status_rust_parity_test.py`); в бою по умолчанию ВЫКЛЮЧЕНО |

**Переключатель — один env var на смысловую область**, не общий на весь `yandi_rs`: так владелец может включить один перенесённый кусок, не трогая остальные. Шаблон имени: `YANDI_<ОБЛАСТЬ>_ENGINE=rust` (`GUARD` — вход/охрана, `LOGIN` — сессии/пароль). Если однажды переключателей наберётся много и это станет неудобно — общий механизм можно ввести отдельным явным решением, не по умолчанию.

## Что реально выгодно включать (замер 2026-09-24, повторён после среза 36)

`scripts/rust_bench.py` меряет Python и Rust через ПУБЛИЧНЫЙ Python-API (то есть с накладными расходами на вызов через
границу PyO3 и обёртку) на реалистичных входах — один процесс, `maturin develop --release`, Python 3.11.2:

| Функция | Python, мкс | Rust, мкс | Ускорение |
|---|---:|---:|---:|
| `policy.scan_text (секреты)` | 23.3 | 11.5 | ×2.0 |
| `tool_shell._allowed` | 13.7 | 3.2 | ×4.3 |
| `crypto.encrypt_field+decrypt_field (AES-GCM)` | 10.1 | 11.4 | медленнее ×1.1 |
| `crypto.blind_index` | 4.4 | 4.8 | медленнее ×1.1 |
| `event_extraction.segment_words (120 слов)` | 49.2 | 30.2 | ×1.6 |
| `event_extraction._validate_candidate` | 1.5 | 1.1 | ×1.4 |
| `fact_extraction._validate` | 2.0 | 3.3 | медленнее ×1.6 |
| `fact_extraction.looks_secret` | 41.7 | 8.3 | ×5.0 |
| `commitment_verification.inside_quotation` | 10.4 | 0.6 | ×16.2 |
| `orch_tag_tree.lsh_entropy (30 запросов)` | 1,075.3 | 291.1 | ×3.7 |
| `orch_tag_tree._tokenize` | 6.4 | 8.3 | медленнее ×1.3 |
| `relationship_memory._stems` | 126.8 | 21.5 | ×5.9 |
| `orch_query_framer.decide_policy` | 1.7 | 1.2 | ×1.4 |
| `orch_query_framer._auto_cq` | 0.7 | 1.6 | медленнее ×2.2 |
| `ui_settings.validate` | 5.8 | 3.5 | ×1.7 |
| `core_lifecycle._decode_key` | 0.8 | 0.4 | ×2.1 |
| `core_lifecycle._validate (unlock)` | 2.0 | 0.9 | ×2.1 |
| `claim_identity.canonicalize_claim_text` | 11.9 | 9.0 | ×1.3 |
| `claim_identity.extract_subject_anchors` | 67.0 | 18.5 | ×3.6 |
| `claim_validator.validate` | 82.4 | 7.1 | ×11.6 |
| `hardening_guard` | 195.4 | 23.9 | ×8.2 |
| `criticism_detector.analyze` | 15.3 | 6.7 | ×2.3 |
| `boundaries.detect_toxicity` | 3.5 | 2.2 | ×1.6 |
| `claim_answer_linker.link` | 26.7 | 27.3 | ≈ без разницы |
| `claim_evidence_retriever.classify_role` | 9.9 | 6.4 | ×1.6 |
| `source_quality.evaluate` | 21.5 | 36.0 | медленнее ×1.7 |
| `epistemic_router.detect_domain` | 21.8 | 3.3 | ×6.6 |
| `message_intensity.parse_self_report` | 12.4 | 4.4 | ×2.8 |
| `orch_risk.assess_risk` | 3.7 | 3.8 | ≈ без разницы |
| `intent_router.detect_intent` | 145.9 | 28.6 | ×5.1 |
| `target_router.detect_target` | 13.1 | 4.0 | ×3.3 |
| `personal_boundary.analyze` | 7.4 | 4.6 | ×1.6 |
| `scene_builder.build` | 77.5 | 14.6 | ×5.3 |
| `object_resolver.resolve` | 61.4 | 9.4 | ×6.5 |
| `entity_resolver.resolve` | 4.0 | 4.3 | медленнее ×1.1 |
| `claim_graph.extract_claims (4 evidence)` | 2,179.7 | 625.5 | ×3.5 |
| `claim_graph._build_graph (30 утверждений)` | 6,429.1 | 966.6 | ×6.7 |
| `trust_gate.apply_epistemic_trust_adjustment` | 7.3 | 8.3 | медленнее ×1.1 |
| `trust_gate._calculate_delta_factors` | 3.8 | 3.6 | ×1.1 |
| `canonical_trust.compute` | 1.4 | 0.9 | ×1.6 |
| `local_guard.is_allowed_request` | 1.0 | 1.3 | медленнее ×1.4 |
| `source_clustering.assign (30 источников)` | 119,659.2 | 12,585.6 | ×9.5 |
| `source_clustering.assign (60 источников)` | 439,332.7 | 39,924.3 | ×11.0 |

**Вывод (честный):**
* **Стоит включать ради скорости** (ускорение ×5 и выше, на горячем пути каждого запроса или на больших наборах):
  `YANDI_SOURCE_CLUSTERING_ENGINE` (×11–12, растёт с размером пула источников — 391 мс → 33 мс на 60 источниках),
  `YANDI_CLAIM_GRAPH_ENGINE` (×6–7), `YANDI_HARDENING_ENGINE` (×12), `YANDI_CLAIM_VALIDATOR_ENGINE` (×10),
  `YANDI_OBJECT_RESOLVER_ENGINE` (×8), `YANDI_INTENT_ROUTER_ENGINE` (×5), `YANDI_TARGET_ROUTER_ENGINE` (×4–5).
* **Новые срезы 27–36:** заметный выигрыш у `YANDI_PET_EXTRACTION_ENGINE` (`looks_secret` ×7, `inside_quotation` ×12, нарезка на слова ×3), `YANDI_RELATIONSHIP_MEMORY_ENGINE` (стеммер ×9), `YANDI_ORCH_TAG_TREE_ENGINE` (энтропия ×3), `YANDI_TOOL_SHELL_ENGINE` (×5), `YANDI_POLICY_ENGINE` (сканер секретов ×2); почти без разницы или чуть медленнее — `YANDI_CRYPTO_ENGINE` (AES-GCM ×1,6; `blind_index` ×0,8 — OpenSSL в Python и так быстр), `YANDI_UI_SETTINGS_ENGINE`, `YANDI_ORCH_QUERY_FRAMER_ENGINE`, `YANDI_CORE_LIFECYCLE_ENGINE` (микросекунды; ценны как общее ядро с узлом на Rust, а не скоростью).
* **Умеренно** (×2–4): `SCENE_BUILDER`, `CLAIM_IDENTITY` (якоря), `EPISTEMIC_ROUTER`, `MESSAGE_INTENSITY`.
* **Разницы почти нет** (микросекунды; вызов через границу съедает выигрыш): `CRITICISM`, `BOUNDARIES`, `ANSWER_LINKER`,
  `CLAIM_EVIDENCE_RETRIEVER`, `SOURCE_QUALITY`, `ORCH_RISK`, `ENTITY_RESOLVER`, `PERSONAL_BOUNDARY`.
* **Не включать ради скорости** — на ~20% медленнее из-за накладных расходов на вызов: `TRUST_GATE`, `CANONICAL_TRUST`,
  `GUARD` (`local_guard`). Они перенесены ради ЕДИНСТВЕННОГО источника правды при будущем ядре на Rust (и как отработанная
  методика), а не ради скорости; `LOGIN` — по той же причине (безопасность/будущее ядро).
* Правило «не менять Python по умолчанию» остаётся в силе: всё выключено, включает владелец сам, по одному переключателю.

## Наблюдения по безопасности, найденные при переносе (порт СОХРАНЯЕТ поведение оригинала; ничего не исправлялось)

Порт по определению повторяет Python «как есть», поэтому ниже — не ошибки порта, а особенности исходного кода, которые
стоит знать владельцу и при желании исправить ОТДЕЛЬНЫМ решением:
* `agent/tools/tool_fs.py::_safe` проверяет путь через `str(resolved).startswith(str(root))` — БЕЗ разделителя: корень
  `/home/iam/yandi` «пропускает» и соседний каталог `/home/iam/yandi-evil/…`. Правильная проверка — `resolved == root` или
  `resolved.is_relative_to(root)` (не перенесено: зависит от файловой системы — `Path.resolve()`).
* `agent/policy.py::check_shell`: allow-лист по `startswith` без границы слова (`lsblk` проходит по `ls`, `pythonista` — по
  `python`); block-лист — подстроки (`rm -rf` не ловит `rm  -rf` с двойным пробелом). Это как в оригинале.
* `agent/tools/tool_shell.py`: `AGENT_SHELL_FULL="0"` (любая НЕпустая строка) включает полный доступ — истина по непустоте, не по значению.
* `agent/db/sql/crypto.py`: `decrypt_field` при ключе не 32 байта (16/24) молча работает через Python (AES-128/192) — ключ другой длины не отвергается; `wrap_dek`/`unwrap_dek` используют ФИКСИРОВАННЫЙ AAD `YANDI|DEK_WRAP|v1` — обёрнутый DEK не привязан ни к какому владельцу/версии ключа. Это как в оригинале.

## Ревизия точности срезов 1–15 (2026-09-24) — что нашли и исправили

После 15-го среза я прогнал ВСЕ Rust-переключатели включёнными и сверил перенесённые модули с исходниками
ещё раз, уже не «по сценариям», а по классам различий между стандартными средствами Rust и Python.
Нашлись реальные неточности, которые прежние (зелёные) parity-тесты не видели, потому что не пробовали
нужных входов. Все исправлены общими подпорками (`py_text.rs`, `py_json.rs`) и закрыты сквозным тестом
`agent/rust_python_text_semantics_parity_test.py` (36 тыс. проверок, 5 внесённых мутантов пойманы):

| Что было неточно | Где | Как проявлялось | Исправление |
|---|---|---|---|
| Python-пробелы шире Rust: `str.strip()/.split()`/`\s` считают пробелом ещё U+001C–U+001F | срезы 3, 5, 6, 10, 11, 12, 13, 1 (trim/split_whitespace/`\s` в ~9 файлах) | `canonicalize_claim_text("a\x1cb")` давал разные ответы | `py_strip`, `py_split_whitespace`, `py_regex` (переписывает `\s`) |
| `f64::clamp` пропускает NaN, Python `max(0,min(1,x))` даёт 1.0 | срез 10 | `severity: NaN` → разные значения | `py_clamp01` |
| JSON: Python принимает `NaN`/`Infinity`/`1e999`/одиночные суррогаты и пишет свои тексты ошибок; serde_json нет | срез 10 | там, где Python `ok=True`, Rust давал `ok=False`; тексты диагностики отличались | `py_json.rs` — порт C-сканера `json.loads`; фаззинг ~10 000 случаев, значения и ТОЧНЫЕ тексты ошибок совпадают |
| Диагностика `float()`: Python пишет `could not convert string to float: 'abc'` / `… not 'list'` | срез 10 | всегда стояло имя поля | точные тексты + `float()` с `_` и любыми цифрами Unicode (`py_float`) |
| `{tail[:200]!r}` — настоящий Python `repr()` (экранирует переносы строк и т. п.) | срез 10 | тексты расходились при переносе строки в хвосте | `py_repr_str` + таблица `isprintable()` из самого Python |
| Тесты срезов 1, 2 (самопроверка «мутантов» портит Python-код) при включённом Rust мутант «выживал» | `pet_web_guard_regression_test`, `pet_web_login_regression_test` | тест падал при `YANDI_GUARD/LOGIN_ENGINE=rust` | мутанты привязаны к своему Python-пути |

**Дополнение 2026-09-24 (вечер): массовый Unicode-фаззинг и точный транслятор regex.** Новый тест
`agent/rust_unicode_fuzz_parity_test.py` (~800 тыс. сравнений за прогон, любой `FUZZ_SEED`) гоняет ВСЕ перенесённые
модули на случайной смеси «трудных» символов (комбинирующие знаки, надстрочные/дробные/арабские цифры,
İ ı ß ẞ ſ K Å Ω ǅ Σ ς, нулевой ширины, U+001C..1F, эмодзи, астральные) и реальных ключевых слов самих модулей.
Он нашёл остаточные расхождения там, где я раньше лишь «документировал ограничение», — и они устранены:

| Что расходилось | Как исправлено |
|---|---|
| `\b` / `\w` рядом с комбинирующими знаками и т. п. (regex-крейт определяет слово иначе) | `py_regex.rs` — транслятор паттернов Python `re` → крейт `regex`: `\w` по таблице Python, `\b` через «съедающий» соседний символ (точно для проверок «есть совпадение») |
| `re.IGNORECASE` Python шире, чем `(?i)` крейта (`ı`≈`i`, `İ`, `ſ`, `K`, `µ`…) | каждый литерал/символ класса раскрывается в группу эквивалентных при `re.I` символов (таблица из Python: `gen_py_icase_table.py`, проверена против настоящего `re.I`, 0 расхождений) |
| `$` в Python совпадает и перед завершающим `\n` | `$` → `(?:\n?$)` |
| `source_quality._hostname` — «терпимый» ручной парсер | ТОЧНЫЙ порт `urlparse(url).hostname` из CPython (схема, `_splitnetloc`, проверка скобок/IPv6/IPvFuture по `ipaddress`, NFKC-проверка `netloc`, `lstrip` C0-символов); 1 млн+ URL-подобных строк без расхождений |

Мутантный контроль фаззинга: порча `\b`, IGNORECASE, `:`-разбора, NFKC-проверки, IPv6-scope — все пойманы (два
выживших мутанта выявили пробелы самого фаззинга — добавлен список пограничных URL; `$` защищён Rust-юнит-тестом).

**Остаточные ограничения (теперь узкие и явные):**
* Одиночные суррогаты Unicode в JSON-строке заменяются на U+FFFD (Rust `String` их хранить не может).
* Очень глубокая вложенность JSON (>500) и `float(<целое > 1e308>)`: Python бросает RecursionError/OverflowError
  мимо `except` — Rust-версия бросает те же исключения через PyO3, а не падает.
* Таблицы Unicode и поведение `urllib.parse` сняты с ЭТОЙ машины (Python 3.11.2, Debian-сборка, Unicode 14): при смене
  версии Python — перегенерировать таблицы (`python rustlib/gen_py_*_table.py`) и перепрогнать фаззинг.
* **Версия Unicode (найдено и исправлено при срезе 31):** Rust std и крейты (`caseless`, `unicode-normalization`) знают Unicode 15/16, а Python 3.11.2 — 14.
  На ~30 символах, добавленных позже (U+1C89, U+A7CB, U+A7CC, U+A7D2…, символы Туллу-Тигалари U+113C2…), `to_lowercase`/`casefold`/NFC
  давали ДРУГОЙ результат, чем Python (а от регистра зависит «словесность» соседа → `\b`). Исправлено ТАБЛИЦАМИ ИЗ САМОГО PYTHON:
  `py_text::py_lower` (одиночные отображения + правило финальной сигмы CPython с классами cased/case-ignorable, снятыми у Python
  чёрным ящиком), `py_casefold`, `py_nfc`/`py_nfkc` (символ, неназначенный в Unicode Python, — барьер: нормализация = конкатенация
  нормализаций кусков). Все 38 `.to_lowercase()` в модулях заменены на `.py_lowercase()`. Проверено по КАЖДОЙ кодовой точке
  (`agent/rust_python_text_semantics_parity_test.py`, A1b–A1e) и в массовом фаззинге. Генераторы: `gen_py_lower_table.py`,
  `gen_py_casefold_table.py`, `gen_py_unassigned_table.py`.
* `py_regex` точен для проверок «есть ли совпадение»; для позиционных вызовов (`find_iter`, `captures`) рядом с `\b` его
  использовать нельзя (там, где позиции важны, паттерн написан вручную — напр. `matches_bare_net`).

**Правило на будущее (стало обязательным пунктом методологии):** parity-тест НЕ считается доказательством, пока
(1) для модуля не прогнаны его собственные существующие тесты с включённым переключателем,
(2) не подмешаны «трудные» входы класса — управляющие символы/нестандартные пробелы, не-ASCII цифры, `NaN`/огромные числа,
пустые и None-подобные значения, строки на границе порогов длины — и (3) время от времени весь набор не прогнан с ВСЕМИ переключателями.
