"""
agent/refiner.py — Универсальный рефайнер ответов через Qwen (прямой Ollama).

Используется:
  - orch_dataset_runner.py  (после Codex-критики)
  - council_chat_server.py  (после критики от DeepSeek/GPT в веб-интерфейсе)

Не важно кто критиковал — Codex или DeepSeek. Qwen всегда получает
одинаковый промпт и пересобирает ответ с учётом замечаний.

ВАЖНО: вызываем Ollama напрямую, не через /api/orchestrator/ask.
Оркестратор публикует каждый запрос как "Вы" в чат — это нежелательно.
"""
from __future__ import annotations

import re
import time

import requests

OLLAMA_URL   = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "heretic:q8"

# Шаблоны мусора из system-prompt оркестратора
_NOISE_PATTERNS = [
    re.compile(r"<details>.*?</details>", re.DOTALL | re.IGNORECASE),
    re.compile(r"\*\*Проверка соответствия правилам:\*\*.*", re.DOTALL),
    re.compile(r"Проверка соответствия правилам.*", re.DOTALL),
    re.compile(r"\bИсточники:.*", re.DOTALL),
    re.compile(r"🔄\s*Отправлен на проверку.*", re.DOTALL),
]


def _clean_response(text: str) -> str:
    """Убрать артефакты system-prompt оркестратора из ответа Qwen."""
    for pat in _NOISE_PATTERNS:
        text = pat.sub("", text)
    return text.strip()

REFINE_TEMPLATE = """Ты дал ответ на вопрос, и получил критику. Учти замечания и дай уточнённый ответ.

Вопрос: {question}

Твой первый ответ:
{initial_answer}

Критика от {critic_model}:
{critique_block}

Правила:
- Исправь фактические ошибки если они указаны
- Добавь важное если указано что пропущено
- Не извиняйся и не ссылайся на критика — просто дай лучший ответ
- Без вступлений типа "Конечно!" или "Вот исправленный ответ:"
- Сразу по существу"""


def build_critique_block(
    correction: str = "",
    supplement: str = "",
    raw_critique: str = "",
) -> str:
    """Собрать блок критики из структурированных полей или свободного текста."""
    if raw_critique and not correction and not supplement:
        return raw_critique.strip()

    parts = []
    if correction:
        parts.append(f"Ошибки: {correction.strip()}")
    if supplement:
        parts.append(f"Что добавить: {supplement.strip()}")
    return "\n".join(parts) if parts else raw_critique.strip()


def refine(
    question: str,
    initial_answer: str,
    correction: str = "",
    supplement: str = "",
    raw_critique: str = "",
    critic_model: str = "GPT-5.5",
    timeout: int = 150,
) -> dict:
    """
    Попросить Qwen пересобрать ответ с учётом критики.

    Принимает либо структурированные correction/supplement (от Codex),
    либо raw_critique — свободный текст (от DeepSeek/GPT в браузере).

    Возвращает dict:
        text      — уточнённый ответ
        latency   — время генерации
        trust     — trust_level от оркестратора
        ok        — bool
        error     — строка ошибки (если ok=False)
    """
    critique_block = build_critique_block(correction, supplement, raw_critique)
    if not critique_block:
        return {"ok": False, "error": "Нет критики для рефайна", "text": "", "latency": 0}

    prompt = REFINE_TEMPLATE.format(
        question=question,
        initial_answer=initial_answer[:2000],
        critic_model=critic_model,
        critique_block=critique_block,
    )

    t0 = time.time()
    try:
        r = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=timeout,
            proxies={"http": None, "https": None},
        )
        d = r.json()
        raw_text = d.get("response", "")
        text = _clean_response(raw_text)
        return {
            "ok":      True,
            "text":    text,
            "trust":   "HYPOTHESIS",
            "latency": round(time.time() - t0, 1),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "text": "", "latency": round(time.time() - t0, 1)}
