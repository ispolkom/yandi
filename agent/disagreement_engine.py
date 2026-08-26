"""
agent/disagreement_engine.py — Спор как обучающий эпизод для YANDI V6.

Формат:
old_position → challenge → analysis → new_position

Система меняет мнение под влиянием контраргументов.
Каждый спор — это обучение.
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

from agent.belief_manager import get_belief_manager, Belief


@dataclass
class Disagreement:
    """Эпизод спора."""
    id: str
    topic: str
    old_position: str
    challenge: str
    analysis: str
    new_position: str
    confidence_before: float
    confidence_after: float
    timestamp: float
    resolved: bool = False
    related_belief_id: Optional[str] = None


class DisagreementEngine:
    """
    Двигатель спора — обучение через контраргументы.
    """
    
    def __init__(self):
        self.belief_manager = get_belief_manager()
        self.disagreements: List[Disagreement] = []
        self._load()
    
    def _load(self):
        storage = BASE / "registry" / "disagreements.json"
        if storage.exists():
            try:
                with open(storage, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.disagreements = [Disagreement(**d) for d in data]
            except Exception:
                pass
    
    def _save(self):
        storage = BASE / "registry" / "disagreements.json"
        try:
            storage.parent.mkdir(parents=True, exist_ok=True)
            with open(storage, 'w', encoding='utf-8') as f:
                json.dump([d.__dict__ for d in self.disagreements], f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    
    def challenge(
        self,
        topic: str,
        old_position: str,
        challenge: str,
        analysis: str,
        new_position: str,
        confidence_before: float,
        confidence_after: float,
        related_belief_id: Optional[str] = None,
    ) -> Disagreement:
        """
        Зафиксировать эпизод спора.
        """
        disagreement = Disagreement(
            id=f"dag_{uuid.uuid4().hex[:8]}",
            topic=topic,
            old_position=old_position,
            challenge=challenge,
            analysis=analysis,
            new_position=new_position,
            confidence_before=confidence_before,
            confidence_after=confidence_after,
            timestamp=time.time(),
            resolved=True,
            related_belief_id=related_belief_id,
        )
        self.disagreements.append(disagreement)
        self._save()
        
        # Если есть связанное убеждение — обновляем его
        if related_belief_id:
            belief = self.belief_manager.challenge_belief(
                belief_id=related_belief_id,
                counter_evidence=challenge,
                new_confidence=confidence_after,
                reason=f"спор: {analysis[:50]}",
            )
        
        return disagreement
    
    def get_recent(self, limit: int = 5) -> List[Disagreement]:
        """Получить последние споры."""
        return sorted(self.disagreements, key=lambda d: d.timestamp, reverse=True)[:limit]
    
    def get_by_topic(self, topic: str) -> List[Disagreement]:
        """Получить споры по теме."""
        return [d for d in self.disagreements if d.topic == topic]
    
    def get_stats(self) -> Dict[str, Any]:
        """Статистика споров."""
        topics = {}
        for d in self.disagreements:
            topics[d.topic] = topics.get(d.topic, 0) + 1
        
        total_conf_change = sum(d.confidence_before - d.confidence_after for d in self.disagreements)
        avg_change = total_conf_change / len(self.disagreements) if self.disagreements else 0
        
        return {
            "total": len(self.disagreements),
            "topics": topics,
            "avg_confidence_change": round(avg_change, 2),
            "resolved": len([d for d in self.disagreements if d.resolved]),
        }
    
    def summary(self) -> str:
        stats = self.get_stats()
        recent = self.get_recent(3)
        
        return f"""
=== DISAGREEMENT ENGINE ===
Всего споров: {stats['total']}
Разрешено: {stats['resolved']}
Среднее изменение уверенности: {stats['avg_confidence_change']}
Темы: {', '.join(f'{k}={v}' for k, v in stats['topics'].items())}

Последние споры:
{chr(10).join(f'  - [{d.confidence_before:.2f}→{d.confidence_after:.2f}] {d.topic}: {d.old_position[:30]} → {d.new_position[:30]}' for d in recent) if recent else '  нет'}
"""


_inst: Optional[DisagreementEngine] = None

def get_disagreement_engine() -> DisagreementEngine:
    global _inst
    if _inst is None:
        _inst = DisagreementEngine()
    return _inst


if __name__ == "__main__":
    de = get_disagreement_engine()
    print(de.summary())
    
    # Симуляция спора
    d = de.challenge(
        topic="consciousness",
        old_position="Сознание — это только нейронная активность",
        challenge="Трудная проблема сознания показывает, что субъективный опыт не сводится к нейронным процессам",
        analysis="Физикализм не объясняет квалиа, нужна дополнительная теория",
        new_position="Сознание связано с нейронной активностью, но имеет субъективный компонент",
        confidence_before=0.8,
        confidence_after=0.55,
    )
    print(f"\n✅ Спор зафиксирован: {d.topic}")
    print(f"   {d.old_position} → {d.new_position}")
    print(f"   Уверенность: {d.confidence_before:.2f} → {d.confidence_after:.2f}")
    
    print(de.summary())
