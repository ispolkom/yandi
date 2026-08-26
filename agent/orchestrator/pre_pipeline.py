"""
Pre-pipeline — extracted from agent/orchestrator_v2.py's process(): URL
extraction, variable initialization, character/scene/target/intent
routing, self-query short-circuit, entity resolution, strategy routing,
swear/criticism detection, personal-boundary handling (provocation/
insult/apology/personal-question short-circuits), intent-dependent
analyzer branches (song/social/self-reflection short-circuits), criticism
context-check short-circuit, thanks/normal processing, and the Early Gate
(break/know_but_not_tell short-circuit).

Structural extraction only: no branch ordering, thresholds, keyword
lists, or log markers changed. 11 short-circuit early-return points,
in the same strict sequential order as before — this ordering is
behaviorally load-bearing (e.g. the swear-check gates whether the
Criticism Detector even runs) and is preserved exactly.

`is_self_query` and `load_yandi_manifest` move here too — each had
exactly one call site (both inside this block), so ownership moves with
them rather than leaving orphaned single-use helpers behind in
orchestrator_v2.py.

Return protocol (deliberately a plain tuple + dict, not a new
PipelineContext class — see the migration brief: don't introduce a
context object until several more modules are proven out, and this
block's own boundary work doesn't require one):

    early_response, state = run_pre_pipeline(...)

`early_response` is an OrchestratorResponse if any of the 11 short-circuit
branches fired; the caller must `return early_response` immediately and
unconditionally when it is not None (`state` is None in that case).
Otherwise `early_response` is None and `state` is a dict of every value
the "standard pipeline" (orchestrator_v2.py, from `[0] Cache check`
onward) reads downstream — verified via a full cross-reference audit of
the rest of the file, not guessed. The caller assigns each entry to its
own same-named local, e.g. `query_to_use = state["query_to_use"]`.
"""

import json
import re
import time
from pathlib import Path
from typing import List

from agent.orch_schemas import EnrichedQuery, OrchestratorResponse, SearchResult
from agent.biography_stats import get_biography
from agent.boundaries import detect_toxicity, ToxicityLevel
from agent.character_engine import get_character
from agent.criticism_detector import get_criticism_detector
from agent.context_registry import get_context_registry
from agent.decision_journal import get_decision_journal
from agent.entity_resolver import get_entity_resolver
from agent.intent_router import detect_intent, get_intent_explanation
from agent.personal_boundary import get_personal_boundary
from agent.relationship_gate import decide_response, apply_gate
from agent.scene_builder import get_scene_builder
from agent.secret_archive import get_secret_archive
from agent.self_reflection_analyzer import get_self_reflection_analyzer
from agent.social_analyzer import get_social_analyzer
from agent.song_analyzer import get_song_analyzer
from agent.strategy_router import get_strategy_router
from agent.target_router import detect_target, get_target_description
from agent.orchestrator.response.assembly import (
    build_self_answer,
    _generate_character_response,
    _generate_apology_response,
)

BASE = Path(__file__).parent.parent.parent


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


def extract_urls(text: str) -> List[str]:
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    urls = re.findall(url_pattern, text, re.IGNORECASE)
    return urls

def clean_query_from_urls(text: str) -> str:
    url_pattern = r'https?://[^\s]+'
    return re.sub(url_pattern, '', text).strip()


def run_pre_pipeline(request, verbose, t_start, query, log, trace, cost, tracer):
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

            tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["self_query"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            ), None
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
        tracer.save_trace(trace)
        return OrchestratorResponse(
            answer=answer,
            trust_level="PARTIALLY_SUPPORTED",
            preliminary=False,
            steps_taken=["provocation"],
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        ), None

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
            tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["blocked"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            ), None

        answer = _generate_character_response(char, state, analysis)
        trace.add_execution("insult_handled", True, 0.0, {"level": level})
        tracer.save_trace(trace)
        return OrchestratorResponse(
            answer=answer,
            trust_level="PARTIALLY_SUPPORTED",
            preliminary=False,
            steps_taken=["insult_handled"],
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        ), None

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
        tracer.save_trace(trace)
        return OrchestratorResponse(
            answer=answer,
            trust_level="PARTIALLY_SUPPORTED",
            preliminary=False,
            steps_taken=["apology"],
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        ), None

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
        tracer.save_trace(trace)
        return OrchestratorResponse(
            answer=answer,
            trust_level="PARTIALLY_SUPPORTED",
            preliminary=False,
            steps_taken=["personal_question"],
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        ), None

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
            tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["song_analysis"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            ), None
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
            tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["social_analysis"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            ), None
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
            tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["self_reflection"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            ), None
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
            tracer.save_trace(trace)
            return OrchestratorResponse(
                answer=answer,
                trust_level="PARTIALLY_SUPPORTED",
                preliminary=False,
                steps_taken=["no_context"],
                latency_total=round(time.time() - t_start, 2),
                session_id=request.session_id,
            ), None

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

        tracer.save_trace(trace)

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
        ), None

    log("[Early Gate] Поиск разрешён")

    cost["pre_pipeline_ms"] = (
        (time.time() - _t0_pre_pipeline) * 1000
    )

    state_out = {
        "query_to_use": query_to_use,
        "state": state,
        "char": char,
        "context_registry": context_registry,
        "intent_type": intent_type,
        "intent_confidence": intent_confidence,
        "entity_info": entity_info,
        "strategy_router": strategy_router,
        "strategy_result": strategy_result,
        "_skip_rag": _skip_rag,
        "_bad_state_prefix": _bad_state_prefix,
        "enrich_result": enrich_result,
        "search_result": search_result,
        "web_result": web_result,
        "web_used": web_used,
        "synthesis_result": synthesis_result,
        "reasoning_info": reasoning_info,
        "claims_data": claims_data,
        "evidence_data": evidence_data,
        "semantic_grounding_score": semantic_grounding_score,
        "epistemic_grounding_score": epistemic_grounding_score,
        "support_grounding_score": support_grounding_score,
        "final_claim_coverage_score": final_claim_coverage_score,
        "final_claims_count": final_claims_count,
        "final_claims_covered": final_claims_covered,
        "final_claims_uncovered": final_claims_uncovered,
        "entity": entity,
        "is_subjective_answer": is_subjective_answer,
        "_exact_mode": _exact_mode,
    }

    return None, state_out
