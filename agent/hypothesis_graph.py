"""
agent/hypothesis_graph.py — Структуры данных для многоуровневого анализа и графа гипотез.

Уровни:
- L1: Observation (наблюдения из источника)
- L2: Inference (логические следствия)
- L3: Hypothesis (конкурирующие модели)
- L4: InterpretiveTradition (школы/традиции)

Каждая гипотеза хранит поддержку по трём осям, список объясняемых фактов,
ограничения, и связи с другими гипотезами.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from enum import Enum


# ===== УРОВНИ АНАЛИЗА =====

class AnalysisLevel(Enum):
    OBSERVATION = "observation"           # L1
    INFERENCE = "inference"               # L2
    PSYCHOLOGICAL_MODEL = "model"         # L3
    INTERPRETIVE_TRADITION = "tradition"  # L4


# ===== ОЦЕНКИ ПОДДЕРЖКИ =====

@dataclass
class SupportScores:
    """
    Оценка поддержки гипотезы по трём осям (от 0.0 до 5.0).
    text_support: насколько прямо следует из источника
    tradition_support: поддержка в традициях/школах
    science_support: поддержка в современной науке (психология, история и т.д.)
    """
    text_support: float = 0.0
    tradition_support: float = 0.0
    science_support: float = 0.0

    def to_stars(self, axis: str) -> str:
        """Вернуть строку звёзд для указанной оси."""
        value = getattr(self, axis, 0.0)
        full = int(value)
        half = 1 if (value - full) >= 0.5 else 0
        return "★" * full + ("☆" if half else "") + "☆" * (5 - full - half)

    @property
    def nodes(self) -> List[Dict[str, Any]]:
        """Все узлы графа (наблюдения + инференции + гипотезы + традиции)."""
        result = []
        for obs in self.observations:
            result.append({"id": obs.id, "type": "observation", "text": obs.text, "source_ref": obs.source_ref})
        for inf in self.inferences:
            result.append({"id": inf.id, "type": "inference", "text": inf.text, "based_on": inf.based_on})
        for hyp in self.hypotheses:
            result.append({"id": hyp.id, "type": "hypothesis", "name": hyp.name, "description": hyp.description})
        for trad in self.traditions:
            result.append({"id": trad.id, "type": "tradition", "name": trad.name, "description": trad.description})
        return result
    def to_dict(self) -> Dict[str, float]:
        return {
            "text_support": self.text_support,
            "tradition_support": self.tradition_support,
            "science_support": self.science_support,
        }


# ===== УРОВЕНЬ L1: НАБЛЮДЕНИЯ =====

@dataclass
class Observation:
    """L1: Прямое наблюдение из источника."""
    id: str
    text: str
    source_ref: str          # например, "Быт. 4:3-5"
    confidence: float = 1.0  # уверенность в наличии в источнике
    is_direct_quote: bool = False
    raw_quote: Optional[str] = None  # если дословная цитата


# ===== УРОВЕНЬ L2: ЛОГИЧЕСКИЕ СЛЕДСТВИЯ =====

@dataclass
class Inference:
    """L2: Логическое следствие из наблюдений."""
    id: str
    text: str
    based_on: List[str]      # список ID наблюдений (L1)
    confidence: float = 0.8  # уверенность в причинно-следственной связи


# ===== УРОВЕНЬ L3: ГИПОТЕЗЫ (ПСИХОЛОГИЧЕСКИЕ МОДЕЛИ) =====

@dataclass
class Hypothesis:
    """L3: Конкурирующая гипотеза/модель."""
    id: str
    name: str
    description: str
    support: SupportScores

    # Какие факты (наблюдения и инференции) она объясняет
    explains: List[str] = field(default_factory=list)  # список ID L1/L2

    # Какие факты не объясняет
    not_explains: List[str] = field(default_factory=list)

    # Конкурирующие гипотезы (по ID)
    competitors: List[str] = field(default_factory=list)

    # Предпосылки (дополнительные допущения)
    assumptions: List[str] = field(default_factory=list)

    # Что могло бы подтвердить эту гипотезу
    confirm_if: List[str] = field(default_factory=list)

    # Что могло бы опровергнуть
    refute_if: List[str] = field(default_factory=list)

    # Происхождение (кто и когда предложил)
    origin: Optional[str] = None


# ===== УРОВЕНЬ L4: ИНТЕРПРЕТАЦИОННЫЕ ШКОЛЫ =====

@dataclass
class InterpretiveTradition:
    """L4: Школа/традиция с её взглядом на вопрос."""
    id: str
    name: str
    description: str
    preferred_hypotheses: List[str] = field(default_factory=list)  # ID гипотез, которые она поддерживает
    key_figures: List[str] = field(default_factory=list)
    representative_works: List[str] = field(default_factory=list)


# ===== ГРАФ ГИПОТЕЗ =====

@dataclass
class HypothesisGraph:
    """
    Контейнер для всех уровней анализа по одному вопросу.
    """
    question: str
    observations: List[Observation] = field(default_factory=list)
    inferences: List[Inference] = field(default_factory=list)
    hypotheses: List[Hypothesis] = field(default_factory=list)
    traditions: List[InterpretiveTradition] = field(default_factory=list)

    def get_observation(self, obs_id: str) -> Optional[Observation]:
        for o in self.observations:
            if o.id == obs_id:
                return o
        return None

    def get_inference(self, inf_id: str) -> Optional[Inference]:
        for i in self.inferences:
            if i.id == inf_id:
                return i
        return None

    def get_hypothesis(self, hyp_id: str) -> Optional[Hypothesis]:
        for h in self.hypotheses:
            if h.id == hyp_id:
                return h
        return None

    def get_tradition(self, trad_id: str) -> Optional[InterpretiveTradition]:
        for t in self.traditions:
            if t.id == trad_id:
                return t
        return None

    @property
    def nodes(self) -> List[Dict[str, Any]]:
        """Все узлы графа (наблюдения + инференции + гипотезы + традиции)."""
        result = []
        for obs in self.observations:
            result.append({"id": obs.id, "type": "observation", "text": obs.text, "source_ref": obs.source_ref})
        for inf in self.inferences:
            result.append({"id": inf.id, "type": "inference", "text": inf.text, "based_on": inf.based_on})
        for hyp in self.hypotheses:
            result.append({"id": hyp.id, "type": "hypothesis", "name": hyp.name, "description": hyp.description})
        for trad in self.traditions:
            result.append({"id": trad.id, "type": "tradition", "name": trad.name, "description": trad.description})
        return result
    def to_dict(self) -> Dict[str, Any]:
        """Сериализация в словарь для трейсинга и логирования."""
        return {
            "question": self.question,
            "observations": [
                {
                    "id": o.id,
                    "text": o.text,
                    "source_ref": o.source_ref,
                    "confidence": o.confidence,
                    "is_direct_quote": o.is_direct_quote,
                    "raw_quote": o.raw_quote,
                }
                for o in self.observations
            ],
            "inferences": [
                {
                    "id": i.id,
                    "text": i.text,
                    "based_on": i.based_on,
                    "confidence": i.confidence,
                }
                for i in self.inferences
            ],
            "hypotheses": [
                {
                    "id": h.id,
                    "name": h.name,
                    "description": h.description,
                    "support": h.support.to_dict(),
                    "explains": h.explains,
                    "not_explains": h.not_explains,
                    "competitors": h.competitors,
                    "assumptions": h.assumptions,
                    "confirm_if": h.confirm_if,
                    "refute_if": h.refute_if,
                    "origin": h.origin,
                }
                for h in self.hypotheses
            ],
            "traditions": [
                {
                    "id": t.id,
                    "name": t.name,
                    "description": t.description,
                    "preferred_hypotheses": t.preferred_hypotheses,
                    "key_figures": t.key_figures,
                    "representative_works": t.representative_works,
                }
                for t in self.traditions
            ],
        }


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====

def build_support(text: float = 0.0, tradition: float = 0.0, science: float = 0.0) -> SupportScores:
    """Утилита для создания SupportScores."""
    return SupportScores(
        text_support=text,
        tradition_support=tradition,
        science_support=science,
    )
