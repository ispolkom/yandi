"""
Frame construction + synthesize() — extracted from agent/orchestrator_v2.py
[8] ("Answer synthesize with hypothesis graph"): refutation scan,
query_frame["epistemic"]/entity assembly, hypothesis graph construction,
local-answer wait, blind analysis, source classification, and the
synthesize() call itself.

Structural extraction only: no frame keys, hypothesis-graph inputs, blind-
analysis ranking, relevance thresholds, or log markers changed.

`blind_analysis` moves here too — it had exactly one call site (inside this
block) in orchestrator_v2.py, so ownership moves with it rather than
leaving an orphaned single-use helper behind.

Boundary note: this stops right after the synthesize() try/except
(TimeoutError only). The bookkeeping immediately after it in
orchestrator_v2.py (trace.set_query_trace, cost["synthesize_ms"],
registry.update_latency/reliability) stays in orchestrator_v2.py on
purpose — one of those lines does `dt if 'dt' in locals() else 0`, a
call-site-scope introspection that would silently change meaning if moved
into a different function's locals().
"""

import time
from functools import partial
from typing import Any, Dict

from agent.claim_relation import classify_sources, extract_main_claim, is_relevant
from agent.hypothesis_builder import build_hypothesis_graph
from agent.orch_schemas import WebQueryResult
from agent.orch_synthesizer import synthesize
from agent.orch_timeout import step_timer
from agent.orch_web_scraper import scrape


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


def build_frame_and_synthesize(
    query_frame,
    epistemic_result,
    is_subjective_answer,
    is_media_query,
    intent_type,
    strategy_result,
    skip_rag,
    request,
    web_result,
    search_result,
    query_to_use,
    local_future,
    parallel_executor,
    request_fetch_cache,
    enrich_result,
    trace,
    cost,
    log,
    verbose,
):
    """
    Mutates query_frame in place (epistemic/intent_type/strategy/entity/
    hypothesis_graph/local_answer/blind_analysis/blind_selected_source/
    blind_status/best_answer/refutation_snippets/classified_sources) and
    cost (profile_refutation_ms/profile_hypothesis_graph_ms/
    profile_local_wait_ms/profile_blind_analysis_ms/
    profile_source_classification_ms). Also mutates trace (add_source,
    add_observation).

    Returns (synthesis_result, reasoning_info, synthesis_timed_out,
    refutation_snippets, entity, answer_mode, prof_synthesis_t0) — the
    values orchestrator_v2.py reads downstream of this block
    (refutation_snippets feeds claims/lifecycle.py's evidence pool
    assembly; entity and answer_mode are read much later, in [10];
    prof_synthesis_t0 is read immediately after the call to compute
    cost["synthesize_ms"] — that computation stays in orchestrator_v2.py,
    see the module docstring's boundary note).
    """
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
            refutation_result, dt_ref, timed_out_ref = step_timer(
                "refutation_scrape",
                partial(scrape, refutation_wq, fetch_cache=request_fetch_cache),
            )
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
    query_frame["strategy"] = strategy_result.strategy.value if not skip_rag else "skip"

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

    return synthesis_result, reasoning_info, synthesis_timed_out, refutation_snippets, entity, answer_mode, _prof_synthesis_t0
