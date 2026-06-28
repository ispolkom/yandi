"""
assistant/orch_clarifier.py — Clarification Engine (Qwen3:14b → 7B в будущем).
Генерирует уточняющие вопросы и собирает ответы пользователя.
Максимум 3 раунда. Async-ready (таймаут 30s на ответ).
"""
from __future__ import annotations

import json
import re

import requests as _requests

from agent.orch_schemas import IntentResult, ClarificationResult, ClarificationQuestion

OLLAMA  = "http://127.0.0.1:11434"
MODEL   = "qwen3:14b"
TIMEOUT = 45
MAX_ROUNDS = 3

_session = _requests.Session()
_session.trust_env = False

SYSTEM_PROMPT = """Ты помощник, который уточняет детали запроса.
На основе оригинального вопроса и списка недостающих параметров сформируй уточняющие вопросы.

Верни ТОЛЬКО валидный JSON:
{
  "questions": [
    {"param": "имя_параметра", "question": "вопрос пользователю"},
    ...
  ]
}

Правила:
- Максимум 3 вопроса за раз, только самые важные
- Вопросы короткие и конкретные
- Предлагай варианты если возможно (напр: "сухой или влажный?")
- На русском языке"""


def _call_ollama(prompt: str) -> str:
    resp = _session.post(
        f"{OLLAMA}/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": 0.2, "num_predict": 400}},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {}


def generate_questions(query: str, intent: IntentResult) -> list[ClarificationQuestion]:
    """Сгенерировать уточняющие вопросы на основе missing параметров."""
    if not intent.missing:
        return []

    missing_str = "\n".join(f"- {m}" for m in intent.missing[:5])
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Оригинальный вопрос: {query}\n"
        f"Тема: {intent.intent}\n"
        f"Недостающие параметры:\n{missing_str}"
    )

    try:
        raw  = _call_ollama(prompt)
        data = _extract_json(raw)
        qs   = data.get("questions", [])
        return [
            ClarificationQuestion(
                param=q.get("param", "unknown"),
                question=q.get("question", ""),
            )
            for q in qs if q.get("question")
        ][:3]
    except Exception as e:
        # Fallback: простые вопросы из missing списка
        return [
            ClarificationQuestion(param=m, question=f"Уточните: {m}?")
            for m in intent.missing[:2]
        ]


def apply_answers(
    intent: IntentResult,
    questions: list[ClarificationQuestion],
    answers: dict[str, str],
) -> tuple[IntentResult, bool]:
    """
    Применить ответы пользователя к intent.entities.

    Returns:
        (обновлённый intent, complete — все required параметры заполнены)
    """
    updated_entities = dict(intent.entities)
    for q in questions:
        answer = answers.get(q.param, "").strip()
        if answer and answer.lower() not in ("не знаю", "нет", "skip", "пропустить"):
            updated_entities[q.param] = answer

    # Обновить intent
    updated_missing = [m for m in intent.missing if m not in updated_entities]
    updated_intent = IntentResult(
        intent=intent.intent,
        entities=updated_entities,
        missing=updated_missing,
        need_clarification=bool(updated_missing),
        confidence=min(1.0, intent.confidence + 0.1 * len(answers)),
        raw=intent.raw,
    )
    complete = len(updated_missing) == 0
    return updated_intent, complete


class ClarificationSession:
    """Управляет диалогом уточнений для одного запроса."""

    def __init__(self, query: str, intent: IntentResult):
        self.query   = query
        self.intent  = intent
        self.rounds  = 0
        self.complete = False
        self._questions: list[ClarificationQuestion] = []

    def next_questions(self) -> list[ClarificationQuestion] | None:
        """Получить следующий набор вопросов или None если хватит."""
        if self.rounds >= MAX_ROUNDS or self.complete or not self.intent.need_clarification:
            return None
        self._questions = generate_questions(self.query, self.intent)
        self.rounds += 1
        return self._questions if self._questions else None

    def submit_answers(self, answers: dict[str, str]) -> IntentResult:
        """Применить ответы и обновить intent."""
        self.intent, self.complete = apply_answers(self.intent, self._questions, answers)
        return self.intent

    def format_questions(self) -> str:
        """Форматировать вопросы для вывода в чат."""
        if not self._questions:
            return ""
        lines = ["Для точного ответа уточните:"]
        for i, q in enumerate(self._questions, 1):
            lines.append(f"{i}. {q.question}")
        return "\n".join(lines)


if __name__ == "__main__":
    from agent.orch_intent import analyze_intent
    query  = "Как приготовить рыбу?"
    intent = analyze_intent(query)
    print(f"Intent: {intent.intent}, missing: {intent.missing}")

    session = ClarificationSession(query, intent)
    questions = session.next_questions()
    if questions:
        print("\nВопросы:")
        print(session.format_questions())
        # Симулируем ответы
        answers = {q.param: "лосось" if "рыб" in q.param else "запекание"
                   for q in questions}
        updated = session.submit_answers(answers)
        print(f"\nОбновлённые entities: {updated.entities}")
        print(f"Complete: {session.complete}")
