"""
agent/memory_episodic.py — Эпизодическая память для YANDI V3.

Хранит события и решения системы:
- запросы и ответы
- выбранные маршруты
- ошибки и уроки
- рефлексии

Не просто факты, а жизненный опыт системы.

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/episodic_memory.json is
retired, not migrated — old JSON was disposable test-era cruft. State
now lives exclusively in episode (class B, append-only) — agent/db/sql/
schema.py. trim()/clear() are RETIRED entirely (neither had a real
production caller, confirmed via grep before this rewrite) — this
system's own life experience is never pruned, only ever added to, same
discipline already applied to belief_assessment_history/claim_
occurrence/decision_event/grievance.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here. There is no JSON fallback left to quietly succeed against.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


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


def _dt_to_unix(value) -> float:
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    return value.timestamp()


def _row_to_episode(row: Dict[str, Any]) -> Episode:
    return Episode(
        id=row["episode_id"],
        timestamp=_dt_to_unix(row.get("created_at")),
        event_type=row["event_type"],
        summary=row["summary"],
        details=row.get("details") or {},
        importance=row.get("importance", 0.5),
        tags=row.get("tags") or [],
        related_episodes=row.get("related_episodes") or [],
    )


class EpisodicMemory:
    """Эпизодическая память — хранение событий жизни системы."""

    def add(self, event_type: str, summary: str, details: Dict[str, Any],
            importance: float = 0.5, tags: Optional[List[str]] = None) -> str:
        """Добавить новый эпизод."""
        episode_id = f"ep_{uuid.uuid4().hex[:12]}"
        with get_connection() as conn:
            repo.record_episode(
                conn, episode_id, event_type, summary, details=details,
                importance=min(1.0, max(0.0, importance)), tags=tags or [],
            )
            conn.commit()
        return episode_id

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
        """Получить последние `limit` эпизодов данного типа."""
        with get_connection() as conn:
            rows = repo.get_episodes_by_type(conn, event_type, limit=limit)
        return [_row_to_episode(r) for r in rows]

    def get_by_tag(self, tag: str, limit: int = 20) -> List[Episode]:
        """Получить эпизоды по тегу."""
        with get_connection() as conn:
            rows = repo.get_episodes_by_tag(conn, tag, limit=limit)
        return [_row_to_episode(r) for r in rows]

    def get_by_importance(self, min_importance: float = 0.7, limit: int = 20) -> List[Episode]:
        """Получить самые важные эпизоды."""
        with get_connection() as conn:
            rows = repo.get_episodes_by_importance(conn, min_importance=min_importance, limit=limit)
        return [_row_to_episode(r) for r in rows]

    def get_recent(self, limit: int = 20) -> List[Episode]:
        """Получить последние эпизоды."""
        with get_connection() as conn:
            rows = repo.get_recent_episodes(conn, limit=limit)
        return [_row_to_episode(r) for r in rows]

    def get_timeline(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Получить временную линию событий."""
        from datetime import datetime
        return [
            {
                "id": e.id,
                "time": datetime.fromtimestamp(e.timestamp).isoformat(),
                "event_type": e.event_type,
                "summary": e.summary,
                "importance": e.importance
            }
            for e in self.get_recent(limit)
        ]

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику памяти."""
        with get_connection() as conn:
            return repo.get_episode_stats(conn)

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
