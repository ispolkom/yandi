"""
assistant/orch_enricher.py — Query Enricher (Qwen3:14b).
Нормализует и расширяет запрос на основе intent + собранных параметров.
"""
from __future__ import annotations

import json
import re
import time

import requests as _requests

from agent.orch_schemas import IntentResult, EnrichedQuery

_session = _requests.Session()
_session.trust_env = False

from agent.orch_config import OLLAMA_BASE as OLLAMA, MODEL, MAX_TOKENS_CONDUCTOR, TEMP_CONDUCTOR
TIMEOUT = 90

SYSTEM_PROMPT = """Ты обогатитель поискового запроса.
Получаешь оригинальный вопрос и параметры. Создаёшь точный расширенный запрос для поиска.

Верни ТОЛЬКО валидный JSON без markdown:
{
  "enriched": "<точный нормализованный запрос на русском>",
  "params": {"ключ": "значение"}
}

Правила:
- enriched: конкретный, без лишних слов, максимум 150 символов
- Включи все известные параметры из entities
- Используй профессиональные термины если уместно
- НЕ добавляй вопросительные слова ("как", "что")"""

TAG_PROMPT = """Classify this question into 3-5 English tags (format: domain:subcategory).
Output ONLY comma-separated tags, nothing else.
Examples: travel:tourism, tech:networking, health:medicine, cooking:recipes, finance:investing, home:renovation, sport:fitness, science:physics, law:civil, education:math

Question: {query}
Tags:"""


def _call_ollama(prompt: str, max_tokens: int = MAX_TOKENS_CONDUCTOR) -> str:
    resp = _session.post(
        f"{OLLAMA}/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": TEMP_CONDUCTOR, "num_predict": max_tokens}},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _classify_tags(query: str) -> list[str]:
    """Отдельный мини-вызов для тег-классификации (50 токенов, быстро)."""
    try:
        prompt = TAG_PROMPT.format(query=query)
        raw = _call_ollama(prompt, max_tokens=50)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        # Обрезаем EOS-токены и спецсимволы модели
        for stop in ("<|endoftext|>", "<|im_start|>", "<|im_end|>", "</s>"):
            raw = raw.split(stop)[0]
        # Берём только первую строку — модель иногда добавляет объяснение
        first_line = raw.splitlines()[0] if raw else ""
        tags = [t.strip().lower() for t in first_line.split(",") if ":" in t.strip()]
        return tags[:5]
    except Exception:
        return []


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


def _simple_enrich(query: str, intent_result: IntentResult) -> str:
    """Простое обогащение без LLM — на случай fallback."""
    parts = [query.strip()]
    for k, v in intent_result.entities.items():
        if v and str(v).lower() not in query.lower():
            parts.append(str(v))
    return " ".join(parts)


def enrich_query(query: str, intent_result: IntentResult) -> EnrichedQuery:
    """
    Расширить запрос используя intent и собранные параметры.

    Args:
        query: оригинальный запрос (возможно уже с уточнениями)
        intent_result: результат IntentAnalyzer

    Returns:
        EnrichedQuery
    """
    params = {k: v for k, v in intent_result.entities.items() if v is not None}

    params_str = ""
    if params:
        params_str = "\nИзвестные параметры:\n" + "\n".join(f"- {k}: {v}" for k, v in params.items())

    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Оригинальный запрос: {query}\n"
        f"Intent: {intent_result.intent}"
        f"{params_str}"
    )

    try:
        raw = _call_ollama(prompt)
        data = _extract_json(raw)
        enriched = data.get("enriched", "").strip()
        if not enriched or len(enriched) < 5:
            enriched = _simple_enrich(query, intent_result)
        merged_params = {**params, **data.get("params", {})}
    except Exception as e:
        enriched = _simple_enrich(query, intent_result)
        merged_params = params
        raw = f"[fallback: {e}]"

    # Отдельный мини-вызов для тегов (50 токенов, не ломает основной JSON)
    tags = _classify_tags(query)

    return EnrichedQuery(
        original=query,
        enriched=enriched,
        params=merged_params,
        tags=tags,
        raw=raw if "raw" in dir() else "",
    )


if __name__ == "__main__":
    from agent.orch_intent import analyze_intent

    tests = [
        ("Как приготовить рыбу?", {"product": "лосось", "method": "запекание"}),
        ("Как лечить кашель?", {"type": "сухой", "age": "взрослый"}),
        ("Как настроить DHT в P2P-сети?", {}),
    ]

    for query, extra_entities in tests:
        print(f"\nQ: {query}")
        intent = analyze_intent(query)
        intent.entities.update(extra_entities)
        result = enrich_query(query, intent)
        print(f"  Enriched: {result.enriched}")
        print(f"  Params:   {result.params}")
