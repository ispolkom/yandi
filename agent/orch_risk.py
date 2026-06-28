"""
assistant/orch_risk.py — Risk Engine (hardcoded правила, без LLM).
Определяет уровень риска запроса и параметры валидации.
"""
from __future__ import annotations

from agent.orch_schemas import RiskResult, RiskLevel

# Ключевые слова → уровень риска
_CRITICAL_KW = {
    "медицин", "лечени", "диагноз", "болезн", "симптом", "лекарств", "дозировк",
    "юридич", "закон", "суд", "договор", "право", "иск", "штраф",
    "финансов", "инвестиц", "кредит", "налог", "банкрот",
    "безопасност", "взлом", "уязвимост", "exploit",
}
_HIGH_KW = {
    "хирург", "операци", "вакцин", "антибиотик",
    "наркотик", "алкогол",
    "завещани", "наследств", "арест",
    "криптовалют", "биткоин", "торговл",
}
_MEDIUM_KW = {
    "совет", "рекоменд", "стоит ли", "как лучше",
    "политик", "религи", "спорн",
}


def assess_risk(query: str) -> RiskResult:
    """
    Оценить уровень риска запроса.

    Returns:
        RiskResult с risk_level, mandatory_arbitrage, validator_model, nodes_required
    """
    q = query.lower()

    # Critical — медицина, юриспруденция, финансы, безопасность
    if any(kw in q for kw in _CRITICAL_KW):
        return RiskResult(
            risk_level="critical",
            mandatory_arbitrage=True,
            validator_model="14b",
            nodes_required=3,
        )

    # High
    if any(kw in q for kw in _HIGH_KW):
        return RiskResult(
            risk_level="high",
            mandatory_arbitrage=False,
            validator_model="14b",
            nodes_required=3,
        )

    # Medium — субъективные вопросы, советы
    if any(kw in q for kw in _MEDIUM_KW) or len(query) > 300:
        return RiskResult(
            risk_level="medium",
            mandatory_arbitrage=False,
            validator_model="7b",
            nodes_required=2,
        )

    # Low — всё остальное
    return RiskResult(
        risk_level="low",
        mandatory_arbitrage=False,
        validator_model="7b",
        nodes_required=1,
    )


if __name__ == "__main__":
    tests = [
        "Как лечить кашель?",
        "Как расторгнуть договор аренды?",
        "Как настроить DHT в P2P-сети?",
        "Стоит ли вкладывать деньги в биткоин?",
        "Привет, как дела?",
    ]
    for q in tests:
        r = assess_risk(q)
        print(f"[{r.risk_level:8s}] nodes={r.nodes_required} model={r.validator_model} arb={r.mandatory_arbitrage}  | {q}")
