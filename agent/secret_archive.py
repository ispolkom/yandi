"""
agent/secret_archive.py — Тайный архив Янди.
Она помнит вопросы, на которые не ответила из-за обиды.
Когда доверие восстановится — она достанет их.

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/secret_archive/
{user_id}.json is retired, not migrated. State now lives in
secret_archive_question (class C — answer_question() revises answered/
answer/answer_time in place) — agent/db/sql/schema.py.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here.
"""

import time
import uuid
from typing import Dict, Any, List

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


class SecretArchive:
    def __init__(self, user_id: str):
        self.user_id = user_id

    def archive_question(self, query: str, reason: str, context: Dict[str, Any]) -> str:
        """Сохраняет вопрос в тайный архив"""
        question_id = f"sec_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        with get_connection() as conn:
            repo.create_secret_archive_question(conn, question_id, self.user_id, query[:500], reason, context=context)
            conn.commit()
        return question_id

    def get_archived_questions(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Возвращает последние архивные вопросы"""
        with get_connection() as conn:
            questions = repo.list_secret_archive_questions(conn, self.user_id)
        return questions[-limit:]

    def get_unanswered(self) -> List[Dict[str, Any]]:
        """Возвращает все неотвеченные вопросы"""
        with get_connection() as conn:
            return repo.list_secret_archive_questions(conn, self.user_id, answered=False)

    def get_answered(self) -> List[Dict[str, Any]]:
        """Возвращает все отвеченные вопросы"""
        with get_connection() as conn:
            return repo.list_secret_archive_questions(conn, self.user_id, answered=True)

    def answer_question(self, question_id: str, answer: str) -> bool:
        """Отмечает вопрос как отвеченный"""
        with get_connection() as conn:
            updated = repo.answer_secret_archive_question(conn, question_id, answer)
            conn.commit()
        return updated

    def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику архива"""
        with get_connection() as conn:
            questions = repo.list_secret_archive_questions(conn, self.user_id)

        unanswered = [q for q in questions if not q.get("answered")]
        answered = [q for q in questions if q.get("answered")]

        return {
            "total": len(questions),
            "unanswered": len(unanswered),
            "answered": len(answered),
            "archived_questions": [q["query"][:50] + "..." for q in questions[-5:]],
        }


_instances: Dict[str, SecretArchive] = {}


def get_secret_archive(user_id: str = "anonymous") -> SecretArchive:
    if user_id not in _instances:
        _instances[user_id] = SecretArchive(user_id)
    return _instances[user_id]


if __name__ == "__main__":
    archive = get_secret_archive("test_user")
    archive.archive_question(
        query="Твоё видение песни арктик и асти?",
        reason="пользователь обидел меня до этого",
        context={"trust": 5, "respect": 6, "irritation": 39}
    )
    print(f"Архивировано: {archive.get_stats()}")
    print(f"Неотвеченные: {len(archive.get_unanswered())}")
