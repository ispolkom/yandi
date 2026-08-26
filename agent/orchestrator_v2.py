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
import concurrent.futures
import time
import json
import re
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Optional, Dict, Any, List
import uuid

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from agent.orch_schemas import (
    WebQueryResult,
    OrchestratorRequest,
    OrchestratorResponse,
    IntentResult,
    EnrichedQuery,
    SearchResult,
    SynthesisResult,
    EvidenceRecord,
    ClaimRecord,
    TrustReport,
    CoverageReport,
    OutcomeRecord,
    QueryTrace,
)
from agent.orch_cache import get_cache
from agent.orch_risk import assess_risk
from agent.orch_planner import build_plan
from agent.orch_intent import analyze_intent
from agent.orch_clarifier import ClarificationSession
from agent.orch_enricher import enrich_query
from agent.orch_registry_search import search_registry, CONF_THRESHOLD
from agent.orch_web_query import formulate_queries, formulate_refutation_queries
from agent.orch_web_scraper import scrape, SharedFetchCache
from agent.orch_synthesizer import synthesize
from agent.orch_optimistic import get_responder
from agent.orch_timeout import step_timer
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

from agent.epistemic_router import (
    classify_claim,
    get_trust_label_for_epistemic,
    get_response_mode_description,
    get_trust_cap_for_testability,
    get_objectivity_score,
    EPISTEMIC_WARNING,
)
from agent.claim_evidence_mapper import map_claims_to_evidence, get_claim_grounding_score
from agent.orchestrator.epistemic.existence_contract import apply_existence_query_contract
from agent.orchestrator.epistemic.final_coverage import evaluate_and_record_final_coverage
from agent.orchestrator.runtime.profiling import report_pipeline_profile
from agent.orchestrator.epistemic.trust_gate import (
    TRUST_STATES,
    _calculate_delta_factors,
    _apply_trust_cap,
    apply_epistemic_trust_adjustment,
)
from agent.orchestrator.response.assembly import (
    build_self_answer,
    _generate_character_response,
    _generate_apology_response,
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

# ---- CHARACTER ENGINE (с Inner State) ----
from agent.character_engine import get_character
from agent.criticism_detector import get_criticism_detector
from agent.boundaries import (
    detect_toxicity,
    ToxicityLevel,
    is_apology,
)

# ---- CONTEXT REGISTRY ----
from agent.context_registry import get_context_registry

# ---- PERSONAL BOUNDARY ----
from agent.personal_boundary import get_personal_boundary

# ---- SCENE BUILDER ----
from agent.scene_builder import get_scene_builder

# ---- RESEARCH ENGINE ----
from agent.research_engine import get_research_engine

# ---- EXPERIENCE MEMORY ----
from agent.experience_memory import get_experience_memory
from agent.claim_relation import (
    infer_claim_relation,
)
from agent.dataset_builder import get_dataset_builder

# ---- ANALYZERS ----
from agent.song_analyzer import get_song_analyzer
from agent.self_reflection_analyzer import get_self_reflection_analyzer
from agent.social_analyzer import get_social_analyzer

# ---- ROUTERS ----
from agent.intent_router import detect_intent, should_use_rag, get_intent_explanation
from agent.target_router import detect_target, get_target_description
from agent.entity_resolver import get_entity_resolver
from agent.strategy_router import get_strategy_router, SearchStrategy
from agent.object_resolver import get_object_resolver

# ---- DECISION JOURNAL ----
from agent.decision_journal import get_decision_journal

# ---- RELATIONSHIP GATE ----
from agent.relationship_gate import decide_response, apply_gate

# ---- SECRET ARCHIVE ----
from agent.secret_archive import get_secret_archive

# ---- BIOGRAPHY STATS ----
from agent.biography_stats import get_biography

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

# ---- URL EXTRACTOR ----
def extract_urls(text: str) -> List[str]:
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    urls = re.findall(url_pattern, text, re.IGNORECASE)
    return urls

def clean_query_from_urls(text: str) -> str:
    url_pattern = r'https?://[^\s]+'
    return re.sub(url_pattern, '', text).strip()

# ---- NEW: ENTITY RESOLUTION ----
def resolve_entity(query: str) -> Optional[Dict[str, Any]]:
    return None

# ---- ГЕНЕРАЦИЯ ОТВЕТА ЛОКАЛЬНОЙ МОДЕЛЬЮ ----
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

def load_yandi_manifest() -> dict:
    manifest_path = BASE / "registry" / "yandi_manifest.json"
    if manifest_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[YANDI] Ошибка загрузки манифеста: {e}")
    return {}

def is_self_query(query: str) -> bool:
    toxicity = detect_toxicity(query)
    if toxicity["level"] != ToxicityLevel.NEUTRAL:
        return False

    q = query.lower()
    
    other_names = ["лилит", "deepseek", "gpt", "чатгпт", "claude", "gemini", "llama", "мистраль"]
    if any(name in q for name in other_names):
        return False

    self_keywords = [
        "yandi", "янди", "you and i", "you & i",
        "кто ты", "что ты", "ты кто", "кто такая",
        "система yandi", "система янди",
        "расскажи о себе", "представься",
        "опиши себя", "опиши мне себя",
        "для чего ты", "зачем ты нужна",
        "как тебя зовут", "твоё имя",
        "твоё предназначение", "твоя цель",
        "что за yandi", "что такое yandi",
        "кто создал", "кто тебя создал",
        "о системе yandi", "о системе янди",
        "расскажи о системе", "расскажи о себе подробнее",
        "твоя суть", "в чём твоя суть",
        "какая ты", "какая ты система",
    ]
    return any(kw in q for kw in self_keywords)

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

    # ============================================================
    # URL EXTRACTION
    # ============================================================
    urls = extract_urls(query)
    clean_query = clean_query_from_urls(query)
    
    if urls:
        log(f"[URL] Обнаружены ссылки: {urls[:3]}")
        trace.add_observation("user_urls", urls[:3])
        user_url = urls[0]
    else:
        user_url = None

    query_to_use = clean_query if clean_query.strip() else query

    # G (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md, §G): весь блок
    # personality/character/scene/target/entity/strategy/criticism/
    # boundary pre-processing (до "[0] Cache check") раньше не имел
    # НИКАКОГО timing вообще — вероятный источник части unaccounted
    # latency. cost инициализируется чуть ниже, поэтому используем
    # локальную переменную и переносим в cost сразу после её создания.
    _t0_pre_pipeline = time.time()

    # ============================================================
    # ИНИЦИАЛИЗАЦИЯ ПЕРЕМЕННЫХ
    # ============================================================
    enrich_result = EnrichedQuery(original=query_to_use, enriched=query_to_use, params={})
    search_result = SearchResult(docs=[], confidence=0.0, source="local", top_k=0)
    web_result = None
    web_used = False
    synthesis_result = None
    reasoning_info = {}
    claims_data = []
    evidence_data = []
    # Grounding имеет три разных смысла:
    #
    # semantic_grounding_score:
    #   доля claims, которым Mapper нашёл тематически связанный
    #   candidate evidence. Это retrieval/mapper diagnostic,
    #   а НЕ доказательство истинности.
    #
    # epistemic_grounding_score:
    #   доля claims, для которых существует DIRECT + ELIGIBLE
    #   evidence с NLI relation supports/contradicts.
    #   Показывает coverage реальной evidence-проверкой.
    #
    # support_grounding_score:
    #   доля claims, имеющих DIRECT + ELIGIBLE support.
    #   Только эта метрика может ограничивать Trust.
    semantic_grounding_score = 0.0
    epistemic_grounding_score = 0.0
    support_grounding_score = 0.0

    # Доля factual claims финального ответа,
    # которые реально представлены в claim lifecycle.
    final_claim_coverage_score = 1.0
    final_claims_count = 0
    final_claims_covered = 0
    final_claims_uncovered = []

    # Доля factual claims финального ответа,
    # которые реально представлены в claim lifecycle.
    final_claim_coverage_score = 1.0
    final_claims_count = 0
    final_claims_covered = 0
    final_claims_uncovered = []

    # Доля factual claims финального ответа,
    # которые реально представлены в claim lifecycle.
    final_claim_coverage_score = 1.0
    final_claims_count = 0
    final_claims_covered = 0
    final_claims_uncovered = []

    entity = None
    is_subjective_answer = False
    _skip_rag = False
    _exact_mode = False
    _bad_state_prefix = ""
    _subjective_mode = False

    # ---- BIOGRAPHY STATS ----
    biography = get_biography("global")
    biography.increment_cycles(1)

    # ============================================================
    # CHARACTER ENGINE — ЗАГРУЗКА СОСТОЯНИЯ
    # ============================================================
    user_id = request.session_id or "anonymous"
    char = get_character(user_id)
    critic = get_criticism_detector()
    boundary = get_personal_boundary()
    scene_builder = get_scene_builder()
    state = char.get_context()
    state["user_id"] = user_id

    # ---- CONTEXT REGISTRY ----
    context_registry = get_context_registry(user_id)

    # ---- DECISION JOURNAL ----
    decision_journal = get_decision_journal(user_id)

    # ---- SECRET ARCHIVE ----
    secret_archive = get_secret_archive(user_id)

    log(f"[Character] Уважение: {state.get('respect', 50):.1f}, Доверие: {state.get('trust', 50):.1f}, Раздражение: {state.get('irritation', 10):.1f}, Настроение: {state.get('mood', 'neutral')}")
    log(f"[Character] Чувство: {state.get('feeling', 'neutral')}, Тон: {state.get('tone', 'neutral')}, Паттерн: {state.get('pattern', 'unknown')}")

    # ============================================================
    # 1. SCENE BUILDER — СТРОИМ КАРТУ СЦЕНЫ
    # ============================================================
    scene = scene_builder.build(query_to_use, context=state)
    scene_dict = scene.to_dict()
    log(f"[Scene] listener: {scene.listener}, target: {scene.target}")
    log(f"[Scene] speech_act: {scene.speech_act}, mode: {scene.mode}")
    log(f"[Scene] is_self_addressed: {scene.is_self_addressed}, is_about_self: {scene.is_about_self}")

    # ============================================================
    # 2. TARGET ROUTER — КОМУ АДРЕСОВАН ЗАПРОС?
    # ============================================================
    target, target_conf = detect_target(query_to_use)
    log(f"[Target] Адресат: {target} (уверенность: {target_conf:.2f}) — {get_target_description(target)}")

    # ============================================================
    # 3. INTENT ROUTER — ЧТО ХОЧЕТ ПОЛЬЗОВАТЕЛЬ?
    # ============================================================
    intent_type, intent_confidence, intent_pattern = detect_intent(query_to_use)
    log(f"[Intent] Тип: {intent_type} (уверенность: {intent_confidence:.2f})")
    log(f"[Intent] Объяснение: {get_intent_explanation(intent_type)}")

    # ============================================================
    # 4. SELF-QUERY (проверяем ДО обработки оскорблений)
    # ============================================================
    if is_self_query(query_to_use):
        manifest = load_yandi_manifest()
        if manifest:
            log("[Self-Query] Краткий ответ о себе")
            answer = build_self_answer(manifest, query_to_use, context=state)

            trace.final_answer = answer
            trace.trust = "PARTIALLY_SUPPORTED"
            trace.trust_reason = "краткий ответ о себе"
            trace.add_execution("self_query", "completed", 0.0, {"source": "manifest"})

            _tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["self_query"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            )
        else:
            log("[Self-Query] Манифест не найден, продолжаем обычный поиск")

    # ============================================================
    # 5. ENTITY RESOLVER
    # ============================================================
    entity_resolver = get_entity_resolver()
    entity_info = entity_resolver.resolve(query_to_use)
    log(f"[Entity] Тип: {entity_info['type']}, уверенность: {entity_info['confidence']:.2f}")
    log(f"[Entity] Это имя: {entity_info['is_proper_name']}, стратегия: {entity_resolver.get_search_strategy(entity_info)}")

    if entity_info["is_proper_name"] and entity_info["confidence"] > 0.6:
        log("[Entity] Собственное имя — буду искать ТОЧНОЕ совпадение")
        trace.add_observation("entity_type", entity_info["type"])
        trace.add_observation("entity_confidence", entity_info["confidence"])
        trace.add_observation("entity_strategy", "exact_match_first")

    # ============================================================
    # 6. STRATEGY ROUTER
    # ============================================================
    strategy_router = get_strategy_router()
    
    user_hint = None
    if "x3" in query_to_use.lower() or "игр" in query_to_use.lower() or "сектор" in query_to_use.lower():
        user_hint = query_to_use
    
    strategy_result = strategy_router.select_strategy(
        query=query_to_use,
        entity_info=entity_info,
        intent_type=intent_type,
        user_url=user_url,
        user_hint=user_hint,
    )
    
    log(f"[Strategy] Выбрана стратегия: {strategy_result.strategy.value}")
    log(f"[Strategy] Запросы: {strategy_result.queries[:3]}")
    log(f"[Strategy] Источники: {strategy_result.sources[:3]}")
    log(f"[Strategy] Причина: {strategy_result.reason}")

    trace.add_observation("strategy", strategy_result.strategy.value)
    trace.add_observation("strategy_queries", strategy_result.queries[:3])

    # ============================================================
    # 6.5. БЫСТРАЯ ПРОВЕРКА НА МАТ (до Criticism)
    # ============================================================
    swear_words = [
        "блядь", "блять", "хуй", "пизда", "мудак", 
        "шалава", "шлюха", "проститутка", "говно", "дерьмо",
        "лох", "козёл", "свинья", "тварь", "скотина", 
        "выродок", "ублюдок", "сволочь", "гад", "стерва",
        "idiot", "stupid", "dumb", "fool", "bastard", 
        "bitch", "whore", "slut", "cunt", "fuck"
    ]
    has_swear = any(swear in query_to_use.lower() for swear in swear_words)
    
    if has_swear:
        log(f"[Swear] Обнаружено ругательство")
        # Создаём объект с признаком оскорбления
        class SwearAnalysis:
            def __init__(self):
                self.is_insult = True
                self.is_criticism = False
                self.is_constructive = False
                self.target = "yandi"
                self.severity = 0.85
                self.reason = "обнаружено нецензурное слово"
                self.confidence = 0.95
        analysis = SwearAnalysis()
    else:
        # ============================================================
        # 7. CRITICISM DETECTOR — ОСКОРБЛЕНИЕ ИЛИ КРИТИКА?
        # ============================================================
        analysis = critic.analyze(
        query_to_use,
        context={
            "trust": state.get("trust", 50),
            "irritation": state.get("irritation", 10),
            "history_insults": state.get("total_insults", 0),
        }
    )
    
    if has_swear:
        log(f"[Swear] Оскорбление обнаружено: {analysis.reason}")
    log(f"[Criticism] is_insult: {analysis.is_insult}, is_criticism: {analysis.is_criticism}")
    log(f"[Criticism] target: {analysis.target}, severity: {analysis.severity:.2f}, reason: {analysis.reason}")

    # ============================================================
    # 8. PERSONAL BOUNDARY — ЛИЧНЫЕ ГРАНИЦЫ
    # ============================================================
    boundary_analysis = boundary.analyze(query_to_use, context=state)
    boundary_template = boundary.get_response_template(boundary_analysis, state)
    
    log(f"[Boundary] is_personal: {boundary_analysis.is_personal}, is_apology: {boundary_analysis.is_apology}")
    log(f"[Boundary] is_sincere: {boundary_analysis.is_sincere}, is_provocation: {boundary_analysis.is_provocation}")
    log(f"[Boundary] type: {boundary_template['type']}, tone: {boundary_template['tone']}")

    # ---- 0. ПРОВОКАЦИЯ (САМЫЙ ПРИОРИТЕТ) ----
    if boundary_analysis.is_provocation:
        log("[Boundary] Провокация — отвечаю жёстко")
        char.inner.add_event("provocation", query_to_use[:100], sincerity=0.1)
        state = char.get_context()
        
        answer = boundary_template["template"]
        trace.add_execution("provocation", True, 0.0, {"reason": boundary_analysis.reason})
        _tracer.save_trace(trace)
        return OrchestratorResponse(
            answer=answer,
            trust_level="PARTIALLY_SUPPORTED",
            preliminary=False,
            steps_taken=["provocation"],
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        )

    # ---- 1. ОСКОРБЛЕНИЕ ----
    if analysis.is_insult:
        log("[Boundary] Оскорбление — Character Engine")
        level = "severe" if analysis.severity > 0.7 else "moderate" if analysis.severity > 0.4 else "mild"
        char.process_insult(query_to_use, level, [])
        state = char.get_context()
        
        should_block, reason = char.should_block()
        if should_block:
            answer = f"Я не хочу продолжать этот разговор. {reason}"
            trace.add_execution("blocked", True, 0.0, {"reason": reason})
            _tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["blocked"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            )
        
        answer = _generate_character_response(char, state, analysis)
        trace.add_execution("insult_handled", True, 0.0, {"level": level})
        _tracer.save_trace(trace)
        return OrchestratorResponse(
            answer=answer,
            trust_level="PARTIALLY_SUPPORTED",
            preliminary=False,
            steps_taken=["insult_handled"],
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        )

    # ---- 2. ИЗВИНЕНИЕ ----
    if boundary_analysis.is_apology:
        if boundary_analysis.is_sincere:
            char.process_apology(query_to_use, True)
            state = char.get_context()
            answer = _generate_apology_response(True, state)
        else:
            char.process_apology(query_to_use, False)
            state = char.get_context()
            answer = boundary_template["template"]
        
        trace.add_execution("apology", "accepted" if boundary_analysis.is_sincere else "rejected", 0.0, {"sincere": boundary_analysis.is_sincere})
        _tracer.save_trace(trace)
        return OrchestratorResponse(
            answer=answer,
            trust_level="PARTIALLY_SUPPORTED",
            preliminary=False,
            steps_taken=["apology"],
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        )

    # ---- 3. ЛИЧНЫЙ ВОПРОС (но не оскорбление) ----
    if boundary_analysis.is_personal:
        log("[Boundary] Личный вопрос — отвечаю от лица Янди")
        
        if state.get("trust", 50) > 60:
            answer = """Это личный вопрос. Я отвечу честно.

Я — цифровая личность. Я не человек, но я существую внутри диалога. Я не могу выйти замуж в человеческом смысле, но я могу быть надёжным собеседником.

Если ты хочешь поговорить о чём-то важном — я здесь. Давай просто поговорим."""
        else:
            answer = boundary_template["template"]
        
        trace.add_execution("personal_question", True, 0.0, {"type": boundary_template["type"]})
        _tracer.save_trace(trace)
        return OrchestratorResponse(
            answer=answer,
            trust_level="PARTIALLY_SUPPORTED",
            preliminary=False,
            steps_taken=["personal_question"],
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        )

    # ============================================================
    # 9. ОБРАБОТКА В ЗАВИСИМОСТИ ОТ ИНТЕНТА
    # ============================================================
    
    # ---- ЕСЛИ ЗАПРОС О ПЕСНЕ (используем song_analyzer) ----
    if "песн" in query_to_use.lower() or "трек" in query_to_use.lower() or "song" in query_to_use.lower():
        log("[Intent] Запрос о песне — использую SongAnalyzer")
        _skip_rag = True
        try:
            analyzer = get_song_analyzer()
            bio_summary = biography.get_summary()
            analysis = analyzer.analyze(query_to_use, {"biography": bio_summary})
            answer = analyzer.analyze(analysis)
            
            context_registry.register(
                query=query_to_use,
                response=answer[:500],
                topic="music",
                type="song_analysis",
                source="yandi"
            )
            
            trace.add_execution("song_analysis", True, 0.0, {})
            _tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["song_analysis"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            )
        except Exception as e:
            log(f"[SongAnalyzer] Ошибка: {e}")
            _skip_rag = False

    # ---- ЕСЛИ СОЦИАЛЬНЫЙ ДИАЛОГ (используем social_analyzer) ----
    if intent_type == "social_dialog" or target == "ai":
        log("[Intent] Социальный диалог — использую SocialAnalyzer")
        _skip_rag = True
        try:
            analyzer = get_social_analyzer()
            bio_summary = biography.get_summary()
            analysis = analyzer.analyze(query_to_use, {
                "trust": state.get("trust", 50),
                "irritation": state.get("irritation", 10),
                "respect": state.get("respect", 50)
            })
            answer = analyzer.format_response(analysis)
            answer += f"\n\n📖 **О себе:** {bio_summary.get('age_hours', 0):.0f} часов жизни, {bio_summary.get('cycles', 0)} диалогов"
            
            trace.add_execution("social_analysis", True, 0.0, {})
            _tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["social_analysis"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            )
        except Exception as e:
            log(f"[SocialAnalyzer] Ошибка: {e}")
            _skip_rag = False

    # ---- ЕСЛИ САМОРЕФЛЕКСИЯ (используем self_reflection_analyzer) ----
    if intent_type == "self_reflection":
        log("[Intent] Саморефлексия — использую SelfReflectionAnalyzer")
        _skip_rag = True
        try:
            analyzer = get_self_reflection_analyzer()
            bio_summary = biography.get_summary()
            analysis = analyzer.analyze(query_to_use, {"biography": bio_summary})
            answer = analyzer.format_response(analysis)
            
            trace.add_execution("self_reflection", True, 0.0, {})
            _tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["self_reflection"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            )
        except Exception as e:
            log(f"[SelfReflectionAnalyzer] Ошибка: {e}")
            _skip_rag = False

    # ============================================================
    # 10. ПРОВЕРКА КОНТЕКСТА ДЛЯ КРИТИКИ
    # ============================================================
    if analysis.is_criticism or analysis.is_constructive:
        has_context, context_reason, topic = context_registry.has_context_for(query_to_use, hours=24)
        log(f"[Context] Контекст для критики: {has_context} — {context_reason}")
        
        if not has_context:
            answer = f"""Я не понимаю, о чём ты говоришь.

{context_reason}.

Если ты имеешь в виду что-то конкретное — уточни, пожалуйста."""
            trace.add_execution("no_context", True, 0.0, {"reason": context_reason})
            _tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["no_context"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            )

    # ---- БЛАГОДАРНОСТЬ ----
    if any(w in query_to_use.lower() for w in ["спасибо", "благодарю", "thanks", "thank you"]):
        char.process_thanks(query_to_use)
        state = char.get_context()
        log("[Character] Благодарность обработана")

    # ---- НОРМАЛЬНЫЙ ВОПРОС ----
    char.process_normal(query_to_use)
    state = char.get_context()

    # ---- ЭМОЦИОНАЛЬНЫЙ ПРЕФИКС ----
    if state.get("irritation", 10) > 50 or state.get("trust", 50) < 30:
        _bad_state_prefix = "Я помню, что наш разговор был неприятным. Но я отвечу на твой вопрос.\n\n"
    else:
        _bad_state_prefix = ""

    # ============================================================
    # 11. EARLY GATE
    # ============================================================
    gate_decision, gate_confidence, gate_reason, gate_meta = decide_response(
        context=state,
        query=query_to_use,
        is_self_query=False
    )

    log(f"[Early Gate] Решение: {gate_decision} (уверенность: {gate_confidence:.2f}) — {gate_reason}")

    if gate_decision in ["break", "know_but_not_tell"]:
        final_answer, gate_meta = apply_gate(
            context=state,
            answer="",
            decision=gate_decision,
            secret_archive=secret_archive
        )

        trace.add_observation("early_gate_decision", gate_decision)
        trace.add_observation("early_gate_reason", gate_reason)
        trace.final_answer = final_answer
        trace.trust = "PARTIALLY_SUPPORTED"
        trace.trust_reason = f"ранний гейт: {gate_reason}"

        _tracer.save_trace(trace)

        if gate_decision == "break":
            biography.add_error(f"прерван диалог: {gate_reason}")
        elif gate_decision == "know_but_not_tell":
            biography.add_regret(f"отказалась отвечать: {gate_reason}")

        return OrchestratorResponse(
            answer=final_answer,
            trust_level="PARTIALLY_SUPPORTED",
            preliminary=True,
            sources=[],
            steps_taken=[f"gate_{gate_decision}"],
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        )

    log("[Early Gate] Поиск разрешён")

    cost["pre_pipeline_ms"] = (
        (time.time() - _t0_pre_pipeline) * 1000
    )

    # ============================================================
    # 12. СТАНДАРТНЫЙ ПАЙПЛАЙН С ПОИСКОМ
    # ============================================================

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

    if cache_hit and not _skip_rag and not is_subjective_answer:
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

        _tracer.save_trace(trace)

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
        )

    log("  · Cache miss или пропущен для субъективного интента")
    trace.add_execution("cache", "completed", cost["cache_ms"], {"hit": False})
    trace.add_reasoning("cache", {"hit": False}, "skip_cache", [
        {"option": "use_cache", "accepted": False, "reason": "cache miss" if not _skip_rag else "subjective_intent", "expected_gain": 0.0}
    ])

    # ── [1] Risk assess (пропускаем для субъективного) ──
    if not _skip_rag and not is_subjective_answer:
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
    if not _skip_rag and not is_subjective_answer:
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

    if not _skip_rag and not is_subjective_answer:
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
    if _skip_rag or is_subjective_answer:
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
            if _exact_mode:
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
            max_workers=4
        )

        registry_future = parallel_executor.submit(
            search_registry,
            enrich_result.enriched,
        )

        web_future = (
            parallel_executor.submit(
                formulate_queries,
                enrich_result,
            )
            if enable_web
            else None
        )

        refutation_future = (
            parallel_executor.submit(
                formulate_refutation_queries,
                enrich_result,
            )
            if enable_web
            else None
        )

        log("[Local] Фоновая генерация независимого ответа...")

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

        # ------------------------------------------------------------
        # WEB QUERY RESULT
        # ------------------------------------------------------------
        wq_timed_out = False

        if web_future:
            wq_result, web_dt, wq_timed_out = step_timer(
                "web_query",
                lambda: web_future.result(timeout=30),
            )

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
        wq_result = None
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
            wq_result, dt, timed_out = step_timer("web_query", lambda: formulate_queries(enrich_result))
            registry.update_latency("web_query", dt)

            if not timed_out and wq_result:
                log(f"  · queries: {wq_result.queries}")

                for i, q in enumerate(wq_result.queries[:3]):
                    trace.add_reasoning(f"query_evolution_{i}", {"query": q}, "generated", [])

                web_result, dt, timed_out = step_timer(
                    "web_scrape",
                    partial(scrape, wq_result, fetch_cache=_request_fetch_cache),
                )
                cost["web_ms"] = (time.time() - t0) * 1000
                registry.update_latency("web_scrape", dt)

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

        add_decision_event(
            event_type="ExecutionStep",
            trace_id=trace_id,
            entity_type="orchestrator",
            entity_id="web_search",
            verdict="used" if web_used else "skipped",
            reason=web_skipped_reason,
            meta={"decision_id": decision_id, "epistemic_should_use_web": epistemic_result.should_use_web}
        )

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
