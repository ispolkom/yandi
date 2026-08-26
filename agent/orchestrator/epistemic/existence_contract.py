"""
Existence Query Contract — extracted from agent/orchestrator_v2.py [9]
(EXISTENCE QUERY CONTRACT, P0-A, autonomous fix pass).

Structural extraction only: no behavior change. See original comment block
preserved below for the rationale.

Аудит (YANDI-autonomous P0-A): для existence-вопроса
("Есть ли разумная жизнь на Юпитере?") pipeline может дойти
до конца с 8/8 claims verified/supported — и ни один из них
не был прямым CORE-ответом на сам вопрос существования (все
про фоновые условия обитаемости). Trust при этом молча
выставлялся как если бы вопрос был проверен. Это отдельный
эпистемический провал от "низкое доверие/support" — здесь
ВСЁ, что проверено, может быть безупречно supported, но
ничего из этого не отвечает на заданный вопрос.

Single-source-of-truth: та же _is_existence_question() и то
же поле supports_query_aspect (роль, вычисленная один раз в
orch_synthesizer.py через _classify_claim_role — не второй
независимый классификатор).

Deterministic detection + trust degradation в этом проходе.
Bounded retry (перегенерировать local_answer/extraction ещё
раз) сознательно НЕ реализован здесь: у нас нет дешёвого
способа доказать, что повторная попытка не потеряет CORE
claim по той же причине, а не-bounded retry запрещён явно.
Задокументировано как рекомендованный следующий шаг, не
решено молча.
"""

from agent.claim_evidence_retriever import _is_existence_question


def apply_existence_query_contract(query_to_use, claims_data, total_claims, synthesis_result, log):
    """
    Mutates synthesis_result in place (trust_level, confidence, answer) when
    an existence-question's claims were all supported without any of them
    being a direct CORE answer to the existence question itself.

    Returns the contract status string ("OK" or "FAILED"), or None if the
    query was not an existence question.
    """
    existence_q = _is_existence_question(query_to_use)

    if not existence_q:
        return None

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

    return contract_status
