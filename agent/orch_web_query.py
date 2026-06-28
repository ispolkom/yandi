"""
assistant/orch_web_query.py — Internet Query Formulator (Qwen3:14b).
Создаёт 2-3 точных варианта поисковых запросов для веб-поиска.
"""
from __future__ import annotations

import json
import re

import requests as _requests

from agent.orch_schemas import EnrichedQuery, WebQueryResult

from agent.orch_config import OLLAMA_BASE as OLLAMA, MODEL, MAX_TOKENS_CONDUCTOR, TEMP_ANALYST
TIMEOUT = 90

_session = _requests.Session()
_session.trust_env = False

def _system_prompt() -> str:
    from datetime import datetime
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    return f"""Ты формулировщик поисковых запросов. Создай 2-3 эффективных варианта запроса для поисковика.

Сегодня: {date_str}.

Верни ТОЛЬКО валидный JSON:
{{
  "queries": ["запрос 1", "запрос 2", "запрос 3"]
}}

Правила:
- Запросы на том языке, где больше релевантных источников (русский или английский)
- Конкретные ключевые слова, без вопросительных слов
- Разные формулировки одной темы
- Максимум 8-10 слов каждый"""


def _call_ollama(prompt: str) -> str:
    resp = _session.post(
        f"{OLLAMA}/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": TEMP_ANALYST, "num_predict": MAX_TOKENS_CONDUCTOR}},
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


def formulate_queries(enriched: EnrichedQuery) -> WebQueryResult:
    """
    Сформулировать поисковые запросы для веб-поиска.

    Args:
        enriched: обогащённый запрос

    Returns:
        WebQueryResult с 2-3 вариантами
    """
    prompt = (
        f"{_system_prompt()}\n\n"
        f"Оригинальный вопрос: {enriched.original}\n"
        f"Уточнённый запрос: {enriched.enriched}"
    )

    try:
        raw     = _call_ollama(prompt)
        data    = _extract_json(raw)
        queries = [q.strip() for q in data.get("queries", []) if q.strip()][:3]
        if not queries:
            queries = [enriched.enriched]
        return WebQueryResult(queries=queries, raw=raw)
    except Exception as e:
        return WebQueryResult(queries=[enriched.enriched], raw=f"[fallback: {e}]")


if __name__ == "__main__":
    from agent.orch_schemas import EnrichedQuery
    tests = [
        EnrichedQuery(original="Как жарить стейк?", enriched="стейк прожарка medium rare сковорода", params={}),
        EnrichedQuery(original="Что такое Kademlia?", enriched="Kademlia DHT distributed hash table алгоритм", params={}),
    ]
    for eq in tests:
        result = formulate_queries(eq)
        print(f"\nQ: {eq.original}")
        for i, q in enumerate(result.queries, 1):
            print(f"  {i}. {q}")
