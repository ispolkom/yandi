"""
agent/experience_memory.py — Память опыта.
Хранит, как Янди реагировала на разные ситуации,
чтобы использовать этот опыт в будущем.

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/experiences/{user_id}.json
is retired, not migrated. State now lives in experience (class C —
get_experience()/update_success() legitimately mutate used_count/
success/user_reaction in place) — agent/db/sql/schema.py. No more
silent 200-entry cap.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here.
"""

import time
import uuid
from typing import Dict, List, Optional, Any

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


class ExperienceMemory:
    """
    Хранит опыт Янди — ситуации и её реакции.
    """

    def __init__(self, user_id: str = "global"):
        self.user_id = user_id

    def add_experience(self, speech_act: str, topic: str, query: str,
                       response: str, context: Dict = None) -> str:
        """
        Добавляет новый опыт.
        Возвращает ID опыта.
        """
        exp_id = f"exp_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        with get_connection() as conn:
            repo.create_experience(conn, exp_id, self.user_id, speech_act, topic, query, response, context=context or {})
            conn.commit()
        return exp_id

    def get_experience(self, speech_act: str, topic: str = None) -> Optional[Dict[str, Any]]:
        """
        Возвращает лучший опыт для ситуации.
        """
        with get_connection() as conn:
            experiences = repo.list_experiences(conn, self.user_id)

            candidates = [
                e for e in experiences
                if e["speech_act"] == speech_act and (topic is None or e["topic"] == topic)
            ]
            if not candidates:
                return None

            best = max(candidates, key=lambda e: (e["success"], e["used_count"]))
            repo.increment_experience_used(conn, best["experience_id"])
            conn.commit()
        best["used_count"] += 1
        return best

    def update_success(self, exp_id: str, user_reaction: str, success: float):
        """
        Обновляет успешность опыта на основе реакции пользователя.
        """
        with get_connection() as conn:
            experiences = repo.list_experiences(conn, self.user_id)
            current = next((e for e in experiences if e["experience_id"] == exp_id), None)
            if not current:
                return
            new_success = (current["success"] + success) / 2  # среднее
            repo.update_experience_success(conn, exp_id, user_reaction, new_success)
            conn.commit()

    def get_lessons(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Возвращает список уроков из опыта.
        Уроки ищутся в context.lessons каждой записи.
        """
        with get_connection() as conn:
            experiences = repo.list_experiences(conn, self.user_id)

        lessons = []
        for exp in experiences:
            context = exp.get("context") or {}
            if context.get("lessons"):
                lessons.append({
                    "query": exp.get("query", ""),
                    "domain": context.get("domain", "unknown"),
                    "trust": context.get("trust", "UNVERIFIED"),
                    "confidence": context.get("confidence", 0.0),
                    "mistakes": context.get("mistakes", []),
                    "lessons": context.get("lessons", []),
                    "policy_changes": context.get("policy_changes", []),
                    "timestamp": context.get("timestamp", ""),
                })
        lessons.sort(key=lambda x: x.get('confidence', 0.0), reverse=True)
        return lessons[:limit]

    def get_relevant_lessons(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Возвращает уроки, релевантные текущему запросу.
        Использует простое сопоставление ключевых слов.
        """
        if not query:
            return self.get_lessons(limit)

        with get_connection() as conn:
            experiences = repo.list_experiences(conn, self.user_id)

        query_words = set(query.lower().split())
        scored_lessons = []

        for exp in experiences:
            context = exp.get("context") or {}
            if context.get("lessons"):
                exp_query = (exp.get("query") or "").lower()
                exp_words = set(exp_query.split())
                overlap = len(query_words & exp_words)
                score = overlap / max(len(query_words), 1)

                scored_lessons.append({
                    "score": score,
                    "lesson": {
                        "query": exp.get("query", ""),
                        "domain": context.get("domain", "unknown"),
                        "trust": context.get("trust", "UNVERIFIED"),
                        "confidence": context.get("confidence", 0.0),
                        "mistakes": context.get("mistakes", []),
                        "lessons": context.get("lessons", []),
                        "policy_changes": context.get("policy_changes", []),
                        "timestamp": context.get("timestamp", ""),
                    }
                })

        scored_lessons.sort(key=lambda x: (x["score"], x["lesson"]["confidence"]), reverse=True)
        return [item["lesson"] for item in scored_lessons[:limit]]

    def get_stats(self) -> Dict:
        """Возвращает статистику по опыту"""
        with get_connection() as conn:
            experiences = repo.list_experiences(conn, self.user_id)

        acts: Dict[str, int] = {}
        for exp in experiences:
            acts[exp["speech_act"]] = acts.get(exp["speech_act"], 0) + 1

        return {
            "total_experiences": len(experiences),
            "speech_acts": acts,
            "avg_success": sum(e["success"] for e in experiences) / len(experiences) if experiences else 0,
        }


def get_experience_memory(user_id: str = "global") -> ExperienceMemory:
    return ExperienceMemory(user_id)


if __name__ == "__main__":
    memory = get_experience_memory("test_user")

    memory.add_experience(
        speech_act="sarcasm",
        topic="general",
        query="Ну ты и умная, да?",
        response="Спасибо! Я стараюсь. А ты умеешь отличать сарказм от комплимента?",
        context={"trust": 50}
    )

    exp = memory.get_experience("sarcasm")
    if exp:
        print(f"Найден опыт: {exp['query'][:50]} → {exp['response'][:50]}")

    print(f"Статистика: {memory.get_stats()}")
