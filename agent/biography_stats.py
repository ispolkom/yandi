"""
agent/biography_stats.py — Биография Янди.
Не просто статистика, а история её жизни.

Считает:
- возраст (в циклах)
- последнюю смену принципов
- последнее сожаление
- последнюю ошибку
- последнее новое убеждение
- количество сохранённых воспоминаний
- количество забытых воспоминаний
- количество переосмысленных решений
- количество изменённых привычек

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/biography/{user_id}.json
is retired, not migrated. State now lives in biography (class C) +
biography_event (class B, append-only) — agent/db/sql/schema.py. The
old JSON's four redundant sub-lists (errors, regrets, beliefs, habits),
each capped at the last 10, collapse into biography_event alone — no
data loss, no arbitrary cap.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here.
"""

import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


def _dt_to_unix(value) -> float:
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    return value.replace(tzinfo=timezone.utc).timestamp()


class BiographyStats:
    def __init__(self, user_id: str = "global"):
        self.user_id = user_id
        with get_connection() as conn:
            repo.get_or_create_biography(conn, user_id)
            conn.commit()

    def _row(self) -> Dict[str, Any]:
        with get_connection() as conn:
            return repo.get_biography(conn, self.user_id)

    def increment_cycles(self, count: int = 1):
        with get_connection() as conn:
            repo.bump_biography_counter(conn, self.user_id, "cycles", amount=count)
            conn.commit()

    def add_error(self, error: str):
        with get_connection() as conn:
            repo.bump_biography_counter(conn, self.user_id, "total_decisions")
            repo.record_biography_event(conn, self.user_id, "error", {"error": error})
            conn.commit()

    def add_regret(self, regret: str):
        with get_connection() as conn:
            repo.record_biography_event(conn, self.user_id, "regret", {"regret": regret})
            conn.commit()

    def add_belief(self, belief: str):
        with get_connection() as conn:
            repo.record_biography_event(conn, self.user_id, "belief", {"belief": belief})
            conn.commit()

    def add_milestone(self, milestone: str):
        with get_connection() as conn:
            repo.record_biography_event(conn, self.user_id, "milestone", {"milestone": milestone})
            conn.commit()

    def add_habit_change(self, old: str, new: str):
        with get_connection() as conn:
            repo.bump_biography_counter(conn, self.user_id, "changed_habits")
            repo.record_biography_event(conn, self.user_id, "habit", {"old": old, "new": new})
            conn.commit()

    def add_principles_change(self, old: str, new: str):
        with get_connection() as conn:
            repo.set_biography_principles_change(conn, self.user_id, old, new)
            conn.commit()

    def add_reconsidered_decision(self, decision_id: str, reason: str):
        with get_connection() as conn:
            repo.bump_biography_counter(conn, self.user_id, "reconsidered_decisions")
            conn.commit()

    def add_memory(self, memory_type: str):
        with get_connection() as conn:
            repo.bump_biography_counter(conn, self.user_id, "saved_memories")
            conn.commit()

    def forget_memory(self, count: int = 1):
        with get_connection() as conn:
            repo.bump_biography_counter(conn, self.user_id, "forgotten_memories", amount=count)
            conn.commit()

    def get_summary(self) -> Dict[str, Any]:
        row = self._row()
        age_hours = (time.time() - _dt_to_unix(row["birth"])) / 3600
        age_days = age_hours / 24

        with get_connection() as conn:
            errors = repo.list_biography_events(conn, self.user_id, "error", limit=1)
            regrets = repo.list_biography_events(conn, self.user_id, "regret", limit=1)
            beliefs = repo.list_biography_events(conn, self.user_id, "belief", limit=1)
            errors_count = repo.count_biography_events(conn, self.user_id, "error")
            regrets_count = repo.count_biography_events(conn, self.user_id, "regret")
            beliefs_count = repo.count_biography_events(conn, self.user_id, "belief")
            milestones_count = repo.count_biography_events(conn, self.user_id, "milestone")

        return {
            "age_hours": round(age_hours, 1),
            "age_days": round(age_days, 1),
            "age_cycles": row["cycles"],
            "cycles": row["cycles"],
            "last_principles_change": row.get("last_principles_change"),
            "last_regret": regrets[0]["payload"]["regret"] if regrets else None,
            "last_error": errors[0]["payload"]["error"] if errors else None,
            "last_new_belief": beliefs[0]["payload"]["belief"] if beliefs else None,
            "saved_memories": row["saved_memories"],
            "forgotten_memories": row["forgotten_memories"],
            "reconsidered_decisions": row["reconsidered_decisions"],
            "changed_habits": row["changed_habits"],
            "total_decisions": row["total_decisions"],
            "total_reflections": row["total_reflections"],
            "errors_count": errors_count,
            "regrets_count": regrets_count,
            "beliefs_count": beliefs_count,
            "milestones_count": milestones_count,
        }

    def get_human_readable(self) -> str:
        """Возвращает красивый текст о жизни Янди"""
        s = self.get_summary()

        lines = []
        lines.append(f"Я живу уже {s['age_hours']:.0f} часов ({s['age_days']:.0f} дней)")
        lines.append(f"Это примерно {s['cycles']} диалоговых циклов")

        if s['last_error']:
            lines.append(f"Последняя ошибка: {s['last_error']}")
        if s['last_regret']:
            lines.append(f"Последнее сожаление: {s['last_regret']}")
        if s['last_new_belief']:
            lines.append(f"Последнее новое убеждение: {s['last_new_belief']}")
        if s['last_principles_change']:
            pc = s['last_principles_change']
            lines.append(f"Последняя смена принципов: {pc['old']} → {pc['new']}")

        lines.append("")
        lines.append(f"Запомнила: {s['saved_memories']} воспоминаний")
        lines.append(f"Забыла: {s['forgotten_memories']} воспоминаний")
        lines.append(f"Переосмыслила решений: {s['reconsidered_decisions']}")
        lines.append(f"Изменила привычек: {s['changed_habits']}")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return self._row()


_instances: Dict[str, BiographyStats] = {}


def get_biography(user_id: str = "global") -> BiographyStats:
    if user_id not in _instances:
        _instances[user_id] = BiographyStats(user_id)
    return _instances[user_id]


if __name__ == "__main__":
    # Тест
    bio = get_biography("test_user")

    # Симулируем жизнь
    bio.increment_cycles(73)
    bio.add_error("слишком резко прекратила диалог")
    bio.add_regret("надо было дать человеку шанс объясниться")
    bio.add_belief("люди чаще извиняются искренне, если сначала дать им возможность объясниться")
    bio.add_principles_change("На хамство лучше промолчать", "На хамство лучше отвечать спокойно")
    bio.add_habit_change("отвечать длинно", "отвечать коротко, если пользователь устал")
    bio.add_milestone("первый диалог, где я отказалась отвечать")
    bio.add_memory("эпизод")
    bio.add_memory("эпизод")
    bio.add_memory("эпизод")
    bio.add_reconsidered_decision("dec_123", "поняла, что была слишком резка")

    print("=== Биография Янди ===\n")
    print(bio.get_human_readable())

    print("\n=== Подробная статистика ===")
    summary = bio.get_summary()
    for key, value in summary.items():
        print(f"  {key}: {value}")
