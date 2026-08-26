"""
agent/orchestrator_v2.py — Orchestrator v2 с интеграцией существующих модулей.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass
class LocalSynthesisResult:
    answer: str
    trust_level: str
    confidence: float
    why_trust: list = None
    sources: list = None
    refutation_text: str = ""

import time
from pathlib import Path
import uuid

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from agent.orch_schemas import (
    OrchestratorRequest,
    OrchestratorResponse,
    SynthesisResult,
    ClaimRecord,
    TrustReport,
    CoverageReport,
    QueryTrace,
)
from agent.orch_synthesizer import synthesize
from agent.orch_tool_registry import get_registry
from agent.orch_session import get_context, add_message, new_session_id
from agent.orch_tracer import DecisionTracer, Trace
from agent.orch_reputation import add_decision_event, get_trace, get_ledger
from agent.orch_tag_tree import update_tree as tag_tree_update
from agent.orch_unanswered import record_unanswered, start_listener_daemon as _start_unanswered_listener

from agent.orchestrator.epistemic.existence_contract import apply_existence_query_contract
from agent.orchestrator.epistemic.final_coverage import evaluate_and_record_final_coverage
from agent.orchestrator.epistemic.trust_gate import (
    TRUST_STATES,
    apply_epistemic_trust_adjustment,
)
from agent.orchestrator.response.assembly import (
    _adapt_answer_to_style,
)
from agent.orchestrator.claims.status import (
    classify_claim_epistemic_status,
    evaluate_claim_status_gate,
    finalize_claim_trace_and_grounding,
)
from agent.orchestrator.claims.validation import apply_structural_claim_validation
from agent.orchestrator.claims.lifecycle import (
    setup_claim_and_evidence_lifecycle,
    update_beliefs_link_answer_and_personality_cycle,
)
from agent.orchestrator.claims.mapping import run_claim_evidence_batch, run_claim_evidence_mapping_pass1
from agent.orchestrator.claims.retrieval import apply_claim_resolution_and_second_retrieval
from agent.orchestrator.claims.disagreement import apply_claim_claim_disagreement
from agent.orchestrator.synthesis import build_frame_and_synthesize
from agent.orchestrator.pre_pipeline import run_pre_pipeline
from agent.orchestrator.pipeline import run_standard_pipeline
from agent.orchestrator.response.writeback import run_optimistic_respond

# ---- YANDI V3: SELF-AWARE SYSTEM ----
from agent.self_model import get_self_model
from agent.memory_episodic import get_memory
from agent.reflection_loop import get_reflection
from agent.motivation import get_motivation
from agent.core_loop import get_core_loop

# ---- YANDI V6: PERSONALITY & BELIEFS ----
from agent.claim_graph import get_claim_graph
from agent.claim_validator import get_claim_validator
from agent.belief_manager import get_belief_manager
from agent.claim_answer_linker import get_claim_answer_linker
from agent.disagreement_engine import get_disagreement_engine
from agent.personality_core import get_personality_core

from agent.boundaries import (
    is_apology,
)

# ---- RESEARCH ENGINE ----
from agent.research_engine import get_research_engine

from agent.claim_relation import (
    infer_claim_relation,
)

# ---- ROUTERS ----
from agent.intent_router import should_use_rag
from agent.strategy_router import SearchStrategy
from agent.object_resolver import get_object_resolver

# ============================================================
# ---- TRACER ----
_tracer = DecisionTracer()
# ---- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ YANDI V3/V6 ----
_self_model = None
_memory = None
_reflection = None
_motivation = None
_core_loop = None
_v3_initialized = False

_claim_graph = None
_claim_validator = None
_belief_manager = None
_claim_answer_linker = None
_disagreement_engine = None
_personality_core = None
_v6_initialized = False



_claim_graph = None
_claim_validator = None
_belief_manager = None
_claim_answer_linker = None
_disagreement_engine = None
_personality_core = None
_v6_initialized = False

def _init_v3():
    global _self_model, _memory, _reflection, _motivation, _core_loop, _v3_initialized
    global _claim_graph, _claim_validator, _belief_manager, _claim_answer_linker, _disagreement_engine, _personality_core, _v6_initialized

    if not _v3_initialized:
        try:
            _self_model = get_self_model()
            _memory = get_memory()
            _reflection = get_reflection()
            _motivation = get_motivation()
            _core_loop = get_core_loop()
            _core_loop.start_background(interval=60.0)
            _v3_initialized = True
            print("[V3] YANDI V3 инициализирован")
        except Exception as e:
            print(f"[V3] Ошибка инициализации: {e}")
            _v3_initialized = True

    if not _v6_initialized:
        try:
            _claim_graph = get_claim_graph()
            _claim_validator = get_claim_validator()
            _belief_manager = get_belief_manager()
            _claim_answer_linker = get_claim_answer_linker()
            _disagreement_engine = get_disagreement_engine()
            _personality_core = get_personality_core()
            _v6_initialized = True
            print("[V6] YANDI V6 инициализирован")
        except Exception as e:
            print(f"[V6] Ошибка инициализации: {e}")
            _v6_initialized = True

    return _self_model, _memory, _reflection, _motivation, _core_loop

# _start_unanswered_listener()



# ============================================================
# SELF-QUERY DETECTION
# ============================================================

def load_core_identity() -> str:
    identity_path = BASE / "registry" / "yandi_core_identity.txt"
    if identity_path.exists():
        try:
            return identity_path.read_text(encoding="utf-8")
        except Exception:
            pass
    return ""

def process(
    request: OrchestratorRequest,
    verbose: bool = False,
    enable_web: bool = False,
    enable_validation: bool = False,
    enable_cache: bool = True,
    clarify_callback=None,
) -> OrchestratorResponse:
    try:
        self_model, memory, reflection, motivation, core_loop = _init_v3()
    except Exception:
        self_model = memory = reflection = motivation = core_loop = None
    query_frame = {}

    t_start = time.time()
    query = request.query
    registry = get_registry()
    ctx = request.context or get_context(request.session_id)

    trace_id = f"trace_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    decision_id = f"dec_{int(time.time())}_{uuid.uuid4().hex[:8]}"

    def log(msg: str):
        if verbose:
            # P0 (performance architecture pass, unaccounted=73.31s
            # investigation): every top-level log() call now carries
            # wall-clock elapsed-since-request-start. This reconstructs
            # a full timeline of the SEQUENTIAL top-level flow from the
            # log alone (post-hoc, deltas between consecutive lines) —
            # pure instrumentation, no behavior change. Sub-phase costs
            # already tracked in cost[...] (claim_setup_ms etc.) stay
            # as they are; this is for finding gaps BETWEEN them.
            print(f"[t+{time.time() - t_start:7.2f}s] {msg}", flush=True)

    def _claims_conflict(text1: str, text2: str) -> bool:
        """
        Проверяет логическое противоречие между двумя claims.

        Конфликт регистрируется только при явном contradicts,
        полученном от основного LLM NLI.

        Аварийный fallback не имеет права создавать спор:
        лучше пропустить потенциальный конфликт, чем выдумать ложный.
        """
        if not text1 or not text2:
            return False

        if text1.strip() == text2.strip():
            return False

        result = infer_claim_relation(text1, text2)

        relation = result.get("relation")
        method = result.get("method")

        if verbose:
            log(
                f"[Claim↔Claim NLI] "
                f"relation={relation} method={method}"
            )

        return (
            method == "llm_nli"
            and relation == "contradicts"
        )

    log(f"\n{'='*60}")
    log(f"Orchestrator v2 | {query[:80]}")
    log(f"{'='*60}")

    trace = Trace(
        trace_id=trace_id,
        timestamp=time.time(),
        query=query,
        goal="получить достоверный ответ на вопрос пользователя",
    )
    cost = {}

    add_decision_event(
        event_type="DecisionStarted",
        trace_id=trace_id,
        entity_type="decision",
        entity_id=decision_id,
        verdict="STARTED",
        reason=f"Query: {query[:100]}",
        domain="general",
        meta={"query": query[:200]}
    )

    # Pre-pipeline — extracted to agent/orchestrator/pre_pipeline.py
    # (structural extraction; behavior unchanged). 11 short-circuit
    # early-return points preserved in the same strict order; see the
    # module docstring there for the free-variable / return-protocol
    # audit this extraction is based on.
    early_response, _pre_pipeline_state = run_pre_pipeline(
        request, verbose, t_start, query, log, trace, cost, _tracer
    )
    if early_response is not None:
        return early_response

    query_to_use = _pre_pipeline_state["query_to_use"]
    state = _pre_pipeline_state["state"]
    char = _pre_pipeline_state["char"]
    context_registry = _pre_pipeline_state["context_registry"]
    intent_type = _pre_pipeline_state["intent_type"]
    intent_confidence = _pre_pipeline_state["intent_confidence"]
    entity_info = _pre_pipeline_state["entity_info"]
    strategy_router = _pre_pipeline_state["strategy_router"]
    strategy_result = _pre_pipeline_state["strategy_result"]
    _skip_rag = _pre_pipeline_state["_skip_rag"]
    _bad_state_prefix = _pre_pipeline_state["_bad_state_prefix"]
    enrich_result = _pre_pipeline_state["enrich_result"]
    search_result = _pre_pipeline_state["search_result"]
    web_result = _pre_pipeline_state["web_result"]
    web_used = _pre_pipeline_state["web_used"]
    synthesis_result = _pre_pipeline_state["synthesis_result"]
    reasoning_info = _pre_pipeline_state["reasoning_info"]
    claims_data = _pre_pipeline_state["claims_data"]
    evidence_data = _pre_pipeline_state["evidence_data"]
    semantic_grounding_score = _pre_pipeline_state["semantic_grounding_score"]
    epistemic_grounding_score = _pre_pipeline_state["epistemic_grounding_score"]
    support_grounding_score = _pre_pipeline_state["support_grounding_score"]
    final_claim_coverage_score = _pre_pipeline_state["final_claim_coverage_score"]
    final_claims_count = _pre_pipeline_state["final_claims_count"]
    final_claims_covered = _pre_pipeline_state["final_claims_covered"]
    final_claims_uncovered = _pre_pipeline_state["final_claims_uncovered"]
    entity = _pre_pipeline_state["entity"]
    is_subjective_answer = _pre_pipeline_state["is_subjective_answer"]
    _exact_mode = _pre_pipeline_state["_exact_mode"]

    # ============================================================
    # 12. СТАНДАРТНЫЙ ПАЙПЛАЙН С ПОИСКОМ
    # ============================================================

    # Standard pipeline phases [0]-[7] — extracted to
    # agent/orchestrator/pipeline.py (structural extraction; behavior
    # unchanged). Wrapped as one function deliberately (see module
    # docstring there) to avoid a cross-phase t0 timer-reuse issue found
    # while scoping this extraction.
    early_response, _pipeline_state = run_standard_pipeline(
        request, enable_web, enable_validation, enable_cache, clarify_callback,
        t_start, query_frame, ctx, memory, self_model,
        log, trace, trace_id, decision_id, cost, registry, _tracer,
        query_to_use, char, state, intent_type, entity_info,
        strategy_router, strategy_result, _skip_rag, _exact_mode,
        is_subjective_answer, enrich_result,
    )
    if early_response is not None:
        return early_response

    epistemic_result = _pipeline_state["epistemic_result"]
    intent_result = _pipeline_state["intent_result"]
    risk_result = _pipeline_state["risk_result"]
    enrich_result = _pipeline_state["enrich_result"]
    search_result = _pipeline_state["search_result"]
    web_result = _pipeline_state["web_result"]
    web_used = _pipeline_state["web_used"]
    parallel_executor = _pipeline_state["parallel_executor"]
    local_future = _pipeline_state["local_future"]
    clarification_answered = _pipeline_state["clarification_answered"]
    is_media_query = _pipeline_state["is_media_query"]
    enable_validation = _pipeline_state["enable_validation"]
    epistemic_trust_label = _pipeline_state["epistemic_trust_label"]
    query_to_use = _pipeline_state["query_to_use"]
    dt = _pipeline_state["dt"]
    _request_fetch_cache = _pipeline_state["_request_fetch_cache"]
    cache = _pipeline_state["cache"]

    # Frame construction + synthesize() — extracted to
    # agent/orchestrator/synthesis.py (structural extraction;
    # behavior unchanged).
    (
        synthesis_result, reasoning_info, synthesis_timed_out,
        refutation_snippets, entity, answer_mode, _prof_synthesis_t0,
    ) = build_frame_and_synthesize(
        query_frame, epistemic_result, is_subjective_answer, is_media_query,
        intent_type, strategy_result, _skip_rag, request, web_result,
        search_result, query_to_use, local_future, parallel_executor,
        _request_fetch_cache, enrich_result, trace, cost, log, verbose,
    )

    trace.set_query_trace(QueryTrace(
        trace_id=trace_id,
        session_id=request.session_id or "",
        query_text=query_to_use,
        query_normalized=query_to_use.lower(),
        intent=intent_result.intent if intent_result else "",
        query_type=epistemic_result.answer_mode if not is_subjective_answer else "analysis",
        start_ts=time.time(),
        end_ts=time.time(),
        final_status="completed" if synthesis_result else "failed",
    ))
    # ВАЖНО:
    # Старый t0 ставился ещё до refutation / graph / local wait /
    # blind analysis / source classification и поэтому synthesize_ms
    # фактически измерял весь этот блок.
    #
    # Теперь synthesize_ms = только реальный вызов synthesize().
    cost["synthesize_ms"] = (
        time.time() - _prof_synthesis_t0
    ) * 1000

    registry.update_latency("synthesize", dt if 'dt' in locals() else 0)
    registry.update_reliability(
        "synthesize",
        not synthesis_timed_out and synthesis_result is not None,
    )

    if synthesis_timed_out or synthesis_result is None:
        log("  ⚠ Timeout")
        synthesis_result = LocalSynthesisResult(
            answer="Не удалось сформировать ответ (timeout).",
            confidence=0.0, sources=[], trust_level="UNVERIFIED",
        )
        reasoning_info = {}
    else:
        log(f"  · trust={synthesis_result.trust_level}, conf={synthesis_result.confidence:.2f}, len={len(synthesis_result.answer)}")

        # Claim & evidence lifecycle setup — extracted to
        # agent/orchestrator/claims/lifecycle.py (structural extraction;
        # behavior unchanged).
        (
            trust_report_data,
            trust_reasons,
            coverage_report_data,
            claims_data,
            evidence_data,
            technical_errors,
        ) = setup_claim_and_evidence_lifecycle(
            reasoning_info, search_result, web_result, refutation_snippets,
            query_to_use, log, verbose,
        )

        # Structural claim validation — extracted to
        # agent/orchestrator/claims/validation.py (structural extraction;
        # behavior unchanged).
        #
        # G (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md, §G): весь блок
        # ClaimValidator + Mapper PASS1 + NLI PASS1 раньше был частью
        # 275.87s unaccounted. Оборачиваем целиком одним таймером —
        # детализация внутри не нужна для profile coverage, у каждого
        # этапа уже есть собственный print с деталями.
        _t0_claim_setup = time.time()

        claims_data, rejected_structural_claims = apply_structural_claim_validation(
            claims_data, _claim_validator, reasoning_info, trace, log, verbose
        )

        # Evidence mapping PASS1 — extracted to
        # agent/orchestrator/claims/mapping.py (structural extraction;
        # behavior unchanged).
        semantic_grounding_score = run_claim_evidence_mapping_pass1(
            claims_data, evidence_data, log, verbose
        )

        # Structural validation уже выполнена ДО Mapper.
        # Здесь начинаются только epistemic проверки claim ↔ evidence.


        # ---- CLAIM <-> EVIDENCE RELATION NLI ----
        #
        # Mapper определил semantic candidate links.
        # Все claim <-> evidence пары классифицируются централизованно
        # через batch helper.
        #
        # Helper:
        #   - строит пары claim/evidence;
        #   - сохраняет Source Quality metadata;
        #   - выполняет batch NLI;
        #   - записывает claim["evidence_relations"];
        #   - возвращает число классифицированных отношений.
        claim_relation_count = run_claim_evidence_batch(
            claims_data,
            evidence_data,
            "PASS1",
            log,
            verbose,
        )

        if verbose:
            log(
                f"[Claim Evidence NLI] "
                f"relations classified={claim_relation_count}"
            )

        cost["claim_setup_ms"] = (
            (time.time() - _t0_claim_setup) * 1000
        )

        # Claim Resolution Gate + second (claim-specific) retrieval pass —
        # extracted to agent/orchestrator/claims/retrieval.py (structural
        # extraction; behavior unchanged).
        evidence_data = apply_claim_resolution_and_second_retrieval(
            claims_data, evidence_data, enable_web, is_subjective_answer,
            _skip_rag, _request_fetch_cache, cost, log, verbose,
        )


        # Claim epistemic status classification — extracted to
        # agent/orchestrator/claims/status.py (structural extraction;
        # behavior unchanged).
        classify_claim_epistemic_status(claims_data, log, verbose)

        # Final claim trace + epistemic grounding — extracted to
        # agent/orchestrator/claims/status.py (structural extraction;
        # behavior unchanged).
        epistemic_grounding_score, support_grounding_score = finalize_claim_trace_and_grounding(
            claims_data, trace, rejected_structural_claims, semantic_grounding_score, log, verbose
        )

        # Belief update + claim<->answer linker + personality cycle —
        # extracted to agent/orchestrator/claims/lifecycle.py (structural
        # extraction; behavior unchanged).
        supporting_ids = update_beliefs_link_answer_and_personality_cycle(
            claims_data, synthesis_result, epistemic_result, is_subjective_answer,
            _belief_manager, _claim_answer_linker, _personality_core,
            cost, log, verbose,
        )

        # Claim<->claim disagreement — extracted to
        # agent/orchestrator/claims/disagreement.py (structural
        # extraction; behavior unchanged).
        apply_claim_claim_disagreement(
            claims_data, _disagreement_engine, epistemic_result,
            is_subjective_answer, cost, log, verbose,
        )

        # Final Claim Coverage — extracted to
        # agent/orchestrator/epistemic/final_coverage.py (structural
        # extraction; behavior unchanged).
        (
            final_claim_coverage_score,
            final_claims_count,
            final_claims_covered,
            final_claims_uncovered,
        ) = evaluate_and_record_final_coverage(
            synthesis_result, claims_data, query_to_use, cost, trace, log, verbose
        )

        # Эпистемическая корректировка trust (v3) — extracted to
        # agent/orchestrator/epistemic/trust_gate.py (structural
        # extraction; behavior unchanged).
        label = apply_epistemic_trust_adjustment(
            is_subjective_answer,
            epistemic_trust_label,
            epistemic_result,
            entity,
            final_claim_coverage_score,
            support_grounding_score,
            _belief_manager,
            trace,
            web_used,
            claims_data,
            search_result,
            epistemic_grounding_score,
            clarification_answered,
            is_media_query,
            supporting_ids,
            coverage_report_data,
            intent_result,
        )

    trace.add_execution("synthesize", "completed" if not synthesis_timed_out else "timeout", cost["synthesize_ms"],
                        {"trust": synthesis_result.trust_level, "confidence": synthesis_result.confidence})

    add_decision_event(
        event_type="ExecutionStep",
        trace_id=trace_id,
        entity_type="orchestrator",
        entity_id="synthesize",
        verdict=synthesis_result.trust_level,
        reason=f"conf={synthesis_result.confidence:.2f}, epistemic={epistemic_result.domain if not is_subjective_answer else 'subjective'}, cap={epistemic_result.max_trust_cap if not is_subjective_answer else 'N/A'}, objectivity={epistemic_result.objectivity_score if not is_subjective_answer else 0.5}",
        meta={"decision_id": decision_id, "is_science_as_model": epistemic_result.is_science_as_model if not is_subjective_answer else False}
    )

    # ---- РЕГИСТРИРУЕМ КОНТЕКСТ ----
    if synthesis_result and synthesis_result.answer:
        topic = epistemic_result.domain if not is_subjective_answer else "subjective_analysis"
        context_registry.register(
            query=query_to_use,
            response=synthesis_result.answer[:500],
            topic=topic,
            type=answer_mode,
            source="yandi"
        )
        log(f"[Context] Зарегистрирован контекст по теме: {topic}")

    # ---- АДАПТАЦИЯ ОТВЕТА ПОД СТИЛЬ ----
    if synthesis_result and synthesis_result.answer:
        synthesis_result.answer = _adapt_answer_to_style(synthesis_result.answer, state)

    # ── [9] CLAIM STATUS GATE ────────────────────────────────────────────────
    #
    # Эпистемические статусы:
    #
    # verified      — подтверждён более сильной процедурой проверки
    # supported     — есть evidence SUPPORTS, но это ещё не verified
    # disputed      — есть одновременно supports и contradicts
    # contradicted  — есть contradicts и нет supports
    # candidate     — ещё не прошёл evidence relation stage
    # unverified    — поддержки/опровержения не найдено
    # rejected      — структурно непригодный claim
    #
    if not _skip_rag and not is_subjective_answer and synthesis_result:
        # Claim Status Gate — extracted to
        # agent/orchestrator/claims/status.py (structural extraction;
        # behavior unchanged). Guard stays here on purpose: downstream
        # code checks 'claims_accepted' in locals() to detect whether
        # this gate ran (see evaluate_claim_status_gate's docstring).
        claims_accepted, total_claims, claims_rejected = evaluate_claim_status_gate(
            claims_data, synthesis_result, log
        )

        # Existence Query Contract — extracted to
        # agent/orchestrator/epistemic/existence_contract.py (P0-A,
        # structural extraction; behavior unchanged).
        apply_existence_query_contract(
            query_to_use, claims_data, total_claims, synthesis_result, log
        )

    # Optimistic respond ([10]) — extracted to
    # agent/orchestrator/response/writeback.py (structural extraction;
    # behavior unchanged). Optional params below replicate the original
    # 'X' in locals() checks at the call site, in process()'s own scope
    # (see writeback.py's module docstring for why each is genuinely
    # sometimes-undefined, not just a defensive check).
    return run_optimistic_respond(
        request, verbose, enable_validation, enable_cache, t_start, query_frame,
        log, trace, trace_id, decision_id, cost, cache, _request_fetch_cache,
        query_to_use, _skip_rag, is_subjective_answer, epistemic_result,
        synthesis_result, risk_result, intent_result, search_result, web_used,
        claims_data, evidence_data, self_model, memory, reflection, motivation,
        core_loop, reasoning_info, intent_type, intent_confidence, _bad_state_prefix,
        entity, enrich_result, _tracer,
        supporting_ids=supporting_ids if 'supporting_ids' in locals() else None,
        technical_errors=technical_errors if 'technical_errors' in locals() else None,
        claims_accepted=claims_accepted if 'claims_accepted' in locals() else None,
        claims_rejected=claims_rejected if 'claims_rejected' in locals() else None,
        total_claims=total_claims if 'total_claims' in locals() else None,
    )


def interactive(
    enable_web: bool = False,
    enable_validation: bool = False,
    enable_cache: bool = True,
):
    print(f"Orchestrator v2 — интерактивный режим (web={'вкл' if enable_web else 'выкл'}, validation={'вкл' if enable_validation else 'выкл'})")
    print("exit/quit для выхода\n")
    session_id = new_session_id()
    print(f"Сессия: {session_id}")

    def clarify_callback(formatted: str) -> dict:
        print(f"\n{formatted}")
        answers = {}
        for line in formatted.split("\n"):
            if line.strip().startswith(tuple("123")):
                try:
                    num = int(line[0])
                    answer = input(f"  Ответ {num}: ").strip()
                    if answer:
                        answers[f"param_{num}"] = answer
                except Exception:
                    pass
        return answers

    while True:
        try:
            query = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query or query.lower() in ("exit", "quit"):
            break
        req = OrchestratorRequest(query=query, session_id=session_id)
        resp = process(
            req,
            verbose=True,
            enable_web=enable_web,
            enable_validation=enable_validation,
            enable_cache=enable_cache,
            clarify_callback=clarify_callback,
        )
        print(f"\n{'─'*60}")
        print(resp.answer)
        print(f"\nLatency: {resp.latency_total}s | Trust: {resp.trust_level}")


if __name__ == "__main__":
    web = "--web" in sys.argv
    validate = "--validate" in sys.argv
    cache_enabled = "--no-cache" not in sys.argv
    if "--interactive" in sys.argv or "-i" in sys.argv:
        interactive(
            enable_web=web,
            enable_validation=validate,
            enable_cache=cache_enabled,
        )
    elif len(sys.argv) > 1:
        q = " ".join(a for a in sys.argv[1:] if not a.startswith("-"))
        if not q:
            q = "Как работает DHT?"
        req = OrchestratorRequest(query=q)
        resp = process(
            req,
            verbose=True,
            enable_web=web,
            enable_validation=validate,
            enable_cache=cache_enabled,
        )
        print(f"\n{'─'*60}")
        print(resp.answer)
        print(f"\nLatency: {resp.latency_total}s | Trust: {resp.trust_level}")
    else:
        print("Использование:")
        print('  python3 agent/orchestrator_v2.py "Вопрос"')
        print('  python3 agent/orchestrator_v2.py "Вопрос" --web [--validate] [--no-cache]')
        print('  python3 agent/orchestrator_v2.py --interactive [--web] [--validate] [--no-cache]')
