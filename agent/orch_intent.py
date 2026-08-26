"""
agent/orch_intent.py — Intent Analyzer.

Определяет intent, извлекает сущности, находит пропущенные параметры.

P3.1: докстринг ранее указывал "Qwen3:14b" — реально модель берётся
из agent.orch_config.MODEL ("heretic:q8"), см. импорт ниже. Комментарий
исправлен, чтобы не вводить в заблуждение при будущих аудитах.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests as _requests

from agent.orch_schemas import IntentResult

_session = _requests.Session()
_session.trust_env = False

from agent.orch_config import OLLAMA_BASE as OLLAMA, MODEL, MAX_TOKENS_CONDUCTOR, TEMP_CONDUCTOR
TIMEOUT  = 90

# Известные домены для нормализации intent
INTENT_DOMAINS = {
    "cooking":    ["рецепт", "приготовить", "готовить", "блюдо", "еда", "cook", "recipe"],
    "medical":    ["лечить", "болезнь", "симптом", "лекарств", "медицин", "врач", "health"],
    "legal":      ["закон", "право", "юридич", "суд", "договор", "legal"],
    "financial":  ["деньги", "финанс", "инвестиц", "банк", "кредит", "налог"],
    "coding":     ["код", "программ", "python", "javascript", "debug", "функци", "class"],
    "science":    ["физик", "химия", "биолог", "математик", "формул", "теорем", "гравитац", "квант"],
    "tech":       ["сервер", "сеть", "linux", "docker", "api", "база данных", "нода"],
    "ai_ml":      ["модель", "обучение", "нейросет", "llm", "датасет", "embedding"],
    "general":    [],  # fallback
}

# Инструкционные маркеры (для повышения objectivity)
_INSTRUCTION_KEYWORDS = [
    "как сделать", "как приготовить", "как поймать", "как построить",
    "как работает", "как настроить", "как установить", "как использовать",
    "инструкция", "алгоритм", "способ", "метод", "пошагово",
    "рецепт", "руководство", "guide", "tutorial", "как сварить",
    "как пожарить", "как собрать", "как подключить", "как запустить",
]


def _get_live_domains() -> list[str]:
    """Получить активные домены из живого DHT Tag Tree (если доступен)."""
    try:
        from agent.orch_tag_tree import get_active_domains
        live = get_active_domains()
        base = list(INTENT_DOMAINS.keys())
        return list(dict.fromkeys(base + [d for d in live if d not in base]))
    except Exception:
        return list(INTENT_DOMAINS.keys())


SYSTEM_PROMPT = """Ты анализатор запросов. Твоя задача — разобрать запрос пользователя и вернуть JSON.

Верни ТОЛЬКО валидный JSON без markdown, без пояснений, без ```json:
{
  "intent": "<одно из: cooking, medical, legal, financial, coding, science, tech, ai_ml, general>",
  "entities": {"ключ": "значение или null"},
  "missing": ["список недостающих параметров для полного ответа"],
  "need_clarification": true/false,
  "confidence": 0.0-1.0,
  "is_instruction": true/false
}

Правила:
- need_clarification = true только если без уточнений невозможно дать полезный ответ
- confidence: 0.9+ если запрос однозначен, 0.5-0.8 если есть неопределённость
- missing: только ВАЖНЫЕ параметры, не все возможные
- entities: только то что уже есть в запросе
- is_instruction = true если запрос просит алгоритм, рецепт, способ, инструкцию, пошаговое руководство"""


def _call_ollama(prompt: str) -> str:
    resp = _session.post(
        f"{OLLAMA}/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": TEMP_CONDUCTOR, "num_predict": MAX_TOKENS_CONDUCTOR}},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _extract_json(text: str) -> dict:
    """Извлечь JSON из ответа модели."""
    # Убрать <think>...</think> блоки (deepseek/qwen3 thinking)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Попробовать прямой парсинг
    try:
        return json.loads(text)
    except Exception:
        pass
    # Найти JSON-блок
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return {}


def _detect_intent_fast(query: str) -> str:
    """Быстрое определение intent по ключевым словам (без LLM)."""
    q = query.lower()
    for domain, keywords in INTENT_DOMAINS.items():
        if domain == "general":
            continue
        if any(kw in q for kw in keywords):
            return domain
    return "general"


def _detect_instruction_fast(query: str) -> bool:
    """Быстрое определение инструкционного запроса."""
    q = query.lower()
    return any(kw in q for kw in _INSTRUCTION_KEYWORDS)


def analyze_intent(query: str, context: list[dict] | None = None) -> IntentResult:
    """
    Анализ запроса: intent, сущности, пропущенные параметры.

    Args:
        query: запрос пользователя
        context: предыдущие вопросы сессии (опционально)

    Returns:
        IntentResult
    """
    t0 = time.time()

    ctx_str = ""
    if context:
        recent = context[-3:]  # последние 3 вопроса
        ctx_str = "\nКонтекст сессии:\n" + "\n".join(
            f"- {c.get('role','?')}: {c.get('content','')[:100]}"
            for c in recent
        )

    # Быстрое определение инструкции (добавляем в данные)
    is_instruction = _detect_instruction_fast(query)

    prompt = f"{SYSTEM_PROMPT}\n\nЗапрос: {query}{ctx_str}"

    try:
        raw = _call_ollama(prompt)
        data = _extract_json(raw)
    except Exception as e:
        # Fallback: быстрое определение без LLM
        intent = _detect_intent_fast(query)
        return IntentResult(
            intent=intent,
            entities={},
            missing=[],
            need_clarification=False,
            confidence=0.5,
            raw=f"[fallback: {e}]",
        )

    # Нормализовать intent (проверяем против живых доменов из Tag Tree)
    live_domains = _get_live_domains()
    intent = data.get("intent", "general")
    if intent not in live_domains:
        intent = _detect_intent_fast(query)

    # Проверяем is_instruction
    is_instruction_data = data.get("is_instruction", False)
    if not is_instruction_data:
        # Если LLM не распознал, но есть ключевые слова
        is_instruction_data = _detect_instruction_fast(query)

    return IntentResult(
        intent=intent,
        entities=data.get("entities", {}),
        missing=data.get("missing", []),
        need_clarification=bool(data.get("need_clarification", False)),
        confidence=float(data.get("confidence", 0.7)),
        raw=data,
        is_instruction=is_instruction_data,
    )


if __name__ == "__main__":
    tests = [
        "Как приготовить рыбу?",
        "Как лечить кашель у взрослого без аллергии?",
        "Напиши функцию на Python для сортировки списка словарей по ключу",
        "Как настроить DHT в P2P-сети с LLM-нодами?",
        "Привет",
        "Сколько планет в Солнечной системе?",
        "Какое расстояние до Солнца?",
        "Как работает гравитация?",
        "Как пожарить щуку?",
        "Как поймать щуку?",
    ]
    for q in tests:
        print(f"\nQ: {q}")
        r = analyze_intent(q)
        print(f"  intent={r.intent}  conf={r.confidence:.2f}  clarify={r.need_clarification}  instruction={r.is_instruction}")
        print(f"  entities={r.entities}")
        print(f"  missing={r.missing}")
