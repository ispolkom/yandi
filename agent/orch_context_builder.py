"""
orch_context_builder.py — Построение обогащённого запроса из контекста чата.

Смотрит последние сообщения диалога и строит точный поисковый запрос.
Если текущее сообщение — ответ на уточняющий вопрос, объединяет их.
Если запрос самодостаточен — возвращает как есть.
"""
from __future__ import annotations

import json
import re

import requests as _requests

OLLAMA = "http://127.0.0.1:11434"
MODEL  = "heretic:q8"

_session = _requests.Session()
_session.trust_env = False

_PROMPT = """\
Ты помощник по формированию поисковых запросов. Проанализируй диалог и составь точный поисковый запрос.

История диалога (от старых к новым):
{history}

Текущее сообщение пользователя: {query}

Верни ТОЛЬКО JSON (без markdown):
{{"search_query": "точный запрос для поиска", "is_followup": true/false}}

Правила:
- Если текущее сообщение — ответ на уточняющий вопрос из истории (короткое слово или фраза): объедини исходный вопрос с этим ответом в один точный запрос
- Если текущее сообщение — самостоятельный вопрос: верни его как есть (можно уточнить формулировку)
- search_query должен быть конкретным и полным, пригодным для веб-поиска
- is_followup: true если текущее сообщение — продолжение предыдущего разговора
"""


def _extract_json(text: str) -> dict:
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            break
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict) and "search_query" in obj:
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        pos = start + 1
    return {}


def build_query_from_context(query: str, history: list[dict]) -> str:
    """
    Построить обогащённый запрос из текущего сообщения и истории чата.

    Args:
        query:   текущее сообщение пользователя
        history: список последних сообщений [{from, text}, ...]

    Returns:
        Строка поискового запроса
    """
    if not history:
        return query

    # Быстрая проверка: если текущий запрос длинный (>5 слов) — скорее всего самостоятельный
    if len(query.split()) > 5:
        return query

    # Форматируем историю для LLM
    lines = []
    for m in history[-4:]:  # последние 4 сообщения
        role = "Пользователь" if m.get("from") == "human" else "Ассистент"
        text = str(m.get("text", "")).strip()
        # Убираем служебные префиксы из сообщений ассистента
        text = re.sub(r"^\[ПРЕДВАРИТЕЛЬНЫЙ.*?\]\n+", "", text, flags=re.DOTALL)
        text = text[:200]  # не больше 200 символов из каждого
        lines.append(f"{role}: {text}")

    history_str = "\n".join(lines)

    prompt = _PROMPT.format(history=history_str, query=query)

    try:
        resp = _session.post(
            f"{OLLAMA}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 200},
            },
            timeout=60,
        )
        raw = resp.json().get("response", "")
        data = _extract_json(raw)
        result = (data.get("search_query") or "").strip()
        return result if result else query
    except Exception:
        return query
