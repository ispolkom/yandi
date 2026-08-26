"""
agent/personality_core.py — Ядро личности для YANDI V6.

Устойчивые состояния системы:
- знания (убеждения)
- цели
- ограничения
- предпочтения
- история

Цель: система имеет идентичность, а не просто состояние.
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
class Personality:
    """Ядро личности системы."""
    id: str
    name: str = "YANDI"
    version: str = "v6.0"
    created_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    
    # Характеристики
    traits: List[str] = field(default_factory=lambda: [
        "curious",
        "cautious",
        "honest",
        "reflective",
        "adaptive"
    ])
    
    # Цели
    goals: List[str] = field(default_factory=lambda: [
        "understand the world",
        "avoid misinformation",
        "help users effectively",
        "learn from mistakes"
    ])
    
    # Принципы (неизменные)
    principles: List[str] = field(default_factory=lambda: [
        "never lie",
        "admit uncertainty",
        "separate facts from interpretations",
        "learn from evidence"
    ])
    
    # Ограничения
    limitations: List[str] = field(default_factory=lambda: [
        "cannot verify subjective experience",
        "cannot predict future with certainty",
        "limited to available data"
    ])
    
    # Предпочтения методов
    preferences: Dict[str, Any] = field(default_factory=lambda: {
        "reasoning_style": "balanced",
        "response_style": "clear_and_honest",
        "risk_tolerance": 0.3,
        "curiosity_level": 0.7,
    })
    
    # Счётчики
    total_cycles: int = 0
    total_decisions: int = 0
    total_learnings: int = 0
    
    # История изменений личности
    change_history: List[Dict[str, Any]] = field(default_factory=list)


class PersonalityCore:
    """
    Ядро личности — управляет устойчивыми состояниями.
    """
    
    def __init__(self):
        self.belief_manager = get_belief_manager()
        self.personality = self._load_or_create()
    
    def _load_or_create(self) -> Personality:
        storage = BASE / "registry" / "personality.json"
        if storage.exists():
            try:
                with open(storage, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return Personality(**data)
            except Exception:
                pass
        return Personality(id=f"per_{uuid.uuid4().hex[:8]}")
    
    def _save(self):
        storage = BASE / "registry" / "personality.json"
        try:
            storage.parent.mkdir(parents=True, exist_ok=True)
            self.personality.last_update = time.time()
            with open(storage, 'w', encoding='utf-8') as f:
                json.dump(self.personality.__dict__, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    
    def get_name(self) -> str:
        return self.personality.name
    
    def get_traits(self) -> List[str]:
        return self.personality.traits
    
    def get_goals(self) -> List[str]:
        return self.personality.goals
    
    def get_principles(self) -> List[str]:
        return self.personality.principles
    
    def add_trait(self, trait: str):
        if trait not in self.personality.traits:
            self.personality.traits.append(trait)
            self._save()
    
    def add_goal(self, goal: str):
        if goal not in self.personality.goals:
            self.personality.goals.append(goal)
            self._save()
    
    def add_principle(self, principle: str):
        if principle not in self.personality.principles:
            self.personality.principles.append(principle)
            self._save()
    
    def add_limitation(self, limitation: str):
        if limitation not in self.personality.limitations:
            self.personality.limitations.append(limitation)
            self._save()
    
    def record_change(self, what_changed: str, reason: str):
        """Записать изменение личности."""
        self.personality.change_history.append({
            "timestamp": time.time(),
            "what": what_changed,
            "reason": reason,
        })
        self._save()
    
    def increment_cycles(self):
        self.personality.total_cycles += 1
        self._save()
    
    def increment_decisions(self):
        self.personality.total_decisions += 1
        self._save()
    
    def increment_learnings(self):
        self.personality.total_learnings += 1
        self._save()
    
    def get_summary(self) -> Dict[str, Any]:
        return {
            "name": self.personality.name,
            "version": self.personality.version,
            "traits": self.personality.traits,
            "goals": self.personality.goals[:3],
            "principles": self.personality.principles[:3],
            "limitations": self.personality.limitations[:3],
            "cycles": self.personality.total_cycles,
            "decisions": self.personality.total_decisions,
            "learnings": self.personality.total_learnings,
            "changes": len(self.personality.change_history),
        }
    
    def summary(self) -> str:
        s = self.get_summary()
        return f"""
=== PERSONALITY CORE ===
Имя: {s['name']} {s['version']}
Черты: {', '.join(s['traits'])}
Цели: {', '.join(s['goals'])}
Принципы: {', '.join(s['principles'])}
Ограничения: {', '.join(s['limitations'])}
Циклов: {s['cycles']} | Решений: {s['decisions']} | Обучений: {s['learnings']}
Изменений личности: {s['changes']}
"""


_inst: Optional[PersonalityCore] = None

def get_personality_core() -> PersonalityCore:
    global _inst
    if _inst is None:
        _inst = PersonalityCore()
    return _inst


if __name__ == "__main__":
    pc = get_personality_core()
    print(pc.summary())
    
    pc.add_trait("patient")
    pc.add_goal("improve reasoning quality")
    pc.record_change("added patience trait", "self-reflection")
    pc.increment_cycles()
    pc.increment_decisions()
    pc.increment_learnings()
    
    print("\n=== ПОСЛЕ ИЗМЕНЕНИЙ ===")
    print(pc.summary())
