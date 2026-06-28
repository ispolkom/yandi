"""
agent/orch_codex_validator.py — Валидатор через GPT-5.5 (Codex CLI).

Заменяет браузерное расширение. Прямой subprocess-вызов codex exec.
Никакого браузера, никакой Redis-очереди для валидации.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

CODEX_BIN = "codex"  # должен быть в PATH на целевой машине
MODEL     = "gpt-5.5"
WORKDIR   = Path(__file__).parent.parent  # project root (yandi/)

CRITIQUE_PROMPT_TEMPLATE = """Проверь фактическую точность ответа. Отвечай plain text без markdown, без звёздочек, без заголовков.

Вопрос: {question}

Ответ для проверки:
{answer}

Ответь строго по пунктам (каждый — 1-2 предложения):
1. Есть ли фактические ошибки? (да / нет)
2. Если да — что именно неверно или устарело? (или "нет ошибок")
3. Есть ли важные дополнения, без которых ответ неполон? (да / нет)
4. Если да — что конкретно стоит добавить? (или "нет дополнений")
5. Итог одним словом:
   VERIFIED — всё верно и достаточно полно
   SUPPLEMENTED — верно, но есть важные дополнения (без фактических ошибок)
   PARTIALLY_VERIFIED — есть фактические ошибки (независимо от полноты)
   REJECTED — ответ принципиально неверен"""

# Алиас для обратной совместимости
PROMPT_TEMPLATE = CRITIQUE_PROMPT_TEMPLATE


def build_critique_prompt(question: str, answer: str) -> str:
    """Собрать готовый промпт для валидации — используется и Codex, и браузерными моделями."""
    return CRITIQUE_PROMPT_TEMPLATE.format(
        question=question,
        answer=answer[:2000],
    )


# ── Парсинг вывода codex exec ─────────────────────────────────────────────────

def _extract_response(raw: str) -> str:
    """Вытащить ответ модели из вывода codex exec."""
    # Формат: ...метадата... "--------" ... "codex\n{ответ}\ntokens used\n..."
    parts = raw.split("--------")
    convo = parts[-1].strip() if len(parts) >= 2 else raw.strip()

    if "\ncodex\n" in convo:
        after = convo.split("\ncodex\n", 1)[1]
        if "\ntokens used\n" in after:
            after = after.split("\ntokens used\n")[0]
        return after.strip()

    # Запасной вариант — последняя непустая строка
    lines = [l.strip() for l in convo.splitlines() if l.strip()]
    return lines[-1] if lines else raw.strip()


_VERDICT_RE = re.compile(
    r"^(\d+[\.\s]+)?(verified|partially[_\s]verified|supplemented|rejected|да|нет|частично)\.?$",
    re.IGNORECASE,
)

# Ищем пронумерованные пункты 1-5
_ITEM_RE = re.compile(r"^\s*(\d+)[.\)]\s+(.+)$", re.MULTILINE)


def _parse_items(text: str) -> dict[int, str]:
    """Извлечь пронумерованные пункты из ответа GPT-5.5."""
    items: dict[int, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = _ITEM_RE.match(line)
        if m:
            current = int(m.group(1))
            items.setdefault(current, []).append(m.group(2).strip())
        elif current and line.strip():
            items[current].append(line.strip())
    return {k: " ".join(v) for k, v in items.items()}


def parse_verdict(text: str) -> dict:
    """Разобрать структурированный ответ GPT-5.5 на вердикт, коррекцию и дополнение."""
    text_lower = text.lower()

    # Вердикт — приоритет по специфичности
    if "rejected" in text_lower:
        verdict = "REJECTED"
    elif "partially_verified" in text_lower or "partially verified" in text_lower:
        verdict = "PARTIALLY_VERIFIED"
    elif "supplemented" in text_lower:
        verdict = "SUPPLEMENTED"
    elif "verified" in text_lower:
        verdict = "VERIFIED"
    elif "частично" in text_lower:
        verdict = "PARTIALLY_VERIFIED"
    elif "нет" in text_lower.split():
        verdict = "REJECTED"
    else:
        verdict = "PARTIALLY_VERIFIED"

    items = _parse_items(text)

    # п.2 — что неверно (correction), п.4 — что добавить (supplement)
    correction = items.get(2, "")
    supplement = items.get(4, "")

    # Чистим "нет ошибок" / "нет дополнений"
    _none_re = re.compile(r"^(нет\s*(ошибок|дополнений)?\.?|nothing|none|всё верно\.?)$", re.I)
    if _none_re.match(correction.strip()):
        correction = ""
    if _none_re.match(supplement.strip()):
        supplement = ""

    return {
        "verdict":    verdict,
        "correction": correction,
        "supplement": supplement,
        "raw":        text[:3000],
        "ts":         time.time(),
    }


# ── Публичный API ─────────────────────────────────────────────────────────────

def validate(question: str, answer: str, model: str = MODEL) -> dict:
    """
    Вызвать GPT-5.5 через codex exec для валидации ответа.
    Возвращает dict: verdict, correction, raw, ts.
    """
    prompt = build_critique_prompt(question, answer)

    try:
        result = subprocess.run(
            [CODEX_BIN, "exec", "--skip-git-repo-check",
             "-c", f"model={model}", prompt],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(WORKDIR),
        )
        raw_out = result.stdout + (result.stderr or "")
        response = _extract_response(raw_out)
        parsed = parse_verdict(response)
        return parsed

    except subprocess.TimeoutExpired:
        return {
            "verdict":    "UNVERIFIED",
            "correction": "Таймаут GPT-5.5.",
            "raw":        "",
            "ts":         time.time(),
        }
    except Exception as e:
        return {
            "verdict":    "UNVERIFIED",
            "correction": str(e),
            "raw":        "",
            "ts":         time.time(),
        }


if __name__ == "__main__":
    # Быстрый тест
    q = "Какова средняя температура на поверхности Марса?"
    a = "Средняя температура поверхности Марса составляет около -63°C."
    r = validate(q, a)
    print("Verdict:", r["verdict"])
    print("Correction:", r["correction"][:300])
