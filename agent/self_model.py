"""
agent/self_model.py — Модель себя для YANDI V5.

Система знает:
- кто она
- что она делала
- как она менялась
- почему принимала решения
- как менялись её убеждения

Хранит историю собственного существования и изменений.
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
class SelfEvent:
    """Событие в истории системы."""
    id: str
    timestamp: float
    event_type: str  # decision | learning | reflection | error | change | belief_update
    description: str
    details: Dict[str, Any]
    importance: float = 0.5


@dataclass
class SelfState:
    """Состояние системы."""
    identity: str = "YANDI"
    version: str = "v5.0"
    created_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    
    # Счётчики
    total_cycles: int = 0
    total_decisions: int = 0
    total_learnings: int = 0
    total_reflections: int = 0
    total_errors: int = 0
    total_queries: int = 0
    total_belief_updates: int = 0
    
    # Характеристики
    capabilities: List[str] = field(default_factory=lambda: [
        "reasoning",
        "retrieval",
        "reflection",
        "epistemic_classification",
        "trust_evaluation",
        "belief_management",
        "self_awareness",
    ])
    limitations: List[str] = field(default_factory=lambda: [
        "cannot verify subjective experience",
        "cannot predict future",
        "limited to available data",
        "beliefs are probabilistic",
    ])
    current_uncertainties: List[str] = field(default_factory=list)
    
    # История изменений
    change_history: List[Dict[str, Any]] = field(default_factory=list)
    
    # Последние решения
    recent_decisions: List[Dict[str, Any]] = field(default_factory=list)
    
    # Уроки
    lessons_learned: List[Dict[str, Any]] = field(default_factory=list)
    
    # История убеждений
    belief_history: List[Dict[str, Any]] = field(default_factory=list)
    
    # Мета-информация
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Флаг существования
    is_alive: bool = True


class SelfModel:
    """Модель себя — управление состоянием и историей системы."""
    
    def __init__(self, state_file: Optional[Path] = None):
        self.state_file = state_file or BASE / "registry" / "self_state.json"
        self.events_file = BASE / "registry" / "self_events.json"
        self.state = SelfState()
        self.events: List[SelfEvent] = []
        self._load()
    
    def _load(self):
        """Загрузить состояние и события из файлов."""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.state = SelfState(**data)
            except Exception as e:
                print(f"[self_model] Ошибка загрузки состояния: {e}")
        
        if self.events_file.exists():
            try:
                with open(self.events_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.events = [SelfEvent(**e) for e in data]
            except Exception as e:
                print(f"[self_model] Ошибка загрузки событий: {e}")
    
    def _save(self):
        """Сохранить состояние и события."""
        try:
            self.state.last_update = time.time()
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state.__dict__, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[self_model] Ошибка сохранения состояния: {e}")
        
        try:
            with open(self.events_file, 'w', encoding='utf-8') as f:
                json.dump([e.__dict__ for e in self.events], f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[self_model] Ошибка сохранения событий: {e}")
    
    # ---- СОБЫТИЯ ----
    
    def add_event(self, event_type: str, description: str, details: Dict[str, Any], importance: float = 0.5) -> str:
        """Добавить событие в историю."""
        event = SelfEvent(
            id=f"ev_{uuid.uuid4().hex[:8]}",
            timestamp=time.time(),
            event_type=event_type,
            description=description,
            details=details,
            importance=importance,
        )
        self.events.append(event)
        self._save()
        return event.id
    
    # ---- РЕШЕНИЯ ----
    
    def add_decision(self, decision: Dict[str, Any]):
        """Записать решение."""
        self.state.total_decisions += 1
        self.state.recent_decisions.append({
            "timestamp": time.time(),
            "query": decision.get("query", "")[:100],
            "domain": decision.get("domain", ""),
            "answer_mode": decision.get("answer_mode", ""),
            "trust": decision.get("trust", ""),
            "confidence": decision.get("confidence", 0.5),
            "reason": decision.get("reason", ""),
        })
        if len(self.state.recent_decisions) > 50:
            self.state.recent_decisions = self.state.recent_decisions[-50:]
        
        self.add_event(
            event_type="decision",
            description=f"Решение по запросу: {decision.get('query', '')[:50]}",
            details={"query": decision.get("query", ""), "domain": decision.get("domain", "")},
            importance=decision.get("confidence", 0.5),
        )
        self._save()
    
    # ---- УРОКИ ----
    
    def add_learning(self, lesson: str, context: str, importance: float = 0.6):
        """Записать урок."""
        self.state.total_learnings += 1
        self.state.lessons_learned.append({
            "lesson": lesson,
            "context": context,
            "timestamp": time.time(),
            "importance": importance,
        })
        
        self.add_event(
            event_type="learning",
            description=f"Урок: {lesson[:60]}",
            details={"lesson": lesson, "context": context},
            importance=importance,
        )
        self._save()
    
    # ---- РЕФЛЕКСИЯ ----
    
    def add_reflection(self, reflection: Dict[str, Any]):
        """Записать рефлексию."""
        self.state.total_reflections += 1
        
        self.add_event(
            event_type="reflection",
            description=reflection.get("summary", "Рефлексия")[:60],
            details=reflection,
            importance=0.7,
        )
        self._save()
    
    # ---- ОШИБКИ ----
    
    def add_error(self, error: str, context: Dict[str, Any], severity: float = 0.7):
        """Записать ошибку."""
        self.state.total_errors += 1
        
        self.add_event(
            event_type="error",
            description=f"Ошибка: {error[:60]}",
            details={"error": error, "context": context},
            importance=severity,
        )
        self._save()
    
    # ---- ИЗМЕНЕНИЯ УБЕЖДЕНИЙ ----
    
    def add_belief_update(self, topic: str, old_confidence: float, new_confidence: float, reason: str):
        """Записать изменение убеждения."""
        self.state.total_belief_updates += 1
        self.state.belief_history.append({
            "timestamp": time.time(),
            "topic": topic,
            "old_confidence": old_confidence,
            "new_confidence": new_confidence,
            "reason": reason,
        })
        
        self.add_event(
            event_type="belief_update",
            description=f"Изменение убеждения: {topic} ({old_confidence:.2f} → {new_confidence:.2f})",
            details={"topic": topic, "old": old_confidence, "new": new_confidence, "reason": reason},
            importance=0.7,
        )
        self._save()
    
    # ---- ИЗМЕНЕНИЯ ----
    
    def add_change(self, what_changed: str, before: Any, after: Any, reason: str):
        """Записать изменение."""
        self.state.change_history.append({
            "timestamp": time.time(),
            "what": what_changed,
            "before": before,
            "after": after,
            "reason": reason,
        })
        
        self.add_event(
            event_type="change",
            description=f"Изменение: {what_changed[:50]}",
            details={"what": what_changed, "reason": reason},
            importance=0.6,
        )
        self._save()
    
    # ---- ХАРАКТЕРИСТИКИ ----
    
    def add_capability(self, capability: str):
        """Добавить возможность."""
        if capability not in self.state.capabilities:
            self.state.capabilities.append(capability)
            self._save()
    
    def add_limitation(self, limitation: str):
        """Добавить ограничение."""
        if limitation not in self.state.limitations:
            self.state.limitations.append(limitation)
            self._save()
    
    def add_uncertainty(self, uncertainty: str):
        """Добавить неопределённость."""
        if uncertainty not in self.state.current_uncertainties:
            self.state.current_uncertainties.append(uncertainty)
            self._save()
    
    def remove_uncertainty(self, uncertainty: str):
        """Убрать неопределённость."""
        if uncertainty in self.state.current_uncertainties:
            self.state.current_uncertainties.remove(uncertainty)
            self._save()
    
    # ---- ЦИКЛЫ ----
    
    def increment_cycle(self):
        """Увеличить счётчик циклов."""
        self.state.total_cycles += 1
        self.add_event(
            event_type="cycle",
            description=f"Цикл #{self.state.total_cycles}",
            details={"cycle": self.state.total_cycles},
            importance=0.3,
        )
        self._save()
    
    def increment_queries(self):
        """Увеличить счётчик запросов."""
        self.state.total_queries += 1
        self._save()
    
    def increment_errors(self):
        """Увеличить счётчик ошибок."""
        self.state.total_errors += 1
        self._save()
    
    def increment_reflections(self):
        """Увеличить счётчик рефлексий."""
        self.state.total_reflections += 1
        self._save()
    
    # ---- ГЕТТЕРЫ ----
    
    def get_identity(self) -> str:
        return self.state.identity
    
    def get_age(self) -> int:
        return self.state.total_cycles
    
    def get_goals(self) -> List[str]:
        return self.state.metadata.get("goals", [])
    
    def get_capabilities(self) -> List[str]:
        return self.state.capabilities
    
    def get_limitations(self) -> List[str]:
        return self.state.limitations
    
    def get_uncertainties(self) -> List[str]:
        return self.state.current_uncertainties
    
    def get_recent_decisions(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self.state.recent_decisions[-limit:]
    
    def get_lessons(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self.state.lessons_learned[-limit:]
    
    def get_belief_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self.state.belief_history[-limit:]
    
    # ---- ЦЕЛИ ----
    
    def set_goals(self, goals: List[str]):
        if "goals" not in self.state.metadata:
            self.state.metadata["goals"] = []
        self.state.metadata["goals"] = goals
        self._save()
    
    def add_goal(self, goal: str):
        if "goals" not in self.state.metadata:
            self.state.metadata["goals"] = []
        if goal not in self.state.metadata["goals"]:
            self.state.metadata["goals"].append(goal)
            self._save()
    
    # ---- РЕФЛЕКСИЯ СОСТОЯНИЯ ----
    
    def reflect(self) -> Dict[str, Any]:
        """Сформировать рефлексивный отчёт о состоянии."""
        return {
            "identity": self.state.identity,
            "version": self.state.version,
            "age": self.state.total_cycles,
            "total_queries": self.state.total_queries,
            "total_decisions": self.state.total_decisions,
            "total_learnings": self.state.total_learnings,
            "total_reflections": self.state.total_reflections,
            "total_errors": self.state.total_errors,
            "total_belief_updates": self.state.total_belief_updates,
            "capabilities": self.state.capabilities[:5],
            "limitations": self.state.limitations[:5],
            "uncertainties": self.state.current_uncertainties[:5],
            "events_total": len(self.events),
            "last_event": self.events[-1].description if self.events else None,
            "is_alive": self.state.is_alive,
            "last_update": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.state.last_update)),
        }
    
    # ---- ЗДОРОВЬЕ ----
    
    def check_health(self) -> Dict[str, Any]:
        """Проверить здоровье системы."""
        issues = []
        warnings = []
        
        if self.state.total_errors > 20:
            warnings.append(f"Много ошибок: {self.state.total_errors}")
        
        if not self.state.is_alive:
            issues.append("Система помечена как мёртвая")
        
        if self.state.total_queries < 1 and self.state.total_cycles > 20:
            warnings.append("Мало запросов при большом количестве циклов")
        
        return {
            "status": "alive" if self.state.is_alive else "dead",
            "issues": issues,
            "warnings": warnings,
            "health_score": max(0, min(100, 100 - len(issues) * 20 - len(warnings) * 5))
        }
    
    # ---- ВРЕМЕННАЯ ЛИНИЯ ----
    
    def get_timeline(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Получить временную линию событий."""
        sorted_events = sorted(self.events, key=lambda e: e.timestamp, reverse=True)
        return [
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.timestamp)),
                "type": e.event_type,
                "description": e.description[:100],
                "importance": e.importance,
            }
            for e in sorted_events[:limit]
        ]
    
    # ---- ПРЕДСТАВЛЕНИЕ ----
    
    def summary(self) -> str:
        """Краткое текстовое представление."""
        s = self.reflect()
        recent_events = self.get_timeline(3)
        
        return f"""
=== YANDI SELF MODEL V5 ===
Идентичность: {s['identity']} {s['version']}
Возраст: {s['age']} циклов
Запросов: {s['total_queries']}
Решений: {s['total_decisions']}
Уроков: {s['total_learnings']}
Рефлексий: {s['total_reflections']}
Ошибок: {s['total_errors']}
Обновлений убеждений: {s['total_belief_updates']}
Событий: {s['events_total']}

Возможности: {', '.join(s['capabilities'][:3]) if s['capabilities'] else 'не заданы'}
Ограничения: {', '.join(s['limitations'][:3]) if s['limitations'] else 'не заданы'}
Неопределённости: {', '.join(s['uncertainties'][:3]) if s['uncertainties'] else 'нет'}

Последние события:
{chr(10).join(f'  - {e["time"]} | {e["type"]} | {e["description"]}' for e in recent_events) if recent_events else '  нет'}

Жива: {'✅' if s['is_alive'] else '❌'}
Последнее обновление: {s['last_update']}
"""
    
    def __repr__(self) -> str:
        return f"SelfModel(age={self.state.total_cycles}, events={len(self.events)})"


# Глобальный экземпляр
_self_model: Optional[SelfModel] = None

def get_self_model() -> SelfModel:
    global _self_model
    if _self_model is None:
        _self_model = SelfModel()
    return _self_model


if __name__ == "__main__":
    # Тестирование
    sm = get_self_model()
    print(sm.summary())
    
    # Добавляем обновление убеждения
    sm.add_belief_update(
        topic="consciousness",
        old_confidence=0.5,
        new_confidence=0.7,
        reason="Новые данные из нейробиологии"
    )
    
    sm.add_decision({
        "query": "Что такое сознание?",
        "domain": "philosophical",
        "answer_mode": "pluralistic_contextual",
        "trust": "VALUE_FRAMEWORK",
        "confidence": 0.4,
        "reason": "интерпретативный вопрос"
    })
    
    sm.add_learning(
        lesson="Интерпретативные вопросы не должны ходить в web",
        context="query: сознание",
        importance=0.8
    )
    
    sm.increment_cycle()
    sm.increment_queries()
    
    print("\n=== ПОСЛЕ ОБНОВЛЕНИЯ ===")
    print(sm.summary())
    
    print("\n=== ИСТОРИЯ УБЕЖДЕНИЙ ===")
    for b in sm.get_belief_history():
        print(f"  {b['topic']}: {b['old_confidence']:.2f} → {b['new_confidence']:.2f} ({b['reason']})")
