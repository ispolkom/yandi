"""
agent/epistemic_router.py — Epistemic Classifier v4.2.

ЧЕСТНАЯ ЭПИСТЕМИЧЕСКАЯ МОДЕЛЬ:
1. ВСЁ — ГИПОТЕЗА. Нет "фактов", есть "наблюдения" и "интерпретации".
2. Наука — это модель, а не истина.
3. Система НЕ знает, она только передаёт чужие наблюдения.
4. Единственный критерий — личный опыт.

ДОБАВЛЕНО v4.2:
- Расширены маркеры для доменов religious и media_interpretation.
"""

from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from agent.claim_types import (
    ClaimType,
    ResponseMode,
    CLAIM_TO_RESPONSE_MODE,
    should_use_web_for_type,
    get_response_mode,
    guess_claim_type_by_text,
)


class TestabilityLevel(Enum):
    FULLY_TESTABLE = "fully_testable"
    PARTIALLY_TESTABLE = "partially_testable"
    INTERPRETIVE = "interpretive"
    NON_FALSIFIABLE = "non_falsifiable"


class KnowledgeStability(Enum):
    STABLE = "stable"
    EMERGING = "emerging"
    CONTROVERSIAL = "controversial"
    UNKNOWN = "unknown"


class AnswerMode(Enum):
    FACTUAL = "factual"
    QUALIFIED_FACTUAL = "qualified_factual"
    CONTEXTUAL = "contextual"
    PLURALISTIC_CONTEXTUAL = "pluralistic_contextual"
    PROCEDURAL = "procedural"
    EXPLORATORY = "exploratory"


@dataclass
class EpistemicClassification:
    domain: str
    testability: str
    answer_mode: str
    subdomain: str = ""
    domains: List[str] = field(default_factory=list)
    confidence: float = 0.5
    trust_score: float = 0.2
    max_trust_cap: str = "PARTIALLY_SUPPORTED"
    reason: str = ""
    recommended_search_strategy: List[str] = field(default_factory=list)
    suggested_clarification: str = ""
    need_clarification: bool = False
    should_use_web: bool = True
    perspective: str = "scientific"
    uncertainty_factors: List[str] = field(default_factory=list)
    is_negative_claim: bool = False
    modality: str = "assertive"
    allow_single_conclusion: bool = True
    needs_frame_split: bool = False
    should_avoid_single_truth_claim: bool = True
    knowledge_stability: str = "unknown"
    stability_confidence: float = 0.5
    stability_reason: str = ""
    claim_type: str = "hypothesis"
    response_mode: str = "qualified_factual"

    is_hypothesis: bool = True
    objectivity_score: float = 0.1
    epistemic_warning: str = ""
    is_science_as_model: bool = True
    consensus_level: float = 0.0
    recommendation: str = ""

    analysis_depth: str = "basic"   # "basic" или "full"


EPISTEMIC_WARNING = """⚠️ **Честное предупреждение от YANDI:**

Я не знаю, что такое "истина". Всё, что я могу — передавать наблюдения и интерпретации других людей.
- Научные теории — это модели, которые могут быть ошибочны.
- Исторические факты — это интерпретация источников.
- Любое знание — это гипотеза, пока вы не проверили её на своём опыте.

Единственный способ узнать — проверить самому. Я лишь помогаю собрать информацию.
"""

# РАСШИРЕННЫЕ МАРКЕРЫ ДЛЯ ДОМЕНОВ
_DOMAIN_MARKERS = {
    "factual": {
        "words": ["сколько", "чему равна", "какой", "какая", "какое", "равен", "равно"],
        "weight": 0.9
    },
    "scientific": {
        "words": ["эксперимент", "наблюдение", "данные", "научный", "исследование",
                  "лаборатория", "гипотеза", "теория", "измерение", "статистика",
                  "анализ", "результат", "доказательство", "гравитация", "квант"],
        "weight": 1.0
    },
    "historical": {
        "words": ["год", "век", "тысячелетие", "до н.э.", "событие", "исторический",
                  "древний", "средневековый", "советский", "война", "первый",
                  "история", "хроника", "архив"],
        "weight": 0.9
    },
    "mathematical": {
        "words": ["доказательство", "аксиома", "теорема", "число", "формула",
                  "расчёт", "дедукция", "логика", "уравнение"],
        "weight": 1.0
    },
    "procedural": {
        "words": ["как сделать", "как работает", "как построить", "инструкция",
                  "алгоритм", "метод", "способ", "процесс", "последовательность",
                  "этап", "шаг", "руководство", "dht", "p2p", "протокол",
                  "как поймать", "как приготовить", "рецепт"],
        "weight": 0.9
    },
    "religious": {
        "words": [
            "вера", "религия", "церковь", "пророк", "откровение", "грех",
            "спасение", "молитва", "ислам", "христианство", "буддизм",
            "коран", "библия", "бог", "каин", "авель", "адам", "ева",
            "библейский", "ветхий завет", "новый завет", "евангелие",
            "иуда", "моисей", "пророчество", "ангел", "архангел",
            "сотворение", "грехопадение", "рай", "ад"
        ],
        "weight": 0.95
    },
    "philosophical": {
        "words": ["смысл", "ценность", "этика", "добро", "зло", "справедливость",
                  "свобода", "воля", "долг", "мораль", "экзистенция",
                  "истина", "знание", "бытие"],
        "weight": 0.8
    },
    "axiological": {
        "words": ["самое ценное", "что важнее", "что главное", "зачем жить",
                  "ценность", "стоит ли", "имеет ли значение", "ради чего",
                  "ценный", "важный"],
        "weight": 0.8
    },
    "metaphysical": {
        "words": ["бытие", "сущность", "первопричина", "абсолют", "сверхъестественный",
                  "дух", "душа", "сознание вне", "трансцендентный", "вечность",
                  "бесконечность", "дуализм"],
        "weight": 0.9
    },
    "normative": {
        "words": ["должен", "правильно ли", "справедливо ли", "можно ли",
                  "имеет ли право", "обязан", "следует", "надлежит"],
        "weight": 0.8
    },
    "biological": {
        "words": ["жизнь", "живой", "неживой", "клетка", "орга", "ген",
                  "эволюция", "вид", "популяция", "экосистем"],
        "weight": 0.7
    },
    "media_interpretation": {
        "words": [
            "фильм", "сериал", "кино", "картина", "лента", "трейлер",
            "смысл фильма", "о чем фильм", "объясни концовку",
            "разбор фильма", "что хотел сказать режиссёр",
            "смысл сериала", "смысл книги", "смысл игры",
            "о чем сериал", "о чем книга", "о чем игра",
            "киновселенная", "сюжет", "персонаж", "режиссёр",
            "экранизация", "посткредитная сцена", "концовка",
            "интерпретация фильма", "что означает фильм"
        ],
        "weight": 0.95
    },
}


def _detect_domain(q: str) -> Tuple[str, str, float]:
    scores = {}
    for domain, data in _DOMAIN_MARKERS.items():
        words = data.get("words", [])
        weight = data.get("weight", 0.5)
        score = sum(weight for w in words if w in q)
        if score > 0:
            scores[domain] = score

    if not scores:
        return "factual", "", 0.3

    max_score = max(scores.values())
    top_domains = [d for d, s in scores.items() if s == max_score]

    if len(top_domains) > 1:
        weights = {d: _DOMAIN_MARKERS[d].get("weight", 0.5) for d in top_domains}
        domain = max(top_domains, key=lambda d: weights[d])
    else:
        domain = top_domains[0]

    return domain, "", min(1.0, max_score / 3)


def _detect_hypothetical(query: str) -> bool:
    markers = [
        "гипотеза", "теория", "предположительно", "возможно",
        "существовал", "могла", "не доказано", "гипотетический",
        "вероятно", "по легенде", "согласно гипотезе",
        "если", "допустим", "предположим", "может быть",
        "считается", "считают", "по мнению", "неизвестно",
        "загадка", "тайна", "спорно", "дискуссионно",
    ]
    q_lower = query.lower()
    return any(m in q_lower for m in markers)


def _detect_negative_claim(query: str) -> bool:
    """
    Запрос сформулирован вокруг отсутствия/необнаружения чего-либо
    (existence-questions вида "есть ли X", "обнаружено ли X").

    P0.2 (YANDI_FULL_PIPELINE_AUDIT.md): is_negative_claim раньше был
    dead stub — всегда False, нигде не вычислялся. Это делает его
    вычисление реальным на уровне query.

    ВАЖНО: пока это ТОЛЬКО диагностическое поле классификации.
    Оно не меняет Claim Status, trust или retrieval — только
    становится корректным (не всегда-False) в trace/логах. Per-claim
    boost для retrieval priority реализован отдельно, локально, в
    claim_evidence_retriever.py::_is_absence_claim (та же лексика,
    но применяется к отдельным claims, а не к целому query).
    """
    markers = [
        "не обнаружен", "не найден", "не зафиксирован",
        "не выявлен", "не установлен", "нет доказательств",
        "нет свидетельств", "нет подтверждени", "не подтвержд",
        "отсутству",
    ]
    q_lower = query.lower()
    return any(m in q_lower for m in markers)


def _detect_testability(q: str, domain: str) -> Tuple[str, float]:
    if domain in ["mathematical"]:
        return "fully_testable", 0.7
    if domain in ["procedural"]:
        return "fully_testable", 0.85
    if domain in ["religious", "metaphysical"]:
        return "non_falsifiable", 0.95
    if domain in ["axiological"]:
        return "interpretive", 0.9
    if domain in ["normative"]:
        return "interpretive", 0.85
    if domain in ["philosophical"]:
        return "interpretive", 0.85
    if domain == "media_interpretation":
        return "partially_testable", 0.8

    if _detect_hypothetical(q):
        return "partially_testable", 0.5

    return "partially_testable", 0.5


def _detect_knowledge_stability(q: str, domain: str, testability: str) -> Tuple[str, float, str]:
    q_lower = q.lower()
    markers = {
        "stable": ["доказано", "установлено", "известно", "факт", "закон", "аксиома", "константа"],
        "emerging": ["новое исследование", "недавно обнаружено", "экспериментальное", "предварительные"],
        "controversial": ["спорно", "дискуссионно", "противоречиво", "разные мнения", "не согласны"],
    }
    scores = {k: sum(1 for m in markers[k] if m in q_lower) for k in markers}
    max_key = max(scores, key=scores.get) if any(scores.values()) else "unknown"

    if max_key == "stable":
        return "stable", 0.6, "Обнаружены маркеры устоявшегося мнения"
    elif max_key == "emerging":
        return "emerging", 0.5, "Обнаружены маркеры нового знания"
    elif max_key == "controversial":
        return "controversial", 0.7, "Обнаружены маркеры спорного вопроса"
    return "unknown", 0.4, "Недостаточно данных"


def _get_answer_mode(domain: str, testability: str) -> str:
    if testability in ["interpretive", "non_falsifiable"]:
        return "pluralistic_contextual"
    if domain in ["scientific", "historical", "religious", "philosophical", 
                  "media_interpretation", "metaphysical", "axiological", "normative",
                  "biological", "factual", "physical", "chemical", "astronomical"]:
        return "hypothesis_first"
    return "qualified_factual"


def _determine_analysis_depth(domain: str, testability: str) -> str:
    full_domains = {
        "religious", "philosophical", "historical", "axiological",
        "normative", "media_interpretation", "metaphysical",
        "scientific"   # для сложных научных вопросов с несколькими моделями
    }
    if domain in full_domains or testability in ("interpretive", "non_falsifiable"):
        return "full"
    return "basic"


def get_trust_cap_for_testability(testability: str) -> str:
    return "PARTIALLY_SUPPORTED"


def get_objectivity_score(
    testability: str,
    domain: str,
    knowledge_stability: str,
    is_hypothetical: bool = False,
) -> Tuple[float, str, bool]:
    base_score = 0.1

    if domain in ["procedural"]:
        base_score = 0.3
    if testability == "fully_testable" and domain in ["procedural"]:
        base_score = 0.4
    if domain == "mathematical":
        base_score = 0.3
    if is_hypothetical:
        base_score = 0.05

    if knowledge_stability == "stable":
        base_score += 0.1
    elif knowledge_stability == "controversial":
        base_score -= 0.05
    elif knowledge_stability == "emerging":
        base_score -= 0.05

    objectivity_score = max(0.0, min(0.5, base_score))

    epistemic_warning = EPISTEMIC_WARNING

    if is_hypothetical:
        epistemic_warning += "\n\n⚠️ **Это гипотетическое утверждение.** Нет прямых доказательств."

    if domain in ["scientific", "biological"]:
        epistemic_warning += "\n\n🧪 **Это научная модель.** Она объясняет наблюдаемые явления, но не является окончательной истиной."

    if knowledge_stability == "controversial":
        epistemic_warning += "\n\n⚡ **Это спорное утверждение.** Существуют разные, иногда противоположные точки зрения."

    if domain in ["historical"]:
        epistemic_warning += "\n\n📜 **Это историческая интерпретация.** История пишется на основе источников, которые могут быть неполными или предвзятыми."

    return objectivity_score, epistemic_warning, True


def classify_claim(query: str, intent: str = "", confidence: float = 0.5) -> EpistemicClassification:
    q = query.lower()
    domain, subdomain, domain_conf = _detect_domain(q)
    is_hypothetical = _detect_hypothetical(q)
    is_negative = _detect_negative_claim(query)
    testability, test_conf = _detect_testability(q, domain)
    answer_mode = _get_answer_mode(domain, testability)
    knowledge_stability, stability_conf, stability_reason = _detect_knowledge_stability(q, domain, testability)

    objectivity_score, epistemic_warning, is_science_as_model = get_objectivity_score(
        testability=testability,
        domain=domain,
        knowledge_stability=knowledge_stability,
        is_hypothetical=is_hypothetical,
    )

    analysis_depth = _determine_analysis_depth(domain, testability)

    trust_score = 0.2
    max_trust_cap = "PARTIALLY_SUPPORTED"
    should_use_web = True
    needs_frame_split = testability in ["interpretive", "non_falsifiable"]
    should_avoid_single_truth_claim = True
    allow_single_conclusion = False

    recommendation = "💡 **Рекомендация:** Проверьте эту информацию на своём опыте. Наблюдайте, экспериментируйте, делайте свои выводы."

    recommended_strategy = ["поиск различных источников и точек зрения"]

    if domain == "scientific":
        recommended_strategy.append("научные публикации (как модель, не как истина)")
    if domain == "historical":
        recommended_strategy.append("первичные и вторичные исторические источники")
    if domain == "procedural":
        recommended_strategy.append("практические руководства и инструкции")
    if testability in ["interpretive", "non_falsifiable"]:
        recommended_strategy.append("сравнение различных позиций и традиций")

    return EpistemicClassification(
        domain=domain,
        testability=testability,
        answer_mode=answer_mode,
        subdomain=subdomain,
        domains=[domain],
        confidence=0.5,
        trust_score=trust_score,
        max_trust_cap=max_trust_cap,
        reason=f"domain={domain}, testability={testability}, hypothesis={is_hypothetical}",
        recommended_search_strategy=recommended_strategy,
        suggested_clarification="",
        need_clarification=False,
        should_use_web=should_use_web,
        perspective="scientific",
        uncertainty_factors=["all_knowledge_is_hypothesis"],
        is_negative_claim=is_negative,
        modality="assertive",
        allow_single_conclusion=allow_single_conclusion,
        needs_frame_split=needs_frame_split,
        should_avoid_single_truth_claim=should_avoid_single_truth_claim,
        knowledge_stability=knowledge_stability,
        stability_confidence=stability_conf,
        stability_reason=stability_reason,
        claim_type="hypothesis",
        response_mode=answer_mode,
        is_hypothesis=True,
        objectivity_score=objectivity_score,
        epistemic_warning=epistemic_warning,
        is_science_as_model=is_science_as_model,
        consensus_level=0.0,
        recommendation=recommendation,
        analysis_depth=analysis_depth,
    )


def get_trust_label_for_epistemic(classification: EpistemicClassification) -> str:
    return "PARTIALLY_SUPPORTED"


def get_response_mode_description(mode: str) -> str:
    descriptions = {
        "factual": "отвечать фактами (но это гипотеза)",
        "qualified_factual": "отвечать с оговорками о неопределённости",
        "contextual": "отвечать с учётом контекста",
        "pluralistic_contextual": "давать обзор различных позиций",
        "procedural": "давать пошаговую инструкцию",
        "exploratory": "исследовательский режим",
    }
    return descriptions.get(mode, "стандартный режим с оговорками")


if __name__ == "__main__":
    test_queries = [
        "Какое расстояние до Марса?",
        "Почему погибла планета Фаэтон?",
        "Как работает гравитация?",
        "Сколько лет Вселенной?",
        "Что такое сознание?",
        "Как пожарить щуку?",
        "За что Каин убил Авеля?",
        "Смысл фильма Матрица",
    ]

    print("=" * 80)
    print("EPISTEMIC ROUTER v4.2 — РАСШИРЕННЫЕ МАРКЕРЫ")
    print("ВСЁ — ГИПОТЕЗА. НАУКА — МОДЕЛЬ.")
    print("=" * 80)

    for q in test_queries:
        result = classify_claim(q)

        print(f"\n📝 Запрос: {q}")
        print(f"  📌 Домен: {result.domain}")
        print(f"  🔬 Проверяемость: {result.testability}")
        print(f"  🏛️  Стабильность: {result.knowledge_stability}")
        print(f"  📊 Объективность: {result.objectivity_score:.2f}")
        print(f"  💬 Режим ответа: {result.answer_mode}")
        print(f"  🏷️  Trust: PARTIALLY_SUPPORTED (всегда)")
        print(f"  🧪 Наука как модель: {result.is_science_as_model}")
        print(f"  ⚠️  Гипотеза: {result.is_hypothesis}")
        print(f"  🔍 Глубина анализа: {result.analysis_depth.upper()}")
        print(f"  💡 Рекомендация: {result.recommendation}")
        print(f"  🌐 Web: {result.should_use_web}")
