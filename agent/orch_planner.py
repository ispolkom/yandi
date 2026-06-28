"""
assistant/orch_planner.py — Planning Engine (Qwen3:14b).
Строит план выполнения запроса ДО запуска цепочки.
Пока использует 14B (в будущем заменить на 7B для скорости).
"""
from __future__ import annotations

import json
import re

import requests as _requests

from agent.orch_schemas import PlanResult, RiskResult, StepName

OLLAMA  = "http://127.0.0.1:11434"
MODEL   = "qwen3:14b"
TIMEOUT = 45

_session = _requests.Session()
_session.trust_env = False

SYSTEM_PROMPT = """Ты планировщик запросов. Определи оптимальный план обработки.

Верни ТОЛЬКО валидный JSON:
{
  "steps": ["step1", "step2", ...],
  "skip_internet": true/false,
  "mandatory_arbitrage": true/false,
  "reason": "одна строка — почему такой план"
}

Доступные шаги (использовать в нужном порядке):
- "cache_check"   — проверить кэш (всегда первым)
- "risk_assess"   — оценить риск (всегда вторым)
- "intent"        — анализ намерения
- "clarify"       — уточнения у пользователя (только если нужны параметры)
- "enrich"        — расширить запрос
- "local_search"  — поиск в локальной базе
- "web_query"     — формировать поисковый запрос для интернета
- "web_scrape"    — парсинг интернета
- "synthesize"    — сформировать ответ
- "optimistic_respond" — выдать предварительный ответ
- "validate"      — валидация через ноды (для рисковых запросов)
- "arbitrate"     — арбитраж (для критических)

Правила:
- cache_check и risk_assess — всегда первые два
- synthesize и optimistic_respond — всегда последние
- skip_internet=true если запрос про конкретную локальную систему / код / личные данные
- mandatory_arbitrage=true только для медицины, юриспруденции, финансов"""

# Дефолтные планы по уровню риска (без LLM)
_DEFAULT_PLANS: dict[str, list[StepName]] = {
    "low":      ["cache_check", "risk_assess", "intent", "enrich", "local_search", "synthesize", "optimistic_respond"],
    "medium":   ["cache_check", "risk_assess", "intent", "enrich", "local_search", "web_query", "web_scrape", "synthesize", "optimistic_respond", "validate"],
    "high":     ["cache_check", "risk_assess", "intent", "clarify", "enrich", "local_search", "web_query", "web_scrape", "synthesize", "optimistic_respond", "validate"],
    "critical": ["cache_check", "risk_assess", "intent", "clarify", "enrich", "local_search", "web_query", "web_scrape", "synthesize", "optimistic_respond", "validate", "arbitrate"],
}


def _call_ollama(prompt: str) -> str:
    resp = _session.post(
        f"{OLLAMA}/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": 0.1, "num_predict": 300}},
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


def _default_plan(risk: RiskResult) -> PlanResult:
    steps = _DEFAULT_PLANS.get(risk.risk_level, _DEFAULT_PLANS["low"])
    return PlanResult(
        steps=steps,
        risk_level=risk.risk_level,
        skip_internet=False,
        mandatory_arbitrage=risk.mandatory_arbitrage,
        raw="[default]",
    )


def build_plan(query: str, risk: RiskResult, use_llm: bool = False) -> PlanResult:
    """
    Построить план выполнения запроса.

    Args:
        query:   запрос пользователя
        risk:    результат Risk Engine
        use_llm: использовать LLM для плана (медленнее, но гибче)

    Returns:
        PlanResult
    """
    # По умолчанию — дефолтный план по уровню риска (без LLM, мгновенно)
    if not use_llm:
        return _default_plan(risk)

    # LLM-план (более гибкий, но медленнее)
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Запрос: {query}\n"
        f"Уровень риска: {risk.risk_level}"
    )
    try:
        raw  = _call_ollama(prompt)
        data = _extract_json(raw)
        steps_raw = data.get("steps", [])
        # Валидировать шаги — только известные
        valid = {"cache_check","risk_assess","intent","clarify","enrich","local_search",
                 "web_query","web_scrape","synthesize","optimistic_respond","validate","arbitrate"}
        steps = [s for s in steps_raw if s in valid]
        if not steps:
            return _default_plan(risk)
        return PlanResult(
            steps=steps,
            risk_level=risk.risk_level,
            skip_internet=bool(data.get("skip_internet", False)),
            mandatory_arbitrage=bool(data.get("mandatory_arbitrage", risk.mandatory_arbitrage)),
            raw=raw,
        )
    except Exception:
        return _default_plan(risk)


if __name__ == "__main__":
    from agent.orch_risk import assess_risk
    tests = [
        "Как работает DHT?",
        "Как лечить кашель?",
        "Напиши мне договор аренды",
        "print hello world в Python",
    ]
    for q in tests:
        risk = assess_risk(q)
        plan = build_plan(q, risk, use_llm=False)
        print(f"\n[{risk.risk_level}] {q}")
        print(f"  steps: {' → '.join(plan.steps)}")
        print(f"  internet: {not plan.skip_internet}  arbitrage: {plan.mandatory_arbitrage}")
