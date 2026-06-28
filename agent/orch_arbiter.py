"""
assistant/orch_arbiter.py — Consensus Arbiter (Qwen3:14b).
Сравнивает результаты валидации нод, выносит финальный вердикт.
"""
from __future__ import annotations

import json
import re

import requests as _requests

from agent.orch_schemas import ValidationResult, ArbiterResult

OLLAMA  = "http://127.0.0.1:11434"
MODEL   = "qwen3:14b"
TIMEOUT = 90

_session = _requests.Session()
_session.trust_env = False

SYSTEM_PROMPT = """Ты арбитр качества ответов. Проанализируй результаты проверки ответа несколькими нодами.

Верни ТОЛЬКО валидный JSON:
{
  "verdict": "VERIFIED|PARTIALLY_VERIFIED|CONFLICT_DETECTED|REJECTED",
  "explanation": "краткое объяснение вердикта (1-2 предложения)",
  "corrected_answer": null
}

Правила:
- VERIFIED: большинство нод согласны (agree >= 2/3)
- PARTIALLY_VERIFIED: смешанные результаты, но больше agree чем disagree
- CONFLICT_DETECTED: серьёзные расхождения между нодами — нужен внешний арбитраж
- REJECTED: большинство нод не согласны (disagree >= 2/3)
- corrected_answer: скорректированный ответ если PARTIALLY_VERIFIED, иначе null"""


def _call_ollama(prompt: str) -> str:
    resp = _session.post(
        f"{OLLAMA}/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": 0.1, "num_predict": 400}},
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


def _rule_based_verdict(result: ValidationResult) -> str:
    """Быстрый вердикт по правилам без LLM."""
    total    = len(result.validations)
    agree    = result.agree_count
    disagree = result.disagree_count
    if total == 0:
        return "CONFLICT_DETECTED"
    if agree >= total * 2 / 3:
        return "VERIFIED"
    if disagree >= total * 2 / 3:
        return "REJECTED"
    if agree > disagree:
        return "PARTIALLY_VERIFIED"
    return "CONFLICT_DETECTED"


def arbitrate(
    question: str,
    answer: str,
    validation: ValidationResult,
    use_llm: bool = True,
) -> ArbiterResult:
    """
    Вынести финальный вердикт на основе результатов валидации.

    Args:
        question:   оригинальный вопрос
        answer:     предварительный ответ
        validation: результаты параллельной валидации
        use_llm:    использовать LLM для анализа (True) или только правила (False)

    Returns:
        ArbiterResult
    """
    # Если ни одна нода не ответила — CONFLICT
    if not validation.validations:
        return ArbiterResult(
            verdict="CONFLICT_DETECTED",
            explanation="Ни одна нода не ответила в срок.",
            final_answer=None,
        )

    # Быстрый вердикт по правилам
    rule_verdict = _rule_based_verdict(validation)

    # Если всё однозначно — не тратим LLM
    if not use_llm or rule_verdict in ("VERIFIED", "REJECTED"):
        exp = {
            "VERIFIED":           f"Подтверждено {validation.agree_count}/{len(validation.validations)} нодами.",
            "REJECTED":           f"Отклонено {validation.disagree_count}/{len(validation.validations)} нодами.",
            "PARTIALLY_VERIFIED": f"Частично: {validation.agree_count} согласны, {validation.disagree_count} нет.",
            "CONFLICT_DETECTED":  f"Расхождение: {validation.agree_count} согласны, {validation.disagree_count} нет.",
        }
        return ArbiterResult(
            verdict=rule_verdict,
            explanation=exp.get(rule_verdict, ""),
            final_answer=None,
            raw="[rule-based]",
        )

    # LLM арбитраж для сложных случаев
    node_reports = "\n".join(
        f"- {v.node_id}: {v.verdict} — {v.reason[:100]}"
        for v in validation.validations
    )
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Вопрос: {question[:300]}\n\n"
        f"Ответ: {answer[:800]}\n\n"
        f"Результаты проверки нод:\n{node_reports}\n\n"
        f"Статистика: agree={validation.agree_count}, "
        f"disagree={validation.disagree_count}, "
        f"timeout={len(validation.timed_out)}"
    )

    try:
        raw  = _call_ollama(prompt)
        data = _extract_json(raw)
        verdict = data.get("verdict", rule_verdict)
        if verdict not in ("VERIFIED", "PARTIALLY_VERIFIED", "CONFLICT_DETECTED", "REJECTED"):
            verdict = rule_verdict
        return ArbiterResult(
            verdict=verdict,
            explanation=data.get("explanation", ""),
            final_answer=data.get("corrected_answer"),
            raw=raw,
        )
    except Exception as e:
        return ArbiterResult(
            verdict=rule_verdict,
            explanation=f"LLM недоступен: {e}. Вердикт по правилам.",
            final_answer=None,
            raw=f"[error: {e}]",
        )


if __name__ == "__main__":
    from agent.orch_schemas import NodeValidation, ValidationResult
    val = ValidationResult(
        validations=[
            NodeValidation(node_id="a", verdict="agree",    reason="Ответ верный", latency=8.0),
            NodeValidation(node_id="b", verdict="agree",    reason="Согласен",     latency=10.0),
            NodeValidation(node_id="c", verdict="partial",  reason="Неполно",      latency=12.0),
        ],
        agree_count=2, disagree_count=0, timed_out=[],
    )
    result = arbitrate("Что такое Kademlia?", "Kademlia — алгоритм маршрутизации...", val)
    print(f"Вердикт: {result.verdict}")
    print(f"Объяснение: {result.explanation}")
