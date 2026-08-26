"""
agent/curiosity.py — Движок любопытства для YANDI V5.

Система самостоятельно:
- находит пробелы в знаниях
- анализирует убеждения с низкой уверенностью
- формирует вопросы
- приоритизирует неизвестное
- инициирует исследование без пользователя

Теперь с интеграцией с belief_manager.
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

from agent.belief_manager import get_belief_manager


@dataclass
class Unknown:
    """Неизвестное, которое хочет исследовать система."""
    id: str
    topic: str
    question: str
    priority: float  # 0..1
    reason: str
    created_at: float
    status: str  # pending | exploring | resolved | abandoned
    related_claims: List[str] = field(default_factory=list)
    confidence_gap: float = 0.0
    related_belief_id: Optional[str] = None


class CuriosityEngine:
    """
    Движок любопытства.
    
    Анализирует убеждения и находит, что ещё нужно узнать.
    """
    
    def __init__(self):
        self.unknowns: List[Unknown] = []
        self.exploration_history: List[Dict] = []
        self.belief_manager = get_belief_manager()
    
    def analyze_beliefs(self) -> List[Unknown]:
        """
        Анализировать убеждения и генерировать вопросы.
        """
        new_unknowns = []
        
        # Получаем все активные убеждения
        beliefs = self.belief_manager.get_all_active()
        
        for belief in beliefs:
            # Если уверенность низкая — нужно исследовать
            if belief.confidence < 0.6:
                new_unknowns.append(self._create_unknown(
                    topic=belief.topic,
                    question=f"Почему уверенность в '{belief.statement[:40]}...' составляет {belief.confidence:.2f}?",
                    priority=1.0 - belief.confidence,
                    reason=f"Низкая уверенность в убеждении",
                    confidence_gap=1.0 - belief.confidence,
                    related_belief_id=belief.id
                ))
            
            # Если уверенность средняя — можно углубить
            elif belief.confidence < 0.8:
                new_unknowns.append(self._create_unknown(
                    topic=belief.topic,
                    question=f"Какие ещё доказательства подтверждают '{belief.statement[:40]}...'?",
                    priority=0.5,
                    reason=f"Средняя уверенность — нужны дополнительные данные",
                    confidence_gap=0.3,
                    related_belief_id=belief.id
                ))
        
        # Добавляем новые неизвестные
        for unknown in new_unknowns:
            self._add_unknown(unknown)
        
        return new_unknowns
    
    def analyze_response(
        self,
        query: str,
        epistemic: Dict[str, Any],
        confidence: float,
        evidence_count: int,
        claims: List[Dict],
        trust: str,
    ) -> List[Unknown]:
        """
        Проанализировать ответ и найти неизвестное.
        """
        new_unknowns = []
        
        # 1. Низкая уверенность → нужно больше знать
        if confidence < 0.6:
            new_unknowns.append(self._create_unknown(
                topic=epistemic.get("domain", "general"),
                question=f"Что ещё неизвестно о {query[:50]}?",
                priority=0.7,
                reason=f"Уверенность {confidence:.2f} — нужны дополнительные данные",
                confidence_gap=1.0 - confidence
            ))
        
        # 2. Мало evidence → нужно больше источников
        if evidence_count < 3:
            new_unknowns.append(self._create_unknown(
                topic=epistemic.get("domain", "general"),
                question=f"Какие ещё источники подтверждают или опровергают это?",
                priority=0.6,
                reason=f"Найдено только {evidence_count} источников",
                confidence_gap=0.3
            ))
        
        # 3. Интерпретативный вопрос → нужны разные рамки
        if epistemic.get("testability") == "interpretive":
            new_unknowns.append(self._create_unknown(
                topic="philosophical",
                question="Какие ещё рамки интерпретации существуют?",
                priority=0.5,
                reason="Вопрос интерпретативный — нужно больше перспектив",
                confidence_gap=0.4
            ))
        
        # 4. Противоречия → нужно разрешить
        if trust in ["UNVERIFIED", "PARTIALLY_SUPPORTED"]:
            new_unknowns.append(self._create_unknown(
                topic=epistemic.get("domain", "general"),
                question=f"Почему нет консенсуса по этому вопросу?",
                priority=0.6,
                reason=f"Trust {trust} — нужно понять причины неопределённости",
                confidence_gap=0.5
            ))
        
        # 5. Специфические темы по запросу
        if "сознание" in query.lower():
            new_unknowns.append(self._create_unknown(
                topic="philosophy_of_mind",
                question="Что такое 'трудная проблема сознания'?",
                priority=0.8,
                reason="Связано с текущим запросом",
                confidence_gap=0.7
            ))
        
        if "жизнь" in query.lower() or "смысл" in query.lower():
            new_unknowns.append(self._create_unknown(
                topic="philosophy",
                question="Какие основные философские позиции по смыслу жизни существуют?",
                priority=0.7,
                reason="Связано с текущим запросом",
                confidence_gap=0.6
            ))
        
        # Добавляем новые неизвестные
        for unknown in new_unknowns:
            self._add_unknown(unknown)
        
        return new_unknowns
    
    def _create_unknown(
        self,
        topic: str,
        question: str,
        priority: float,
        reason: str,
        confidence_gap: float = 0.0,
        related_belief_id: Optional[str] = None,
    ) -> Unknown:
        """Создать объект неизвестного."""
        return Unknown(
            id=f"unk_{uuid.uuid4().hex[:8]}",
            topic=topic,
            question=question,
            priority=min(1.0, max(0.0, priority)),
            reason=reason,
            created_at=time.time(),
            status="pending",
            confidence_gap=confidence_gap,
            related_belief_id=related_belief_id,
        )
    
    def _add_unknown(self, unknown: Unknown):
        """Добавить неизвестное, если его ещё нет."""
        # Проверяем дубли
        for existing in self.unknowns:
            if existing.question == unknown.question:
                # Обновляем приоритет
                existing.priority = max(existing.priority, unknown.priority)
                return
        
        self.unknowns.append(unknown)
        self._sort_by_priority()
    
    def _sort_by_priority(self):
        """Сортировать неизвестные по приоритету."""
        self.unknowns.sort(key=lambda x: x.priority, reverse=True)
    
    def get_next_question(self) -> Optional[Unknown]:
        """Получить следующий вопрос для исследования."""
        for unknown in self.unknowns:
            if unknown.status == "pending":
                unknown.status = "exploring"
                return unknown
        return None
    
    def mark_resolved(self, unknown_id: str):
        """Отметить неизвестное как решённое."""
        for unknown in self.unknowns:
            if unknown.id == unknown_id:
                unknown.status = "resolved"
                break
    
    def get_pending(self) -> List[Unknown]:
        """Получить все ожидающие исследования вопросы."""
        return [u for u in self.unknowns if u.status == "pending"]
    
    def get_exploring(self) -> List[Unknown]:
        """Получить все исследуемые вопросы."""
        return [u for u in self.unknowns if u.status == "exploring"]
    
    def get_by_topic(self, topic: str) -> List[Unknown]:
        """Получить неизвестные по теме."""
        return [u for u in self.unknowns if u.topic == topic]
    
    def get_summary(self) -> Dict[str, Any]:
        """Получить сводку любопытства."""
        total = len(self.unknowns)
        pending = len(self.get_pending())
        exploring = len(self.get_exploring())
        resolved = len([u for u in self.unknowns if u.status == "resolved"])
        
        top_questions = [
            {"question": u.question[:80], "priority": u.priority}
            for u in self.unknowns[:5] if u.status != "resolved"
        ]
        
        return {
            "total_unknowns": total,
            "pending": pending,
            "exploring": exploring,
            "resolved": resolved,
            "top_questions": top_questions,
        }
    
    def to_dict(self) -> Dict[str, Any]:
        """Преобразовать в словарь."""
        return {
            "unknowns": [
                {
                    "id": u.id,
                    "topic": u.topic,
                    "question": u.question,
                    "priority": u.priority,
                    "reason": u.reason,
                    "status": u.status,
                    "confidence_gap": u.confidence_gap,
                    "related_belief_id": u.related_belief_id,
                }
                for u in self.unknowns
            ],
            "summary": self.get_summary(),
        }


# Глобальный экземпляр
_curiosity: Optional[CuriosityEngine] = None

def get_curiosity() -> CuriosityEngine:
    global _curiosity
    if _curiosity is None:
        _curiosity = CuriosityEngine()
    return _curiosity


if __name__ == "__main__":
    # Тестирование
    cu = get_curiosity()
    
    # Добавляем убеждения через belief_manager
    bm = get_belief_manager()
    bm.add_belief(
        topic="consciousness",
        statement="Сознание является эмерджентным свойством нейронных сетей",
        confidence=0.55,
        evidence_ids=["ev_001"]
    )
    bm.add_belief(
        topic="abiogenesis",
        statement="Жизнь возникла из неживой материи около 3,5 млрд лет назад",
        confidence=0.9,
        evidence_ids=["ev_002"]
    )
    
    print("=== АНАЛИЗ УБЕЖДЕНИЙ ===")
    new_unknowns = cu.analyze_beliefs()
    print(f"Создано новых неизвестных: {len(new_unknowns)}")
    
    print("\n=== ВСЕ НЕИЗВЕСТНЫЕ ===")
    for u in cu.unknowns:
        print(f"  [{u.priority:.2f}] {u.question[:70]}... ({u.status})")
        print(f"      Причина: {u.reason}")
    
    print("\n=== СВОДКА ===")
    print(cu.get_summary())
