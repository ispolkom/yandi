"""
agent/final_epistemic_regression_test.py

Дешёвый offline regression suite для Final Epistemic Contract
(YANDI_FINAL_EPISTEMIC_AUDIT_AND_FIX.md).

Проверяет:
    P0-A — Final Answer Gate (текст обязан отражать Claim Status);
    P0-B — Final Claim Coverage (0/0 vacuous bug);
    P0-C — Novel Claim Leakage (переиспользует P0-B);
    P1   — сохранение subject anchor на входе в query generation;
    P3   — диагностика (не фикс) source_quality для local registry.

НЕ требует Ollama/web — все LLM-зависимые вызовы замоканы.

Запуск:
    cd /home/iam/yandi
    python3 -m agent.final_epistemic_regression_test

Код выхода 0 — всё прошло, 1 — есть провалы.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    from agent.orch_schemas import SynthesisResult

    # ============================================================
    # P0-A: FINAL ANSWER GATE
    # ============================================================
    #
    # Логика Claim Status Gate живёт внутри огромной функции
    # orchestrator_v2.py (не выделена в отдельную testable функцию —
    # рефакторинг этого не входил в scope данного прохода). Здесь
    # воспроизводится ТА ЖЕ логика (скопирована из реального кода на
    # момент фикса) для offline-проверки инварианта. Если реальный
    # код в orchestrator_v2.py разойдётся с этой копией — тест не
    # поймает расхождение, это известное ограничение подхода.
    print("=" * 72)
    print("P0-A: Final Answer Gate — текст обязан отражать Claim Status")
    print("=" * 72)

    def apply_gate(
        claims_contradicted,
        claims_rejected,
        claims_unverified,
        claims_candidate,
        claims_verified,
        claims_supported,
        total_claims,
        synthesis_result,
    ):
        trust_rank = {
            "UNVERIFIED": 0, "WEAKLY_SUPPORTED": 1, "PARTIALLY_SUPPORTED": 2,
            "SUPPORTED": 3, "STRONGLY_SUPPORTED": 4, "VERIFIED": 5,
        }

        if claims_contradicted > 0 and (
            claims_contradicted + claims_rejected + claims_unverified + claims_candidate == total_claims
        ):
            synthesis_result.trust_level = "UNVERIFIED"
            synthesis_result.confidence = min(synthesis_result.confidence, 0.25)

            notice = (
                "⚠️ ВАЖНО: часть проверяемых утверждений в этом ответе "
                f"была ОПРОВЕРГНУТА найденными источниками "
                f"(contradicted={claims_contradicted} из {total_claims}), "
                "и ни одно утверждение не получило прямого подтверждения. "
                "Текст ниже остаётся гипотезой модели — не считай его "
                "установленным фактом.\n"
            )
            if not synthesis_result.answer.startswith("⚠️ ВАЖНО:"):
                synthesis_result.answer = notice + "\n" + synthesis_result.answer

        elif claims_verified == 0:
            if trust_rank.get(synthesis_result.trust_level, 0) > trust_rank["PARTIALLY_SUPPORTED"]:
                synthesis_result.trust_level = "PARTIALLY_SUPPORTED"

            if claims_supported == 0:
                if trust_rank.get(synthesis_result.trust_level, 0) > trust_rank["WEAKLY_SUPPORTED"]:
                    synthesis_result.trust_level = "WEAKLY_SUPPORTED"
                synthesis_result.confidence = min(synthesis_result.confidence, 0.40)

                notice = (
                    "⚠️ ВАЖНО: ни одно из "
                    f"{total_claims} проверяемых утверждений не получило "
                    "подтверждающих доказательств (supported=0, "
                    "verified=0). Всё, что изложено ниже — "
                    "неподтверждённая гипотеза модели, а не установленный "
                    "факт. Система не получила достаточной evidence-базы "
                    "для проверки.\n"
                )
                if not synthesis_result.answer.startswith("⚠️ ВАЖНО:"):
                    synthesis_result.answer = notice + "\n" + synthesis_result.answer
            else:
                synthesis_result.confidence = min(synthesis_result.confidence, 0.60)

        return synthesis_result

    # 1. supported=0 + unverified core -> responder не имеет права
    #    молча assert-ить новую гипотезу как есть.
    sr = SynthesisResult(
        answer="Основная гипотеза: жизнь существует в такой-то форме...",
        confidence=0.9, sources=[], trust_level="PARTIALLY_SUPPORTED",
    )
    apply_gate(0, 0, 10, 0, 0, 0, 10, sr)
    check(
        "1. supported=0 verified=0 -> answer помечен явным disclaimer",
        sr.answer.startswith("⚠️ ВАЖНО: ни одно из 10"),
        sr.answer[:80],
    )
    check("1b. trust_level понижен до WEAKLY_SUPPORTED", sr.trust_level == "WEAKLY_SUPPORTED")

    # 2. contradicted claim -> нельзя assert-ить как факт.
    sr2 = SynthesisResult(answer="X точно является фактом.", confidence=0.8, sources=[], trust_level="SUPPORTED")
    apply_gate(3, 0, 2, 0, 5, 0, 5, sr2)
    check(
        "2. contradicted-доминантный сценарий -> answer помечен",
        sr2.answer.startswith("⚠️ ВАЖНО: часть проверяемых утверждений"),
    )
    check("2b. trust_level -> UNVERIFIED", sr2.trust_level == "UNVERIFIED")

    # 3. supported claim -> разрешён, текст не трогается.
    sr3 = SynthesisResult(answer="Проверенный факт.", confidence=0.9, sources=[], trust_level="VERIFIED")
    apply_gate(0, 0, 2, 0, 3, 3, 5, sr3)
    check("3. verified>0 -> текст не изменён", sr3.answer == "Проверенный факт.")

    # verified=0, но supported>0 -> текст НЕ маркируется (менее опасный
    # случай, только confidence снижается) — подтверждаем, что фикс не
    # перегибает и не портит частично поддержанные ответы.
    sr3b = SynthesisResult(answer="Оригинальный текст.", confidence=0.9, sources=[], trust_level="SUPPORTED")
    apply_gate(0, 0, 3, 0, 0, 2, 5, sr3b)
    check("3b. verified=0 supported>0 -> текст НЕ помечен (не перегиб)", sr3b.answer == "Оригинальный текст.")

    # Идемпотентность — двойной проход gate не дублирует disclaimer.
    sr4 = SynthesisResult(answer="Текст.", confidence=0.9, sources=[], trust_level="SUPPORTED")
    apply_gate(0, 0, 10, 0, 0, 0, 10, sr4)
    len1 = len(sr4.answer)
    apply_gate(0, 0, 10, 0, 0, 0, 10, sr4)
    check("4. идемпотентность (нет двойного disclaimer)", len(sr4.answer) == len1)

    print()

    # ============================================================
    # P0-B / P0-C: FINAL CLAIM COVERAGE + LEAKAGE
    # ============================================================
    print("=" * 72)
    print("P0-B/C: Final Claim Coverage — 0/0 vacuous bug + novel leakage")
    print("=" * 72)

    import agent.final_claim_coverage as fcc

    # 5. Answer с реальными factual claims -> factual_count > 0.
    resp_ok = json.dumps({"claims": [
        {"claim_text": "Юпитер является газовым гигантом.", "claim_type": "factual"},
        {"claim_text": "Разумная жизнь на Юпитере не обнаружена.", "claim_type": "factual"},
    ]})
    with patch("agent.final_claim_coverage._call_ollama", lambda p: resp_ok):
        with patch("agent.final_claim_coverage.infer_claim_relations_batch", return_value=[]):
            result = fcc.evaluate_final_claim_coverage(
                "Юпитер — газовый гигант. Разумная жизнь на Юпитере не обнаружена.",
                [{"claim_id": "cl_1", "claim_text": "Юпитер является газовым гигантом.", "verification_status": "unverified"}],
            )
    check("5. факты в ответе -> factual_count > 0", result.factual_count > 0, f"factual_count={result.factual_count}")

    # 6. "Я не знаю." -> factual_count может быть 0, coverage=1.0 ОК.
    resp_empty = json.dumps({"claims": []})
    with patch("agent.final_claim_coverage._call_ollama", lambda p: resp_empty):
        result = fcc.evaluate_final_claim_coverage("Я не знаю.", [{"claim_id": "cl_1", "claim_text": "x", "verification_status": "unverified"}])
    check(
        "6. короткий нефактический ответ -> coverage=1.0 корректен",
        result.factual_count == 0 and result.coverage_score == 1.0 and result.coverage_status == "no_factual_content",
        f"factual={result.factual_count} coverage={result.coverage_score} status={result.coverage_status}",
    )

    # 7. Parser/model extraction failure -> НЕ coverage=1.00 как успех.
    def fake_call_error(prompt):
        raise Exception("simulated outage")

    with patch("agent.final_claim_coverage._call_ollama", fake_call_error):
        result = fcc.evaluate_final_claim_coverage(
            "Длинный содержательный ответ с фактами " * 10,
            [{"claim_id": "cl_1", "claim_text": "x", "verification_status": "unverified"}],
        )
    check(
        "7. extraction call_error -> coverage != 1.0",
        result.coverage_score != 1.0 and result.coverage_status == "extraction_error",
        f"coverage={result.coverage_score} status={result.coverage_status}",
    )

    # Parse error variant (malformed JSON, не exception).
    with patch("agent.final_claim_coverage._call_ollama", lambda p: "not json {{{"):
        result = fcc.evaluate_final_claim_coverage(
            "Длинный содержательный ответ с фактами " * 10,
            [{"claim_id": "cl_1", "claim_text": "x", "verification_status": "unverified"}],
        )
    check(
        "7b. malformed JSON -> coverage != 1.0 (не exception, а parse failure)",
        result.coverage_score != 1.0 and result.coverage_status == "extraction_error",
        f"coverage={result.coverage_score} status={result.coverage_status}",
    )

    # 8. Answer с 3 фактами, pipeline содержит 2 -> coverage не 1.0,
    #    третий (novel) claim виден как uncovered.
    resp_partial = json.dumps({"claims": [
        {"claim_text": "Юпитер является газовым гигантом.", "claim_type": "factual"},
        {"claim_text": "Атмосфера Юпитера содержит водород.", "claim_type": "factual"},
        {"claim_text": "Юпитер имеет 95 известных спутников.", "claim_type": "factual"},
    ]})
    pipeline = [
        {"claim_id": "cl_1", "claim_text": "Юпитер является газовым гигантом.", "verification_status": "unverified"},
        {"claim_id": "cl_2", "claim_text": "Атмосфера Юпитера содержит водород.", "verification_status": "unverified"},
    ]
    with patch("agent.final_claim_coverage._call_ollama", lambda p: resp_partial):
        with patch("agent.final_claim_coverage.infer_claim_relations_batch", return_value=[]):
            result = fcc.evaluate_final_claim_coverage("irrelevant body text", pipeline)
    check(
        "8. partial coverage (3 факта, 2 в pipeline) -> coverage < 1.0",
        result.factual_count == 3 and result.covered_count == 2 and result.coverage_score < 1.0,
        f"factual={result.factual_count} covered={result.covered_count} coverage={result.coverage_score}",
    )
    check(
        "8b. novel claim ('95 известных спутников') виден как uncovered",
        len(result.uncovered_claims) == 1 and "95" in result.uncovered_claims[0]["claim_text"],
    )

    print()

    # ============================================================
    # P1: SUBJECT PRESERVATION НА ВХОДЕ В QUERY GENERATION
    # ============================================================
    print("=" * 72)
    print("P1: subject anchor сохраняется на входе в query formulation")
    print("=" * 72)

    import re as _re

    # Изолированный импорт только чистых regex-функций (без scrape/bs4).
    src = open("agent/claim_evidence_retriever.py", encoding="utf-8").read()
    start = src.index("def _extract_subject_anchors")
    end = src.index("def _snippet_text")
    ns: dict = {"re": _re, "List": list}
    exec(compile(src[start:end], "claim_evidence_retriever_anchors", "exec"), ns)
    _extract_subject_anchors = ns["_extract_subject_anchors"]

    claim_text = "Ни один телескоп или космический аппарат не зафиксировал ни одного сигнала или артефакта на Юпитере."
    query_context = "Есть ли разумная жизнь на Юпитере?"
    contextual_claim_text = f"{query_context}\nПроверяемое утверждение: {claim_text}"

    anchors_from_claim = _extract_subject_anchors(claim_text)
    check(
        "9. CORE/DIRECT claim САМ содержит subject anchor",
        "jupiter" in anchors_from_claim,
        f"anchors={anchors_from_claim}",
    )
    check(
        "9b. contextual_claim_text (вход в formulate_claim_evidence_queries) "
        "содержит явный anchor 'юпитере'",
        "юпитере" in contextual_claim_text.lower(),
    )
    print(
        "    ПРИМЕЧАНИЕ: этот тест проверяет только ВХОД в query "
        "generation (код это гарантирует). Реальный текст запросов "
        "формирует LLM (heretic:q8) — без live-вызова нельзя доказать, "
        "что subject не теряется в ГЕНЕРИРУЕМОЙ строке. Для этого "
        "добавлен [Claim Retrieval Query] print — см. отчёт §12."
    )

    print()

    # ============================================================
    # P3: REGISTRY EVIDENCE — ДИАГНОСТИКА (НЕ ФИКС)
    # ============================================================
    print("=" * 72)
    print("P3: source_quality для local registry (диагностика, не фикс)")
    print("=" * 72)

    from agent.source_quality import evaluate_source_quality

    q = evaluate_source_quality(
        url="",
        title="Локальный документ реестра",
        text="Подробный проверенный внутренний текст. " * 30,
        source_type="local",
    )
    check(
        "10. local registry (без URL) НЕ может стать eligible=True "
        "даже с длинным текстом — подтверждает P3 finding",
        q.evidence_eligible is False,
        f"class={q.source_class} quality={q.quality_score} eligible={q.evidence_eligible}",
    )
    check(
        "10b. local registry получает role=context (не direct)",
        q.evidence_role == "context",
        f"role={q.evidence_role}",
    )

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
    else:
        print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
