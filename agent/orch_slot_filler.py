"""
orch_slot_filler.py — Динамическое заполнение слотов перед веб-поиском.

LLM разбирает запрос → находит недостающие слоты → задаёт уточняющие вопросы →
заполняет слоты ответами пользователя → строит точный запрос для веб-поиска.

Состояние хранится в Redis по session_id (TTL 30 минут).
Максимум 3 раунда уточнений.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

import redis as _redis_lib
import requests as _requests

OLLAMA     = "http://127.0.0.1:11434"
MODEL      = "heretic:q8"
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
SESSION_TTL = 1800   # 30 минут
MAX_ROUNDS  = 3

_session = _requests.Session()
_session.trust_env = False

_SKIP_HINTS = frozenset({"не знаю", "нет", "skip", "пропустить", "хз", "без разницы", "любой", "не важно"})

PARSE_PROMPT = """\
Ты анализатор запросов. Разбери запрос и определи что нужно уточнить для точного поиска.

Сегодня: {date}

Верни ТОЛЬКО JSON (без markdown, без пояснений):
{{
  "intent": "краткое описание намерения",
  "known_slots": {{"слот": "значение"}},
  "missing_slots": [
    {{"name": "слот", "importance": "critical|optional", "question": "вопрос пользователю на русском"}}
  ],
  "ready_to_search": false
}}

Правила:
- known_slots: что ЯВНО указано в тексте запроса (сезон, место, тип и т.д.)
- missing_slots: чего нет явно, но нужно для точного поиска. Максимум 2 слота.
- importance="critical" — без этого поиск вернёт неверный результат.
  Примеры critical слотов:
    • рыбалка → сезон (зима/весна/лето/осень)
    • погода → город
    • рецепт → главный ингредиент
    • покупка → бюджет или цель использования
    • маршрут → точка отправления и назначения
- importance="optional" — полезно, но поиск и без этого даст результат
- ready_to_search: ВСЕГДА false если есть хотя бы один critical слот.
  true ТОЛЬКО если все critical слоты заполнены или тема не требует уточнений.
- Вопросы короткие, предлагай варианты: "В какой сезон? (весна/лето/осень/зима)"
"""


# ── Redis ─────────────────────────────────────────────────────────────────────

def _r() -> _redis_lib.Redis:
    return _redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def _slot_key(session_id: str) -> str:
    return f"orch:slots:{session_id}"


def load_state(session_id: str) -> Optional[dict]:
    try:
        raw = _r().get(_slot_key(session_id))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def save_state(session_id: str, state: dict):
    try:
        _r().setex(_slot_key(session_id), SESSION_TTL, json.dumps(state, ensure_ascii=False))
    except Exception:
        pass


def clear_state(session_id: str):
    try:
        _r().delete(_slot_key(session_id))
    except Exception:
        pass


# ── LLM ───────────────────────────────────────────────────────────────────────

def _call_llm(prompt: str) -> str:
    resp = _session.post(
        f"{OLLAMA}/api/generate",
        json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 3000},
        },
        timeout=120,
    )
    return resp.json().get("response", "")


def _extract_first_json(text: str) -> dict:
    """Находит первый валидный JSON-объект в тексте — ищет по всему тексту включая think-блоки."""
    # Ищем все вхождения '{' и пробуем разобрать JSON от каждого
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            break
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(text[start:i + 1])
                        if isinstance(candidate, dict) and len(candidate) > 1:
                            return candidate
                    except json.JSONDecodeError:
                        pass
                    break
        pos = start + 1
    return {}


def parse_query(query: str) -> dict:
    """LLM разбирает запрос на known/missing слоты. До 2 попыток если модель вернула пустой результат."""
    prompt = PARSE_PROMPT.format(date=datetime.now().strftime("%Y-%m-%d")) + f"\n\nЗапрос: {query}"
    for attempt in range(2):
        try:
            raw = _call_llm(prompt)
            if raw:
                data = _extract_first_json(raw)
                if data:
                    # Если модель вернула пустые слоты на короткий запрос — подозрительно, повторим
                    missing = data.get("missing_slots") or []
                    known = data.get("known_slots") or {}
                    if attempt == 0 and not missing and not known and len(query.split()) < 6:
                        continue  # повторная попытка
                    return data
        except Exception:
            pass
    return {"ready_to_search": True, "known_slots": {}, "missing_slots": [], "intent": query}


# ── Логика слотов ─────────────────────────────────────────────────────────────

def _next_question(missing_slots: list[dict]) -> Optional[dict]:
    """Следующий вопрос: сначала critical, потом остальные."""
    for slot in missing_slots:
        if slot.get("importance") == "critical":
            return slot
    # Если critical нет — берём первый оставшийся
    return missing_slots[0] if missing_slots else None


def _apply_answer(state: dict, answer: str) -> dict:
    """Применить ответ пользователя к pending слоту."""
    pending = state.get("pending_question") or {}
    slot_name = pending.get("name", "")
    answer = answer.strip()
    if slot_name and answer.lower() not in _SKIP_HINTS:
        state["known_slots"][slot_name] = answer
    # Удалить отвеченный слот из missing
    state["missing_slots"] = [s for s in state.get("missing_slots", []) if s.get("name") != slot_name]
    state["pending_question"] = None
    # Готов к поиску если нет оставшихся critical
    state["ready_to_search"] = not any(s.get("importance") == "critical" for s in state["missing_slots"])
    return state


def build_enriched_query(original: str, known_slots: dict) -> str:
    """Собирает обогащённый запрос: оригинал + значения слотов."""
    parts = [original.strip()]
    for v in known_slots.values():
        v = str(v).strip()
        if v and v.lower() not in original.lower():
            parts.append(v)
    return " ".join(parts)


# ── Главная точка входа ────────────────────────────────────────────────────────

def process(query: str, session_id: str, user_answer: str = "") -> dict:
    """
    Обработать запрос с учётом состояния сессии.

    Args:
        query:       исходный запрос пользователя
        session_id:  ID сессии (для Redis)
        user_answer: ответ пользователя на предыдущий уточняющий вопрос

    Returns:
        {
          "ready":         bool,        # True → можно идти в веб-поиск
          "question":      str | None,  # уточняющий вопрос (если not ready)
          "enriched_query": str,        # обогащённый запрос для поиска
          "known_slots":   dict,        # заполненные слоты
          "rounds":        int,
        }
    """
    state = load_state(session_id)

    if state is None:
        # Первый вызов — парсим запрос через LLM
        parsed = parse_query(query)
        missing = parsed.get("missing_slots") or []
        has_critical = any(s.get("importance") == "critical" for s in missing)
        # Не доверяем ready_to_search если есть critical слоты
        ready = bool(parsed.get("ready_to_search", True)) and not has_critical
        state = {
            "original_query": query,
            "intent":         parsed.get("intent", query),
            "known_slots":    parsed.get("known_slots") or {},
            "missing_slots":  missing,
            "ready_to_search": ready,
            "pending_question": None,
            "rounds": 0,
        }
    elif user_answer:
        # Пользователь ответил — применяем ответ
        state = _apply_answer(state, user_answer)

    state["rounds"] = state.get("rounds", 0) + 1
    enriched = build_enriched_query(state["original_query"], state["known_slots"])

    # Принудительно завершаем уточнения после MAX_ROUNDS
    if state["rounds"] > MAX_ROUNDS:
        state["ready_to_search"] = True

    if state["ready_to_search"]:
        clear_state(session_id)
        return {
            "ready":          True,
            "question":       None,
            "enriched_query": enriched,
            "known_slots":    state["known_slots"],
            "rounds":         state["rounds"],
        }

    # Задаём следующий вопрос
    next_q = _next_question(state["missing_slots"])
    if next_q:
        state["pending_question"] = next_q
        save_state(session_id, state)
        return {
            "ready":          False,
            "question":       next_q["question"],
            "enriched_query": enriched,
            "known_slots":    state["known_slots"],
            "rounds":         state["rounds"],
        }

    # Нет вопросов — можно искать
    clear_state(session_id)
    return {
        "ready":          True,
        "question":       None,
        "enriched_query": enriched,
        "known_slots":    state["known_slots"],
        "rounds":         state["rounds"],
    }
