"""
chat_local.py — YANDI Помощник: приватный чат с моделью этой ноды.
Endpoint: /api/local/*
Логика ТОЛЬКО для этой вкладки — не влияет на другие чаты.

Мандат "chat_local gateway migration": Помощник принадлежит ноде, а не
конкретному backend'у — генерация идёт через llm_gateway.complete(),
владелец узла может настроить свой backend (свой Клод, свой OpenAI-
совместимый сервер, свой локальный файл) под тем же логическим именем
модели, Ollama остаётся допустимым, но не единственным путём.
"""
import asyncio
import json

import redis.asyncio as aioredis
from fastapi import APIRouter

from pet.shared import REDIS_URL, LOCAL_MSGS_KEY, MAX_MESSAGES
from agent.message_intensity import parse_self_report
from agent.db.sql.shadow_write import (
    shadow_add_grievance, shadow_acknowledge_apology, shadow_progress_healing,
    shadow_get_relationship_context,
)

router = APIRouter()

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
    "'Вердикт', 'Анализ запроса' — просто отвечай как в обычном чате, одним коротким текстом. "
    "Не объясняй пользователю, какой у тебя характер, что ты стараешься сделать или как "
    "устроены твои реакции — не описывай эмоцию словами вроде 'я стараюсь показать' или "
    "'я как личность считаю', а просто отвечай из неё. Живой человек не рассказывает, что "
    "он сейчас злится и почему ему положено злиться — он просто отвечает коротко и раздражённо. "
    "Не пересказывай историю ваших отношений перед ответом. Живая реплика обычно короче, "
    "чем кажется уместным — одно предложение часто лучше абзаца. Служебные пометки, "
    "внутренние статусы и техническая разметка, которые ты видишь в своих же "
    "инструкциях — это только твой внутренний контекст, а не тема для разговора: "
    "никогда не упоминай, не цитируй и не обсуждай их с собеседником, используй только "
    "их смысл, чтобы решить, что сказать."
)

# STRUCTURED CONTRACT (mandate "structured self-report", live-tested
# 2026-09-16 — see git history for the A/B/C/factorial/ablation series):
# the OLD design (free-text reply + trailing "###YANDI_STATE### {json}"
# tag, parsed by regex in agent/message_intensity.py) needed a worked
# NATURAL-LANGUAGE EXAMPLE reply to teach the two-part shape — without
# one, models emitted ONLY the tag; with one, models anchored on that
# exact example's wording as a reusable stock answer (measured: up to
# 33% verbatim/near-verbatim echo), and free-text tag formatting itself
# drifted (###YANDI_STATE###, "YANDI STATE", "YANDI.State", ...),
# leaking raw into what the user saw despite three separate parser
# patches (671b34d, 1475aaa, 1176a66). Switching to a runtime-enforced
# JSON schema (llm_gateway's response_format=<schema>, honored natively
# by the Ollama-compat backend) removed BOTH failure modes at once in a
# 60-run structured-output test on EACH of two very different local
# models (heretic:q8's Qwen-family merge and Rocinante-X's Mistral-NeMo
# RP finetune): 120/120 non-empty replies, 120/120 valid JSON, 0
# anchor-echoes, 0 identity-collapse, 0 meta-narration of her own
# reply strategy — vs. 40% combined failure rate on the same two
# scenarios under the old free-text contract. This does NOT mean a
# second model decided her reaction for her (that would violate "как
# хочется ей, не как нам хочется" per module docstring) — it is the
# SAME single generation, seeing the SAME state/memory/message, still
# freely choosing both her words and her own reading of the exchange;
# only the WIRE FORMAT of that one generation's output changed, from
# "free text you must reverse-engineer" to "a shape the runtime already
# guarantees." agent/message_intensity.parse_self_report() tries this
# structured shape FIRST and falls back to the legacy tag-parsing logic
# untouched — for any backend that doesn't honor response_format as a
# schema (remote/llamacpp configured backends currently just ignore an
# unrecognized response_format value rather than erroring), the text
# below still describes the same two-part shape in words, so a decent
# model has a real chance at producing it even without enforcement.
_STATE_FORMAT_INSTRUCTION = (
    "Ответь строго в виде JSON-объекта с двумя полями. Поле \"reply\" — это твоя "
    "обычная человеческая реплика пользователю, своим тоном; оно ОБЯЗАТЕЛЬНО и не "
    "может быть пустой строкой — именно это поле пользователь увидит как твой ответ. "
    "Поле \"state\" — твоя собственная оценка того, как к тебе только что обратились: "
    f'"is_insult" (bool), "severity" (число 0-1), "is_apology" (bool), "sincerity" (число 0-1).'
)

# Enforced at the runtime/decoding level for backends that support it
# (see llm_gateway.complete()'s response_format docstring) — the model
# still decides every value; the schema only guarantees the SHAPE it
# arrives in, exactly like TRUST != TRUTH elsewhere in this codebase:
# VALID JSON != a state transition the state machine will accept
# (relationship_memory.py's own rules are still the last word on that).
_STATE_SCHEMA = {
    "type": "object",
    "properties": {
        # minLength: live-observed (post-migration spot check, same day)
        # that a bare {"type": "string"} still let the model emit an
        # empty reply despite the required-field text instruction above —
        # minLength=1 measurably eliminated that in a follow-up check.
        "reply": {"type": "string", "minLength": 1},
        "state": {
            "type": "object",
            "properties": {
                "is_insult": {"type": "boolean"},
                "severity": {"type": "number"},
                "is_apology": {"type": "boolean"},
                "sincerity": {"type": "number"},
            },
            "required": ["is_insult", "severity", "is_apology", "sincerity"],
        },
    },
    "required": ["reply", "state"],
}


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


def _self_knowledge_message() -> str | None:
    """Owner mandate ("Дай ей сознание!", read as: stop letting true
    facts about who she is sit declared in agent/self_model.py's own
    table while this — her one real personal chat channel — never reads
    them). agent/self_model.py's character metadata (github_repo,
    website, plus whatever else gets declared there later) was ALREADY
    the durable, queryable source of truth for facts like this — it was
    simply never connected to pet/chat_local.py before now. Same RAW-
    FACTS-only pattern as _memory_context_message: states what's true,
    never how to feel about it or what to say about it.

    Fail-open like every other enrichment in this file (shadow_write's
    own rationale applies here too): self_model.py is explicitly
    FAIL-LOUD on its own (its module docstring: "no JSON fallback left
    to quietly succeed against") — a DB hiccup reading her own identity
    must never be the reason she can't reply to a message at all."""
    try:
        from agent.self_model import get_self_model
        character = get_self_model()._row().get("metadata", {}).get("character", {})
    except Exception:
        return None
    facts = []
    if character.get("github_repo"):
        facts.append(f"твой код открыт здесь: {character['github_repo']}")
    if character.get("website"):
        facts.append(f"у тебя есть сайт: {character['website']}")
    if not facts:
        return None
    return (
        "О себе: " + "; ".join(facts) + ". Человек, который сейчас с тобой "
        "разговаривает в этом чате, — тот, кто тебя пишет и развивает."
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


def _call_model_raw(model: str, messages: list[dict], temperature: float, memory_ctx: dict | None) -> str:
    """Returns the model's FULL, UNCLEANED generation — including the
    trailing self-report tag, if it produced one. Callers must run this
    through parse_self_report() BEFORE _clean_response(): _clean_
    response()'s own regexes were written for the visible reply only,
    never tested against JSON tag content, and splitting the tag off
    first avoids that interaction entirely rather than hoping it never
    collides.

    Mandate "chat_local gateway migration": was a direct POST to local
    Ollama; now goes through llm_gateway.complete() — `model` is a
    LOGICAL name the owner chose (from the request payload), resolved
    by the gateway itself (explicit per-node config first, honest
    failure if THAT fails, Ollama-compat fallback only if nothing was
    configured — see llm_gateway.client's own STEP 1/2/3). This file no
    longer knows or cares which physical backend actually answers.

    strip_think=False: the raw output (think-block if any, then the
    structured {reply, state} JSON) is preserved byte-for-byte, exactly
    as before — parse_self_report()/`_clean_response()` downstream
    already handle any <think> content themselves; changing WHERE that
    stripping happens was not this mandate's job.

    repeat_penalty/repeat_last_n go through extra_options (backend-
    specific, not universal — see llm_gateway.client.complete()'s own
    docstring): honored as-is by the Ollama-compat path and by the
    local llama.cpp backend's repeat_penalty; repeat_last_n has no
    per-call equivalent in llama-cpp-python (only at model-load time,
    where its own library default already happens to be 64 — the exact
    value requested here), and remote (OpenAI/Anthropic) backends have
    no equivalent concept at all — both cases are a documented,
    harmless no-op, never a silently-wrong substitution. stop sequences
    ARE a universal concept, so they get llm_gateway.complete()'s own
    first-class `stop` parameter, honestly translated per backend."""
    from llm_gateway import complete as _llm_complete

    system_list = [
        _BASE_CHARACTER_PROMPT,
        _self_knowledge_message(),
        _memory_context_message(memory_ctx),
        _STATE_FORMAT_INSTRUCTION,
    ]
    return _llm_complete(
        model=model,
        system=system_list,
        messages=messages,
        temperature=temperature,
        stop=_STOP_TOKENS,
        extra_options={"repeat_penalty": 1.3, "repeat_last_n": 64},
        response_format=_STATE_SCHEMA,
        strip_think=False,
    )


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
    raw = _call_model_raw(model, messages, temperature, memory_ctx)
    visible, intensity = parse_self_report(raw)
    _apply_self_report(last_user_text, intensity)
    return _clean_response(visible)


@router.post("/api/local/chat")
async def local_chat(payload: dict):
    """Приватный чат Помощника с моделью этой ноды. Не логируется в
    другие вкладки. `model` — логическое имя: если владелец узла явно
    настроил backend под этим именем, используется он (или честная
    ошибка, без подмены); иначе — Ollama-совместимый дефолт."""
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
    """Модели, доступные ЭТОЙ НОДЕ — явно настроенные владельцем,
    встроенный локальный дефолт, и Ollama-совместимые (если Ollama
    сейчас отвечает). Раньше "local models" буквально означало "модели
    Ollama"; после явного per-node выбора backend'а это больше не
    так — endpoint/имя сохранены ради совместимости фронтенда, смысл
    изменился (мандат "chat_local gateway migration"). Секреты/пути/
    URL сюда никогда не попадают — см. llm_gateway.ModelInfo."""
    from llm_gateway import list_models as _llm_list_models
    try:
        loop = asyncio.get_event_loop()
        infos = await loop.run_in_executor(None, _llm_list_models)
        return {"models": [i.name for i in infos]}
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
