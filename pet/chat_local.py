"""
chat_local.py — YANDI Помощник: приватный чат с локальной Ollama-моделью.
Endpoint: /api/local/*
Логика ТОЛЬКО для этой вкладки — не влияет на другие чаты.
"""
import asyncio
import json

import redis.asyncio as aioredis
from fastapi import APIRouter

from pet.shared import REDIS_URL, LOCAL_MSGS_KEY, MAX_MESSAGES

router = APIRouter()

_OLLAMA_URL = "http://127.0.0.1:11434"

# Системный промпт и параметры — только для Помощника
_SYSTEM_PROMPT = (
    "Ты дружелюбный ИИ-помощник. Отвечай кратко и по делу. "
    "НЕ пиши '## Response', '## Answer', '### ', 'Вердикт', 'Анализ запроса'. "
    "Отвечай на том же языке, на котором написан вопрос. "
    "НЕ дублируй ответ на другом языке. "
    "Просто отвечай как в обычном чате — одним коротким текстом."
)

_STOP_TOKENS = [
    "\nassistant\n", "\nuser\n", "<|im_start|>", "<|endoftext|>",
    "\nTranslate to ", "\nNote: The ", "\nHere is the translation",
    "\nThis is a translation", "\nThe phrase", "\nWould you like",
    "\nIn Russian", "\nIn English", "\nThe word", "\nThe text",
    "\n## ", "\n### ", "assistant\n\n##",
]

_CLEANUP_TOKENS = (
    "<|endoftext|>", "<|im_start|>", "<|im_end|>", "</s>", "<|end|>", "<|eot_id|>",
    "Translate to English:", "Translate to Russian:", "Here is the translation",
    "Note: The original", "This is a translation", "This phrase", "The phrase",
    "Would you like me", "In Russian:", "In English:", "The word ",
    "The text above", "Note that",
)


def _dedup_paragraphs(text: str) -> str:
    """Обрезает текст при первом повторе абзаца (модель зациклилась)."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    seen: set[str] = set()
    result = []
    for p in paragraphs:
        key = p[:80]
        if key in seen:
            break
        seen.add(key)
        result.append(p)
    return "\n\n".join(result)


def _clean_response(raw: str) -> str:
    import re
    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"</?think>", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<\|[^|]+\|>", "", raw)
    for tok in _CLEANUP_TOKENS:
        if tok in raw:
            raw = raw.split(tok)[0]
    # Обрезать role-маркер "assistant" в середине
    raw = re.sub(r'\s*\bassistant\b\s*(\n|$).*', '', raw, flags=re.DOTALL | re.IGNORECASE)
    # Обрезать ## блоки (дублирующий ответ)
    raw = re.split(r'\n## |\n### ', raw)[0]
    raw = re.sub(r"\n*(assistant|user|system)\s*:?\s*$", "", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"^[^а-яёА-ЯЁa-zA-Z0-9(\"'«]+", "", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return _dedup_paragraphs(raw).strip()


def _call_ollama(model: str, messages: list[dict], temperature: float) -> str:
    import requests
    s = requests.Session()
    s.trust_env = False
    full_msgs = [{"role": "system", "content": _SYSTEM_PROMPT}] + messages
    resp = s.post(
        f"{_OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "messages": full_msgs,
            "stream": False,
            "options": {
                "temperature": temperature,
                "repeat_penalty": 1.3,
                "repeat_last_n": 64,
            },
            "stop": _STOP_TOKENS,
        },
        timeout=60,
    )
    resp.raise_for_status()
    raw = resp.json().get("message", {}).get("content", "")
    return _clean_response(raw)


@router.post("/api/local/chat")
async def local_chat(payload: dict):
    """Приватный чат Помощника с Ollama. Не логируется в другие вкладки."""
    model       = (payload.get("model") or "heretic:q8").strip()
    temperature = float(payload.get("temperature", 0.7))
    messages    = payload.get("messages", [])
    if not messages:
        return {"ok": False, "error": "empty messages"}
    loop = asyncio.get_event_loop()
    try:
        content = await loop.run_in_executor(
            None, lambda: _call_ollama(model, messages, temperature)
        )
        return {"ok": True, "content": content}
    except Exception as e:
        return {"ok": False, "error": str(e), "content": f"❌ {e}"}


@router.get("/api/local/models")
async def local_models():
    """Список моделей в Ollama."""
    import requests
    try:
        s = requests.Session(); s.trust_env = False
        r = s.get(f"{_OLLAMA_URL}/api/tags", timeout=5)
        return {"models": [m["name"] for m in r.json().get("models", [])]}
    except Exception as e:
        return {"models": [], "error": str(e)}


@router.get("/api/local/history")
async def local_history():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lrange(LOCAL_MSGS_KEY, 0, MAX_MESSAGES - 1)
    await r.aclose()
    return {"messages": [json.loads(m) for m in reversed(raw)]}


@router.post("/api/local/message")
async def local_save_message(payload: dict):
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.lpush(LOCAL_MSGS_KEY, json.dumps(payload))
    await r.ltrim(LOCAL_MSGS_KEY, 0, MAX_MESSAGES - 1)
    await r.aclose()
    return {"ok": True}


@router.post("/api/local/clear")
async def local_clear_history():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.delete(LOCAL_MSGS_KEY)
    await r.aclose()
    return {"ok": True}
