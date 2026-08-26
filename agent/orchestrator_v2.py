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

import threading
import time
from datetime import datetime
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
    OutcomeRecord,
    QueryTrace,
)
from agent.orch_synthesizer import synthesize
from agent.orch_optimistic import get_responder
from agent.orch_tool_registry import get_registry
from agent.orch_session import get_context, add_message, new_session_id
from agent.orch_node_selector import select_nodes, select_nodes_federated, _should_use_federation
from agent.orch_validator import validate_parallel
from agent.orch_arbiter import arbitrate
from agent.orch_knowledge_writer import write_from_arbiter
from agent.orch_monitoring import record as mon_record
from agent.orch_tracer import DecisionTracer, Trace
from agent.orch_reputation import add_decision_event, get_trace, get_ledger
from agent.orch_query_archive import record_query as archive_query
from agent.orch_tag_tree import update_tree as tag_tree_update
from agent.orch_unanswered import record_unanswered, start_listener_daemon as _start_unanswered_listener

from agent.claim_evidence_mapper import map_claims_to_evidence, get_claim_grounding_score
from agent.orchestrator.epistemic.existence_contract import apply_existence_query_contract
from agent.orchestrator.epistemic.final_coverage import evaluate_and_record_final_coverage
from agent.orchestrator.runtime.profiling import report_pipeline_profile
from agent.orchestrator.epistemic.trust_gate import (
    TRUST_STATES,
    _calculate_delta_factors,
    apply_epistemic_trust_adjustment,
)
from agent.orchestrator.response.assembly import (
    _adapt_answer_to_style,
)
from agent.orchestrator.claims.status import (
    classify_claim_epistemic_status,
    evaluate_claim_status_gate,
)
from agent.orchestrator.claims.validation import apply_structural_claim_validation
from agent.orchestrator.claims.lifecycle import (
    setup_claim_and_evidence_lifecycle,
    update_beliefs_link_answer_and_personality_cycle,
)
from agent.orchestrator.claims.mapping import run_claim_evidence_batch
from agent.orchestrator.claims.retrieval import apply_claim_resolution_and_second_retrieval
from agent.orchestrator.claims.disagreement import apply_claim_claim_disagreement
from agent.orchestrator.synthesis import build_frame_and_synthesize
from agent.orchestrator.pre_pipeline import run_pre_pipeline
from agent.orchestrator.pipeline import run_standard_pipeline

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

# ---- EXPERIENCE MEMORY ----
from agent.experience_memory import get_experience_memory
from agent.claim_relation import (
    infer_claim_relation,
)
from agent.dataset_builder import get_dataset_builder

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

_DOMAIN_TAG: dict[str, str] = {
    "general": "general",
    "legal": "legal",
    "medical": "health:medical",
    "financial": "finance",
    "coding": "tech:coding",
    "science": "science",
    "tech": "tech",
    "ai_ml": "tech:ai",
    "cooking": "lifestyle:cooking",
    "travel": "travel",
    "sport": "lifestyle:sport",
    "music": "culture:music",
    "history": "culture:history",
    "education": "education",
    "ecology": "science:ecology",
    "psychology": "health:psychology",
    "geography": "science:geography",
    "literature": "culture:literature",
}


def _build_tags(intent_result, enrich_result, query: str = "") -> list[str]:
    domain = getattr(intent_result, "intent", "general") or "general"
    base = _DOMAIN_TAG.get(domain, domain)
    if base == "general" and query:
        q_lower = query.lower()
        keywords = [
            ("рецепт", "lifestyle:cooking"),
            ("путешест", "travel:tourism"),
            ("гора", "science:geography"),
            ("закон", "legal"),
            ("симптом", "health:medical"),
            ("акция", "finance"),
            ("код", "tech:coding"),
            ("нейросет", "tech:ai"),
        ]
        for kw, tag in keywords:
            if kw in q_lower:
                base = tag
                break
    tags = [base]
    return tags[:3]


def _background_validate(
    question: str,
    answer: str,
    synthesis,
    risk,
    intent_result,
    validation_id: str,
    decision_id: str,
    trace_id: str,
    domain: str,
    search_result,
    web_used: bool,
    verbose: bool,
):
    def log(msg):
        if verbose:
            print(msg, flush=True)

    log(f"\n[BG:{validation_id[:6]}] Старт валидации...")

    try:
        add_decision_event(
            event_type="ValidationStarted",
            trace_id=trace_id,
            entity_type="route",
            entity_id="registry_first",
            verdict="VERIFYING",
            reason=f"ValidationStarted: {validation_id}",
            domain=domain,
            meta={"decision_id": decision_id}
        )

        nodes = select_nodes_federated(risk, domain=domain) if _should_use_federation() else select_nodes(risk, domain=domain)
        log(f"[BG] Ноды: {[n.node_id for n in nodes.nodes]}")

        val_result = validate_parallel(question, answer, nodes, domain=domain)
        log(f"[BG] agree={val_result.agree_count} disagree={val_result.disagree_count}")

        use_llm = risk.risk_level in ("medium", "high", "critical")
        arb = arbitrate(question, answer, val_result, use_llm=use_llm)

        verdict = arb.verdict
        log(f"[BG] Вердикт: {verdict} — {arb.explanation}")

        add_decision_event(
            event_type="ValidationFinished",
            trace_id=trace_id,
            entity_type="route",
            entity_id="registry_first",
            verdict=verdict,
            confidence=synthesis.confidence if synthesis else 0.5,
            reason=f"ValidationFinished: verdict={verdict}",
            domain=domain,
            meta={"decision_id": decision_id}
        )

        if verdict in ("VERIFIED", "PARTIALLY_VERIFIED"):
            write_from_arbiter(question, synthesis, arb, topic=domain)
            log(f"[BG] Записано в knowledge registry ({verdict})")
            add_decision_event(
                event_type="KnowledgeStored",
                trace_id=trace_id,
                entity_type="knowledge",
                entity_id="registry",
                verdict=verdict,
                reason=f"KnowledgeStored: {verdict}",
                domain=domain,
                meta={"decision_id": decision_id}
            )

        delta_factors = _calculate_delta_factors(
            verification_verdict=verdict,
            confidence=synthesis.confidence if synthesis else 0.5,
            has_sources=bool(synthesis.sources if synthesis else []),
            consensus_agreement=val_result.agree_count,
            total_nodes=len(nodes.nodes) if nodes else 0,
        )

        add_decision_event(
            event_type="ReputationUpdated",
            trace_id=trace_id,
            entity_type="route",
            entity_id="registry_first",
            verdict=verdict,
            confidence=synthesis.confidence if synthesis else 0.5,
            delta=delta_factors["total"],
            delta_factors=delta_factors,
            reason=f"ReputationUpdated: verdict={verdict}",
            domain=domain,
            meta={
                "decision_id": decision_id,
                "agree_count": val_result.agree_count,
                "disagree_count": val_result.disagree_count,
            }
        )

        add_decision_event(
            event_type="ReputationUpdated",
            trace_id=trace_id,
            entity_type="model",
            entity_id="heretic:q8",
            verdict=verdict,
            confidence=synthesis.confidence if synthesis else 0.5,
            delta=delta_factors["total"],
            reason=f"ReputationUpdated: verdict={verdict}",
            domain=domain,
            meta={"decision_id": decision_id}
        )

        if search_result and search_result.docs:
            add_decision_event(
                event_type="ReputationUpdated",
                trace_id=trace_id,
                entity_type="source",
                entity_id="local_registry",
                verdict=verdict,
                confidence=synthesis.confidence if synthesis else 0.5,
                delta=delta_factors["total"],
                reason=f"ReputationUpdated: docs={len(search_result.docs)}",
                domain=domain,
                meta={"decision_id": decision_id, "docs_count": len(search_result.docs)}
            )

        if web_used:
            add_decision_event(
                event_type="ReputationUpdated",
                trace_id=trace_id,
                entity_type="source",
                entity_id="web_search",
                verdict=verdict,
                confidence=synthesis.confidence if synthesis else 0.5,
                delta=delta_factors["total"],
                reason=f"ReputationUpdated: web_used=True",
                domain=domain,
                meta={"decision_id": decision_id, "web_used": True}
            )

        get_responder().on_validation_done(validation_id, verdict, arb.explanation)

        for v in val_result.validations:
            mon_record("validate", v.latency, v.verdict != "disagree")

        log(f"[BG] Репутация обновлена: {delta_factors['total']:+.3f}")

    except Exception as e:
        log(f"[BG] Ошибка валидации: {e}")
        add_decision_event(
            event_type="ValidationFailed",
            trace_id=trace_id,
            entity_type="route",
            entity_id="registry_first",
            verdict="REJECTED",
            reason=f"ValidationFailed: {str(e)[:100]}",
            domain=domain,
            meta={"decision_id": decision_id}
        )


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

        # ============================================================
        # EVIDENCE MAPPING
        # ============================================================
        #
        # Mapper видит:
        #
        #   initial evidence
        #       +
        #   claim-specific evidence
        #
        # но сам решает semantic candidate links.
        mapped_claims = map_claims_to_evidence(
            claims_data,
            evidence_data,
        )

        # ------------------------------------------------------------
        # CLAIM <-> EVIDENCE SINGLE SOURCE OF TRUTH
        # ------------------------------------------------------------
        #
        # map_claims_to_evidence() — единственный компонент,
        # который имеет право назначать derived_from_evidence_ids.
        #
        # Важно вернуть результат mapping обратно в claims_data.
        # Иначе trace видел бы правильные связи, а Validator,
        # BeliefManager и Linker продолжали бы работать со старой
        # версией claims.
        # ------------------------------------------------------------

        mapped_by_id = {
            mc.claim_id: mc
            for mc in mapped_claims
            if getattr(mc, "claim_id", None)
        }

        for claim in claims_data:
            claim_id = claim.get("claim_id")
            mapped = mapped_by_id.get(claim_id)

            if mapped is None:
                # Если mapper не смог обработать claim,
                # связь не выдумываем.
                claim["derived_from_evidence_ids"] = []
                claim["verification_status"] = "candidate"
                continue

            claim["derived_from_evidence_ids"] = list(
                mapped.derived_from_evidence_ids or []
            )

            # candidate означает:
            # evidence тематически привязан, но истинность claim
            # ещё НЕ установлена.
            claim["verification_status"] = (
                mapped.verification_status or "candidate"
            )

        # ВАЖНО:
        # mapped_claims здесь ещё имеют промежуточный status=candidate.
        # В trace они будут записаны ПОСЛЕ Claim Evidence NLI
        # и вычисления окончательного epistemic status.

        semantic_grounding_score = get_claim_grounding_score(
            mapped_claims
        )

        mapped_with_evidence = sum(
            1
            for mc in mapped_claims
            if getattr(mc, "derived_from_evidence_ids", None)
        )

        total_candidate_links = sum(
            len(getattr(mc, "derived_from_evidence_ids", None) or [])
            for mc in mapped_claims
        )

        log(
            f"[Evidence Mapper] "
            f"claims={len(claims_data)}, "
            f"processed={len(mapped_claims)}, "
            f"linked_claims={mapped_with_evidence}, "
            f"candidate_links={total_candidate_links}, "
            f"semantic_grounding={semantic_grounding_score:.2f}"
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

        # ============================================================
        # FINAL CLAIM TRACE
        # ============================================================
        #
        # Trace получает claim только ПОСЛЕ:
        #
        #   structural validation
        #   semantic mapping
        #   Claim ↔ Evidence NLI
        #   Source Quality Gate
        #   epistemic status calculation
        #
        # Поэтому trace больше не хранит устаревший candidate status
        # вместо supported/contradicted/disputed/unverified.
        traced_claim_ids = set()

        for claim in claims_data:
            claim_id = claim.get("claim_id")

            if claim_id and claim_id in traced_claim_ids:
                continue

            trace.add_claim_raw(claim)

            if claim_id:
                traced_claim_ids.add(claim_id)

        if verbose:
            log(
                f"[Claim Trace] final={len(claims_data)} "
                f"rejected={len(rejected_structural_claims)}"
            )

        # ====================================================
        # EPISTEMIC GROUNDING
        # ====================================================
        #
        # В denominator не включаем structural rejected claims:
        # они не являются содержательными claims ответа,
        # пригодными для evidence-проверки.
        #
        # epistemic_grounding:
        #   direct+eligible supports ИЛИ contradicts.
        #
        # support_grounding:
        #   direct+eligible supports.
        #
        # ВАЖНО:
        # высокий epistemic_grounding сам по себе НЕ повышает
        # Trust. Evidence может полностью противоречить ответу.
        effective_claims = [
            claim
            for claim in claims_data
            if claim.get("verification_status") != "rejected"
        ]

        if effective_claims:
            epistemically_grounded_claims = sum(
                1
                for claim in effective_claims
                if (
                    int(claim.get("support_count", 0) or 0) > 0
                    or
                    int(
                        claim.get(
                            "contradiction_count",
                            0,
                        ) or 0
                    ) > 0
                )
            )

            support_grounded_claims = sum(
                1
                for claim in effective_claims
                if int(claim.get("support_count", 0) or 0) > 0
            )

            epistemic_grounding_score = (
                epistemically_grounded_claims
                / len(effective_claims)
            )

            support_grounding_score = (
                support_grounded_claims
                / len(effective_claims)
            )

        else:
            epistemic_grounding_score = 0.0
            support_grounding_score = 0.0

        log(
            "[Grounding] "
            f"semantic={semantic_grounding_score:.2f} "
            f"epistemic={epistemic_grounding_score:.2f} "
            f"support={support_grounding_score:.2f}"
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

    # ── [10] Optimistic respond ─────────────────────────────────────────────
    log("[10] Optimistic respond...")
    responder = get_responder()
    validation_id = ""

    def _start_bg_validation(val_id: str):
        nonlocal validation_id
        validation_id = val_id
        if not enable_validation:
            query_frame["external_validation_performed"] = False
            return
        if _skip_rag or is_subjective_answer:
            query_frame["external_validation_performed"] = False
            log(f"  · Валидация пропущена для субъективного интента")
            return
        if epistemic_result.testability in ["interpretive", "non_falsifiable"] and epistemic_result.domain != "media_interpretation":
            query_frame["external_validation_performed"] = False
            log(f"  · Валидация пропущена для {epistemic_result.testability} утверждения")
            return

        try:
            from agent.ai_validator_redis import send_to_deepseek
            ai_task_id = send_to_deepseek(
                query=query_to_use,
                answer=synthesis_result.answer,
                frame={"epistemic": epistemic_result.__dict__},
                sources=synthesis_result.sources,
            )
            query_frame["external_validation_performed"] = True
            log(f"  · AI валидация отправлена в DeepSeek (ID: {ai_task_id})")
        except Exception as e:
            query_frame["external_validation_performed"] = False
            log(f"  · Ошибка AI валидации: {e}")

        t = threading.Thread(
            target=_background_validate,
            args=(
                query_to_use,
                synthesis_result.answer,
                synthesis_result,
                risk_result,
                intent_result,
                val_id,
                decision_id,
                trace_id,
                intent_result.intent if intent_result else "general",
                search_result,
                web_used,
                verbose
            ),
            daemon=False,
        )
        t.start()
    optimistic = responder.respond(synthesis_result, start_validation=_start_bg_validation)

    if enable_validation and not _skip_rag and not is_subjective_answer and epistemic_result.testability not in ["interpretive", "non_falsifiable"]:
        log(f"  · Фоновая валидация запущена (ID: {validation_id[:8]})")
    else:
        log("  · Валидация отключена или пропущена")

    if (
        enable_cache
        and synthesis_result.confidence > 0.3
        and not _skip_rag
        and not is_subjective_answer
    ):
        try:
            cache.put_from_synthesis(
                query=query_to_use,
                synthesis_result=synthesis_result,
                epistemic=epistemic_result.__dict__,
                claims=claims_data if 'claims_data' in locals() else [],
                evidence=evidence_data if 'evidence_data' in locals() else [],
            )
        except Exception as e:
            if verbose:
                log(f"[V6] Ошибка сохранения в кэш: {e}")

    total = round(time.time() - t_start, 2)
    cost["total_ms"] = total * 1000
    mon_record("full_request", total, success=True)

    # Pipeline wall-clock profile report — extracted to
    # agent/orchestrator/runtime/profiling.py (structural extraction;
    # behavior unchanged).
    report_pipeline_profile(cost, total, _request_fetch_cache, log, verbose)

    log(f"\n✓ Готово за {total}s")

    tags = _build_tags(intent_result, enrich_result, query_to_use)
    primary_tag = tags[0] if tags else "general"

    try:
        archive_query(
            query=query_to_use,
            tag=primary_tag,
            answer=synthesis_result.answer,
            confidence=synthesis_result.confidence,
            trust_level=synthesis_result.trust_level,
            session_id=request.session_id or "",
            sources=synthesis_result.sources,
        )
    except Exception:
        pass

    trace.cost = cost
    trace.final_answer = synthesis_result.answer
    trace.add_observation("intent_type", intent_type)
    trace.add_observation("intent_confidence", intent_confidence)

    if synthesis_result:
        outcome = OutcomeRecord(
            final_answer=synthesis_result.answer[:500],
            final_answer_type="direct_answer",
            trust_label=synthesis_result.trust_level,
            trust_score=synthesis_result.confidence,
            coverage_ratio=0.5 if len(synthesis_result.answer) > 100 else 0.0,
            latency_ms=cost["total_ms"],
            learning_tags=[primary_tag] if primary_tag else [],
            supporting_claim_ids=supporting_ids if 'supporting_ids' in locals() else [],
        )
        trace.set_outcome(outcome)

    # ---- YANDI V6: ЗАПИСЬ В ПАМЯТЬ И РЕФЛЕКСИЯ ----
    if self_model and memory and reflection and motivation and core_loop:
        try:
            self_model.add_decision({
                "query": query_to_use,
                "domain": epistemic_result.domain if not is_subjective_answer else "subjective_analysis",
                "answer_mode": epistemic_result.answer_mode if not is_subjective_answer else "analysis",
                "trust": synthesis_result.trust_level,
                "confidence": synthesis_result.confidence,
                "reason": epistemic_result.reason if not is_subjective_answer else "subjective_analysis",
                "objectivity_score": epistemic_result.objectivity_score if not is_subjective_answer else 0.5,
                "is_science_as_model": epistemic_result.is_science_as_model if not is_subjective_answer else False,
            })
            self_model.increment_queries()

            memory.add_query(
                query=query_to_use,
                domain="subjective_analysis" if is_subjective_answer else epistemic_result.domain,
                answer_mode="analysis" if is_subjective_answer else epistemic_result.answer_mode,
                trust=synthesis_result.trust_level,
                confidence=synthesis_result.confidence
            )

            if verbose:
                log("[V3] Запуск рефлексии...")

            evidence_count = len(reasoning_info.get("evidence_records", []))
            reflection_result = reflection.reflect_on_query(
                query=query_to_use,
                response=synthesis_result.answer,
                epistemic={
                    "domain": epistemic_result.domain if not is_subjective_answer else "subjective_analysis",
                    "testability": epistemic_result.testability if not is_subjective_answer else "subjective",
                    "answer_mode": epistemic_result.answer_mode if not is_subjective_answer else "analysis",
                    "should_use_web": epistemic_result.should_use_web if not is_subjective_answer else False,
                    "reason": epistemic_result.reason if not is_subjective_answer else "subjective_analysis",
                    "evidence_count": evidence_count,
                    "objectivity_score": epistemic_result.objectivity_score if not is_subjective_answer else 0.5,
                    "is_science_as_model": epistemic_result.is_science_as_model if not is_subjective_answer else False,
                },
                trust=synthesis_result.trust_level,
                confidence=synthesis_result.confidence,
                errors=[] if synthesis_result.confidence > 0.3 else ["low_confidence"],
                validation_result={
                    "performed": bool(query_frame.get("external_validation_performed", False)),
                    "accepted": (
                        claims_accepted
                        if query_frame.get("external_validation_performed", False)
                        and "claims_accepted" in locals()
                        else 0
                    ),
                    "rejected": (
                        claims_rejected
                        if query_frame.get("external_validation_performed", False)
                        and "claims_rejected" in locals()
                        else 0
                    ),
                    "total": (
                        total_claims
                        if query_frame.get("external_validation_performed", False)
                        and "total_claims" in locals()
                        else 0
                    ),
                },
            )

            if verbose and reflection_result.mistakes:
                log(f"[V3] Рефлексия: ошибки: {reflection_result.mistakes}")
            if verbose and reflection_result.lessons:
                log(f"[V3] Уроки: {reflection_result.lessons}")
            # ---- КОРРЕКТИРОВКА TRUST НА ОСНОВЕ РЕФЛЕКСИИ (синхронная) ----
            if reflection_result.mistakes:
                old_conf = synthesis_result.confidence
                synthesis_result.confidence = max(0.1, old_conf - 0.15)
                if synthesis_result.trust_level == "STRONGLY_SUPPORTED":
                    synthesis_result.trust_level = "PARTIALLY_SUPPORTED"
                elif synthesis_result.trust_level == "PARTIALLY_SUPPORTED":
                    synthesis_result.trust_level = "WEAKLY_SUPPORTED"
                if verbose:
                    log(f"[V3] Рефлексия: confidence {old_conf:.2f} → {synthesis_result.confidence:.2f}, trust → {synthesis_result.trust_level}")
                query_frame["reflection_verdict"] = {
                    "mistakes": reflection_result.mistakes,
                    "lessons": reflection_result.lessons,
                    "confidence_adjustment": -0.15,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                query_frame["reflection_verdict"] = {
                    "mistakes": [],
                    "lessons": reflection_result.lessons,
                    "confidence_adjustment": 0.0,
                    "timestamp": datetime.now().isoformat()
                }
            
            # ---- СОХРАНЕНИЕ ОПЫТА В ПАМЯТЬ (асинхронное обучение) ----
            try:
                experience_memory = get_experience_memory()
                if experience_memory:
                    # Определяем speech_act на основе интента
                    speech_act = intent_result.intent if intent_result else "general"
                    # Определяем topic на основе домена
                    topic = epistemic_result.domain if not is_subjective_answer else "subjective"
                    # Сохраняем опыт
                    exp_id = experience_memory.add_experience(
                        speech_act=speech_act,
                        topic=topic,
                        query=query_to_use,
                        response=synthesis_result.answer[:500],  # обрезаем до 500 символов
                        context={
                            "domain": epistemic_result.domain if not is_subjective_answer else "subjective_analysis",
                            "trust": synthesis_result.trust_level,
                            "confidence": synthesis_result.confidence,
                            "mistakes": reflection_result.mistakes,
                            "lessons": reflection_result.lessons,
                            "policy_changes": reflection_result.policy_changes if hasattr(reflection_result, "policy_changes") else [],
                            "timestamp": datetime.now().isoformat()
                        }
                    )
                    if verbose:
                        log(f"[V3] Опыт сохранён в память (ID: {exp_id})")
            except Exception as e:
                if verbose:
                    log(f"[V3] Ошибка сохранения опыта: {e}")

            motivation.update_from_experience({
                "was_useful": synthesis_result.confidence > 0.5,
                "was_correct": synthesis_result.trust_level not in ["UNVERIFIED", "REJECTED"],
                "had_conflict": False,
            })
            # ---- СОХРАНЕНИЕ ЭПИЗОДА В DATASET ----
            try:
                dataset_builder = get_dataset_builder()
                dataset_builder.record_episode({
                    "query": query_to_use,
                    "intent": intent_result.intent if intent_result else "unknown",
                    "domain": epistemic_result.domain if not is_subjective_answer else "subjective",
                    "trust": synthesis_result.trust_level,
                    "confidence": synthesis_result.confidence,
                    "mistakes": reflection_result.mistakes if "reflection_result" in locals() else [],
                    "lessons": reflection_result.lessons if "reflection_result" in locals() else [],
                    "validation": {
                        "accepted": claims_accepted if "claims_accepted" in locals() else 0,
                        "rejected": claims_rejected if "claims_rejected" in locals() else 0,
                        "total": total_claims if "total_claims" in locals() else 0,
                    },  # Закрываем validation
                    "technical_errors": technical_errors if "technical_errors" in locals() else [],
                    "answer": synthesis_result.answer[:500] if synthesis_result.answer else "",
                })
                if verbose:
                    log("[V3] Эпизод сохранён в Dataset")
            except Exception as e:
                if verbose:
                    log(f"[V3] Ошибка сохранения Dataset: {e}")

            if not core_loop.state.is_running:
                core_loop.run_cycle({
                    "query": query_to_use,
                    "epistemic": {
                        "domain": epistemic_result.domain if not is_subjective_answer else "subjective_analysis",
                        "testability": epistemic_result.testability if not is_subjective_answer else "subjective",
                        "answer_mode": epistemic_result.answer_mode if not is_subjective_answer else "analysis",
                        "trust": synthesis_result.trust_level,
                        "confidence": synthesis_result.confidence,
                        "objectivity_score": epistemic_result.objectivity_score if not is_subjective_answer else 0.5,
                        "is_science_as_model": epistemic_result.is_science_as_model if not is_subjective_answer else False,
                    }
                })

            if verbose:
                log(f"[V3] Состояние: цикл {core_loop.state.cycle_number}, "
                    f"запросов {self_model.state.total_queries}, "
                    f"эпизодов {memory.get_stats()['total_episodes']}")

        except Exception as e:
            if verbose:
                log(f"[V3] Ошибка V3: {e}")

    _tracer.save_trace(trace)
    log(f"  · Трейс сохранен: {trace_id}")

    if _bad_state_prefix:
        synthesis_result.answer = _bad_state_prefix + synthesis_result.answer

    # ---- БАННЕР ----
    if is_subjective_answer:
        banner = "[МНЕНИЕ ЯНДИ • СУБЪЕКТИВНАЯ ИНТЕРПРЕТАЦИЯ]"
    elif epistemic_result.domain == "media_interpretation" and not is_subjective_answer:
        if entity:
            banner = f"[РАЗБОР ФИЛЬМА • {entity.get('title', '')}]"
        else:
            banner = "[РАЗБОР ФИЛЬМА • ТРЕБУЕТСЯ УТОЧНЕНИЕ]"
    elif epistemic_result.testability in ["interpretive", "non_falsifiable"] and not is_subjective_answer:
        banner = "[ИНТЕРПРЕТАТИВНЫЙ ОТВЕТ • ОБЗОР РАМОК]"
    elif epistemic_result.answer_mode == "pluralistic_contextual" and not is_subjective_answer:
        banner = "[МНОГОПЕРСПЕКТИВНЫЙ ОТВЕТ • НЕТ ЕДИНСТВЕННОЙ ТРАКТОВКИ]"
    elif epistemic_result.is_science_as_model and not is_subjective_answer:
        banner = "[НАУЧНАЯ МОДЕЛЬ • ЭТО ТЕОРИЯ, НЕ ИСТИНА]"
    else:
        banner = "[ПРЕДВАРИТЕЛЬНЫЙ • ⏳ На проверке]"

    if not optimistic.text.startswith(banner):
        optimistic.text = f"{banner}\n\n{optimistic.text}"

    return OrchestratorResponse(
        answer=optimistic.text,
        trust_level=synthesis_result.trust_level,
        preliminary=True,
        sources=synthesis_result.sources,
        steps_taken=[],
        latency_total=total,
        session_id=request.session_id,
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
