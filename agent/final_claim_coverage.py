"""
agent/final_claim_coverage.py

Контроль покрытия фактических утверждений финального ответа.

Задача:

    final_answer
        ↓
    extract factual claims
        ↓
    compare with pipeline claims
        ↓
    claim_coverage_score

ВАЖНО:

- coverage != truth;
- coverage != grounding;
- coverage != support;
- модуль НЕ повышает Trust;
- модуль НЕ назначает supports/contradicts;
- модуль отвечает только на вопрос:

    "Все ли проверяемые утверждения, которые YANDI
     говорит пользователю, вообще прошли claim lifecycle?"
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

import requests as _requests

from agent.orch_config import (
    OLLAMA_BASE,
    MODEL,
    TEMP_ANALYST,
    FINAL_CLAIM_EXTRACTION_MAX_TOKENS,
    GENERATION_SEMAPHORE,
)
from agent.claim_relation import (
    infer_claim_relation,
    infer_claim_relations_batch,
)

_session = _requests.Session()
_session.trust_env = False

_EXTRACTION_TIMEOUT = 180  # same order as orch_synthesizer's analyst-role calls


def _call_ollama_for_extraction(prompt: str) -> Dict[str, Any]:
    """
    P0-B: dedicated Ollama call for extract_final_claims() — deliberately
    NOT the shared orch_web_query._call_ollama(), whose num_predict is
    hardcoded to MAX_TOKENS_CONDUCTOR (500, sized for short 2-3-query
    formulation, not for a variable-size JSON array of every claim in a
    final answer). Uses its own FINAL_CLAIM_EXTRACTION_MAX_TOKENS budget
    without touching the web-query conductor's budget at all.

    Returns metadata (done_reason/eval_count) alongside the text so the
    caller can tell a token-limit cutoff apart from a genuine formatting
    failure, instead of collapsing both into the same generic parse
    error.
    """
    _wait_started = time.time()

    with GENERATION_SEMAPHORE:
        _waited = time.time() - _wait_started

        if _waited > 0.05:
            print(
                f"[Final Claim Extraction LLM] generation queue wait={_waited:.2f}s"
            )

        resp = _session.post(
            f"{OLLAMA_BASE}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": TEMP_ANALYST,
                    "num_predict": FINAL_CLAIM_EXTRACTION_MAX_TOKENS,
                },
            },
            timeout=_EXTRACTION_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

    return {
        "response": (data.get("response") or "").strip(),
        "done_reason": data.get("done_reason"),
        "eval_count": data.get("eval_count"),
        "num_predict": FINAL_CLAIM_EXTRACTION_MAX_TOKENS,
    }


def _json_completeness_indicator(text: str) -> str:
    """
    P0-B: cheap heuristic, NOT a JSON validator (json.loads already does
    real validation) — just a fast diagnostic signal for whether the raw
    response looks like a balanced, closed top-level object.

    "Ends with `}`" alone is not enough: truncated output that stops
    right after the LAST array item's own closing `}` (missing the
    outer array's `]` and the outer object's `}`) also ends with `}` —
    brace/bracket counts must additionally balance. Still approximate
    (doesn't account for braces inside string literals) — good enough
    for a diagnostic tag, not a claim of correctness.
    """
    if not text:
        return "empty"

    stripped = text.strip()

    if stripped.endswith("```"):
        stripped = stripped[:-3].rstrip()

    if not stripped.endswith("}"):
        return "incomplete"

    balanced = (
        stripped.count("{") == stripped.count("}")
        and stripped.count("[") == stripped.count("]")
    )

    return "complete" if balanced else "incomplete"


@dataclass
class FinalClaimCoverageResult:
    final_claims: List[Dict[str, Any]] = field(default_factory=list)
    covered_claims: List[Dict[str, Any]] = field(default_factory=list)
    uncovered_claims: List[Dict[str, Any]] = field(default_factory=list)

    factual_count: int = 0
    covered_count: int = 0
    coverage_score: float = 0.0

    # P0-B (YANDI_FINAL_EPISTEMIC_AUDIT_AND_FIX.md): раньше "0 factual
    # claims" всегда означало coverage_score=1.0 — независимо от того,
    # действительно ли ответ не содержит фактов, или extraction молча
    # упала (LLM call error / malformed JSON / отсутствие ключа "claims").
    # Это поле различает эти случаи, НЕ трогая саму Trust-формулу —
    # coverage_score остаётся числом, потребители downstream не меняются.
    #
    #   "ok"                 — извлечение реально успешно (claims могут
    #                          быть пустыми, если ответ действительно
    #                          нефактический, напр. "Я не знаю.");
    #   "call_error"         — LLM-вызов упал исключением;
    #   "parse_error"        — ответ модели не распарсился как ожидаемый
    #                          JSON с ключом "claims";
    #   "suspicious_empty"   — статус "ok", но 0 claims извлечено из
    #                          заметно длинного ответа — подозрительно,
    #                          не доверяем слепо;
    #   "no_factual_content" — статус "ok", 0 claims, короткий ответ —
    #                          похоже на добросовестный пустой результат.
    coverage_status: str = "ok"


# ============================================================
# CANDIDATE ROUTING (P0 follow-up, per explicit user decision)
# ============================================================
#
# THIS IS A ROUTING LAYER, NOT AN EPISTEMIC DECISION.
#
# It answers ONLY: "which (final_claim, pipeline_claim) pairs are
# worth sending to the expensive NLI batch call?" It NEVER assigns
# supports/contradicts/unrelated itself — a pair not selected here is
# simply never given an NLI pair_id, which downstream already treats
# as "no relation found" (relation_by_id.get(...) -> {} -> no match),
# identical to how a genuinely-checked-and-unrelated pair already
# behaves. See NO_NLI_CANDIDATES handling in evaluate_final_claim_
# coverage() for why that distinction still matters for diagnostics.
#
# Numbers below (threshold, top-K) are NOT invented — they come from
# an offline recall experiment (scratchpad, not committed — the
# corpus itself is real: live-run claim texts from two actual
# orchestrator runs plus explicit adversarial pairs across 3 domains
# — Jupiter/life, Mars/water, Higgs boson — to rule out domain-
# specific tuning) that computed REAL embedding similarity (live
# embeddinggemma) against REAL ground-truth NLI relations (live
# infer_claim_relation(), not guessed) for 29 pairs across 8 families
# shaped like production (one final claim vs several pipeline claims):
#
#   threshold=0.45 alone: supports_recall=1.00, contradicts_recall=1.00
#     (29-pair corpus), ~31% pair reduction.
#   Per-family top-K: the true supports/contradicts pair was NEVER
#     ranked worse than 3rd by cosine similarity within its family
#     (worst case: a contradicts pair narrowly edged out for 2nd place
#     by an uncertain pair, 0.717 vs 0.730 similarity — a 0.013 margin,
#     not a wide safety gap).
#
# COVERAGE_ROUTING_TOP_K=5 adds a 2-slot margin above that observed
# worst case (rank 3), because production pipeline_claims counts
# (13-20 in the two live runs) are larger than this corpus's families
# (3-6), so a similar "narrowly edged out" case could plausibly rank
# slightly lower with more candidates competing for top slots.
COVERAGE_ROUTING_SIM_THRESHOLD = 0.45
COVERAGE_ROUTING_TOP_K = 5


def _content_words(text: str) -> set:
    stopwords = {
        "что", "это", "как", "для", "на", "в", "с", "по", "из", "от", "до",
        "за", "у", "о", "к", "и", "а", "но", "или", "же", "бы", "не", "да",
        "нет", "кто", "какой", "какая", "какие", "его", "её", "их", "быть",
        "уже", "также", "при", "об", "то", "если", "только", "все", "всё",
    }
    return {
        w for w in re.findall(r"[а-яёa-z0-9]+", (text or "").lower())
        if len(w) >= 4 and w not in stopwords
    }


def _lexical_overlap(a: str, b: str) -> float:
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


_NEGATION_MARKERS_RE = re.compile(
    r"\bне{1,2}[а-яё]*\b|\bнет\b|\bотсутств|\bникак|\bни\s+один",
    re.IGNORECASE,
)


def _has_negation(text: str) -> bool:
    return bool(_NEGATION_MARKERS_RE.search((text or "").lower()))


def _shares_number(a: str, b: str) -> bool:
    na = set(re.findall(r"\d+(?:[.,]\d+)?", a or ""))
    nb = set(re.findall(r"\d+(?:[.,]\d+)?", b or ""))
    return bool(na & nb)


def _is_near_duplicate(a: str, b: str) -> bool:
    a_norm, b_norm = (a or "").strip().lower(), (b or "").strip().lower()
    if a_norm == b_norm:
        return True
    return _lexical_overlap(a, b) >= 0.8


# Same "moderate shared content" bar for both mandatory rules below —
# not a new number: matches CLAIM_CONFLICT_SIM_THRESHOLD's role
# elsewhere in this codebase (a low bar meant to catch "same rough
# topic", not "same claim") translated to lexical terms since this
# check is cheaper than an extra embedding call per pair.
_MANDATORY_OVERLAP_FLOOR = 0.15


def _mandatory_routing_reason(
    final_text: str,
    pipeline_text: str,
    final_role: "str | None" = None,
    pipeline_role: "str | None" = None,
) -> "str | None":
    """
    Returns a short reason string if this pair MUST be sent to NLI
    regardless of embedding similarity/top-K, else None.

    Deliberately conservative in the "include" direction — every rule
    here is a cheap, domain-generic heuristic for "this pair might be
    a real supports/contradicts relation", never a judgment about
    truth or relation itself. Over-including costs a bit of NLI time;
    under-including risks recall, which is the one thing this layer is
    not allowed to trade away.
    """
    if _is_near_duplicate(final_text, pipeline_text):
        return "exact_or_near_duplicate"

    overlap = _lexical_overlap(final_text, pipeline_text)

    if overlap >= _MANDATORY_OVERLAP_FLOOR:
        if _has_negation(final_text) or _has_negation(pipeline_text):
            return "negation_plus_overlap"

        if _shares_number(final_text, pipeline_text):
            return "shared_number_plus_overlap"

    if final_role == "CORE" and pipeline_role == "CORE":
        return "core_plus_core"

    return None


def _embed_texts_batch(texts: "List[str]") -> "Dict[str, Any]":
    """
    Batched, live embedding lookup — same pattern as the
    extract_claim_from_source fix (one HTTP round-trip for N texts,
    not N). Returns {text: normalized_vector}. Graceful degradation:
    on any failure, returns {} — callers must treat a missing text as
    "similarity unknown", never invent a similarity score.
    """
    import numpy as np

    unique_texts = list(dict.fromkeys(t for t in texts if t))

    if not unique_texts:
        return {}

    try:
        resp = _session.post(
            f"{OLLAMA_BASE}/api/embed",
            json={
                "model": "embeddinggemma:latest",
                "input": [t[:2000] for t in unique_texts],
            },
            timeout=60,
        )
        resp.raise_for_status()

        vecs = np.array(resp.json()["embeddings"], dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vecs = vecs / norms

        return {t: vecs[i] for i, t in enumerate(unique_texts)}

    except Exception as e:
        print(f"[Candidate Routing] embedding batch failed, degrading to mandatory-only: {e}")
        return {}


def _route_candidate_pairs(
    final_claims_text: "List[str]",
    pipeline_claims_text: "List[str]",
    query: str = "",
) -> "tuple[Dict[int, Dict[int, str]], Dict[str, int]]":
    """
    High-recall candidate ROUTING for the final<->pipeline NLI step.

    Returns (routing, stats):
      routing[final_index][pipeline_index] = reason string
        ("top_k" / "threshold" / one of the mandatory reasons)
      stats = counters for the [Candidate Routing] diagnostic log.

    NEVER returns a relation. NEVER marks anything unrelated/supports/
    contradicts. A pipeline_index absent from routing[final_index]
    means only "not selected for NLI this pass", tracked downstream as
    NOT_SELECTED_FOR_NLI — never as a proxy for UNRELATED.
    """
    import numpy as np

    routing: "Dict[int, Dict[int, str]]" = {
        i: {} for i in range(len(final_claims_text))
    }

    stats = {
        "mandatory": 0,
        "top_k": 0,
        "threshold": 0,
    }

    if not final_claims_text or not pipeline_claims_text:
        return routing, stats

    existence_query = bool(query) and _is_existence_question_safe(query)

    final_roles = [
        _classify_claim_role_safe(t, query) if existence_query else None
        for t in final_claims_text
    ]
    pipeline_roles = [
        _classify_claim_role_safe(t, query) if existence_query else None
        for t in pipeline_claims_text
    ]

    vec_by_text = _embed_texts_batch(final_claims_text + pipeline_claims_text)

    for fi, final_text in enumerate(final_claims_text):
        sims = None

        if vec_by_text:
            fv = vec_by_text.get(final_text)
            if fv is not None:
                sims = [
                    float(np.dot(fv, vec_by_text[pt]))
                    if pt in vec_by_text else float("-inf")
                    for pt in pipeline_claims_text
                ]

        if sims is not None:
            ranked = sorted(
                range(len(pipeline_claims_text)),
                key=lambda i: -sims[i],
            )

            for pi in ranked[:COVERAGE_ROUTING_TOP_K]:
                if sims[pi] > float("-inf"):
                    routing[fi].setdefault(pi, "top_k")
                    stats["top_k"] += 1

            for pi, sim in enumerate(sims):
                if sim >= COVERAGE_ROUTING_SIM_THRESHOLD and pi not in routing[fi]:
                    routing[fi][pi] = "threshold"
                    stats["threshold"] += 1

        for pi, pipeline_text in enumerate(pipeline_claims_text):
            reason = _mandatory_routing_reason(
                final_text,
                pipeline_text,
                final_roles[fi],
                pipeline_roles[pi],
            )

            if reason:
                if pi not in routing[fi]:
                    stats["mandatory"] += 1
                routing[fi][pi] = reason

        # Embedding unavailable for this final claim entirely -> fall
        # back to "every pipeline claim is mandatory" rather than
        # silently checking nothing. Soft-fails toward MORE NLI calls,
        # never toward skipping a claim's coverage check outright.
        if sims is None and not routing[fi]:
            for pi in range(len(pipeline_claims_text)):
                routing[fi][pi] = "embedding_unavailable_fallback"
                stats["mandatory"] += 1

    return routing, stats


def _is_existence_question_safe(query: str) -> bool:
    try:
        from agent.claim_evidence_retriever import _is_existence_question
        return _is_existence_question(query)
    except Exception:
        return False


def _classify_claim_role_safe(text: str, query: str) -> "str | None":
    try:
        from agent.claim_evidence_retriever import _classify_claim_role
        return _classify_claim_role(text, query)["role"]
    except Exception:
        return None


def _extract_json(text: str) -> dict:
    """
    P1-C (YANDI_EVIDENCE_ELIGIBILITY_AND_REGISTRY_AUDIT.md): расширено
    только распространёнными, конкретно наблюдаемыми паттернами LLM-
    форматирования — НЕ полноценный JSON5/lenient-parser. Каждый шаг —
    попытка, не гарантия; на любой неудаче падаем в следующий шаг, а
    в конце — в {} как и раньше (поведение при полном провале не
    изменилось).
    """
    if not text:
        return {}

    text = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL,
    ).strip()

    # Шаг 1: снять markdown code fence (```json ... ``` или ``` ... ```),
    # частый паттерн — модель оборачивает JSON в блок кода, несмотря
    # на инструкцию "верни ТОЛЬКО JSON".
    fence_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        text,
        re.DOTALL,
    )

    candidates = [text]

    if fence_match:
        candidates.insert(0, fence_match.group(1))

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            pass

    match = re.search(r"\{.*\}", text, re.DOTALL)

    if match:
        candidate = match.group()

        try:
            return json.loads(candidate)
        except Exception:
            pass

        # Шаг 2: убрать trailing commas перед `]`/`}` — частая LLM-
        # ошибка форматирования (не JSON5, просто одна конкретная
        # починка одного конкретного паттерна).
        sanitized = re.sub(r",\s*([\]}])", r"\1", candidate)

        if sanitized != candidate:
            try:
                return json.loads(sanitized)
            except Exception:
                pass

    return {}


def _format_hint(text: str) -> str:
    """
    P1-C: дешёвая диагностика ПОЧЕМУ parse не удался — без хранения/
    печати самого текста целиком. Возвращает короткие теги через "+".
    """
    if not text:
        return "empty"

    hints = []

    if "```" in text:
        hints.append("has_code_fence")

    stripped = text.strip()

    if stripped and not stripped.startswith("{"):
        hints.append("prose_before_json")

    if "{" not in text:
        hints.append("no_brace_found")

    if re.search(r",\s*[\]}]", text):
        hints.append("trailing_comma_suspected")

    if "<think>" in text:
        hints.append("unstripped_think_tag")

    return "+".join(hints) if hints else "unknown_format_issue"


def extract_final_claims(
    answer: str,
) -> "tuple[List[Dict[str, Any]], str]":
    """
    Извлечь атомарные проверяемые factual claims
    непосредственно из текста FINAL ANSWER.

    Не извлекаем:
    - заголовки;
    - методологические оговорки;
    - чистые оценки;
    - рекомендации;
    - meta-текст;
    - ссылки;
    - фразы вроде "по имеющейся информации"
      отдельно от самого утверждения.

    P0-B: возвращает (claims, status) вместо голого списка. Раньше
    LLM call error, malformed JSON и "модель реально не нашла claims"
    были неразличимы — все давали []. Теперь статус явно говорит,
    ЧТО произошло, чтобы evaluate_final_claim_coverage() не выдавала
    coverage=1.0 при технической ошибке extraction.
    """

    answer = (answer or "").strip()

    if not answer:
        return [], "ok"

    prompt = f"""
Ты извлекаешь АТОМАРНЫЕ ПРОВЕРЯЕМЫЕ УТВЕРЖДЕНИЯ
из уже сформированного ответа ИИ.

FINAL ANSWER:

{answer}

Нужно найти ВСЕ factual claims, которые потенциально
можно проверить по внешним данным.

КРИТИЧЕСКИЕ ПРАВИЛА:

1. Один claim = одно атомарное утверждение.

ПЛОХО:
"Юпитер является газовым гигантом, состоит из водорода
и имеет сильное магнитное поле."

ХОРОШО:
"Юпитер является газовым гигантом."
"Атмосфера Юпитера содержит преимущественно водород."
"Юпитер имеет сильное магнитное поле."

2. Не пропускай утверждения внутри:
- списков;
- таблиц;
- заключения;
- скобок;
- подзаголовков;
- предложений с вводными словами.

3. Сохраняй модальность.

"жизнь существует"
!=
"жизнь может существовать"
!=
"жизнь не обнаружена"
!=
"доказательств жизни нет"

4. Не превращай гипотезу в факт.

5. Не извлекай как factual claim:
- "по имеющейся информации";
- "согласно наблюдениям";
- "возможно";
  если это только вводная конструкция без отдельного содержания;
- методологические комментарии;
- названия разделов;
- URL;
- инструкции пользователю.

6. Для каждого claim укажи тип:

factual
    проверяемое описание мира;

inferential
    вывод из нескольких фактов;

speculative
    гипотеза / возможность;

meta
    утверждение о методе, источниках или самом ответе.

Верни ТОЛЬКО JSON:

{{
  "claims": [
    {{
      "claim_text": "...",
      "claim_type": "factual"
    }}
  ]
}}
""".strip()

    try:
        _gen = _call_ollama_for_extraction(prompt)
        raw = _gen["response"]
    except Exception as exc:
        print(
            f"[Final Claim Extraction] status=call_error "
            f"raw_len=0 format_hint=- error={type(exc).__name__}"
        )
        return [], "call_error"

    data = _extract_json(raw)

    # P0-B: пустой dict от _extract_json() означает, что raw вообще
    # не распарсился как JSON (или был пуст) — это НЕ "модель сказала
    # 0 claims", а parse failure. "claims" отсутствует в data — тоже
    # признак того, что модель вернула не тот формат, а не пустой
    # список осознанно.
    #
    # P1-C: format_hint — компактная диагностика БЕЗ дампа самого
    # raw-текста (только короткие теги вида "has_code_fence+...").
    #
    # P0-B (autonomous fix pass): если Ollama сам сообщил
    # done_reason="length" — генерация была реально ОБОРВАНА лимитом
    # токенов, это НЕ generic parse_error (проблема формата), а честный
    # generation_truncated (проблема бюджета). Разделяем статусы, чтобы
    # downstream (evaluate_final_claim_coverage) и будущий аудит не
    # путали "модель написала мусор" с "модели не хватило места дописать
    # валидный JSON". Bounded tail (не весь raw) печатается ТОЛЬКО в
    # failure-ветках — ровно то, чего не хватало прошлому аудиту, чтобы
    # доказать truncation без гадания.
    if not data or "claims" not in data:
        _no_claims_key = bool(data) and "claims" not in data
        _truncated = _gen.get("done_reason") == "length"
        _status = "generation_truncated" if _truncated else "parse_error"

        print(
            f"[Final Claim Extraction] status={_status} "
            f"{'(no claims key) ' if _no_claims_key else ''}"
            f"raw_len={len(raw or '')} "
            f"format_hint={_format_hint(raw)} "
            f"json_complete={_json_completeness_indicator(raw)} "
            f"done_reason={_gen.get('done_reason')} "
            f"eval_count={_gen.get('eval_count')} "
            f"num_predict={_gen.get('num_predict')}"
        )

        if _status != "ok":
            _tail_len = 300
            print(
                f"[Final Claim Extraction Tail] "
                f"last_{_tail_len}_chars={(raw or '')[-_tail_len:]!r}"
            )

        return [], _status

    try:
        result = []

        for item in data.get("claims", []) or []:
            if not isinstance(item, dict):
                continue

            text = (
                item.get("claim_text", "")
                or item.get("text", "")
                or ""
            ).strip()

            if len(text) < 15:
                continue

            claim_type = (
                item.get("claim_type", "factual")
                or "factual"
            ).strip().lower()

            if claim_type not in {
                "factual",
                "inferential",
                "speculative",
                "meta",
            }:
                claim_type = "factual"

            result.append({
                "claim_text": text,
                "claim_type": claim_type,
            })

        return result, "ok"

    except Exception as exc:
        print(
            f"[Final Claim Extraction] status=parse_error "
            f"raw_len={len(raw or '')} "
            f"format_hint={_format_hint(raw)} "
            f"error={type(exc).__name__}"
        )
        return [], "parse_error"


def _pipeline_claim_text(
    claim: Dict[str, Any],
) -> str:
    return (
        claim.get("claim_text", "")
        or claim.get("text", "")
        or ""
    ).strip()


def _is_same_claim(
    final_claim: str,
    pipeline_claim: str,
) -> bool:
    """
    Используем общий NLI primitive.

    Если pipeline claim поддерживает final claim ИЛИ наоборот,
    считаем, что смысл утверждения уже представлен lifecycle.

    Это НЕ evidence relation.
    Здесь NLI используется только для semantic claim identity.
    """

    if not final_claim or not pipeline_claim:
        return False

    if final_claim.strip().lower() == pipeline_claim.strip().lower():
        return True

    try:
        forward = infer_claim_relation(
            final_claim,
            pipeline_claim,
        )

        if forward.get("relation") == "supports":
            return True

        backward = infer_claim_relation(
            pipeline_claim,
            final_claim,
        )

        if backward.get("relation") == "supports":
            return True

    except Exception:
        return False

    return False


def evaluate_final_claim_coverage(
    answer: str,
    pipeline_claims: List[Dict[str, Any]],
    query: str = "",
) -> FinalClaimCoverageResult:
    """
    Сравнить factual claims финального ответа
    с claims, реально существующими в pipeline.

    PERFORMANCE:
    Раньше semantic identity проверялась вложенным циклом:

        final_claims × pipeline_claims × до 2 Ollama calls

    То есть 10 × 16 claims могли породить до 320
    последовательных LLM generation calls.

    Затем это стало ONE BATCH PIPELINE (infer_claim_relations_batch),
    но по-прежнему строило ВСЕ final×pipeline×2 пары без разбора —
    live run: 20 factual × 13 pipeline × 2 = 520 пар, 17 batch-вызовов,
    220.57s generation. candidate routing (см. _route_candidate_pairs)
    теперь решает, какие пары вообще стоит отправлять в NLI — это
    ROUTING layer, не эпистемическое решение: пара, не выбранная
    роутингом, не помечается unrelated/unsupported, а просто не
    участвует в этом проходе (NOT_SELECTED_FOR_NLI).

    query: опционально — используется только для CORE↔CORE mandatory
    routing правила (existence-question claim role). Пустая строка =
    правило неактивно, поведение как раньше для вызовов без query.
    """

    total_t0 = time.time()

    # --------------------------------------------------------
    # 1. EXTRACT FINAL FACTUAL CLAIMS
    # --------------------------------------------------------

    extract_t0 = time.time()

    extracted, extract_status = extract_final_claims(answer)

    extract_elapsed = time.time() - extract_t0

    factual_claims = [
        claim
        for claim in extracted
        if claim.get("claim_type") == "factual"
    ]

    usable_pipeline_claims = [
        claim
        for claim in (pipeline_claims or [])
        if isinstance(claim, dict)
        and _pipeline_claim_text(claim)
        and claim.get("verification_status") != "rejected"
    ]

    covered = []
    uncovered = []

    if not factual_claims:
        # P0-B: 0 factual claims НЕ означает автоматически
        # coverage=1.0. Различаем:
        #   - extraction реально упала (call_error/parse_error) —
        #     НЕ доверяем "0", conservative score;
        #   - extraction "ok", но ответ заметно длинный — 0 claims
        #     из содержательного текста подозрительно;
        #   - extraction "ok" и ответ короткий/нефактический —
        #     добросовестный пустой результат, coverage=1.0 уместен.
        ANSWER_LENGTH_SUSPICIOUS_THRESHOLD = 200

        if extract_status != "ok":
            coverage_score = 0.0
            coverage_status = "extraction_error"
        elif len(answer) > ANSWER_LENGTH_SUSPICIOUS_THRESHOLD:
            coverage_score = 0.0
            coverage_status = "suspicious_empty"
        else:
            coverage_score = 1.0
            coverage_status = "no_factual_content"

        result = FinalClaimCoverageResult(
            final_claims=extracted,
            covered_claims=[],
            uncovered_claims=[],
            factual_count=0,
            covered_count=0,
            coverage_score=coverage_score,
            coverage_status=coverage_status,
        )

        print(
            f"[Final Coverage Timing] "
            f"extract={extract_elapsed:.2f}s "
            f"pairs=0 "
            f"nli=0.00s "
            f"total={time.time() - total_t0:.2f}s "
            f"extract_status={extract_status} "
            f"coverage_status={coverage_status}"
        )

        return result

    if not usable_pipeline_claims:
        uncovered = [
            dict(claim)
            for claim in factual_claims
        ]

        result = FinalClaimCoverageResult(
            final_claims=extracted,
            covered_claims=[],
            uncovered_claims=uncovered,
            factual_count=len(factual_claims),
            covered_count=0,
            coverage_score=0.0,
            coverage_status="no_pipeline_claims",
        )

        print(
            f"[Final Coverage Timing] "
            f"extract={extract_elapsed:.2f}s "
            f"pairs=0 "
            f"nli=0.00s "
            f"total={time.time() - total_t0:.2f}s "
            f"extract_status={extract_status} "
            f"coverage_status=no_pipeline_claims"
        )

        return result

    # --------------------------------------------------------
    # 2. EXACT MATCH FIRST — БЕЗ LLM
    # --------------------------------------------------------

    matched_final = {}
    unmatched_final_indexes = []

    pipeline_normalized = {}

    for pipeline_index, pipeline_claim in enumerate(
        usable_pipeline_claims
    ):
        pipeline_text = _pipeline_claim_text(
            pipeline_claim
        )

        normalized = pipeline_text.strip().lower()

        pipeline_normalized.setdefault(
            normalized,
            [],
        ).append(pipeline_index)

    for final_index, final_claim in enumerate(
        factual_claims
    ):
        final_text = (
            final_claim.get("claim_text", "")
            or ""
        ).strip()

        normalized = final_text.lower()

        exact_candidates = pipeline_normalized.get(
            normalized,
            [],
        )

        if exact_candidates:
            matched_final[final_index] = (
                exact_candidates[0]
            )
        else:
            unmatched_final_indexes.append(
                final_index
            )

    # --------------------------------------------------------
    # 3. CANDIDATE ROUTING, THEN BUILD BIDIRECTIONAL NLI PAIRS
    # --------------------------------------------------------
    #
    # Сохраняем прежнюю семантику для КАЖДОЙ пары, которую routing
    # отобрал:
    #
    # final -> pipeline == supports
    # ИЛИ
    # pipeline -> final == supports
    #
    # означает semantic identity / coverage. Routing решает только
    # КАКИЕ (final, pipeline) пары стоит проверять — не их отношение.
    # --------------------------------------------------------

    unmatched_final_texts = [
        factual_claims[i]["claim_text"] for i in unmatched_final_indexes
    ]
    pipeline_texts_all = [
        _pipeline_claim_text(c) for c in usable_pipeline_claims
    ]

    routing, routing_stats = _route_candidate_pairs(
        unmatched_final_texts,
        pipeline_texts_all,
        query=query,
    )

    no_nli_candidates_final_indexes = set()

    pairs = []
    total_candidate_slots = 0

    for local_i, final_index in enumerate(unmatched_final_indexes):

        final_text = unmatched_final_texts[local_i]
        selected = routing.get(local_i, {})

        if not selected:
            no_nli_candidates_final_indexes.add(final_index)
            continue

        total_candidate_slots += len(selected)

        for pipeline_index in selected:
            pipeline_text = pipeline_texts_all[pipeline_index]

            base_id = (
                f"{final_index}:{pipeline_index}"
            )

            pairs.append({
                "pair_id": f"F:{base_id}",
                "main_claim": final_text,
                "other_claim": pipeline_text,
            })

            pairs.append({
                "pair_id": f"B:{base_id}",
                "main_claim": pipeline_text,
                "other_claim": final_text,
            })

    _total_possible_pairs = len(unmatched_final_indexes) * len(usable_pipeline_claims) * 2

    print(
        "[Candidate Routing] "
        f"final_claims={len(unmatched_final_indexes)} "
        f"pipeline_claims={len(usable_pipeline_claims)} "
        f"total_possible_pairs={_total_possible_pairs} "
        f"routed_pairs={len(pairs)} "
        f"reduction={(1 - len(pairs) / _total_possible_pairs) * 100 if _total_possible_pairs else 0:.1f}% "
        f"mandatory={routing_stats['mandatory']} "
        f"top_k={routing_stats['top_k']} "
        f"threshold={routing_stats['threshold']} "
        f"no_nli_candidates={len(no_nli_candidates_final_indexes)}"
    )

    # --------------------------------------------------------
    # 4. ONE BATCH PIPELINE INSTEAD OF N×M×2 CALLS
    # --------------------------------------------------------

    nli_t0 = time.time()

    relations = infer_claim_relations_batch(
        pairs,
        batch_size=32,
    )

    nli_elapsed = time.time() - nli_t0

    relation_by_id = {
        str(item.get("pair_id", "")): item
        for item in (relations or [])
        if isinstance(item, dict)
    }

    # --------------------------------------------------------
    # 5. RESOLVE COVERAGE
    # --------------------------------------------------------

    for final_index in unmatched_final_indexes:

        matched_pipeline_index = None

        for pipeline_index in range(
            len(usable_pipeline_claims)
        ):
            base_id = (
                f"{final_index}:{pipeline_index}"
            )

            forward = relation_by_id.get(
                f"F:{base_id}",
                {},
            )

            backward = relation_by_id.get(
                f"B:{base_id}",
                {},
            )

            if (
                forward.get("relation") == "supports"
                or backward.get("relation") == "supports"
            ):
                matched_pipeline_index = (
                    pipeline_index
                )
                break

        if matched_pipeline_index is not None:
            matched_final[final_index] = (
                matched_pipeline_index
            )

    # --------------------------------------------------------
    # 6. BUILD RESULT
    # --------------------------------------------------------

    for final_index, final_claim in enumerate(
        factual_claims
    ):

        pipeline_index = matched_final.get(
            final_index
        )

        if pipeline_index is not None:
            pipeline_claim = (
                usable_pipeline_claims[
                    pipeline_index
                ]
            )

            item = dict(final_claim)

            item["pipeline_claim_id"] = (
                pipeline_claim.get(
                    "claim_id"
                )
            )

            item["pipeline_status"] = (
                pipeline_claim.get(
                    "verification_status",
                    "unknown",
                )
            )

            covered.append(item)

        else:
            item = dict(final_claim)

            # P0 (candidate routing follow-up): distinguish "we
            # actually checked candidates and found no supports" from
            # "routing gave this claim zero candidates to check at
            # all" — NOT_SELECTED_FOR_NLI is a technical routing
            # outcome, never an epistemic verdict. Coverage semantics
            # UNCHANGED: this claim still counts as uncovered either
            # way (conservative — a missing verdict is never upgraded
            # to a positive one), only the diagnostic reason differs.
            item["coverage_reason"] = (
                "NO_NLI_CANDIDATES"
                if final_index in no_nli_candidates_final_indexes
                else "no_supporting_relation_found"
            )

            uncovered.append(item)

    factual_count = len(factual_claims)
    covered_count = len(covered)

    coverage_score = (
        covered_count / factual_count
        if factual_count
        else 1.0
    )

    total_elapsed = (
        time.time() - total_t0
    )

    generation_calls = (
        (len(pairs) + 31) // 32
        if pairs
        else 0
    )

    print(
        f"[Final Coverage Batch] "
        f"factual={factual_count} "
        f"pipeline={len(usable_pipeline_claims)} "
        f"exact={factual_count - len(unmatched_final_indexes)} "
        f"pairs={len(pairs)} "
        f"generation_calls<={generation_calls}"
    )

    print(
        f"[Final Coverage Timing] "
        f"extract={extract_elapsed:.2f}s "
        f"nli={nli_elapsed:.2f}s "
        f"total={total_elapsed:.2f}s"
    )

    return FinalClaimCoverageResult(
        final_claims=extracted,
        covered_claims=covered,
        uncovered_claims=uncovered,
        factual_count=factual_count,
        covered_count=covered_count,
        coverage_score=coverage_score,
        coverage_status="ok",
    )

if __name__ == "__main__":

    answer = """
Юпитер является газовым гигантом.
Его атмосфера состоит преимущественно из водорода и гелия.
Подтверждённых свидетельств разумной жизни на Юпитере нет.
Европа является спутником Юпитера.
Теоретически в облаках Юпитера могут существовать микроорганизмы.
"""

    pipeline = [
        {
            "claim_id": "cl_1",
            "claim_text": "Юпитер является газовым гигантом.",
            "verification_status": "supported",
        },
        {
            "claim_id": "cl_2",
            "claim_text":
                "Разумная жизнь на Юпитере не обнаружена.",
            "verification_status": "unverified",
        },
    ]

    result = evaluate_final_claim_coverage(
        answer,
        pipeline,
    )

    print("===== FINAL CLAIM COVERAGE =====")
    print("factual :", result.factual_count)
    print("covered :", result.covered_count)
    print(
        "coverage:",
        round(result.coverage_score, 3),
    )
    print("status  :", result.coverage_status)

    print()
    print("COVERED:")
    for claim in result.covered_claims:
        print(
            "-",
            claim["claim_text"],
            "->",
            claim.get("pipeline_claim_id"),
            claim.get("pipeline_status"),
        )

    print()
    print("UNCOVERED:")
    for claim in result.uncovered_claims:
        print("-", claim["claim_text"])
