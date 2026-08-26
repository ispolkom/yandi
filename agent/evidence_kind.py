"""
assistant/evidence_kind.py — Типы источников для YANDI.

Разделяет источники по качеству и назначению.
"""
from __future__ import annotations

from enum import Enum


class EvidenceKind(str, Enum):
    """Тип источника по качеству и назначению."""
    PRIMARY = "primary"                          # Первоисточник
    SCIENTIFIC = "scientific"                    # Научная публикация
    REFERENCE = "reference"                      # Энциклопедия, словарь
    NEWS = "news"                                # Новости
    POPULAR_ARTICLE = "popular_article"          # Популярная статья
    BLOG_OPINION = "blog_opinion"                # Блог / мнение
    FORUM = "forum"                              # Форум / обсуждение
    GENERATED_PIPELINE = "generated_pipeline"    # Сгенерировано пайплайном
    PHILOSOPHICAL_INTERPRETATION = "philosophical_interpretation"  # Философская интерпретация


# Правила использования источников по типам вопросов
USE_RULES = {
    ClaimType.FACTUAL_EMPIRICAL: {
        "allowed": [EvidenceKind.PRIMARY, EvidenceKind.SCIENTIFIC, EvidenceKind.REFERENCE],
        "forbidden": [EvidenceKind.BLOG_OPINION, EvidenceKind.FORUM, EvidenceKind.POPULAR_ARTICLE],
        "trust_weight": 0.9,
    },
    ClaimType.HISTORICAL_EVENT: {
        "allowed": [EvidenceKind.PRIMARY, EvidenceKind.REFERENCE, EvidenceKind.SCIENTIFIC],
        "forbidden": [EvidenceKind.BLOG_OPINION, EvidenceKind.FORUM],
        "trust_weight": 0.8,
    },
    ClaimType.PROCEDURAL: {
        "allowed": [EvidenceKind.REFERENCE, EvidenceKind.PRIMARY],
        "forbidden": [EvidenceKind.FORUM],
        "trust_weight": 0.7,
    },
    ClaimType.AXIOLOGICAL: {
        "allowed": [EvidenceKind.PHILOSOPHICAL_INTERPRETATION, EvidenceKind.POPULAR_ARTICLE],
        "forbidden": [],
        "trust_weight": 0.3,  # Не "истина", а "рамка"
    },
    ClaimType.PHILOSOPHICAL_OPEN: {
        "allowed": [EvidenceKind.PHILOSOPHICAL_INTERPRETATION, EvidenceKind.POPULAR_ARTICLE, EvidenceKind.REFERENCE],
        "forbidden": [],
        "trust_weight": 0.3,
    },
    ClaimType.BOUNDARY_DEFINITION: {
        "allowed": [EvidenceKind.PRIMARY, EvidenceKind.REFERENCE, EvidenceKind.PHILOSOPHICAL_INTERPRETATION],
        "forbidden": [EvidenceKind.FORUM, EvidenceKind.BLOG_OPINION],
        "trust_weight": 0.6,
    },
    ClaimType.ONTOLOGICAL: {
        "allowed": [EvidenceKind.PHILOSOPHICAL_INTERPRETATION, EvidenceKind.REFERENCE],
        "forbidden": [],
        "trust_weight": 0.3,
    },
}
