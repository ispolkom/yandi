"""
assistant/orchestrator_v2.py — Orchestrator v2.
Фазы А + Б + В: cache → risk → plan → intent → clarify → enrich → local_search
                → (web_query → web_scrape) → synthesize → optimistic_respond
                → [фон] validate → arbitrate → knowledge_write

CLI:
  python3 assistant/orchestrator_v2.py "Как работает DHT?"
  python3 assistant/orchestrator_v2.py --interactive [--web] [--validate]
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from agent.orch_schemas            import OrchestratorRequest, OrchestratorResponse, IntentResult, EnrichedQuery, SearchResult, SynthesisResult
from agent.orch_cache              import get_cache
from agent.orch_risk               import assess_risk
from agent.orch_planner            import build_plan
from agent.orch_intent             import analyze_intent
from agent.orch_clarifier          import ClarificationSession
from agent.orch_enricher           import enrich_query
from agent.orch_registry_search    import search_registry, CONF_THRESHOLD
from agent.orch_web_query          import formulate_queries
from agent.orch_web_scraper        import scrape
from agent.orch_synthesizer        import synthesize
from agent.orch_optimistic         import quick_respond, get_responder
from agent.orch_timeout            import step_timer
from agent.orch_tool_registry      import get_registry
from agent.orch_session            import get_context, add_message, new_session_id
from agent.orch_node_selector      import select_nodes, select_nodes_federated, _should_use_federation
from agent.orch_validator          import validate_parallel
from agent.orch_arbiter            import arbitrate
from agent.orch_knowledge_writer   import write_from_arbiter
from agent.orch_monitoring         import record as mon_record
from agent.orch_tracer             import OrchestratorTracer
from agent.orch_query_archive      import record_query as archive_query
from agent.orch_tag_tree           import update_tree as tag_tree_update
from agent.orch_unanswered         import record_unanswered, start_listener_daemon as _start_unanswered_listener

_tracer = OrchestratorTracer()
_start_unanswered_listener()   # daemon: слушает Redis события о слабых ответах

_STEP_TRACE_DIR = BASE / "registry" / "dataset" / "orch_sft" / "step_traces"
_STEP_TRACE_DIR.mkdir(parents=True, exist_ok=True)


def _write_step_trace(
    query: str,
    tags: list[str],
    risk_result,
    intent,
    enrich,
    search,
    web_used: bool,
    synthesis,
    steps: list[str],
    total_ms: int,
    plan=None,
    wq_result=None,
):
    """Записывает пошаговый трейс в SFT-формат совместимый с orch_synth_dataset."""
    import json, time as _time
    from datetime import datetime

    risk = getattr(risk_result, "risk_level", "low")

    sources_used = []
    if search and search.docs:
        sources_used.append("local_db")
    if web_used:
        sources_used.append("web")

    step_details = []
    for s in steps:
        detail = {"step": s, "result": ""}
        if s == "cache_check":
            detail["result"] = "miss"
        elif s == "risk_assess":
            nodes = getattr(risk_result, "nodes_required", 1)
            detail["result"] = f"risk={risk}, nodes={nodes}"
        elif s == "plan":
            if plan:
                n = len(getattr(plan, "steps", []))
                skip = getattr(plan, "skip_internet", False)
                detail["result"] = f"steps={n}, skip_internet={skip}"
        elif s == "intent":
            detail["result"] = f"domain={getattr(intent,'intent','?')}, risk={risk}"
        elif s == "enrich":
            enriched = getattr(enrich, "enriched", "")[:60]
            etags = getattr(enrich, "tags", [])
            detail["result"] = f"enriched='{enriched}', tags={etags}"
        elif s == "local_search":
            conf = getattr(search, "confidence", 0)
            n    = len(getattr(search, "docs", []))
            detail["result"] = f"confidence={conf:.2f}, found={n} docs"
        elif s == "web_query":
            if wq_result:
                qs = getattr(wq_result, "queries", [])
                detail["result"] = f"queries={qs[:2]}"
        elif s == "web_scrape":
            if web_used and wq_result:
                detail["result"] = "scraped pages from web"
            else:
                detail["result"] = "skipped"
        elif s == "synthesize":
            trust = getattr(synthesis, "trust_level", "?")
            conf  = getattr(synthesis, "confidence", 0)
            detail["result"] = f"trust={trust}, confidence={conf:.2f}"
        elif s == "optimistic_respond":
            detail["result"] = "preliminary answer sent to user"
        elif s == "validate":
            detail["result"] = "background validation started"
        step_details.append(detail)

    label = "verified" if getattr(synthesis,"confidence",0) >= 0.85 else \
            "partial"  if getattr(synthesis,"confidence",0) >= 0.5  else "unverified"

    record = {
        "query":          query,
        "ts":             _time.time(),
        "date":           datetime.now().strftime("%Y-%m-%d"),
        "generation":     "real",
        "model":          "heretic:q8",
        "classification": {
            "tags":   tags,
            "domain": tags[0].split(":")[0] if tags else "general",
            "risk":   risk,
        },
        "decision": {
            "use_local_db": bool(search and search.docs),
            "use_web":      web_used,
            "use_dht":      False,
            "use_ai_chats": False,
            "reason":       f"local_conf={getattr(search,'confidence',0):.2f}",
        },
        "steps":               step_details,
        "final_answer":        getattr(synthesis, "answer", ""),
        "verification_label":  label,
        "source_used":         sources_used,
        "total_ms":            total_ms,
    }

    day_file = _STEP_TRACE_DIR / f"{datetime.now().strftime('%Y%m%d')}.jsonl"
    with day_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

# ── Домен → базовый тег (иерархический, английский) ──────────────────────────
_DOMAIN_TAG: dict[str, str] = {
    "general":     "general",
    "legal":       "legal",
    "medical":     "health:medical",
    "financial":   "finance",
    "coding":      "tech:coding",
    "science":     "science",
    "tech":        "tech",
    "ai_ml":       "tech:ai",
    "cooking":     "lifestyle:cooking",
    "travel":      "travel",
    "sport":       "lifestyle:sport",
    "music":       "culture:music",
    "history":     "culture:history",
    "education":   "education",
    "ecology":     "science:ecology",
    "psychology":  "health:psychology",
    "geography":   "science:geography",
    "literature":  "culture:literature",
    "search":      "general:search",
    "question":    "general",
}


_KEYWORD_TAGS: list[tuple[list[str], str]] = [
    (["рецепт","блюдо","жарить","варить","испечь","готовить","суп","торт","паста","борщ"], "lifestyle:cooking"),
    (["путешест","турист","курорт","отель","виза","страна","город","отдых","тур","перелёт"], "travel:tourism"),
    (["гора","океан","море","река","озеро","география","континент","страна","столица","климат"], "science:geography"),
    (["закон","право","суд","договор","юрист","адвокат","преступлен","уголовн","гражданск"], "legal"),
    (["симптом","болезнь","лечение","врач","лекарство","диагноз","медицин","здоровь","давлени"], "health:medical"),
    (["акция","инвестиц","банк","кредит","налог","финанс","биржа","вклад","бюджет","ипотек"], "finance"),
    (["код","программ","python","javascript","алгоритм","функция","база данных","git","api","linux"], "tech:coding"),
    (["нейросет","искусственный интеллект","модель","gpt","llm","трансформер","обучение","датасет"], "tech:ai"),
    (["физик","химия","биолог","квантов","атом","эволюц","наука","теория","формула"], "science"),
    (["история","война","революц","империя","средневеков","цивилизац","античн","эпоха"], "culture:history"),
    (["роман","книга","автор","литератур","писатель","поэт","стих","рассказ"], "culture:literature"),
    (["музык","песня","гитара","джаз","рок","альбом","инструмент","нота","блюз"], "culture:music"),
    (["спорт","тренировк","бег","плавание","футбол","мышц","упражнени","кардио"], "lifestyle:sport"),
    (["психолог","стресс","депресси","тревога","эмоц","поведени","мотивац","характер"], "health:psychology"),
    (["экологи","климат","загрязнен","углерод","переработк","природа","выброс"], "science:ecology"),
    (["ремонт","строительство","утеплитель","стена","краска","обои","кровля","пол"], "tech:construction"),
    (["собака","кошка","животное","ветерин","прививка","корм","порода","питомец"], "lifestyle:pets"),
    (["образование","школа","экзамен","егэ","университет","обучение","диплом","урок"], "education"),
    (["автомобиль","машина","двигатель","vin","кузов","страховка","тормоза","авто"], "tech:auto"),
    (["nas","сервер","vpn","роутер","сеть","wi-fi","безопасность","шифровани","антивирус"], "tech:network"),
]


def _build_tags(intent_result, enrich_result, query: str = "") -> list[str]:
    """Строит иерархические теги из intent + entities + keyword-fallback."""
    domain = getattr(intent_result, "intent", "general") or "general"
    base   = _DOMAIN_TAG.get(domain, domain)

    # Keyword-fallback: если intent=general — ищем по ключевым словам запроса
    if base == "general" and query:
        q_lower = query.lower()
        for keywords, tag in _KEYWORD_TAGS:
            if any(kw in q_lower for kw in keywords):
                base = tag
                break

    tags = [base]

    # Уточняющие теги из entities
    entities = getattr(intent_result, "entities", {}) or {}
    params   = getattr(enrich_result, "params",   {}) or {}
    for v in {**entities, **params}.values():
        if not v:
            continue
        v_str = str(v).lower().strip()
        if len(v_str) > 2 and v_str not in tags:
            tags.append(f"{base}:{v_str.replace(' ', '_')[:20]}")

    return tags[:5]


def _background_validate(
    question: str,
    answer: str,
    synthesis: SynthesisResult,
    risk,
    intent_result,
    validation_id: str,
    verbose: bool,
):
    """Фоновая валидация — запускается в отдельном треде после optimistic_respond."""
    def log(msg):
        if verbose:
            print(msg, flush=True)

    log(f"\n[BG:{validation_id[:6]}] Старт валидации...")
    domain = intent_result.intent if intent_result else "general"

    try:
        # Выбор нод (локальные или через Federation)
        nodes = select_nodes_federated(risk, domain=domain) if _should_use_federation() else select_nodes(risk, domain=domain)
        log(f"[BG] Ноды: {[n.node_id for n in nodes.nodes]}")

        # Параллельная валидация
        val_result = validate_parallel(question, answer, nodes, domain=domain)
        log(f"[BG] agree={val_result.agree_count} disagree={val_result.disagree_count}")

        # Арбитраж
        use_llm = risk.risk_level in ("medium", "high", "critical")
        arb     = arbitrate(question, answer, val_result, use_llm=use_llm)
        log(f"[BG] Вердикт: {arb.verdict} — {arb.explanation}")

        # Запись в реестр знаний
        if arb.verdict in ("VERIFIED", "PARTIALLY_VERIFIED"):
            write_from_arbiter(question, synthesis, arb, topic=domain)
            log(f"[BG] Записано в knowledge registry ({arb.verdict})")

        # Уведомить optimistic responder об обновлении
        get_responder().on_validation_done(validation_id, arb.verdict, arb.explanation)

        # Метрики
        for v in val_result.validations:
            mon_record("validate", v.latency, v.verdict != "disagree")

    except Exception as e:
        log(f"[BG] Ошибка валидации: {e}")


def process(
    request: OrchestratorRequest,
    verbose: bool = False,
    enable_web: bool = False,
    enable_validation: bool = False,
    clarify_callback=None,
) -> OrchestratorResponse:
    """
    Обработать запрос пользователя через полную цепочку.

    Args:
        request:          OrchestratorRequest
        verbose:          печатать прогресс шагов
        enable_web:       разрешить веб-поиск если локальный confidence < threshold
        clarify_callback: функция для получения уточнений от пользователя
                          fn(formatted_questions: str) -> dict[param, answer]
                          None = пропустить уточнения

    Returns:
        OrchestratorResponse
    """
    t_start  = time.time()
    query    = request.query
    steps    = []
    registry = get_registry()

    # Загрузить контекст сессии
    ctx = request.context or get_context(request.session_id)

    def log(msg: str):
        if verbose:
            print(msg, flush=True)

    log(f"\n{'='*60}")
    log(f"Orchestrator v2 | {query[:80]}")
    log(f"{'='*60}")

    # ── [0] Cache check ───────────────────────────────────────────
    log("[0] Cache check...")
    cache = get_cache()
    cache_result, dt, _ = step_timer("cache_check", lambda: cache.get(query))
    registry.update_latency("cache_check", dt)
    steps.append("cache_check")

    if cache_result and cache_result.hit:
        log(f"  ✓ Cache HIT (similarity={cache_result.similarity:.2f})")
        return OrchestratorResponse(
            answer=cache_result.answer,
            trust_level=cache_result.trust_level or "HYPOTHESIS",
            preliminary=False,
            steps_taken=steps,
            latency_total=round(time.time() - t_start, 2),
            session_id=request.session_id,
        )
    log("  · Cache miss")

    # ── [1] Risk assess ───────────────────────────────────────────
    log("[1] Risk assess...")
    risk_result, dt, _ = step_timer("risk_assess", lambda: assess_risk(query))
    registry.update_latency("risk_assess", dt)
    steps.append("risk_assess")
    log(f"  · risk={risk_result.risk_level}, nodes={risk_result.nodes_required}")

    # ── [2] Plan ─────────────────────────────────────────────────
    log("[2] Planning...")
    plan, dt, _ = step_timer("plan", lambda: build_plan(query, risk_result, use_llm=False))
    registry.update_latency("plan", dt)
    steps.append("plan")
    log(f"  · steps={len(plan.steps)}, internet={not plan.skip_internet}")

    # ── [3] Intent analyze ────────────────────────────────────────
    log("[3] Intent analyze...")
    intent_result, dt, timed_out = step_timer(
        "intent",
        lambda: analyze_intent(query, ctx),
    )
    registry.update_latency("intent", dt)
    registry.update_reliability("intent", not timed_out and intent_result is not None)
    steps.append("intent")

    if timed_out or intent_result is None:
        log("  ⚠ Timeout — дефолт")
        intent_result = IntentResult(
            intent="general", entities={}, missing=[],
            need_clarification=False, confidence=0.5,
        )
    else:
        log(f"  · intent={intent_result.intent}, conf={intent_result.confidence:.2f}, "
            f"clarify={intent_result.need_clarification}")

    # ── [4] Clarification (опционально) ──────────────────────────
    if intent_result.need_clarification and clarify_callback:
        log("[4] Clarification...")
        steps.append("clarify")
        cl_session = ClarificationSession(query, intent_result)
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
                break
    else:
        log("[4] Clarification — пропуск (не нужно или нет callback)")

    # ── [5] Query enrich ──────────────────────────────────────────
    log("[5] Query enrich...")
    enrich_result, dt, timed_out = step_timer(
        "enrich",
        lambda: enrich_query(query, intent_result),
    )
    registry.update_latency("enrich", dt)
    steps.append("enrich")

    if timed_out or enrich_result is None:
        log("  ⚠ Timeout — оригинальный запрос")
        enrich_result = EnrichedQuery(original=query, enriched=query, params={})
    else:
        log(f"  · enriched: {enrich_result.enriched[:80]}")

    # ── [6] Local registry search ─────────────────────────────────
    log("[6] Local registry search...")
    search_result, dt, timed_out = step_timer(
        "local_search",
        lambda: search_registry(enrich_result.enriched),
    )
    registry.update_latency("local_search", dt)
    steps.append("local_search")

    if timed_out or search_result is None:
        log("  ⚠ Timeout")
        search_result = SearchResult(docs=[], confidence=0.0, source="local")
    else:
        log(f"  · confidence={search_result.confidence:.3f}, docs={len(search_result.docs)}")

    # ── [7] Web search (если confidence низкий и разрешено) ───────
    web_result = None
    if enable_web and not plan.skip_internet and search_result.confidence < CONF_THRESHOLD:
        log("[7] Web search (confidence низкий)...")

        # Если QueryFrame передал готовые search_queries — используем их, иначе формулируем сами
        if request.search_queries:
            from agent.orch_schemas import WebQueryResult
            wq_result = WebQueryResult(queries=request.search_queries[:3], raw="[from QueryFrame]")
            dt = 0.0
            timed_out = False
            log(f"  · queries (from QueryFrame): {wq_result.queries}")
        else:
            wq_result, dt, timed_out = step_timer(
                "web_query",
                lambda: formulate_queries(enrich_result),
            )
            registry.update_latency("web_query", dt)
        steps.append("web_query")

        if not timed_out and wq_result:
            log(f"  · queries: {wq_result.queries}")
            web_result, dt, timed_out = step_timer(
                "web_scrape",
                lambda: scrape(wq_result),
            )
            registry.update_latency("web_scrape", dt)
            steps.append("web_scrape")
            if web_result:
                log(f"  · сниппетов: {len(web_result.snippets)}, символов: {web_result.total_chars}")
            else:
                log("  ⚠ Web scrape timeout")
    else:
        reason = "отключён" if not enable_web else ("confidence ok" if search_result.confidence >= CONF_THRESHOLD else "plan skip")
        log(f"[7] Web search — пропуск ({reason})")

    # ── [8] Answer synthesize ─────────────────────────────────────
    log("[8] Answer synthesize...")
    synthesis_result, dt, timed_out = step_timer(
        "synthesize",
        lambda: synthesize(
            enrich_result,
            search_result=search_result,
            web_result=web_result,
            query_frame=request.query_frame or {},
        ),
    )
    registry.update_latency("synthesize", dt)
    registry.update_reliability("synthesize", not timed_out and synthesis_result is not None)
    steps.append("synthesize")

    if timed_out or synthesis_result is None:
        log("  ⚠ Timeout")
        from agent.orch_schemas import SynthesisResult
        synthesis_result = SynthesisResult(
            answer="Не удалось сформировать ответ (timeout).",
            confidence=0.0, sources=[], trust_level="UNVERIFIED",
        )
    else:
        log(f"  · trust={synthesis_result.trust_level}, conf={synthesis_result.confidence:.2f}, "
            f"len={len(synthesis_result.answer)}")

    # ── [9] Optimistic respond ────────────────────────────────────
    log("[9] Optimistic respond...")

    _val_thread: list[threading.Thread] = []

    def _start_bg_validation(val_id: str):
        if not enable_validation:
            return
        t = threading.Thread(
            target=_background_validate,
            args=(query, synthesis_result.answer, synthesis_result,
                  risk_result, intent_result, val_id, verbose),
            daemon=False,  # не daemon — процесс дождётся завершения
        )
        t.start()
        _val_thread.append(t)

    responder = get_responder()
    optimistic = responder.respond(synthesis_result, start_validation=_start_bg_validation)
    steps.append("optimistic_respond")
    if enable_validation:
        steps.append("validate")
        log(f"  · Фоновая валидация запущена (ID: {optimistic.validation_id[:8]})")

    # Сохранить в кэш и сессию
    if synthesis_result.confidence > 0.3:
        cache.put(query, synthesis_result.answer, synthesis_result.trust_level)
    if request.session_id:
        add_message(request.session_id, "user", query)
        add_message(request.session_id, "assistant", synthesis_result.answer)

    # Метрика всего запроса
    total = round(time.time() - t_start, 2)
    mon_record("full_request", total, success=True)
    log(f"\n✓ Готово за {total}s | {len(steps)} шагов")

    # Иерархические теги — из enricher (LLM-классификация) или fallback
    tags = enrich_result.tags or _build_tags(intent_result, enrich_result, query=query)
    primary_tag = tags[0] if tags else "general"

    # Query Archive — сохранить анонимизированный запрос по тегу
    try:
        archive_query(
            query=query,
            tag=primary_tag,
            answer=synthesis_result.answer,
            confidence=synthesis_result.confidence,
            trust_level=synthesis_result.trust_level,
            session_id=request.session_id or "",
            sources=synthesis_result.sources,
        )
        tag_tree_update(primary_tag, query)
        record_unanswered(
            query=query,
            tag=primary_tag,
            confidence=synthesis_result.confidence,
            answer=synthesis_result.answer,
            session_id=request.session_id or "",
        )
    except Exception:
        pass

    # Пошаговый трейс для обучения оркестратора (SFT-формат)
    try:
        _tracer.trace(
            task=query,
            task_type=primary_tag,
            model="heretic:q8",
            result=synthesis_result.answer,
            context=f"risk={risk_result.risk_level}, steps={len(steps)}, "
                    f"conf={search_result.confidence:.2f}, trust={synthesis_result.trust_level}",
            outcome="success" if synthesis_result.confidence > 0.3 else "partial",
            elapsed_ms=int(total * 1000),
            steps=steps,
        )
        # Детальный пошаговый трейс — отдельный файл для SFT
        _write_step_trace(
            query=query,
            tags=tags,
            risk_result=risk_result,
            intent=intent_result,
            enrich=enrich_result,
            search=search_result,
            web_used=web_result is not None,
            synthesis=synthesis_result,
            steps=steps,
            total_ms=int(total * 1000),
            plan=plan,
            wq_result=wq_result if 'wq_result' in dir() else None,
        )
    except Exception:
        pass

    return OrchestratorResponse(
        answer=optimistic.text,
        trust_level=synthesis_result.trust_level,
        preliminary=True,
        sources=synthesis_result.sources,
        steps_taken=steps,
        latency_total=total,
        session_id=request.session_id,
    )


def interactive(enable_web: bool = False):
    """Интерактивный режим с сессией и уточнениями."""
    print(f"Orchestrator v2 — интерактивный режим (web={'вкл' if enable_web else 'выкл'})")
    print("exit/quit для выхода\n")

    session_id = new_session_id()
    print(f"Сессия: {session_id}")

    def clarify_callback(formatted: str) -> dict:
        print(f"\n{formatted}")
        answers = {}
        lines = [l for l in formatted.split("\n") if l.strip().startswith(tuple("123"))]
        for line in lines:
            try:
                num = int(line[0])
                answer = input(f"  Ответ {num}: ").strip()
                param  = f"param_{num}"
                if answer:
                    answers[param] = answer
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

        req  = OrchestratorRequest(query=query, session_id=session_id)
        resp = process(req, verbose=True, enable_web=enable_web,
                       clarify_callback=clarify_callback)
        print(f"\n{'─'*60}")
        print(resp.answer)
        print(f"\nLatency: {resp.latency_total}s | Trust: {resp.trust_level}")


if __name__ == "__main__":
    web      = "--web"      in sys.argv
    validate = "--validate" in sys.argv
    if "--interactive" in sys.argv or "-i" in sys.argv:
        interactive(enable_web=web)
    elif len(sys.argv) > 1:
        q = " ".join(a for a in sys.argv[1:] if not a.startswith("-"))
        if not q:
            q = "Как работает DHT?"
        req  = OrchestratorRequest(query=q)
        resp = process(req, verbose=True, enable_web=web, enable_validation=validate)
        print(f"\n{'─'*60}")
        print(resp.answer)
        print(f"\nLatency: {resp.latency_total}s | Trust: {resp.trust_level}")
    else:
        print("Использование:")
        print('  python3 assistant/orchestrator_v2.py "Вопрос"')
        print('  python3 assistant/orchestrator_v2.py "Вопрос" --web')
        print('  python3 assistant/orchestrator_v2.py --interactive [--web]')
