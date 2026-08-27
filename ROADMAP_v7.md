================================================================================
YANDI MASTER ROADMAP
SELF-LEARNING SYSTEM → SELF-IMPROVING LOCAL MODEL
Версия: 2026-08-27
Статус: MASTER ARCHITECTURAL ROADMAP
================================================================================

НАЗНАЧЕНИЕ ДОКУМЕНТА
================================================================================

Этот документ является ДОЛГОСРОЧНОЙ архитектурной картой YANDI.

Это НЕ текущее техническое задание.

Это НЕ приказ реализовать всё последовательно без повторного аудита.

Перед каждым крупным этапом должен создаваться ОТДЕЛЬНЫЙ технический план,
основанный на реальном состоянии кода на момент начала работы.

Если код противоречит roadmap:

    CODE > ROADMAP.

Если измерения опровергают предполагаемую архитектурную необходимость:

    MEASURE > PLAN.

Если синтетические тесты зелёные, но live-run показывает ошибку:

    LIVE > TEST CONFIDENCE.

Roadmap должен обновляться по мере изменения архитектуры.


================================================================================
0. ГЛАВНАЯ ЦЕЛЬ
================================================================================

Перейти от:

    "YANDI умеет искать, проверять и пересматривать знания"

к:

    "YANDI умеет наблюдать собственную работу,
     обнаруживать собственные систематические ошибки,
     извлекать проверяемый опыт,
     экспериментировать со стратегиями,
     менять собственное поведение,
     проверять последствия этих изменений,
     откатывать неудачные изменения,
     а позднее — использовать накопленный опыт
     для контролируемого дообучения локальной модели."


Целевая архитектура:

    QUERY
      ↓
    OBSERVATION
      ↓
    PLANNING
      ↓
    SEARCH / RETRIEVAL
      ↓
    EVIDENCE
      ↓
    CLAIMS
      ↓
    VALIDATION / CONTRADICTION
      ↓
    BELIEFS
      ↓
    SYNTHESIS
      ↓
    CANONICAL TRUST
      ↓
    ANSWER
      ↓
    TRACE / EPISODE
      ↓
    DELAYED OUTCOME
      ↓
    EXPERIENCE
      ↓
    REFLECTION
      ↓
    POLICY HYPOTHESIS
      ↓
    EXPERIMENT
      ↓
    POLICY UPDATE
      ↓
    PLANNER
      ↓
    следующий QUERY


После накопления доказанного опыта:

    VERIFIED EXPERIENCE
           ↓
    TRAINING DATASET
           ↓
    CANDIDATE LOCAL MODEL
           ↓
    SHADOW EVALUATION
           ↓
    BLIND / CONTROLLED COMPARISON
           ↓
    PROMOTION OR REJECTION
           ↓
    новая версия локальной модели


================================================================================
1. ФУНДАМЕНТАЛЬНЫЕ ИНВАРИАНТЫ
================================================================================

1.1. НЕ ДОВЕРЯЙ — ПРОВЕРЯЙ.

1.2. НЕ ДОВЕРЯЙ != ОТВЕРГАЙ.

1.3. TRUST != TRUTH.

1.4. NOT FOUND != FALSE.

1.5. SEARCH ERROR != CONTRADICTION.

1.6. RECHECK FAILURE != CONTRADICTION.

1.7. CONSENSUS != TRUTH.

1.8. SOURCE COUNT != INDEPENDENT EVIDENCE COUNT.

1.9. TEXTUAL SIMILARITY != SEMANTIC IDENTITY.

1.10. SEMANTIC SIMILARITY != EPISTEMIC EQUIVALENCE.

1.11. Новое состояние знания не уничтожает историю старого.

1.12. Новый опыт не становится политикой автоматически.

1.13. Новая политика не считается улучшением,
      пока её эффект не измерен.

1.14. Новая версия модели не считается лучше,
      потому что она была дообучена.

1.15. ЗНАНИЕ и НАВЫК РАССУЖДЕНИЯ — разные сущности.

ЗНАНИЕ должно преимущественно жить в:

    Evidence
    Claims
    Beliefs
    Provenance
    Graph
    History

НАВЫК должен жить в:

    Planner
    Policy
    Retrieval strategy
    Reflection
    Local model behavior

1.16. Нельзя превращать веса LLM в скрытую базу фактов,
      если эти факты могут храниться как проверяемые Claims/Beliefs.

1.17. Любая самоизменяющаяся подсистема должна иметь:

    baseline
    experiment
    measurement
    rollback
    history


================================================================================
2. ИЗВЕСТНАЯ СТАБИЛЬНАЯ ТОЧКА
================================================================================

На момент создания roadmap:

    origin/main:
    517eb1a9de954c00f7b02d19a6fd37023061b3c9

    working tree:
    clean

К этому моменту реализованы или существенно развиты:

    - modularized orchestrator;
    - claim↔evidence provenance;
    - evidence relation persistence;
    - search outcome persistence;
    - claim occurrence identity;
    - content_hash identity;
    - semantic claim families;
    - source independence clustering;
    - independent support/contradiction counting;
    - claim graph shadow processing;
    - persistent family dependency graph;
    - cross-request dependency detection;
    - bounded re-evaluation;
    - belief history;
    - canonical Trust;
    - response/trace Trust consistency.

Эта точка является BASELINE,
но НЕ гарантирует, что все старые roadmap-задачи действительно реализованы.

Перед использованием старых требований необходим reconciliation audit.


################################################################################
################################################################################
#
#                    БОЛЬШОЙ ЭТАП I
#
#              YANDI УЧИТСЯ НА СОБСТВЕННОМ ОПЫТЕ
#
################################################################################
################################################################################


================================================================================
PHASE I-0. ROADMAP + ARCHITECTURE RECONCILIATION
================================================================================

ЦЕЛЬ:

Не начинать self-learning поверх неверного представления о текущем коде.

Необходимо сопоставить:

    старые roadmap
    текущий код
    Phases 0–14
    regression suites
    live behavior
    persistent schemas

Для каждого старого требования присвоить:

    IMPLEMENTED
    IMPLEMENTED_DIFFERENTLY
    PARTIALLY_IMPLEMENTED
    SUPERSEDED
    OBSOLETE
    STILL_MISSING
    NEEDS_REEVALUATION

Особенно проверить:

    Validation → Reflection
    Reflection → Planner
    Planner → Trust
    Trust → Planner
    Dataset Builder
    Experience Memory
    Strategy statistics
    Source reputation
    Adaptive Planner
    Reflection usefulness

РЕЗУЛЬТАТ:

Одна актуальная карта реальной архитектуры.

НЕ писать production code в этой фазе.


================================================================================
PHASE I-1. EXPERIENCE RECORD 2.0
================================================================================

ЦЕЛЬ:

Превратить trace/episode из истории выполнения
в структурированный объект опыта.

Trace отвечает:

    "Что произошло?"

Experience должен отвечать:

    "Что из произошедшего может быть полезно в будущем?"


Минимальная концептуальная структура:

    ExperienceRecord {
        experience_id

        episode_id
        timestamp

        query_features
        intent
        domain
        epistemic_class
        risk

        strategy_used

        claims
        evidence_summary
        contradiction_summary

        validation_outcome
        trust_outcome

        immediate_result

        later_outcome

        detected_failure_modes
        detected_success_factors

        lessons

        confidence_in_lesson

        provenance

        status
    }


ВАЖНО:

Не копировать весь trace второй раз.

ExperienceRecord должен ссылаться на trace/episode,
а не дублировать гигабайты данных.


================================================================================
PHASE I-2. OUTCOME MODEL
================================================================================

ЦЕЛЬ:

Отделить:

    "что YANDI думала сразу"

от:

    "что выяснилось позже".


Необходимы как минимум:

    immediate_outcome
    delayed_outcome


Пример:

    T0:
        claim X = SUPPORTED
        trust = 0.76

    T+30:
        новое evidence
        claim X = DISPUTED

Это должно породить событие:

    OUTCOME_REVISION


Типы outcome:

    CONFIRMED
    REVISED
    CONTRADICTED
    REMAINS_UNCERTAIN
    INCONCLUSIVE
    OBSOLETE
    UNKNOWN


UNKNOWN не является failure.


================================================================================
PHASE I-3. DELAYED SUPERVISION
================================================================================

ЦЕЛЬ:

Использовать будущие изменения знания
как сигнал качества прошлых решений.


Цепочка:

    новый Evidence
         ↓
    Claim family изменилась
         ↓
    Belief обновился
         ↓
    Dependency recheck
         ↓
    найти Episodes,
    где этот Claim влиял на решение
         ↓
    сравнить:
        old decision
        new state
         ↓
    learning signal


Система должна уметь спросить:

    Почему я тогда решила именно так?

И получить структурированный ответ:

    retrieval coverage было низким
    source independence была переоценена
    contradiction search не нашёл evidence
    semantic merge оказался ошибочным
    planner выбрал недостаточную глубину
    synthesis сделал слишком сильный вывод
    trust был плохо откалиброван


================================================================================
PHASE I-4. FAILURE TAXONOMY
================================================================================

ЦЕЛЬ:

YANDI должна знать НЕ только факт ошибки,
но и класс ошибки.


Начальная таксономия:

    INTENT_ERROR
    EPISTEMIC_CLASSIFICATION_ERROR
    PLANNER_ERROR

    RETRIEVAL_MISS
    RETRIEVAL_NOISE
    QUERY_GENERATION_ERROR

    SOURCE_DEPENDENCE_ERROR
    SOURCE_QUALITY_ERROR

    CLAIM_EXTRACTION_ERROR
    CLAIM_IDENTITY_ERROR
    SEMANTIC_MERGE_ERROR

    NLI_RELATION_ERROR
    CONTRADICTION_MISSED
    FALSE_CONTRADICTION

    VALIDATION_ERROR

    BELIEF_UPDATE_ERROR

    SYNTHESIS_OVERCLAIM
    SYNTHESIS_UNDERCLAIM

    TRUST_OVERESTIMATION
    TRUST_UNDERESTIMATION

    REFLECTION_ERROR
    POLICY_ERROR

    UNKNOWN_FAILURE


Таксономия должна расширяться осторожно.

НЕ создавать новый тип ошибки для каждого отдельного случая.


================================================================================
PHASE I-5. SUCCESS TAXONOMY
================================================================================

Самообучение только на ошибках создаст перекос.

Нужно фиксировать и устойчивые успехи:

    RETRIEVAL_SUCCESS
    INDEPENDENT_EVIDENCE_SUCCESS
    USEFUL_REFUTATION
    CORRECT_UNCERTAINTY
    CORRECT_ABSTENTION
    STABLE_CLAIM
    GOOD_SYNTHESIS
    WELL_CALIBRATED_TRUST
    SUCCESSFUL_RECHECK
    SUCCESSFUL_STRATEGY


Особенно важно:

    "Я сказала НЕ ЗНАЮ и позже оказалось,
     что данных действительно было недостаточно"

должно считаться успехом.


================================================================================
PHASE I-6. STRUCTURED REFLECTION 2.0
================================================================================

ЦЕЛЬ:

Reflection перестаёт писать преимущественно текстовые уроки.

Вместо:

    "Нужно искать больше источников"

нужно:

    Reflection {
        observation
        suspected_cause
        evidence
        proposed_action
        scope
        confidence
        falsification_condition
    }


Пример:

    observation:
        supported claim later became disputed

    suspected_cause:
        insufficient source independence

    proposed_action:
        min_independent_sources += 1

    scope:
        domain = medicine
        epistemic_class = causal

    confidence:
        0.62

    falsification_condition:
        strategy does not reduce revision rate


Reflection создаёт ГИПОТЕЗУ ОБ УЛУЧШЕНИИ,
а не приказ системе.


================================================================================
PHASE I-7. EXPERIENCE IDENTITY + DEDUPLICATION
================================================================================

Нельзя получить:

    10 000 episodes
    →
    10 000 одинаковых lessons.


Нужны:

    experience identity
    semantic lesson identity
    policy hypothesis identity


Система должна уметь объединять наблюдения:

    "нужно больше независимых источников"

из многих episodes в одну hypothesis family.


Но:

    "больше источников"

и

    "больше НЕЗАВИСИМЫХ источников"

не должны автоматически сливаться.


================================================================================
PHASE I-8. SEMANTIC EXPERIENCE RETRIEVAL
================================================================================

ЦЕЛЬ:

Перед новым решением YANDI ищет похожий прошлый опыт.


Не:

    загрузить всю память.


А:

    NEW QUERY
        ↓
    feature extraction
        ↓
    semantic retrieval
        ↓
    3–5 relevant experiences
        ↓
    Planner


Similarity должна учитывать не только текст query.

Желательные признаки:

    intent
    domain
    epistemic class
    risk
    claim type
    failure mode
    contradiction structure
    retrieval strategy
    source structure


Необходимо сравнить:

    lexical retrieval
    embedding retrieval
    hybrid retrieval

и выбрать по измерениям.


================================================================================
PHASE I-9. POLICY HYPOTHESIS
================================================================================

Experience НЕ должен напрямую менять Planner.

Цепочка:

    EXPERIENCE
        ↓
    REFLECTION
        ↓
    POLICY HYPOTHESIS


PolicyHypothesis:

    policy_id
    rule
    scope
    evidence_for
    evidence_against
    expected_effect
    confidence
    status
    created_from_experiences
    history


Статусы:

    OBSERVED
    HYPOTHESIS
    TRIAL
    SUPPORTED
    ACTIVE
    DEGRADED
    REJECTED
    RETIRED


================================================================================
PHASE I-10. POLICY LIFECYCLE
================================================================================

ЦЕЛЬ:

YANDI пересматривает собственные правила
так же, как она пересматривает Claims.


Пример:

    ACTIVE POLICY:
        controversial → 5 sources

Позже статистика показывает:

    latency +80%
    contradiction discovery +1%
    final stability без улучшения

Policy должна:

    ACTIVE
      ↓
    DEGRADED
      ↓
    RETIRED


История политики сохраняется.

Нельзя silently overwrite policy.


================================================================================
PHASE I-11. POLICY DEPENDENCY GRAPH
================================================================================

Политики могут зависеть друг от друга.

Например:

    POLICY A:
        controversial → refutation depth 3

    POLICY B:
        if refutation quality low → quality filter strict

Изменение A может менять полезность B.

Нужен bounded dependency mechanism,
но НЕ копировать claim dependency graph вслепую.

Сначала доказать необходимость реальными cases.


================================================================================
PHASE I-12. EXPERIMENT ENGINE
================================================================================

КРИТИЧЕСКИЙ ЭТАП.

Новая политика НЕ становится ACTIVE сразу.


Нужно:

    CURRENT POLICY
          ↓
       CONTROL

    CANDIDATE POLICY
          ↓
       EXPERIMENT


Пример:

    A:
        source_count = 3

    B:
        source_count = 5


Сравнивать:

    validation acceptance
    later revision rate
    contradiction discovery
    independent evidence
    grounding
    coverage
    trust calibration
    latency
    external calls
    failures


================================================================================
PHASE I-13. SHADOW POLICY MODE
================================================================================

До реального воздействия:

    Planner принимает production decision.

Параллельно:

    candidate policy строит SHADOW decision.

Shadow ничего не меняет.

Сохраняется:

    production_decision
    shadow_decision
    divergence
    predicted_effect


После накопления выборки можно решить,
имеет ли смысл controlled trial.


================================================================================
PHASE I-14. CONTROLLED POLICY TRIAL
================================================================================

Только после shadow evaluation.

Часть подходящих запросов:

    baseline policy

часть:

    candidate policy


Ограничения:

    bounded traffic
    rollback
    no high-risk experimentation initially
    deterministic assignment where possible
    full trace


Никакого автоматического rollout 100%.


================================================================================
PHASE I-15. POLICY PROMOTION / ROLLBACK
================================================================================

Candidate становится ACTIVE,
только если доказано улучшение.

Promotion criteria задаются заранее.

Не:

    "кажется, ответы лучше".


А:

    revision_rate ↓
    contradiction_detection ↑
    calibration_error ↓

при допустимых:

    latency
    compute cost
    failure rate


Если ухудшение:

    rollback.


================================================================================
PHASE I-16. ADAPTIVE PLANNER 2.0
================================================================================

Теперь Planner может использовать доказанный опыт.


Вход:

    query
    intent
    epistemic class
    risk
    relevant experiences
    active policies
    historical strategy performance
    current trust state


Выход:

    search strategy
    source count
    independence requirements
    refutation depth
    validation depth
    graph depth
    local model use
    external model use
    clarification requirement


ВАЖНО:

Planner не должен становиться бесконтрольной LLM,
которая каждый раз изобретает pipeline заново.

Нужны bounded actions.


================================================================================
PHASE I-17. STRATEGY IDENTITY
================================================================================

Чтобы учиться на стратегиях,
их нужно идентифицировать.

Strategy fingerprint должен описывать:

    search paths
    source requirements
    refutation settings
    validation settings
    model usage
    graph settings
    relevant policy set


Иначе статистика:

    "general_web работает 78%"

бессмысленна,
если general_web каждый раз означает разное.


================================================================================
PHASE I-18. STRATEGY OUTCOME + RELIABILITY
================================================================================

Для каждой стратегии:

    executions
    success
    failures
    delayed revisions
    calibration
    latency
    domain distribution


Но Strategy Reliability НЕ является Truth.

Она является prior для Planner.


================================================================================
PHASE I-19. SOURCE REPUTATION 2.0
================================================================================

НЕ:

    wikipedia = 0.84
    random_blog = 0.37


А условная модель:

    source
      × domain
      × claim_type
      × time
      × evidence_relation


Пример:

    source X:
        astronomy factual → strong
        political forecast → weak


Source reputation обновляется по судьбе Claims.

Нельзя:

    источник подтвердил сам себя
    → reputation ↑


================================================================================
PHASE I-20. SOURCE REPUTATION DECAY
================================================================================

Источники меняются.

Репутация 2026 года не обязательно равна репутации 2030.

Нужны:

    temporal windows
    decay
    minimum sample size
    uncertainty


Новый источник:

    UNKNOWN

а не:

    BAD.


================================================================================
PHASE I-21. TRUST CALIBRATION
================================================================================

Canonical Trust уже существует.

Теперь нужно проверить его исторически.


Вопрос:

    Когда YANDI говорила Trust=0.7,
    насколько часто такие выводы оставались устойчивыми?


Строить:

    predicted trust
        vs
    delayed outcome


Метрики:

    calibration curve
    Brier-like error where applicable
    revision frequency
    contradiction frequency


Сначала SHADOW calibration.

Не менять production thresholds автоматически.


================================================================================
PHASE I-22. METACOGNITIVE PROFILE
================================================================================

YANDI строит модель собственных способностей.


Пример:

    DOMAIN: astronomy
        retrieval_success: high
        revision_rate: low

    DOMAIN: medicine causal
        contradiction_miss: elevated
        semantic_relation_error: elevated


Также:

    error type frequency
    trend
    sample size
    uncertainty


Это НЕ Character Engine.

Это статистическая модель качества собственной работы.


================================================================================
PHASE I-23. UNCERTAINTY ABOUT SELF
================================================================================

Метакогниция тоже должна иметь uncertainty.

Нельзя:

    2 ошибки по Rust
    →
    "Я плохо знаю Rust".


Нужны:

    sample_count
    confidence interval / uncertainty
    recency
    domain coverage


================================================================================
PHASE I-24. METACOGNITION → PLANNER
================================================================================

Только после проверки профиля.

Пример:

    causal medical claims historically difficult
        ↓
    Planner:
        validation mandatory
        independent sources +1
        contradiction depth +1


Таким образом система заранее компенсирует
собственные известные слабости.


================================================================================
PHASE I-25. AUTONOMOUS GAP DETECTION
================================================================================

Теперь YANDI может искать:

    где у меня систематически мало evidence?
    где много revisions?
    где Trust плохо calibrated?
    где Planner часто ошибается?
    где retrieval часто ничего не находит?


Это создаёт:

    LearningGap


Не запускает бесконтрольное обучение.


================================================================================
PHASE I-26. AUTONOMOUS STUDY TASKS
================================================================================

LearningGap может породить bounded task:

    изучить тему
    собрать evidence
    проверить claims
    обновить beliefs
    создать experience


Но:

    budget
    depth
    frequency
    allowed sources

жёстко ограничены.


================================================================================
PHASE I-27. SELF-LEARNING SAFETY
================================================================================

Обязательные ограничения:

    max autonomous tasks
    max cascade depth
    cooldown
    compute budget
    web budget
    model-call budget
    disk budget


Никаких бесконечных:

    Reflection → Planner → Search → Reflection → ...


================================================================================
PHASE I-28. SELF-LEARNING OBSERVABILITY
================================================================================

Каждое изменение поведения должно объясняться.


YANDI должна отвечать:

    Какое правило изменилось?
    Почему?
    На основании каких episodes?
    Какой эксперимент?
    Какая метрика улучшилась?
    Какая версия правила была раньше?
    Можно ли откатить?


================================================================================
PHASE I-29. SELF-LEARNING MILESTONE
================================================================================

ЭТАП I считается завершённым НЕ когда существуют классы:

    ExperienceManager
    PolicyManager
    AdaptivePlanner


А когда доказана цепочка:

    реальный запрос
        ↓
    реальный outcome
        ↓
    delayed evidence
        ↓
    обнаруженная ошибка/успех
        ↓
    structured experience
        ↓
    policy hypothesis
        ↓
    shadow evaluation
        ↓
    controlled experiment
        ↓
    measurable improvement
        ↓
    policy promotion
        ↓
    следующий похожий запрос
        ↓
    Planner реально действует иначе
        ↓
    результат лучше baseline


ТОЛЬКО ТОГДА:

    YANDI = SELF-LEARNING SYSTEM.


################################################################################
################################################################################
#
#                    БОЛЬШОЙ ЭТАП II
#
#          YANDI ДОУЧИВАЕТ СОБСТВЕННУЮ ЛОКАЛЬНУЮ МОДЕЛЬ
#
################################################################################
################################################################################


================================================================================
3. ПРИНЦИП ЭТАПА II
================================================================================

НЕ обучать модель прежде всего "знаниям".

Знания остаются:

    Evidence
    Claims
    Beliefs
    Provenance
    History


Локальную модель обучаем:

    КАК ИССЛЕДОВАТЬ;
    КАК СОМНЕВАТЬСЯ;
    КАК СТРОИТЬ CLAIMS;
    КАК ИСКАТЬ ПРОТИВОРЕЧИЯ;
    КАК ОЦЕНИВАТЬ НЕДОСТАТОК ДАННЫХ;
    КАК МЕНЯТЬ МНЕНИЕ;
    КАК ОБЪЯСНЯТЬ ИЗМЕНЕНИЕ МНЕНИЯ;
    КАК НЕ ПРЕВРАЩАТЬ НЕОПРЕДЕЛЁННОСТЬ В ФАКТ.


То есть:

    KNOWLEDGE → externalized epistemic memory

    REASONING SKILL → model training


================================================================================
PHASE II-0. TRAINING READINESS GATE
================================================================================

До fine-tuning должны существовать:

    достаточный объём episodes;
    structured experiences;
    delayed outcomes;
    failure taxonomy;
    success taxonomy;
    policy outcomes;
    stable evaluation benchmark;
    train/validation/test split;
    contamination controls.


Если этого нет:

    НЕ ОБУЧАТЬ.


================================================================================
PHASE II-1. TRACE QUALITY SCORING
================================================================================

Не каждый trace годится для обучения.


TraceQuality:

    provenance completeness
    claim quality
    evidence quality
    contradiction handling
    final outcome stability
    reflection quality
    later verification
    absence of pipeline errors


Плохой trace не должен обучать модель плохому поведению.


================================================================================
PHASE II-2. TRAINING EXAMPLE TYPES
================================================================================

Разделить dataset.


A. SUCCESS TRACE

    хорошее исследование
    устойчивый результат


B. CORRECTED TRACE

    первоначальная ошибка
    новое evidence
    пересмотр
    объяснение причины


C. UNCERTAINTY TRACE

    данных недостаточно
    система корректно отказалась от сильного вывода


D. CONTRADICTION TRACE

    evidence конфликтует
    система не выбрала искусственного победителя


E. RETRIEVAL FAILURE TRACE

    первоначальный поиск слаб
    улучшенный поиск находит необходимое evidence


F. POLICY IMPROVEMENT TRACE

    стратегия A
    проблема
    стратегия B
    измеренное улучшение


================================================================================
PHASE II-3. NEGATIVE EXAMPLES
================================================================================

Особенно ценны пары:

    BAD DECISION
        vs
    CORRECTED DECISION


Пример:

    claim:
        A causes B

    плохое reasoning:
        correlation interpreted as causation

    исправленное:
        relation insufficient for causal conclusion


Но negative example должен быть доказан,
а не объявлен плохим другой LLM.


================================================================================
PHASE II-4. REVISION DATASET
================================================================================

Отдельный dataset:

    ORIGINAL POSITION
    ORIGINAL EVIDENCE
    ORIGINAL TRUST

    NEW EVIDENCE

    UPDATED POSITION
    UPDATED TRUST

    WHY CHANGED

    WHAT DID NOT CHANGE


Это может стать одним из самых ценных datasets YANDI.


================================================================================
PHASE II-5. DISAGREEMENT DATASET
================================================================================

Хранить случаи:

    model A says X
    model B says Y
    evidence says ...
    YANDI resolves / remains uncertain


Цель обучения:

не заставить local model соглашаться с большинством,

а научить:

    распознавать disagreement;
    искать причину;
    не путать consensus с evidence.


================================================================================
PHASE II-6. ABSTENTION DATASET
================================================================================

Отдельно обучать:

    "данных недостаточно"

    "это не проверено"

    "источники расходятся"

    "вопрос сформулирован неоднозначно"

    "необходимо уточнение"


Хороший отказ от вывода —
полноценный positive training example.


================================================================================
PHASE II-7. TRACE SANITIZATION
================================================================================

До training dataset:

    удалить технический мусор;
    stack traces;
    случайные parser artifacts;
    duplicated snippets;
    irrelevant HTML;
    malformed records.


Не уничтожать epistemic provenance.


================================================================================
PHASE II-8. PRIVACY BOUNDARY
================================================================================

Training pipeline не должен внезапно начать собирать
данные, которых YANDI сейчас не хранит.

Не добавлять:

    browser cookies
    account tokens
    email
    browser profile
    external account identity


Если пользователь сам написал персональные данные в query,
политика использования таких traces для training
должна быть определена ДО обучения.


================================================================================
PHASE II-9. TRAIN / VALIDATION / TEST SPLIT
================================================================================

КРИТИЧЕСКИ:

нельзя случайно положить semantic variants одного claim family
и в train, и в test.


Split желательно делать по:

    semantic family
    topic cluster
    temporal boundary
    episode family


Иначе evaluation будет ложно высокой.


================================================================================
PHASE II-10. TEMPORAL HOLDOUT
================================================================================

Особенно полезно:

    train = прошлое
    test = более поздние episodes


Это проверяет:

модель действительно научилась поведению

или:

просто запомнила dataset.


================================================================================
PHASE II-11. EXPERIENCE RETRIEVAL BEFORE FINE-TUNING
================================================================================

ПЕРЕД ИЗМЕНЕНИЕМ ВЕСОВ провести главный эксперимент.


BASELINE:

    local model alone


RAG-EXPERIENCE:

    local model
        +
    3–5 relevant historical traces/examples


Сравнить:

    claim quality
    contradiction handling
    abstention
    retrieval planning
    later correction
    latency


Если retrieved demonstrations не помогают,
fine-tuning преждевременен.


================================================================================
PHASE II-12. DEMONSTRATION SELECTION
================================================================================

Не выбирать примеры только по similarity.

Нужны:

    relevance
    quality
    outcome stability
    diversity
    recency
    contradiction usefulness


Избегать:

    5 почти одинаковых examples.


================================================================================
PHASE II-13. BASE MODEL BENCHMARK
================================================================================

Перед обучением заморозить baseline.

Набор задач:

    factual
    ambiguous
    controversial
    causal
    insufficient evidence
    contradiction
    revision
    retrieval planning
    claim extraction
    abstention


Сохранить:

    outputs
    scores
    traces
    latency
    resource usage


================================================================================
PHASE II-14. FIRST CONTROLLED FINE-TUNING
================================================================================

Только после readiness gate.

Начать с минимального вмешательства:

    LoRA / adapter / equivalent lightweight tuning

а не сразу full model training.


Первый dataset должен быть маленьким,
высококачественным и проверенным.


Цель первого обучения:

НЕ "сделать YANDI умнее вообще".

Одна измеримая способность.

Например:

    better claim decomposition

или:

    better uncertainty handling

или:

    better contradiction recognition.


================================================================================
PHASE II-15. MODEL VERSIONING
================================================================================

Каждая модель:

    model_id
    base_model
    dataset_version
    training_config
    training_code_version
    timestamp
    benchmark_result
    parent_model
    status


Статусы:

    BASELINE
    CANDIDATE
    SHADOW
    TRIAL
    PROMOTED
    REJECTED
    RETIRED


================================================================================
PHASE II-16. CANDIDATE SHADOW MODE
================================================================================

Production использует:

    model_v1


Параллельно:

    model_v2_candidate


На одинаковых задачах сравнивать:

    claims
    query generation
    uncertainty
    contradictions
    final result


Candidate пока НЕ управляет production pipeline.


================================================================================
PHASE II-17. BLIND MODEL EVALUATION
================================================================================

Не позволять evaluator знать:

    old model
    new model


Сравнивать анонимно.

По возможности:

    deterministic metrics
    epistemic pipeline outcome
    delayed outcome

имеют приоритет над LLM judge.


================================================================================
PHASE II-18. REGRESSION BENCHMARK
================================================================================

Новая модель может улучшить одно
и разрушить другое.


Проверять:

    factual accuracy
    hallucination
    claim decomposition
    contradiction handling
    uncertainty
    abstention
    instruction following
    query generation
    multilingual behavior
    latency
    memory/resource usage


================================================================================
PHASE II-19. CATASTROPHIC BEHAVIOR CHECK
================================================================================

Особенно искать:

    стала увереннее без оснований;
    стала реже говорить "не знаю";
    начала соглашаться с majority;
    хуже замечает contradictions;
    переобучилась на стиле traces;
    начала повторять служебные форматы;
    хуже работает вне training domains.


================================================================================
PHASE II-20. MODEL PROMOTION GATE
================================================================================

Candidate становится production model
только если:

    predefined metrics improved

И:

    critical regressions absent.


Не:

    average score +2%
    при abstention collapse.


Критические эпистемические свойства
имеют hard veto.


================================================================================
PHASE II-21. MODEL ROLLBACK
================================================================================

Всегда сохранять предыдущую production model.

Если live показывает деградацию:

    immediate rollback.


Никакого:

    "мы уже потратили неделю на обучение,
     поэтому оставим".


================================================================================
PHASE II-22. MODEL OUTCOME HISTORY
================================================================================

После promotion продолжать сравнение.

Модель может выглядеть лучше offline,
но хуже через месяц по delayed outcomes.


Хранить:

    model version
    episode
    immediate outcome
    delayed outcome
    revision rate
    calibration
    policy interaction


================================================================================
PHASE II-23. MODEL SELF-CRITIQUE DATASET
================================================================================

После нескольких поколений появится dataset:

    model_v1 decision
    model_v2 correction
    later evidence
    final outcome


Это позволяет изучать:

    какие ошибки действительно исчезают;
    какие мигрируют;
    какие появляются новые.


================================================================================
PHASE II-24. CONTINUAL LEARNING — ТОЛЬКО ПОСЛЕ ДОКАЗАТЕЛЬСТВА
================================================================================

НЕ делать:

    каждый день собрали traces
    →
    ночью автоматически переобучили
    →
    утром новая production model.


Сначала controlled generations:

    dataset N
    ↓
    candidate
    ↓
    evaluation
    ↓
    human/automatic gate
    ↓
    promotion


Автоматизация promotion —
очень поздняя стадия.


================================================================================
PHASE II-25. KNOWLEDGE / WEIGHT FIREWALL
================================================================================

Критический архитектурный контроль.

Перед добавлением training example спросить:

    Это навык?

или:

    Это конкретный изменяемый факт?


Если:

    "Плутон имеет характеристику X"

→ Knowledge Engine.


Если:

    "при conflicting measurements
     нельзя делать сильный вывод"

→ candidate for model training.


================================================================================
PHASE II-26. SELF-IMPROVEMENT LOOP
================================================================================

Целевая петля:

    production model
          ↓
    real tasks
          ↓
    traces
          ↓
    delayed outcomes
          ↓
    verified experiences
          ↓
    high-quality training subset
          ↓
    candidate model
          ↓
    shadow
          ↓
    blind evaluation
          ↓
    controlled trial
          ↓
    promotion / rejection
          ↓
    production model N+1


Но модель N+1 снова проходит тот же цикл.

Никакой версии не доверяем навсегда.


================================================================================
PHASE II-27. ЭТАП II ЗАВЕРШЁН, ЕСЛИ
================================================================================

Доказано на реальных данных:

    model N
       ↓
    накопленный опыт YANDI
       ↓
    training dataset
       ↓
    model N+1
       ↓
    blind evaluation
       ↓
    regression evaluation
       ↓
    controlled live trial
       ↓
    статистически/эпистемически лучше
       ↓
    promotion

И после promotion:

    delayed outcomes подтверждают,
    что улучшение сохранилось.


Только тогда можно утверждать:

    YANDI НЕ ТОЛЬКО УЧИТ СВОЮ АРХИТЕКТУРУ,

    YANDI УЧИТ СОБСТВЕННУЮ МОДЕЛЬ.


################################################################################
################################################################################
#
#                    БОЛЬШОЙ ЭТАП III
#
#                ПОЗДНЕЕ РАЗВИТИЕ
#
################################################################################
################################################################################


================================================================================
4. ЭТАП III НЕ ЯВЛЯЕТСЯ ТЕКУЩЕЙ ЗАДАЧЕЙ
================================================================================

Сюда относятся будущие направления:

    multi-LLM longitudinal evaluation;
    external AI response history;
    distributed reasoning;
    P2P knowledge verification;
    node reliability;
    distributed experience evaluation;
    distributed learning;
    autonomous research;
    cooperative model improvement.


Токенизация, экономические стимулы,
оплата нод, Proof of Utility,
вознаграждение проверяющих,
фонды и governance

НАМЕРЕННО НЕ ВХОДЯТ
в текущий roadmap self-learning.


================================================================================
5. MULTI-LLM EXPERIENCE — БУДУЩАЯ ВЕТКА
================================================================================

В будущем внешние AI-чаты могут рассматриваться как:

    интеллектуальные наблюдатели,

а не как Truth Oracle.


Хранить:

    provider/model
    node identifier
    query
    answer
    timestamp
    verdict
    evidence where available
    later outcome


Изучать:

    model agreement
    model disagreement
    temporal stability
    correction behavior
    systematic failure modes


Один provider на 10 нодах:

    execution diversity

но НЕ обязательно:

    knowledge independence.


================================================================================
6. DISTRIBUTED SELF-LEARNING — БУДУЩАЯ ВЕТКА
================================================================================

Локальный Experience:

    ↓
anonymized / network-safe epistemic record

    ↓
independent node evaluation

    ↓
aggregated evidence about policy

    ↓
local decision


Никакая нода не обязана принимать
чужую policy автоматически.


================================================================================
7. AUTONOMOUS RESEARCH — БУДУЩАЯ ВЕТКА
================================================================================

После зрелой метакогниции:

    detect knowledge gap
        ↓
    formulate research task
        ↓
    bounded retrieval
        ↓
    claims/evidence
        ↓
    validation
        ↓
    belief update
        ↓
    experience


Но автономность всегда bounded.


################################################################################
################################################################################
#
#                   ОБЩИЕ ИНЖЕНЕРНЫЕ ПРАВИЛА
#
################################################################################
################################################################################


================================================================================
8. ОБЯЗАТЕЛЬНАЯ ДИСЦИПЛИНА ИЗМЕНЕНИЙ
================================================================================

Для каждого production-changing этапа:

    AUDIT
      ↓
    DESIGN
      ↓
    TARGETED REGRESSION
      ↓
    FULL REGRESSION
      ↓
    LIVE RUN
      ↓
    READ ACTUAL TRACE / PERSISTENCE
      ↓
    COMMIT


NO LIVE → NO COMMIT.


Перед milestone push:

    audit commits
    full regression
    live matrix
    inspect persisted state
    working tree clean


Только затем:

    push origin/main.


================================================================================
9. НЕЛЬЗЯ ЧИНИТЬ ПЛАН ВМЕСТО СИСТЕМЫ
================================================================================

Если benchmark показывает,
что предполагаемый bottleneck не является bottleneck:

    НЕ РЕАЛИЗОВЫВАТЬ ПЛАН РАДИ ПЛАНА.


Если экспериментальная policy хуже:

    REJECT.


Если новая модель хуже:

    REJECT.


Если новый алгоритм сложнее,
но не улучшает измеримый результат:

    НЕ ВНЕДРЯТЬ.


================================================================================
10. SHADOW FIRST
================================================================================

Для механизмов, меняющих решение:

    SHADOW FIRST.


Применимо к:

    Trust
    Policies
    Adaptive Planner
    Calibration
    Source Reputation
    Model Candidate


Сначала:

    посчитать

потом:

    сравнить

потом:

    доказать

только потом:

    переключить production.


================================================================================
11. НЕ СОЗДАВАТЬ ПАРАЛЛЕЛЬНЫЕ ИСТИНЫ
================================================================================

Перед созданием нового:

    identity system
    graph
    trust engine
    policy store
    experience store
    model registry

проверить существующее.


Предпочитать:

    EXTEND / CONNECT

вместо:

    DUPLICATE.


================================================================================
12. STORAGE DISCIPLINE
================================================================================

Не хранить бесконечные копии одних данных.

Предпочитать:

    immutable/raw trace
         +
    compact derived records
         +
    references


Нужны:

    retention policy
    archival strategy
    indexes
    schema versioning


Но storage optimization выполнять
только после измерений.


================================================================================
13. PRIVACY DISCIPLINE
================================================================================

Не расширять собираемые данные без необходимости.

Текущая архитектурная идея:

    node identity = technical identity

а не:

    human identity.


Browser/session credentials
не являются epistemic data.


================================================================================
14. ОБЪЯСНИМОСТЬ САМОИЗМЕНЕНИЯ
================================================================================

Ни Planner,
ни Policy Engine,
ни Model Trainer

не имеют права менять систему так,
чтобы потом нельзя было ответить:

    ЧТО изменилось?
    ПОЧЕМУ?
    НА КАКИХ ДАННЫХ?
    КАКОЙ БЫЛ BASELINE?
    КАКОЙ ПОЛУЧЕН ЭФФЕКТ?
    МОЖНО ЛИ ОТКАТИТЬ?


================================================================================
15. ИЕРАРХИЯ ДОВЕРИЯ К СОБСТВЕННЫМ ИЗМЕНЕНИЯМ
================================================================================

Наблюдение:

    возможно интересно.


Повторяющийся pattern:

    hypothesis.


Reflection:

    proposed explanation.


Policy:

    экспериментальная гипотеза.


Shadow:

    прогноз эффекта.


Controlled trial:

    измерение эффекта.


Repeated success:

    evidence of usefulness.


Active policy:

    рабочее правило,
    но НЕ вечная истина.


Model promotion:

    лучшая известная версия,
    но НЕ окончательная версия.


================================================================================
16. ЧТО НЕ ДЕЛАТЬ ПРЕЖДЕВРЕМЕННО
================================================================================

НЕ:

    автоматически fine-tune на всех traces;

    обучать модель на каждом новом belief;

    превращать Reflection text в policy напрямую;

    повышать Source Trust только за согласие с YANDI;

    считать большинство LLM доказательством;

    автоматически менять Trust thresholds;

    автоматически выкатывать candidate model;

    запускать бесконечные autonomous research loops;

    строить огромный новый framework,
    если можно замкнуть существующие компоненты.


================================================================================
17. КЛЮЧЕВОЙ ПЕРЕХОД №1
================================================================================

Сегодня:

    YANDI умеет пересматривать ЗНАНИЕ.


Следующая большая цель:

    YANDI умеет пересматривать
    СОБСТВЕННЫЕ СПОСОБЫ ПОЛУЧЕНИЯ ЗНАНИЯ.


Это означает:

    claim re-evaluation
        +
    policy re-evaluation.


================================================================================
18. КЛЮЧЕВОЙ ПЕРЕХОД №2
================================================================================

После зрелого self-learning:

    YANDI умеет изменять архитектурное поведение.


Следующая цель:

    YANDI умеет улучшать
    саму локальную модель.


Но:

    веса модели никогда не становятся
    единственным хранилищем знания.


================================================================================
19. КОНЕЧНАЯ ЦЕЛЬ
================================================================================

Не построить систему, которая:

    "всегда знает правильный ответ".


Построить систему, которая:

    знает, почему она пришла к выводу;

    знает, откуда взялись доказательства;

    знает, что им противоречит;

    знает, насколько вывод устойчив;

    замечает, когда вывод перестал быть устойчивым;

    пересматривает его;

    анализирует собственную ошибку;

    предлагает изменение стратегии;

    проверяет это изменение экспериментально;

    сохраняет полезное изменение;

    откатывает вредное;

    накапливает доказанный опыт;

    и только после этого использует этот опыт,
    чтобы обучить следующую версию самой себя.


================================================================================
20. КРИТЕРИЙ НАСТОЯЩЕГО SELF-LEARNING
================================================================================

НЕ:

    "YANDI записала lesson."


НЕ:

    "YANDI изменила параметр."


НЕ:

    "YANDI дообучила модель."


А:

    YANDI сделала X
        ↓
    наблюдала результат
        ↓
    обнаружила проблему
        ↓
    сформировала гипотезу Y
        ↓
    проверила Y
        ↓
    Y дала лучший результат
        ↓
    Y была принята
        ↓
    на новых данных улучшение сохранилось
        ↓
    история X и Y сохранена
        ↓
    при деградации возможен rollback.


ТОЛЬКО ЭТО НАЗЫВАТЬ:

    SELF-LEARNING.


================================================================================
21. КРИТЕРИЙ НАСТОЯЩЕГО MODEL SELF-IMPROVEMENT
================================================================================

НЕ:

    "loss уменьшился."


НЕ:

    "LoRA обучилась."


НЕ:

    "новая модель отвечает красивее."


А:

    накопленный доказанный опыт
        ↓
    curated training dataset
        ↓
    candidate model
        ↓
    unseen evaluation
        ↓
    epistemic regression suite
        ↓
    blind comparison
        ↓
    controlled live evaluation
        ↓
    delayed outcome
        ↓
    устойчивое улучшение
        ↓
    promotion.


================================================================================
22. MASTER PRIORITY
================================================================================

ПРИОРИТЕТ A:

    EXPERIENCE
    OUTCOMES
    REFLECTION
    POLICY
    EXPERIMENTS
    ADAPTIVE PLANNER


ПРИОРИТЕТ B:

    CALIBRATION
    SOURCE HISTORY
    METACOGNITION
    AUTONOMOUS GAP DETECTION


ПРИОРИТЕТ C:

    TRACE DEMONSTRATIONS
    TRAINING DATASET
    CONTROLLED FINE-TUNING
    MODEL EVALUATION
    MODEL PROMOTION


ПРИОРИТЕТ D:

    DISTRIBUTED SELF-LEARNING
    MULTI-LLM LONGITUDINAL DATA
    AUTONOMOUS RESEARCH


================================================================================
23. ПЕРВАЯ РАБОТА ПОСЛЕ ЭТОГО ROADMAP
================================================================================

НЕ НАЧИНАТЬ РЕАЛИЗАЦИЮ ROADMAP ЦЕЛИКОМ.

Создать отдельный документ-ТЗ.

Первое ТЗ должно начинаться с:

    CURRENT ARCHITECTURE AUDIT
        ↓
    ROADMAP RECONCILIATION
        ↓
    EXPERIENCE / REFLECTION / PLANNER DATA FLOW AUDIT


После аудита определить
МИНИМАЛЬНЫЙ первый production step.


Не предполагать заранее,
что новый модуль необходим.


================================================================================
КОНЕЦ YANDI MASTER ROADMAP
================================================================================