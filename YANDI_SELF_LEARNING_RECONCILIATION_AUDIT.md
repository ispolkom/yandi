================================================================================
YANDI SELF-LEARNING RECONCILIATION AUDIT — PHASE I-0
================================================================================
Дата: 2026-08-27
Скоуп: /home/iam/yandi/agent/ ТОЛЬКО (не yandi целиком — pet/, node/, registry/
       затрагивались только как persistence-потребители agent/).
Метод: 5 параллельных read-only форков (Planner/Reflection/Experience/Policy;
       Dataset/Trace/Persistence; Claims/Beliefs/Identity; Trust/Validation/
       Sources; Orchestrator wiring/Perf/Dead code) + личная верификация
       ключевых находок ведущим агентом (grep, чтение кода, `git show` на
       удалённый отчёт). Production код не менялся, коммитов не делалось до
       завершения аудита.

================================================================================
1. EXECUTIVE SUMMARY
================================================================================

Главный вывод: **self-learning в YANDI не начинается с нуля.** В проде уже
работает ЖИВОЙ, НЕЗАЩИЩЁННЫЙ (без shadow/experiment/promotion/rollback) цикл
"reflection → policy → planner", который меняет поведение планировщика на
СЛЕДУЮЩЕМ запросе — сегодня, без разрешения этого ТЗ. Это одновременно и
хорошая новость (часть инфраструктуры Stage I уже написана и приносит
пользу), и главный P0: этот механизм нарушает roadmap-инварианты 1.12, 1.13,
§10 (SHADOW FIRST), §40, и должен быть явно взят под контроль (гейтирован
или заменён), а не расширен вслепую.

Эпистемическое ядро (Claims → Evidence → Belief → Dependency, Phases 0-14) —
в хорошем состоянии: единые canonical owners, стабильная сквозная identity,
причинная история (`BeliefManager.history[]`) уже соответствует форме, которую
roadmap просит для Delayed Supervision (I-3) и Observability (I-28). Это
надёжный фундамент для будущего Experience-слоя.

Trust НЕ является единым источником истины для ВСЕХ потребителей: финальный
пользовательский Trust — canonical (один cutover, `writeback.py:626-638`), но
7+ более ранних consumers (включая `OutcomeRecord`/`trace.outcome`,
`archive_query`, dataset episodes) сохраняют **pre-canonical**, более
"рыхлый" Trust. Это уже было известно и намеренно отложено предыдущей фазой
(см. §15) — но именно на этих полях будущий ExperienceRecord/OutcomeModel
не должен строиться напрямую.

Найдено множество naming collisions ("Reflection" ×4, "Policy" ×2, "Trust"
×2, "Validator" ×4, "Experience"/"Knowledge Graph" — пересечение с
personality-доменом) — не логическое дублирование, а риск того, что будущий
Stage I код случайно "приземлится" не туда или спутает домены.

Единственный существующий trace→SFT датасет-билдер (`orch_dataset.py`)
доказанно сломан на 100% реальных данных (фильтр не соответствует реальной
схеме) — Stage II по датасету НЕ готов, но это не блокирует Stage I.

**Рекомендация: CONDITIONAL GO** — можно начинать Stage I, но НЕ с
`ExperienceRecord`/`PolicyHypothesis` с нуля, а с (a) явного гейтирования
существующего reflection→policy loop и (b) починки identity-связки
episode↔trace↔canonical trust. См. §25.

================================================================================
2. BASELINE
================================================================================

- HEAD (до аудита и после, production код не менялся): `27f1d9ac54480e6301ddfad707913b7065b5dbeb`
- origin/main: `517eb1a9de954c00f7b02d19a6fd37023061b3c9`
- Divergence: HEAD на 1 коммит впереди origin/main (`27f1d9a` — docs cleanup,
  уже запушенный до этого ТЗ не был; аудит НЕ пушит, см. §32 брифа).
- Working tree на старте: чисто, кроме ожидаемого `agent/dataset/episodes_20260827.jsonl`
  (живой append производственным трафиком).
- **Regression sweep: 29/29 GREEN.** Все `agent/*_regression_test.py`
  (найдено 29 файлов) пройдены через `/home/iam/venv/bin/python3` с
  `PYTHONPATH=/home/iam/yandi` (venv существует по пути `/home/iam/venv`,
  содержит numpy и прочие зависимости; system python3 их не имеет — это
  важно для любого будущего CI/regression шага в Stage I).
- Redis: жив (`PONG`). Ollama: не отвечает на `:11434` — новые live LLM-вызовы
  в ходе аудита не делались, все находки о "live" поведении основаны на
  РЕАЛЬНО persisted traces/episodes от предыдущих live-прогонов, а не на
  новых запросах.
- `registry/` (persistence root): 29MB, ~27 top-level записей — полная карта
  в §5.
- Production live sanity: не запускался НОВЫЙ live-запрос (Ollama недоступен
  в этой сессии) — вместо этого выполнен **live trace walkthrough** (TOR §23)
  по уже persisted трассам, см. §15. Это ослабление относительно буквы ТЗ
  зафиксировано явно, не скрыто.

================================================================================
3. CURRENT COGNITIVE DATA FLOW
================================================================================

Подтверждённый живой production entry point:
`start.sh` → `pet/council_chat_server.py` → `pet/chat_orch.py:151`
→ `from agent.orchestrator_v2 import process` (единственная live HTTP-точка
входа, `agent/orchestrator.py` — мёртв, см. §8).

```
QUERY
  → orchestrator_v2.process()
  → orchestrator/pre_pipeline.py::run_pre_pipeline
        [СОЦИАЛЬНЫЙ ДИАЛОГ?] → character_engine/relationship_gate/social_analyzer
                                (ОТДЕЛЬНЫЙ домен, "Experience"/"Reflection" там
                                 означают personality-banter, НЕ epistemic — см. §11)
        [ИНАЧЕ] → epistemic pipeline продолжается
  → orchestrator/pipeline.py::run_standard_pipeline (1005 строк, не pure —
        cache/disk/network/LLM side effects на всех стадиях, см. §9)
        [0] cache check → [1] risk assess → [3] intent → [3.5] epistemic
        classify → [4] clarification → retrieval (registry+web) → claims
        (validation→mapping→retrieval→status→disagreement→lifecycle)
        → belief_manager.add_belief() [LIVE, registry/beliefs.json]
        → trust: 2 независимых strand'a (synthesizer формула + trust_gate
          строгий гейт) → MIN'уются в ОДНУ canonical точку
          (writeback.py:626-638) → RESPONSE
  → orchestrator/response/writeback.py
        → archive_query() [PRE-canonical trust snapshot, см. §15]
        → OutcomeRecord/trace.set_outcome() [PRE-canonical, persists into trace]
        → self_model.add_decision(), memory.add_query() [PRE-canonical]
        → reflection.reflect_on_query() [LIVE — mistakes/lessons →
          policy_change → _apply_policy_to_planner() → immediate write
          registry/reflection_policies.json, БЕЗ shadow/experiment — см. §12]
        → experience_memory.add_experience() [LIVE, registry/experiences/*.json]
        → dataset_builder.record_episode() [LIVE, agent/dataset/episodes_*.jsonl,
          PRE-canonical trust]
        → core_loop.run_cycle() — ТОЛЬКО на первом запросе процесса
          (is_running latch bug, см. §8/§12 P1)
        → canonical Trust cutover (§ above) → tracer.save_trace(trace)
          [registry/dataset/orch_traces/*.jsonl — реальный богатый trace]
  → RESPONSE (canonical trust) → пользователь
  → TRACE (orch_traces/*.jsonl, с trust_gate-типизированным полем
    trace.learning[] — структурными rule-объектами {type,rule,confidence})
  → DATASET (episodes_*.jsonl — компактный digest, БЕЗ episode_id/trace_id)
  → REFLECTION/EXPERIENCE (см. выше — уже read-back для reflection_policies.json
    и experience_memory'шных lessons; но trace.learning[] — WRITE-ONLY,
    подтверждено: ни один pipeline-файл не импортирует
    trace_metrics/trace_evaluator/trace_inspector, см. §12)
  → NEXT QUERY: orch_planner.py читает reflection_policies.json +
    experience_memory lessons (string-matching) → мутирует план (веб-поиск
    skip/steps) — ЭТО ЖИВАЯ СТРЕЛКА, не аспирационная.
```

Классификация ключевых стрелок (TOR §8):
| Стрелка | Статус |
|---|---|
| RESPONSE → TRACE (orch_traces) | LIVE |
| RESPONSE → DATASET (episodes) | LIVE, но без общего id с trace |
| TRACE → trust_gate.learning[] | WRITE-ONLY (подтверждено, не читается) |
| RESPONSE → REFLECTION (reflection_loop) | LIVE |
| REFLECTION → POLICY → PLANNER (next query) | **LIVE, UNGATED** — главный P0 |
| RESPONSE → EXPERIENCE (experience_memory) | LIVE, читается planner'ом |
| RESPONSE → CORE_LOOP (per-query cycle) | DEAD после 1-го запроса процесса (P1 bug) |
| Delayed Outcome / OUTCOME_REVISION (roadmap I-2) | MISSING — нигде не найдено |
| Strategy Identity / Reliability (roadmap I-17/18) | MISSING — нет persisted per-strategy статистики |
| Source Reputation (web-source, roadmap I-19) | MISSING — только within-request clustering, нет cross-request store |

================================================================================
4. OWNERSHIP MAP
================================================================================

| Концепция | Canonical owner | Persistence | Статус |
|---|---|---|---|
| Query/Plan | `agent/orch_planner.py::build_plan`/`PlanResult` | не персистится отдельно | ACTIVE, transient |
| Trace | `agent/orch_tracer.py::DecisionTracer` | `registry/dataset/orch_traces/*.jsonl` | ACTIVE — canonical, богатая схема |
| Episode (digest) | неизвестный writer внутри `writeback.py`-цепочки (dataset_builder), НЕ найден в файлах, покрытых форками — **NEEDS VERIFICATION** конкретной функции | `agent/dataset/episodes_*.jsonl` | ACTIVE, но identity изолирован от Trace |
| Claim | `agent/orchestrator/claims/*.py` + `claim_validator.py` | через trace/dataset | ACTIVE, единый |
| Claim Family | `claim_family_registry.py::ClaimFamilyRegistry` | `registry/claim_families.json`, `claim_family_graph.json` | ACTIVE |
| Evidence | `evidence_pool.py::build_canonical_evidence_pool/merge_evidence` | через belief/trace | ACTIVE, единый |
| Belief | `belief_manager.py::BeliefManager` | `registry/beliefs.json` (599 записей, mixed-writer — epistemic + personality, см. §6) | ACTIVE, единый |
| Dependency (family↔family) | `family_dependency_graph.py::FamilyDependencyGraph` | persistence на диск НЕ подтверждена (NEEDS VERIFICATION) | ACTIVE (in-memory/shadow-origin) |
| Trust (user-facing final) | `canonical_trust.py::compute_canonical_trust` (MIN двух strand'ов) | `trace.trust` | ACTIVE, единый, но см. §15 про pre-cutover consumers |
| Trust (strand 1) | `orch_synthesizer.py` формула | `synthesis_result.trust_level` (pre-cutover) | ACTIVE, ввод в canonical |
| Trust (strand 2) | `orchestrator/epistemic/trust_gate.py::apply_epistemic_trust_adjustment` | было `trace.trust` до консолидации | ACTIVE, ввод в canonical |
| Source (web) reputation | **ОТСУТСТВУЕТ** cross-request owner — `source_clustering.py`/`source_independence_prototype.py` работают только within-request | нет | **MISSING** |
| Node (P2P) reputation | `orch_reputation.py` (другое identity-пространство — node_id, не URL) | `registry/reputation/*.db` | ACTIVE, но не то, что нужно для roadmap I-19 |
| Reflection (epistemic per-query) | `reflection_loop.py::ReflectionLoop` (singleton) | `registry/reflection_policies.json` | **ACTIVE / LIVE**, untyped |
| Policy (behavioral) | тот же `ReflectionLoop.active_policies` + ad-hoc string-derived (orch_planner.py, не персистится) | `registry/reflection_policies.json` (частично) | **ACTIVE, но БЕЗ lifecycle-статусов** (roadmap I-9) |
| Lesson | Inline `ReflectionLoop._extract_lessons()` — `List[str]`, без id/confidence-структуры | 3 разных места без общей identity | ACTIVE, но неструктурирован |
| Experience | `experience_memory.py::ExperienceMemory` | `registry/experiences/*.json` | ACTIVE, но personality-first схема, без episode_id |
| Outcome (immediate) | `agent/orch_schemas.py::OutcomeRecord` | внутри Trace | ACTIVE, immediate-only, PRE-canonical trust |
| Outcome (delayed) | **ОТСУТСТВУЕТ** | — | **MISSING** |
| Strategy | `strategy_router.py`/`target_router.py` | не персистится с outcome-статистикой | ACTIVE, но без identity/reliability |
| Historical Reliability | **ОТСУТСТВУЕТ** отдельно от Belief confidence decay | — | **MISSING** как отдельная концепция, но паттерн уже есть в `BeliefManager._apply_decay` |

================================================================================
5. PERSISTENCE MAP
================================================================================

| Store | Формат | Объём (на момент аудита) | Writer | Readers | Статус |
|---|---|---|---|---|---|
| `registry/dataset/orch_traces/{YYYYMMDD}.jsonl` | JSONL, append, day-bucketed | 434 записи, 10 файлов, 2026-08-01..27 | `orch_tracer.DecisionTracer.save_trace()` | trace_* CLI-инструменты (не в live pipeline) | **ACTIVE — канонический trace store** |
| `agent/dataset/episodes_{YYYYMMDD}.jsonl` | JSONL, append | 152 записи, 8 файлов | не найден конкретный writer в покрытых файлах (NEEDS VERIFICATION) | `orch_planner.py` (частично, через experience/lessons) | ACTIVE, но без id-связи с trace |
| `registry/beliefs.json` | JSON, полная перезапись | 1.4MB, 599 записей | epistemic pipeline + personality-код (curiosity.py, disagreement_engine.py, personality_core.py) | belief_manager, dependency_recheck | ACTIVE, mixed-writer, unbounded growth (P2) |
| `registry/claim_families.json`, `claim_family_graph.json` | JSON | 30KB/16KB | claim_family_registry.py | сам себя + dependency graph | ACTIVE |
| `registry/experiences/{user}.json`, `global.json` | JSON, полная перезапись | 280KB (global) | experience_memory.py | orch_planner.py | ACTIVE |
| `registry/reflection_policies.json` | JSON, полная перезапись | 147KB | reflection_loop.py | orch_planner.py | **ACTIVE — ключевой P0-компонент** |
| `registry/disagreements.json` | JSON | 748KB, 900 записей | disagreement_engine.py | belief_manager, orchestrator/claims/disagreement.py | ACTIVE, mixed epistemic+personality |
| `registry/episodic_memory.json` | JSON | 703KB | memory_episodic.py | — | ACTIVE (не аудировано глубоко) |
| `registry/transport_memory.json` | JSON | 936KB | transport_memory.py | — | ACTIVE (вне скоупа форков) |
| `registry/traces/*.json` (2 файла) | JSON | 2 файла, старые | `orchestrator_v2.py` (генератор trace_id) — но это старый путь | — | **DORMANT/LEGACY** |
| `registry/traces/{category}.db` (9 SQLite) | SQLite (`db/schema.py`) | — | только `db/migrate.py` (одноразовая миграция с внешнего смонтированного диска) | никто | **DEAD** (P2P knowledge-cache половина `KnowledgeDB`) |
| `registry/knowledge/{cat}.db`, `registry/index.db` | SQLite | — | `orch_query_archive.py::record_query` (LIVE, вызывается на каждый ответ) | — | ACTIVE (другая половина `KnowledgeDB`, split-liveness) |
| `registry/orch_metrics.jsonl` | JSONL | 38KB | `orch_monitoring.py` (buffered, flush каждые 10 событий) | никто в live pipeline | **НЕНАДЁЖЕН** — не обновлялся с 2026-07-15 из-за отсутствия flush-on-shutdown (P1) |
| `registry/dataset/orch_sft/` | JSONL (цель) | пусто/почти пусто на практике | `orch_dataset.py::OrchDatasetBuilder` | orch_finetune.py | **ЛОМАН** — фильтр не матчит реальную схему (P1, доказано на 434/434 записях) |
| `registry/reputation/*.db` | SQLite | — | `orch_reputation.py` | consensus_engine.py (мёртв) | ACTIVE, но другое identity-пространство (P2P node, не web source) |
| `registry/policy/` | пусто | 0 файлов | `agent/policy.py` (secret-scanning/audit — НЕ epistemic policy) | — | naming collision, не self-learning |

Storage discipline: `beliefs.json`/`disagreements.json`/`episodic_memory.json`/
`transport_memory.json` — монолитные JSON-файлы, полная перезапись при
каждом сохранении, без retention/decay/archival механизма. Не коррекционный
баг сегодня, но при продолжении роста ударит по latency read-modify-write.
**P2.**

================================================================================
6. IDENTITY MAP
================================================================================

| Identity | База | Persistent? | Cross-request? | Связан с другими? |
|---|---|---|---|---|
| `claim_id` | assigned при extraction (upstream) | через belief.claim_ids | да | ДА — 598/599 beliefs имеют непустой `claim_ids` (реально wired, не write-only) |
| `content_hash` | `claim_identity.py::compute_claim_content_hash` | функция, не store | — | питает claim_family dedup |
| `semantic_family_id` | `claim_family_registry.py` через `claim_semantic_identity_prototype.py` | `registry/claim_families.json` | да | связан с belief через `_belief_for_family()` |
| `belief.id` (`bel_...`) | random при создании | `registry/beliefs.json` | да | `evidence_for`/`evidence_against` (`ev_...`), `claim_ids`, `history[]` — хорошая причинная трассируемость |
| `evidence.id` (`ev_...`) | evidence_pool/mapping слой | только внутри belief/trace, нет отдельного store | частично | — |
| `trace_id` | `orch_tracer.py` (day-bucketed jsonl) | `registry/dataset/orch_traces/*.jsonl` | да | **НЕ связан** ни с episode, ни с experience_memory записью |
| episode (episodes_*.jsonl) | **отсутствует явный id** | файл | да (по файлу) | **НЕ связан** с trace_id — попытка сопоставить по timestamp дала расхождение ~309 секунд на реальном примере — ненадёжно |
| experience_memory record | `Experience.id` (personality-домена) | `registry/experiences/*.json` | да | не связан с episode_id/trace_id |
| source cluster (within-request) | `source_clustering.py` | не персистится cross-request | **НЕТ** | не связан с `orch_reputation.py`'s node identity (разные пространства) |
| node identity (P2P) | `orch_reputation.py` | `registry/reputation/*.db` | да | НЕ путается с source identity (разные концепции, подтверждено) |

**Параллельных/конкурирующих identity-систем на одну и ту же концепцию НЕ
найдено** (хорошая новость) — но обнаружен **разрыв identity** между Trace и
Episode/Experience, что и есть конкретное место разрыва причинной
трассируемости (см. §15).

================================================================================
7. DUPLICATION MAP
================================================================================

Настоящих дублирований логики (два production-владельца одной концепции)
**НЕ найдено**. Найдены naming collisions (одно и то же имя/похожее имя —
разные, несвязанные домены) — не менее опасны для будущего Stage I, если
кто-то по названию решит "продолжить" не тот файл:

| Имя/концепция | Что реально есть | Вердикт |
|---|---|---|
| "Reflection" (4-way) | `reflection_loop.py` (ACTIVE, канонический) vs `reflection_engine.py` (DEAD, 0 importers) vs `reflector.py` (DORMANT, только через daemon.py) vs `self_reflection_analyzer.py` (ACTIVE, но personality-домен) | naming collision, не дублирование |
| "Policy" | `reflection_loop.py`'s поведенческие policy vs `agent/policy.py` (Secret Scanner/Command Audit — безопасность, не self-learning) | naming collision |
| "Trust" | `canonical_trust.py`/`trust_gate.py` (epistemic, ACTIVE) vs `trust_model.py` (relationship trust юзер↔YANDI, 0 importers внутри agent/) | naming collision |
| "Validator" (4-way) | `claim_validator.py` (claim garbage-filter) vs `orch_validator.py` (parallel N-node pseudo-independent answer validator) vs `orch_ai_validator.py` (DORMANT, superseded) vs `ai_validator_redis.py` (ACTIVE, DeepSeek via Redis) | naming collision, каждый делает разное |
| "Hypothesis" | `hypothesis_builder.py`/`hypothesis_graph.py` (claim/evidence синтез-домен, ACTIVE) vs будущий roadmap `PolicyHypothesis` | naming collision — Stage I ДОЛЖЕН выбрать другое имя |
| "Experience"/"Knowledge Graph" | `experience_memory.py` (personality-домен, banter/success) vs эпистемический claim-family граф; `knowledge_graph.py` (personality-слой, используется reflector.py/decision_tracker.py) vs epistemic `family_dependency_graph.py` | naming collision — два "knowledge graph" под разными именами в разных доменах |
| "finetune"/"dataset pipeline" | `finetune.py`+`dataset_pipeline.py` (character/council-chat корпус, qwen3-0.6b) vs `orch_finetune.py`+`orch_dataset.py` (orchestrator/executor, epistemic traces) | два параллельных, по-разному названных пайплайна — не дублирование, но легко спутать |
| `claim_semantic_identity_*` (4 файла) | `prototype.py`+`hardening.py` — ЖИВАЯ production-пара; `corpus.py`/`corpus_hard.py` — offline eval-фикстуры, 0 production importers | НЕ дублирование — корректно разделены |
| `orchestrator/epistemic/final_coverage.py` vs `final_claim_coverage.py` | thin-wrapper extraction, задокументировано явно | НЕ дублирование |

================================================================================
8. DEAD/DORMANT SYSTEMS
================================================================================

**DEAD (0 importers, подтверждено):**
`agent/analysis_engine.py`, `agent/consensus_engine.py` (Stage III P2P-голосование,
корректно вне скоупа), `agent/evidence_kind.py` (и вдобавок СЛОМАН — ссылается
на `ClaimType` без импорта, вызовет `NameError`), `agent/orch_context_builder.py`,
`agent/orch_slot_filler.py`, `agent/trace_diff.py`, `agent/reflection_engine.py`,
`agent/orchestrator.py` (старый "единый оркестратор", живёт только через
`daemon.py`, который сам не запускается — см. ниже).

**Пустой stub-пакет:** `agent/orchestrator/registry/` — директория без единого
файла, даже без `__init__.py`. Не предполагать, что это готовое место для
будущего policy/experience registry — сегодня туда никто не пишет и не читает.

**DORMANT (недостижимо в live pipeline, но не 100% мёртво):**
`agent/daemon.py` — не запускается ни из `start.sh`/`start_headless.sh`, ни из
crontab (пусто), ни из systemd; зависит от несуществующего `reader/config.yaml`.
Через него потенциально достижимы: `agent/orchestrator.py`, `reflector.py`,
`decision_tracker.py`, `failure_collector.py` — статус этих файлов: **UNKNOWN**,
не DEAD (оператор может запускать вручную) — но не влияют на реальный live-трафик.

`agent/orch_ai_validator.py` — 0 importers, вероятно вытеснен `ai_validator_redis.py`.
`agent/trust_model.py` — 0 importers внутри agent/ (не проверено вне скоупа `pet/`).
`agent/claim_graph.py`'s module singleton `_claim_graph` — инстанцирован в
`orchestrator_v2.py:154`, никогда не читается после (живая теневая обсервация
`claim_graph_shadow.py` намеренно строит свежий граф вместо использования синглтона).
`registry/traces/*.json` + `registry/traces/{cat}.db` — legacy/dead (см. §5).

**Дормант/CLI-only (не мертво, не в live-пути):** `trace_dashboard.py`,
`trace_evaluator.py`, `trace_inspector.py`, `trace_regression.py`,
`orch_synth_dataset.py`, `orch_trace_generator.py`, `dashboard.py`,
`council_analyzer.py`, `council_questioner.py`, `council_watcher.py`,
`curiosity.py`, `dialogue_generator.py`, `forgiveness_model.py`,
`inner_monologue.py`, `intent_classifier.py`, `model_runner.py`,
`orch_citations.py`, `orch_entity.py`, `orch_federation.py`, `orch_ledger.py`,
`orch_tool_agent.py`, `relationship_model.py`, `social_reasoning.py`.
`orch_dataset_runner.py` — реальный, регулярно используемый CLI (регенерация
датасета), не мёртв, просто вне HTTP-пути.

================================================================================
9. SIDE EFFECT MAP
================================================================================

`run_standard_pipeline` (`agent/orchestrator/pipeline.py:149`, 1005 строк) —
явно НЕ pure: cache read/write, disk-backed registry search, network web-search,
LLM synthesis вызовы, и (через `writeback.py`) belief/trace/dataset/experience
persistence writes — всё в одной большой функции с явными стадийными
маркерами (`[0] Cache check`, `[1] Risk assess`, `[3] Intent`, `[3.5] Epistemic
classification`, `[4] Clarification`, ...).

Критично для будущего self-learning: **`reflection.reflect_on_query()` — это
НЕ диагностическая, "read-only" функция.** Она имеет production side effect
внутри самой себя — немедленно вызывает `_apply_policy_to_planner()`, который
пишет на диск и мутирует состояние, которое planner прочитает уже на
следующем запросе (см. §12). Если Stage I планирует ввести отдельный,
by-design диагностический Reflection-объект — он должен архитектурно НЕ
иметь такого side effect, либо side effect должен быть явно вынесен за
shadow-границу.

`agent/orch_validator.py` — параллельно опрашивает "3 ноды", которые по
собственному комментарию в коде — локальный Ollama с разными seed
("псевдо-независимость", MVP). Это признанный, не гипотетический риск —
см. §16.

================================================================================
10. PERFORMANCE MAP
================================================================================

Основной "метрики"-стор `registry/orch_metrics.jsonl` **ненадёжен**: буферизуется
в памяти (`orch_monitoring.py`, flush каждые 10 событий), без flush-on-shutdown/
atexit — файл не обновлялся с 2026-07-15 несмотря на активную продакшн-нагрузку
до 27 августа. Причина доказана кодом, не предположением: частые рестарты
процесса теряют буфер молча.

Надёжные данные о latency ЕСТЬ — per-request, в `registry/dataset/orch_traces/*.jsonl`
и `registry/traces/*.json`, поле `execution[]`/`cost{}` с реальным `duration_ms`
по стадиям. Один прочитанный пример (`trace_..._98816675.json`, запрос "Что
такое ИИ"): `total_ms=27470`, из них `synthesize_ms=12666` (46%, LLM-вызов) и
`intent_ms=6628` (24%) — топ latency contributors, а НЕ retrieval, вопреки
интуитивному предположению. Выборка = 1 трасса, не статистически надёжна;
механизм агрегации по всем `orch_traces` уже есть (поле `cost`), нужен только
скрипт-агрегатор — не создавался в рамках этого аудита (не production-changing).

================================================================================
11. EXPERIENCE CURRENT STATE
================================================================================

`agent/experience_memory.py::ExperienceMemory` — ЖИВАЯ, пишется каждый запрос
(`writeback.py`), читается planner'ом (`get_relevant_lessons`). НО: схема
(`speech_act, topic, query, response, user_reaction, success, used_count`)
спроектирована personality/banter-first, и лишь "довешена" эпистемическим
контентом (mistakes/lessons/policy_changes реально туда попадают). Нет
`episode_id`/`trace_id` — нельзя чисто сослаться на "какая именно трасса
породила этот опыт". Это ровно roadmap I-1 (ExperienceRecord), но в сыром,
не типизированном виде и без ссылочной идентичности (roadmap явно требует
"ссылаться на trace/episode, а не дублировать").

================================================================================
12. REFLECTION CURRENT STATE
================================================================================

`agent/reflection_loop.py::ReflectionLoop.reflect_on_query()` (вызывается
`writeback.py:459` на каждый запрос):
`_identify_mistakes()` → `_extract_lessons()` (List[str], без structured
observation/suspected_cause/proposed_action/scope/confidence/falsification_condition
из roadmap I-6) → `_suggest_policy_change()` → **немедленно**
`_apply_policy_to_planner()` (`reflection_loop.py:99-123`, подтверждено
чтением кода лично ведущим агентом):
- Если правило (`rule`) с таким же текстом уже есть — `confidence += 0.1`
  (максимум 1.0) **при каждом повторении паттерна ошибки**, без всякой
  проверки delayed outcome.
- Если новое — добавляется с `confidence=0.7` по умолчанию.
- В любом случае — немедленная запись в `registry/reflection_policies.json`.

Отдельно существует **другой**, структурно более похожий на будущую
Structured Reflection механизм — `trace.learning[]` (типизированные объекты
`{type, rule, confidence}`, пишутся `trust_gate.py::add_learning_rule` в
момент canonical trust вычисления). Лично проверено: **это write-only** —
ни `orch_planner.py`, ни `pipeline.py`, ни `pre_pipeline.py` не импортируют
ничего, что читало бы `trace.learning[]` из прошлых трасс перед планированием
нового запроса (только CLI-инструменты `trace_metrics.py`/`trace_evaluator.py`/
`trace_inspector.py`, ни один из которых не вызывается из live pipeline).

Итог: в системе одновременно есть (a) незащищённый, но ЗАМКНУТЫЙ цикл
(reflection_loop→policy→planner) и (b) структурно готовый, но НЕЗАМКНУТЫЙ
источник structured-reflection-подобных данных (`trace.learning[]`). Это
меняет формулировку задачи Stage I: не "построить Reflection с нуля", а
"типизировать/гейтировать существующий (a) и, отдельно, решить — замкнуть
ли (b) или деприоритизировать".

================================================================================
13. PLANNER CURRENT STATE
================================================================================

`agent/orch_planner.py::_get_reflection_policies()` (строка ~136) читает
`reflection_loop.get_policies()` (сырой `active_policies` список) **и
отдельно** `experience_memory.get_relevant_lessons(query, limit=2)`, конвертируя
текст урока в ad-hoc policy через substring-мэтчинг русских фраз (например,
`"не прошли валидацию" in lesson_text` → добавить policy "Запретить web-поиск
для interpretive/non_falsifiable вопросов"). Это ХРУПКИЙ механизм (текстовое
сопоставление, не структурные признаки), но он ЖИВОЙ: `_apply_reflection_policies()`
и `_should_skip_internet()` реально мутируют план — шаги, решение об
использовании веб-поиска — на новом запросе.

Никакого `StrategyIdentity`/`StrategyReliability` (roadmap I-17/I-18) не
существует: `strategy_router.py` выбирает стратегию per-request, но нигде не
находится persisted per-strategy статистика исходов.

================================================================================
14. DATASET READINESS
================================================================================

Объём реального сигнала: 434 богатых production-трасс (orch_traces) + 152
lesson-дайджеста (episodes) за ~4 недели прерывистых live/verification
прогонов. Схема orch_traces покрывает большую часть Stage II training-signal
wishlist (query, частично context через `query_trace`/`goal`, evidence,
claims, trust до/после через `trust`+`confidence_evolution`, `reasoning`/
`learning`). ОТСУТСТВУЮТ/не подтверждены: явный `delayed_outcome`, явная
`strategy_used` identity, явная пара original-vs-corrected-answer,
`contradiction` как отдельное типизированное поле верхнего уровня.

**Единственный конвертер trace→SFT (`orch_dataset.py::_filter`, строки 74-83)
доказанно сломан на 100% реальных данных**, лично подтверждено чтением кода:
```python
if t.get("outcome") != "success":      # outcome — ВСЕГДА dict, никогда строка "success"
    continue
if t.get("quality", 0) < QUALITY_THRESHOLD:  # поля "quality" НЕТ в реальной схеме → всегда 0 < 0.7
    continue
```
Ноль записей из 434 когда-либо проходили этот фильтр. Ошибка не бросается —
пайплайн молча производит пустой/почти пустой SFT-датасет каждый раз, когда
запускается.

Train/val/test split-дисциплина (roadmap II-9/II-10, semantic-family-aware /
temporal holdout) — не существует нигде в проверенном скоупе; единственный
существующий сплит (`finetune.py`, случайный `random.shuffle`) относится к
ДРУГОМУ (character-chat) корпусу и нарушал бы оба принципа, если бы был
переиспользован для эпистемики.

**Вердикт: NOT READY для Stage II fine-tuning как есть.** Диагностика по
TOR §22 — только диагностика, реализация Stage II не входила в скоуп.

================================================================================
15. CAUSAL TRACEABILITY
================================================================================

**Работает:** Claim → Belief (через `belief.claim_ids`, 598/599 непустых) →
Evidence → Belief (через `evidence_for`/`evidence_against`) → "почему belief
изменился" (полностью — `BeliefManager` пишет структурированный `history[]`:
timestamp/old_confidence/new_confidence/reason/change при каждой мутации;
это УЖЕ ровно та форма, которую roadmap просит для Phase I-3/I-28).

**Рвётся, конкретно, в двух местах (обе доказаны, не гипотеза):**

1. **Episode ↔ Trace identity gap.** `agent/dataset/episodes_*.jsonl` не
   содержит ни `episode_id`, ни `trace_id` — никакой общий ключ с
   `registry/dataset/orch_traces/*.jsonl`. Попытка сопоставить по timestamp
   на реальном примере ("Сколько спутников у Сатурна?", 2026-08-27) дала
   расхождение ~309 секунд между записью в episodes и предполагаемой той же
   записью в orch_traces — timestamp-proximity ненадёжен как join key.
   Значит: сегодня нельзя программно перейти от "урока" (episodes.lessons)
   к полной трассе (claims/evidence/trust), которая его породила, иначе как
   вручную/эвристически.

2. **Canonical vs pre-canonical Trust divergence.** Лично подтверждено
   чтением `writeback.py:395-638`: `archive_query()` (строка 402),
   `OutcomeRecord`/`trace.set_outcome()` (строка 419-430, персистится ВНУТРЬ
   самого Trace-объекта как `trace.outcome.trust_label`), `self_model`,
   `memory`, `reflection.reflect_on_query()` — все читают
   `synthesis_result.trust_level` **ДО** canonical cutover'а на строке
   626-638. После cutover'а `trace.trust` = canonical, но
   `trace.outcome.trust_label` внутри ТОГО ЖЕ объекта — никогда не
   обновляется задним числом (проверено: нет кода, который трогал бы
   `trace.outcome` после `set_outcome()`). Восстановленный из git-истории
   удалённый отчёт (`git show 7637887:YANDI_EPISTEMIC_TRUST_CONSOLIDATION_REPORT.md`,
   раздел "9. Legacy paths remaining") подтверждает: это ИЗВЕСТНОЕ,
   намеренно отложенное ограничение прошлой фазы, не новый баг — но оно
   ранее не было решено, и любой будущий ExperienceRecord/OutcomeModel,
   построенный наивно на `dataset_builder.record_episode()` или
   `trace.outcome.trust_label`, систематически унаследует завышенный
   (pre-min) Trust-сигнал. Корректный источник — `trace.trust`/
   `trace.add_observation("canonical_trust", ...)`.

================================================================================
16. SELF-CONFIRMATION RISKS
================================================================================

**Подтверждённый, реальный (не гипотетический) риск:**
`_apply_policy_to_planner()` (см. §12) — confidence policy растёт
исключительно от частоты повторения того же паттерна ошибки, БЕЗ какой-либо
независимой проверки delayed outcome (помогло ли применение policy на самом
деле). Это ровно форма, которую roadmap §19 называет "Reflection предлагает
изменение → Planner применяет → Reflection оценивает собственное изменение →
policy усиливается" — только даже без явной "оценки", просто механическим
счётчиком повторений. **Это главный P0.**

**Подтверждённый, но пока изолированный риск:**
`agent/orch_validator.py` — параллельный опрос "3 нод", являющихся, по
собственному комментарию файла, одной и той же локальной Ollama-моделью с
разными seed ("псевдо-независимость", MVP). Проверено: `source_clustering.py`/
`source_independence_prototype.py` кластеризуют web-источники по URL, НЕ
validator-ноды — то есть сегодня это не смешивается со счётом независимых
evidence. Но любой будущий TrustCalibration/SourceReputation/Validator-
reliability механизм ДОЛЖЕН явно исключать/маркировать эти псевдо-независимые
ноды, иначе согласие "3 из 3" будет ошибочно засчитано как независимое
подтверждение.

**Не найдено:** самоцитирования собственного синтезированного ответа как
independent evidence; `consensus_engine.py` мёртв (не может создать живой
цикл); `disagreement_engine.py` спроектирован ИЗМЕНЯТЬ позиции
(confidence_after < confidence_before в выборке), а не подтверждать их —
хороший пример НЕ self-confirming механизма.

`archive_query()`/`KnowledgeDB.save_knowledge` реализует one-way ratchet
(`VERIFIED` запись не может быть тихо понижена не-`VERIFIED` записью) — само
по себе разумно, но не проверено до конца, откуда берётся `trust_level='VERIFIED'`
выше по стеку вызова (canonical Trust или локальное optimistic-значение) —
**NEEDS VERIFICATION**, не блокирующая находка.

================================================================================
17. STAGE-I REUSE MATRIX
================================================================================

| Будущий компонент | Вердикт | Почему |
|---|---|---|
| ExperienceRecord | **EXTEND** `experience_memory.py` (или напрямую `orch_traces` как anchor) — НЕ строить с нуля. Добавить `episode_id`/`trace_id` как общий ключ. | Уже живой, уже пишется каждый запрос, схема требует расширения, не замены. |
| Outcome Model (immediate) | **REUSE** `OutcomeRecord` (`orch_schemas.py`), но исправить источник trust на `trace.trust`, не pre-canonical | структура уже есть, источник данных — нет |
| Outcome Model (delayed) | **NEW COMPONENT** | ничего похожего не найдено нигде |
| Failure/Success Taxonomy | **ADAPT**, не изобретать заново — `learning[].type` в orch_traces (`coverage`, `planner`, `evidence`, `belief`, `linker`, `epistemic_skepticism`) и текстовые `mistakes`/`lessons` в episodes уже дают эмбриональную типизацию | реальные типизированные категории уже эмитятся на каждой трассе |
| Structured Reflection (I-6) | **NEW SCHEMA, но REUSE call site** `reflect_on_query()` — не создавать второй reflection-вызов | входные данные уже там, выходная форма — List[str], а не структурный объект |
| PolicyHypothesis / Lifecycle / Shadow / Promotion-Rollback | **DO NOT EXTEND существующий `_apply_policy_to_planner` как есть — это то, что нужно заменить/гейтировать**, но интеграционную точку (`orch_planner._get_reflection_policies`/`_apply_reflection_policies`) можно переиспользовать как финальный "apply ACTIVE policy" шаг ПОСЛЕ shadow/experiment | текущий механизм нарушает shadow-first, self-reinforcement без delayed-outcome check |
| AdaptivePlanner / StrategyIdentity / StrategyReliability | **NEW COMPONENT** | ничего похожего не найдено — ни fingerprinting, ни per-strategy outcome-статистики |
| SourceReputation 2.0 | **NEW COMPONENT** (не путать с `orch_reputation.py` — другое identity-пространство, P2P node, не web source) | подтверждено: cross-request web-source identity отсутствует |
| Source Reputation Decay pattern | **ADAPT** паттерн из `BeliefManager._apply_decay` (проверено живьём: 0.7→0.057 за ~40 дней) | уже решённая проблема на соседнем слое |
| Trust Calibration | **REUSE** уже логируемые `canonical_trust_diverged`/`canonical_trust_stricter_strand` observations per-trace как готовый сигнал "как часто расходятся два strand'а Trust" | данные уже есть, нужна только корреляция с delayed outcome |
| MetacognitiveProfile | **PARTIAL REUSE** — `self_model.py`'s счётчики (`increment_queries`, `increment_errors`, `increment_reflections`) как substrate; `orch_reputation.py`'s domain-scoped scoring — архитектурный прецедент, не готовый компонент | нужна отдельная глубокая проверка `self_model.py`, вне скоупа этого прохода |
| Policy Dependency Graph (I-11) | **DO NOT BUILD пока не доказана необходимость** (сам roadmap требует это) — `family_dependency_graph.py` хороший структурный референс, не объект для прямого расширения | другой домен объекта |
| Model-training harness (Stage II, позже) | **EXTEND** `orch_finetune.py`+`orch_dataset.py` пару (после починки фильтра), НЕ `finetune.py` (не тот корпус, нет promotion gate, нарушает II-20) | два существующих пайплайна, только один целится в правильный корпус |

================================================================================
18. P0 BLOCKERS
================================================================================

**P0-1 — Незащищённый, уже живой self-reinforcing policy loop.**
`agent/reflection_loop.py:99-123` (`_apply_policy_to_planner`) немедленно
применяет и усиливает поведенческую policy на основе одной лишь частоты
повторения паттерна ошибки, без shadow-режима, без эксперимента, без
delayed-outcome проверки, без promotion/rollback. Прямо нарушает roadmap
инварианты 1.12, 1.13, §10, §40. **Действие до начала Stage I production
work**: явно решить — гейтировать этот механизм (флаг, за которым можно
временно отключить unconditional apply) или обернуть его в предлагаемый
Stage I PolicyHypothesis/Shadow слой ДО того, как что-либо новое начнёт
опираться на `registry/reflection_policies.json` как на "рабочий" сигнал.

**P0-2 — Canonical vs pre-canonical Trust divergence в данных, на которых
будет строиться ExperienceRecord/OutcomeModel.**
`agent/dataset/episodes_*.jsonl` и `trace.outcome.trust_label` систематически
несут pre-canonical (более рыхлый) Trust; canonical живёт только в
`trace.trust`/`trace.add_observation("canonical_trust", ...)`. Известное,
ранее задокументированное (и удалённое из репозитория, но восстановленное
из git-истории) ограничение. **Действие**: любой Stage I код, строящий
Experience/Outcome, обязан явно читать `trace.trust`, никогда
`trace.outcome.trust_label` или dataset episode `"trust"` поле напрямую —
зафиксировать это как жёсткое архитектурное правило до первой строчки кода.

Других P0 (два production canonical owners одной концепции, невозможность
связать outcome с episode, потеря causal references, self-confirmation
cycle уже в проде за пределами P0-1) — **не найдено**.

================================================================================
19. P1 — SHOULD FIX BEFORE PRODUCTION ADAPTATION
================================================================================

1. `core_loop.state.is_running` (`agent/core_loop.py:277-278`) никогда не
   сбрасывается → `core_loop.run_cycle()` с реальными per-query данными
   срабатывает максимум один раз за жизнь процесса (`writeback.py:586`).
   Любой Stage I дизайн, предполагающий, что CoreLoop's 7-шаговый цикл живёт
   per-request — работает на ложной предпосылке.
2. `episodes_*.jsonl` ↔ `orch_traces/*.jsonl` не имеют общего identity-ключа
   (см. §15) — блокирует чистую ссылочность будущего `ExperienceRecord.episode_id`.
3. `orch_dataset.py`'s SFT-фильтр сломан на 100% реальных данных (§14) —
   нельзя предполагать "у нас уже есть рабочий trace→dataset pipeline".
4. Два параллельных, конфликтующих по имени finetune-пайплайна
   (`finetune.py` vs `orch_finetune.py`+`orch_dataset.py`) — нужно явно
   зафиксировать канонический перед любой Stage II работой; будущий
   `self_learning` package не должен добавить третий.
5. `registry/orch_metrics.jsonl` теряет данные при каждом рестарте процесса
   (нет flush-on-shutdown) — не использовать как источник для будущей
   StrategyReliability/latency-политики без починки.
6. `orch_validator.py`'s псевдо-независимые "3 ноды" — см. §16, зафиксировать
   как известное ограничение для будущего Trust Calibration/Source Reputation.
7. Naming collisions (§7) — обязательное требование для Stage I: не называть
   новые классы `Reflection`/`Policy`/`Trust`/`Experience`/`Hypothesis` без
   явно отличимого префикса/модуля.
8. `agent/evidence_kind.py` — мёртв И сломан (NameError при импорте) —
   не давать будущему компоненту source-классификации "найти" и
   переиспользовать этот файл, думая что он рабочий.
9. Отсутствие cross-request web-source identity/reputation (только P2P node
   reputation существует, другое пространство) — SourceReputation2.0 будет
   полностью новым компонентом, зафиксировать это явно, чтобы не тратить
   время на попытку "найти" существующий owner.

================================================================================
20. P2/P3 DEBT
================================================================================

**P2 (technical debt, не блокирует, не трогать сейчас):**
- Неограниченный рост монолитных JSON-стораджей (beliefs/disagreements/
  episodic_memory/transport_memory) без retention/archival.
- `registry/traces/{cat}.db` (KnowledgeDB.save_trace путь) — мёртвый груз от
  одноразовой миграции; `orchestrator_v2.py`'s individual trace-json путь —
  legacy/dormant.
- `agent/orchestrator.py`+`agent/daemon.py` — недостижимы в проде, зависимость
  на несуществующий `reader/config.yaml`.
- Шесть подтверждённых мёртвых файлов + один пустой stub-пакет (§8) —
  безопасно игнорировать, не строить на них.
- `agent/claim_graph.py` module-level singleton — инстанцирован, не читается,
  без side effect (harmless), но не считать его "тем самым claim graph".
- `agent/DesignSync`... (нерелевантно, вычеркнуто вручную) — н/п.

**P3 (cosmetic):**
- `technical_errors` поле в episodes.jsonl — всегда пусто на 152/152 строках.
- `pipeline.py:104-113` — осиротевшая ссылка на удалённый
  `YANDI_FULL_PIPELINE_AUDIT.md`, описывающая изменение, самоотмеченное как
  "не прошло live A/B валидацию" — отдельный тикет вне скоупа этого аудита.
- `orch_ai_validator.py` — похоже, полностью вытеснен `ai_validator_redis.py`.

================================================================================
21. SHOULD agent/self_learning/ EXIST?
================================================================================

**Вариант C — HYBRID.**

Не строить новый пакет как единственный source of truth "со всем внутри".
Большая часть требуемых Stage I концепций уже имеет canonical owner
(Claims/Evidence/Belief/Dependency — Fork C; трассы — `orch_tracer`/orch_traces
— Fork B) или частично-живую, но неструктурированную реализацию
(Reflection/Policy/Experience — Fork A). Новый пакет нужен ТОЛЬКО для
responsibilities, у которых СЕГОДНЯ нет owner вообще:

- Типизированный `PolicyHypothesis` объект + status-машина
  (OBSERVED→HYPOTHESIS→TRIAL→SUPPORTED→ACTIVE→DEGRADED→REJECTED→RETIRED) —
  ничего похожего не существует.
- Shadow-режим для policy-решений (production decision vs shadow candidate
  decision, без применения) — не существует.
- Experiment engine (controlled comparison, bounded traffic) — не существует.
- Strategy identity + reliability statistics — не существует.
- Delayed Outcome / OUTCOME_REVISION механизм — не существует.

Всё остальное (persistence trace-данных, claim/belief identity, Trust,
Experience-запись как таковая) должно EXTEND существующие owners, не
дублировать их.

================================================================================
22. MINIMAL PROPOSED STAGE-I ARCHITECTURE
================================================================================

Это ОРИЕНТИР для следующего отдельного ТЗ, не разрешение реализовывать
сейчас (см. §23/§33 брифа).

```
agent/self_learning/
    policy_hypothesis.py   # типизированный объект + status-машина.
                            # ИМЯ НАМЕРЕННО не "hypothesis.py" (коллизия
                            # с hypothesis_graph.py/hypothesis_builder.py)
    shadow.py               # shadow-decision comparison, ничего не применяет
    experiment.py            # controlled trial, bounded traffic, rollback
```

Плюс МИНИМАЛЬНЫЕ, точечные изменения (не рефакторинг) в существующих файлах:
1. `agent/orch_tracer.py`/`dataset_builder` (writer, найти конкретно) —
   добавить общий `episode_id`, чтобы episodes и orch_traces были joinable.
2. Явное правило/линт (не обязательно код): "Experience/Outcome читают
   `trace.trust`, не `trace.outcome.trust_label` и не dataset episode `trust`".
3. `agent/reflection_loop.py::_apply_policy_to_planner` — гейтировать
   (флаг shadow-only / отключение unconditional apply) ДО того, как поверх
   него начнёт строиться PolicyHypothesis/Promotion.
4. НЕ трогать Claims/Evidence/Belief/Dependency/Trust-ядро — оно уже
   соответствует roadmap-требованиям для фундамента (§4, §6, §15).

================================================================================
23. WHAT MUST NOT BE BUILT
================================================================================

- НЕ строить новый Experience store с нуля — расширить существующий
  (`experience_memory.py` + `orch_traces`).
- НЕ строить новый Reflection call site — расширить `reflect_on_query()`.
- НЕ называть новые классы `Reflection`/`Policy`/`Trust`/`Hypothesis`/
  `Experience`/`KnowledgeGraph` без явно отличимого имени (см. §7 naming
  collisions).
- НЕ расширять `_apply_policy_to_planner` как есть — это то, что нужно
  заменить/гейтировать, не фундамент.
- НЕ строить SourceReputation2.0 поверх `orch_reputation.py` (P2P node
  identity, другое пространство).
- НЕ предполагать, что `orch_dataset.py`/`orch_metrics.jsonl` уже работают —
  оба доказанно сломаны/ненадёжны.
- НЕ трогать Claims/Evidence/Belief/Dependency/Trust-ядро ради "улучшений" —
  оно не блокирует Stage I.
- НЕ запускать Stage II (fine-tuning) — датасет не готов (§14).
- НЕ автоматизировать promotion/rollback без явного отдельного ТЗ на
  Experiment Engine.

================================================================================
24. RECOMMENDED IMPLEMENTATION ORDER
================================================================================

1. **Gate existing reflection→policy loop** (P0-1) — минимальное изменение,
   делающее текущий self-reinforcement либо shadow-only, либо явно помеченным
   legacy с ограниченным confidence cap и логированием для наблюдения.
2. **Fix episode↔trace identity gap** (P1) — общий id, без которого
   ExperienceRecord не может ссылаться на trace честно.
3. **Document/enforce canonical-trust-only rule** (P0-2) — до первого байта
   кода, который читает Trust из dataset/outcome.
4. Только после 1-3: типизированный `PolicyHypothesis` + `shadow.py` (без
   применения к production).
5. `experiment.py` (controlled trial) — только после того, как shadow
   накопит достаточно наблюдений для сравнения.
6. Strategy identity/reliability — параллельно или после 4-5, независимая
   ветка.
7. Всё остальное (SourceReputation2.0, TrustCalibration, MetacognitiveProfile,
   GapDetection) — по приоритетам roadmap §22 (Priority B), после
   доказанного цикла 1-я итерация Policy Hypothesis → Shadow → Experiment →
   Promotion на реальных данных.

================================================================================
25. STOP/GO RECOMMENDATION
================================================================================

**CONDITIONAL GO.**

Не STOP: фундамент (epistemic core, Phases 0-14) в хорошем состоянии,
подтверждён множественными owner'ами без конкурирующих истин, baseline
regression зелёный (29/29), рабочее дерево чистое. Нет находок, которые
делали бы архитектуру принципиально небезопасной для продолжения.

Не безусловный GO: два P0 (§18) должны быть явно признаны и адресованы ДО
или В РАМКАХ первого production-changing шага Stage I — не потому что они
криминальны сегодня (система работает), а потому что именно они определяют,
корректно ли будущий self-learning код будет "видеть" систему. Строить
PolicyHypothesis поверх незамеченного self-reinforcing loop или Outcome
Model поверх pre-canonical Trust — значит повторить путь, который сам
roadmap называет самой опасной ошибкой Stage I (§2 брифа: "создать новую
правильную подсистему, дублирующую уже существующую").

Рекомендация пользователю: следующее отдельное ТЗ должно начинаться с §24
пункт 1-3 (gate loop, fix identity, enforce canonical trust) как
самостоятельного, маленького, полностью регресс-тестируемого шага — и
только по его завершении переходить к PolicyHypothesis/Shadow/Experiment.

================================================================================
ПРИЛОЖЕНИЕ: NEEDS VERIFICATION (не блокирует GO, для следующего прохода)
================================================================================

- Конкретная функция-writer `agent/dataset/episodes_*.jsonl` (не найдена ни
  одним из 5 форков в покрытых файлах — вероятно `dataset_builder.py`,
  не читан построчно).
- Персистится ли `FamilyDependencyGraph` на диск, или живёт только in-memory.
- `source_quality.py` — персистит ли что-то cross-request (не подтверждено
  ни одним фор ком).
- Полный список содержимого `registry/dataset/{council_synthesis,embeddings,
  validation_reports}`, `orch_cache/`, `orch_index/` — не прочитаны детально.
- Откуда `archive_query(trust_level=...)` реально берёт значение выше по
  стеку — canonical или локальное optimistic (§16, one-way ratchet вопрос).
- Реальный production-run статус `agent/daemon.py` (запускается ли оператором
  вручную) — не может быть доказан или опровергнут содержимым репозитория.
- `claim_evidence_retriever.py` (2053 строки) и `claim_relation.py` (1205
  строк) — просмотрены поверхностно (импорты/вызовы), не построчно.
