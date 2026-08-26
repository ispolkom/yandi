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
"""

import time
import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

BASE = Path(__file__).parent.parent
BIOGRAPHY_DIR = BASE / "registry" / "biography"
BIOGRAPHY_DIR.mkdir(parents=True, exist_ok=True)


class BiographyStats:
    def __init__(self, user_id: str = "global"):
        self.user_id = user_id
        self.file_path = BIOGRAPHY_DIR / f"{user_id}.json"
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[Biography] Ошибка загрузки: {e}")
        return self._default_data()

    def _default_data(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "birth": time.time(),
            "last_updated": time.time(),
            "cycles": 0,
            "last_principles_change": None,
            "last_regret": None,
            "last_error": None,
            "last_new_belief": None,
            "saved_memories": 0,
            "forgotten_memories": 0,
            "reconsidered_decisions": 0,
            "changed_habits": 0,
            "total_decisions": 0,
            "total_reflections": 0,
            "errors": [],
            "regrets": [],
            "beliefs": [],
            "habits": [],
            "milestones": [],
        }

    def _save(self):
        self.data["last_updated"] = time.time()
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Biography] Ошибка сохранения: {e}")

    def increment_cycles(self, count: int = 1):
        self.data["cycles"] += count
        self._save()

    def add_error(self, error: str):
        self.data["errors"].append({
            "timestamp": time.time(),
            "error": error,
        })
        self.data["last_error"] = error
        self.data["total_decisions"] += 1
        # Если ошибок больше 10, забываем старые
        if len(self.data["errors"]) > 10:
            forgotten = len(self.data["errors"]) - 10
            self.data["forgotten_memories"] += forgotten
            self.data["errors"] = self.data["errors"][-10:]
        self._save()

    def add_regret(self, regret: str):
        self.data["regrets"].append({
            "timestamp": time.time(),
            "regret": regret,
        })
        self.data["last_regret"] = regret
        if len(self.data["regrets"]) > 10:
            self.data["regrets"] = self.data["regrets"][-10:]
        self._save()

    def add_belief(self, belief: str):
        self.data["beliefs"].append({
            "timestamp": time.time(),
            "belief": belief,
        })
        self.data["last_new_belief"] = belief
        if len(self.data["beliefs"]) > 10:
            self.data["beliefs"] = self.data["beliefs"][-10:]
        self._save()

    def add_milestone(self, milestone: str):
        self.data["milestones"].append({
            "timestamp": time.time(),
            "milestone": milestone,
        })
        self._save()

    def add_habit_change(self, old: str, new: str):
        self.data["changed_habits"] += 1
        self.data["habits"].append({
            "timestamp": time.time(),
            "old": old,
            "new": new,
        })
        if len(self.data["habits"]) > 10:
            self.data["habits"] = self.data["habits"][-10:]
        self._save()

    def add_principles_change(self, old: str, new: str):
        self.data["last_principles_change"] = {
            "timestamp": time.time(),
            "old": old,
            "new": new,
        }
        self._save()

    def add_reconsidered_decision(self, decision_id: str, reason: str):
        self.data["reconsidered_decisions"] += 1
        self._save()

    def add_memory(self, memory_type: str):
        self.data["saved_memories"] += 1
        self._save()

    def forget_memory(self, count: int = 1):
        self.data["forgotten_memories"] += count
        self._save()

    def get_summary(self) -> Dict[str, Any]:
        data = self.data
        age_hours = (time.time() - data["birth"]) / 3600
        age_days = age_hours / 24
        age_cycles = data["cycles"]

        return {
            "age_hours": round(age_hours, 1),
            "age_days": round(age_days, 1),
            "age_cycles": age_cycles,
            "cycles": data["cycles"],
            "last_principles_change": data.get("last_principles_change"),
            "last_regret": data.get("last_regret"),
            "last_error": data.get("last_error"),
            "last_new_belief": data.get("last_new_belief"),
            "saved_memories": data["saved_memories"],
            "forgotten_memories": data["forgotten_memories"],
            "reconsidered_decisions": data["reconsidered_decisions"],
            "changed_habits": data["changed_habits"],
            "total_decisions": data["total_decisions"],
            "total_reflections": data["total_reflections"],
            "errors_count": len(data.get("errors", [])),
            "regrets_count": len(data.get("regrets", [])),
            "beliefs_count": len(data.get("beliefs", [])),
            "milestones_count": len(data.get("milestones", [])),
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
        return self.data


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
