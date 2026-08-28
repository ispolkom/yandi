"""
agent/claim_evidence_retriever.py

Claim-specific evidence retrieval.

Назначение:
    После structural validation claims выполнить узкий второй
    retrieval pass для конкретных factual claims.

ВАЖНО:
    retrieval НЕ определяет истинность claim;
    retrieval НЕ назначает supports / contradicts;
    retrieval НЕ создаёт epistemic status.

Он только:
    claim
      -> поисковые запросы
      -> web candidates
      -> Source Quality
      -> EvidenceRecord-compatible dict

Логическое отношение evidence -> claim позже определяет NLI.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time

import json
import re
import uuid
from typing import Any, Dict, List

from agent.orch_schemas import WebQueryResult
from agent.orch_web_query import _call_ollama
from agent.orch_web_scraper import scrape_budgeted, SharedFetchCache
from agent.source_quality import evaluate_source_quality
from agent.claim_relation import (
    is_relevant,
    extract_claim_from_source,
)
from agent.claim_identity import extract_subject_anchors as _extract_subject_anchors


# ------------------------------------------------------------
# Ограничители стоимости / latency.
# ------------------------------------------------------------

# Максимальное число claims для adaptive second-pass retrieval.
#
# ВАЖНО:
# Orchestrator уже сам определяет, какие claims требуют дополнительного
# retrieval. Поэтому retriever не должен молча отбрасывать половину
# переданного списка.
MAX_CLAIMS = 8
MAX_QUERIES_PER_CLAIM = 2

# Async claim pipeline: serializes retrieve_claim_evidence()'s fallback
# single-claim query-generation call (see call site for full rationale) —
# a bounded-2 concurrent path (GENERATION_SEMAPHORE) is reduced to fully
# sequential, closing the one new concurrent-Ollama-call source the async
# pipeline introduces outside its own single-consumer NLI queue.
_query_generation_lock = threading.Lock()

# P1 (performance architecture pass): claims per batched query-
# generation LLM call. Chosen over batch_size=8 (all MAX_CLAIMS in one
# call) after a live experiment showed both are equally accurate
# (100% ownership, 0 leakage, modality preserved) — 4 keeps a single
# failed/truncated batch call's blast radius to at most half of
# MAX_CLAIMS rather than all of it.
QUERY_BATCH_SIZE = 4



def _extract_json(text: str) -> dict:
    """
    Безопасно извлечь JSON из ответа модели.
    """
    if not text:
        return {}

    text = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL,
    ).strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)

    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass

    return {}


def formulate_claim_evidence_queries(
    claim_text: str,
) -> WebQueryResult:
    """
    Сформировать ДВА разных поисковых запроса для одного claim.

    1. DIRECT:
       искать данные / наблюдения / исследования,
       которые непосредственно относятся к claim.

    2. COUNTER:
       искать данные, способные опровергнуть claim
       или показать противоположный результат.

    ВАЖНО:
       найденный результат НЕ считается support/counter
       автоматически. Это решит NLI.
    """

    claim_text = (claim_text or "").strip()

    if not claim_text:
        return WebQueryResult(
            queries=[],
            raw="[empty claim]",
        )

    prompt = f"""
Ты формулировщик поисковых запросов для проверки ОДНОГО
атомарного factual claim.

CLAIM:
{claim_text}

Создай РОВНО 2 поисковых запроса.

1. DIRECT_EVIDENCE
Ищи первичные данные, наблюдения, измерения, исследования,
эксперименты, миссии, документы или научные материалы,
непосредственно относящиеся к этому claim.

2. COUNTER_EVIDENCE
Ищи данные, наблюдения, исследования или документы,
которые могли бы противоречить claim либо показывать
противоположный результат.

ПРАВИЛА:

- Не предполагай, что claim истинен.
- Не предполагай, что claim ложен.
- Search result НЕ является доказательством сам по себе.
- Не заменяй объект claim похожим объектом.
- Сохраняй subject scope.
- Сохраняй временной scope, если он есть.

- КРИТИЧЕСКИ ВАЖНО сохранять epistemic modality claim.
  Отрицание и степень утверждения нельзя терять при генерации query.

  Например:

    claim:
      "Разумная жизнь на Юпитере не обнаружена"

    DIRECT_EVIDENCE должен искать формулировки типа:
      "Jupiter no confirmed evidence intelligent life"
      "Jupiter no detection of intelligent life observations"

    а НЕ просто:
      "Jupiter intelligent life research"

  Для claim с:
      "не обнаружено"
      "нет подтверждений"
      "не доказано"
      "не найдено"

  direct query обязан содержать эквивалент отрицательной
  epistemic конструкции:
      no evidence
      no confirmed evidence
      no detection
      not detected
      no confirmed detection

  COUNTER_EVIDENCE, наоборот, должен искать положительный
  противоположный результат:
      detected
      discovery
      evidence found
      confirmed evidence
      biosignature detected

- Сохраняй модальность:
  "существует", "возможно", "не обнаружено", "доказано"
  означают разные claims.

- Для научных вопросов английский язык допустим
  и предпочтителен, если он улучшает поиск.
- Максимум 8-14 слов на запрос.
- Не создавай две простые перефразировки.
- Без вопросительных предложений.

Верни ТОЛЬКО JSON:

{{
  "queries": [
    "direct evidence query",
    "counter evidence query"
  ]
}}
""".strip()

    try:
        raw = _call_ollama(prompt)
        data = _extract_json(raw)

        queries = [
            q.strip()
            for q in data.get("queries", [])
            if isinstance(q, str) and q.strip()
        ][:MAX_QUERIES_PER_CLAIM]

        if not queries:
            # Нейтральный fallback.
            #
            # Здесь намеренно нет "доказательство того, что..."
            # или "опровержение...", потому что retrieval не должен
            # заранее назначать relation.
            queries = [
                f"{claim_text} evidence observations data",
                f"{claim_text} contradictory evidence research",
            ]

        return WebQueryResult(
            queries=queries,
            raw=raw,
        )

    except Exception as exc:
        return WebQueryResult(
            queries=[
                f"{claim_text} evidence observations data",
                f"{claim_text} contradictory evidence research",
            ],
            raw=f"[fallback: {exc}]",
        )


_BATCH_QUERY_PROMPT_TEMPLATE = """
Ты формулировщик поисковых запросов для проверки НЕСКОЛЬКИХ независимых
атомарных factual claims ОДНОВРЕМЕННО.

КАЖДЫЙ claim обрабатывается ПОЛНОСТЬЮ НЕЗАВИСИМО от остальных:
- Не переноси субъект/сущность одного claim в query другого claim.
- Не смешивай формулировки между claims.
- Каждый claim_id должен получить РОВНО 2 запроса: direct и counter.

CLAIMS:
{claims_json}

Для КАЖДОГО claim_id создай:

1. direct
   Ищи первичные данные, наблюдения, измерения, исследования,
   эксперименты, миссии, документы, непосредственно относящиеся
   ИМЕННО К ЭТОМУ claim_id (не к другим).

2. counter
   Ищи данные, наблюдения или документы, которые могли бы
   противоречить ИМЕННО ЭТОМУ claim_id либо показывать
   противоположный результат.

ПРАВИЛА (применяются к каждому claim_id независимо от остальных):

- Не предполагай, что claim истинен.
- Не предполагай, что claim ложен.
- Не заменяй объект claim похожим объектом.
- Сохраняй subject scope и временной scope именно ЭТОГО claim_id.
- КРИТИЧЕСКИ ВАЖНО сохранять epistemic modality claim. Отрицание и
  степень утверждения нельзя терять при генерации query.

  Например для claim с "не обнаружено"/"нет подтверждений"/"не найдено":
  direct query обязан содержать эквивалент отрицательной epistemic
  конструкции (no evidence, no confirmed detection, not detected), а
  counter — положительный противоположный результат (detected,
  discovery, confirmed evidence).

- Для научных вопросов английский язык допустим и предпочтителен.
- Максимум 8-14 слов на запрос.
- Не создавай две простые перефразировки.
- Без вопросительных предложений.

Верни ТОЛЬКО JSON — один ключ на КАЖДЫЙ claim_id из входа, ни одного
пропущенного, ни одного лишнего:

{{
  "<claim_id из входа>": {{"direct": "...", "counter": "..."}},
  ...
}}
""".strip()


def _call_ollama_for_query_batch(prompt: str, batch_size: int) -> str:
    """
    P1 (performance architecture pass): dedicated call for batch query
    generation. NOT the shared orch_web_query._call_ollama(), whose
    num_predict is hardcoded to MAX_TOKENS_CONDUCTOR=500 — sized for
    the ORIGINAL single-claim 2-query call, not a variable-size batch
    of N claims x 2 queries each. Same scaling pattern already used by
    infer_claim_relations_batch() (claim_relation.py): num_predict
    grows with batch_size instead of risking the same truncation bug
    P0-B already found and fixed elsewhere.
    """
    import requests
    from agent.orch_config import (
        OLLAMA_BASE,
        MODEL,
        TEMP_CONDUCTOR,
        GENERATION_SEMAPHORE,
    )

    session = requests.Session()
    session.trust_env = False

    num_predict = max(300, batch_size * 120)

    wait_started = time.time()

    with GENERATION_SEMAPHORE:
        waited = time.time() - wait_started

        if waited > 0.05:
            print(f"[Claim Query Batch LLM] generation queue wait={waited:.2f}s")

        resp = session.post(
            f"{OLLAMA_BASE}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": TEMP_CONDUCTOR,
                    "num_predict": num_predict,
                },
            },
            timeout=90,
        )
        resp.raise_for_status()

        return resp.json().get("response", "").strip()


def formulate_claim_evidence_queries_batch(
    claims: "List[Dict[str, str]]",
) -> "Dict[str, WebQueryResult]":
    """
    P1 prototype (performance architecture pass) — batch query
    generation for several claims in ONE LLM call instead of one call
    per claim.

    claims: [{"claim_id": ..., "claim_text": ...}, ...] — claim_text
    here is the SAME contextual_claim_text the per-claim path uses.

    Returns {claim_id: WebQueryResult}. EVERY input claim_id is
    guaranteed a key in the result — either from the batch response,
    or (bounded, diagnosed) from a per-claim
    formulate_claim_evidence_queries() fallback call. Query ownership
    is explicit by claim_id key in the JSON, never inferred by
    position — never attributes one claim's queries to another.
    """
    results: "Dict[str, WebQueryResult]" = {}

    valid_claims = [
        c for c in claims
        if isinstance(c, dict)
        and (c.get("claim_text") or "").strip()
        and c.get("claim_id")
    ]

    if not valid_claims:
        return results

    claims_payload = [
        {"claim_id": c["claim_id"], "claim_text": c["claim_text"].strip()}
        for c in valid_claims
    ]

    prompt = _BATCH_QUERY_PROMPT_TEMPLATE.format(
        claims_json=json.dumps(claims_payload, ensure_ascii=False, indent=2),
    )

    raw = ""
    data: Dict[str, Any] = {}

    try:
        raw = _call_ollama_for_query_batch(prompt, len(valid_claims))

        from agent.final_claim_coverage import _extract_json as _extract_json_lenient
        parsed = _extract_json_lenient(raw)

        if isinstance(parsed, dict):
            data = parsed

    except Exception as exc:
        print(
            f"[Claim Query Batch] status=call_error "
            f"claims={len(valid_claims)} "
            f"error={type(exc).__name__}"
        )

    fallback_claim_ids = []

    for c in valid_claims:
        cid = c["claim_id"]
        entry = data.get(cid) if isinstance(data, dict) else None

        queries = []

        if isinstance(entry, dict):
            direct = str(entry.get("direct", "") or "").strip()
            counter = str(entry.get("counter", "") or "").strip()
            queries = [q for q in (direct, counter) if q]

        if queries:
            results[cid] = WebQueryResult(
                queries=queries[:MAX_QUERIES_PER_CLAIM],
                raw=raw,
            )
        else:
            fallback_claim_ids.append(cid)

    if fallback_claim_ids:
        # Bounded, diagnosed fallback — never silent, never a second
        # unbounded round of individual calls for the WHOLE batch,
        # only for the specific claim_ids the batch response actually
        # missed.
        print(
            f"[Claim Query Batch] status=partial_fallback "
            f"claims={len(valid_claims)} "
            f"fallback_calls={len(fallback_claim_ids)}"
        )

        for c in valid_claims:
            if c["claim_id"] in fallback_claim_ids:
                results[c["claim_id"]] = formulate_claim_evidence_queries(
                    c["claim_text"]
                )
    else:
        print(
            f"[Claim Query Batch] status=ok "
            f"claims={len(valid_claims)} "
            f"fallback_calls=0"
        )

    return results


def _subject_anchor_matches(
    claim_text: str,
    passage: str,
    *,
    title: str = "",
    url: str = "",
) -> "tuple[bool, List[str]]":
    """
    MULTI-SIGNAL SUBJECT IDENTITY GATE.

    Отвечает ТОЛЬКО на вопрос: "документ относится к объекту claim?".
    НЕ отвечает на вопрос "документ доказывает claim?" — это задача
    Mapper/NLI, gate её не подменяет.

    Если claim имеет явный subject anchor, документ должен подтвердить
    объект хотя бы ОДНИМ надёжным identity-сигналом. Приоритет
    сигналов (любой один достаточен):

        1. title
        2. url
        3. passage (claim_passage — узкий top-3 фрагмент,
           либо fallback на полный text, см. вызывающий код)

    ВАЖНО:
    Полный текст документа как отдельный сигнал сюда сознательно
    НЕ добавлен. На объёмной multi-subject странице anchor может
    встретиться один случайный раз в несвязанном контексте; чтобы
    учитывать это безопасно, потребовалась бы новая эвристика
    (частота упоминаний, окно контекста, threshold) — её решили не
    изобретать в рамках этого исправления.

    Если anchor извлечь не удалось — gate ничего не запрещает
    (как и раньше).

    Возвращает (matched: bool, matched_fields: List[str]) — список
    полей, которые реально подтвердили identity (для диагностики).
    """
    anchors = _extract_subject_anchors(claim_text)

    if not anchors:
        return True, []

    matched_fields: List[str] = []

    title_haystack = (title or "").lower()
    url_haystack = (url or "").lower()
    passage_haystack = (passage or "").lower()

    if any(anchor in title_haystack for anchor in anchors):
        matched_fields.append("title")

    if any(anchor in url_haystack for anchor in anchors):
        matched_fields.append("url")

    if any(anchor in passage_haystack for anchor in anchors):
        matched_fields.append("passage")

    return bool(matched_fields), matched_fields


def _snippet_text(snippet: Any) -> str:
    """
    Получить основной текст WebSnippet.
    """
    text = getattr(snippet, "text", "") or ""
    content = getattr(snippet, "content", "") or ""

    return text or content


def _resolve_query_context(claim: Dict[str, Any]) -> str:
    """
    Single source of truth for resolving a claim's query context.

    Root-cause fix (query_context NameError, see
    YANDI_EPISTEMIC_DEPENDENCY_REEVALUATION_REPORT.md §14 item 1):
    commit 61279fe ("perf: batch claim-specific query generation")
    extracted this exact precedence chain out of retrieve_claim_evidence()
    into _build_contextual_claim_text(), but only carried the DERIVED
    contextual_claim_text value into the new helper. retrieve_claim_
    evidence() had a SECOND, independent use of the same raw query_context
    local further down (the SUBJECT ANCHOR VIEW block's `elif
    query_context:`), which the extraction left behind, referencing a name
    that no longer existed in that scope. Meanwhile _claim_retrieval_
    priority() already had its own independent, correct copy of this exact
    precedence chain — so by the time this was found, the logic existed
    in two places that could have silently drifted apart, plus one broken
    call site. This helper is the single definition now; both existing
    copies and the missing call site all resolve through it.
    """
    return (
        claim.get("query_context", "")
        or claim.get("source_query", "")
        or claim.get("original_query", "")
        or ""
    ).strip()


def _build_contextual_claim_text(claim: Dict[str, Any], claim_text: str) -> str:
    """
    Shared by retrieve_claim_evidence() and retrieve_for_claims()'s P1
    batch pre-computation — one definition, not two copies that could
    silently drift apart.
    """
    query_context = _resolve_query_context(claim)

    if query_context:
        return f"{query_context}\nПроверяемое утверждение: {claim_text}"

    return claim_text


def retrieve_claim_evidence(
    claim: Dict[str, Any],
    fetch_cache: "SharedFetchCache | None" = None,
    precomputed_query_result: "WebQueryResult | None" = None,
) -> List[Dict[str, Any]]:
    """
    Выполнить evidence retrieval для одного accepted claim.

    Возвращает EvidenceRecord-compatible dictionaries.

    НИКАКИХ derived_from_evidence_ids здесь не создаётся.
    НИКАКИХ relations здесь не создаётся.

    fetch_cache: request-scoped, передаётся от retrieve_for_claims()
    и разделяется МЕЖДУ claims в рамках одного запроса пользователя —
    чтобы один и тот же URL, независимо найденный разными claims, не
    скачивался физически дважды. Это НЕ меняет evidence ownership:
    каждый claim по-прежнему получает СВОЮ evidence-запись с
    retrieval_claim_id=этот claim — см. SharedFetchCache docstring.

    precomputed_query_result: P1 (performance architecture pass) —
    если retrieve_for_claims() уже сформировал queries для ЭТОГО claim
    батчем (formulate_claim_evidence_queries_batch), передаётся сюда
    вместо повторного одиночного LLM-вызова. None (по умолчанию) —
    прежнее поведение, свой собственный вызов formulate_claim_
    evidence_queries() — сохраняет обратную совместимость для прямых
    вызовов этой функции (регрессия, другие callers).
    """

    claim_text = (
        claim.get("claim_text", "")
        if isinstance(claim, dict)
        else ""
    )

    claim_text = (claim_text or "").strip()

    if not claim_text:
        return []

    # ========================================================
    # CONTEXTUAL CLAIM VIEW
    # ========================================================
    #
    # Atomic claim может потерять субъект исходного вопроса:
    #
    #   query:
    #       "Есть ли разумная жизнь на Юпитере?"
    #
    #   atomic claim:
    #       "Температура варьируется от -145°C..."
    #
    # Для retrieval это опасно: поиск и embeddings начинают
    # находить тематически похожие данные о Земле, экзопланетах
    # и других объектах.
    #
    # Поэтому retrieval получает contextual view.
    #
    # ВАЖНО:
    # original claim_text НЕ изменяется.
    # NLI по-прежнему проверяет именно atomic claim.
    #
    contextual_claim_text = _build_contextual_claim_text(claim, claim_text)

    # ========================================================
    # SUBJECT ANCHOR VIEW
    # ========================================================
    #
    # Retrieval context и subject identity — разные вещи.
    #
    # Нельзя извлекать subject anchors из всей contextual строки:
    # служебные/начальные слова вроде "Проверяемое",
    # "Температуры" и т.п. могут ошибочно стать anchors.
    #
    # Если atomic claim уже содержит явный subject — используем его.
    # Если subject потерян при atomization — наследуем его из query.
    #
    # query_context здесь ДОЛЖЕН быть тем же значением, что уже
    # использовал _build_contextual_claim_text() выше (single source
    # of truth — см. _resolve_query_context()'s docstring про root
    # cause утерянной переменной после коммита 61279fe).
    query_context = _resolve_query_context(claim)

    claim_subject_anchors = _extract_subject_anchors(
        claim_text
    )

    if claim_subject_anchors:
        subject_anchor_text = claim_text
    elif query_context:
        subject_anchor_text = query_context
    else:
        subject_anchor_text = claim_text

    # P1 (autonomous fix pass, claim_specific_retrieval=235.56s live
    # bottleneck investigation): per-phase sub-timers so the aggregate
    # worker time can actually be attributed instead of staying one
    # opaque number. Real code paths only — no NLI/LLM call happens
    # inside this function (that's a separate, already-instrumented
    # downstream stage: claim_pass2_mapping_nli_ms).
    _t0_query_gen = time.time()

    if precomputed_query_result is not None:
        query_result = precomputed_query_result
    else:
        # Async claim pipeline (agent/orchestrator/claims/async_pipeline.py):
        # up to MAX_CLAIM_WORKERS=3 claims can call retrieve_claim_evidence()
        # concurrently (via asyncio.to_thread), each hitting this fallback
        # when no batch-precomputed query was available. Before the async
        # pipeline existed, retrieve_for_claims() ALWAYS precomputed queries
        # for the whole retrieval_claims list up front, so this branch was
        # never exercised concurrently in production. GENERATION_SEMAPHORE
        # (orch_config.py) already bounds concurrent Ollama generation calls
        # to 2, but a live async-pipeline run showed a batch of
        # HTTPError:500 responses from the local Ollama server shortly
        # after a run with this fallback exercised at up to 2-3 concurrent
        # callers for the first time — root cause not conclusively proven,
        # but the correlation (zero HTTPError:500 in any of 5 prior
        # benchmark runs, including concurrent ones, vs. 33 in this run)
        # is real and this is the one NEW concurrent-Ollama-call path the
        # async pipeline introduces outside its own single-consumer NLI
        # queue. Serializing it fully (not just semaphore-bounding to 2)
        # is a low-cost precaution — query generation is a short call, not
        # the GPU-heavy NLI path — that removes this specific new
        # concurrency source entirely rather than leaving it as a
        # plausible-but-unconfirmed contributor.
        with _query_generation_lock:
            query_result = formulate_claim_evidence_queries(
                contextual_claim_text
            )

    _query_generation_ms = (time.time() - _t0_query_gen) * 1000

    # P1 (YANDI_FINAL_EPISTEMIC_AUDIT_AND_FIX.md): раньше сгенерированные
    # queries не логировались НИГДЕ в момент формирования — видны были
    # только задним числом, внутри уже успешной evidence record
    # ("retrieval_queries"). Для claim, у которого retrieval вернул 0
    # записей (типичный CORE/DIRECT кейс), реальные search-строки были
    # полностью невидимы — нельзя было доказать, теряется ли subject
    # anchor при формулировке запроса. Компактный print, не полный текст.
    print(
        f"[Claim Retrieval Query] "
        f"claim_id={claim.get('claim_id', 'unknown')} "
        f"anchors={claim_subject_anchors or '-'} "
        f"queries={list(query_result.queries)}"
    )

    if not query_result.queries:
        return []

    _t0_web_request = time.time()

    # P4 (web budget 3+3): scrape_budgeted() replaces the old single
    # scrape(query_result, max_results=CLAIM_RETRIEVAL_POOL=10) call.
    # query_result.queries is [direct_query, counter_query] by
    # construction (formulate_claim_evidence_queries() and its batch
    # counterpart both build the list in that exact order - "Создай
    # РОВНО 2 поисковых запроса: 1. DIRECT_EVIDENCE 2. COUNTER_EVIDENCE").
    # Each side now gets its own independent max-3 fetch budget instead
    # of a single shared max-10 pool that could silently starve one
    # side (see YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md P4).
    direct_q = query_result.queries[0] if len(query_result.queries) > 0 else ""
    counter_q = query_result.queries[1] if len(query_result.queries) > 1 else ""

    try:
        web_result = scrape_budgeted(
            direct_q,
            counter_q,
            fetch_cache=fetch_cache,
            claim_id=claim.get("claim_id", ""),
            # P6 (Этап 4 §7): pass only the identity token, not the
            # claim object — scrape_budgeted() must not couple to
            # claims_data internals.
            content_hash=claim.get("content_hash", "") or "",
        )
    except Exception as exc:
        print(
            f"[Claim Retrieval] scrape error "
            f"claim={claim.get('claim_id', 'unknown')} "
            f"error={exc}"
        )
        return []

    _web_request_ms = (time.time() - _t0_web_request) * 1000

    if not web_result or not web_result.snippets:
        print(
            "[Claim Retrieval Worker SubProfile] "
            f"claim_id={claim.get('claim_id', 'unknown')} "
            f"query_generation={_query_generation_ms:.1f}ms "
            f"web_request={_web_request_ms:.1f}ms "
            f"parsing=0.0ms embedding=0.0ms "
            f"total={(_query_generation_ms + _web_request_ms):.1f}ms "
            f"note=no_snippets"
        )
        return []

    evidence_records = []

    seen_urls = set()

    _parsing_ms = 0.0
    _embedding_ms = 0.0

    for snippet in web_result.snippets:
        url = getattr(snippet, "url", "") or ""
        title = getattr(snippet, "title", "") or ""
        text = _snippet_text(snippet).strip()

        if not text or len(text) < 100:
            continue

        # URL dedup внутри одного claim retrieval.
        if url and url in seen_urls:
            continue

        if url:
            seen_urls.add(url)

        # ----------------------------------------------------
        # CLAIM-SPECIFIC SEMANTIC RELEVANCE GATE
        # ----------------------------------------------------
        #
        # scrape() уже проверил соответствие поисковым запросам.
        # Но поисковый query и сам atomic claim — не одно и то же.
        #
        # Например:
        #
        #   claim: "На Юпитере разумная жизнь не обнаружена"
        #   source: Wikipedia / Moon
        #
        # Source может быть качественным reference, но не иметь
        # достаточной смысловой связи с конкретным claim.
        #
        # Embedding здесь используется ТОЛЬКО как semantic gate.
        # Он НЕ определяет supports/contradicts.
        try:
            # Сначала достаём из документа наиболее близкие к claim
            # passages. Полная страница может содержать меню,
            # вводный текст и тысячи нерелевантных символов.
            _t0_parsing = time.time()
            claim_passage = extract_claim_from_source(
                text,
                contextual_claim_text,
            )
            _parsing_ms += (time.time() - _t0_parsing) * 1000

            passage_for_check = claim_passage or text

            # ------------------------------------------------
            # SUBJECT ANCHOR GATE (multi-signal)
            # ------------------------------------------------
            #
            # Semantic similarity не должна подменять объект:
            #
            #   Jupiter != K2-18b
            #   Jupiter != generic exoplanets
            #   Jupiter != Europa
            #
            # Если claim содержит явный именованный субъект, объект
            # должен подтвердиться хотя бы одним из: title, url,
            # claim_passage. Узкий top-3 passage — не единственный
            # источник identity: широкая multi-subject страница может
            # упоминать объект в title/URL, даже если он не попал
            # именно в отобранные под claim 3 предложения.
            subject_match, subject_matched_fields = (
                _subject_anchor_matches(
                    subject_anchor_text,
                    passage_for_check,
                    title=title,
                    url=url,
                )
            )

            if not subject_match:
                print(
                    f"[Subject Gate] decision=reject "
                    f"claim_id={claim.get('claim_id', 'unknown')} "
                    f"anchors={_extract_subject_anchors(subject_anchor_text)} "
                    f"url={url[:120]} "
                    f"title={title[:120]!r} "
                    f"passage={passage_for_check[:300]!r} "
                    f"matched_fields=[]"
                )
                continue

            print(
                f"[Subject Gate] decision=pass "
                f"claim_id={claim.get('claim_id', 'unknown')} "
                f"matched_fields={','.join(subject_matched_fields) or 'none'} "
                f"url={url[:120]}"
            )

            _t0_embedding = time.time()
            claim_relevant = is_relevant(
                passage_for_check,
                contextual_claim_text,
                threshold=0.4,
            )
            _embedding_ms += (time.time() - _t0_embedding) * 1000

        except Exception:
            # При технической ошибке relevance не выдумываем.
            claim_passage = ""
            claim_relevant = False

        if not claim_relevant:
            print(
                f"[Claim Retrieval] "
                f"reject semantic_irrelevant "
                f"claim={claim.get('claim_id', 'unknown')} "
                f"url={url[:70]}"
            )
            continue

        quality = evaluate_source_quality(
            url=url,
            title=title,
            text=text,
            source_type="web",
        )

        ev_id = f"ev_{uuid.uuid4().hex[:8]}"

        evidence_records.append({
            "evidence_id": ev_id,

            "source_type": "web",
            "source_uri": url,
            "source_title": title,

            # Для downstream Mapper/NLI сохраняем passage,
            # который наиболее близок именно к проверяемому claim.
            # Это не означает supports — отношение всё ещё определяет NLI.
            "content_excerpt": (
                claim_passage[:500]
                if claim_passage
                else text[:500]
            ),

            # Это candidate relevance.
            # НЕ epistemic relation.
            "relevance_to_query": getattr(
                snippet,
                "relevance",
                0.5,
            ),

            # Source Quality metadata.
            "quality_score": quality.quality_score,
            "source_class": quality.source_class,
            "evidence_eligible": quality.evidence_eligible,
            "evidence_role": quality.evidence_role,
            "authority": quality.authority,
            "traceability": quality.traceability,
            "primaryness": quality.primaryness,
            "quality_reasons": list(quality.reasons),

            "is_meta_pipeline_output": False,
            "is_subject_matter_evidence": True,
            "rejection_reason": None,

            # Диагностика происхождения.
            #
            # Это НЕ mapping.
            # Mapper всё равно обязан самостоятельно решить,
            # относится ли evidence к claim.
            "retrieval_origin": "claim_specific",
            "retrieval_claim_id": claim.get(
                "claim_id",
                "",
            ),
            "retrieval_claim_text": claim_text[:300],
            "retrieval_queries": list(
                query_result.queries
            ),
            # P6 (Этап 4 §9, Finding 2 fix): WebSnippet.origin (Этап 2)
            # already tags "direct"/"counter" per-snippet — carried
            # through here instead of being silently dropped.
            "route_side": getattr(snippet, "origin", "") or "",
        })

    # --------------------------------------------------------
    # FINAL CLAIM-SPECIFIC RANKING
    # --------------------------------------------------------
    #
    # Только теперь, ПОСЛЕ проверки semantic relation к claim,
    # используем Source Quality для финального выбора.
    role_priority = {
        "direct": 3,
        "secondary": 2,
        "context": 1,
        "internal": 0,
    }

    evidence_records.sort(
        key=lambda ev: (
            role_priority.get(
                ev.get("evidence_role", "context"),
                1,
            ),
            float(ev.get("quality_score", 0.0) or 0.0),
        ),
        reverse=True,
    )

    _total_ms = _query_generation_ms + _web_request_ms + _parsing_ms + _embedding_ms

    print(
        "[Claim Retrieval Worker SubProfile] "
        f"claim_id={claim.get('claim_id', 'unknown')} "
        f"query_generation={_query_generation_ms:.1f}ms "
        f"web_request={_web_request_ms:.1f}ms "
        f"parsing={_parsing_ms:.1f}ms "
        f"embedding={_embedding_ms:.1f}ms "
        f"total={_total_ms:.1f}ms "
        f"snippets={len(web_result.snippets)}"
    )

    # P4 (web budget 3+3): previously truncated to MAX_RESULTS_PER_CLAIM
    # (3), back when fetch itself was uncapped at up to CLAIM_RETRIEVAL_
    # POOL=10 candidates - a real, needed safety margin then. Now that
    # scrape_budgeted() already caps fetch at <=6 (3 direct + 3
    # counter), this list can never exceed 6 to begin with, and
    # truncating further would risk silently discarding counter-side
    # evidence whenever direct-side items happen to rank higher by
    # role/quality - defeating the point of giving counter its own
    # dedicated, independent budget. Sort (still useful for downstream
    # priority) kept; the cut removed.
    return evidence_records


def _query_relevance_score(
    claim_text: str,
    query_context: str,
) -> float:
    """
    Непрерывная relevance claim -> исходный query пользователя.

    Аудит (YANDI_FULL_PIPELINE_AUDIT.md, P0.1) установил: priority
    ранее измерял только лексическую специфичность claim (цифры,
    физическая терминология, упоминание субъекта), но НЕ relevance
    к тому, что именно спросил пользователь. Из-за этого дорогой
    PASS2 retrieval budget доставался background/explanatory claims
    чаще, чем claims, напрямую отвечающим на вопрос.

    ВАЖНО:
    - это НЕ truth score и НЕ epistemic status;
    - это НЕ замена Mapper (Mapper связывает claim с evidence,
      здесь же — claim с исходным query);
    - при недоступности embedding endpoint возвращает 0.0
      (нейтрально: не штрафует и не поднимает claim), чтобы не
      выдумывать relevance при технической ошибке — тот же принцип,
      что уже используется в is_relevant()/classify_relation().

    Переиспользует тот же embedding call pattern (embeddinggemma
    через локальный Ollama), что уже применяется в
    claim_evidence_mapper.py и claim_relation.py — новый backend
    не добавляется.
    """
    claim_text = (claim_text or "").strip()
    query_context = (query_context or "").strip()

    if not claim_text or not query_context:
        return 0.0

    try:
        import requests
        import numpy as np

        session = requests.Session()
        session.trust_env = False

        def _embed(value: str):
            resp = session.post(
                "http://127.0.0.1:11434/api/embed",
                json={
                    "model": "embeddinggemma:latest",
                    "input": value[:2000],
                },
                timeout=15,
            )
            resp.raise_for_status()

            vec = np.array(
                resp.json()["embeddings"][0],
                dtype=np.float32,
            )

            norm = np.linalg.norm(vec)
            return vec / norm if norm > 0 else vec

        claim_vec = _embed(claim_text)
        query_vec = _embed(query_context)

        return float(np.dot(claim_vec, query_vec))

    except Exception:
        return 0.0


# ------------------------------------------------------------
# ABSENCE / NEGATIVE-EVIDENCE CLAIM DETECTION (P0.2)
# ------------------------------------------------------------
#
# Для existence-вопросов ("есть ли X?") отсутствие evidence может
# быть ЦЕНТРАЛЬНЫМ ответом (YANDI_FULL_PIPELINE_AUDIT.md, §14/§18).
# Прежняя формула priority ничего не знала о such claims явно —
# они получали низкий score не из-за штрафа, а по лексической
# случайности (напр. "не обнаружил" не совпадает с паттерном
# "обнаружен" — разные глагольные формы).
#
# Это НЕ epistemic gate, НЕ Claim Status, НЕ trust. Единственная
# роль — не дать claims вида "не найдено/не обнаружено/нет
# доказательств" систематически проигрывать retrieval budget
# background claims только из-за формы отрицания.
# CANONICAL claim-level absence/negative-evidence markers
# (YANDI_ABSENCE_REGRESSION_FIX.md).
#
# ЕДИНСТВЕННОЕ место, где определяется claim-level absence
# semantics. И _is_absence_claim(), и _classify_claim_role() (через
# _EXISTENCE_ASSERTION_MARKERS ниже) читают именно этот tuple —
# специально, чтобы не могло возникнуть двух расходящихся
# определений "отрицания" claim.
#
# Родовая грамматическая конструкция "не X" в русском языке часто
# разбивается вспомогательным глаголом/наречием: "не БЫЛА
# обнаружена", "не БЫЛИ обнаружены", "ПОКА не обнаружена". Простое
# "не\s+обнаруж" ловит только смежный случай ("не обнаружена") и
# пропускает "не была обнаружена" — раньше это создавало
# рассинхронизацию между _is_absence_claim() (глухой к таким
# формам) и _classify_claim_role() (ловил их случайно, через
# отдельный БЕЗУСЛОВНЫЙ positive-detection marker, не различающий
# полярность). Допускаем один короткий разрыв — вспомогательный
# глагол/наречие — между "не" и корнем.
_NEGATION_GAP = (
    r"(?:\s+(?:была|было|были|есть|пока|ещё|уже))?"
)

_ABSENCE_MARKERS = (
    rf"не{_NEGATION_GAP}\s+обнаруж",
    rf"не{_NEGATION_GAP}\s+найден",
    # YANDI_CLAIM_ROLE_MORPHOLOGY_FIX.md, BUG 2: "зафиксирован" (12
    # симв.) — participle-форма (зафиксирован/-а/-ы), НЕ покрывает
    # глагольную форму "зафиксировали" (о-в-а-л-и, а не -о-в-а-н).
    # Общий корень для обеих форм короче — "зафиксирова".
    rf"не{_NEGATION_GAP}\s+зафиксирова",
    rf"не{_NEGATION_GAP}\s+выявлен",
    # Аналогично "установлен" -> общий корень с "установили".
    rf"не{_NEGATION_GAP}\s+установ",
    r"нет\s+доказательств",
    r"нет\s+свидетельств",
    r"нет\s+подтверждени",
    r"не\s+подтвержд",
    r"отсутству",
    r"ни\s+один[^.]*не\s+",
    # BUG 1: голое "Нет X" (экзистенциальное отрицание — "X не
    # существует") — самая прямая форма absence claim, не покрытая
    # "нет доказательств/свидетельств/подтверждения" (evidence-of-X
    # паттерны). ВАЖНО: absence != CORE сам по себе — target_match
    # в _classify_claim_role() отдельно решает, относится ли это
    # "нет X" к target вопроса (см. BACKGROUND-кейс "нет жидкой
    # воды" при target="жизнь").
    #
    # Исключаем "нет сомнений" — это hedge-конструкция двойного
    # отрицания ("нет сомнений, что X" означает X ИСТИННО, обратное
    # по смыслу обычному "нет X"). Не претендуем на полное покрытие
    # всех таких идиом — только самый частый случай.
    r"\bнет\s+(?!сомнени)[а-яё]",
)


def _is_absence_claim(claim_text: str) -> bool:
    """
    Claim СЕМАНТИЧЕСКИ утверждает отсутствие / необнаружение /
    неподтверждённость некоторого объекта, явления или evidence.

    ВАЖНО (архитектурная роль, не менять смысл при правках):
    это НЕ "в предложении есть отрицание" (generic grammatical
    negation) — например, "температура не превышает -145°C" не
    absence claim, это отрицательное количественное сравнение.
    Список маркеров сознательно ограничен глаголами обнаружения/
    подтверждения/фиксации, а не любым "не".

    Чисто лексическая эвристика (как и остальной priority scoring
    в этом модуле) — не LLM, не epistemic вывод.
    """
    lower = (claim_text or "").lower()

    return any(
        re.search(marker, lower)
        for marker in _ABSENCE_MARKERS
    )


# ============================================================
# CLAIM ROLE / DECISION RELEVANCE (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md, §B)
# ============================================================
#
# Аудит показал: embedding topic similarity (P0.1) награждает
# claims, ТЕМАТИЧЕСКИ близкие к query ("отсутствие жидкой воды"
# близко к "есть ли жизнь" по теме обитаемости), но НЕ отличает
# их от claims, чей truth-value НЕПОСРЕДСТВЕННО меняет ответ на
# исходный вопрос.
#
# Это ОТДЕЛЬНЫЙ от topic similarity сигнал: decision relevance.
# Полностью детерминированный, без LLM per claim, без хардкода
# конкретного предмета (Юпитер/жизнь) — только структура
# existence-вопроса ("есть ли X", "существует ли X", ...) и
# лексическое перекрытие claim с извлечённым из query "target".
#
# ВАЖНО: применяется ТОЛЬКО когда query распознан как
# existence-question. Для "Почему...", "Расскажи...", "Какие
# условия..." role остаётся None — ширина retrieval должна
# сохраняться для этих типов вопросов (см. отчёт, часть C).
_EXISTENCE_QUESTION_PATTERN = re.compile(
    r"(?:есть\s+ли|существует\s+ли|имеется\s+ли|"
    r"обнаружен[аоы]?\s+ли|найден[аоы]?\s+ли|"
    r"зафиксирован[аоы]?\s+ли)",
    re.IGNORECASE,
)

_EXISTENCE_TARGET_PATTERN = re.compile(
    r"(?:есть\s+ли|существует\s+ли|имеется\s+ли|"
    r"обнаружен[аоы]?\s+ли|найден[аоы]?\s+ли|"
    r"зафиксирован[аоы]?\s+ли)\s+(.+?)"
    r"(?:\s+(?:на|в|у|при|под|около|близ|для)\s+|[?.!]|$)",
    re.IGNORECASE,
)

_TARGET_STOPWORDS = {
    "какие", "какой", "какая", "какие-то", "какая-то", "какой-то",
    "хоть", "вообще", "действительно", "точно",
}

# Позитивная (не отрицательная) констатация обнаружения/оценки —
# отдельно от _ABSENCE_MARKERS, т.к. существование может быть
# и подтверждено, и опровергнуто, и оценено как маловероятное.
_EXISTENCE_ASSERTION_MARKERS = _ABSENCE_MARKERS + (
    r"\bобнаружен[аоы]?\b",
    r"\bнайден[аоы]?\b",
    r"\bзафиксирован[аоы]?\b",
    r"\bвыявлен[аоы]?\b",
    r"\bподтвержд[её]н[аоы]?\b",
    r"\bустановлен[аоы]?\b",
    r"маловероятн",
    r"крайне\s+невероятн",
    r"считается\s+(?:маловероятн|невозможн|возможн)",
)

# Claims про ИНСТРУМЕНТЫ/МЕТОДЫ обнаружения — отдельная категория
# DIRECT_DECISION_EVIDENCE (напр. "ни один телескоп не обнаружил...").
_EVIDENCE_INSTRUMENT_MARKERS = (
    "телескоп", "зонд", "аппарат", "сигнал", "сигнатур",
    "спектр", "наблюдени", "радар", "датчик",
    # YANDI_CLAIM_ROLE_MORPHOLOGY_FIX.md, BUG 2: "миссия" (6 симв.)
    # не покрывает "миссии" (5-й символ различается: я vs и).
    # Общий корень короче.
    "мисси",
)


def _is_existence_question(query: str) -> bool:
    """Query имеет структуру "есть ли X" — распознаётся без учёта предмета."""
    return bool(_EXISTENCE_QUESTION_PATTERN.search(query or ""))


def _extract_existence_target(query: str) -> List[str]:
    """
    Извлечь слова, описывающие ЧТО именно спрашивается на
    существование (напр. "разумная жизнь" из "Есть ли разумная
    жизнь на Юпитере?").

    Эвристика, не полноценный parser: берёт фразу между "ли" и
    следующим предлогом места/цели или концом предложения.
    Достаточно для сравнения claim-текстов лексическим
    перекрытием — большего здесь не требуется.
    """
    query = (query or "").strip()

    m = _EXISTENCE_TARGET_PATTERN.search(query)

    if not m:
        return []

    phrase = m.group(1)

    words = [
        w.lower()
        for w in re.findall(r"[A-Za-zА-Яа-яЁё-]+", phrase)
        if len(w) >= 4 and w.lower() not in _TARGET_STOPWORDS
    ]

    return words


def _target_overlap(claim_lower: str, target_words: List[str]) -> bool:
    """
    Claim лексически упоминает existence-target.

    YANDI_CLAIM_ROLE_MORPHOLOGY_FIX.md: фиксированный стем в 4
    символа ломался на коротких словах — "вода" (4 симв.) целиком
    становился "стемом" и не совпадал с "воды"/"водой" (различие
    уже в последней букве). Правило теперь: отбросить последние
    ~2 символа (обычно падежное окончание), но не короче 3 —
    работает и для 4-буквенных, и для длинных слов.
    """
    return any(
        word[:max(3, len(word) - 2)] in claim_lower
        for word in target_words
        if len(word) >= 4
    )


def _classify_claim_role(
    claim_text: str,
    query: str,
) -> Dict[str, Any]:
    """
    Роль claim относительно existence-вопроса.

    Возвращает dict с полями role/target_match/has_assertion/
    has_instrument — для diagnostics (§F) и для priority (§B).

    role одно из:
        CORE                    — прямо утверждает/отрицает target
        DIRECT_DECISION_EVIDENCE — про инструмент/метод обнаружения
        EXPLANATORY             — упоминает target, но не как assertion
        BACKGROUND              — не про target вообще
        None                    — query не existence-question,
                                   role-логика не применяется
    """
    if not _is_existence_question(query):
        return {
            "role": None,
            "target_match": False,
            "has_assertion": False,
            "has_instrument": False,
        }

    target_words = _extract_existence_target(query)
    lower = (claim_text or "").lower()

    target_match = _target_overlap(lower, target_words)

    has_assertion = any(
        re.search(marker, lower)
        for marker in _EXISTENCE_ASSERTION_MARKERS
    )

    has_instrument = any(
        marker in lower
        for marker in _EVIDENCE_INSTRUMENT_MARKERS
    )

    # YANDI_CLAIM_ROLE_MORPHOLOGY_FIX.md, BUG 2: DIRECT_DECISION_EVIDENCE
    # проверяется ПЕРВЫМ, до общего CORE. Claim вида "Телескопические
    # наблюдения не зафиксировали..." одновременно удовлетворяет и
    # CORE-условию (target_match+assertion), и instrument-условию —
    # но он СЕМАНТИЧЕСКИ более специфичен: называет конкретный метод/
    # источник наблюдения, а не просто утверждает факт. Порядок
    # проверки определяет специфичность категории, не её "силу"
    # (сами role boost значения не менялись).
    if has_instrument and (target_match or has_assertion):
        role = "DIRECT_DECISION_EVIDENCE"
    elif target_match and has_assertion:
        role = "CORE"
    elif target_match:
        role = "EXPLANATORY"
    else:
        role = "BACKGROUND"

    return {
        "role": role,
        "target_match": target_match,
        "has_assertion": has_assertion,
        "has_instrument": has_instrument,
    }


_CLAIM_ROLE_BOOST = {
    "CORE": 6.0,
    "DIRECT_DECISION_EVIDENCE": 4.0,
    "EXPLANATORY": 0.0,
    "BACKGROUND": 0.0,
}


def _claim_retrieval_priority(
    claim: Dict[str, Any],
) -> float:
    """
    Приоритет claim для ограниченного second-pass retrieval.

    Это НЕ truth score и НЕ confidence.

    Функция отвечает только на вопрос:
        какой claim разумнее проверять раньше,
        если latency budget позволяет проверить не все.

    Формула (после YANDI_RUNTIME_REGRESSION_FIX_REPORT.md, §B)
    состоит из компонент:

        query relevance (topic similarity, embedding)
            + lexical specificity (tie-breaker)
            + claim role boost (decision relevance — ОТДЕЛЬНЫЙ
              от topic similarity сигнал; см. _classify_claim_role)

    ВАЖНО: topic similarity и decision relevance — РАЗНЫЕ вещи.
    "Отсутствие жидкой воды" тематически близко к "есть ли жизнь"
    (обитаемость), но не является прямым ответом на вопрос —
    role boost применяется только когда claim лексически
    пересекается с extracted existence-target ("жизнь"), а не
    просто с общей темой query.
    """

    if not isinstance(claim, dict):
        return -1000.0

    text = (
        claim.get("claim_text", "")
        or ""
    ).strip()

    if not text:
        return -1000.0

    lower = text.lower()

    claim_type = (
        claim.get("claim_type", "factual")
        or "factual"
    ).lower()

    score = 0.0

    # --------------------------------------------------------
    # QUERY RELEVANCE (P0.1) — topic similarity, НЕ decision
    # relevance (см. docstring выше).
    # --------------------------------------------------------
    RELEVANCE_WEIGHT = 8.0

    query_context = _resolve_query_context(claim)

    relevance = _query_relevance_score(
        text,
        query_context,
    )

    relevance_component = relevance * RELEVANCE_WEIGHT
    score += relevance_component

    # --------------------------------------------------------
    # Epistemic type
    # --------------------------------------------------------

    if claim_type == "factual":
        score += 5.0
    elif claim_type == "hypothesis":
        score += 1.5
    else:
        score += 0.5

    # --------------------------------------------------------
    # Concrete / externally checkable form
    # --------------------------------------------------------

    concrete_patterns = (
        r"\d",
        r"\b(?:давлен|температур|атмосфер|поверхност)",
        r"\b(?:водород|гелий|метан|аммиак)",
        r"\b(?:радиац|шторм|магнит|металлическ)",
        r"\b(?:обнаружен|измерен|составляет|достигает)",
    )

    specificity_component = 0.0

    for pattern in concrete_patterns:
        if re.search(
            pattern,
            lower,
            flags=re.IGNORECASE,
        ):
            specificity_component += 0.7

    # Явный предметный субъект полезнее обезличенного текста.
    if "юпитер" in lower:
        specificity_component += 1.0

    score += specificity_component

    # --------------------------------------------------------
    # CLAIM ROLE / DECISION RELEVANCE BOOST (§B)
    # --------------------------------------------------------
    #
    # Заменяет прежний блanket ABSENCE-CLAIM BOOST (P0.2): тот
    # поднимал ЛЮБОЙ claim вида "не найдено/отсутствует", даже
    # если он не про то, что реально спрашивает query (пример из
    # отчёта: "отсутствует жидкая вода" получал boost наравне с
    # "жизнь не обнаружена"). Роль требует ещё и совпадения с
    # extracted existence-target, а не просто формы отрицания.
    # D: supports_query_aspect теперь несёт ту же роль, вычисленную
    # один раз при construction claim в orch_synthesizer.py. Если
    # она уже есть и валидна — переиспользуем (не пересчитываем),
    # иначе считаем локально (fallback для claims из других
    # источников, не прошедших orch_synthesizer).
    _existing_aspect = claim.get("supports_query_aspect") or []
    _existing_role = (
        _existing_aspect[0]
        if _existing_aspect and _existing_aspect[0] in _CLAIM_ROLE_BOOST
        else None
    )

    if _existing_role is not None:
        # P0-A (autonomous fix pass): observability fix, not a second
        # classifier. orch_synthesizer.py now persists the full
        # _classify_claim_role() output (not just the role label) on
        # the claim as "_role_classification" — reuse it here instead
        # of hardcoding None for claims that carry it. Claims from any
        # other source (that never went through that construction
        # path) still fall back to None, exactly as before.
        _stored_info = claim.get("_role_classification")

        if isinstance(_stored_info, dict):
            role_info = {
                "role": _existing_role,
                "target_match": _stored_info.get("target_match"),
                "has_assertion": _stored_info.get("has_assertion"),
                "has_instrument": _stored_info.get("has_instrument"),
            }
        else:
            role_info = {
                "role": _existing_role,
                "target_match": None,
                "has_assertion": None,
                "has_instrument": None,
            }
        role = _existing_role
    else:
        role_info = _classify_claim_role(text, query_context)
        role = role_info["role"]

    role_boost = _CLAIM_ROLE_BOOST.get(role, 0.0) if role else 0.0
    score += role_boost

    # --------------------------------------------------------
    # Retrieval-cost penalties
    # --------------------------------------------------------

    # Source/meta wrappers дают плохие поисковые запросы.
    source_meta_prefixes = (
        "согласно современным",
        "по имеющейся информации",
        "по имеющимся данным",
        "некоторые источники",
        "ответ на вопрос",
    )

    if lower.startswith(source_meta_prefixes):
        score -= 6.0

    # Спекулятивные claims сохраняем, но проверяем после
    # конкретных наблюдаемых утверждений.
    speculative_markers = (
        "гипотетическ",
        "некоторые гипотезы",
        "экзотических форм",
        "теоретическим предположением",
    )

    if any(
        marker in lower
        for marker in speculative_markers
    ):
        score -= 2.0

    # --------------------------------------------------------
    # DIAGNOSTIC (§F) — компактно, без полного текста claim.
    # --------------------------------------------------------
    if role_boost > 0:
        _reason = f"role={role} boost dominant"
    elif relevance_component >= specificity_component:
        _reason = "topic_similarity dominant"
    else:
        _reason = "lexical specificity dominant"

    print(
        f"[Claim Retrieval Priority] "
        f"claim_id={claim.get('claim_id', 'unknown')} "
        f"role={role or '-'} "
        f"topic_similarity={relevance:.3f} "
        f"decision_relevance={role_boost:.1f} "
        f"specificity={specificity_component:.2f} "
        f"absence={_is_absence_claim(text)} "
        f"target_match={role_info['target_match']} "
        f"final={score:.2f} "
        f"reason={_reason}"
    )

    return score


def retrieve_for_claims(
    claims: List[Dict[str, Any]],
    fetch_cache: Optional["SharedFetchCache"] = None,
) -> List[Dict[str, Any]]:
    """
    Claim-specific retrieval для ограниченного числа claims.

    Выбирает до MAX_CLAIMS наиболее полезных для проверки
    factual/hypothesis claims.

    Selection является latency/cost gate и НЕ меняет truth/confidence.
    """

    if not claims:
        return []

    eligible_claims = []

    retrieval_eligible_types = {
        "factual",
        "hypothesis",
    }

    for original_index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue

        claim_text = (
            claim.get("claim_text", "")
            or ""
        ).strip()

        if not claim_text:
            continue

        claim_type = (
            claim.get("claim_type", "factual")
            or "factual"
        ).lower()

        if claim_type not in retrieval_eligible_types:
            print(
                f"[Claim Retrieval Select] "
                f"SKIP id={claim.get('claim_id', 'unknown')} "
                f"reason=claim_type:{claim_type!r}"
            )
            continue

        priority = _claim_retrieval_priority(
            claim
        )

        eligible_claims.append(
            (
                priority,
                original_index,
                claim,
            )
        )

    # Лучшие epistemically useful claims идут первыми.
    #
    # При одинаковом priority сохраняем исходный порядок,
    # чтобы selection оставался стабильным.
    eligible_claims.sort(
        key=lambda item: (
            -item[0],
            item[1],
        )
    )

    selected = [
        item[2]
        for item in eligible_claims[:MAX_CLAIMS]
    ]

    for rank, (
        priority,
        original_index,
        claim,
    ) in enumerate(
        eligible_claims,
        1,
    ):
        selected_flag = (
            rank <= MAX_CLAIMS
        )

        print(
            f"[Claim Retrieval Select] "
            f"rank={rank} "
            f"selected={selected_flag} "
            f"priority={priority:.2f} "
            f"id={claim.get('claim_id', 'unknown')} "
            f"type={claim.get('claim_type', 'factual')!r} "
            f"text={claim.get('claim_text', '')[:110]}"
        )

    print(
        f"[Claim Retrieval] batch_input={len(claims)} "
        f"selected={len(selected)} "
        f"max_claims={MAX_CLAIMS}"
    )

    all_evidence = []

    # Claim-specific evidence принадлежит конкретному retrieval.
    # Один URL может законно быть evidence для нескольких claims.
    #
    # Поэтому dedup выполняется по:
    #
    #     (claim_id, URL)
    #
    # а не глобально только по URL.
    seen_claim_urls = set()

    # ========================================================
    # PARALLEL CLAIM-SPECIFIC RETRIEVAL
    # ========================================================
    #
    # Раньше claims обрабатывались строго последовательно:
    #
    #   claim1 -> query -> scrape -> filtering
    #   claim2 -> query -> scrape -> filtering
    #   ...
    #
    # Это сильно увеличивает wall-clock latency.
    #
    # Ограничиваем concurrency двумя workers:
    # - web I/O частично перекрывается;
    # - Ollama не получает лавину одновременных generation calls.
    # --------------------------------------------------------

    retrieval_started = time.time()

    # P0 (performance architecture pass): ONE fetch cache shared
    # across ALL claim workers for this call — a URL discovered
    # independently by two different claims' search queries gets
    # physically fetched only once. Request-scoped: created fresh
    # here, per retrieve_for_claims() call, never persisted across
    # separate user queries. See SharedFetchCache docstring for the
    # epistemic-ownership invariant this preserves.
    #
    # Refutation performance audit: callers that already have a
    # request-scoped cache (main web scrape + refutation scrape) can
    # pass it in here too, so a URL discovered by main web/refutation
    # AND independently needed by claim-specific retrieval is also
    # only fetched once. Falls back to a fresh instance when not
    # provided (e.g. standalone/test callers) — same as scrape().
    shared_fetch_cache = fetch_cache if fetch_cache is not None else SharedFetchCache()

    # P1 (performance architecture pass): precompute search queries
    # for ALL selected claims via batched LLM calls (QUERY_BATCH_SIZE
    # claims per call) instead of one call per claim. Verified via a
    # live offline experiment before shipping (7 claims across 3
    # domains: Jupiter temperature/life/magnetic-field, Mars, Russian
    # history, math, biology) at batch sizes 2/4/8: query ownership
    # 100%, ZERO cross-claim subject-anchor leakage, epistemic
    # modality (negation) preserved in every single case. batch_size=4
    # chosen over 8 to keep a single failed/truncated batch call's
    # blast radius smaller (at most 4 claims fall back to individual
    # calls, not all MAX_CLAIMS). formulate_claim_evidence_queries_
    # batch() already guarantees every claim_id gets a bounded,
    # diagnosed per-claim fallback if the batch response is missing it
    # — this precompute step can never leave a claim without queries.
    _query_batch_input = [
        {
            "claim_id": c.get("claim_id", "unknown"),
            "claim_text": _build_contextual_claim_text(
                c, (c.get("claim_text") or "").strip()
            ),
        }
        for c in selected
        if (c.get("claim_text") or "").strip()
    ]

    precomputed_queries: Dict[str, Any] = {}

    for _i in range(0, len(_query_batch_input), QUERY_BATCH_SIZE):
        _chunk = _query_batch_input[_i:_i + QUERY_BATCH_SIZE]
        precomputed_queries.update(
            formulate_claim_evidence_queries_batch(_chunk)
        )

    def _retrieve_one(claim, submitted_at):
        claim_id = claim.get(
            "claim_id",
            "unknown",
        )

        started = time.time()

        # P1 (autonomous fix pass): with max_workers=3 and up to
        # MAX_CLAIMS selected claims, tasks beyond the first 3 sit
        # queued inside the ThreadPoolExecutor until a worker frees up.
        # queue_wait makes that visible instead of it being silently
        # folded into the per-claim "time=" number.
        queue_wait = started - submitted_at

        print(
            f"[Claim Retrieval Worker] "
            f"START claim={claim_id} "
            f"queue_wait={queue_wait:.2f}s "
            f"text={claim.get('claim_text', '')[:100]}"
        )

        try:
            records = retrieve_claim_evidence(
                claim,
                fetch_cache=shared_fetch_cache,
                precomputed_query_result=precomputed_queries.get(claim_id),
            )
            error = None
        except Exception as exc:
            records = []
            error = str(exc)[:300]

        elapsed = time.time() - started

        print(
            f"[Claim Retrieval Worker] "
            f"DONE claim={claim_id} "
            f"records={len(records)} "
            f"queue_wait={queue_wait:.2f}s "
            f"time={elapsed:.2f}s"
            + (
                f" error={error}"
                if error
                else ""
            )
        )

        return claim, records, elapsed, error, queue_wait

    worker_results = []

    max_workers = min(
        3,
        len(selected),
    )

    if max_workers > 0:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers
        ) as executor:

            _submitted_at = time.time()

            future_map = {
                executor.submit(
                    _retrieve_one,
                    claim,
                    _submitted_at,
                ): claim
                for claim in selected
            }

            for future in concurrent.futures.as_completed(
                future_map
            ):
                try:
                    worker_results.append(
                        future.result()
                    )
                except Exception as exc:
                    claim = future_map[future]

                    worker_results.append((
                        claim,
                        [],
                        0.0,
                        str(exc)[:300],
                        0.0,
                    ))

    # Сохраняем deterministic merge order согласно selected.
    result_by_claim_id = {
        (
            item[0].get(
                "claim_id",
                "unknown",
            )
        ): item
        for item in worker_results
    }

    per_claim_times = []
    per_claim_queue_waits = []

    for claim in selected:
        claim_id = claim.get(
            "claim_id",
            "unknown",
        )

        item = result_by_claim_id.get(
            claim_id
        )

        if item is None:
            records = []
            elapsed = 0.0
            queue_wait = 0.0
        else:
            _, records, elapsed, _, queue_wait = item

        per_claim_times.append(
            elapsed
        )
        per_claim_queue_waits.append(
            queue_wait
        )

        added = 0

        for record in records:
            url = record.get(
                "source_uri",
                "",
            )

            # Claim-aware dedup второго pass.
            dedup_key = (
                record.get(
                    "retrieval_claim_id",
                    claim_id,
                ),
                url,
            )

            if (
                url
                and dedup_key
                in seen_claim_urls
            ):
                continue

            if url:
                seen_claim_urls.add(
                    dedup_key
                )

            all_evidence.append(record)
            added += 1

        print(
            f"[Claim Retrieval] "
            f"claim={claim_id} "
            f"candidates={added}"
        )

    retrieval_elapsed = (
        time.time()
        - retrieval_started
    )

    _worker_sum = sum(per_claim_times)

    # P1 (autonomous fix pass): effective_parallelism close to
    # `workers` means concurrency is actually being used; close to 1.0
    # despite workers=3 would mean something (queue contention,
    # GENERATION_SEMAPHORE, or an actually-serialized dependency) is
    # eating the concurrency — proves it with a number instead of
    # guessing from wall/worker_sum by hand.
    _effective_parallelism = (
        _worker_sum / retrieval_elapsed
        if retrieval_elapsed > 0
        else 0.0
    )

    print(
        f"[Claim Retrieval Timing] "
        f"claims={len(selected)} "
        f"workers={max_workers} "
        f"wall={retrieval_elapsed:.2f}s "
        f"worker_sum={_worker_sum:.2f}s "
        f"worker_max={max(per_claim_times, default=0.0):.2f}s "
        f"effective_parallelism={_effective_parallelism:.2f} "
        f"queue_wait_sum={sum(per_claim_queue_waits):.2f}s "
        f"queue_wait_max={max(per_claim_queue_waits, default=0.0):.2f}s"
    )

    _fc = shared_fetch_cache.summary()
    print(
        f"[Shared Fetch Cache] "
        f"requests={_fc['requests']} "
        f"unique={_fc['unique']} "
        f"hits={_fc['hits']} "
        f"inflight_waits={_fc['inflight_waits']} "
        f"network_fetches={_fc['network_fetches']} "
        f"saved={_fc['saved']} "
        f"hit_ratio={_fc['hit_ratio']:.2f}"
    )

    direct_count = sum(
        1
        for ev in all_evidence
        if ev.get("evidence_role") == "direct"
    )
    context_count = sum(
        1
        for ev in all_evidence
        if ev.get("evidence_role") == "context"
    )
    eligible_count = sum(
        1
        for ev in all_evidence
        if ev.get("evidence_eligible") is True
    )

    print(
        f"[Claim Retrieval] batch_return={len(all_evidence)} "
        f"direct={direct_count} "
        f"context={context_count} "
        f"eligible={eligible_count}"
    )

    return all_evidence


if __name__ == "__main__":
    # Только query-generation probe.
    #
    # Сам web retrieval специально здесь не запускаем,
    # чтобы unit probe не зависел от сети.

    test_claims = [
        "На Юпитере разумная жизнь не обнаружена.",
        "Юпитер является газовым гигантом.",
    ]

    for text in test_claims:
        result = formulate_claim_evidence_queries(text)

        print()
        print("CLAIM:", text)

        for i, query in enumerate(
            result.queries,
            1,
        ):
            print(f"{i}. {query}")
