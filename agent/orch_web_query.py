"""
assistant/orch_web_query.py — Internet Query Formulator.

Создаёт 2-3 точных варианта поисковых запросов для веб-поиска.

P3.1: докстринг ранее указывал "Qwen3:14b" — реально модель берётся
из agent.orch_config.MODEL ("heretic:q8"), см. импорт ниже. Комментарий
исправлен, чтобы не вводить в заблуждение при будущих аудитах.
"""
from __future__ import annotations

import json
import re
import time

import requests as _requests

from agent.orch_schemas import EnrichedQuery, WebQueryResult

from agent.orch_config import (
    OLLAMA_BASE as OLLAMA,
    MODEL,
    MAX_TOKENS_CONDUCTOR,
    TEMP_ANALYST,
    GENERATION_SEMAPHORE,
)
TIMEOUT = 90

_session = _requests.Session()
_session.trust_env = False

def _system_prompt() -> str:
    from datetime import datetime
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    return f"""Ты формулировщик поисковых запросов для исследовательского поиска.

Сегодня: {date_str}.

Создай РОВНО 3 запроса с РАЗНЫМИ поисковыми задачами.

1. BROAD
   Найти хорошие материалы по самой теме.

2. DIRECT_EVIDENCE
   Искать наблюдения, измерения, данные, исследования,
   эксперименты, миссии, документы или другие проверяемые
   свидетельства по вопросу.

3. PRIMARY_INSTITUTIONAL
   Искать материалы, максимально близкие к первичным,
   научным или институциональным источникам:
   исследовательские организации, университеты,
   научные журналы, миссии, агентства, официальные отчёты.

ВАЖНО:
- Не считай официальный или научный источник автоматически истинным.
- Не ограничивай поиск одним заранее заданным сайтом.
- Не создавай три перефразировки одного и того же запроса.
- Каждый запрос должен иметь собственную функцию.
- Для factual/scientific вопросов допустимо использовать английский,
  если на нём вероятнее найти первичные исследования.
- Используй слова вроде evidence, observations, data, research,
  mission, spacecraft, study, measurements только когда они
  соответствуют смыслу вопроса.
- Не добавляй criticism/opposition — это делает отдельный
  refutation pipeline.
- Максимум 8-12 слов на запрос.
- Без вопросительных предложений.

Верни ТОЛЬКО валидный JSON:
{{
  "queries": [
    "broad query",
    "direct evidence query",
    "primary institutional query"
  ]
}}"""


def _call_ollama(prompt: str) -> str:
    # P2.2: раньше этот вызов не имел никакого concurrency gate,
    # в отличие от orch_synthesizer._call(). До 3 потоков PASS2
    # claim-specific retrieval могли одновременно слать generation
    # запросы на тот же локальный Ollama без координации.
    #
    # H (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md, §H): без видимости
    # wait-time нельзя было доказать, помогает semaphore или мешает.
    # Тот же паттерн, что уже используется в orch_synthesizer._call().
    _wait_started = time.time()

    with GENERATION_SEMAPHORE:
        _waited = time.time() - _wait_started

        if _waited > 0.05:
            print(
                f"[Web Query LLM] generation queue wait="
                f"{_waited:.2f}s"
            )

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

def formulate_refutation_queries(enriched: EnrichedQuery) -> WebQueryResult:
    """
    Сформулировать поисковые запросы для поиска опровержений и альтернативных точек зрения.
    """
    from datetime import datetime
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    prompt = f"""Ты формулировщик поисковых запросов для поиска ОПРОВЕРЖЕНИЙ.
    
    Сегодня: {date_str}.
    
    Оригинальный вопрос: {enriched.original}
    
    Создай 2-3 запроса для поиска:
    1. Критика и опровержения основной точки зрения
    2. Альтернативные точки зрения
    3. Противоположные аргументы
    
    Верни ТОЛЬКО валидный JSON:
    {{"queries": ["запрос 1", "запрос 2", "запрос 3"]}}
    
    Примеры:
    - вместо "польза витамина C" → "витамин C вред побочные эффекты"
    - вместо "эффективность демократии" → "критика демократии недостатки"
    - вместо "наука и религия" → "наука vs религия конфликт противоречия"
    """
    try:
        raw = _call_ollama(prompt)
        data = _extract_json(raw)
        queries = [q.strip() for q in data.get("queries", []) if q.strip()][:3]
        if not queries:
            queries = [f"{enriched.enriched} критика", f"{enriched.enriched} альтернативная точка зрения"]
        return WebQueryResult(queries=queries, raw=raw)
    except Exception as e:
        return WebQueryResult(queries=[f"{enriched.enriched} критика"], raw=f"[fallback: {e}]")


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
