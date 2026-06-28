"""
orch_tagger.py — Авто-тегирование ответов.
LLM определяет 3-5 тематических тегов для записи в реестр.
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
Ты классификатор контента. Определи категории для записи в базу знаний.

Вопрос: {question}
Ответ: {answer_snippet}

Верни ТОЛЬКО JSON (без markdown, без пояснений):
{{"tags": ["категория", "тег2", "тег3"]}}

Теги: 3-5 штук, на русском, строчные, без пробелов (рыбная_ловля).
Первый — широкая категория: спорт, наука, кулинария, технологии, медицина, природа, история...
Остальные — уточняющие.
"""


def _extract_tags(text: str) -> list[str]:
    """Ищет {"tags": [...]} в любом месте текста — включая внутри think-блоков."""
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
                        if isinstance(obj, dict) and "tags" in obj:
                            raw = obj["tags"]
                            if isinstance(raw, list):
                                return [str(t).strip().lower().replace(" ", "_") for t in raw if t][:5]
                    except json.JSONDecodeError:
                        pass
                    break
        pos = start + 1
    return []


def auto_tag(question: str, answer: str) -> list[str]:
    """
    Определить 3-5 тегов для ответа.
    Возвращает пустой список при ошибке — не блокирует основной поток.
    """
    prompt = _PROMPT.format(
        question=question.strip(),
        answer_snippet=answer.strip()[:600],
    )
    try:
        resp = _session.post(
            f"{OLLAMA}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 300},
            },
            timeout=90,
        )
        raw = resp.json().get("response", "")
        tags = _extract_tags(raw)
        return tags if len(tags) >= 2 else []
    except Exception:
        return []
