"""
agent/orch_ai_validator.py — Оркестратор: канал валидации через DeepSeek.

Полностью изолированный Redis-канал, отдельный от council-чата.

Redis-ключи (префикс orch:ai:):
  orch:ai:queue            — LIST задач ожидающих отправки в DeepSeek
  orch:ai:result:{task_id} — STRING результат (TTL 10 мин)
  orch:ai:log              — LIST лог всех валидаций (последние 500)

Схема задачи:
  { task_id, query, frame, sources[], answer, ts }

Схема результата:
  { task_id, verdict, correction, additions, raw, ts }
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Optional

import redis as _redis

REDIS_URL = "redis://127.0.0.1:6379"

# ── Ключи ─────────────────────────────────────────────────────────────────────
QUEUE_KEY  = "orch:ai:queue"
RESULT_PFX = "orch:ai:result:"
LOG_KEY    = "orch:ai:log"
LOG_MAX    = 500
RESULT_TTL = 600  # 10 минут


def _r():
    client = _redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return client


# ── Промпт валидации ──────────────────────────────────────────────────────────

def _build_prompt(query: str, frame: dict, sources: list[dict], answer: str) -> str:
    # Матрица слотов
    parts = []
    if frame.get("object"):
        parts.append(f"Объект: {frame['object']}")
    if frame.get("action"):
        parts.append(f"Действие: {frame['action']}")
    c = frame.get("constraints") or {}
    if c:
        parts.append("Контекст: " + ", ".join(f"{k}={v}" for k, v in c.items() if v))
    if frame.get("missing"):
        parts.append("Неизвестно: " + ", ".join(frame["missing"]))
    matrix_str = "\n".join(parts) if parts else "(нет данных)"

    # Источники со ссылками
    src_lines = []
    for i, s in enumerate(sources[:5], 1):
        url   = s.get("url", "—")
        title = s.get("title", url)
        text  = s.get("text", "")[:300]
        src_lines.append(f"[{i}] {title}\n    {url}\n    {text}")
    sources_str = "\n\n".join(src_lines) if src_lines else "(веб-поиск не дал результатов)"

    return f"""Проверь фактическую точность ответа на основе своих знаний. Отвечай plain text — без markdown, заголовков, списков со звёздочками, таблиц. Только цифры и текст.

Вопрос: {query}

Ответ для проверки:
{answer}

Ответь строго по пунктам (каждый — 1 предложение):
1. Факты верны? (да / частично / нет)
2. Что конкретно неверно или устарело? (или "всё верно")
3. Что важное пропущено? (или "ничего")
4. Итог: VERIFIED / PARTIALLY_VERIFIED / REJECTED"""


# ── Push задачи ───────────────────────────────────────────────────────────────

def push_validation_task(
    query:   str,
    frame:   dict,
    sources: list[dict],
    answer:  str,
) -> str:
    """Добавить задачу в очередь. Возвращает task_id."""
    task_id = str(uuid.uuid4())
    prompt  = _build_prompt(query, frame, sources, answer)
    task = {
        "task_id": task_id,
        "model":   "deepseek",
        "text":    prompt,
        "ts":      time.time(),
        # мета для датасета
        "_query":  query,
        "_frame":  json.dumps(frame),
        "_answer": answer,
    }
    r = _r()
    r.rpush(QUEUE_KEY, json.dumps(task))
    r.close()
    return task_id


# ── Получение результата ──────────────────────────────────────────────────────

def get_validation_result(task_id: str, timeout: int = 300) -> Optional[dict]:
    """
    Блокирующее ожидание результата. timeout — секунды.
    Возвращает dict или None если истёк таймаут.
    """
    r     = _r()
    key   = RESULT_PFX + task_id
    start = time.time()
    while time.time() - start < timeout:
        raw = r.get(key)
        if raw:
            r.delete(key)
            r.close()
            return json.loads(raw)
        time.sleep(2)
    r.close()
    return None


# ── Парсинг ответа DeepSeek ───────────────────────────────────────────────────

_UI_NOISE = [
    "deepthink", "search", "ai-generated, for reference only",
    "ai-generated", "for reference only",
]

def _clean_raw(raw: str) -> str:
    """Убрать UI-мусор DeepSeek (кнопки, плашки) из захваченного текста."""
    lines = []
    for line in raw.splitlines():
        low = line.strip().lower()
        if any(noise in low for noise in _UI_NOISE):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


_VERDICT_WORDS = re.compile(
    r"^(\d+\s*)?(verified|partially[_\s]verified|rejected|да|нет|частично)$",
    re.IGNORECASE,
)

def parse_deepseek_verdict(raw: str) -> dict:
    """
    Структура ответа DeepSeek (нумерованная или по абзацам):
      [1] да/частично/нет          ← первый блок — отбрасываем
      [2..N-1] контент              ← берём ВСЁ
      [N] VERIFIED/PARTIALLY/REJECTED ← последний блок — отбрасываем
    """
    raw = _clean_raw(raw)
    raw_lower = raw.lower()

    # Определяем вердикт по всему тексту
    if "partially_verified" in raw_lower or "partially verified" in raw_lower:
        verdict = "PARTIALLY_VERIFIED"
    elif "rejected" in raw_lower:
        verdict = "REJECTED"
    elif "verified" in raw_lower:
        verdict = "VERIFIED"
    elif "частично" in raw_lower:
        verdict = "PARTIALLY_VERIFIED"
    elif "нет" in raw_lower.split():
        verdict = "REJECTED"
    else:
        verdict = "PARTIALLY_VERIFIED"

    # Разбиваем на блоки по двойному переносу
    blocks = [b.strip() for b in re.split(r"\n{2,}", raw) if b.strip()]

    # Убираем первый блок если это просто да/нет/частично
    if blocks and _VERDICT_WORDS.match(blocks[0].strip().rstrip(".")):
        blocks = blocks[1:]

    # Убираем последний блок если это слово-вердикт на английском
    if blocks and _VERDICT_WORDS.match(blocks[-1].strip().rstrip(".")):
        blocks = blocks[:-1]

    # Убираем числовые префиксы "1 ", "2.", "3 " в начале каждого блока
    def strip_num(s: str) -> str:
        return re.sub(r"^\d+[\.\s]+", "", s).strip()

    blocks = [strip_num(b) for b in blocks if strip_num(b)]

    # Весь контент — одним текстом
    content = "\n\n".join(blocks)

    return {
        "verdict":    verdict,
        "correction": content,   # всё в одном поле
        "additions":  "",
        "raw":        raw[:3000],
        "ts":         time.time(),
    }


# ── Запись в лог ──────────────────────────────────────────────────────────────

def log_validation(task_id: str, query: str, frame: dict, answer: str, result: dict):
    """Пишем запись в orch:ai:log для датасета."""
    entry = {
        "task_id":  task_id,
        "query":    query,
        "frame":    frame,
        "answer":   answer,
        "verdict":  result.get("verdict"),
        "correction": result.get("correction"),
        "additions":  result.get("additions"),
        "raw":      result.get("raw"),
        "ts":       time.time(),
    }
    r = _r()
    r.lpush(LOG_KEY, json.dumps(entry, ensure_ascii=False))
    r.ltrim(LOG_KEY, 0, LOG_MAX - 1)
    r.close()
