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

from agent.orch_web_query import _call_ollama
from agent.claim_relation import (
    infer_claim_relation,
    infer_claim_relations_batch,
)


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
        raw = _call_ollama(prompt)
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
    if not data:
        print(
            f"[Final Claim Extraction] status=parse_error "
            f"raw_len={len(raw or '')} "
            f"format_hint={_format_hint(raw)}"
        )
        return [], "parse_error"

    if "claims" not in data:
        print(
            f"[Final Claim Extraction] status=parse_error "
            f"(no 'claims' key) raw_len={len(raw or '')} "
            f"format_hint={_format_hint(raw)}"
        )
        return [], "parse_error"

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
) -> FinalClaimCoverageResult:
    """
    Сравнить factual claims финального ответа
    с claims, реально существующими в pipeline.

    PERFORMANCE:
    Раньше semantic identity проверялась вложенным циклом:

        final_claims × pipeline_claims × до 2 Ollama calls

    То есть 10 × 16 claims могли породить до 320
    последовательных LLM generation calls.

    Теперь все необходимые направления NLI классифицируются
    batch-вызовами через infer_claim_relations_batch().
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
    # 3. BUILD BIDIRECTIONAL NLI PAIRS
    # --------------------------------------------------------
    #
    # Сохраняем прежнюю семантику:
    #
    # final -> pipeline == supports
    # ИЛИ
    # pipeline -> final == supports
    #
    # означает semantic identity / coverage.
    # --------------------------------------------------------

    pairs = []

    for final_index in unmatched_final_indexes:

        final_text = factual_claims[
            final_index
        ]["claim_text"]

        for pipeline_index, pipeline_claim in enumerate(
            usable_pipeline_claims
        ):
            pipeline_text = _pipeline_claim_text(
                pipeline_claim
            )

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
            uncovered.append(
                dict(final_claim)
            )

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
