"""
agent/personality_core.py — Ядро личности для YANDI V6.

Устойчивые состояния системы:
- знания (убеждения)
- цели
- ограничения
- предпочтения
- история

Цель: система имеет идентичность, а не просто состояние.

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/personality.json is
retired, not migrated — old JSON was disposable test-era cruft. State
now lives exclusively in personality (class C, one singleton row) and
personality_change (class B, append-only) — agent/db/sql/schema.py.
No caching of the row in this class between calls (same discipline as
agent/belief_manager.py's own "точка ноль" rewrite): every getter opens
a fresh connection, so callers always see the current committed state,
never a stale snapshot held across an unrelated write from elsewhere.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here. There is no JSON fallback left to quietly succeed against.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo

DEFAULT_TRAITS = ["curious", "cautious", "honest", "reflective", "adaptive"]
DEFAULT_GOALS = [
    "understand the world",
    "avoid misinformation",
    "help users effectively",
    "learn from mistakes",
]
DEFAULT_PRINCIPLES = [
    "never lie",
    "admit uncertainty",
    "separate facts from interpretations",
    "learn from evidence",
]
DEFAULT_LIMITATIONS = [
    "cannot verify subjective experience",
    "cannot predict future with certainty",
    "limited to available data",
]
DEFAULT_PREFERENCES = {
    "reasoning_style": "balanced",
    "response_style": "clear_and_honest",
    "risk_tolerance": 0.3,
    "curiosity_level": 0.7,
}


class PersonalityCore:
    """Ядро личности — управляет устойчивыми состояниями."""

    def __init__(self):
        with get_connection() as conn:
            repo.get_or_create_personality(
                conn, name="YANDI", version="v6.0",
                traits=DEFAULT_TRAITS, goals=DEFAULT_GOALS,
                principles=DEFAULT_PRINCIPLES, limitations=DEFAULT_LIMITATIONS,
                preferences=DEFAULT_PREFERENCES,
            )
            conn.commit()

    def _row(self) -> Dict[str, Any]:
        with get_connection() as conn:
            return repo.get_personality(conn)

    def get_name(self) -> str:
        return self._row()["name"]

    def get_traits(self) -> List[str]:
        return self._row()["traits"]

    def get_goals(self) -> List[str]:
        return self._row()["goals"]

    def get_principles(self) -> List[str]:
        return self._row()["principles"]

    def add_trait(self, trait: str):
        row = self._row()
        if trait not in row["traits"]:
            with get_connection() as conn:
                repo.update_personality_lists(conn, traits=row["traits"] + [trait])
                conn.commit()

    def add_goal(self, goal: str):
        row = self._row()
        if goal not in row["goals"]:
            with get_connection() as conn:
                repo.update_personality_lists(conn, goals=row["goals"] + [goal])
                conn.commit()

    def add_principle(self, principle: str):
        row = self._row()
        if principle not in row["principles"]:
            with get_connection() as conn:
                repo.update_personality_lists(conn, principles=row["principles"] + [principle])
                conn.commit()

    def add_limitation(self, limitation: str):
        row = self._row()
        if limitation not in row["limitations"]:
            with get_connection() as conn:
                repo.update_personality_lists(conn, limitations=row["limitations"] + [limitation])
                conn.commit()

    def record_change(self, what_changed: str, reason: str):
        """Записать изменение личности."""
        with get_connection() as conn:
            repo.record_personality_change(conn, what_changed, reason)
            conn.commit()

    def increment_cycles(self):
        with get_connection() as conn:
            repo.increment_personality_counter(conn, "total_cycles")
            conn.commit()

    def increment_decisions(self):
        with get_connection() as conn:
            repo.increment_personality_counter(conn, "total_decisions")
            conn.commit()

    def increment_learnings(self):
        with get_connection() as conn:
            repo.increment_personality_counter(conn, "total_learnings")
            conn.commit()

    def get_summary(self) -> Dict[str, Any]:
        row = self._row()
        with get_connection() as conn:
            changes = repo.count_personality_changes(conn)
        return {
            "name": row["name"],
            "version": row["version"],
            "traits": row["traits"],
            "goals": row["goals"][:3],
            "principles": row["principles"][:3],
            "limitations": row["limitations"][:3],
            "cycles": row["total_cycles"],
            "decisions": row["total_decisions"],
            "learnings": row["total_learnings"],
            "changes": changes,
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
