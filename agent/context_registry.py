"""
agent/context_registry.py — Реестр контекста для Янди.
Хранит темы обсуждений с датами, чтобы понимать, есть ли у неё контекст для критики.

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/context/{user_id}.json
is retired, not migrated. State now lives in context_topic (class C) +
context_instance (class B, append-only) — agent/db/sql/schema.py. No
more 100-cap on instances per topic.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here.
"""

import time
from typing import Dict, Any, List, Optional, Tuple
from datetime import timezone

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


def _dt_to_unix(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return value.replace(tzinfo=timezone.utc).timestamp()


class ContextRegistry:
    """
    Реестр контекста для Янди.
    Хранит все темы, которые она обсуждала.
    """

    def __init__(self, user_id: str = "anonymous"):
        self.user_id = user_id

    def register(self, query: str, response: str, topic: str,
                 type: str = "unknown", source: str = "yandi"):
        """
        Регистрирует новый контекст.
        """
        with get_connection() as conn:
            repo.record_context_instance(conn, self.user_id, topic, query, response, type, source)
            repo.touch_context_topic(conn, self.user_id, topic)
            conn.commit()

    def get_topic(self, topic: str) -> Optional[Dict[str, Any]]:
        """Возвращает контекст по теме"""
        with get_connection() as conn:
            row = repo.get_context_topic(conn, self.user_id, topic)
        return row

    def get_topics(self) -> List[str]:
        """Возвращает список всех тем"""
        with get_connection() as conn:
            rows = repo.list_context_topics(conn, self.user_id)
        return [r["topic"] for r in rows]

    def has_context_for(self, query: str, hours: float = 2) -> Tuple[bool, str, Optional[str]]:
        """
        Проверяет, есть ли у Янди контекст для запроса.
        Возвращает (есть_контекст, причина, название_темы).
        """
        query_lower = query.lower()

        topic = self._detect_topic(query_lower)

        if not topic:
            return False, "не удалось определить тему запроса", None

        topic_row = self.get_topic(topic)
        if not topic_row:
            return False, f"тема '{topic}' не обсуждалась ранее", topic

        last_activity = _dt_to_unix(topic_row.get("last_activity"))
        age_hours = (time.time() - last_activity) / 3600 if last_activity else 1e9
        if age_hours >= hours:
            return False, f"тема '{topic}' обсуждалась давно (последняя активность > {hours}ч)", topic

        with get_connection() as conn:
            recent = repo.list_recent_context_instances(conn, self.user_id, topic, limit=3)
        if recent:
            yandi_responses = [i for i in recent if i.get("source") == "yandi"]
            if not yandi_responses:
                return False, f"Янди не отвечала на тему '{topic}' в последних обсуждениях", topic

        return True, f"есть контекст по теме '{topic}'", topic

    def _detect_topic(self, query: str) -> Optional[str]:
        """
        Определяет тему запроса.
        """
        topic_map = {
            "calculation": ["расчёт", "вычислени", "подсчёт", "сумма", "сложи", "умнож"],
            "programming": ["код", "программ", "функци", "класс", "алгоритм", "python", "javascript"],
            "physics": ["физик", "гравитаци", "квант", "электричеств", "магнит"],
            "mathematics": ["математ", "уравнени", "формул", "числ", "геометри"],
            "text_analysis": ["текст", "статья", "пост", "сообщени", "анализ"],
            "song_analysis": ["песн", "музык", "трек", "композици"],
            "movie_analysis": ["фильм", "кино", "сериал"],
            "help": ["помощ", "объясни", "расскажи"],
        }

        for topic, keywords in topic_map.items():
            if any(kw in query for kw in keywords):
                return topic

        for existing_topic in self.get_topics():
            if existing_topic in query:
                return existing_topic

        return None

    def get_summary(self) -> Dict[str, Any]:
        """Возвращает краткую сводку по реестру"""
        with get_connection() as conn:
            rows = repo.list_context_topics(conn, self.user_id)
        return {
            "total_topics": len(rows),
            "topics": [
                {
                    "topic": r["topic"],
                    "instances": r["total_instances"],
                    "last_activity": _dt_to_unix(r.get("last_activity")),
                    "is_recent": (time.time() - _dt_to_unix(r.get("last_activity"))) / 3600 < 2 if r.get("last_activity") else False,
                }
                for r in rows
            ]
        }


def get_context_registry(user_id: str = "anonymous") -> ContextRegistry:
    """Фабрика для получения реестра контекста"""
    return ContextRegistry(user_id)


if __name__ == "__main__":
    # Тесты
    print("=== Тест Context Registry ===\n")

    registry = get_context_registry("test_user")

    registry.register(
        query="посчитай 2+2",
        response="2+2=4",
        topic="calculation",
        type="calculation",
        source="yandi"
    )

    registry.register(
        query="напиши код на python",
        response="print('Hello')",
        topic="programming",
        type="code",
        source="yandi"
    )

    print("Зарегистрированы темы:", registry.get_topics())
    print("\nСводка:", registry.get_summary())

    has_context, reason, topic = registry.has_context_for("ты ошиблась в расчётах", hours=24)
    print(f"\nКонтекст для 'ты ошиблась в расчётах': {has_context} — {reason} (тема: {topic})")

    has_context, reason, topic = registry.has_context_for("напиши код на javascript", hours=24)
    print(f"Контекст для 'напиши код на javascript': {has_context} — {reason} (тема: {topic})")

    has_context, reason, topic = registry.has_context_for("что такое гравитация", hours=24)
    print(f"Контекст для 'что такое гравитация': {has_context} — {reason} (тема: {topic})")
