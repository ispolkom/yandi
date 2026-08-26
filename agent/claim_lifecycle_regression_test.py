"""
agent/claim_lifecycle_regression_test.py

Regression suite for YANDI_CLAIM_LIFECYCLE_DISAPPEARANCE_AUDIT.md.

Проверяет реальную production-цепочку:

    orch_synthesizer.synthesize()
        -> reasoning_info["claims"]   (synthesized)
        -> normalize (orchestrator_v2 логика, воспроизведена офлайн)
        -> claims_data                (lifecycle / validator_input)
        -> ClaimValidator.filter_claims()   (accepted / rejected)

Мокается только сетевой уровень (_call -> Ollama /api/generate) —
чтобы детерминированно воспроизвести реальный сценарий бага:
extraction succeeds (3 claims), НО compose (answer synthesis) падает
с исключением ПОСЛЕ того, как claims уже были построены. До фикса
это приводило к reasoning_info == {"error": ...} без ключа "claims"
(15 -> 0 в реальном прогоне). После фикса claims сохраняются.

Запуск (через реальный project venv, где есть bs4/numpy):
    cd /home/iam/yandi
    /home/iam/venv/bin/python3 -m agent.claim_lifecycle_regression_test
"""

from __future__ import annotations

import sys
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


THREE_CLAIM_SENTENCES = [
    "Юпитер является газовым гигантом без твёрдой поверхности.",
    "На Юпитере отсутствуют условия, подходящие для известной формы жизни.",
    "Радиообзоры космоса не зафиксировали аномальных сигналов от Юпитера.",
]


def _normalize_like_orchestrator(claims_data):
    """
    Точная копия normalize-блока orchestrator_v2.py (строки ~2647-2670),
    воспроизведена офлайн, т.к. полный orchestrator_v2.process() имеет
    тяжёлые side effects при импорте (фоновые потоки, инициализация
    V3/V6) и не предназначен для юнит-тестирования как модуль.
    """
    normalized = []
    for claim in claims_data or []:
        if isinstance(claim, dict):
            if "claim_text" not in claim:
                if "text" in claim:
                    claim["claim_text"] = claim["text"]
                elif "claim" in claim:
                    claim["claim_text"] = claim["claim"]
            normalized.append(claim)
        elif isinstance(claim, str):
            text = claim.strip()
            if text:
                normalized.append({"claim_text": text, "source": "synthesizer"})
    return normalized


def main() -> int:
    from agent.orch_schemas import EnrichedQuery
    import agent.orch_synthesizer as orch_synthesizer
    from agent.claim_validator import ClaimValidator

    call_count = {"n": 0}

    def _fake_call(prompt, max_tokens=600, temp=0.3):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Симулирует claim extraction LLM-вызов — успешен.
            return "\n".join(THREE_CLAIM_SENTENCES)
        # Симулирует ПОЗДНИЙ, не связанный с claims сбой —
        # ровно то, что произошло в реальном прогоне (ReadTimeout
        # на compose_prompt вызове, TIMEOUT=180s).
        raise TimeoutError("simulated: compose LLM call exceeded timeout (180s)")

    local_answer_text = "\n".join(THREE_CLAIM_SENTENCES)

    print("=" * 72)
    print("P0: synthesize() с поздним сбоем compose — claims не теряются")
    print("=" * 72)

    with patch.object(orch_synthesizer, "_call", side_effect=_fake_call):
        synthesis_result, reasoning_info = orch_synthesizer.synthesize(
            EnrichedQuery(
                original="Есть ли разумная жизнь на Юпитере?",
                enriched="Есть ли разумная жизнь на Юпитере? — наука исследование",
                params={},
            ),
            search_result=None,
            web_result=None,
            query_frame={"local_answer": local_answer_text},
            response_mode="hypothesis_first",
        )

    check(
        "synthesis_result.trust_level == UNVERIFIED (сбой compose честно отражён)",
        synthesis_result.trust_level == "UNVERIFIED",
    )
    check(
        "reasoning_info содержит error (сбой не скрыт)",
        "error" in reasoning_info,
    )

    synthesized_claims = reasoning_info.get("claims", [])
    synthesized = len(synthesized_claims)

    check(
        "extracted=3 (claims сохранены в reasoning_info несмотря на "
        "поздний сбой compose)",
        synthesized == 3,
        f"got {synthesized}",
    )

    # ---- normalize (orchestrator_v2 логика) ----
    claims_data = _normalize_like_orchestrator(synthesized_claims)
    lifecycle = len(claims_data)

    check("lifecycle=3 (после normalize)", lifecycle == 3, f"got {lifecycle}")

    validator_input = len(claims_data)
    check("validator_input=3 (то же, что уходит в ClaimValidator)", validator_input == 3)

    print(
        "[Claim Pipeline Boundary] "
        f"synthesized={synthesized} lifecycle={lifecycle} validator_input={validator_input}"
    )

    # ---- ClaimValidator (реальный production-класс) ----
    validator = ClaimValidator()
    pre_validation_claims = list(claims_data)
    accepted_claims = validator.filter_claims(pre_validation_claims)

    rejected_claims = [
        c for c in pre_validation_claims
        if c.get("structural_validation") == "rejected" or c.get("_rejected") is True
    ]

    accepted = len(accepted_claims)
    rejected = len(rejected_claims)

    check(
        "КРИТИЧЕСКИЙ ИНВАРИАНТ: accepted + rejected == validator_input (3)",
        accepted + rejected == validator_input,
        f"accepted={accepted} rejected={rejected} validator_input={validator_input}",
    )
    check(
        "КРИТИЧЕСКИЙ ИНВАРИАНТ: lifecycle>0 => validator НЕ получает 0 без "
        "явного filtering (здесь filtering тривиален, но проходит честно)",
        validator_input > 0,
    )

    print()
    print("=" * 72)
    print("Baseline: ранний сбой (до claims extraction) — claims=[] ожидаемо")
    print("=" * 72)

    def _fail_immediately(prompt, max_tokens=600, temp=0.3):
        raise TimeoutError("simulated: extraction itself times out")

    with patch.object(orch_synthesizer, "_call", side_effect=_fail_immediately):
        _sr2, reasoning_info2 = orch_synthesizer.synthesize(
            EnrichedQuery(
                original="Есть ли разумная жизнь на Юпитере?",
                enriched="Есть ли разумная жизнь на Юпитере? — наука исследование",
                params={},
            ),
            search_result=None,
            web_result=None,
            query_frame={"local_answer": local_answer_text},
            response_mode="hypothesis_first",
        )

    check(
        "ранний сбой (до extraction) -> claims=[] (честно пусто, "
        "не пропущенное значение, а корректная деградация)",
        reasoning_info2.get("claims", []) == [],
    )
    check(
        "ранний сбой -> reasoning_info содержит error",
        "error" in reasoning_info2,
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
