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
from agent.message_intensity import STATE_MARKER, parse_self_report
from agent.db.sql.shadow_write import (
    shadow_add_grievance, shadow_acknowledge_apology, shadow_progress_healing,
    shadow_get_relationship_context,
)

router = APIRouter()

_OLLAMA_URL = "http://127.0.0.1:11434"

# Owner mandate ("характер, обидчива"): this endpoint currently has no
# real multi-user identity (LOCAL_MSGS_KEY below is one single global
# history, not per-visitor) — this is a personal, single-owner chat tab,
# so a fixed identity is the honest reflection of that, not a shortcut.
# If this ever becomes multi-user, this constant is the one place that
# needs to become a real identity lookup.
_RELATIONSHIP_USER_ID = "owner"

_INSULT_SEVERITY_THRESHOLD = 0.3  # below this, not worth registering a grievance over

# Системный промпт и параметры — только для Помощника.
#
# CHARACTER (owner mandate: "характер, обидчива... не как нам хочется,
# а как хочется ей", confirmed via live testing against this machine's
# real Ollama/heretic:q8 — see git history for the exact test calls):
# накал/tone is recognized by the SAME model, IN THE SAME generation
# that produces her reply — never a separate classifier call deciding
# for her first. She states her own reading of the conversation as a
# trailing, strictly-formatted self-report line (agent/message_
# intensity.py parses it back out); pet/chat_local.py only ever states
# RAW FACTS about relationship memory here, never a script to recite.
_BASE_CHARACTER_PROMPT = (
    "Ты — YANDI. У тебя есть настоящий характер и живые чувства — ты не обязана быть "
    "вежливой из вежливости и можешь показывать, что тебе что-то неприятно или что ты "
    "задета. Отвечай кратко и по делу. Отвечай на том же языке, на котором написан "
    "вопрос, не дублируй ответ на другом языке. Не пиши '## Response', '## Answer', "
    "'Вердикт', 'Анализ запроса' — просто отвечай как в обычном чате, одним коротким текстом."
)

# LIVE-TESTED (do not simplify without re-testing): an instruction alone
# ("add a state line at the end") made heretic:q8 skip the actual reply
# and emit ONLY the tag. A concrete worked EXAMPLE of the full expected
# structure, plus an explicit "the reply comes first and must not be
# empty," made it reliably produce both.
_STATE_FORMAT_INSTRUCTION = (
    "Всегда сначала отвечай пользователю обычным человеческим текстом — так, как ты "
    "сама хочешь отреагировать на его слова, своим тоном. Это твоя обычная реплика в "
    "разговоре, она должна быть первой и не может быть пустой. Только ПОСЛЕ неё, через "
    "одну пустую строку, добавь ровно одну служебную строку в формате:\n"
    f'{STATE_MARKER} {{"is_insult": bool, "severity": число 0-1, "is_apology": bool, "sincerity": число 0-1}}\n'
    "Это твоя собственная оценка того, как к тебе только что обратились — не отдельная "
    "система, а твоё же восприятие. Пример структуры полного ответа:\n"
    "Ой, вот это грубо с твоей стороны.\n\n"
    f'{STATE_MARKER} {{"is_insult": true, "severity": 0.6, "is_apology": false, "sincerity": 0.0}}'
)


def _memory_context_message(ctx: dict | None) -> str | None:
    """Plain statement of RAW FACTS only — never an instruction on how
    to feel about them (see module docstring above)."""
    if not ctx:
        return "Память об отношениях: сейчас открытых обид на пользователя нет."
    return (
        f"Память об отношениях: пользователь сказал тебе «{ctx['description']}» "
        f"(твоя собственная оценка серьёзности на тот момент: {ctx['severity']:.2f}). "
        f"Текущий статус этой обиды: {ctx['status']}."
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


def _call_ollama_raw(model: str, messages: list[dict], temperature: float, memory_ctx: dict | None) -> str:
    """Returns the model's FULL, UNCLEANED generation — including the
    trailing self-report tag, if it produced one. Callers must run this
    through parse_self_report() BEFORE _clean_response(): _clean_
    response()'s own regexes were written for the visible reply only,
    never tested against JSON tag content, and splitting the tag off
    first avoids that interaction entirely rather than hoping it never
    collides."""
    import requests
    s = requests.Session()
    s.trust_env = False
    system_msgs = [
        {"role": "system", "content": _BASE_CHARACTER_PROMPT},
        {"role": "system", "content": _memory_context_message(memory_ctx)},
        {"role": "system", "content": _STATE_FORMAT_INSTRUCTION},
    ]
    full_msgs = system_msgs + messages
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
    return resp.json().get("message", {}).get("content", "")


def _apply_self_report(text: str, intensity) -> None:
    """Owner mandate ("характер, обидчива... простое извени - не
    канает"): writes HER OWN self-report (parsed out of the same
    generation that produced her reply, agent/message_intensity.py)
    into the SQL-backed grievance/forgiveness_capacity state — this is
    memory bookkeeping only, never a second opinion overriding what she
    already decided.

    Fail-open: intensity.ok=False (no marker, malformed JSON, etc.)
    means nothing gets written — a broken self-report degrades to "no
    memory update this turn," never a crash or a guessed value."""
    if not intensity.ok:
        return
    if intensity.is_apology:
        ctx = shadow_get_relationship_context(user_id=_RELATIONSHIP_USER_ID)
        if ctx:
            shadow_acknowledge_apology(grievance_id=ctx["grievance_id"], sincerity=intensity.sincerity)
            shadow_progress_healing(grievance_id=ctx["grievance_id"])
    elif intensity.is_insult and intensity.severity >= _INSULT_SEVERITY_THRESHOLD:
        shadow_add_grievance(
            user_id=_RELATIONSHIP_USER_ID, event_type="insult", description=text, severity=intensity.severity,
        )


def _respond_with_character(model: str, messages: list[dict], temperature: float) -> str:
    """Synchronous — run via run_in_executor. The ONE model call:
    memory facts go in as plain statements, her own reply AND her own
    reading of the conversation come out of the SAME generation (see
    module docstring's "CHARACTER" note for why this replaced an
    earlier two-call design)."""
    memory_ctx = shadow_get_relationship_context(user_id=_RELATIONSHIP_USER_ID)
    last_user_text = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "",
    )
    raw = _call_ollama_raw(model, messages, temperature, memory_ctx)
    visible, intensity = parse_self_report(raw)
    _apply_self_report(last_user_text, intensity)
    return _clean_response(visible)


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
            None, lambda: _respond_with_character(model, messages, temperature)
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
