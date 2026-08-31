"""
Standard pipeline phases [0]-[7] — extracted from agent/orchestrator_v2.py's
process(): cache check (with its own early-return on cache hit), risk
assess, plan, intent analyze, epistemic classification, epistemic-based
clarification, query enrich, discovery fan-out (registry/web/refutation/
local-answer), and epistemic-based web search decision.

Structural extraction only: no thresholds, prompts, ordering, or log
markers changed. Wrapped as ONE function (matching how `pre_pipeline.py`
wrapped its ~600-line/11-early-return range) specifically to avoid
splitting `[0]`-`[7]` across multiple functions/files — a cross-phase
timer-reuse dependency was found while scoping this (`cost["registry_ms"]`
in `[6]` is computed from a `t0` that was actually set back in `[5]`) that
a single flat scope sidesteps entirely, with zero change to what any cost
bucket measures.

`generate_local_answer` and `resolve_entity` move here too — each had
exactly one call site, both inside this block (`resolve_entity` is a
permanent stub that always returns `None`; kept verbatim, not "fixed").

Free-variable audit performed via a dedicated fork before writing any
code (same methodology as `pre_pipeline.py`): confirmed the input list
below by direct re-derivation, not assumption — several candidates
(`verbose`, `intent_confidence`, `context_registry`, `entity`,
`synthesis_result`, `reasoning_info`, `claims_data`, `evidence_data`, the
three grounding scores, the four `final_claim*` values) turned out to be
completely unread within this range and are NOT threaded through; they
stay as orchestrator_v2.py's own untouched locals from `pre_pipeline.py`'s
state dict. `plan`/`risk_result` are read only within this same range
(never after it), so they don't need to flow out either, despite being
nested inside `if not skip_rag and not is_subjective_answer:` — confirmed
that guard is never actually False in any reachable execution (`skip_rag`
always resets to False in its own exception handlers, `is_subjective_answer`
is permanently False everywhere in the codebase), so `plan`/`risk_result`
are always genuinely defined by the time anything inside this range reads
them.

One subtler dependency found and preserved: `dt` (the loop-reused timer
variable most phases assign via `step_timer(...)`) is read by
orchestrator_v2.py immediately after this function returns, via
`registry.update_latency("synthesize", dt if 'dt' in locals() else 0)` —
that check currently always finds a real `dt` because `[0]`'s cache-check
phase unconditionally sets it and every later phase either overwrites it
or leaves that value in place. Extracting `[0]`-`[7]` into a separate
function would silently make that check always fall to the `else 0`
branch (since `dt` becomes local to this function and vanishes from
process()'s scope), so `dt` is returned explicitly and the caller assigns
it as its own local — restoring the exact original always-defined
semantics rather than "fixing" the fragile check itself.

Return protocol matches `pre_pipeline.py`: `(early_response, state)` where
`early_response` is an `OrchestratorResponse` on a cache hit (caller must
return it immediately and unconditionally) or `None` on the continuation
path, in which case `state` is a dict the caller unpacks key-by-key into
its own locals.
"""

import concurrent.futures
from dataclasses import asdict
import threading
import time
import uuid
from typing import Any, Dict, Optional

from agent.orch_schemas import (
    EnrichedQuery,
    EvidenceRecord,
    IntentResult,
    OrchestratorResponse,
    SearchResult,
    WebQueryResult,
)
from agent.orch_cache import get_cache
from agent.orch_risk import assess_risk
from agent.orch_planner import build_plan
from agent.orch_intent import analyze_intent
from agent.orch_clarifier import ClarificationSession
from agent.orch_enricher import enrich_query
from agent.orch_registry_search import search_registry, CONF_THRESHOLD
from agent.orch_web_query import formulate_queries, formulate_refutation_queries
from agent.orch_web_scraper import (
    SharedFetchCache,
    scrape_budgeted_side,
    STAGE6_MAIN_BUDGET,
    STAGE6_COUNTER_BUDGET,
)
from agent.orch_timeout import step_timer
from agent.orch_reputation import add_decision_event
from agent.epistemic_router import (
    classify_claim,
    get_trust_label_for_epistemic,
    get_response_mode_description,
    get_trust_cap_for_testability,
    get_objectivity_score,
)
from agent.strategy_router import SearchStrategy
from agent.orchestrator.epistemic.trust_gate import _apply_trust_cap
from agent.orchestrator.response.assembly import _adapt_answer_to_style
from agent.acquisition import (
    AcquisitionRequest,
    network_node_stub,
    persist_acquisition_observation,
)
from agent.external_ai_acquisition import acquire_external_ai_parallel


def _serialize_acquisition_observation(obs):
    row = asdict(obs)
    status = row.get("status")
    row["status"] = getattr(status, "value", status)
    return row


def resolve_entity(query: str) -> Optional[Dict[str, Any]]:
    return None


def generate_local_answer(query: str, context: str = "") -> str:
    """Генерирует ответ локальной моделью на основе запроса и контекста."""
    from agent.orch_synthesizer import _call, EPISTEMIC_WARNING

    # P1.1 (YANDI_FULL_PIPELINE_AUDIT.md, §5/§31): это первая точка
    # semantic drift — раньше prompt не ограничивал scope ответа,
    # из-за чего даже узкий существование-вопрос превращался в
    # энциклопедический обзор объекта. Инструкция ниже добавляет
    # scope-binding, но НЕ задаёт заранее конкретный ответ и НЕ
    # содержит предметно-специфичных (напр. астрономических) правил.
    #
    # ВАЖНО: это изменение НЕ прошло live A/B валидацию (требует
    # реального вызова LLM, что не выполнялось в рамках аудита) —
    # см. §34 отчёта. Поведенческий эффект не подтверждён рантаймом.
    prompt = f"""{EPISTEMIC_WARNING}

    Вопрос пользователя: {query}

    Контекст: {context if context else "Нет дополнительного контекста"}

    СНАЧАЛА ответь на то, что именно спросил пользователь — не шире и не уже.
    Если вопрос является вопросом существования ("есть ли", "существует ли",
    "обнаружен ли") — сосредоточься на прямых свидетельствах за и против
    существования, а не на общих условиях объекта.

    Добавляй фоновые/объясняющие факты ТОЛЬКО если они непосредственно
    необходимы, чтобы объяснить прямой ответ. Не превращай узкий вопрос
    в энциклопедический обзор объекта.

    Используй выражения: "согласно наблюдениям", "по имеющейся информации", "некоторые источники указывают".
    Не выдавай ничего как "факт" или "истина". Всё — гипотеза.

    Ответ:"""

    try:
        response = _call(prompt, max_tokens=800, temp=0.3)
        return response.strip()
    except Exception as e:
        # Technical failure != answer.
        #
        # Ошибка модели не должна проходить дальше как semantic content,
        # участвовать в blind analysis, claim extraction или evidence.
        print(
            f"[Local] generation failed: "
            f"{type(e).__name__}: {e}"
        )
        return ""


def run_standard_pipeline(
    request,
    enable_web,
    enable_validation,
    enable_cache,
    clarify_callback,
    t_start,
    query_frame,
    ctx,
    memory,
    self_model,
    log,
    trace,
    trace_id,
    decision_id,
    cost,
    registry,
    tracer,
    query_to_use,
    char,
    state,
    intent_type,
    entity_info,
    strategy_router,
    strategy_result,
    skip_rag,
    exact_mode,
    is_subjective_answer,
    enrich_result,
):
    cost["_t_start"] = t_start

    # ── [0] Cache check ────────────────────────────────────────────────────
    log("[0] Cache check...")
    t0 = time.time()
    cache = get_cache()

    if enable_cache:
        cache_result, dt, _ = step_timer(
            "cache_check",
            lambda: cache.get(query_to_use)
        )
        cost["cache_ms"] = (time.time() - t0) * 1000
        registry.update_latency("cache_check", dt)
        cache_hit = bool(cache_result and cache_result.hit)
    else:
        cache_result = None
        cache_hit = False
        dt = 0.0
        cost["cache_ms"] = 0.0
        log("  · Cache отключён (--no-cache)")

    if cache_hit and not skip_rag and not is_subjective_answer:
        log(f"  ✓ Cache HIT (similarity={cache_result.similarity:.2f})")
        trust_level = cache_result.trust_level or "HYPOTHESIS"

        epistemic_result = classify_claim(query_to_use)
        cap_label = get_trust_cap_for_testability(epistemic_result.testability)
        trust_level = _apply_trust_cap(trust_level, cap_label)
        log(f"  · cache trust adjusted: {trust_level} (cap={cap_label})")

        trace.add_execution("cache", "completed", cost["cache_ms"], {"hit": True, "similarity": cache_result.similarity})
        trace.add_reasoning("cache", {"hit": True, "similarity": cache_result.similarity}, "use_cache", [])
        trace.trust = trust_level
        trace.trust_reason = f"ответ из кэша, cap={cap_label}"

        if cache_result.claims:
            for claim_data in cache_result.claims:
                if isinstance(claim_data, dict):
                    trace.add_claim_raw(claim_data)
        if cache_result.evidence:
            for ev_data in cache_result.evidence:
                if isinstance(ev_data, dict):
                    trace.add_evidence(EvidenceRecord(
                        evidence_id=ev_data.get("evidence_id", f"ev_{uuid.uuid4().hex[:8]}"),
                        source_type=ev_data.get("source_type", "web"),
                        source_uri=ev_data.get("source_uri", ""),
                        source_title=ev_data.get("source_title", ""),
                        content_excerpt=ev_data.get("content_excerpt", "")[:300],
                        relevance_to_query=ev_data.get("relevance_to_query", 0.5),
                        quality_score=ev_data.get("quality_score", 0.0),
                        source_class=ev_data.get("source_class", "unknown"),
                        evidence_eligible=ev_data.get("evidence_eligible", False),
                        authority=ev_data.get("authority", 0.0),
                        traceability=ev_data.get("traceability", 0.0),
                        primaryness=ev_data.get("primaryness", 0.0),
                        is_meta_pipeline_output=ev_data.get("is_meta_pipeline_output", False),
                        is_subject_matter_evidence=ev_data.get("is_subject_matter_evidence", True),
                        rejection_reason=ev_data.get("rejection_reason"),
                        source_cluster_id=ev_data.get("source_cluster_id"),
                    ))
        if cache_result.epistemic:
            trace.set_epistemic(cache_result.epistemic)
            for key, value in cache_result.epistemic.items():
                trace.add_observation(f"cache_epistemic_{key}", value)

        trace.final_answer = cache_result.answer
        trace.cost = cost

        if memory and self_model:
            try:
                memory.add(
                    event_type="cache_hit",
                    summary=f"Cache hit: {query_to_use[:60]}",
                    details={"query": query_to_use, "similarity": cache_result.similarity},
                    importance=0.7
                )
                self_model.increment_queries()
            except Exception:
                pass

        tracer.save_trace(trace)

        char.process_help(query_to_use)
        final_answer = cache_result.answer

        final_answer = _adapt_answer_to_style(final_answer, state)

        return OrchestratorResponse(
            answer=final_answer,
            trust_level=trust_level,
            preliminary=False,
            steps_taken=["cache_check"],
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        ), None

    log("  · Cache miss или пропущен для субъективного интента")
    trace.add_execution("cache", "completed", cost["cache_ms"], {"hit": False})
    trace.add_reasoning("cache", {"hit": False}, "skip_cache", [
        {"option": "use_cache", "accepted": False, "reason": "cache miss" if not skip_rag else "subjective_intent", "expected_gain": 0.0}
    ])

    # ── [1] Risk assess (пропускаем для субъективного) ──
    if not skip_rag and not is_subjective_answer:
        log("[1] Risk assess...")
        t0 = time.time()
        risk_result, dt, _ = step_timer("risk_assess", lambda: assess_risk(query_to_use))
        cost["risk_ms"] = (time.time() - t0) * 1000
        registry.update_latency("risk_assess", dt)
        risk_level = risk_result.risk_level
        log(f"  · risk={risk_level}, nodes={risk_result.nodes_required}")

        trace.add_execution("risk", "completed", cost["risk_ms"], {"risk": risk_level, "nodes": risk_result.nodes_required})
        trace.add_reasoning("risk", {"query": query_to_use[:50]}, risk_level, [])

        add_decision_event(
            event_type="ExecutionStep",
            trace_id=trace_id,
            entity_type="orchestrator",
            entity_id="risk_assess",
            verdict=risk_level,
            reason=f"risk={risk_level}, nodes={risk_result.nodes_required}",
            meta={"decision_id": decision_id}
        )

        # ── [2] Plan ───────────────────────────────────────────────────────────
        log("[2] Planning...")
        # P1 (autonomous fix pass, plan/intent ~6s investigation):
        # step_timer overhead (~0ms, isolated benchmark), build_plan()'s
        # own logic (~2ms, [Plan SubProfile]), a single core_loop idle
        # cycle (61ms, isolated benchmark), module import (~0.3s) and
        # _init_v3() (~82ms) were all measured directly and ruled out —
        # none explain a 6s gap. No timeout occurred before this point in
        # the last live run, ruling out an orphaned P0-C background
        # thread as the cause THAT time. Remaining candidate: OS/GIL
        # thread-scheduling contention from a concurrently running
        # thread (e.g. the core_loop background thread) that isolated
        # microbenchmarks can't reproduce. This logs live thread count/
        # names right at measurement start so the next live run can
        # confirm or rule this out with real data instead of guessing
        # further.
        log(
            f"  · threads_alive={threading.active_count()} "
            f"names={[t.name for t in threading.enumerate()]}"
        )
        t0 = time.time()
        plan, dt, _ = step_timer("plan", lambda: build_plan(query_to_use, risk_result, use_llm=False))
        cost["plan_ms"] = (time.time() - t0) * 1000
        registry.update_latency("plan", dt)
        steps = [s.name if hasattr(s, "name") else str(s) for s in plan.steps]
        log(f"  · steps={len(steps)}, internet={not plan.skip_internet}")

        trace.add_execution("plan", "completed", cost["plan_ms"], {"steps": steps, "skip_internet": plan.skip_internet})
        trace.add_reasoning("plan", {"risk": risk_level}, "use_plan", [
            {"option": "skip_internet", "accepted": plan.skip_internet, "reason": "plan decision"}
        ])

        add_decision_event(
            event_type="ExecutionStep",
            trace_id=trace_id,
            entity_type="orchestrator",
            entity_id="plan",
            verdict=f"steps={len(steps)}",
            reason=f"skip_internet={plan.skip_internet}",
            meta={"decision_id": decision_id}
        )

        # ---- АВТОМАТИЧЕСКОЕ ВКЛЮЧЕНИЕ VALIDATION ПО РИСКУ ----
        if not enable_validation and risk_level in ["medium", "high", "critical"]:
            enable_validation = True
            log(f"[Validation] Автоматически включена для risk={risk_level}")

    # ── [3] Intent analyze ─────────────────────────────────────────────────
    log("[3] Intent analyze...")
    log(
        f"  · threads_alive={threading.active_count()} "
        f"names={[t.name for t in threading.enumerate()]}"
    )
    t0 = time.time()
    intent_result, dt, timed_out = step_timer("intent", lambda: analyze_intent(query_to_use, ctx))
    cost["intent_ms"] = (time.time() - t0) * 1000
    registry.update_latency("intent", dt)
    registry.update_reliability("intent", not timed_out and intent_result is not None)

    if timed_out or intent_result is None:
        log("  ⚠ Timeout — дефолт")
        intent_result = IntentResult(
            intent="general", entities={}, missing=[],
            need_clarification=False, confidence=0.5,
        )
    else:
        log(f"  · intent={intent_result.intent}, conf={intent_result.confidence:.2f}, clarify={intent_result.need_clarification}")
        trace.add_confidence("intent", intent_result.confidence, f"определён как {intent_result.intent}")

    trace.add_execution("intent", "completed" if not timed_out else "timeout", cost["intent_ms"],
                        {"intent": intent_result.intent, "confidence": intent_result.confidence})
    trace.add_reasoning("intent", {"query": query_to_use[:50]}, intent_result.intent, [])

    add_decision_event(
        event_type="ExecutionStep",
        trace_id=trace_id,
        entity_type="orchestrator",
        entity_id="intent",
        verdict=intent_result.intent,
        reason=f"conf={intent_result.confidence:.2f}",
        meta={"decision_id": decision_id}
    )

    # ── [3.5] Epistemic classification (пропускаем для субъективного) ──
    if not skip_rag and not is_subjective_answer:
        log("[3.5] Epistemic classification (v3)...")
        epistemic_result = classify_claim(query_to_use, intent_result.intent, intent_result.confidence)

        media_triggers = [
            "смысл фильма", "о чем фильм", "объясни концовку",
            "разбор фильма", "что хотел сказать режиссёр",
            "смысл сериала", "смысл книги", "смысл игры",
            "о чем сериал", "о чем книга", "о чем игра"
        ]
        is_media_query = any(t in query_to_use.lower() for t in media_triggers)

        if is_media_query and epistemic_result.domain in ["philosophical", "interpretive"]:
            log(f"  · overriding domain: {epistemic_result.domain} → media_interpretation")
            epistemic_result.domain = "media_interpretation"
            epistemic_result.testability = "partially_testable"
            epistemic_result.answer_mode = "qualified_factual"
            epistemic_result.should_use_web = True
            epistemic_result.max_trust_cap = "SUPPORTED"
            epistemic_result.allow_single_conclusion = False
            epistemic_result.needs_frame_split = False
            epistemic_result.should_avoid_single_truth_claim = True

            entity = resolve_entity(query_to_use)
            if entity:
                log(f"  · entity resolved: {entity.get('title')} ({entity.get('year', 'unknown')})")
                request._entity = entity
            else:
                log(f"  · entity not resolved, will ask for clarification")
                epistemic_result.need_clarification = True
                epistemic_result.suggested_clarification = "Не удалось точно идентифицировать фильм. Укажите год или оригинальное название."

        objectivity_score, epistemic_warning, is_science_as_model = get_objectivity_score(
            testability=epistemic_result.testability,
            domain=epistemic_result.domain,
            knowledge_stability=epistemic_result.knowledge_stability,
        )

        epistemic_result.objectivity_score = objectivity_score
        epistemic_result.epistemic_warning = epistemic_warning
        epistemic_result.is_science_as_model = is_science_as_model

        log(f"  · domain={epistemic_result.domain}, testability={epistemic_result.testability}")
        log(f"  · answer_mode={epistemic_result.answer_mode} ({get_response_mode_description(epistemic_result.answer_mode)})")
        log(f"  · should_use_web={epistemic_result.should_use_web}")
        log(f"  · trust_score={epistemic_result.trust_score:.2f}")
        log(f"  · max_trust_cap={epistemic_result.max_trust_cap}")
        log(f"  · objectivity_score={epistemic_result.objectivity_score:.2f}")
        if epistemic_result.is_science_as_model:
            log(f"  · ⚠️  НАУКА КАК МОДЕЛЬ — это теория, а не истина")

        log(f"  · allow_single_conclusion={epistemic_result.allow_single_conclusion}")
        log(f"  · needs_frame_split={epistemic_result.needs_frame_split}")
        log(f"  · should_avoid_single_truth_claim={epistemic_result.should_avoid_single_truth_claim}")

        if epistemic_result.need_clarification:
            log(f"  · need_clarification=True: {epistemic_result.suggested_clarification}")

        if epistemic_result.testability in ["interpretive", "non_falsifiable"]:
            old_intent = intent_result.intent
            intent_result.intent = epistemic_result.domain
            intent_result.confidence = epistemic_result.confidence
            log(f"  · intent overridden: {old_intent} → {intent_result.intent} (by epistemic)")

        trace.add_reasoning(
            "epistemic_v3",
            {
                "domain": epistemic_result.domain,
                "subdomain": epistemic_result.subdomain,
                "testability": epistemic_result.testability,
                "answer_mode": epistemic_result.answer_mode,
                "domains": epistemic_result.domains,
                "reason": epistemic_result.reason,
                "need_clarification": epistemic_result.need_clarification,
                "suggested_clarification": epistemic_result.suggested_clarification,
                "should_use_web": epistemic_result.should_use_web,
                "trust_score": epistemic_result.trust_score,
                "max_trust_cap": epistemic_result.max_trust_cap,
                "perspective": epistemic_result.perspective,
                "modality": epistemic_result.modality,
                "allow_single_conclusion": epistemic_result.allow_single_conclusion,
                "needs_frame_split": epistemic_result.needs_frame_split,
                "should_avoid_single_truth_claim": epistemic_result.should_avoid_single_truth_claim,
                "intent_after_override": intent_result.intent,
                "is_media_query": is_media_query,
                "entity_resolved": bool(getattr(request, '_entity', None)),
                "objectivity_score": epistemic_result.objectivity_score,
                "is_science_as_model": epistemic_result.is_science_as_model,
                "epistemic_warning": epistemic_result.epistemic_warning,
            },
            epistemic_result.domain,
            []
        )
        trace.add_observation("epistemic_domain", epistemic_result.domain)
        trace.add_observation("epistemic_subdomain", epistemic_result.subdomain)
        trace.add_observation("epistemic_testability", epistemic_result.testability)
        trace.add_observation("epistemic_answer_mode", epistemic_result.answer_mode)
        trace.add_observation("epistemic_need_clarification", epistemic_result.need_clarification)
        trace.add_observation("epistemic_should_use_web", epistemic_result.should_use_web)
        trace.add_observation("epistemic_perspective", epistemic_result.perspective)
        trace.add_observation("epistemic_modality", epistemic_result.modality)

        # E (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md, §E): is_negative_claim
        # раньше вычислялся (после P0.2), но был полностью невидим —
        # ни один компонент его не читал. Это query-level сигнал (сам
        # query сформулирован вокруг отрицания), намеренно НЕ подключён
        # к retrieval priority — там работает claim-level
        # _is_absence_claim() (claim_evidence_retriever.py) через
        # claim role classifier (§B). Смешивание этих двух сигналов
        # рискует сужать breadth для "Почему X не..." вопросов, для
        # которых ширина должна сохраняться. Здесь — только видимость
        # в trace, без влияния на решения.
        trace.add_observation(
            "epistemic_is_negative_claim",
            epistemic_result.is_negative_claim,
        )
        trace.add_observation("epistemic_max_trust_cap", epistemic_result.max_trust_cap)
        trace.add_observation("epistemic_needs_frame_split", epistemic_result.needs_frame_split)
        trace.add_observation("intent_after_override", intent_result.intent)
        trace.add_observation("is_media_query", is_media_query)
        trace.add_observation("objectivity_score", epistemic_result.objectivity_score)
        trace.add_observation("is_science_as_model", epistemic_result.is_science_as_model)

        trace.set_epistemic({
            "domain": epistemic_result.domain,
            "subdomain": epistemic_result.subdomain,
            "testability": epistemic_result.testability,
            "answer_mode": epistemic_result.answer_mode,
            "confidence": epistemic_result.confidence,
            "trust_score": epistemic_result.trust_score,
            "max_trust_cap": epistemic_result.max_trust_cap,
            "reason": epistemic_result.reason,
            "should_use_web": epistemic_result.should_use_web,
            "perspective": epistemic_result.perspective,
            "modality": epistemic_result.modality,
            "allow_single_conclusion": epistemic_result.allow_single_conclusion,
            "needs_frame_split": epistemic_result.needs_frame_split,
            "should_avoid_single_truth_claim": epistemic_result.should_avoid_single_truth_claim,
            "knowledge_stability": getattr(epistemic_result, "knowledge_stability", "unknown"),
            "stability_confidence": getattr(epistemic_result, "stability_confidence", 0.5),
            "stability_reason": getattr(epistemic_result, "stability_reason", ""),
            "is_media_query": is_media_query,
            "objectivity_score": epistemic_result.objectivity_score,
            "is_science_as_model": epistemic_result.is_science_as_model,
            "epistemic_warning": epistemic_result.epistemic_warning,
        })

        epistemic_trust_label = get_trust_label_for_epistemic(epistemic_result)
        log(f"  · epistemic_trust_label={epistemic_trust_label}")

        trace._epistemic = {
            "domain": epistemic_result.domain,
            "testability": epistemic_result.testability,
            "answer_mode": epistemic_result.answer_mode,
            "max_trust_cap": epistemic_result.max_trust_cap,
            "needs_frame_split": epistemic_result.needs_frame_split,
            "should_avoid_single_truth_claim": epistemic_result.should_avoid_single_truth_claim,
            "is_media_query": is_media_query,
            "objectivity_score": epistemic_result.objectivity_score,
            "is_science_as_model": epistemic_result.is_science_as_model,
            "epistemic_warning": epistemic_result.epistemic_warning,
        }

        add_decision_event(
            event_type="ExecutionStep",
            trace_id=trace_id,
            entity_type="orchestrator",
            entity_id="epistemic",
            verdict=epistemic_result.domain,
            reason=f"testability={epistemic_result.testability}, mode={epistemic_result.answer_mode}, cap={epistemic_result.max_trust_cap}, objectivity={epistemic_result.objectivity_score:.2f}",
            meta={
                "decision_id": decision_id,
                "perspective": epistemic_result.perspective,
                "needs_frame_split": epistemic_result.needs_frame_split,
                "is_media_query": is_media_query,
                "is_science_as_model": epistemic_result.is_science_as_model,
            }
        )
        # ---- ПЕРЕСОЗДАНИЕ PLAN НА ОСНОВЕ EPISTEMIC ----
        if epistemic_result.should_use_web and plan.skip_internet:
            log("  · Rebuilding plan: web required by epistemic, overriding skip_internet")
            plan, dt2, _ = step_timer("plan_rebuild", lambda: build_plan(query_to_use, risk_result, use_llm=False))
            plan.skip_internet = False
            cost["plan_ms"] += (time.time() - t0) * 1000
            trace.add_execution("plan_rebuild", "completed", dt2 * 1000, {"skip_internet": plan.skip_internet})

    # ── [4] Epistemic-based clarification (пропускаем для субъективного) ──
    clarification_done = False
    clarification_answered = False

    if not skip_rag and not is_subjective_answer:
        if epistemic_result.need_clarification and clarify_callback:
            log("[4] Epistemic clarification required...")
            t0 = time.time()
            clarification_text = epistemic_result.suggested_clarification
            if not clarification_text:
                clarification_text = "Уточните, пожалуйста, ваш запрос: что именно вы имеете в виду?"
            log(f"  · asking: {clarification_text}")
            try:
                answers = clarify_callback(clarification_text)
                if answers:
                    clarification_answered = True
                    clarification_done = True
                    log(f"  · clarification received: {answers}")
                    if isinstance(answers, dict):
                        for key, val in answers.items():
                            if val:
                                query_to_use = f"{query_to_use} {val}"
            except Exception as e:
                log(f"  · clarification error: {e}")
            cost["clarify_ms"] = (time.time() - t0) * 1000
        elif intent_result.need_clarification and clarify_callback:
            log("[4] Intent clarification...")
            t0 = time.time()
            cl_session = ClarificationSession(query_to_use, intent_result)
            rounds = 0
            while rounds < 3:
                questions = cl_session.next_questions()
                if not questions:
                    break
                formatted = cl_session.format_questions()
                log(f"  · раунд {rounds+1}: {len(questions)} вопросов")
                try:
                    answers = clarify_callback(formatted)
                    intent_result = cl_session.submit_answers(answers)
                except Exception:
                    break
                rounds += 1
                if cl_session.complete:
                    log("  ✓ Уточнения получены")
                    clarification_done = True
                    break
            cost["clarify_ms"] = (time.time() - t0) * 1000
        else:
            log("[4] Clarification — пропуск")
            cost["clarify_ms"] = 0.0
    else:
        log("[4] Clarification — пропуск (субъективный режим)")
        cost["clarify_ms"] = 0.0

    trace.add_execution("clarify", "completed" if clarification_done else "skipped", cost["clarify_ms"],
                        {"done": clarification_done, "answered": clarification_answered})
    trace.add_reasoning("clarify", {"need": intent_result.need_clarification, "epistemic_need": getattr(epistemic_result, "need_clarification", False) if not is_subjective_answer else False},
                        "skip" if not clarification_done else "done", [
        {"option": "clarify", "accepted": clarification_done, "reason": "user input needed" if clarification_done else "not needed"}
    ])

    add_decision_event(
        event_type="ExecutionStep",
        trace_id=trace_id,
        entity_type="orchestrator",
        entity_id="clarify",
        verdict="completed" if clarification_done else "skipped",
        reason=f"epistemic_need={getattr(epistemic_result, 'need_clarification', False) if not is_subjective_answer else False}",
        meta={"decision_id": decision_id}
    )

    # ---- ПРОПУСКАЕМ RAG ДЛЯ СУБЪЕКТИВНЫХ ИНТЕНТОВ ----
    if skip_rag or is_subjective_answer:
        log("[RAG] Пропущен для субъективного интента")
        pass
    else:
        # ── [5] Query enrich ──────────────────────────────────────────────────
        log("[5] Query enrich...")
        t0 = time.time()

        if epistemic_result.testability in ["interpretive", "non_falsifiable"] and epistemic_result.domain != "media_interpretation":
            log("  · enrich пропущен (эпистемический режим)")
            enrich_result = EnrichedQuery(original=query_to_use, enriched=query_to_use, params={})
            timed_out = False
            cost["enrich_ms"] = 0
        else:
            if exact_mode:
                log("  · EXACT MODE — enrich пропущен")
                enrich_result = EnrichedQuery(original=query_to_use, enriched=query_to_use, params={})
                timed_out = False
                cost["enrich_ms"] = 0
            else:
                enrich_result, dt, timed_out = step_timer("enrich", lambda: enrich_query(query_to_use, intent_result))
                cost["enrich_ms"] = (time.time() - t0) * 1000
                registry.update_latency("enrich", dt)
                if timed_out or enrich_result is None:
                    log("  ⚠ Timeout — оригинальный запрос")
                    enrich_result = EnrichedQuery(original=query_to_use, enriched=query_to_use, params={})
                else:
                    log(f"  · enriched: {enrich_result.enriched[:80]}")

        trace.add_execution("enrich", "completed" if not timed_out else "timeout", cost["enrich_ms"],
                            {"original": query_to_use[:50], "enriched": enrich_result.enriched[:50]})

        add_decision_event(
            event_type="ExecutionStep",
            trace_id=trace_id,
            entity_type="orchestrator",
            entity_id="enrich",
            verdict=enrich_result.enriched[:60],
            reason="query enrichment",
            meta={"decision_id": decision_id}
        )

        # ============================================================
        # REQUEST-SCOPED SHARED FETCH CACHE
        # ============================================================
        #
        # Refutation performance audit: main web scrape(), refutation
        # scrape() и claim-specific retrieve_for_claims() раньше каждый
        # создавали СВОЙ собственный SharedFetchCache (или scrape()
        # создавал одноразовый внутри себя) — то есть один и тот же URL,
        # физически обнаруженный и в основном web, и в refutation
        # discovery pool, мог быть скачан дважды. Один экземпляр на
        # запрос, переданный во все три места, устраняет это дублирование
        # БЕЗ изменения relation/evidence_role/retrieval_origin — это
        # чисто физический fetch-уровень, epistemic ownership каждого
        # snippet/claim остаётся независимым, как и раньше.
        _request_fetch_cache = SharedFetchCache()

        # ── [6] Local registry search ─────────────────────────────────────────
        #
        # ВАЖНО:
        # local answer больше НЕ запускается до control-plane LLM calls.
        #
        # Один локальный Ollama обслуживает все generation-запросы.
        # Если тяжёлая генерация answer первой захватывает generation
        # semaphore, formulate_queries/refutation_queries не успевают
        # стартовать до своих orchestration timeout.
        log("[6] Parallel fan-out: registry + web query + refutation + local answer")

        # ============================================================
        # PARALLEL FAN-OUT
        # ============================================================
        #
        # CPU / I/O и GPU-задачи стартуют одновременно.
        #
        # GPU generation concurrency ограничивается внутри
        # orch_synthesizer через Semaphore(2), поэтому здесь нет
        # необходимости искусственно сериализовать задачи.
        #
        # registry       -> CPU / embeddings
        # web query      -> GPU generation
        # refutation     -> GPU generation
        # local answer   -> GPU generation
        #
        # Из трёх generation-задач две выполняются одновременно,
        # третья ждёт свободный GPU-slot.

        parallel_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=6
        )

        cost["acq_registry_start_ms"] = (time.time() - t_start) * 1000
        registry_future = parallel_executor.submit(
            search_registry,
            enrich_result.enriched,
        )

        wq_result = None
        web_dt = 0.0
        refutation_result = None

        web_future = None
        if enable_web:
            cost["acq_web_query_start_ms"] = (time.time() - t_start) * 1000
            web_future = parallel_executor.submit(
                formulate_queries,
                enrich_result,
            )

        refutation_future = None
        if enable_web:
            cost["acq_refutation_query_start_ms"] = (time.time() - t_start) * 1000
            refutation_future = parallel_executor.submit(
                formulate_refutation_queries,
                enrich_result,
            )

        external_ai_future = None
        external_ai_deadline_s = 45.0
        if enable_validation:
            cost["acq_external_ai_start_ms"] = (time.time() - t_start) * 1000
            external_ai_future = parallel_executor.submit(
                acquire_external_ai_parallel,
                query_to_use,
                ["gpt", "deepseek", "claude", "kimi"],
                request_id=trace_id,
                provider_timeout_s=90.0,
                overall_deadline_s=external_ai_deadline_s,
            )

        cost["acq_node_start_ms"] = (time.time() - t_start) * 1000
        node_future = parallel_executor.submit(
            network_node_stub,
            AcquisitionRequest(
                channel="network_node",
                prompt=query_to_use,
                provider="stub",
                request_id=trace_id,
                enabled_by_user=False,
            ),
        )

        log("[Local] Фоновая генерация независимого ответа...")

        cost["acq_local_start_ms"] = (time.time() - t_start) * 1000
        local_future = parallel_executor.submit(
            generate_local_answer,
            query_to_use,
            query_to_use,
        )

        # ------------------------------------------------------------
        # REGISTRY RESULT
        # ------------------------------------------------------------
        search_result, dt, registry_timed_out = step_timer(
            "local_search",
            lambda: registry_future.result(timeout=30),
        )
        cost["acq_registry_finish_ms"] = (time.time() - t_start) * 1000

        # ------------------------------------------------------------
        # WEB QUERY RESULT
        # ------------------------------------------------------------
        wq_timed_out = False

        if web_future:
            wq_result, web_dt, wq_timed_out = step_timer(
                "web_query",
                lambda: web_future.result(timeout=30),
            )
            cost["acq_web_query_finish_ms"] = (time.time() - t_start) * 1000

            if wq_timed_out or wq_result is None:
                log(
                    "[Web Query] timeout/failure — "
                    "используем enriched query fallback"
                )
                wq_result = WebQueryResult(
                    queries=[enrich_result.enriched],
                    raw="[orchestrator fallback]",
                )

        # ------------------------------------------------------------
        # REFUTATION RESULT
        # ------------------------------------------------------------
        if refutation_future:
            try:
                refutation_result = refutation_future.result(
                    timeout=30
                )
                cost["acq_refutation_query_finish_ms"] = (time.time() - t_start) * 1000

                log(
                    f"[Refutation] Найдено опровержений: "
                    f"{len(refutation_result.queries) if refutation_result else 0}"
                )

                if (
                    refutation_result
                    and refutation_result.queries
                ):
                    log(
                        "[Refutation] Setting refutation_queries: "
                        + str(refutation_result.queries)
                    )

                    query_frame["refutation_queries"] = (
                        refutation_result.queries
                    )

                    trace.add_observation(
                        "refutation_queries",
                        refutation_result.queries,
                    )

            except Exception as e:
                log(
                    f"[Refutation] Ошибка: "
                    f"{type(e).__name__}: {e}"
                )

        try:
            node_payload = node_future.result(timeout=1)
            cost["acq_node_finish_ms"] = (time.time() - t_start) * 1000
            query_frame["network_node_observation"] = node_payload
            trace.add_observation("network_node_observation", node_payload)
        except Exception as e:
            cost["acq_node_finish_ms"] = (time.time() - t_start) * 1000
            log(f"[Network Node] stub unavailable: {type(e).__name__}: {e}")

        # Старый timed_out ниже относится к registry.
        timed_out = registry_timed_out

        cost["registry_ms"] = (time.time() - t0) * 1000
        registry.update_latency("local_search", dt)

        if timed_out or search_result is None:
            log("  ⚠ Timeout")
            search_result = SearchResult(docs=[], confidence=0.0, source="local", top_k=0)
        else:
            log(f"  · confidence={search_result.confidence:.3f}, docs={len(search_result.docs)}")
            trace.add_confidence("registry", search_result.confidence, f"найдено {len(search_result.docs)} документов")

        trace.add_execution("registry", "completed" if not timed_out else "timeout", cost["registry_ms"],
                            {"docs": len(search_result.docs), "confidence": search_result.confidence})
        trace.add_reasoning("registry",
                            {"docs": len(search_result.docs), "confidence": search_result.confidence},
                            "use_web" if search_result.confidence < CONF_THRESHOLD else "use_registry",
                            [
                                {"option": "use_registry", "accepted": search_result.confidence >= CONF_THRESHOLD,
                                 "reason": f"confidence {search_result.confidence:.3f} >= {CONF_THRESHOLD}"},
                                {"option": "use_web", "accepted": search_result.confidence < CONF_THRESHOLD,
                                 "reason": f"confidence {search_result.confidence:.3f} < {CONF_THRESHOLD}"}
                            ])

        add_decision_event(
            event_type="ExecutionStep",
            trace_id=trace_id,
            entity_type="orchestrator",
            entity_id="local_search",
            verdict=f"conf={search_result.confidence:.3f}",
            reason=f"docs={len(search_result.docs)}",
            meta={"decision_id": decision_id}
        )

        # ── [7] Epistemic-based web search decision ──────────────────────────
        web_result = None
        web_used = False
        web_skipped_reason = ""
        rejected_sources = []

        should_use_web = (
            enable_web
            and (not plan.skip_internet or enable_web)
            and epistemic_result.should_use_web
            and (search_result.confidence < CONF_THRESHOLD or enable_web)
        )

        if strategy_result.strategy == SearchStrategy.GAME_PROFILE:
            should_use_web = True
            log("[Strategy] Игровой профиль — включаю поиск по игровым источникам")

        if epistemic_result.testability in ["interpretive", "non_falsifiable"] and epistemic_result.domain != "media_interpretation":
            should_use_web = False
            web_skipped_reason = f"testability={epistemic_result.testability} (нужен обзор позиций, а не факты)"

        if should_use_web:
            log("[7] Web search...")
            t0 = time.time()
            dt = web_dt
            timed_out = wq_timed_out
            registry.update_latency("web_query", dt)

            if not timed_out and wq_result:
                log(f"  · queries: {wq_result.queries}")

                for i, q in enumerate(wq_result.queries[:3]):
                    trace.add_reasoning(f"query_evolution_{i}", {"query": q}, "generated", [])

                # P4 (web budget 3+3): stage 6 main side now gets its
                # own hard fetch budget (STAGE6_MAIN_BUDGET=3) instead
                # of the old scrape()'s default MAX_RESULTS-per-query
                # with no real fetch cap (see orch_web_scraper.py
                # scrape_budgeted_side() docstring for why this is a
                # standalone call rather than sharing one function
                # with the counter/refutation call in synthesis.py).
                cost["acq_web_main_start_ms"] = (time.time() - t_start) * 1000
                main_scrape_future = parallel_executor.submit(
                    scrape_budgeted_side,
                    wq_result.queries[:3],
                    STAGE6_MAIN_BUDGET,
                    fetch_cache=_request_fetch_cache,
                    side="main",
                    scope="initial",
                )
                counter_scrape_future = None
                if query_frame.get("refutation_queries"):
                    cost["acq_web_counter_start_ms"] = (time.time() - t_start) * 1000
                    counter_scrape_future = parallel_executor.submit(
                        scrape_budgeted_side,
                        query_frame["refutation_queries"],
                        STAGE6_COUNTER_BUDGET,
                        fetch_cache=_request_fetch_cache,
                        side="counter",
                        scope="initial",
                    )

                web_result, dt, timed_out = step_timer(
                    "web_scrape",
                    lambda: main_scrape_future.result(timeout=30),
                )
                cost["web_ms"] = (time.time() - t0) * 1000
                cost["acq_web_main_wait_finish_ms"] = (time.time() - t_start) * 1000
                registry.update_latency("web_scrape", dt)

                if counter_scrape_future:
                    _counter_t0 = time.time()
                    refutation_scrape_result, dt_ref, timed_out_ref = step_timer(
                        "refutation_scrape",
                        lambda: counter_scrape_future.result(timeout=30),
                        timeout=30,
                    )
                    cost["profile_refutation_ms"] = (
                        time.time() - _counter_t0
                    ) * 1000
                    cost["acq_web_counter_wait_finish_ms"] = (time.time() - t_start) * 1000
                    if not timed_out_ref and refutation_scrape_result:
                        query_frame["_prefetched_refutation_result"] = refutation_scrape_result
                        query_frame["_prefetched_refutation_dt"] = dt_ref
                        if getattr(refutation_scrape_result, "snippets", None):
                            log(
                                f"[Refutation] prefetched snippets: "
                                f"{len(refutation_scrape_result.snippets)}"
                            )

                if web_result:
                    web_used = True
                    log(f"  · сниппетов: {len(web_result.snippets)}, символов: {web_result.total_chars}")

                    for i, s in enumerate(web_result.snippets[:5]):
                        used = i < 3
                        domain = s.url.split("/")[2] if "/" in s.url else ""
                        trace.add_source(
                            url=s.url,
                            domain=domain,
                            domain_score=0.8 if used else 0.3,
                            freshness=0.7,
                            authority=0.6 if used else 0.3,
                            used=used,
                            rejected_reason="" if used else "low_relevance"
                        )

                    if hasattr(web_result, "_rejected"):
                        rejected_sources = web_result._rejected
                        log(f"  · rejected: {len(rejected_sources)} источников")
                        for r in rejected_sources[:3]:
                            trace.add_source(
                                url=r.get("url", ""),
                                domain=r.get("url", "").split("/")[2] if "/" in r.get("url", "") else "",
                                domain_score=0.1,
                                used=False,
                                rejected_reason=r.get("reason", "unknown")
                            )

                    trace.add_confidence("web", 0.7 if len(web_result.snippets) >= 3 else 0.5,
                                         f"найдено {len(web_result.snippets)} источников")

                    if len(web_result.snippets) == 0:
                        log("[Strategy] Web search не дал результатов — запоминаем неудачу")
                        strategy_router.record_result(strategy_result.strategy, 0, False)

                        if strategy_router.should_switch_strategy():
                            log("[Strategy] Смена стратегии...")
                            new_strategy_result = strategy_router.select_strategy(
                                query=query_to_use,
                                entity_info=entity_info,
                                intent_type=intent_type,
                                user_hint="игровой сектор X3",
                            )
                            log(f"[Strategy] Новая стратегия: {new_strategy_result.strategy.value}")
                else:
                    web_skipped_reason = "timeout"
            else:
                web_skipped_reason = "no queries"
        else:
            if not enable_web:
                web_skipped_reason = "disabled"
            elif plan.skip_internet:
                web_skipped_reason = "plan skip"
            elif not epistemic_result.should_use_web:
                web_skipped_reason = f"epistemic: {epistemic_result.domain} не требует web"
            elif search_result.confidence >= CONF_THRESHOLD:
                web_skipped_reason = f"registry confidence {search_result.confidence:.3f} >= {CONF_THRESHOLD}"
            else:
                web_skipped_reason = f"other: enable_web={enable_web}, should_use_web={epistemic_result.should_use_web}"
            log(f"[7] Web search — пропуск ({web_skipped_reason})")

        trace.add_execution("web_search", "used" if web_used else "skipped",
                            cost.get("web_ms", 0), {"used": web_used, "reason": web_skipped_reason})

        if external_ai_future:
            try:
                external_ai_started_at = t_start + cost["acq_external_ai_start_ms"] / 1000
                wait_timeout = max(0.0, external_ai_started_at + external_ai_deadline_s - time.time())
                external_ai_observations, external_ai_submit = external_ai_future.result(timeout=wait_timeout + 1.0)
                cost["acq_external_ai_finish_ms"] = (time.time() - t_start) * 1000
                query_frame["external_ai_observations"] = [
                    _serialize_acquisition_observation(obs)
                    for obs in external_ai_observations
                ]
                query_frame["external_ai_submit"] = external_ai_submit
                trace.add_observation("external_ai_observations", query_frame["external_ai_observations"])
                for obs in external_ai_observations:
                    if obs.provider:
                        prefix = f"ai_{obs.provider}"
                        cost[f"{prefix}_status"] = obs.status.value
                        if obs.started_at:
                            cost[f"{prefix}_start_ms"] = (obs.started_at - t_start) * 1000
                        if obs.finished_at:
                            cost[f"{prefix}_finish_ms"] = (obs.finished_at - t_start) * 1000
                        if obs.started_at and obs.finished_at:
                            cost[f"{prefix}_duration_ms"] = (obs.finished_at - obs.started_at) * 1000
                    persist_acquisition_observation(obs, run_id=trace_id)
                log(
                    "[External AI] raw observations: "
                    + ", ".join(
                        f"{obs.provider}={obs.status.value}/len={len(obs.raw_response or '')}"
                        for obs in external_ai_observations
                    )
                )
            except Exception as e:
                cost["acq_external_ai_finish_ms"] = (time.time() - t_start) * 1000
                log(f"[External AI] raw acquisition unavailable: {type(e).__name__}: {e}")

        add_decision_event(
            event_type="ExecutionStep",
            trace_id=trace_id,
            entity_type="orchestrator",
            entity_id="web_search",
            verdict="used" if web_used else "skipped",
            reason=web_skipped_reason,
            meta={"decision_id": decision_id, "epistemic_should_use_web": epistemic_result.should_use_web}
        )

    state_out = {
        "epistemic_result": epistemic_result,
        "intent_result": intent_result,
        "risk_result": risk_result,
        "enrich_result": enrich_result,
        "search_result": search_result,
        "web_result": web_result,
        "web_used": web_used,
        "parallel_executor": parallel_executor,
        "local_future": local_future,
        "clarification_answered": clarification_answered,
        "is_media_query": is_media_query,
        "enable_validation": enable_validation,
        "epistemic_trust_label": epistemic_trust_label,
        "query_to_use": query_to_use,
        "dt": dt,
        "_request_fetch_cache": _request_fetch_cache,
        "cache": cache,
    }

    return None, state_out
