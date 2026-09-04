"""
agent/self_model.py — Модель себя для YANDI V5.

Система знает:
- кто она
- что она делала
- как она менялась
- почему принимала решения
- как менялись её убеждения

Хранит историю собственного существования и изменений.

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/self_state.json +
registry/self_events.json are retired, not migrated — old JSON was
disposable test-era cruft. State now lives exclusively in self_state
(class C, one singleton row) and self_event (class B, append-only) —
agent/db/sql/schema.py. The OLD SelfState dataclass also embedded four
separate, redundant, LOSSY sub-histories directly on the state
(recent_decisions — hard-capped at 50, silently dropping older rows;
lessons_learned; belief_history; change_history) even though every one
of those four calls ALSO wrote an equivalent self_event row right next
to it. Collapsed into self_event alone (filtered by event_type) — no
data loss, no second copy to keep in sync, no arbitrary cap on the
system's own decision history.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here. There is no JSON fallback left to quietly succeed against.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo

DEFAULT_CAPABILITIES = [
    "reasoning",
    "retrieval",
    "reflection",
    "epistemic_classification",
    "trust_evaluation",
    "belief_management",
    "self_awareness",
]
DEFAULT_LIMITATIONS = [
    "cannot verify subjective experience",
    "cannot predict future",
    "limited to available data",
    "beliefs are probabilistic",
]


class SelfModel:
    """Модель себя — управление состоянием и историей системы."""

    def __init__(self):
        with get_connection() as conn:
            repo.get_or_create_self_state(
                conn, identity="YANDI", version="v5.0",
                capabilities=DEFAULT_CAPABILITIES, limitations=DEFAULT_LIMITATIONS,
                current_uncertainties=[], metadata={},
            )
            conn.commit()

    def _row(self) -> Dict[str, Any]:
        with get_connection() as conn:
            return repo.get_self_state(conn)

    # ---- СОБЫТИЯ ----

    def add_event(self, event_type: str, description: str, details: Dict[str, Any], importance: float = 0.5) -> str:
        """Добавить событие в историю."""
        event_id = f"ev_{uuid.uuid4().hex[:8]}"
        with get_connection() as conn:
            repo.record_self_event(conn, event_id, event_type, description, details=details, importance=importance)
            conn.commit()
        return event_id

    # ---- РЕШЕНИЯ ----

    def add_decision(self, decision: Dict[str, Any]):
        """Записать решение."""
        with get_connection() as conn:
            repo.increment_self_state_counter(conn, "total_decisions")
            conn.commit()
        self.add_event(
            event_type="decision",
            description=f"Решение по запросу: {decision.get('query', '')[:50]}",
            details={
                "query": decision.get("query", "")[:100],
                "domain": decision.get("domain", ""),
                "answer_mode": decision.get("answer_mode", ""),
                "trust": decision.get("trust", ""),
                "confidence": decision.get("confidence", 0.5),
                "reason": decision.get("reason", ""),
            },
            importance=decision.get("confidence", 0.5),
        )

    # ---- УРОКИ ----

    def add_learning(self, lesson: str, context: str, importance: float = 0.6):
        """Записать урок."""
        with get_connection() as conn:
            repo.increment_self_state_counter(conn, "total_learnings")
            conn.commit()
        self.add_event(
            event_type="learning",
            description=f"Урок: {lesson[:60]}",
            details={"lesson": lesson, "context": context, "importance": importance},
            importance=importance,
        )

    # ---- РЕФЛЕКСИЯ ----

    def add_reflection(self, reflection: Dict[str, Any]):
        """Записать рефлексию."""
        with get_connection() as conn:
            repo.increment_self_state_counter(conn, "total_reflections")
            conn.commit()
        self.add_event(
            event_type="reflection",
            description=reflection.get("summary", "Рефлексия")[:60],
            details=reflection,
            importance=0.7,
        )

    # ---- ОШИБКИ ----

    def add_error(self, error: str, context: Dict[str, Any], severity: float = 0.7):
        """Записать ошибку."""
        with get_connection() as conn:
            repo.increment_self_state_counter(conn, "total_errors")
            conn.commit()
        self.add_event(
            event_type="error",
            description=f"Ошибка: {error[:60]}",
            details={"error": error, "context": context},
            importance=severity,
        )

    # ---- ИЗМЕНЕНИЯ УБЕЖДЕНИЙ ----

    def add_belief_update(self, topic: str, old_confidence: float, new_confidence: float, reason: str):
        """Записать изменение убеждения."""
        with get_connection() as conn:
            repo.increment_self_state_counter(conn, "total_belief_updates")
            conn.commit()
        self.add_event(
            event_type="belief_update",
            description=f"Изменение убеждения: {topic} ({old_confidence:.2f} → {new_confidence:.2f})",
            details={"topic": topic, "old_confidence": old_confidence, "new_confidence": new_confidence, "reason": reason},
            importance=0.7,
        )

    # ---- ИЗМЕНЕНИЯ ----

    def add_change(self, what_changed: str, before: Any, after: Any, reason: str):
        """Записать изменение."""
        self.add_event(
            event_type="change",
            description=f"Изменение: {what_changed[:50]}",
            details={"what": what_changed, "before": before, "after": after, "reason": reason},
            importance=0.6,
        )

    # ---- ХАРАКТЕРИСТИКИ ----

    def add_capability(self, capability: str):
        """Добавить возможность."""
        row = self._row()
        if capability not in row["capabilities"]:
            with get_connection() as conn:
                repo.update_self_state_lists(conn, capabilities=row["capabilities"] + [capability])
                conn.commit()

    def add_limitation(self, limitation: str):
        """Добавить ограничение."""
        row = self._row()
        if limitation not in row["limitations"]:
            with get_connection() as conn:
                repo.update_self_state_lists(conn, limitations=row["limitations"] + [limitation])
                conn.commit()

    def add_uncertainty(self, uncertainty: str):
        """Добавить неопределённость."""
        row = self._row()
        if uncertainty not in row["current_uncertainties"]:
            with get_connection() as conn:
                repo.update_self_state_lists(conn, current_uncertainties=row["current_uncertainties"] + [uncertainty])
                conn.commit()

    def remove_uncertainty(self, uncertainty: str):
        """Убрать неопределённость."""
        row = self._row()
        if uncertainty in row["current_uncertainties"]:
            remaining = [u for u in row["current_uncertainties"] if u != uncertainty]
            with get_connection() as conn:
                repo.update_self_state_lists(conn, current_uncertainties=remaining)
                conn.commit()

    # ---- ЦИКЛЫ ----

    def increment_cycle(self):
        """Увеличить счётчик циклов."""
        with get_connection() as conn:
            repo.increment_self_state_counter(conn, "total_cycles")
            conn.commit()
        row = self._row()
        self.add_event(
            event_type="cycle",
            description=f"Цикл #{row['total_cycles']}",
            details={"cycle": row["total_cycles"]},
            importance=0.3,
        )

    def increment_queries(self):
        """Увеличить счётчик запросов."""
        with get_connection() as conn:
            repo.increment_self_state_counter(conn, "total_queries")
            conn.commit()

    def increment_errors(self):
        """Увеличить счётчик ошибок."""
        with get_connection() as conn:
            repo.increment_self_state_counter(conn, "total_errors")
            conn.commit()

    def increment_reflections(self):
        """Увеличить счётчик рефлексий."""
        with get_connection() as conn:
            repo.increment_self_state_counter(conn, "total_reflections")
            conn.commit()

    # ---- ГЕТТЕРЫ ----

    def get_identity(self) -> str:
        return self._row()["identity"]

    def get_age(self) -> int:
        return self._row()["total_cycles"]

    def get_metadata(self) -> Dict[str, Any]:
        return self._row()["metadata"]

    def set_metadata_value(self, key: str, value: Any):
        """Generic key/value slot on self_state.metadata — replaces
        direct `self.state.metadata[...]` mutation (agent/motivation.py
        was the one real caller of that pattern before this rewrite)."""
        metadata = dict(self._row()["metadata"])
        metadata[key] = value
        with get_connection() as conn:
            repo.update_self_state_lists(conn, metadata=metadata)
            conn.commit()

    def get_goals(self) -> List[str]:
        return self.get_metadata().get("goals", [])

    def get_capabilities(self) -> List[str]:
        return self._row()["capabilities"]

    def get_limitations(self) -> List[str]:
        return self._row()["limitations"]

    def get_uncertainties(self) -> List[str]:
        return self._row()["current_uncertainties"]

    def get_recent_decisions(self, limit: int = 10) -> List[Dict[str, Any]]:
        with get_connection() as conn:
            rows = repo.get_self_events_by_type(conn, "decision", limit=limit)
        return [r["details"] for r in rows]

    def get_lessons(self, limit: int = 10) -> List[Dict[str, Any]]:
        with get_connection() as conn:
            rows = repo.get_self_events_by_type(conn, "learning", limit=limit)
        return [r["details"] for r in rows]

    def get_belief_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        with get_connection() as conn:
            rows = repo.get_self_events_by_type(conn, "belief_update", limit=limit)
        return [r["details"] for r in rows]

    # ---- ЦЕЛИ ----

    def set_goals(self, goals: List[str]):
        self.set_metadata_value("goals", goals)

    def add_goal(self, goal: str):
        goals = self.get_goals()
        if goal not in goals:
            self.set_metadata_value("goals", goals + [goal])

    # ---- РЕФЛЕКСИЯ СОСТОЯНИЯ ----

    def reflect(self) -> Dict[str, Any]:
        """Сформировать рефлексивный отчёт о состоянии."""
        row = self._row()
        with get_connection() as conn:
            events_total = repo.count_self_events(conn)
            recent = repo.get_recent_self_events(conn, limit=1)
        return {
            "identity": row["identity"],
            "version": row["version"],
            "age": row["total_cycles"],
            "total_queries": row["total_queries"],
            "total_decisions": row["total_decisions"],
            "total_learnings": row["total_learnings"],
            "total_reflections": row["total_reflections"],
            "total_errors": row["total_errors"],
            "total_belief_updates": row["total_belief_updates"],
            "capabilities": row["capabilities"][:5],
            "limitations": row["limitations"][:5],
            "uncertainties": row["current_uncertainties"][:5],
            "events_total": events_total,
            "last_event": recent[0]["description"] if recent else None,
            "is_alive": bool(row["is_alive"]),
            "last_update": _fmt_dt(row["updated_at"]),
        }

    # ---- ЗДОРОВЬЕ ----

    def check_health(self) -> Dict[str, Any]:
        """Проверить здоровье системы."""
        row = self._row()
        issues = []
        warnings = []

        if row["total_errors"] > 20:
            warnings.append(f"Много ошибок: {row['total_errors']}")

        if not row["is_alive"]:
            issues.append("Система помечена как мёртвая")

        if row["total_queries"] < 1 and row["total_cycles"] > 20:
            warnings.append("Мало запросов при большом количестве циклов")

        return {
            "status": "alive" if row["is_alive"] else "dead",
            "issues": issues,
            "warnings": warnings,
            "health_score": max(0, min(100, 100 - len(issues) * 20 - len(warnings) * 5))
        }

    # ---- ВРЕМЕННАЯ ЛИНИЯ ----

    def get_timeline(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Получить временную линию событий."""
        with get_connection() as conn:
            rows = repo.get_recent_self_events(conn, limit=limit)
        return [
            {
                "time": _fmt_dt(r["created_at"]),
                "type": r["event_type"],
                "description": r["description"][:100],
                "importance": r["importance"],
            }
            for r in rows
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
        row = self._row()
        with get_connection() as conn:
            events_total = repo.count_self_events(conn)
        return f"SelfModel(age={row['total_cycles']}, events={events_total})"


def _fmt_dt(value) -> str:
    if value is None:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(0))
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


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
