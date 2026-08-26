"""
agent/orch_clarifier.py — Уточнение запроса.
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class ClarificationQuestion:
    """Вопрос для уточнения"""
    question: str
    options: Optional[List[str]] = None
    param: str = ""
    description: str = ""


class ClarificationSession:
    """Сессия уточнения"""
    
    def __init__(self, query: str, intent_result=None):
        self.query = query
        self.intent = intent_result
        self._questions = []
        self._answers = {}
        self._current_index = 0
        self.complete = False
        self._generate_questions()
    
    def _generate_questions(self):
        """Генерирует вопросы для уточнения"""
        if self.intent and hasattr(self.intent, "missing") and self.intent.missing:
            self._questions = [
                ClarificationQuestion(
                    param=m,
                    question=f"Уточните: {m}?"
                )
                for m in self.intent.missing
            ]
        else:
            # Общие вопросы
            self._questions = [
                ClarificationQuestion(
                    param="context",
                    question="Что именно вы имеете в виду? Пожалуйста, уточните."
                )
            ]
    
    def next_questions(self) -> List[ClarificationQuestion]:
        """Возвращает следующие вопросы для уточнения"""
        if self._current_index < len(self._questions):
            return [self._questions[self._current_index]]
        return []
    
    def format_questions(self) -> str:
        """Форматирует вопросы для отображения пользователю"""
        questions = self.next_questions()
        if not questions:
            return "Уточните, пожалуйста."
        
        lines = []
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q.question}")
        return "\n".join(lines)
    
    def submit_answers(self, answers: Dict[str, str]) -> Any:
        """Принимает ответы на уточнения"""
        if not answers:
            return self.intent
        
        for key, value in answers.items():
            self._answers[key] = value
        
        self._current_index += 1
        if self._current_index >= len(self._questions):
            self.complete = True
        
        # Обновляем intent с полученными данными
        if self.intent:
            for key, value in self._answers.items():
                if key not in self.intent.entities:
                    self.intent.entities[key] = value
        
        return self.intent


def generate_questions(query: str, intent_result) -> List[ClarificationQuestion]:
    """Генерирует вопросы для уточнения (утилита)"""
    session = ClarificationSession(query, intent_result)
    return session._questions
