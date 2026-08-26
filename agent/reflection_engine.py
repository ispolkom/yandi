"""
agent/reflection_engine.py — Глубокая рефлексия для YANDI V4.

Анализирует:
- почему выбран этот ответ
- какие альтернативы были
- что могло пойти не так
- какие уроки извлечь
- как изменить поведение в будущем
"""

from __future__ import annotations

import sys
from pathlib import Path
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List


@dataclass
class Reflection:
    """Глубокий результат рефлексии."""
    reflection_id: str
    timestamp: float
    
    # Анализ решения
    query: str
    chosen_action: str
    alternatives: List[str]
    why_chosen: str
    
    # Анализ ошибок
    mistakes: List[str]
    assumptions: List[str]  # что предполагали
    contradictions: List[str]  # что не сошлось
    
    # Уроки
    lessons: List[str]
    future_rule: Optional[str]  # что делать в будущем
    
    # Состояние после
    confidence_delta: float  # изменилась ли уверенность
    state_after: Dict[str, Any]


class ReflectionEngine:
    """Двигатель глубокой рефлексии."""
    
    def __init__(self):
        self.reflections: List[Reflection] = []
        self.past_rules: List[str] = []
    
    def reflect_on_query(
        self,
        query: str,
        epistemic: Dict[str, Any],
        trust: str,
        confidence: float,
        evidence_count: int,
        errors: Optional[List[str]] = None,
    ) -> Reflection:
        """
        Выполнить глубокую рефлексию над запросом.
        """
        reflection_id = f"ref_{uuid.uuid4().hex[:8]}"
        
        # 1. Анализ выбора
        chosen_action = epistemic.get("answer_mode", "unknown")
        alternatives = self._generate_alternatives(epistemic)
        why_chosen = self._explain_why(epistemic)
        
        # 2. Анализ ошибок
        mistakes = errors or []
        mistakes.extend(self._detect_mistakes(epistemic, trust, confidence))
        
        # 3. Анализ предположений
        assumptions = self._detect_assumptions(epistemic, query)
        
        # 4. Поиск противоречий
        contradictions = self._find_contradictions(epistemic, trust, evidence_count)
        
        # 5. Уроки
        lessons = self._extract_lessons(mistakes, confidence)
        
        # 6. Правило на будущее
        future_rule = self._suggest_rule(mistakes, epistemic)
        if future_rule and future_rule not in self.past_rules:
            self.past_rules.append(future_rule)
        
        # 7. Оценка изменения уверенности
        confidence_delta = self._calculate_confidence_delta(confidence, mistakes)
        
        reflection = Reflection(
            reflection_id=reflection_id,
            timestamp=time.time(),
            query=query,
            chosen_action=chosen_action,
            alternatives=alternatives,
            why_chosen=why_chosen,
            mistakes=mistakes,
            assumptions=assumptions,
            contradictions=contradictions,
            lessons=lessons,
            future_rule=future_rule,
            confidence_delta=confidence_delta,
            state_after={
                "trust": trust,
                "confidence": confidence,
                "evidence_count": evidence_count,
                "lessons_count": len(lessons),
            }
        )
        
        self.reflections.append(reflection)
        return reflection
    
    def _generate_alternatives(self, epistemic: Dict[str, Any]) -> List[str]:
        """Сгенерировать альтернативы."""
        domain = epistemic.get("domain", "unknown")
        testability = epistemic.get("testability", "unknown")
        current = epistemic.get("answer_mode", "unknown")
        
        alternatives = []
        
        # По домену
        if domain == "philosophical":
            alternatives.append("factual (если бы были данные)")
            alternatives.append("exploratory (если бы данных не было совсем)")
        elif domain == "factual":
            alternatives.append("qualified_factual (если бы были сомнения)")
            alternatives.append("pluralistic_contextual (если бы вопрос был неоднозначным)")
        elif domain == "biological":
            alternatives.append("scientific (если бы были эксперименты)")
            alternatives.append("philosophical (если бы вопрос был ценностным)")
        
        # По проверяемости
        if testability == "interpretive":
            alternatives.append("dialogic (если бы нужно было уточнить)")
        elif testability == "partially_testable":
            alternatives.append("qualified_factual (если бы было больше данных)")
        
        return alternatives[:3] if alternatives else ["нет очевидных альтернатив"]
    
    def _explain_why(self, epistemic: Dict[str, Any]) -> str:
        """Объяснить, почему выбран этот путь."""
        domain = epistemic.get("domain", "unknown")
        testability = epistemic.get("testability", "unknown")
        mode = epistemic.get("answer_mode", "unknown")
        
        reason = f"Выбран {mode} для вопроса типа {domain} "
        
        if testability == "interpretive":
            reason += "потому что вопрос интерпретативный — нельзя дать один ответ"
        elif testability == "fully_testable":
            reason += "потому что вопрос проверяемый — можно дать факт"
        elif testability == "partially_testable":
            reason += "потому что вопрос частично проверяемый — нужны оговорки"
        else:
            reason += "на основе эпистемической классификации"
        
        return reason
    
    def _detect_mistakes(self, epistemic: Dict[str, Any], trust: str, confidence: float) -> List[str]:
        """Обнаружить ошибки в решении."""
        mistakes = []
        
        if trust == "UNVERIFIED" and confidence < 0.4:
            mistakes.append("Низкая уверенность при отсутствии проверки")
        
        if epistemic.get("testability") == "fully_testable" and confidence < 0.5:
            mistakes.append("Проверяемый вопрос получил низкую уверенность")
        
        if epistemic.get("domain") == "philosophical" and epistemic.get("should_use_web"):
            mistakes.append("Философский вопрос использовал web-поиск (часто избыточно)")
        
        if epistemic.get("domain") == "factual" and epistemic.get("should_use_web") is False:
            mistakes.append("Фактологический вопрос без web-поиска (может быть недостаточно данных)")
        
        return mistakes
    
    def _detect_assumptions(self, epistemic: Dict[str, Any], query: str) -> List[str]:
        """Обнаружить предположения."""
        assumptions = []
        
        if "сознание" in query.lower():
            assumptions.append("Сознание существует как объект изучения")
        
        if "смысл" in query.lower() or "зачем" in query.lower():
            assumptions.append("У жизни/существования есть смысл")
        
        if epistemic.get("domain") == "philosophical":
            assumptions.append("Философские вопросы могут иметь несколько легитимных ответов")
        
        if epistemic.get("testability") == "partially_testable":
            assumptions.append("Часть вопроса проверяема, часть — интерпретативна")
        
        return assumptions
    
    def _find_contradictions(self, epistemic: Dict[str, Any], trust: str, evidence_count: int) -> List[str]:
        """Найти противоречия в решении."""
        contradictions = []
        
        # Противоречие: высокий trust при низком evidence
        if trust in ["SUPPORTED", "STRONGLY_SUPPORTED"] and evidence_count < 3:
            contradictions.append("Высокий trust при малом количестве evidence")
        
        # Противоречие: factual режим для interpretive вопроса
        if epistemic.get("testability") == "interpretive" and epistemic.get("answer_mode") == "factual":
            contradictions.append("Интерпретативный вопрос отвечен как factual")
        
        # Противоречие: web не использован, но вопрос проверяемый
        if epistemic.get("testability") == "fully_testable" and epistemic.get("should_use_web") is False:
            contradictions.append("Проверяемый вопрос без web-поиска")
        
        return contradictions
    
    def _extract_lessons(self, mistakes: List[str], confidence: float) -> List[str]:
        """Извлечь уроки."""
        lessons = []
        
        if not mistakes:
            lessons.append("Решение было правильным — повторять стратегию")
        else:
            for mistake in mistakes:
                if "web" in mistake.lower():
                    lessons.append("Проверять необходимость web-поиска перед использованием")
                if "уверенность" in mistake.lower():
                    lessons.append("Не давать ответы с низкой уверенностью без проверки")
                if "интерпретативный" in mistake.lower():
                    lessons.append("Интерпретативные вопросы не должны ходить в web")
        
        if confidence < 0.5:
            lessons.append("Нужно искать больше источников для подтверждения")
        
        return lessons[:3]
    
    def _suggest_rule(self, mistakes: List[str], epistemic: Dict[str, Any]) -> Optional[str]:
        """Предложить правило на будущее."""
        if not mistakes:
            return None
        
        if "web" in str(mistakes).lower():
            if epistemic.get("domain") in ["philosophical", "interpretive"]:
                return "ЗАПРЕТИТЬ web-поиск для interpretive/non_falsifiable вопросов"
        
        if "уверенность" in str(mistakes).lower():
            return "Понижать trust для ответов с confidence < 0.4"
        
        if "интерпретативный" in str(mistakes).lower():
            return "Для интерпретативных вопросов использовать pluralistic_contextual"
        
        return None
    
    def _calculate_confidence_delta(self, confidence: float, mistakes: List[str]) -> float:
        """Рассчитать изменение уверенности после рефлексии."""
        # Если есть ошибки — уверенность падает
        delta = 0.0
        if mistakes:
            delta -= 0.05 * len(mistakes)
        
        # Если уверенность и так низкая — падает меньше
        if confidence < 0.3:
            delta = max(delta, -0.05)
        
        return round(delta, 2)
    
    def get_summary(self) -> Dict[str, Any]:
        """Получить сводку рефлексий."""
        if not self.reflections:
            return {
                "total": 0,
                "avg_lessons": 0,
                "rules_count": len(self.past_rules),
                "recent": [],
            }
        
        recent = self.reflections[-5:]
        return {
            "total": len(self.reflections),
            "avg_lessons": sum(len(r.lessons) for r in self.reflections) / len(self.reflections),
            "rules_count": len(self.past_rules),
            "recent": [
                {
                    "query": r.query[:50],
                    "action": r.chosen_action,
                    "mistakes": r.mistakes,
                    "lessons": r.lessons,
                }
                for r in recent
            ],
        }
    
    def to_dict(self, reflection: Reflection) -> Dict[str, Any]:
        """Преобразовать рефлексию в словарь."""
        return {
            "reflection_id": reflection.reflection_id,
            "timestamp": reflection.timestamp,
            "query": reflection.query,
            "chosen_action": reflection.chosen_action,
            "alternatives": reflection.alternatives,
            "why_chosen": reflection.why_chosen,
            "mistakes": reflection.mistakes,
            "assumptions": reflection.assumptions,
            "contradictions": reflection.contradictions,
            "lessons": reflection.lessons,
            "future_rule": reflection.future_rule,
            "confidence_delta": reflection.confidence_delta,
            "state_after": reflection.state_after,
        }


# Глобальный экземпляр
_reflection_engine: Optional[ReflectionEngine] = None

def get_reflection_engine() -> ReflectionEngine:
    global _reflection_engine
    if _reflection_engine is None:
        _reflection_engine = ReflectionEngine()
    return _reflection_engine


if __name__ == "__main__":
    # Тестирование
    re = get_reflection_engine()
    
    # Симуляция запроса
    reflection = re.reflect_on_query(
        query="Зачем создаётся жизнь?",
        epistemic={
            "domain": "biological",
            "testability": "partially_testable",
            "answer_mode": "qualified_factual",
            "should_use_web": True,
        },
        trust="SUPPORTED",
        confidence=0.6,
        evidence_count=4,
        errors=["Философский уклон источников"]
    )
    
    print("=== ГЛУБОКАЯ РЕФЛЕКСИЯ ===")
    print(f"Запрос: {reflection.query}")
    print(f"Выбор: {reflection.chosen_action}")
    print(f"Альтернативы: {reflection.alternatives}")
    print(f"Почему: {reflection.why_chosen}")
    print(f"Ошибки: {reflection.mistakes}")
    print(f"Предположения: {reflection.assumptions}")
    print(f"Противоречия: {reflection.contradictions}")
    print(f"Уроки: {reflection.lessons}")
    print(f"Правило: {reflection.future_rule}")
    print(f"Изменение уверенности: {reflection.confidence_delta}")
    
    print("\n=== СВОДКА ===")
    print(re.get_summary())
