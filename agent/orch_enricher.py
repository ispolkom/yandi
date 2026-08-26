"""
assistant/orch_enricher.py — Query Enricher (простая версия без LLM).
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


def _simple_enrich(query: str, intent_result: IntentResult) -> str:
    """Простое обогащение без LLM."""
    enriched = query.strip()
    
    # Добавляем контекст из intent
    if intent_result and intent_result.intent:
        domain_map = {
            "cooking": "кулинария рецепт приготовление",
            "medical": "медицина здоровье лечение",
            "legal": "юриспруденция закон право",
            "financial": "финансы экономика",
            "coding": "программирование код разработка",
            "science": "наука исследование",
            "tech": "технологии IT",
            "ai_ml": "искусственный интеллект машинное обучение",
            "general": "",
        }
        domain_context = domain_map.get(intent_result.intent, "")
        if domain_context:
            enriched = f"{enriched} — {domain_context}"
    
    # Добавляем параметры из entities
    if intent_result and intent_result.entities:
        for key, value in intent_result.entities.items():
            if value and str(value).lower() not in enriched.lower():
                enriched = f"{enriched} {key}:{value}"
    
    # Ограничиваем длину
    if len(enriched) > 150:
        enriched = enriched[:150]
    
    return enriched


def _classify_tags(query: str) -> list[str]:
    """Классификация тегов (быстрый мини-вызов)."""
    try:
        TAG_PROMPT = """Classify this question into 3-5 English tags (format: domain:subcategory).
Output ONLY comma-separated tags, nothing else.
Examples: travel:tourism, tech:networking, health:medicine, cooking:recipes, finance:investing, home:renovation, sport:fitness, science:physics, law:civil, education:math

Question: {query}
Tags:"""
        
        resp = _session.post(
            f"{OLLAMA}/api/generate",
            json={"model": MODEL, "prompt": TAG_PROMPT.format(query=query), "stream": False,
                  "options": {"temperature": 0.1, "num_predict": 50}},
            timeout=15,
        )
        raw = resp.json().get("response", "").strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        for stop in ("<|endoftext|>", "<|im_start|>", "<|im_end|>", "</s>"):
            raw = raw.split(stop)[0]
        first_line = raw.splitlines()[0] if raw else ""
        tags = [t.strip().lower() for t in first_line.split(",") if ":" in t.strip()]
        return tags[:5]
    except Exception:
        return []


def enrich_query(query: str, intent_result: IntentResult) -> EnrichedQuery:
    """
    Расширить запрос используя intent и собранные параметры (простая версия).

    Args:
        query: оригинальный запрос
        intent_result: результат IntentAnalyzer

    Returns:
        EnrichedQuery
    """
    # Простое обогащение без LLM
    enriched = _simple_enrich(query, intent_result)
    
    # Параметры из entities
    params = {}
    if intent_result and intent_result.entities:
        params = {k: v for k, v in intent_result.entities.items() if v is not None}
    
    # Теги (быстрый вызов)
    tags = []
    try:
        tags = _classify_tags(query)
    except Exception:
        pass
    
    return EnrichedQuery(
        original=query,
        enriched=enriched,
        params=params,
        tags=tags,
    )


if __name__ == "__main__":
    from agent.orch_intent import analyze_intent

    tests = [
        ("Как приготовить рыбу?", {}),
        ("как собирать грибы", {}),
        ("Как лечить кашель?", {}),
        ("Как настроить DHT в P2P-сети?", {}),
    ]

    for query, extra_entities in tests:
        print(f"\nQ: {query}")
        intent = analyze_intent(query)
        intent.entities.update(extra_entities)
        result = enrich_query(query, intent)
        print(f"  Enriched: {result.enriched}")
        print(f"  Params:   {result.params}")
        print(f"  Tags:     {result.tags}")
