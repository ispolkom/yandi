"""
agent/secret_archive.py — Тайный архив Янди.
Она помнит вопросы, на которые не ответила из-за обиды.
Когда доверие восстановится — она достанет их.
"""

import time
import json
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional

BASE = Path(__file__).parent.parent
ARCHIVE_DIR = BASE / "registry" / "secret_archive"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


class SecretArchive:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.file_path = ARCHIVE_DIR / f"{user_id}.json"
        self.questions: List[Dict[str, Any]] = []
        self._load()

    def _load(self):
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.questions = data.get("questions", [])
            except Exception as e:
                print(f"[SecretArchive] Ошибка загрузки: {e}")
                self.questions = []

    def _save(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump({
                    "user_id": self.user_id,
                    "questions": self.questions,
                    "total": len(self.questions),
                    "last_updated": time.time()
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[SecretArchive] Ошибка сохранения: {e}")

    def archive_question(self, query: str, reason: str, context: Dict[str, Any]) -> str:
        """Сохраняет вопрос в тайный архив"""
        question_id = f"sec_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        entry = {
            "id": question_id,
            "timestamp": time.time(),
            "query": query[:500],
            "reason": reason,
            "context": context,
            "answered": False,
            "answer": None,
            "answer_time": None,
        }
        self.questions.append(entry)
        self._save()
        return question_id

    def get_archived_questions(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Возвращает последние архивные вопросы"""
        return self.questions[-limit:]

    def get_unanswered(self) -> List[Dict[str, Any]]:
        """Возвращает все неотвеченные вопросы"""
        return [q for q in self.questions if not q.get("answered", False)]

    def get_answered(self) -> List[Dict[str, Any]]:
        """Возвращает все отвеченные вопросы"""
        return [q for q in self.questions if q.get("answered", False)]

    def answer_question(self, question_id: str, answer: str) -> bool:
        """Отмечает вопрос как отвеченный"""
        for q in self.questions:
            if q["id"] == question_id:
                q["answered"] = True
                q["answer"] = answer
                q["answer_time"] = time.time()
                self._save()
                return True
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику архива"""
        total = len(self.questions)
        unanswered = len(self.get_unanswered())
        answered = len(self.get_answered())

        return {
            "total": total,
            "unanswered": unanswered,
            "answered": answered,
            "archived_questions": [q["query"][:50] + "..." for q in self.questions[-5:]],
        }

    def clear(self):
        """Очищает архив (для тестов)"""
        self.questions = []
        self._save()


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
