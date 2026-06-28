"""
assistant/orch_session.py — Session Context Manager.
Хранит историю вопросов и уточнений сессии в Redis (TTL=4h).
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

import redis as _redis

REDIS_HOST   = "127.0.0.1"
REDIS_PORT   = 6379
SESSION_TTL  = 14400   # 4 часа
PREFIX       = "orch:session:"
MAX_MESSAGES = 20      # максимум сообщений в истории


def _r() -> _redis.Redis:
    return _redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def new_session_id() -> str:
    return str(uuid.uuid4())[:8]


def get_context(session_id: str) -> list[dict]:
    """Получить историю сообщений сессии."""
    if not session_id:
        return []
    try:
        raw = _r().get(PREFIX + session_id)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return []


def add_message(session_id: str, role: str, content: str):
    """Добавить сообщение в историю сессии."""
    if not session_id:
        return
    try:
        r   = _r()
        key = PREFIX + session_id
        ctx = get_context(session_id)
        ctx.append({"role": role, "content": content, "ts": time.time()})
        # Ограничить историю
        if len(ctx) > MAX_MESSAGES:
            ctx = ctx[-MAX_MESSAGES:]
        r.setex(key, SESSION_TTL, json.dumps(ctx, ensure_ascii=False))
    except Exception:
        pass


def clear_session(session_id: str):
    try:
        _r().delete(PREFIX + session_id)
    except Exception:
        pass


def get_recent_questions(session_id: str, n: int = 3) -> list[str]:
    """Последние N вопросов пользователя из сессии."""
    ctx = get_context(session_id)
    return [m["content"] for m in ctx if m.get("role") == "user"][-n:]


if __name__ == "__main__":
    sid = new_session_id()
    add_message(sid, "user", "Как работает DHT?")
    add_message(sid, "assistant", "DHT — распределённая хэш-таблица...")
    add_message(sid, "user", "А что такое Kademlia?")
    ctx = get_context(sid)
    print(f"Session {sid}: {len(ctx)} messages")
    print("Recent questions:", get_recent_questions(sid))
    clear_session(sid)
