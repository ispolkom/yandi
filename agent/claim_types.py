"""
agent/claim_types.py — Типы утверждений и режимы ответа для эпистемической классификации.
"""

from enum import Enum
from typing import Dict, List, Optional


class ClaimType(Enum):
    """Тип утверждения."""
    FACTUAL = "factual"
    DESCRIPTIVE_FACT = "descriptive_fact"
    INTERPRETATION = "interpretation"
    NORMATIVE_CLAIM = "normative_claim"
    METAPHYSICAL_CLAIM = "metaphysical_claim"
    PROCEDURAL = "procedural"
    EMPIRICAL = "empirical"
    THEORETICAL = "theoretical"
    DOGMATIC = "dogmatic"
    UNKNOWN = "unknown"


class ResponseMode(Enum):
    """Режим ответа."""
    FACTUAL = "factual"
    QUALIFIED_FACTUAL = "qualified_factual"
    CONTEXTUAL = "contextual"
    PLURALISTIC_CONTEXTUAL = "pluralistic_contextual"
    PROCEDURAL = "procedural"
    EXPLORATORY = "exploratory"
    UNKNOWN = "unknown"


class TestabilityLevel(Enum):
    """Уровень проверяемости."""
    FULLY_TESTABLE = "fully_testable"
    PARTIALLY_TESTABLE = "partially_testable"
    INTERPRETIVE = "interpretive"
    NON_FALSIFIABLE = "non_falsifiable"


class KnowledgeStability(Enum):
    """Стабильность знания."""
    STABLE = "stable"
    EMERGING = "emerging"
    CONTROVERSIAL = "controversial"
    UNKNOWN = "unknown"


# Маппинг ClaimType -> ResponseMode
CLAIM_TO_RESPONSE_MODE: Dict[ClaimType, ResponseMode] = {
    ClaimType.FACTUAL: ResponseMode.FACTUAL,
    ClaimType.DESCRIPTIVE_FACT: ResponseMode.FACTUAL,
    ClaimType.EMPIRICAL: ResponseMode.QUALIFIED_FACTUAL,
    ClaimType.THEORETICAL: ResponseMode.QUALIFIED_FACTUAL,
    ClaimType.INTERPRETATION: ResponseMode.PLURALISTIC_CONTEXTUAL,
    ClaimType.NORMATIVE_CLAIM: ResponseMode.PLURALISTIC_CONTEXTUAL,
    ClaimType.METAPHYSICAL_CLAIM: ResponseMode.PLURALISTIC_CONTEXTUAL,
    ClaimType.PROCEDURAL: ResponseMode.PROCEDURAL,
    ClaimType.DOGMATIC: ResponseMode.QUALIFIED_FACTUAL,
    ClaimType.UNKNOWN: ResponseMode.CONTEXTUAL,
}


def get_response_mode(claim_type: ClaimType) -> ResponseMode:
    """Получить режим ответа для типа утверждения."""
    return CLAIM_TO_RESPONSE_MODE.get(claim_type, ResponseMode.CONTEXTUAL)


def should_use_web_for_type(claim_type: ClaimType) -> bool:
    """Определить, нужен ли веб-поиск для типа утверждения."""
    if claim_type in [ClaimType.PROCEDURAL, ClaimType.FACTUAL, ClaimType.DESCRIPTIVE_FACT]:
        return True
    if claim_type in [ClaimType.EMPIRICAL, ClaimType.THEORETICAL]:
        return True
    if claim_type in [ClaimType.INTERPRETATION, ClaimType.NORMATIVE_CLAIM, ClaimType.METAPHYSICAL_CLAIM]:
        return False
    return False


def guess_claim_type_by_text(text: str) -> ClaimType:
    """Угадать тип утверждения по тексту."""
    text_lower = text.lower()
    
    # Метафизические маркеры
    if any(w in text_lower for w in ["бог", "душа", "дух", "абсолют", "трансцендентный", "сверхъестественный"]):
        return ClaimType.METAPHYSICAL_CLAIM
    
    # Нормативные маркеры
    if any(w in text_lower for w in ["должен", "следует", "обязан", "надлежит", "правильно ли", "справедливо"]):
        return ClaimType.NORMATIVE_CLAIM
    
    # Процедурные маркеры
    if any(w in text_lower for w in ["как сделать", "как работает", "инструкция", "алгоритм", "способ"]):
        return ClaimType.PROCEDURAL
    
    # Теоретические маркеры
    if any(w in text_lower for w in ["теория", "гипотеза", "предположение", "модель"]):
        return ClaimType.THEORETICAL
    
    # Эмпирические маркеры
    if any(w in text_lower for w in ["эксперимент", "наблюдение", "данные", "измерение"]):
        return ClaimType.EMPIRICAL
    
    # Интерпретационные маркеры
    if any(w in text_lower for w in ["смысл", "ценность", "этика", "мораль"]):
        return ClaimType.INTERPRETATION
    
    # Догматические маркеры
    if any(w in text_lower for w in ["доказано", "установлено", "факт", "наука говорит"]):
        return ClaimType.DOGMATIC
    
    return ClaimType.FACTUAL


def get_trust_cap_for_testability(testability: str) -> str:
    """Получить максимальный trust для уровня проверяемости."""
    caps = {
        "fully_testable": "STRONGLY_SUPPORTED",
        "partially_testable": "SUPPORTED",
        "interpretive": "PARTIALLY_SUPPORTED",
        "non_falsifiable": "PARTIALLY_SUPPORTED",
    }
    return caps.get(testability, "PARTIALLY_SUPPORTED")


# ── ОПИСАНИЯ ДЛЯ UI ──────────────────────────────────────────────────────────

RESPONSE_MODE_DESCRIPTIONS: Dict[ResponseMode, str] = {
    ResponseMode.FACTUAL: "Отвечать проверяемыми фактами и данными",
    ResponseMode.QUALIFIED_FACTUAL: "Отвечать фактами с оговорками о неопределённости",
    ResponseMode.CONTEXTUAL: "Отвечать с учётом контекста и истории вопроса",
    ResponseMode.PLURALISTIC_CONTEXTUAL: "Давать обзор различных позиций и традиций",
    ResponseMode.PROCEDURAL: "Давать пошаговую инструкцию или алгоритм",
    ResponseMode.EXPLORATORY: "Исследовательский режим с признанием недостатка данных",
    ResponseMode.UNKNOWN: "Стандартный режим",
}


def get_response_mode_description(mode: ResponseMode) -> str:
    """Получить описание режима ответа."""
    return RESPONSE_MODE_DESCRIPTIONS.get(mode, "Стандартный режим")
