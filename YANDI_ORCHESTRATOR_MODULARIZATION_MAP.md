# YANDI Orchestrator — Structural Audit / Modularization Map

Audit target: `agent/orchestrator_v2.py` (5620 lines total).
Baseline: all 13 known `*_regression_test.py` suites GREEN before this audit (confirmed via
`python3 -m agent.<suite>` from `/home/iam/yandi`, using `/home/iam/venv/bin/python3`).

This document is the living engineering map for the strangler-fig migration into
`agent/orchestrator/`. It is structural only — no behavior has been changed yet.

---

## 0. File-level shape

| Range | Content |
|---|---|
| 1-168 | imports (see §3) |
| 170-186 | module globals (V3/V6 singletons) — duplicated verbatim again at 313-319 (dead duplicate, harmless — both assign `None` at import time) |
| 12-188 (interleaved) | `LocalSynthesisResult` dataclass (~12), small free functions |
| 189-841 | top-level helper functions (pure-ish; see §5) |
| **843-5546** | **`process()` — the monolith function (4703 lines, ~84% of the file)** |
| 5546-5620 | `interactive()` + `if __name__ == "__main__"` CLI entrypoint |

`process()` is one unbroken function body. There is no nested phase-function structure at
all today — every phase below is a *region* inside one Python function's local scope, not a
separate callable. This is the central migration obstacle: extraction requires deciding what
each phase reads/writes from the ~60 local variables `process()` accumulates, since Python
closures over an enclosing function's locals (see §7, nested closures) currently substitute
for an explicit context object.

---

## 1. Global / singleton inventory

| Global | Owner (init point) | Init call | Read by | Written by |
|---|---|---|---|---|
| `_tracer` (module-level `DecisionTracer()`) | module import time, line 171 | n/a (constructed at import) | every early-return path + final return (`_tracer.save_trace(trace)`) | never reassigned after import |
| `_self_model`, `_memory`, `_reflection`, `_motivation`, `_core_loop`, `_v3_initialized` | `_init_v3()` (321-353) | `_init_v3()` called once at top of `process()` (line 851) | `_init_v3()` returns them; `process()` immediately rebinds to **local** vars `self_model, memory, reflection, motivation, core_loop` (no leading underscore) at line 851 — the module globals themselves are not read again directly by `process()` after that | `_init_v3()` only (idempotent — guarded by `_v3_initialized`) |
| `_claim_graph`, `_claim_validator`, `_belief_manager`, `_claim_answer_linker`, `_disagreement_engine`, `_personality_core`, `_v6_initialized` | `_init_v3()` (339-351, same function handles both V3 and V6 despite the name) | same call as above | `_claim_validator`, `_belief_manager`, `_claim_answer_linker`, `_disagreement_engine`, `_personality_core` are read **directly as module globals** (underscore-prefixed) throughout the claim pipeline (§6) — NOT rebound to locals | `_init_v3()` only |
| `_claim_graph` | as above | `get_claim_graph()` (line 341) | **never read again anywhere in the file** — dead reference, but `get_claim_graph()` itself may have side effects (constructing a process-wide singleton other modules also fetch) so the call must still happen once | n/a |

**Gotcha for migration**: `_init_v3()` is called unconditionally on every `process()` invocation
but is internally idempotent (`if not _v3_initialized`). If this moves to `runtime/`, the module
that owns it must preserve exactly this "called every request, real work done once" semantic —
do not change it to e.g. call-once-at-import, since request-time re-check-and-noop is the
existing contract other tests may implicitly rely on.

`registry = get_registry()` (line 859) is a **different** singleton — the tool-performance
registry from `orch_tool_registry.py` (latency/reliability tracking per pipeline step), not the
knowledge registry. Written via `registry.update_latency(...)` / `registry.update_reliability(...)`
at: cache_check(1470), risk_assess(1565), plan(1605), intent(1638/1639), enrich(1946),
local_search(2117), web_query(2174), web_scrape(2187), synthesize(2610/2611). Do not confuse this
with the knowledge-registry writes inside `_background_validate()` (`write_from_arbiter`, a
background-thread-only path, separate from the main claim pipeline entirely).

`cache = get_cache()` (line 1462) — orch_cache singleton, read/written only in the `[0] Cache
check` phase and once more at `[10]` (`cache.put_from_synthesis(...)`, line 5172).

---

## 2. Log markers to preserve verbatim (bracket-tag inventory)

Counted via `grep -oP '\[[A-Z][A-Za-z0-9 ↔:_/-]{2,40}\]'`. Highest-frequency first; full list
(80 distinct tags) captured below. These strings are read by existing external diagnostic
tooling per the project's own instructions (§17 of the modularization brief) — **do not rename
during structural moves**.

```
8  [Strategy]                    2  [Claim↔Claim Prefilter]
7  [Graph]                       2  [Belief Update Timing]
7  [Claim Status Gate]           1  [YANDI] [Web Query] [Validation] [URL] [Target]
6  [Refutation]                  1  [SongAnalyzer] [SocialAnalyzer] [SelfReflectionAnalyzer]
6  [Boundary]                    1  [Search Work Audit] [RAG] [PROFILE BOTTLENECK]
6  [Blind]                       1  [Plan SubProfile] [None] [Grounding] [Frame]
5  [Pass2 Trace]                 1  [Final Claim Leakage] [Existence Contract]
5  [Local]                       1  [Evidence Pool] [Evidence Mapper] [Evidence Eligibility]
5  [Intent]                      1  [DEBUG] [Claim Trace] [Claim Support Decision]
4  [PROFILE]                     1  [Claim Retrieval Timing] [Claim Resolution Gate]
4  [Claim Validator]             1  [Claim Pipeline Boundary] [Claim Evidence NLI Pass 2]
3  [Scene] [Refutation DEBUG]    1  [Claim Evidence NLI] [Claim↔Claim NLI]
3  [Final Claim Coverage]        1  [Claim↔Claim Batch Summary] [Claim↔Claim Batch] [Belief]
3  [Entity] [Character]
2  [Swear] [Self-Query] [Early Gate] [Criticism] [Context] [Claim Status] [Claims]
2  [Claim Retrieval Pass 2] [Claim↔Claim Timing]
```

The `[PROFILE]` block (5234-5294) is the single most tool-load-bearing region: it drives
`[PROFILE]`, `[PROFILE BOTTLENECK]`, and `[Search Work Audit]` output used by the just-completed
performance work (`refutation_performance_regression_test.py` and friends read/compare this
shape). Any extraction touching it needs an equivalence check against real `[verbose]` output,
not just return-value diffing.

---

## 3. External dependency map (already-modular domain code — DO NOT DUPLICATE)

Every one of these is imported at module top (lines 34-168) and must remain the single source of
truth. The new `agent/orchestrator/` package wraps/orchestrates these; it must never re-implement
their logic.

| Module | Symbols used | Phase(s) |
|---|---|---|
| `orch_schemas` | `OrchestratorRequest/Response`, `EnrichedQuery`, `SearchResult`, `WebQueryResult`, `EvidenceRecord`, `IntentResult`, `OutcomeRecord`, `QueryTrace`, etc. | everywhere (shared dataclasses) |
| `orch_cache` | `get_cache` | [0] |
| `orch_risk` | `assess_risk` | [1] |
| `orch_planner` | `build_plan` | [2], plan-rebuild in [3.5] |
| `orch_intent` | `analyze_intent` | [3] |
| `orch_clarifier` | `ClarificationSession` | [4] |
| `orch_enricher` | `enrich_query` | [5] |
| `orch_registry_search` | `search_registry`, `CONF_THRESHOLD` | [6], [7] |
| `orch_web_query` | `formulate_queries`, `formulate_refutation_queries` | [6] fan-out, [7] |
| `orch_web_scraper` | `scrape`, `SharedFetchCache` | [6] fan-out (refutation), [7], claim retrieval pass2 |
| `orch_synthesizer` | `synthesize`, `_call` (used directly by `generate_local_answer`/`blind_analysis` helpers), `EPISTEMIC_WARNING` | SYNTHESIS — **claim extraction itself happens inside `synthesize()`**, not in orchestrator_v2.py (`claims_data = reasoning_info.get("claims", [])`) |
| `orch_optimistic` | `get_responder` | [10] |
| `orch_timeout` | `step_timer` | nearly every phase 0-7 |
| `orch_tool_registry` | `get_registry` | latency/reliability tracking, all phases |
| `orch_session` | `get_context`, `add_message` (imported, **never called**), `new_session_id` | request init, `interactive()` |
| `orch_node_selector` | `select_nodes`, `select_nodes_federated`, `_should_use_federation` | `_background_validate()` only (background thread) |
| `orch_validator` | `validate_parallel` | `_background_validate()` only |
| `orch_arbiter` | `arbitrate` | `_background_validate()` only |
| `orch_knowledge_writer` | `write_from_arbiter` | `_background_validate()` only |
| `orch_monitoring` | `record` (aliased `mon_record`) | scattered (`validate` events in bg thread, `full_request` at end) |
| `orch_tracer` | `DecisionTracer`, `Trace` | trace lifecycle, all phases |
| `orch_reputation` | `add_decision_event`, `get_trace` (imported, **never called**), `get_ledger` (imported, **never called**) | decision-event logging, all phases |
| `orch_query_archive` | `record_query` (aliased `archive_query`) | [10] |
| `orch_tag_tree` | `update_tree` (imported, **never called** directly — `tag_tree_update` symbol unused) | dead import |
| `orch_unanswered` | `record_unanswered` (imported, **never called**), `start_listener_daemon` (imported as `_start_unanswered_listener`, only referenced in a commented-out call at line 355) | dead import |
| `epistemic_router` | `classify_claim`, `get_trust_label_for_epistemic`, `get_response_mode_description`, `get_trust_cap_for_testability`, `get_objectivity_score`, `EPISTEMIC_WARNING` | [3.5], cache-hit path, trust gate |
| `claim_evidence_mapper` | `map_claims_to_evidence`, `get_claim_grounding_score` | claim setup (pass1), claim retrieval (pass2) |
| `claim_evidence_retriever` | `retrieve_for_claims`, `_is_existence_question` | claim retrieval pass2, existence contract |
| `source_quality` | `evaluate_evidence_directness` | claim evidence NLI batch (both passes) |
| `evidence_pool` | `build_canonical_evidence_pool`, `merge_evidence` | synthesis entry, claim retrieval pass2 |
| `final_claim_coverage` | `evaluate_final_claim_coverage` (imported **3× duplicate**, lines 89-91) | FINAL CLAIM COVERAGE |
| `self_model`/`memory_episodic`/`reflection_loop`/`motivation`/`core_loop` (V3) | `get_self_model`, `get_memory`, `get_reflection`, `get_motivation`, `get_core_loop` | `_init_v3()`, [10] memory/reflection write-back |
| `claim_graph`/`claim_validator`/`belief_manager`/`claim_answer_linker`/`disagreement_engine`/`personality_core` (V6) | `get_claim_graph` (dead ref), `get_claim_validator`, `get_belief_manager`, `get_claim_answer_linker`, `get_disagreement_engine`, `get_personality_core` | `_init_v3()`, claim pipeline, [10] |
| `character_engine`, `criticism_detector`, `boundaries`, `context_registry`, `personal_boundary`, `scene_builder` | various `get_*`, `detect_toxicity`, `ToxicityLevel`, `is_apology` (**never called**) | pre-pipeline (§4) |
| `research_engine` (`get_research_engine`), `object_resolver` (`get_object_resolver`) | imported, **never called anywhere in the file** | dead imports |
| `experience_memory` | `get_experience_memory` | [10] |
| `claim_relation` | `classify_sources`, `classify_claim_evidence_batch`, `extract_main_claim`, `is_relevant`, `infer_claim_relation`, `infer_claim_relations_batch` (**`infer_claim_relations_batch` imported, never called directly** — only `infer_claim_relation` used, inside the `_claims_conflict` closure which itself also appears unused — see §9) | source classification, claim evidence NLI batches |
| `dataset_builder` | `get_dataset_builder` | [10] episode write |
| `song_analyzer`/`self_reflection_analyzer`/`social_analyzer` | `get_*` | intent-dependent early-return branches (§4) |
| `intent_router` | `detect_intent`, `should_use_rag` (**never called**), `get_intent_explanation` | [3] pre-pipeline |
| `target_router` | `detect_target`, `get_target_description` | pre-pipeline |
| `entity_resolver` | `get_entity_resolver` | pre-pipeline |
| `strategy_router` | `get_strategy_router`, `SearchStrategy` | pre-pipeline, [7] strategy-switch-on-empty-web-result |
| `decision_journal` | `get_decision_journal` | request init (constructed, referenced later only implicitly — no explicit method call found besides construction) |
| `relationship_gate` | `decide_response`, `apply_gate` | [11] Early Gate |
| `secret_archive` | `get_secret_archive` | request init, used by `apply_gate` |
| `biography_stats` | `get_biography` | request init, cycles/errors/regrets |
| `hypothesis_builder` | `build_hypothesis_graph` | FRAME CONSTRUCTION |
| `ai_validator_redis` | `send_to_deepseek` (local import inside `_start_bg_validation` closure) | [10] background validation kickoff only |

---

## 4. PRE-PIPELINE (lines 843-1451, ends at `cost["pre_pipeline_ms"]`)

No `cost[]` sub-buckets until the very end (1451) — this whole region was, per the code's own
comments, previously **completely unmeasured** (P0/G fix added the wrapping timer only).

| Step | Lines | Purpose | Early return? |
|---|---|---|---|
| URL extraction | 935-945 | `extract_urls`/`clean_query_from_urls` | no |
| Init locals | 946-1012 | `query_frame = {}` (855, inside try at top), all claim/grounding score locals, biography increment | no |
| Character state load | 1013-1039 | `get_character`, `get_criticism_detector`, `get_personal_boundary`, `get_scene_builder`, `get_context_registry`, `get_decision_journal`, `get_secret_archive` | no |
| 1. Scene Builder | 1041-1047 | `scene_builder.build(...)` | no |
| 2. Target Router | 1050-1053 | `detect_target(...)` | no |
| 3. Intent Router | 1056-1060 | `detect_intent(...)` (query-classification intent, distinct from epistemic `[3] Intent analyze` LLM call later) | no |
| 4. Self-Query | 1063-1086 | `is_self_query` + `build_self_answer` | **YES** — `steps_taken=["self_query"]` |
| 5. Entity Resolver | 1089-1101 | `entity_resolver.resolve(...)` | no |
| 6. Strategy Router | 1103-1126 | `strategy_router.select_strategy(...)` | no |
| 6.5 Swear quick-check | 1128-1152 | inline `SwearAnalysis` class if profanity found (bypasses Criticism Detector call) | no |
| 7. Criticism Detector | 1153-1169 | `critic.analyze(...)` — **note: nested inside the `else` of the swear check**, i.e. skipped entirely when a swear word was found | no |
| 8. Personal Boundary | 1172-1276 | `boundary.analyze` + `boundary.get_response_template`; then 4 ordered priority checks | **YES, 4 separate paths**: provocation (1182-1197), insult→blocked (1200-1218), insult→handled (1220-1230), apology (1233-1252), personal_question (1255-1276) |
| 9. Intent-dependent handling | 1279-1365 | song/social/self-reflection analyzer branches, each `_skip_rag = True` first | **YES, 3 paths**: song_analysis, social_analysis, self_reflection (each also has an internal try/except fallback that un-sets `_skip_rag` and falls through on error) |
| 10. Criticism context check | 1368-1389 | `context_registry.has_context_for(...)` | **YES**: no_context |
| — thanks/normal processing | 1391-1405 | `char.process_thanks`/`char.process_normal`, sets `_bad_state_prefix` | no |
| 11. Early Gate | 1408-1450 | `decide_response`/`apply_gate` | **YES**: break / know_but_not_tell |

**Order matters and is currently strictly sequential with short-circuit returns** — 11 distinct
early-return exit points before the "standard pipeline" even starts (plus 2 more inside it, cache
hit and none found in [8]/[9]/[10], for 13 total `return OrchestratorResponse(...)` sites file-wide
at lines: 1077, 1190, 1211, 1223, 1245, 1269, 1302, 1331, 1355, 1382, 1439, 1544, 5535).

CANDIDATE MODULE: `pre_pipeline.py`. MIGRATION RISK: **MEDIUM** — individually each branch is
simple and mostly self-contained (reads `state`/`query_to_use`/a couple of routers, returns early),
but the ordering between them is behaviorally load-bearing (e.g., swear-check gates whether
Criticism Detector even runs) and every branch touches `trace`/`_tracer`/`biography` side effects
that must fire in the same order. Best extracted as one function `run_pre_pipeline(...)` returning
either an early `OrchestratorResponse` or the accumulated pre-pipeline state, not as 11 separate
functions.

---

## 5. Top-level helper functions (lines 189-841) — outside `process()`

| Function | Lines | Purpose | Pure? | Migration risk |
|---|---|---|---|---|
| `extract_urls` | 189-192 | regex URL extraction | pure | LOW |
| `clean_query_from_urls` | 194-196 | regex strip | pure | LOW |
| `resolve_entity` | 199-200 | **stub, always returns `None`** | pure (trivial) | LOW — but note: dead/no-op, preserve as-is, do not "fix" |
| `generate_local_answer` | 203-249 | local LLM call via `orch_synthesizer._call` | impure (network/LLM) | LOW (self-contained, single call site via `parallel_executor.submit`) |
| `blind_analysis` | 252-312 | LLM-judge ranking of candidate answers | impure (LLM) | LOW-MEDIUM (used once, well-bounded) |
| `_init_v3` | 321-353 | idempotent V3/V6 singleton init | impure (globals) | MEDIUM — see §1 gotcha |
| `TRUST_STATES`, `_TRUST_ORDER`, `_DOMAIN_TAG` | 357-404 | static dicts | pure | LOW |
| `_build_tags` | 407-427 | tag derivation | pure | LOW |
| `_calculate_delta_factors` | 430-463 | reputation delta math | pure | LOW |
| `_apply_trust_cap` | 466-472 | trust label capping | pure | **LOW — P0 candidate** |
| `_background_validate` | 475-630 | background-thread node validation + reputation writes | impure (threading, HTTP via `validate_parallel`, decision events, knowledge-registry writes via `write_from_arbiter`) | HIGH if moved carelessly (runs on a spawned thread, order-sensitive `add_decision_event` sequence) but is already a fully self-contained function with explicit params — mechanically movable as-is |
| `load_core_identity`, `load_yandi_manifest` | 637-654 | file reads from `registry/` | impure (disk I/O), simple | LOW |
| `is_self_query` | 656-683 | keyword self-query detection | pure (calls `detect_toxicity`) | LOW |
| `build_self_answer` | 685-716 | manifest-based canned response | pure | LOW |
| `_generate_character_response` | 722-778 | state-driven canned response | pure | LOW |
| `_generate_apology_response` | 781-792 | state-driven canned response | pure | LOW |
| `_adapt_answer_to_style` | 795-820 | text post-processing by tone/verbosity | pure | **LOW — P0 candidate, used twice (cache-hit path + [10])** |
| `_generate_vulgar_response` | 823-840 | canned response | pure | **DEAD CODE — defined, never called anywhere in `process()`. Confirm before moving; do not silently drop, flag to user.** |

CANDIDATE MODULES: `response/assembly.py` (`_adapt_answer_to_style`, `_generate_*_response`,
`build_self_answer`), `epistemic/trust_gate.py` (`_apply_trust_cap`, `_calculate_delta_factors`,
`TRUST_STATES`/`_TRUST_ORDER`), `runtime/shared_work.py` or `discovery.py` (`generate_local_answer`,
`blind_analysis`), `runtime/profiling.py`+`registry/integration.py` for `_background_validate`
(it straddles both — it's background validation orchestration, arguably its own
`claims/validation.py`-adjacent module, not exactly what the original P0 tier proposed; recommend
a dedicated `background_validation.py` rather than forcing it into an existing bucket).

---

## 6. Standard pipeline — `process()` phases `[0]`-`[10]` (lines 1459-5543)

Markers found via `# ── [N] ──` comments already present in the code (a project convention worth
keeping):

| # | Lines | Name | `cost[]` bucket(s) | Early return? |
|---|---|---|---|---|
| 0 | 1459-1558 | Cache check | `cache_ms` | **YES** (cache hit, line 1544) |
| 1 | 1559-1581 | Risk assess | `risk_ms` | no — but **entire [1]/[2] block is nested inside `if not _skip_rag and not is_subjective_answer`**, so `risk_result`/`plan`/`risk_level` are never set on the subjective/skip_rag path (see §9 risk note) |
| 2 | 1582-1628 | Plan | `plan_ms` (+= adjustment later in [3.5], line 1849) | no |
| 3 | 1629-1664 | Intent analyze (LLM) | `intent_ms` | no |
| 3.5 | 1665-1851 | Epistemic classification | (folds into `plan_ms` via rebuild) | no |
| 4 | 1852-1927 | Epistemic-based clarification | `clarify_ms` | no (clarify_callback may loop but function doesn't return early here) |
| 5 | 1928-1965 | Query enrich | `enrich_ms` | no |
| 6 | 1966-2147 | **Discovery fan-out**: `SharedFetchCache` construction + `ThreadPoolExecutor(max_workers=4)` parallel submit of registry search / web-query formulation / refutation-query formulation / local-answer generation, then sequential `.result()` collection | `registry_ms` | no |
| 7 | 2148-2263 | Epistemic-based web search decision + scrape | `web_ms` | no |
| 8 | 2264-4783 | **Synthesize + full claim/epistemic lifecycle** (see §7 breakdown — this is the 2500-line block that needs the most internal decomposition) | `profile_refutation_ms`, `profile_hypothesis_graph_ms`, `profile_local_wait_ms`, `profile_blind_analysis_ms`, `profile_source_classification_ms`, `synthesize_ms`, `claim_setup_ms`, `claim_retrieval_ms`, `claim_pass2_mapping_nli_ms`, **[claim status classification loop 3487-3809: ZERO cost[] tracking — known, documented gap, not to be silently fixed]**, `belief_update_ms`, `claim_claim_nli_ms`, `final_coverage_ms` | no |
| 9 | 4784-5104 | Claim Status Gate + Existence Query Contract | (none — uses timers from [8]) | no |
| 10 | 5105-5543 | Optimistic respond: bg-validation kickoff, cache write, `[PROFILE]` report, archive/memory/reflection/dataset write-back, trust banner, final return | `total_ms` | **the final return itself** (5535) |

### 6a. Threading / concurrency inventory

| Site | Lines | What |
|---|---|---|
| `ThreadPoolExecutor(max_workers=4)` | 2011-2044 | Discovery fan-out: `registry_future`, `web_future`, `refutation_future`, `local_future` — **not shut down until line 2425** (`parallel_executor.shutdown(wait=False, cancel_futures=False)`), which is well past [8]'s frame-construction sub-phase, not immediately after [7] |
| `threading.Thread(target=_background_validate, ..., daemon=False)` | 5139-5157 | Started inside `_start_bg_validation` closure (nested in [10]), non-daemon — **process will not exit until this thread finishes**, a real behavioral constraint to preserve |
| `threading.active_count()`/`enumerate()` diagnostic logging | 1599-1600, 1632-1633 | pure logging, no semantic effect |

---

## 7. Inside `[8]` — Synthesize + claim/epistemic lifecycle (2264-4783)

This single `# ── [8] ──` region is the real decomposition target; it is far larger than any
other phase and contains almost the entire claim pipeline the spec calls out by name.

| Sub-block | Lines | Purpose | Reads | Writes | Network/LLM | Candidate module |
|---|---|---|---|---|---|---|
| Refutation scan | 2270-2311 | scrape refutation queries via shared cache | `query_frame["refutation_queries"]`, `_request_fetch_cache` | `refutation_snippets` (local) | scrape() → HTTP | `discovery.py` (tail end, arguably belongs with [6]) |
| `query_frame["epistemic"]` build | 2312-2350ish | mirror epistemic_result into query_frame dict | `epistemic_result`, `is_subjective_answer` | `query_frame["epistemic"]` | none | `context.py` |
| Hypothesis graph | ~2350-2408 | `build_hypothesis_graph(...)` | refutation texts, `query_to_use` | `query_frame["hypothesis_graph"]` | LLM (inside `hypothesis_builder`) | `synthesis.py` or dedicated `frame_construction.py` |
| Local-answer wait | 2412-2434 | `local_future.result(timeout=180)`, shuts down executor | `local_future` | `query_frame["local_answer"]` | (already-submitted call) | `discovery.py` |
| Blind analysis | 2437-2484 | LLM-judge best-source selection | `local_answer`, `search_result`, `web_result` | `query_frame["blind_analysis"]`, `blind_selected_source`, `blind_status`, `best_answer`, `refutation_snippets` | LLM (`blind_analysis()` helper, §5) | `synthesis.py` |
| Source classification | 2486-2560 | relevance filter + `classify_sources(main_claim, ...)` | `best_answer`, `search_result`, `web_result`, `refutation_snippets` | `query_frame["classified_sources"]` | embedding/LLM via `is_relevant`/`classify_sources` | `synthesis.py` |
| **`synthesize()` call** | 2576-2599 | the actual synthesis call | `enrich_result`, `search_result`, `web_result`, `query_frame`, `answer_mode` | `synthesis_result`, `reasoning_info` | LLM (inside `orch_synthesizer`) | `synthesis.py` — cleanest single call in the whole block |
| Evidence pool assembly | 2631-2723 | canonical pool + merge + claim normalization | `search_result`, `web_result`, `refutation_snippets`, `reasoning_info` | `evidence_data`, `claims_data` (normalized) | none | `claims/lifecycle.py` |
| Claim identity assignment | 2724-2760 | assign `claim_id`/`claim_type`/`claim_confidence`/`query_context` | `claims_data` | mutates each claim dict in place | none | `claims/lifecycle.py` |
| Structural claim validation | 2761-2867 | `_claim_validator.filter_claims(...)` | `claims_data`, global `_claim_validator` | `claims_data` (filtered), `rejected_structural_claims`, `trace.rejected_claims` | none | `claims/validation.py` — thin wrapper, real logic already in `agent/claim_validator.py` |
| Evidence mapping PASS1 | 2868-2952 | `map_claims_to_evidence(...)` + grounding score | `claims_data`, `evidence_data` | `claim["derived_from_evidence_ids"]`, `claim["verification_status"]="candidate"`, `semantic_grounding_score` | none | `claims/mapping.py` — thin wrapper over `claim_evidence_mapper.py` |
| `_run_claim_evidence_batch` (nested closure!) | 2957-3194 | builds claim/evidence pair jobs, calls `classify_claim_evidence_batch`, writes `claim["evidence_relations"]` | `claims`, `evidence`, closes over `log`/`verbose` from `process()` | `claim["evidence_relations"]` | LLM batch NLI via `claim_relation.classify_claim_evidence_batch` | `claims/mapping.py` — **must become an explicit function taking `log`/`verbose` as params, since it's called twice (PASS1 at 3209, PASS2 at 3377) and currently only exists as a closure** |
| Claim Resolution Gate + retrieval PASS2 | 3225-3486 | `retrieve_for_claims(...)` for unresolved claims, re-run mapper + `_run_claim_evidence_batch` PASS2 | `claims_data`, `_request_fetch_cache` | `evidence_data` (extended), re-mapped claims | HTTP via `retrieve_for_claims` (scraping) | `claims/retrieval.py` — thin wrapper over `claim_evidence_retriever.py` |
| Claim epistemic status | 3487-3809 | authority-or-directness counting → `supported/disputed/contradicted/unverified/rejected` | `claims_data[*]["evidence_relations"]` | `claim["verification_status"]`, `support_count`, `contradiction_count`, etc. | none | `claims/status.py` — **note: no `cost[]` timer wraps this (documented gap, §6 table)** |
| Grounding scores | 3811-3794 (epistemic/support grounding) | aggregate metrics | `claims_data` | `epistemic_grounding_score`, `support_grounding_score` | none | `claims/status.py` |
| Belief update | 3815-3911 | `_belief_manager.add_belief(...)` per claim (max 3) | `claims_data[:3]`, global `_belief_manager` | belief-manager internal state | none (pure Python + internal embedding calls inside `add_belief`) | `claims/lifecycle.py` or dedicated `belief_update.py` — thin wrapper |
| Claim↔Answer linker | 3913-3925 | `_claim_answer_linker.link_answer_to_claims(...)` | `synthesis_result.answer`, `claims_data` | `supporting_ids` | none | `claims/lifecycle.py` |
| Personality cycle | 3927-3937 | `_personality_core.increment_cycles/decisions()` | global `_personality_core` | personality-core internal state | none | `runtime/shared_work.py` or leave inline in pipeline.py (trivial) |
| Claim↔Claim disagreement | 3939-4451 | embedding-prefilter + batch NLI over claim pairs, `_disagreement_engine.challenge(...)` | `claims_data` | `_disagreement_engine` state | **direct HTTP** to `http://127.0.0.1:11434/api/embed` (own `requests.Session`, not via a shared helper) + LLM batch NLI | `claims/disagreement.py` — **HIGH internal complexity, self-contained inputs/outputs though** |
| Final claim coverage | 4452-4553 | `evaluate_final_claim_coverage(...)` | `synthesis_result.answer`, `claims_data`, `query_to_use` | `final_claim_coverage_score`, `final_claims_count/covered/uncovered` | LLM (inside `final_claim_coverage.py`) | `epistemic/final_coverage.py` — thin wrapper, **P0 candidate**, cleanly bounded |
| Epistemic trust adjustment | 4566-4753 | domain/testability/coverage/grounding-based `label` computation and capping | `epistemic_result`, `final_claim_coverage_score`, `support_grounding_score`, `_belief_manager` | `label`, `trace.trust`, `trust_reasons`, several `trace.add_learning_rule(...)` calls | none | `epistemic/trust_gate.py` — **P0 candidate**, well-bounded pure-ish logic (only reads globals, doesn't mutate them) |

CANDIDATE MODULE for the whole `[8]` region: split as above rather than one file — this matches
the spec's `claims/*` and `epistemic/*` submodule breakdown well. `synthesis.py` should own only
the frame-construction + `synthesize()` call sub-blocks (refutation scan through source
classification through the synthesize() call itself); everything from "Evidence pool assembly"
onward is claims/epistemic, not synthesis.

MIGRATION RISK: **HIGH** for the region as a whole (2500 lines, ~15 local variables threaded
through, two circular-looking data dependencies — `claims_data`/`evidence_data` are mutated in
place and re-read by nearly every subsequent sub-block). **LOW-MEDIUM** for each individual
sub-block in isolation once given explicit inputs/outputs instead of shared locals — this is
exactly why `_run_claim_evidence_batch` must be de-closured first (P1, not P0, since two call
sites already prove its input/output contract).

---

## 8. `[9]` Claim Status Gate + Existence Contract (4784-5104)

Two clean, well-bounded pieces:

- **Claim Status Gate** (4796-5023): counts claims by `verification_status`, rewrites
  `synthesis_result.answer`/`trust_level`/`confidence` for 5 mutually exclusive cases (no claims;
  all rejected; only-contradicted; disputed present; verified==0). Reads `claims_data`, writes
  `synthesis_result` in place, plus computes `claims_verified/supported/disputed/contradicted/
  candidate/rejected/unverified` and `total_claims`/`claims_accepted` — **these locals are read
  much later at line 5378-5394 inside the V3 reflection call** (`'claims_accepted' in locals()`
  checks), so they cannot be fully localized to this sub-block without also updating that later
  read site or passing them forward explicitly.
- **Existence Query Contract** (5025-5104): `_is_existence_question(query_to_use)` +
  `supports_query_aspect` check → possible trust downgrade + notice text prepended to the answer.
  Fully self-contained given `query_to_use`, `claims_data`, `synthesis_result`. **This is the
  single cleanest, most literally-named P0 extraction target in the entire file** — it matches
  `agent/orchestrator/epistemic/existence_contract.py` from the target structure almost exactly
  as-is.

CANDIDATE MODULE: `epistemic/existence_contract.py` (existence block only) +
`claims/status.py` (gate block, but must return the counts forward, not just mutate
`synthesis_result`).
MIGRATION RISK: Existence Contract — **LOW**. Claim Status Gate — **MEDIUM** (the forward-read of
`claims_accepted`/`total_claims`/`claims_rejected` at line ~5378 in [10] is the main risk).

---

## 9. `[10]` Optimistic respond (5105-5543) — response assembly + everything else

This is the second-largest undifferentiated region and the highest-risk one to extract, because
it is where nearly every remaining global/singleton gets touched once, in a specific order, with
real side effects:

1. `_start_bg_validation` nested closure (5110-5157) — `nonlocal validation_id`, conditionally
   calls `ai_validator_redis.send_to_deepseek` then spawns the non-daemon `_background_validate`
   thread (§6a).
2. `responder.respond(synthesis_result, start_validation=_start_bg_validation)` (5158) — the
   optimistic-responder pattern: response text is produced *before* validation completes,
   validation happens on a background thread that mutates state later via
   `get_responder().on_validation_done(...)` (inside `_background_validate`, §5).
3. Cache write (`cache.put_from_synthesis(...)`, 5171-5178) — gated on
   `enable_cache and confidence>0.3 and not _skip_rag and not is_subjective_answer`.
4. `[PROFILE]` wall-clock report (5187-5292) — pure read of `cost{}`, no side effects besides
   logging; **cleanest sub-block in [10]**, good P0 candidate for `runtime/profiling.py`.
5. Query archive write (`archive_query(...)`, 5301-5312).
6. V3 write-back block (5332-5507): `self_model.add_decision`/`increment_queries`,
   `memory.add_query`, `reflection.reflect_on_query` (reads `claims_accepted`/`total_claims`/
   `claims_rejected` from locals set in [9] — see §8), confidence/trust downgrade on
   `reflection_result.mistakes`, `query_frame["reflection_verdict"]` write,
   `experience_memory.add_experience`, `motivation.update_from_experience`,
   `dataset_builder.record_episode`, `core_loop.run_cycle` (guarded by
   `core_loop.state.is_running`) — **this entire block is one big `try/except` and order between
   these ~8 side-effecting calls is exactly the "side effects must stay in the same order" case
   the spec warns about (§15 of the brief)**.
7. `_tracer.save_trace(trace)`.
8. `_bad_state_prefix` prepend.
9. Trust banner selection (5-way if/elif on `is_subjective_answer`/`epistemic_result.domain`/
   `testability`/`answer_mode`/`is_science_as_model`) + prepend if not already present.
10. Final `return OrchestratorResponse(...)`.

CANDIDATE MODULE: split into `runtime/profiling.py` (item 4, P0), `response/assembly.py` (items
2, 8, 9, 10 — the actual response-text construction), and a `knowledge_writeback.py` or similar
for item 6 (V3 memory/reflection/dataset persistence — this is arguably its own domain, not
"response assembly" at all). Items 1/2/3 (background validation kickoff, cache write) are
orchestration glue that plausibly stays in `pipeline.py` itself rather than moving to a submodule,
since they're one-shot calls with no internal complexity of their own.

MIGRATION RISK: **HIGH** for the block as a whole — dense side-effect ordering, a `nonlocal`
closure, and forward-reads from earlier phases (`claims_accepted` etc. from §8). **LOW** for the
`[PROFILE]` report sub-block specifically (P0 candidate, pure read of already-computed `cost{}`
and `_request_fetch_cache.summary()`).

---

## 10. `interactive()` + CLI (5546-5620)

Trivial: a REPL loop calling `process()`, plus `if __name__ == "__main__"` argument parsing for
`--web`/`--validate`/`--no-cache`/`--interactive`/`-i`. Must remain byte-for-byte reachable the
same way (`python3 agent/orchestrator_v2.py "query" [flags]`,
`python3 agent/orchestrator_v2.py --interactive [flags]`) per the CLI-compatibility requirement.
MIGRATION RISK: LOW, but this is explicitly **P3/last** per the brief — don't touch until
`pipeline.process()` exists and is proven equivalent.

---

## 11. Dead code / pre-existing issues found (do NOT fix silently — flag only)

These are **not** targets for cleanup during structural extraction (the brief explicitly forbids
mixing bugfixes with moves), but extraction code review should not mistake them for bugs
introduced by the migration:

1. Duplicate global-declaration block: lines 173-186 duplicated verbatim at 313-319.
2. Triple-duplicate init block: `final_claim_coverage_score`/`final_claims_count`/
   `final_claims_covered`/`final_claims_uncovered` initialized 3× in a row (985-1004).
3. Triple-duplicate import: `from agent.final_claim_coverage import evaluate_final_claim_coverage`
   at lines 89, 90, 91.
4. `_claim_graph` global is written (`get_claim_graph()`, line 341) but never read again anywhere
   in the file — dead reference, though the call itself may have side effects worth preserving.
5. `_generate_vulgar_response` (823-840) is defined but has **no call site** anywhere in
   `process()` or `interactive()` — dead function.
6. Dead imports (present in the `from agent.X import ...` list, never referenced anywhere in the
   file): `get_research_engine`, `get_object_resolver`, `should_use_rag`, `is_apology`,
   `add_message`, `get_trace` (from `orch_reputation`), `get_ledger`, `record_unanswered`,
   `update_tree` (aliased `tag_tree_update`), `infer_claim_relations_batch`.
7. `resolve_entity()` (199-200) is a permanent stub returning `None` — the one call site (1689,
   inside the media-interpretation entity-resolution branch) therefore always takes the
   "entity not resolved, ask for clarification" path. This is existing (probably intentional or
   at least long-standing) behavior, not something the migration should "fix".
8. `_claims_conflict` nested function (877-907, inside `process()` itself, defined via `def
   _claims_conflict(...)` right after the `log` closure) — grep found **no call site** for it
   anywhere in `process()`. The actual claim↔claim conflict detection in the disagreement block
   (§7) reimplements similar logic inline rather than calling this closure. Worth flagging to the
   user before moving — may be genuinely dead, or may be a latent bug (intended call site lost).
9. `[1] Risk assess` / `[2] Plan` are both nested inside `if not _skip_rag and not
   is_subjective_answer:` (line 1560) — meaning on the subjective/skip_rag path, `risk_result`,
   `plan`, `risk_level` are **never assigned**, yet later code (e.g. line 1845
   `if epistemic_result.should_use_web and plan.skip_internet:`) is guarded by the same outer
   condition so it doesn't currently crash — but this is a fragile implicit contract that any
   extraction must preserve exactly (do not "helpfully" hoist `plan`/`risk_result` to always be
   defined with defaults, that would be a behavior change).

---

## 12. Prioritized first-extraction candidates (P0 tier)

Ranked by (low risk × high self-containment × clear naming match to target structure):

| Rank | Target | Current lines | Why P0 |
|---|---|---|---|
| 1 | `epistemic/existence_contract.py` ← Existence Query Contract | 5025-5104 | Exact match to target module name; fully self-contained given `(query_to_use, claims_data, synthesis_result)`; single clean side effect (mutates `synthesis_result` in place or returns a new one) |
| 2 | `epistemic/final_coverage.py` ← Final Claim Coverage | 4452-4553 (call) | Already a thin wrapper around `final_claim_coverage.evaluate_final_claim_coverage`; inputs/outputs are 3 plain values in, ~5 plain values out |
| 3 | `runtime/profiling.py` ← `[PROFILE]` wall-clock report | 5187-5294 | Pure read of `cost{}` + `_request_fetch_cache.summary()`; zero mutation of pipeline state; only side effect is `log()` calls (which the caller already provides) |
| 4 | `epistemic/trust_gate.py` ← `_apply_trust_cap`, `_calculate_delta_factors`, `TRUST_STATES`/`_TRUST_ORDER` (top-level helpers) + Epistemic trust adjustment block (4566-4753) | 357-472 (helpers) + 4566-4753 (call site) | Helpers are already pure top-level functions, trivially movable; the trust-adjustment block only *reads* globals (`_belief_manager.get_all_active()`) and local scores, never mutates cross-phase state — output is just `label`/`trust_reasons` |
| 5 | `response/assembly.py` ← `_adapt_answer_to_style`, `_generate_character_response`, `_generate_apology_response`, `build_self_answer` | 685-820 | Pure top-level functions already, zero globals touched, two of them (`_adapt_answer_to_style`) already called from two separate places in `process()` proving a stable contract |
| 6 | `claims/status.py` ← claim epistemic status classification loop | 3487-3809 | Self-contained given `claims_data` with `evidence_relations` already populated; the one wrinkle is the missing `cost[]` timer (§11 — preserve as missing, don't add one silently) and the forward-read of counts in [10]/§8 — extract the *classification* logic first, leave the Claim Status Gate counting (§8) for a P1 pass once the forward-read is handled |
| 7 | `runtime/timeout.py` — likely already effectively covered by existing `agent/orch_timeout.py` (`step_timer`); confirm during implementation whether there's anything orchestrator_v2-specific left to extract here, or whether this candidate module should be dropped from the target tree entirely | n/a | avoid creating an empty/near-empty module just to match the original template (§28 of the brief: don't create files for their own sake) |

P1 (next tier, more coupled but still tractable): `claims/lifecycle.py` (claim identity + evidence
pool assembly), `claims/validation.py` (thin wrapper over `_claim_validator.filter_claims`),
`claims/mapping.py` (requires de-closuring `_run_claim_evidence_batch` first — do this as its own
sub-step before extracting the module, since two call sites already prove the needed signature).

P2/P3 per the brief's own milestone ordering: `claims/retrieval.py`, `discovery.py` (fan-out +
refutation + local-wait), `claims/disagreement.py` (embedding+NLI block, self-contained but large
and has its own raw HTTP session), `response/assembly.py`'s item-6 knowledge-writeback slice, then
finally `pre_pipeline.py` and `pipeline.py` itself (moving `process()`'s outer shape last, per the
brief's explicit P3 instruction).

---

## 13. Circular-import risk assessment

No evidence of a cycle risk for the P0/P1 candidates above: every domain module they'd wrap
(`epistemic_router`, `final_claim_coverage`, `claim_validator`, `belief_manager`, etc.) currently
has zero import of anything under a hypothetical `agent.orchestrator.*` namespace (verified by the
import list in §3 — all imports in `orchestrator_v2.py` point *into* domain modules, never the
reverse, and this audit did not find any of those domain modules importing `orchestrator_v2`
itself). The direction `agent.orchestrator → domain modules` from the brief's §13 should hold
cleanly as long as new `agent/orchestrator/*.py` files only import from existing domain modules
and from `agent.orch_schemas` for shared dataclasses — never from `agent.orchestrator_v2` itself
(the facade should import *from* the new package, not vice versa, once that direction flips in a
later milestone).

One thing to watch: `orch_synthesizer._call` and `orch_synthesizer.EPISTEMIC_WARNING` are imported
directly (not through the public `synthesize` API) by `generate_local_answer` — if
`generate_local_answer` moves into `agent/orchestrator/discovery.py`, that file will need the same
direct `orch_synthesizer._call` import; confirm `_call` is intended as semi-public (leading
underscore suggests private-but-reused-elsewhere already, since orchestrator_v2.py itself reaches
past the public API here) rather than assuming it's safe to keep reaching into.

---

## 14. Statistics (current state, before any extraction)

```
legacy_orchestrator_lines=5620
modular_package_lines=0
functions_moved=0
blocks_moved=0
legacy_process_lines=4703  (lines 843-5546)
regressions_passed=13/13 (baseline, before any change)
equivalence_checks=0  (package does not exist yet)
```
