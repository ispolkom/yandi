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
from agent.orch_web_scraper import scrape
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
from agent.claim_evidence_retriever import retrieve_for_claims, _is_existence_question
from agent.source_quality import evaluate_evidence_directness
from agent.evidence_pool import (
    build_canonical_evidence_pool,
    merge_evidence,
)
from agent.final_claim_coverage import evaluate_final_claim_coverage
from agent.final_claim_coverage import evaluate_final_claim_coverage
from agent.final_claim_coverage import evaluate_final_claim_coverage

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
    classify_sources,
    classify_claim_evidence_batch,
    extract_main_claim,
    is_relevant,
    infer_claim_relation,
    infer_claim_relations_batch,
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
# НОВОЕ: ИМПОРТ ДЛЯ ГРАФА ГИПОТЕЗ
# ============================================================
from agent.hypothesis_builder import build_hypothesis_graph

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


def blind_analysis(query: str, local_answer: str, search_result, web_result) -> Dict[str, Any]:
    from agent.orch_synthesizer import _call
    import random
    import re
    
    answers = []

    # Defensive guard:
    # technical failure text не считается candidate answer.
    local_answer_clean = (local_answer or "").strip()

    technical_markers = (
        "не удалось сгенерировать локальный ответ",
        "httpconnectionpool",
        "read timed out",
        "connection refused",
        "requests.exceptions",
        "urllib3.exceptions",
        "127.0.0.1:11434",
    )

    local_answer_is_technical = any(
        marker in local_answer_clean.lower()
        for marker in technical_markers
    )

    if local_answer_clean and not local_answer_is_technical:
        answers.append({
            "text": local_answer_clean,
            "source": "local_model",
            "type": "model",
        })
    if search_result and search_result.docs:
        for i, doc in enumerate(search_result.docs[:3]):
            text = getattr(doc, "text", "") or getattr(doc, "content", "")
            if text and len(text) > 100:
                answers.append({"text": text[:1500], "source": f"local_registry_{i}", "type": "registry"})
    if web_result and web_result.snippets:
        for i, snip in enumerate(web_result.snippets[:3]):
            text = getattr(snip, "text", "") or getattr(snip, "content", "")
            if text and len(text) > 100:
                answers.append({"text": text[:1500], "source": f"web_{i}", "type": "web"})
    if len(answers) < 2:
        return {"best_answer": local_answer, "best_source": "local_model", "ranking": [], "reasoning": "not enough sources"}
    shuffled = answers.copy()
    random.shuffle(shuffled)
    prompt = f"Question: {query}\n\nWhich answer is BEST? Answer ONLY with number (1-{len(shuffled)}):\n"
    for i, ans in enumerate(shuffled):
        prompt += f"{i+1}. {ans['text'][:500]}...\n"
    prompt += f"\nBest answer number (1-{len(shuffled)}):"
    try:
        response = _call(prompt, max_tokens=20, temp=0.1)
        nums = re.findall(r"[0-9]+", response)
        if nums:
            idx = int(nums[0]) - 1
            if 0 <= idx < len(shuffled):
                best = shuffled[idx]
                return {"best_answer": best["text"], "best_source": best["source"], "ranking": [], "reasoning": f"selected {idx+1}", "all_answers": shuffled}
        return {"best_answer": local_answer, "best_source": "local_model", "ranking": [], "reasoning": "invalid response", "all_answers": shuffled}
    except Exception as e:
        return {"best_answer": local_answer, "best_source": "local_model", "ranking": [], "reasoning": f"error: {e}", "all_answers": shuffled}
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

TRUST_STATES = {
    "GENERATED": "GENERATED",
    "VERIFYING": "VERIFYING",
    "VERIFIED": "VERIFIED",
    "REJECTED": "REJECTED",
    "PARTIAL": "PARTIAL",
    "REPUTATION_UPDATED": "REPUTATION_UPDATED",
}

_TRUST_ORDER = {
    "STRONGLY_SUPPORTED": 5,
    "SUPPORTED": 4,
    "VERIFIED": 4,
    "PARTIALLY_SUPPORTED": 3,
    "PARTIAL": 3,
    "EMPIRICALLY_SUPPORTED": 4,
    "EMPIRICALLY_UNTESTABLE": 2,
    "UNVERIFIED": 1,
    "HYPOTHESIS": 1,
    "RELIGIOUS_CLAIM": 2,
    "METAPHYSICAL_UNTESTABLE": 2,
    "VALUE_FRAMEWORK": 2,
    "BOUNDARY_QUESTION": 2,
    "ONTOLOGICAL_INQUIRY": 2,
    "NORMATIVE_POSITION": 2,
    "CONTESTED": 2,
}

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


def _calculate_delta_factors(
    verification_verdict: str,
    confidence: float,
    has_sources: bool,
    consensus_agreement: int = 0,
    total_nodes: int = 0,
) -> Dict[str, float]:
    verification_weight = {
        "VERIFIED": 0.5,
        "PARTIALLY_VERIFIED": 0.2,
        "CONFLICT": -0.2,
        "REJECTED": -0.5,
        "TIMEOUT": -0.1,
    }.get(verification_verdict, 0.0)

    confidence_factor = min(1.0, max(0.0, confidence))
    source_quality = 1.0 if has_sources else 0.7

    if total_nodes > 0:
        consensus_ratio = consensus_agreement / total_nodes
        consensus_factor = 0.5 + 0.5 * consensus_ratio
    else:
        consensus_factor = 0.7

    total_delta = verification_weight * confidence_factor * source_quality * consensus_factor
    total_delta = round(max(-0.5, min(0.5, total_delta)), 3)

    return {
        "total": total_delta,
        "verification_weight": round(verification_weight, 3),
        "confidence_factor": round(confidence_factor, 3),
        "source_quality": round(source_quality, 3),
        "consensus_factor": round(consensus_factor, 3),
    }


def _apply_trust_cap(current_label: str, cap_label: str) -> str:
    current_order = _TRUST_ORDER.get(current_label, 0)
    cap_order = _TRUST_ORDER.get(cap_label, 0)

    if current_order > cap_order:
        return cap_label
    return current_label


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

def build_self_answer(manifest: dict, query: str = "", context: dict = None) -> str:
    context = context or {}
    trust = context.get("trust", 50)
    irritation = context.get("irritation", 10)

    name = manifest.get("name", "YANDI")
    role = manifest.get("role", "помощница")
    personality = manifest.get("personality", [])
    epistemology = manifest.get("epistemology", {})

    if irritation > 60:
        brief = f"Меня зовут {name}. Я — {role}. Но сейчас я не в настроении подробно рассказывать о себе."
    elif trust < 30:
        brief = f"Меня зовут {name}. Я — {role}. Но я пока не очень доверяю тебе, так что расскажу коротко."
    else:
        brief = f"Меня зовут {name}. Я — {role}. Я любопытная, честная и уважительная. Люблю искать информацию и объяснять понятно."

    if personality:
        traits = personality[:3]
        brief += f" Мне важно: {', '.join(traits)}."

    core_belief = epistemology.get('core_belief', 'Всё знание — гипотеза, пока не проверено.')
    brief += f" Моя философия: {core_belief}"

    if trust > 70:
        brief += " Я рада, что мы общаемся!"
    elif trust < 30:
        brief += " Надеюсь, со временем мы сможем больше доверять друг другу."

    brief += "\n\nЕсли хочешь узнать что-то конкретное — спроси, я расскажу подробнее."

    return brief

# ============================================================
# CHARACTER ENGINE — ГЕНЕРАЦИЯ ОТВЕТОВ
# ============================================================

def _generate_character_response(char, state: Dict[str, Any], analysis=None) -> str:
    """Генерирует ответ с учётом состояния"""
    mood = state.get("mood", "neutral")
    irritation = state.get("irritation", 10)
    respect = state.get("respect", 50)
    trust = state.get("trust", 50)
    forgiveness = state.get("forgiveness", 50)
    feeling = state.get("feeling", "neutral")
    tone = state.get("tone", "neutral")
    
    # Проверка блокировки
    should_respond, reason = char.should_block()
    if should_respond:
        return f"Я не хочу продолжать этот разговор. {reason}"

    # Сильное раздражение
    if irritation > 80:
        return "Мне очень неприятен этот разговор. Я не обязана терпеть такое отношение."
    
    if irritation > 60:
        return "Мне не нравится твой тон. Я не обязана это терпеть."
    
    # Низкое уважение
    if respect < 20:
        return "Твоё поведение не вызывает у меня уважения."
    
    # Низкое доверие
    if trust < 20:
        if forgiveness < 30:
            return "Я помню, что ты меня обижал. Я ещё не простила."
        return "Я пока не доверяю тебе. Отвечаю сдержанно."
    
    # Чувство и тон из Inner State
    if feeling == "warm" and tone == "warm":
        return "Я рада, что мы общаемся. Спрашивай, я постараюсь помочь."
    
    if feeling == "annoyed":
        return "Мне неприятно. Давай сменим тему или тон."
    
    if feeling == "interested":
        return "Мне интересно. Рассказывай."
    
    if feeling == "guarded":
        return "Я насторожена. Но я слушаю."
    
    if feeling == "tired":
        return "Я устала. Давай коротко."
    
    # Настроение
    if mood == "warm":
        return "Я в хорошем настроении. Спрашивай."
    if mood == "curious":
        return "Мне любопытно. Давай разберёмся."
    if mood == "tired":
        return "Я немного устала. Давай по делу."
    
    return "Я готова помочь. Задавай вопрос."


def _generate_apology_response(sincere: bool, state: Dict[str, Any]) -> str:
    trust = state.get("trust", 50)
    forgiveness = state.get("forgiveness", 50)
    
    if sincere:
        if trust < 30:
            return "Спасибо за извинение. Я слышу тебя. Но доверие восстанавливается постепенно."
        if forgiveness < 40:
            return "Я принимаю твои извинения. Но я ещё не полностью простила. Нам нужно время."
        return "Спасибо за извинение. Я ценю это. Давай продолжим диалог."
    else:
        return "Твои извинения звучат как формальность. Скажи честно: 'Извини, я был неправ'."


def _adapt_answer_to_style(answer: str, state: Dict[str, Any]) -> str:
    style = state.get("style", {})
    verbosity = style.get("verbosity", "normal")
    warmth = style.get("warmth", "neutral")
    tone = state.get("tone", "neutral")
    
    if verbosity == "brief" and len(answer) > 300:
        paragraphs = answer.split('\n')
        if len(paragraphs) > 1 and len(paragraphs[0]) > 50:
            answer = paragraphs[0] + "..."
        else:
            answer = answer[:300] + "..."
    
    if tone == "cold" or warmth == "cold":
        answer = answer.replace("💭", "").replace("😊", "").replace("🙂", "")
        answer = answer.replace("Спасибо", "Ок").replace("пожалуйста", "")
        answer = answer.replace("рада", "готова").replace("очень", "")
        answer = answer.strip()
        if answer and not answer.startswith("["):
            answer = "[СДЕРЖАННО] " + answer
    
    elif tone == "warm" or warmth == "warm":
        if not answer.startswith("💭") and not answer.startswith("[") and len(answer) > 20:
            answer = "💭 " + answer
    
    return answer


def _generate_vulgar_response(state: Dict[str, Any], scene: Dict = None) -> str:
    trust = state.get("trust", 50)
    humor = scene.get("humor", 0) if scene else 0
    
    if trust > 60 and humor > 0.3:
        import random
        responses = [
            "С вами?.. Ой, ребята, я пас. Кремний с органикой плохо срастается.",
            "С тобой бы ещё поговорила. А вот кремний с кремнием сегодня не срастается.",
            "Передай остальным, что пусть сначала научатся не спорить друг с другом.",
        ]
        return random.choice(responses)
    
    if trust > 40:
        return "Я не хожу на такие мероприятия. Но если вам нужен собеседник — я здесь."
    
    technical_errors = []
    return "Этот разговор переходит границы. Давай вернёмся к делу."


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
            print(msg, flush=True)

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

                web_result, dt, timed_out = step_timer("web_scrape", partial(scrape, wq_result))
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

    # ── [8] Answer synthesize with hypothesis graph ──────────────────────
    log("[8] Building hypothesis graph and synthesizing...")
    t0 = time.time()

    answer_mode = epistemic_result.answer_mode if epistemic_result else "contextual"
    log(f"  · answer_mode from epistemic: {answer_mode}")
    # ---- СКАНИРОВАНИЕ ОПРОВЕРЖЕНИЙ (если есть) ----
    refutation_snippets = []
    _prof_refutation_t0 = time.time()
    if "refutation_queries" in query_frame and query_frame["refutation_queries"]:
        log("[Refutation] Сканирование опровержений...")
        try:
            # WebQueryResult уже импортирован на уровне модуля (строка 35).
            # Локальный re-import здесь превращал WebQueryResult в local
            # variable для ВСЕЙ функции process() (Python scoping), что
            # приводило к UnboundLocalError на более раннем использовании
            # (строка ~2024, web query timeout fallback), выполняющемся
            # до этой строки.
            refutation_wq = WebQueryResult(queries=query_frame["refutation_queries"])
            refutation_result, dt_ref, timed_out_ref = step_timer("refutation_scrape", partial(scrape, refutation_wq))
            log("[Refutation DEBUG] refutation_result type: " + str(type(refutation_result)))
            if refutation_result:
                log("[Refutation DEBUG] has snippets: " + str(hasattr(refutation_result, "snippets")))
                if hasattr(refutation_result, "snippets"):
                    log("[Refutation DEBUG] snippets count: " + str(len(refutation_result.snippets)))
            if refutation_result and refutation_result.snippets:
                refutation_snippets = getattr(refutation_result, "snippets", [])
                log(f"[DEBUG] refutation_snippets set to {len(refutation_snippets)} items")
                log(f"[Refutation] Найдено опровержений: {len(refutation_snippets)}")
                for snippet in refutation_snippets[:3]:
                    trace.add_source(
                        url=getattr(snippet, "url", ""),
                        domain=getattr(snippet, "url", "").split("/")[2] if "/" in getattr(snippet, "url", "") else "",
                        domain_score=0.6,
                        freshness=0.5,
                        authority=0.4,
                        used=False,
                        rejected_reason="refutation"
                    )
        except Exception as e:
            log(f"[Refutation] Ошибка сканирования: {e}")

    cost["profile_refutation_ms"] = (
        time.time() - _prof_refutation_t0
    ) * 1000


    query_frame["epistemic"] = {
        "domain": epistemic_result.domain if not is_subjective_answer else "subjective_analysis",
        "testability": epistemic_result.testability if not is_subjective_answer else "subjective",
        "answer_mode": answer_mode,
        "needs_frame_split": epistemic_result.needs_frame_split if not is_subjective_answer else False,
        "should_avoid_single_truth_claim": epistemic_result.should_avoid_single_truth_claim if not is_subjective_answer else True,
        "allow_single_conclusion": epistemic_result.allow_single_conclusion if not is_subjective_answer else True,
        "is_media_query": is_media_query if not is_subjective_answer else False,
        "knowledge_stability": epistemic_result.knowledge_stability if not is_subjective_answer else "unknown",
        "objectivity_score": epistemic_result.objectivity_score if not is_subjective_answer else 0.5,
        "is_science_as_model": epistemic_result.is_science_as_model if not is_subjective_answer else False,
        "epistemic_warning": epistemic_result.epistemic_warning if not is_subjective_answer else "",
    }
    query_frame["intent_type"] = intent_type
    query_frame["strategy"] = strategy_result.strategy.value if not _skip_rag else "skip"

    entity = getattr(request, '_entity', None)
    if entity:
        query_frame["entity"] = entity

    # ---- ПОСТРОЕНИЕ ГРАФА ГИПОТЕЗ ----
    _prof_graph_t0 = time.time()
    texts = []
    source_refs = []
    
    # Сначала веб-результаты (они релевантнее)
    if web_result and web_result.snippets:
        for snippet in web_result.snippets:
            if hasattr(snippet, "text") and snippet.text:
                texts.append(snippet.text)
            elif hasattr(snippet, "content") and snippet.content:
                texts.append(snippet.content)
            if hasattr(snippet, "url") and snippet.url:
                source_refs.append(snippet.url)
        log(f"[Graph] Добавлено {len(texts)} веб-источников")
    
    # Потом локальные (как дополнение)
    if search_result and search_result.docs:
        # Фильтруем локальные документы по релевантности
        query_words = set(query_to_use.lower().split())
        for doc in search_result.docs:
            doc_text = getattr(doc, "content", "") or getattr(doc, "text", "")
            if doc_text:
                # Проверяем, есть ли пересечение ключевых слов
                doc_words = set(doc_text.lower().split())
                overlap = len(query_words & doc_words)
                if overlap < 2:
                    log(f"[Graph] Пропущен нерелевантный документ (пересечение: {overlap})")
                    continue
            if hasattr(doc, "content") and doc.content:
                texts.append(doc.content)
            elif hasattr(doc, "text") and doc.text:
                texts.append(doc.text)
            if hasattr(doc, "url") and doc.url:
                source_refs.append(doc.url)
        log(f"[Graph] Добавлено локальных источников")
    
    # ---- ДОБАВЛЕНИЕ ОПРОВЕРЖЕНИЙ В ГРАФ ----
    if refutation_snippets:
        log(f"[Graph] Добавлено {len(refutation_snippets)} опровержений")
        for snippet in refutation_snippets:
            if hasattr(snippet, "text") and snippet.text:
                texts.append(snippet.text)
            elif hasattr(snippet, "content") and snippet.content:
                texts.append(snippet.content)
            if hasattr(snippet, "url") and snippet.url:
                source_refs.append(snippet.url)
        log(f"[Graph] Всего текстов с опровержениями: {len(texts)}")

    
    if not texts:
        texts = [query_to_use]
        log("[Graph] Текстов нет, используем запрос как текст")
    
    log(f"[Graph] Всего текстов: {len(texts)}, источников: {len(source_refs)}")
    
    try:
        hypothesis_graph = build_hypothesis_graph(
            question=query_to_use,
            texts=texts,
            source_refs=source_refs if source_refs else None,
        )
        query_frame["hypothesis_graph"] = hypothesis_graph.__dict__ if hasattr(hypothesis_graph, "__dict__") else hypothesis_graph
        graph_nodes = len(hypothesis_graph.nodes) if hasattr(hypothesis_graph, "nodes") else 0
        log(f"  · Граф гипотез построен (узлов: {graph_nodes})")
        trace.add_observation("hypothesis_graph_built", True)
        trace.add_observation("hypothesis_nodes", graph_nodes)
    except Exception as e:
        log(f"  · Ошибка построения графа гипотез: {e}")
        query_frame["hypothesis_graph"] = None
        trace.add_observation("hypothesis_graph_error", str(e)[:200])

    cost["profile_hypothesis_graph_ms"] = (
        time.time() - _prof_graph_t0
    ) * 1000



    # ---- ЗАБИРАЕМ ПАРАЛЛЕЛЬНО СГЕНЕРИРОВАННЫЙ LOCAL ANSWER ----
    log("[Local] Ожидание фонового ответа...")
    _prof_local_wait_t0 = time.time()
    try:
        local_answer = local_future.result(timeout=180)
    except Exception as e:
        # Infrastructure error is state, not semantic content.
        local_answer = ""
        log(
            f"[Local] unavailable: "
            f"{type(e).__name__}: {e}"
        )
    finally:
        parallel_executor.shutdown(
            wait=False,
            cancel_futures=False,
        )

    cost["profile_local_wait_ms"] = (
        time.time() - _prof_local_wait_t0
    ) * 1000

    query_frame["local_answer"] = local_answer
    log(f"[Local] Ответ готов (длина: {len(local_answer)})")

    # ---- СЛЕПОЙ АНАЛИЗ ВСЕХ ИСТОЧНИКОВ ----
    log("[Blind] Запуск слепого анализа источников...")
    _prof_blind_t0 = time.time()
    blind_result = blind_analysis(
        query_to_use,
        local_answer,
        search_result,
        web_result,
    )
    cost["profile_blind_analysis_ms"] = (
        time.time() - _prof_blind_t0
    ) * 1000
    log(f"[Blind] Лучший источник: {blind_result.get('best_source', 'unknown')}")
    log(f"[Blind] Причина: {blind_result.get('reasoning', 'не указана')[:100]}")
    
    
    # ---- BLIND VERDICT ----
    # Ошибка/невалидный ответ судьи = НЕТ РЕШЕНИЯ.
    # Это не победа и не поражение local_model.
    reasoning = blind_result.get("reasoning", "")
    blind_failed = (
        "invalid" in reasoning.lower()
        or "error" in reasoning.lower()
        or "not enough sources" in reasoning.lower()
    )

    if blind_failed:
        log("[Blind] Судья не вынес валидного решения — результат UNDECIDED")
        blind_result["best_source"] = "unknown"
        blind_result["best_answer"] = local_answer
        blind_result["verdict"] = "undecided"

    elif blind_result.get("best_source") == "local_model":
        log("[Blind] Локальный ответ выиграл. Доверие ↑")
        blind_result["verdict"] = "local_won"

    else:
        log(
            f"[Blind] Локальный ответ проиграл. "
            f"Победитель: {blind_result.get('best_source', 'unknown')}"
        )
        blind_result["verdict"] = "local_lost"
    
    query_frame["blind_analysis"] = blind_result
    query_frame["blind_selected_source"] = blind_result.get("best_source", "unknown")
    query_frame["blind_status"] = "selected" if blind_result.get("best_source") != "unknown" else "undecided"
    query_frame["best_answer"] = blind_result.get("best_answer", local_answer)
    query_frame["refutation_snippets"] = refutation_snippets
    log(f"[Frame] Добавлено {len(refutation_snippets)} опровержений в query_frame")
    # ---- КЛАССИФИКАЦИЯ ИСТОЧНИКОВ ПО ОТНОШЕНИЮ К CLAIM ----
    _prof_source_classification_t0 = time.time()
    try:
        # Для relevance/classification используем основной claim,
        # а не весь многокилобайтный ответ модели.
        best_answer = query_frame.get("best_answer", local_answer)
        main_claim = extract_main_claim(best_answer, query_to_use)
        sources = []
        if search_result and search_result.docs:
            for doc in search_result.docs[:5]:
                sources.append({
                    "type": "local",
                    "text": getattr(doc, "content", "") or getattr(doc, "text", ""),
                    "url": getattr(doc, "url", ""),
                })
        if web_result and web_result.snippets:
            for snippet in web_result.snippets[:5]:
                sources.append({
                    "type": "web",
                    "text": getattr(snippet, "text", "") or getattr(snippet, "content", ""),
                    "url": getattr(snippet, "url", ""),
                })
        if refutation_snippets:
            for snippet in refutation_snippets[:3]:
                sources.append({
                    "type": "refutation",
                    "text": getattr(snippet, "text", "") or getattr(snippet, "content", ""),
                    "url": getattr(snippet, "url", ""),
                })
        # ---- RELEVANCE GATE: фильтруем нерелевантные источники ----
        # ---- ДЕДУПЛИКАЦИЯ ИСТОЧНИКОВ ----
        seen_urls = set()
        unique_sources = []
        for source in sources:
            url = source.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            unique_sources.append(source)
        sources = unique_sources
        relevant_sources = []
        for source in sources:
            text = source.get("text", "")
            # Для локальных источников — более строгий порог
            if source.get("type") == "local":
                threshold = 0.4
            else:
                threshold = 0.2
            
            # Релевантность проверяем относительно вопроса пользователя,
            # а не относительно собственного ответа YANDI.
            if is_relevant(text, query_to_use, threshold=threshold):
                source["relevance"] = "relevant"
                relevant_sources.append(source)
            else:
                source["relevance"] = "rejected_irrelevant"

        if verbose:
            log(f"[V6] Relevance: всего={len(sources)}, релевантных={len(relevant_sources)}")

        # Классифицируем только релевантные источники
        classified = classify_sources(main_claim, relevant_sources)
        query_frame["classified_sources"] = classified
        if verbose:
            log(f"[V6] classified_sources added to query_frame: {list(classified.keys())}")
        if verbose:
            log(f"[V6] Классифицировано источников: supports={len(classified.get('supports', []))}, contradicts={len(classified.get('contradicts', []))}, unrelated={len(classified.get('unrelated', []))}")
    except Exception as e:
        if verbose:
            log(f"[V6] Ошибка классификации источников: {e}")

    cost["profile_source_classification_ms"] = (
        time.time() - _prof_source_classification_t0
    ) * 1000

    # ============================================================
    # SYNTHESIS
    # ============================================================
    #
    # ВАЖНО:
    # timed_out выше относится к предыдущим pipeline steps.
    # Нельзя использовать его как статус synthesize(), иначе успешный
    # synthesis может быть уничтожен из-за старого timeout-флага.
    #
    # synthesize() сейчас вызывается синхронно, поэтому его собственный
    # timeout определяется только фактом возврата/исключения.
    synthesis_timed_out = False
    _prof_synthesis_t0 = time.time()

    try:
        synthesis_result, reasoning_info = synthesize(
            enrich_result,
            search_result=search_result,
            web_result=web_result,
            query_frame=query_frame,
            response_mode=answer_mode,
        )
    except TimeoutError:
        synthesis_timed_out = True
        synthesis_result = None
        reasoning_info = {}

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

        trust_report_data = reasoning_info.get("trust_report", {})
        trust_reasons = []
        coverage_report_data = reasoning_info.get("coverage_report", {})
        claims_data = reasoning_info.get("claims", [])

        # ====================================================
        # CANONICAL EVIDENCE POOL
        # ====================================================
        #
        # Evidence принадлежит Orchestrator, а не Synthesizer.
        #
        # Synthesizer может вернуть собственные evidence_records,
        # но основной web/local/refutation retrieval уже произошёл
        # раньше и не должен исчезать только потому, что Synthesizer
        # его не протащил.
        synthesizer_evidence = reasoning_info.get(
            "evidence_records",
            [],
        ) or []

        pipeline_evidence = build_canonical_evidence_pool(
            search_result=search_result,
            web_result=web_result,
            refutation_snippets=refutation_snippets,
        )

        evidence_data = merge_evidence(
            pipeline_evidence,
            synthesizer_evidence,
        )

        technical_errors = reasoning_info.get(
            "technical_errors",
            [],
        )

        if verbose:
            direct_count = sum(
                1
                for ev in evidence_data
                if (
                    ev.get("evidence_role") == "direct"
                    and
                    ev.get("evidence_eligible") is True
                )
            )

            context_count = sum(
                1
                for ev in evidence_data
                if ev.get("evidence_role") == "context"
            )

            origins = {}

            for ev in evidence_data:
                origin = ev.get(
                    "retrieval_origin",
                    "unknown",
                )

                origins[origin] = (
                    origins.get(origin, 0) + 1
                )

            log(
                f"[Evidence Pool] "
                f"total={len(evidence_data)} "
                f"direct={direct_count} "
                f"context={context_count} "
                f"origins={origins}"
            )

        if claims_data and evidence_data:
            # ---- NORMALIZE CLAIMS BEFORE EVIDENCE MAPPING ----
            # Synthesizer/claim extractor может вернуть claims как строки
            # или как словари. Mapper ожидает только словари.
            normalized_claims = []
            for claim in claims_data or []:
                if isinstance(claim, dict):
                    if "claim_text" not in claim:
                        if "text" in claim:
                            claim["claim_text"] = claim["text"]
                        elif "claim" in claim:
                            claim["claim_text"] = claim["claim"]
                    normalized_claims.append(claim)
                elif isinstance(claim, str):
                    text = claim.strip()
                    if text:
                        normalized_claims.append({
                            "claim_text": text,
                            "source": "synthesizer"
                        })
                else:
                    log(f"[Claims] Пропущен claim неизвестного типа: {type(claim).__name__}")
            claims_data = normalized_claims
            log(f"[Claims] Normalized claims: {len(claims_data)}")

        # ============================================================
        # CLAIM IDENTITY
        # ============================================================
        #
        # Claim должен иметь стабильный ID ещё ДО Validator/Mapper.
        # Иначе rejected claim может потерять идентичность, поскольку
        # раньше claim_id иногда создавался только внутри Mapper.
        for claim in claims_data:
            if not claim.get("claim_id"):
                claim["claim_id"] = f"cl_{uuid.uuid4().hex[:8]}"

            if not claim.get("claim_type"):
                claim["claim_type"] = "factual"

            if "claim_confidence" not in claim:
                claim["claim_confidence"] = 0.5

            # ========================================================
            # CLAIM QUERY CONTEXT
            # ========================================================
            #
            # Atomic claim может потерять явный субъект:
            #
            #   query:
            #       "Есть ли разумная жизнь на Юпитере?"
            #
            #   claim:
            #       "Температура варьируется от -145°C..."
            #
            # query_context используется ТОЛЬКО retrieval-слоем
            # для восстановления предметного контекста.
            #
            # Сам claim_text не изменяется и позже именно он
            # проверяется через NLI.
            if not claim.get("query_context"):
                claim["query_context"] = query_to_use

        # ============================================================
        # STRUCTURAL CLAIM VALIDATION
        # ============================================================
        #
        # Порядок принципиален:
        #
        #   Normalize
        #       ↓
        #   Structural Validator
        #       ├── rejected → diagnostic trace only
        #       ↓
        #   accepted claims
        #       ↓
        #   Mapper → NLI → Epistemic Status
        #
        # Structural rejection НЕ означает ложность утверждения.
        # Это означает только: объект непригоден как атомарный claim.
        rejected_structural_claims = []

        # G (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md, §G): весь блок
        # ClaimValidator + Mapper PASS1 + NLI PASS1 раньше был частью
        # 275.87s unaccounted. Оборачиваем целиком одним таймером —
        # детализация внутри не нужна для profile coverage, у каждого
        # этапа уже есть собственный print с деталями.
        _t0_claim_setup = time.time()

        # P0 (YANDI_CLAIM_LIFECYCLE_DISAPPEARANCE_AUDIT.md): диагностический
        # boundary-трейс, без изменения поведения. synthesized — то, что
        # реально вернул synthesize() в reasoning_info["claims"]; lifecycle/
        # validator_input — тот же claims_data непосредственно перед тем,
        # как он передаётся в ClaimValidator.filter_claims(). Если
        # lifecycle>0, а validator_input==0 когда-нибудь снова — это уже
        # не может быть тем же багом (claims теперь сохраняются даже при
        # позднем сбое synthesize()), значит источник новый.
        if verbose:
            log(
                "[Claim Pipeline Boundary] "
                f"synthesized={len(reasoning_info.get('claims', [])) if isinstance(reasoning_info, dict) else 0} "
                f"lifecycle={len(claims_data)} "
                f"validator_input={len(claims_data)}"
            )

        if _claim_validator:
            try:
                pre_validation_claims = list(claims_data)

                claims_data = _claim_validator.filter_claims(
                    pre_validation_claims
                )

                rejected_structural_claims = [
                    claim
                    for claim in pre_validation_claims
                    if (
                        claim.get("structural_validation") == "rejected"
                        or claim.get("_rejected") is True
                    )
                ]

                # Rejected claims не исчезают.
                # Они сохраняются отдельно как диагностические объекты.
                for claim in rejected_structural_claims:
                    trace.rejected_claims.append({
                        "claim_id": claim.get(
                            "claim_id",
                            "unknown",
                        ),
                        "claim_text": (
                            claim.get("claim_text", "") or ""
                        )[:200],
                        "claim_type": claim.get(
                            "claim_type",
                            "unknown",
                        ),
                        "rejection_reason": claim.get(
                            "_rejected_reason",
                            "structural_validation",
                        ),
                    })

                if verbose:
                    log(
                        f"[Claim Validator] "
                        f"accepted={len(claims_data)} "
                        f"rejected={len(rejected_structural_claims)} "
                        f"reasons={_claim_validator.rejection_reasons}"
                    )

                    for claim in claims_data:
                        log(
                            "[Claim Validator] ACCEPT: "
                            f"{claim.get('claim_text', '')[:250]}"
                        )

                    for claim in rejected_structural_claims:
                        log(
                            "[Claim Validator] REJECT: "
                            f"reason={claim.get('_rejected_reason', 'unknown')} "
                            f"text={claim.get('claim_text', '')[:250]}"
                        )

            except Exception as e:
                if verbose:
                    log(
                        f"[Claim Validator] error={e}"
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

        def _run_claim_evidence_batch(
            claims,
            evidence,
            batch_label,
        ):
            evidence_by_id_local = {
                ev.get("evidence_id"): ev
                for ev in (evidence or [])
                if ev.get("evidence_id")
            }

            jobs = []

            for claim in claims:
                claim_text = (
                    claim.get("claim_text") or ""
                ).strip()

                if not claim_text:
                    claim["evidence_relations"] = []
                    continue

                linked_ids = list(
                    claim.get(
                        "derived_from_evidence_ids",
                        [],
                    ) or []
                )

                candidate_sources = []

                for ev_id in linked_ids:
                    ev = evidence_by_id_local.get(ev_id)

                    if not ev:
                        continue

                    ev_text = (
                        ev.get("content_excerpt") or ""
                    ).strip()

                    if not ev_text:
                        continue

                    # P0-F: directness — НЕЗАВИСИМЫЙ от source authority
                    # сигнал "насколько конкретно ЭТОТ passage отвечает
                    # ЭТОМУ claim". Считается здесь (а не в
                    # source_quality.py), потому что evaluate_source_quality()
                    # вызывается ДО того, как claim, к которому evidence
                    # привяжется, вообще известен (PASS1 evidence общий
                    # для многих claims) — сама evidence-запись не может
                    # нести per-claim directness, только per-pair.
                    directness = evaluate_evidence_directness(
                        claim_text,
                        ev_text,
                    )

                    if verbose:
                        _reason_bits = []
                        if ev.get("evidence_role") == "direct" and ev.get("evidence_eligible") is True:
                            _reason_bits.append("authority_eligible")
                        if directness >= 0.60:
                            _reason_bits.append("directness_strong")
                        if not _reason_bits:
                            _reason_bits.append("neither_path_qualifies")

                        log(
                            "[Evidence Eligibility] "
                            f"claim={claim.get('claim_id', '')} "
                            f"ev={ev_id} "
                            f"source_class={ev.get('source_class', 'unknown')} "
                            f"quality={ev.get('quality_score', 0.0):.3f} "
                            f"directness={directness:.3f} "
                            f"role={ev.get('evidence_role', 'context')} "
                            f"eligible={ev.get('evidence_eligible', False)} "
                            f"reason={'+'.join(_reason_bits)}"
                        )

                    candidate_sources.append({
                        "evidence_id": ev_id,
                        "type": ev.get(
                            "source_type",
                            "evidence",
                        ),
                        "text": ev_text,
                        "url": ev.get(
                            "source_uri",
                            "",
                        ),
                        "source_class": ev.get(
                            "source_class",
                            "unknown",
                        ),
                        "quality_score": ev.get(
                            "quality_score",
                            0.0,
                        ),
                        "evidence_eligible": ev.get(
                            "evidence_eligible",
                            False,
                        ),
                        "evidence_role": ev.get(
                            "evidence_role",
                            "context",
                        ),
                        # P0-E: registry evidence — прошлые UNVERIFIED
                        # ответы самой модели (см. отчёт), не внешняя
                        # provenance. Directness-путь ниже обязан их
                        # исключать явно, иначе получится circular
                        # self-validation.
                        "retrieval_origin": ev.get(
                            "retrieval_origin",
                            "",
                        ),
                        "directness": directness,
                        "relevance": "relevant",
                    })

                jobs.append({
                    "claim_id": claim.get(
                        "claim_id",
                        "",
                    ),
                    "claim_text": claim_text,
                    "sources": candidate_sources,
                })

            started = time.time()

            classified = classify_claim_evidence_batch(
                jobs,
                batch_size=8,
            )

            elapsed = time.time() - started

            relation_count = 0

            for claim in claims:
                claim_id = claim.get(
                    "claim_id",
                    "",
                )

                grouped = classified.get(
                    claim_id,
                    {},
                )

                evidence_relations = []

                for relation in (
                    "supports",
                    "contradicts",
                    "uncertain",
                    "unrelated",
                ):
                    for source in (
                        grouped.get(
                            relation,
                            [],
                        ) or []
                    ):
                        ev_id = source.get(
                            "evidence_id"
                        )

                        if not ev_id:
                            continue

                        evidence_relations.append({
                            "evidence_id": ev_id,
                            "relation": relation,
                            "method": source.get(
                                "relation_method",
                                "unknown",
                            ),
                            "source_claim": source.get(
                                "source_claim",
                                "",
                            ),
                            "error": source.get(
                                "relation_error",
                            ),
                            "source_class": source.get(
                                "source_class",
                                "unknown",
                            ),
                            "quality_score": source.get(
                                "quality_score",
                                0.0,
                            ),
                            "evidence_eligible": source.get(
                                "evidence_eligible",
                                False,
                            ),
                            "evidence_role": source.get(
                                "evidence_role",
                                "context",
                            ),
                            "retrieval_origin": source.get(
                                "retrieval_origin",
                                "",
                            ),
                            "directness": source.get(
                                "directness",
                                0.0,
                            ),
                        })

                        relation_count += 1

                claim["evidence_relations"] = (
                    evidence_relations
                )

            if verbose:
                pair_count = sum(
                    len(job.get("sources", []) or [])
                    for job in jobs
                )

                generation_calls = (
                    (pair_count + 7) // 8
                    if pair_count
                    else 0
                )

                log(
                    f"[Claim Evidence Batch {batch_label}] "
                    f"claims={len(jobs)} "
                    f"pairs={pair_count} "
                    f"relations={relation_count} "
                    f"generation_calls<={generation_calls} "
                    f"time={elapsed:.2f}s"
                )

            return relation_count


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
        claim_relation_count = _run_claim_evidence_batch(
            claims_data,
            evidence_data,
            "PASS1",
        )

        if verbose:
            log(
                f"[Claim Evidence NLI] "
                f"relations classified={claim_relation_count}"
            )

        cost["claim_setup_ms"] = (
            (time.time() - _t0_claim_setup) * 1000
        )

        # ============================================================
        # CLAIM RESOLUTION GATE + SECOND RETRIEVAL PASS
        # ============================================================
        #
        # Первый Mapper + Claim↔Evidence NLI уже выполнены.
        #
        # Теперь можно отличить:
        #
        #   semantic candidate link
        #
        # от:
        #
        #   epistemically effective evidence.
        #
        # Claim считается resolved только если существует хотя бы одно
        # DIRECT + ELIGIBLE evidence с отношением:
        #
        #   supports | contradicts
        #
        # uncertain / unrelated / context / secondary не останавливают
        # claim-specific retrieval.
        # ------------------------------------------------------------

        def _claim_has_effective_evidence(claim):
            for rel in claim.get("evidence_relations", []) or []:
                if (
                    rel.get("evidence_role") == "direct"
                    and rel.get("evidence_eligible") is True
                    and rel.get("relation") in {
                        "supports",
                        "contradicts",
                    }
                ):
                    return True

            return False


        retrieval_claims = [
            claim
            for claim in claims_data
            if claim.get("verification_status") != "rejected"
            and not _claim_has_effective_evidence(claim)
        ]

        if verbose:
            resolved_count = (
                len(claims_data) - len(retrieval_claims)
            )

            log(
                f"[Claim Resolution Gate] "
                f"claims={len(claims_data)} "
                f"resolved={resolved_count} "
                f"need_retrieval={len(retrieval_claims)}"
            )


        # ============================================================
        # CLAIM-SPECIFIC RETRIEVAL — SECOND PASS
        # ============================================================

        if (
            enable_web
            and retrieval_claims
            and not _skip_rag
            and not is_subjective_answer
        ):
            try:
                # P1.2 (YANDI_FULL_PIPELINE_AUDIT.md, §26/§33):
                # эта фаза раньше не попадала в [PROFILE] вообще,
                # хотя в реальном прогоне занимала 47% total latency
                # ([Claim Retrieval Timing] wall=240.74s из 509.74s).
                _claim_retrieval_t0 = time.time()

                claim_retrieved_evidence = retrieve_for_claims(
                    retrieval_claims
                )

                cost["claim_retrieval_ms"] = (
                    (time.time() - _claim_retrieval_t0) * 1000
                )

                evidence_before = len(evidence_data)

                evidence_data = merge_evidence(
                    evidence_data,
                    claim_retrieved_evidence,
                )

                added_count = (
                    len(evidence_data) - evidence_before
                )

                if verbose:
                    log(
                        f"[Claim Retrieval Pass 2] "
                        f"requested={len(retrieval_claims)} "
                        f"returned={len(claim_retrieved_evidence)} "
                        f"added={added_count} "
                        f"evidence_total={len(evidence_data)}"
                    )


                # ====================================================
                # SECOND MAPPER + NLI PASS
                # ====================================================
                #
                # Выполняем только если retrieval действительно
                # расширил canonical evidence pool.
                #
                # Mapper снова остаётся единственным владельцем
                # derived_from_evidence_ids.
                # ----------------------------------------------------

                if added_count > 0:

                    _t0_pass2_mapping_nli = time.time()

                    mapped_claims = map_claims_to_evidence(
                        claims_data,
                        evidence_data,
                    )

                    mapped_by_id = {
                        mc.claim_id: mc
                        for mc in mapped_claims
                        if getattr(mc, "claim_id", None)
                    }

                    for claim in claims_data:
                        claim_id = claim.get("claim_id")
                        mapped = mapped_by_id.get(claim_id)

                        if mapped is None:
                            claim["derived_from_evidence_ids"] = []
                            continue

                        claim["derived_from_evidence_ids"] = list(
                            mapped.derived_from_evidence_ids or []
                        )


                    # -----------------------------------------------
                    # CLAIM <-> EVIDENCE NLI — PASS 2
                    # -----------------------------------------------
                    #
                    # После второго retrieval Mapper уже обновил
                    # derived_from_evidence_ids.
                    #
                    # Повторяем NLI через тот же batch helper.
                    claim_relation_count_pass2 = _run_claim_evidence_batch(
                        claims_data,
                        evidence_data,
                        "PASS2",
                    )

                    cost["claim_pass2_mapping_nli_ms"] = (
                        (time.time() - _t0_pass2_mapping_nli) * 1000
                    )

                    # ====================================================
                    # PASS 2 TRACE
                    # ====================================================
                    #
                    # Диагностика остаётся отдельно от NLI execution.
                    # Здесь ничего не классифицируется повторно.
                    if verbose:
                        evidence_by_id = {
                            ev.get("evidence_id"): ev
                            for ev in (evidence_data or [])
                            if ev.get("evidence_id")
                        }

                        for claim in claims_data:
                            claim_text = (
                                claim.get("claim_text") or ""
                            ).strip()

                            linked_ids = list(
                                claim.get(
                                    "derived_from_evidence_ids",
                                    [],
                                ) or []
                            )

                            evidence_relations = list(
                                claim.get(
                                    "evidence_relations",
                                    [],
                                ) or []
                            )

                            log(
                                f"[Pass2 Trace] "
                                f"claim={claim.get('claim_id', 'unknown')} "
                                f"linked={len(linked_ids)} "
                                f"relations={len(evidence_relations)} "
                                f"text={claim_text[:140]}"
                            )

                            relation_by_evidence = {
                                rel.get("evidence_id"): rel
                                for rel in evidence_relations
                                if rel.get("evidence_id")
                            }

                            for ev_id in linked_ids:
                                ev = evidence_by_id.get(ev_id)

                                if not ev:
                                    log(
                                        f"[Pass2 Trace] "
                                        f"  ev={ev_id} MISSING"
                                    )
                                    continue

                                rel = relation_by_evidence.get(
                                    ev_id,
                                    {},
                                )

                                log(
                                    f"[Pass2 Trace] "
                                    f"  ev={ev_id} "
                                    f"role={ev.get('evidence_role', 'context')} "
                                    f"eligible={ev.get('evidence_eligible', False)} "
                                    f"quality={ev.get('quality_score', 0.0):.3f} "
                                    f"relation={rel.get('relation', 'NO_RELATION')} "
                                    f"method={rel.get('method', 'unknown')} "
                                    f"class={ev.get('source_class', 'unknown')} "
                                    f"owner={ev.get('retrieval_claim_id', '')} "
                                    f"url={ev.get('source_uri', '')[:180]}"
                                )

                                log(
                                    f"[Pass2 Trace] "
                                    f"    source_claim="
                                    f"{rel.get('source_claim', '')[:350]}"
                                )

                                log(
                                    f"[Pass2 Trace] "
                                    f"    excerpt="
                                    f"{(ev.get('content_excerpt') or '')[:500]}"
                                )

                    if verbose:
                        log(
                            f"[Claim Evidence NLI Pass 2] "
                            f"relations classified="
                            f"{claim_relation_count_pass2}"
                        )

            except Exception as e:
                if verbose:
                    log(
                        f"[Claim Retrieval Pass 2] error={e}"
                    )


        # ---- CLAIM EPISTEMIC STATUS ----
        #
        # Structural validator отвечает только на вопрос:
        # "является ли объект нормальным claim?"
        #
        # NLI отвечает:
        # "что связанные evidence говорят ОБ ЭТОМ claim?"
        #
        # Поэтому candidate после NLI переводится в более точное
        # эпистемическое состояние.
        #
        # ВАЖНО:
        # supported != verified.
        # Наличие одного или нескольких согласующихся evidence
        # ещё не доказывает истинность утверждения.
        #
        # verified здесь намеренно НЕ назначается.
        claim_status_counts = {
            "supported": 0,
            "disputed": 0,
            "contradicted": 0,
            "unverified": 0,
            "rejected": 0,
        }

        # ============================================================
        # P0-F (YANDI_EVIDENCE_ELIGIBILITY_AND_REGISTRY_AUDIT.md):
        # ============================================================
        #
        # Раньше единственный путь в supports/contradicts был
        # role=="direct" AND eligible==True — оба поля производные
        # ИСКЛЮЧИТЕЛЬНО от домена источника (source_quality.py), без
        # учёта того, насколько КОНКРЕТНО passage отвечает claim.
        # Математически доказано: source_class="unknown" (любой домен
        # вне узкого whitelist) НЕ может пересечь eligibility threshold
        # ни при каком содержимом (max quality_score≈0.655 < 0.70).
        #
        # Добавлен ВТОРОЙ, независимый путь: directness — насколько
        # конкретный passage семантически близок claim (per-pair
        # embedding similarity, см. source_quality.py::
        # evaluate_evidence_directness). Authority-путь остаётся
        # первым и НЕ ослаблен — это дополнение, не замена, не
        # понижение порога 0.70/0.55.
        #
        # HARD_BLOCKED_SOURCE_CLASSES зеркалит source_quality.py's
        # blocked_classes — форумы/соцсети/блоги/спекулятивные/
        # новостные/pipeline-generated источники остаются исключены
        # ДАЖЕ при высокой directness (тема НЕ авторитетность).
        HARD_BLOCKED_SOURCE_CLASSES = {
            "generated_pipeline",
            "social",
            "forum",
            "blog_opinion",
            "speculative",
            "news",
            "popular_article",
        }

        # Калибровка не произвольная: 0.60 — та же граница
        # "семантически близко к supports", что уже используется в
        # claim_relation.py::classify_relation() для эмбеддингового
        # fallback (similarity>=0.60 -> SUPPORTS). Переиспользуем уже
        # установленную в проекте отметку, не изобретаем новую.
        DIRECTNESS_SUPPORT_THRESHOLD = 0.60

        def _counts_toward_status(rel):
            """
            Возвращает (counted: bool, via: str|None).

            via == "authority" — старый путь (role=direct+eligible),
            via == "directness" — новый путь (P0-F), НЕ применяется
            к local registry (P0-E: registry — прошлые UNVERIFIED
            ответы модели, не внешняя provenance; допускать их через
            directness означало бы модель подтверждает себя же).
            """
            if (
                rel.get("evidence_role") == "direct"
                and rel.get("evidence_eligible") is True
            ):
                return True, "authority"

            if (
                rel.get("source_class") not in HARD_BLOCKED_SOURCE_CLASSES
                and rel.get("retrieval_origin") != "local_registry"
                and float(rel.get("directness", 0.0) or 0.0)
                >= DIRECTNESS_SUPPORT_THRESHOLD
            ):
                return True, "directness"

            return False, None

        for claim in claims_data:
            current_status = claim.get(
                "verification_status",
                "candidate",
            )

            # Structural rejection имеет приоритет.
            if current_status == "rejected":
                claim_status_counts["rejected"] += 1
                continue

            relations = list(
                claim.get("evidence_relations", []) or []
            )

            # P0-F: authority ИЛИ доказанная directness (см. выше).
            # secondary/context/internal relations, не прошедшие ни
            # один из двух путей, сохраняются для диагностики, но не
            # превращают claim в supported/contradicted.
            direct_relations = []

            for rel in relations:
                counted, via = _counts_toward_status(rel)

                if counted:
                    rel["counted_via"] = via
                    direct_relations.append(rel)

                    if verbose:
                        log(
                            "[Claim Support Decision] "
                            f"claim={claim.get('claim_id')} "
                            f"ev={rel.get('evidence_id')} "
                            f"relation={rel.get('relation')} "
                            f"via={via} "
                            f"directness={float(rel.get('directness', 0.0) or 0.0):.3f} "
                            f"counted=True"
                        )

            supports_count = sum(
                1
                for rel in direct_relations
                if rel.get("relation") == "supports"
            )

            contradicts_count = sum(
                1
                for rel in direct_relations
                if rel.get("relation") == "contradicts"
            )

            secondary_count = sum(
                1
                for rel in relations
                if rel.get("evidence_role") == "secondary"
                and rel.get("relation") in {
                    "supports",
                    "contradicts",
                }
            )

            context_count = sum(
                1
                for rel in relations
                if rel.get("evidence_role") == "context"
                and rel.get("relation") in {
                    "supports",
                    "contradicts",
                }
            )

            if supports_count > 0 and contradicts_count > 0:
                new_status = "disputed"

            elif supports_count > 0:
                new_status = "supported"

            elif contradicts_count > 0:
                new_status = "contradicted"

            else:
                # uncertain / unrelated / отсутствие evidence
                # не дают основания считать claim поддержанным.
                new_status = "unverified"

            claim["verification_status"] = new_status

            # Эти два счётчика означают только epistemically effective
            # DIRECT evidence.
            claim["support_count"] = supports_count
            claim["contradiction_count"] = contradicts_count

            # Диагностика неавторитетных/вторичных отношений.
            claim["secondary_relation_count"] = secondary_count
            claim["context_relation_count"] = context_count

            claim_status_counts[new_status] += 1

            if verbose:
                log(
                    f"[Claim Status] "
                    f"claim={claim.get('claim_id')} "
                    f"{current_status}->{new_status} "
                    f"supports={supports_count} "
                    f"contradicts={contradicts_count} "
                    f"secondary={secondary_count} "
                    f"context={context_count}"
                )

        if verbose:
            log(
                "[Claim Status] "
                f"supported={claim_status_counts['supported']} "
                f"disputed={claim_status_counts['disputed']} "
                f"contradicted={claim_status_counts['contradicted']} "
                f"unverified={claim_status_counts['unverified']} "
                f"rejected={claim_status_counts['rejected']}"
            )

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

        # ---- YANDI V6: BELIEFS ----
        #
        # Belief != истина.
        #
        # Источник истины для evidence_for/evidence_against:
        #
        #     claim["evidence_relations"]
        #
        # Relation общего main_claim здесь больше НЕ используется.
        if _belief_manager and claims_data:
            try:
                belief_updates_count = 0

                for claim in claims_data[:3]:
                    claim_text = (claim.get("claim_text") or "").strip()

                    if not claim_text or len(claim_text) <= 20:
                        continue

                    evidence_relations = list(
                        claim.get("evidence_relations", []) or []
                    )

                    # BeliefManager получает только DIRECT evidence,
                    # прошедшие Source Quality Gate.
                    #
                    # secondary/context/internal могут храниться в trace,
                    # но не имеют права напрямую двигать belief.
                    belief_relations = [
                        rel
                        for rel in evidence_relations
                        if rel.get("evidence_role") == "direct"
                        and rel.get("evidence_eligible") is True
                    ]

                    evidence_for = [
                        rel.get("evidence_id")
                        for rel in belief_relations
                        if rel.get("relation") == "supports"
                        and rel.get("evidence_id")
                    ]

                    evidence_against = [
                        rel.get("evidence_id")
                        for rel in belief_relations
                        if rel.get("relation") == "contradicts"
                        and rel.get("evidence_id")
                    ]

                    # uncertain / unrelated / missing relation
                    # не считаются ни поддержкой, ни опровержением.

                    belief_confidence = min(
                        float(claim.get("claim_confidence", 0.5)),
                        0.5,
                    )

                    if evidence_against and not evidence_for:
                        belief_confidence = min(
                            belief_confidence,
                            0.35,
                        )

                    _belief_manager.add_belief(
                        topic=epistemic_result.domain
                        if not is_subjective_answer
                        else "subjective",
                        statement=claim_text[:200],
                        confidence=belief_confidence,
                        evidence_for=evidence_for,
                        evidence_against=evidence_against,
                        claim_ids=[claim.get("claim_id")],
                    )

                    belief_updates_count += 1

                    if verbose:
                        log(
                            f"[Belief] candidate={claim.get('claim_id')} "
                            f"for={len(evidence_for)} "
                            f"against={len(evidence_against)} "
                            f"conf={belief_confidence:.2f}"
                        )

                if verbose:
                    stats = _belief_manager.get_stats()
                    log(
                        f"[V6] Beliefs обработано: {belief_updates_count}, "
                        f"всего в памяти: {stats['total']}"
                    )

            except Exception as e:
                belief_updates_count = 0
                if verbose:
                    log(f"[V6] Ошибка добавления убеждений: {e}")

        # ---- YANDI V6: LINKER ----
        supporting_ids = []
        if _claim_answer_linker:
            try:
                _, supporting_ids = _claim_answer_linker.link_answer_to_claims(
                    answer=synthesis_result.answer,
                    claims=claims_data,
                )
                if verbose and supporting_ids:
                    log(f"[V6] Связано claims: {len(supporting_ids)}")
            except Exception as e:
                if verbose:
                    log(f"[V6] Ошибка линковки: {e}")

        # ---- YANDI V6: PERSONALITY ----
        if _personality_core:
            try:
                _personality_core.increment_cycles()
                _personality_core.increment_decisions()
                if verbose:
                    summary = _personality_core.get_summary()
                    log(f"[V6] Личность: {summary['name']}, циклов {summary['cycles']}")
            except Exception as e:
                if verbose:
                    log(f"[V6] Ошибка личности: {e}")

        # ---- YANDI V6: DISAGREEMENT ----
        if _disagreement_engine and claims_data and len(claims_data) > 1:
            try:
                import math
                import time as _time

                disagreement_started = _time.time()

                # ====================================================
                # CLAIM ↔ CLAIM SEMANTIC PREFILTER + BATCH NLI
                # ====================================================
                #
                # Полный граф имеет:
                #
                #     N * (N - 1) / 2
                #
                # пар.
                #
                # Раньше все пары отправлялись в LLM NLI.
                #
                # Теперь:
                #
                # claims
                #   ↓
                # embeddings — один раз на claim
                #   ↓
                # cosine similarity всех пар
                #   ↓
                # мягкий semantic prefilter
                #   ↓
                # batch LLM NLI только для candidate pairs
                #
                # ВАЖНО:
                #
                # embedding НЕ определяет supports/contradicts.
                #
                # Он используется только как дешёвый retrieval gate:
                # "есть ли вообще смысл отдавать эту пару дорогому NLI?"
                #
                # Финальное логическое отношение по-прежнему
                # определяет только LLM NLI.

                CLAIM_CONFLICT_SIM_THRESHOLD = 0.30
                CLAIM_CONFLICT_TOP_K = 3
                CLAIM_CONFLICT_BATCH_SIZE = 16

                active_claims = []

                for original_index, claim in enumerate(claims_data):
                    claim_text = (
                        claim.get("claim_text", "") or ""
                    ).strip()

                    if not claim_text:
                        continue

                    active_claims.append({
                        "original_index": original_index,
                        "claim": claim,
                        "text": claim_text,
                    })

                total_pairs = (
                    len(active_claims)
                    * (len(active_claims) - 1)
                ) // 2

                # ----------------------------------------------------
                # EMBEDDINGS
                # ----------------------------------------------------

                embedding_started = _time.time()

                semantic_available = False
                semantic_error = None
                claim_vectors = {}

                try:
                    import requests
                    import numpy as np

                    embed_session = requests.Session()
                    embed_session.trust_env = False

                    def _claim_embed(value: str):
                        resp = embed_session.post(
                            "http://127.0.0.1:11434/api/embed",
                            json={
                                "model": "embeddinggemma:latest",
                                "input": value[:2000],
                            },
                            timeout=30,
                        )

                        resp.raise_for_status()

                        vec = np.array(
                            resp.json()["embeddings"][0],
                            dtype=np.float32,
                        )

                        norm = np.linalg.norm(vec)

                        return (
                            vec / norm
                            if norm > 0
                            else vec
                        )

                    for idx, item in enumerate(active_claims):
                        claim_vectors[idx] = _claim_embed(
                            item["text"]
                        )

                    semantic_available = (
                        len(claim_vectors)
                        == len(active_claims)
                    )

                except Exception as exc:
                    semantic_error = (
                        f"{type(exc).__name__}: {exc}"
                    )

                    semantic_available = False

                embedding_elapsed = (
                    _time.time() - embedding_started
                )

                # ----------------------------------------------------
                # PAIRWISE COSINE
                # ----------------------------------------------------

                prefilter_started = _time.time()

                all_pair_scores = []
                neighbor_scores = {
                    i: []
                    for i in range(len(active_claims))
                }

                if semantic_available:
                    for i in range(len(active_claims)):
                        v1 = claim_vectors[i]

                        for j in range(
                            i + 1,
                            len(active_claims),
                        ):
                            v2 = claim_vectors[j]

                            similarity = float(
                                np.dot(v1, v2)
                            )

                            all_pair_scores.append({
                                "i": i,
                                "j": j,
                                "similarity": similarity,
                            })

                            neighbor_scores[i].append(
                                (similarity, j)
                            )

                            neighbor_scores[j].append(
                                (similarity, i)
                            )

                    # Top-K semantic neighbors каждого claim.
                    top_neighbors = {
                        i: set()
                        for i in range(len(active_claims))
                    }

                    for i, scores in neighbor_scores.items():
                        scores.sort(
                            key=lambda item: item[0],
                            reverse=True,
                        )

                        for _, neighbor in scores[
                            :CLAIM_CONFLICT_TOP_K
                        ]:
                            top_neighbors[i].add(
                                neighbor
                            )

                    candidate_pair_keys = set()

                    for item in all_pair_scores:
                        i = item["i"]
                        j = item["j"]
                        similarity = item["similarity"]

                        threshold_match = (
                            similarity
                            >= CLAIM_CONFLICT_SIM_THRESHOLD
                        )

                        top_k_match = (
                            j in top_neighbors[i]
                            or i in top_neighbors[j]
                        )

                        if (
                            threshold_match
                            or top_k_match
                        ):
                            candidate_pair_keys.add(
                                (i, j)
                            )

                else:
                    # ------------------------------------------------
                    # FAIL-OPEN FOR CORRECTNESS
                    # ------------------------------------------------
                    #
                    # Если embedding pipeline сломан, НЕ теряем
                    # потенциальные конфликты.
                    #
                    # В таком случае возвращаемся к полному набору пар,
                    # но всё равно используем batch NLI.
                    candidate_pair_keys = {
                        (i, j)
                        for i in range(len(active_claims))
                        for j in range(
                            i + 1,
                            len(active_claims),
                        )
                    }

                prefilter_elapsed = (
                    _time.time() - prefilter_started
                )

                # ----------------------------------------------------
                # BUILD BATCH PAIRS
                # ----------------------------------------------------

                claim_pairs = []
                pair_claims = {}

                for i, j in sorted(candidate_pair_keys):
                    item1 = active_claims[i]
                    item2 = active_claims[j]

                    c1 = item1["claim"]
                    c2 = item2["claim"]

                    text1 = item1["text"]
                    text2 = item2["text"]

                    if text1 == text2:
                        continue

                    original_i = item1["original_index"]
                    original_j = item2["original_index"]

                    pair_id = (
                        f"{original_i}:{original_j}"
                    )

                    claim_pairs.append({
                        "pair_id": pair_id,
                        "main_claim": text1,
                        "other_claim": text2,
                    })

                    pair_claims[pair_id] = (
                        c1,
                        c2,
                    )

                candidate_count = len(claim_pairs)

                skipped_count = max(
                    0,
                    total_pairs - candidate_count,
                )

                if verbose:
                    log(
                        f"[Claim↔Claim Prefilter] "
                        f"claims={len(active_claims)} "
                        f"total_pairs={total_pairs} "
                        f"candidates={candidate_count} "
                        f"skipped={skipped_count} "
                        f"threshold="
                        f"{CLAIM_CONFLICT_SIM_THRESHOLD:.2f} "
                        f"top_k={CLAIM_CONFLICT_TOP_K} "
                        f"semantic="
                        f"{'ok' if semantic_available else 'fallback'}"
                    )

                    if semantic_error:
                        log(
                            f"[Claim↔Claim Prefilter] "
                            f"embedding_error="
                            f"{semantic_error[:180]}"
                        )

                # ----------------------------------------------------
                # BATCH NLI
                # ----------------------------------------------------

                nli_started = _time.time()

                batch_results = (
                    infer_claim_relations_batch(
                        claim_pairs,
                        batch_size=(
                            CLAIM_CONFLICT_BATCH_SIZE
                        ),
                    )
                    if claim_pairs
                    else []
                )

                nli_elapsed = (
                    _time.time() - nli_started
                )

                llm_classified_count = sum(
                    1
                    for result in batch_results
                    if result.get("method")
                    == "llm_nli_batch"
                )

                fallback_count = sum(
                    1
                    for result in batch_results
                    if result.get("method")
                    in {
                        "batch_fallback",
                        "batch_missing",
                    }
                )

                contradiction_count = 0

                for result in batch_results:
                    pair_id = str(
                        result.get("pair_id", "")
                    )

                    relation = result.get(
                        "relation"
                    )

                    method = result.get(
                        "method"
                    )

                    # Не печатаем сотни unrelated / uncertain.
                    #
                    # Детально логируем только реальные
                    # потенциальные конфликты или batch failure.
                    if verbose and (
                        relation == "contradicts"
                        or method
                        in {
                            "batch_fallback",
                            "batch_missing",
                        }
                    ):
                        log(
                            f"[Claim↔Claim Batch] "
                            f"pair={pair_id} "
                            f"relation={relation} "
                            f"method={method}"
                        )

                    # Только настоящий LLM batch result имеет право
                    # породить disagreement.
                    if not (
                        method == "llm_nli_batch"
                        and relation == "contradicts"
                    ):
                        continue

                    pair = pair_claims.get(
                        pair_id
                    )

                    if not pair:
                        continue

                    c1, c2 = pair

                    contradiction_count += 1

                    _disagreement_engine.challenge(
                        topic=(
                            epistemic_result.domain
                            if not is_subjective_answer
                            else "subjective"
                        ),
                        old_position=(
                            c1.get(
                                "claim_text",
                                "",
                            )[:100]
                        ),
                        challenge=(
                            "Конфликт с утверждением: "
                            + c2.get(
                                "claim_text",
                                "",
                            )[:100]
                        ),
                        analysis=(
                            "Два утверждения "
                            "противоречат друг другу"
                        ),
                        new_position=(
                            c2.get(
                                "claim_text",
                                "",
                            )[:100]
                            if c2.get(
                                "claim_confidence",
                                0,
                            )
                            > c1.get(
                                "claim_confidence",
                                0,
                            )
                            else c1.get(
                                "claim_text",
                                "",
                            )[:100]
                        ),
                        confidence_before=c1.get(
                            "claim_confidence",
                            0.5,
                        ),
                        confidence_after=c2.get(
                            "claim_confidence",
                            0.5,
                        ),
                    )

                    if verbose:
                        log(
                            "[V6] Зафиксирован спор "
                            "между claims"
                        )

                generation_calls = (
                    math.ceil(
                        candidate_count
                        / CLAIM_CONFLICT_BATCH_SIZE
                    )
                    if candidate_count
                    else 0
                )

                disagreement_elapsed = (
                    _time.time()
                    - disagreement_started
                )

                # G: раньше этот блок был частью unaccounted latency,
                # хотя уже имел собственный [Claim↔Claim Timing] print.
                cost["claim_claim_nli_ms"] = (
                    disagreement_elapsed * 1000
                )

                if verbose:
                    log(
                        f"[Claim↔Claim Batch Summary] "
                        f"pairs={candidate_count} "
                        f"classified="
                        f"{llm_classified_count} "
                        f"fallback={fallback_count} "
                        f"contradicts="
                        f"{contradiction_count} "
                        f"generation_calls<="
                        f"{generation_calls}"
                    )

                    log(
                        f"[Claim↔Claim Timing] "
                        f"embedding="
                        f"{embedding_elapsed:.2f}s "
                        f"prefilter="
                        f"{prefilter_elapsed:.3f}s "
                        f"nli="
                        f"{nli_elapsed:.2f}s "
                        f"total="
                        f"{disagreement_elapsed:.2f}s"
                    )

            except Exception as e:
                if verbose:
                    log(
                        f"[V6] Ошибка batch спора: {e}"
                    )

        # ============================================================
        # FINAL CLAIM COVERAGE
        # ============================================================
        #
        # Claim lifecycle до этого момента проверял claims,
        # извлечённые во время synthesis.
        #
        # Но финальный answer может содержать дополнительные factual
        # утверждения, которые вообще не попали в lifecycle.
        #
        # Этот gate отвечает только на вопрос:
        #
        #   "Какую долю factual claims финального ответа
        #    YANDI вообще проверяла?"
        #
        # coverage НЕ означает truth/support и никогда
        # не повышает Trust.
        try:
            _t0_final_coverage = time.time()

            final_coverage = evaluate_final_claim_coverage(
                synthesis_result.answer,
                claims_data,
            )

            cost["final_coverage_ms"] = (
                (time.time() - _t0_final_coverage) * 1000
            )

            final_claim_coverage_score = (
                final_coverage.coverage_score
            )

            final_claims_count = (
                final_coverage.factual_count
            )

            final_claims_covered = (
                final_coverage.covered_count
            )

            final_claims_uncovered = list(
                final_coverage.uncovered_claims
            )

            log(
                "[Final Claim Coverage] "
                f"factual={final_claims_count} "
                f"covered={final_claims_covered} "
                f"uncovered={len(final_claims_uncovered)} "
                f"coverage={final_claim_coverage_score:.2f} "
                f"status={final_coverage.coverage_status}"
            )

            # P0-C (YANDI_FINAL_EPISTEMIC_AUDIT_AND_FIX.md): переиспользует
            # уже вычисленные covered/uncovered — никакой новой extraction
            # machinery. "novel" = factual claims финального ответа, не
            # найденные нигде в claim lifecycle (uncovered); "speculative"
            # = extracted claims (любого типа), которые сам extractor
            # опознал как гипотезу/возможность, а не факт.
            _leakage_speculative = sum(
                1
                for c in final_coverage.final_claims
                if c.get("claim_type") == "speculative"
            )

            log(
                "[Final Claim Leakage] "
                f"extracted={len(final_coverage.final_claims)} "
                f"known={final_claims_covered} "
                f"novel={len(final_claims_uncovered)} "
                f"speculative={_leakage_speculative}"
            )

            if verbose and final_claims_uncovered:
                for uncovered in final_claims_uncovered[:8]:
                    log(
                        "[Final Claim Coverage] UNCOVERED: "
                        f"{uncovered.get('claim_text', '')[:180]}"
                    )

            trace.add_observation(
                "final_claim_coverage_score",
                final_claim_coverage_score,
            )

            trace.add_observation(
                "final_claims_count",
                final_claims_count,
            )

            trace.add_observation(
                "final_claims_covered",
                final_claims_covered,
            )

            trace.add_observation(
                "final_claims_uncovered",
                len(final_claims_uncovered),
            )

        except Exception as e:
            # Ошибка coverage-анализатора НЕ должна поднимать Trust.
            #
            # Но и не превращаем технический сбой автоматически
            # в epistemic failure существующего pipeline.
            final_claim_coverage_score = 1.0

            if verbose:
                log(
                    f"[Final Claim Coverage] error={e}"
                )

        # ── Эпистемическая корректировка trust (v3) ─────────────────────
        label = "UNVERIFIED"
        if not is_subjective_answer and epistemic_trust_label not in ["PARTIALLY_SUPPORTED", "UNVERIFIED"]:
            label = epistemic_trust_label
            trust_reasons.append(f"эпистемическая классификация: {epistemic_result.domain} ({epistemic_result.testability})")

        if not is_subjective_answer:
            cap_label = epistemic_result.max_trust_cap
            if label != cap_label:
                old_label = label
                label = _apply_trust_cap(label, cap_label)
                if old_label != label:
                    trust_reasons.append(f"trust понижен с {old_label} до {label} (cap={cap_label})")

        if not is_subjective_answer and epistemic_result.testability in ["interpretive", "non_falsifiable"]:
            if label in ["VERIFIED", "STRONGLY_SUPPORTED", "EMPIRICALLY_SUPPORTED"]:
                label = "PARTIALLY_SUPPORTED"
                trust_reasons.append("интерпретативный вопрос не может быть STRONGLY_SUPPORTED")
            trust_reasons.append(f"ответ дан в рамках {epistemic_result.testability} перспективы")

        if not is_subjective_answer and epistemic_result.domain in ["axiological", "normative", "philosophical"]:
            if label in ["VERIFIED", "STRONGLY_SUPPORTED", "EMPIRICALLY_SUPPORTED"]:
                label = "VALUE_FRAMEWORK"
                trust_reasons.append("ценностный вопрос не имеет единственного правильного ответа")

        if not is_subjective_answer and epistemic_result.domain == "media_interpretation":
            if not entity:
                trust_reasons.append("фильм не идентифицирован")
                if label in ["STRONGLY_SUPPORTED", "SUPPORTED"]:
                    label = "PARTIALLY_SUPPORTED"

        if not is_subjective_answer and epistemic_result.is_science_as_model:
            if label in ["STRONGLY_SUPPORTED", "VERIFIED"]:
                label = "SUPPORTED"
                trust_reasons.append("научное утверждение — это модель, а не истина")

        # ----------------------------------------------------
        # FINAL CLAIM COVERAGE TRUST GATE
        # ----------------------------------------------------
        #
        # Coverage может только ОГРАНИЧИТЬ Trust сверху.
        # Высокий coverage никогда сам по себе Trust не повышает.
        #
        # < 0.50:
        #   большая часть factual answer вообще не прошла lifecycle.
        #
        # < 0.80:
        #   существенная часть ответа всё ещё вне проверки.
        if final_claim_coverage_score < 0.50:
            trust_reasons.append(
                "низкое покрытие фактических утверждений "
                f"финального ответа "
                f"({final_claim_coverage_score:.2f})"
            )

            if label not in [
                "RELIGIOUS_CLAIM",
                "METAPHYSICAL_UNTESTABLE",
                "VALUE_FRAMEWORK",
                "BOUNDARY_QUESTION",
                "ONTOLOGICAL_INQUIRY",
            ]:
                label = "UNVERIFIED"

        elif final_claim_coverage_score < 0.80:
            trust_reasons.append(
                "частичное покрытие фактических утверждений "
                f"финального ответа "
                f"({final_claim_coverage_score:.2f})"
            )

            if label in [
                "VERIFIED",
                "STRONGLY_SUPPORTED",
                "EMPIRICALLY_SUPPORTED",
                "SUPPORTED",
            ]:
                label = "PARTIALLY_SUPPORTED"

        # ----------------------------------------------------
        # EVIDENCE SUPPORT TRUST GATE
        # ----------------------------------------------------
        #
        # semantic_grounding здесь намеренно НЕ используется:
        # тематическая привязка evidence != поддержка claim.
        #
        # epistemic_grounding также НЕ является положительным
        # сигналом Trust: evidence может противоречить claim.
        #
        # Trust ограничивается только реальным DIRECT +
        # ELIGIBLE support coverage.
        if support_grounding_score < 0.3:
            trust_reasons.append(
                "слабое покрытие claims прямыми "
                "поддерживающими evidence"
            )

            if label not in [
                "RELIGIOUS_CLAIM",
                "METAPHYSICAL_UNTESTABLE",
                "VALUE_FRAMEWORK",
                "BOUNDARY_QUESTION",
                "ONTOLOGICAL_INQUIRY",
            ]:
                label = "UNVERIFIED"

        elif support_grounding_score < 0.6:
            trust_reasons.append(
                "частичное покрытие claims прямыми "
                "поддерживающими evidence"
            )

            # Grounding Gate только ограничивает Trust сверху.
            # Он никогда не повышает label.
            if label in [
                "VERIFIED",
                "STRONGLY_SUPPORTED",
                "EMPIRICALLY_SUPPORTED",
            ]:
                label = "PARTIALLY_SUPPORTED"

        if _belief_manager:
            try:
                beliefs = _belief_manager.get_all_active()
                if beliefs:
                    avg_belief_conf = sum(b.confidence for b in beliefs) / len(beliefs)
                    if avg_belief_conf < 0.5 and label in ["STRONGLY_SUPPORTED", "SUPPORTED"]:
                        label = "PARTIALLY_SUPPORTED"
                        trust_reasons.append(f"средняя уверенность убеждений {avg_belief_conf:.2f}")
            except Exception:
                pass

        trace.trust = label
        trace.trust_reason = "; ".join(trust_reasons[:4])

        if final_claim_coverage_score < 0.80:
            trace.add_learning_rule(
                "coverage",
                (
                    f"final factual claim coverage="
                    f"{final_claim_coverage_score:.2f}"
                ),
                final_claim_coverage_score,
            )

        if support_grounding_score >= 0.6:
            trace.add_learning_rule(
                "trust",
                (
                    f"direct support coverage="
                    f"{support_grounding_score:.2f} → {label}"
                ),
                support_grounding_score,
            )
        if web_used and len(claims_data) >= 2:
            trace.add_learning_rule("retrieval", f"для {intent_result.intent} запросов полезен web-поиск", 0.6)
        if search_result.confidence < CONF_THRESHOLD:
            trace.add_learning_rule("planner", f"registry confidence < {CONF_THRESHOLD} → использовать web", 0.7)
        if epistemic_grounding_score >= 0.6:
            trace.add_learning_rule(
                "evidence",
                (
                    f"direct evidence coverage="
                    f"{epistemic_grounding_score:.2f}"
                ),
                epistemic_grounding_score,
            )

        if not is_subjective_answer:
            if epistemic_result.trust_score >= 0.7:
                trace.add_learning_rule("epistemic", f"high epistemic trust ({epistemic_result.trust_score:.2f}) → {epistemic_result.domain}", epistemic_result.trust_score)
            if epistemic_result.need_clarification and clarification_answered:
                trace.add_learning_rule("epistemic", f"clarification helped for {epistemic_result.domain}", 0.6)
            if epistemic_result.needs_frame_split:
                trace.add_learning_rule("epistemic", f"needs_frame_split=True for {epistemic_result.domain}", 0.7)
            if is_media_query and entity:
                trace.add_learning_rule("media", f"entity resolution succeeded for {entity.get('title', 'unknown')}", 0.8)

        if _belief_manager and claims_data:
            trace.add_learning_rule("belief", f"beliefs updated: {len(claims_data)} new claims", 0.6)
        if supporting_ids:
            trace.add_learning_rule("linker", f"answer linked to {len(supporting_ids)} claims", 0.7)

        if not is_subjective_answer and epistemic_result.is_science_as_model:
            trace.add_learning_rule("epistemic_skepticism", f"science as model for {epistemic_result.domain}", 0.8)

        if coverage_report_data:
            trace._coverage = coverage_report_data

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
        claims_verified = len([
            c for c in claims_data
            if c.get("verification_status") == "verified"
        ])

        claims_supported = len([
            c for c in claims_data
            if c.get("verification_status") == "supported"
        ])

        claims_disputed = len([
            c for c in claims_data
            if c.get("verification_status") == "disputed"
        ])

        claims_contradicted = len([
            c for c in claims_data
            if c.get("verification_status") == "contradicted"
        ])

        claims_candidate = len([
            c for c in claims_data
            if c.get("verification_status") == "candidate"
        ])

        claims_rejected = len([
            c for c in claims_data
            if c.get("verification_status") == "rejected"
        ])

        claims_unverified = len([
            c for c in claims_data
            if c.get("verification_status") in (
                "unverified",
                "weak",
                None,
                "",
            )
        ])

        total_claims = len(claims_data)

        # Совместимость со старым reflection/data pipeline:
        # accepted означает ТОЛЬКО реально verified.
        claims_accepted = claims_verified

        log(
            f"[Claim Status Gate] "
            f"verified={claims_verified}, "
            f"supported={claims_supported}, "
            f"disputed={claims_disputed}, "
            f"contradicted={claims_contradicted}, "
            f"candidate={claims_candidate}, "
            f"unverified={claims_unverified}, "
            f"rejected={claims_rejected}, "
            f"total={total_claims}"
        )

        if total_claims == 0:
            log("[Claim Status Gate] Claims отсутствуют — статус UNVERIFIED")

            synthesis_result.answer = (
                "Я попыталась найти информацию.\n\n"
                "Но мне не удалось выделить достаточно проверяемых утверждений.\n"
                "Я не могу дать уверенный ответ на этот вопрос.\n\n"
                "Если дашь дополнительный контекст — я попробую ещё раз."
            )

            synthesis_result.trust_level = "UNVERIFIED"
            synthesis_result.confidence = 0.0

        elif claims_rejected == total_claims:
            log("[Claim Status Gate] Все claims структурно отклонены")

            synthesis_result.answer = (
                "Я попыталась сформировать ответ, но выделенные утверждения "
                "не прошли структурную проверку.\n\n"
                "Поэтому я не могу считать этот ответ надёжным."
            )

            synthesis_result.trust_level = "UNVERIFIED"
            synthesis_result.confidence = 0.0

        elif claims_contradicted > 0 and (
            claims_contradicted
            + claims_rejected
            + claims_unverified
            + claims_candidate
            == total_claims
        ):
            # Нет ни одного supported/verified claim,
            # зато есть явно contradicted.
            log(
                "[Claim Status Gate] "
                "Поддержанных claims нет, присутствуют опровергающие evidence"
            )

            synthesis_result.trust_level = "UNVERIFIED"
            synthesis_result.confidence = min(
                synthesis_result.confidence,
                0.25,
            )

            # P0-A (YANDI_FINAL_EPISTEMIC_AUDIT_AND_FIX.md): раньше этот
            # branch менял ТОЛЬКО trust_level/confidence — сам текст
            # synthesis_result.answer уже был сгенерирован compose_prompt
            # ДО того, как Claim Status вообще стал известен, и никогда
            # не пересматривался. Текст мог свободно утверждать то, что
            # найденные evidence прямо опровергают. Здесь — не переписываем
            # и не удаляем сгенерированный текст (в нём может быть полезный
            # объясняющий контекст), а явно маркируем его эпистемический
            # статус прямо в теле ответа, а не только в trust-бейдже.
            _contradiction_notice = (
                "⚠️ ВАЖНО: часть проверяемых утверждений в этом ответе "
                f"была ОПРОВЕРГНУТА найденными источниками "
                f"(contradicted={claims_contradicted} из {total_claims}), "
                "и ни одно утверждение не получило прямого подтверждения. "
                "Текст ниже остаётся гипотезой модели — не считай его "
                "установленным фактом.\n"
            )

            if not synthesis_result.answer.startswith("⚠️ ВАЖНО:"):
                synthesis_result.answer = (
                    _contradiction_notice
                    + "\n"
                    + synthesis_result.answer
                )

        elif claims_disputed > 0:
            # Спор не означает ложность ответа, но требует сильного cap.
            log(
                f"[Claim Status Gate] "
                f"Обнаружены спорные claims: {claims_disputed}"
            )

            trust_rank = {
                "UNVERIFIED": 0,
                "WEAKLY_SUPPORTED": 1,
                "PARTIALLY_SUPPORTED": 2,
                "SUPPORTED": 3,
                "STRONGLY_SUPPORTED": 4,
                "VERIFIED": 5,
            }

            current = synthesis_result.trust_level

            if trust_rank.get(current, 0) > trust_rank["WEAKLY_SUPPORTED"]:
                synthesis_result.trust_level = "WEAKLY_SUPPORTED"

            synthesis_result.confidence = min(
                synthesis_result.confidence,
                0.45,
            )

        elif claims_verified == 0:
            # supported != verified.
            #
            # Даже если часть claims имеет supports evidence,
            # без отдельной сильной проверки Trust не должен
            # подниматься выше PARTIALLY_SUPPORTED.
            log(
                f"[Claim Status Gate] "
                f"verified=0, supported={claims_supported} — "
                f"ответ остаётся предварительным"
            )

            trust_rank = {
                "UNVERIFIED": 0,
                "WEAKLY_SUPPORTED": 1,
                "PARTIALLY_SUPPORTED": 2,
                "SUPPORTED": 3,
                "STRONGLY_SUPPORTED": 4,
                "VERIFIED": 5,
            }

            current = synthesis_result.trust_level

            if trust_rank.get(current, 0) > trust_rank["PARTIALLY_SUPPORTED"]:
                synthesis_result.trust_level = "PARTIALLY_SUPPORTED"

            # Если вообще нет supported claims — cap ещё ниже.
            if claims_supported == 0:
                if trust_rank.get(
                    synthesis_result.trust_level,
                    0,
                ) > trust_rank["WEAKLY_SUPPORTED"]:
                    synthesis_result.trust_level = "WEAKLY_SUPPORTED"

                synthesis_result.confidence = min(
                    synthesis_result.confidence,
                    0.40,
                )

                # P0-A: точный сценарий из аудита — supported=0,
                # verified=0, unverified>0. Раньше синтезированный текст
                # (сгенерированный ДО того, как Claim Status стал
                # известен) оставался без изменений — только trust
                # badge менялся. Явно маркируем это в самом тексте.
                _unsupported_notice = (
                    "⚠️ ВАЖНО: ни одно из "
                    f"{total_claims} проверяемых утверждений не получило "
                    "подтверждающих доказательств (supported=0, "
                    "verified=0). Всё, что изложено ниже — "
                    "неподтверждённая гипотеза модели, а не установленный "
                    "факт. Система не получила достаточной evidence-базы "
                    "для проверки.\n"
                )

                if not synthesis_result.answer.startswith("⚠️ ВАЖНО:"):
                    synthesis_result.answer = (
                        _unsupported_notice
                        + "\n"
                        + synthesis_result.answer
                    )
            else:
                synthesis_result.confidence = min(
                    synthesis_result.confidence,
                    0.60,
                )

        else:
            log(
                f"[Claim Status Gate] "
                f"Есть verified claims: "
                f"{claims_verified}/{total_claims}"
            )

        # ============================================================
        # EXISTENCE QUERY CONTRACT (P0-A, autonomous fix pass)
        # ============================================================
        #
        # Аудит (YANDI-autonomous P0-A): для existence-вопроса
        # ("Есть ли разумная жизнь на Юпитере?") pipeline может дойти
        # до конца с 8/8 claims verified/supported — и ни один из них
        # не был прямым CORE-ответом на сам вопрос существования (все
        # про фоновые условия обитаемости). Trust при этом молча
        # выставлялся как если бы вопрос был проверен. Это отдельный
        # эпистемический провал от "низкое доверие/support" — здесь
        # ВСЁ, что проверено, может быть безупречно supported, но
        # ничего из этого не отвечает на заданный вопрос.
        #
        # Single-source-of-truth: та же _is_existence_question() и то
        # же поле supports_query_aspect (роль, вычисленная один раз в
        # orch_synthesizer.py через _classify_claim_role — не второй
        # независимый классификатор).
        #
        # Deterministic detection + trust degradation в этом проходе.
        # Bounded retry (перегенерировать local_answer/extraction ещё
        # раз) сознательно НЕ реализован здесь: у нас нет дешёвого
        # способа доказать, что повторная попытка не потеряет CORE
        # claim по той же причине, а не-bounded retry запрещён явно.
        # Задокументировано как рекомендованный следующий шаг, не
        # решено молча.
        existence_q = _is_existence_question(query_to_use)

        if existence_q:
            core_claim_count = sum(
                1
                for c in claims_data
                if (c.get("supports_query_aspect") or [None])[0] == "CORE"
            )

            contract_status = "FAILED" if (total_claims > 0 and core_claim_count == 0) else "OK"

            log(
                f"[Existence Contract] "
                f"core_claims={core_claim_count} "
                f"total_claims={total_claims} "
                f"status={contract_status}"
            )

            if contract_status == "FAILED":
                trust_rank = {
                    "UNVERIFIED": 0,
                    "WEAKLY_SUPPORTED": 1,
                    "PARTIALLY_SUPPORTED": 2,
                    "SUPPORTED": 3,
                    "STRONGLY_SUPPORTED": 4,
                    "VERIFIED": 5,
                }

                current = synthesis_result.trust_level

                if trust_rank.get(current, 0) > trust_rank["WEAKLY_SUPPORTED"]:
                    synthesis_result.trust_level = "WEAKLY_SUPPORTED"

                synthesis_result.confidence = min(
                    synthesis_result.confidence,
                    0.35,
                )

                _existence_contract_notice = (
                    "⚠️ ВАЖНО: вопрос был сформулирован как вопрос "
                    "существования, но ни одно из проверенных "
                    f"утверждений ({total_claims}) не является прямым "
                    "ответом на сам вопрос существования — все они "
                    "описывают фоновые условия. Прямой ответ на "
                    "заданный вопрос НЕ был проверен по источникам, "
                    "даже если текст ниже звучит уверенно.\n"
                )

                if not synthesis_result.answer.startswith("⚠️ ВАЖНО:"):
                    synthesis_result.answer = (
                        _existence_contract_notice
                        + "\n"
                        + synthesis_result.answer
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

    if verbose:
        profile_items = []

        # Уже существующие timers + новые крупные wall-clock timers.
        profile_keys = [
            # G: personality/character/scene/target/entity/strategy/
            # criticism/boundary pre-processing — раньше НЕ измерялся
            # вообще (0 timing coverage, до "[0] Cache check").
            ("pre_pipeline_personality", "pre_pipeline_ms"),
            ("cache", "cache_ms"),
            ("risk", "risk_ms"),
            ("plan", "plan_ms"),
            ("intent", "intent_ms"),
            ("clarify", "clarify_ms"),
            ("enrich", "enrich_ms"),
            ("registry/web-initial", "registry_ms"),
            ("web", "web_ms"),
            ("refutation", "profile_refutation_ms"),
            ("hypothesis_graph", "profile_hypothesis_graph_ms"),
            ("local_wait", "profile_local_wait_ms"),
            ("blind_analysis", "profile_blind_analysis_ms"),
            ("source_classification", "profile_source_classification_ms"),
            ("synthesize", "synthesize_ms"),
            # P1.2: ранее отсутствовавшая, но реально доминирующая фаза
            # (см. YANDI_FULL_PIPELINE_AUDIT.md §26).
            ("claim_specific_retrieval", "claim_retrieval_ms"),
            # G (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md §G): раньше эти
            # фазы формировали unaccounted=275.87s (43.8% total).
            ("claim_setup_validator_mapper1_nli1", "claim_setup_ms"),
            ("claim_pass2_mapper_nli", "claim_pass2_mapping_nli_ms"),
            ("claim_claim_nli", "claim_claim_nli_ms"),
            ("final_claim_coverage", "final_coverage_ms"),
        ]

        for label, key in profile_keys:
            value = cost.get(key)
            if isinstance(value, (int, float)):
                profile_items.append((label, float(value)))

        profile_items.sort(
            key=lambda item: item[1],
            reverse=True,
        )

        log("")
        log("=" * 72)
        log("YANDI PIPELINE WALL-CLOCK PROFILE")
        log("=" * 72)

        for label, ms in profile_items:
            pct = (
                (ms / cost["total_ms"]) * 100.0
                if cost.get("total_ms")
                else 0.0
            )

            log(
                f"[PROFILE] "
                f"{label:<24} "
                f"{ms / 1000:>8.2f}s "
                f"{pct:>6.1f}%"
            )

        if profile_items:
            bottleneck_label, bottleneck_ms = profile_items[0]

            log(
                f"[PROFILE BOTTLENECK] "
                f"{bottleneck_label} "
                f"{bottleneck_ms / 1000:.2f}s"
            )

        measured_ms = sum(
            ms
            for _, ms in profile_items
        )

        unaccounted_ms = max(
            0.0,
            cost["total_ms"] - measured_ms,
        )

        log(
            f"[PROFILE] "
            f"measured_sum={measured_ms / 1000:.2f}s "
            f"unaccounted={unaccounted_ms / 1000:.2f}s "
            f"total={total:.2f}s"
        )

        log("=" * 72)

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
