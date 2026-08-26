"""
agent/memory_episodic.py — Эпизодическая память для YANDI V3.

Хранит события и решения системы:
- запросы и ответы
- выбранные маршруты
- ошибки и уроки
- рефлексии

Не просто факты, а жизненный опыт системы.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

BASE = Path(__file__).parent.parent
EPISODIC_FILE = BASE / "registry" / "episodic_memory.json"


@dataclass
class Episode:
    """Один эпизод в памяти системы."""
    id: str
    timestamp: float
    event_type: str  # query | decision | error | reflection | learning | action
    summary: str
    details: Dict[str, Any]
    importance: float = 0.5
    tags: List[str] = field(default_factory=list)
    related_episodes: List[str] = field(default_factory=list)


class EpisodicMemory:
    """Эпизодическая память — хранение событий жизни системы."""
    
    def __init__(self, memory_file: Optional[Path] = None):
        self.memory_file = memory_file or EPISODIC_FILE
        self.episodes: List[Episode] = self._load()
    
    def _load(self) -> List[Episode]:
        """Загрузить эпизоды из файла."""
        if self.memory_file.exists():
            try:
                with open(self.memory_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return [Episode(**e) for e in data]
            except Exception as e:
                print(f"[episodic_memory] Ошибка загрузки: {e}")
                return []
        return []
    
    def _save(self):
        """Сохранить эпизоды в файл."""
        try:
            data = [e.__dict__ for e in self.episodes]
            with open(self.memory_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[episodic_memory] Ошибка сохранения: {e}")
    
    def add(self, event_type: str, summary: str, details: Dict[str, Any], 
            importance: float = 0.5, tags: Optional[List[str]] = None) -> str:
        """Добавить новый эпизод."""
        episode = Episode(
            id=f"ep_{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            event_type=event_type,
            summary=summary,
            details=details,
            importance=min(1.0, max(0.0, importance)),
            tags=tags or []
        )
        self.episodes.append(episode)
        self._save()
        return episode.id
    
    def add_query(self, query: str, domain: str, answer_mode: str, 
                  trust: str, confidence: float) -> str:
        """Добавить эпизод запроса."""
        return self.add(
            event_type="query",
            summary=f"Запрос: {query[:60]}",
            details={
                "query": query,
                "domain": domain,
                "answer_mode": answer_mode,
                "trust": trust,
                "confidence": confidence,
            },
            importance=confidence,
            tags=[domain, answer_mode]
        )
    
    def add_decision(self, decision_type: str, reason: str, 
                     details: Dict[str, Any], importance: float = 0.5) -> str:
        """Добавить эпизод решения."""
        return self.add(
            event_type="decision",
            summary=f"Решение: {decision_type} — {reason[:40]}",
            details=details,
            importance=importance,
            tags=["decision", decision_type]
        )
    
    def add_error(self, error: str, context: Dict[str, Any], 
                  severity: float = 0.7) -> str:
        """Добавить эпизод ошибки."""
        return self.add(
            event_type="error",
            summary=f"Ошибка: {error[:60]}",
            details=context,
            importance=severity,
            tags=["error"]
        )
    
    def add_reflection(self, reflection: Dict[str, Any]) -> str:
        """Добавить эпизод рефлексии."""
        return self.add(
            event_type="reflection",
            summary=reflection.get("summary", "Рефлексия"),
            details=reflection,
            importance=0.8,
            tags=["reflection"]
        )
    
    def add_learning(self, lesson: str, context: Dict[str, Any], 
                     importance: float = 0.6) -> str:
        """Добавить эпизод обучения."""
        return self.add(
            event_type="learning",
            summary=f"Урок: {lesson[:60]}",
            details={**context, "lesson": lesson},
            importance=importance,
            tags=["learning"]
        )
    
    # ---- ПОИСК И АНАЛИЗ ----
    
    def get_by_type(self, event_type: str, limit: int = 20) -> List[Episode]:
        """Получить эпизоды по типу."""
        return [e for e in self.episodes[-limit:] if e.event_type == event_type]
    
    def get_by_tag(self, tag: str, limit: int = 20) -> List[Episode]:
        """Получить эпизоды по тегу."""
        return [e for e in self.episodes[-limit:] if tag in e.tags]
    
    def get_by_importance(self, min_importance: float = 0.7, limit: int = 20) -> List[Episode]:
        """Получить самые важные эпизоды."""
        sorted_eps = sorted(self.episodes, key=lambda e: e.importance, reverse=True)
        return sorted_eps[:limit]
    
    def get_recent(self, limit: int = 20) -> List[Episode]:
        """Получить последние эпизоды."""
        return self.episodes[-limit:]
    
    def get_timeline(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Получить временную линию событий."""
        return [
            {
                "id": e.id,
                "time": datetime.fromtimestamp(e.timestamp).isoformat(),
                "event_type": e.event_type,
                "summary": e.summary,
                "importance": e.importance
            }
            for e in self.episodes[-limit:]
        ]
    
    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику памяти."""
        types = {}
        for e in self.episodes:
            types[e.event_type] = types.get(e.event_type, 0) + 1
        
        total_importance = sum(e.importance for e in self.episodes)
        avg_importance = total_importance / len(self.episodes) if self.episodes else 0
        
        return {
            "total_episodes": len(self.episodes),
            "by_type": types,
            "avg_importance": round(avg_importance, 2),
            "last_episode": self.episodes[-1].timestamp if self.episodes else None,
            "oldest_episode": self.episodes[0].timestamp if self.episodes else None,
        }
    
    def clear(self):
        """Очистить память."""
        self.episodes = []
        self._save()
    
    def trim(self, max_episodes: int = 1000):
        """Обрезать память до указанного размера."""
        if len(self.episodes) > max_episodes:
            # Сохраняем самые важные и последние
            important = sorted(self.episodes, key=lambda e: e.importance, reverse=True)[:max_episodes // 2]
            recent = self.episodes[-max_episodes // 2:]
            merged = {e.id: e for e in important + recent}
            self.episodes = list(merged.values())
            self._save()
    
    def summary(self) -> str:
        """Краткое текстовое представление."""
        stats = self.get_stats()
        recent = self.get_recent(3)
        return f"""
=== ЭПИЗОДИЧЕСКАЯ ПАМЯТЬ ===
Всего эпизодов: {stats['total_episodes']}
Типы: {', '.join(f'{k}={v}' for k, v in stats['by_type'].items())}
Средняя важность: {stats['avg_importance']}
Последние эпизоды:
{chr(10).join(f'  - [{e.event_type}] {e.summary[:60]}' for e in recent)}
"""


# Глобальный экземпляр
_memory: Optional[EpisodicMemory] = None

def get_memory() -> EpisodicMemory:
    global _memory
    if _memory is None:
        _memory = EpisodicMemory()
    return _memory


if __name__ == "__main__":
    # Тестирование
    mem = get_memory()
    print(mem.summary())
    
    # Добавляем тестовые эпизоды
    mem.add_query("Что такое сознание?", "philosophical", "pluralistic_contextual", "VALUE_FRAMEWORK", 0.4)
    mem.add_query("Как полететь на Марс?", "factual", "factual", "EMPIRICALLY_SUPPORTED", 0.7)
    mem.add_decision("epistemic_route", "Выбран pluralistic_contextual", {"domain": "philosophical"}, 0.6)
    mem.add_error("web search timeout", {"query": "Yandi"}, 0.5)
    mem.add_learning("Интерпретативные вопросы не должны ходить в web", {"domain": "philosophical"}, 0.8)
    
    print("\n=== ПОСЛЕ ДОБАВЛЕНИЯ ===")
    print(mem.summary())
    print("\n=== ВРЕМЕННАЯ ЛИНИЯ ===")
    for item in mem.get_timeline(5):
        print(f"  {item['time']} | {item['event_type']} | {item['summary'][:40]}")
    
    print("\n=== СТАТИСТИКА ===")
    print(mem.get_stats())
