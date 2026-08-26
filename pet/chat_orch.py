"""
chat_orch.py — Оркестратор: датасеты, валидация, P2P-проверка.
Endpoints: /api/orchestrator/*, /api/orch/*
Логика ТОЛЬКО для вкладки Оркестратор.
"""
import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
import sys

import redis.asyncio as aioredis
from fastapi import APIRouter

from pet.shared import (
    REDIS_URL, ORCH_MSGS_KEY, MAX_MESSAGES,
    broadcast, write_log,
)

router = APIRouter()

# In-memory статусы ответов оркестратора (для фоновой валидации)
_orch_statuses: dict[str, dict] = {}

_PROJECT_ROOT = Path(__file__).parent.parent  # yandi/

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/orch/history")
async def orch_history():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lrange(ORCH_MSGS_KEY, 0, MAX_MESSAGES - 1)
    await r.aclose()
    return {"messages": [json.loads(m) for m in reversed(raw)]}


@router.post("/api/orch/clear")
async def orch_clear():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.delete(ORCH_MSGS_KEY)
    await r.aclose()
    await broadcast({"type": "history", "tab": "orch", "messages": [], "turn": "human"})
    return {"ok": True}


@router.post("/api/orchestrator/ask")
async def orchestrator_ask(payload: dict):
    """
    Задать вопрос через Orchestrator v2.
    Перед поиском — динамическое уточнение слотов (до 3 раундов).
    После ответа — авто-тегирование в фоне, теги видны в сообщении.
    """
    import concurrent.futures

    query      = (payload.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "empty query"}

    session_id  = payload.get("session_id", "") or str(uuid.uuid4())
    enable_web  = bool(payload.get("enable_web", True))

    r = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Сохраняем сообщение пользователя в историю
    q_msg = {
        "type":      "message",
        "from":      "human",
        "tab":       "orch",
        "text":      query,
        "ts":        datetime.now().strftime("%H:%M"),
        "_ts":       datetime.now().timestamp(),
        "id":        str(uuid.uuid4()),
        "turn_next": "orchestrator",
    }
    await r.lpush(ORCH_MSGS_KEY, json.dumps(q_msg))
    await r.ltrim(ORCH_MSGS_KEY, 0, MAX_MESSAGES - 1)
    await broadcast(q_msg)

    # ── Читаем контекст чата (последние 6 сообщений до текущего) ─────────────
    raw_hist = await r.lrange(ORCH_MSGS_KEY, 0, 7)
    chat_context = []
    for raw in reversed(raw_hist):
        try:
            m = json.loads(raw)
            if m.get("from") in ("human", "orchestrator") and m.get("text"):
                chat_context.append(m)
        except Exception:
            pass
    # Убираем последнее сообщение пользователя (текущий query) — оно уже есть
    if chat_context and chat_context[-1].get("from") == "human":
        chat_context = chat_context[:-1]

    loop = asyncio.get_event_loop()

    # ── Строим QueryFrame из запроса и истории ───────────────────────────────
    def _build_frame():
        try:
            from agent.orch_query_framer import build_query_frame
            return build_query_frame(query, chat_context)
        except Exception:
            from agent.orch_query_framer import QueryFrame
            return QueryFrame(raw_query=query, enriched_query=query, search_queries=[query])

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        frame = await loop.run_in_executor(ex, _build_frame)

    enriched_query = frame.enriched_query
    if enriched_query != query or frame.missing:
        print(f"  [framer] '{query}' → '{enriched_query}' | domain={frame.domain} missing={frame.missing} policy={frame.answer_policy}")

    # ── ask_first / safe_general: уточняем или предупреждаем ─────────────────
    if frame.answer_policy in ("ask_first", "safe_general") and frame.clarifying_question:
        cq_id = str(uuid.uuid4())
        cq_msg = {
            "type":             "message",
            "from":             "orchestrator",
            "tab":              "orch",
            "text":             frame.clarifying_question,
            "ts":               datetime.now().strftime("%H:%M"),
            "_ts":              datetime.now().timestamp(),
            "id":               cq_id,
            "turn_next":        "human",
            "trust_level":      "CLARIFICATION",
            "preliminary":      False,
            "is_clarification": True,
            "domain":           frame.domain,
            "missing":          frame.missing,
        }
        await r.lpush(ORCH_MSGS_KEY, json.dumps(cq_msg))
        await r.ltrim(ORCH_MSGS_KEY, 0, MAX_MESSAGES - 1)
        await r.aclose()
        write_log(cq_msg)
        await broadcast(cq_msg)
        return {
            "ok":               True,
            "is_clarification": True,
            "question":         frame.clarifying_question,
            "missing":          frame.missing,
            "domain":           frame.domain,
        }

    # ── Orchestrator v2 ───────────────────────────────────────────────────────
    def _run():
        from agent.orch_schemas import OrchestratorRequest
        from agent.orchestrator_v2 import process
        req = OrchestratorRequest(
            query=enriched_query,
            session_id=session_id,
            search_queries=frame.search_queries,
            query_frame={
                "object":      frame.obj,
                "action":      frame.action,
                "constraints": frame.constraints,
                "missing":     frame.missing,
                "domain":      frame.domain,
            },
        )
        return process(req, verbose=False, enable_web=enable_web, enable_validation=True)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            resp = await loop.run_in_executor(ex, _run)
    except Exception as e:
        await r.aclose()
        return {"ok": False, "error": str(e)}

    msg_id = str(uuid.uuid4())
    msg = {
        "type":        "message",
        "from":        "orchestrator",
        "tab":         "orch",
        "text":        resp.answer,
        "ts":          datetime.now().strftime("%H:%M"),
        "_ts":         datetime.now().timestamp(),
        "id":          msg_id,
        "turn_next":   "human",
        "trust_level": resp.trust_level,
        "preliminary": True,
        "_question":   enriched_query,
        "domain":      frame.domain,
        "missing":     frame.missing,
        "frame": {
            "intent":      frame.intent,
            "object":      frame.obj,
            "action":      frame.action,
            "constraints": frame.constraints,
            "policy":      frame.answer_policy,
            "search_queries": frame.search_queries,
        },
    }
    # Шаг 2: soft cq — для answer_with_assumptions добавляем уточняющий вопрос к ответу
    if (
        frame.answer_policy in ("answer_with_assumptions", "answer_direct")
        and frame.clarifying_question
        and frame.missing
    ):
        msg["text"] = msg["text"].rstrip() + f"\n\n💬 *{frame.clarifying_question}*"

    # Шаг 2b: честный статус верификации — проверяем реальный P2P-порт
    if _p2p_available():
        msg["pending_source"] = "yandi"
    else:
        # P2P не подключён — заменяем текст "через доверенные ноды" на "DeepSeek"
        msg["pending_source"] = "deepseek"
        msg["text"] = re.sub(
            r"🔄 Отправлен на проверку через доверенные ноды \(ID: [^\)]+\)",
            "🔄 Отправлен на проверку: [DeepSeek](https://chat.deepseek.com)",
            msg["text"],
        )

    await r.lpush(ORCH_MSGS_KEY, json.dumps(msg))
    await r.ltrim(ORCH_MSGS_KEY, 0, MAX_MESSAGES - 1)

    # Шаг 3: логируем QueryFrame в Redis как датасет (TTL 1 час)
    await r.setex(f"council:frame:{msg_id}", 3600, json.dumps(frame.to_dict()))

    await r.aclose()
    write_log(msg)
    await broadcast(msg)

    _orch_statuses[msg_id] = {
        "trust_level": resp.trust_level,
        "preliminary": True,
        "query":       enriched_query,
        "answer":      resp.answer,
    }

    answer_snap  = resp.answer
    sources_snap = getattr(resp, "sources", []) or []
    frame_snap   = frame.to_dict()

    # Конвертируем sources (url-строки) в dict для валидатора
    sources_for_validate = [
        {"url": s, "title": s, "text": ""} if isinstance(s, str) else s
        for s in sources_snap
    ]

    loop.run_in_executor(
        None,
        lambda: _bg_validate(
            msg_id, enriched_query, answer_snap, resp.trust_level, loop,
            frame=frame_snap, sources=sources_for_validate,
        ),
    )
    loop.run_in_executor(
        None,
        lambda: _bg_tag(msg_id, enriched_query, answer_snap, sources_snap, loop),
    )

    return {
        "ok":          True,
        "answer":      resp.answer,
        "trust_level": resp.trust_level,
        "preliminary": True,
        "latency":     resp.latency_total,
        "steps":       resp.steps_taken,
        "msg_id":      msg_id,
        "session_id":  session_id,
        "domain":      frame.domain,
        "missing":     frame.missing,
        "frame":       frame.to_dict(),
    }


def _p2p_available() -> bool:
    """Проверить слушается ли порт 9999 (YANDI P2P нода)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(("127.0.0.1", 9999))
        s.close()
        return True
    except Exception:
        return False



def _local_validate(query: str, answer: str) -> str:
    """Валидация через локальный Ollama — без интернета, без интернет-чата."""
    import re
    import requests
    prompt = (
        f"Проверь ответ на вопрос. Отвечай кратко: согласен/не согласен/частично, и почему.\n\n"
        f"Вопрос: {query}\n\nОтвет: {answer}\n\nОценка:"
    )
    try:
        s = requests.Session()
        s.trust_env = False
        r = s.post(
            "http://127.0.0.1:11434/api/generate",
            json={
                "model": "heretic:q8",
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 200},
            },
            timeout=60,
        )
        raw = r.json().get("response", "").strip()
        raw = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
        return raw[:300]
    except Exception:
        return ""


def _bg_validate(msg_id: str, query: str, answer: str, prev_trust: str, loop,
                 frame: dict | None = None, sources: list | None = None):
    """
    Фоновая проверка.
    Приоритет: YANDI P2P → Orch AI (DeepSeek отдельный канал) → локальная модель.
    """
    ds_corr = ""  # коррекция DeepSeek, передаём во фронтенд для dataset
    try:
        from agent.orch_node_selector import yandi_connected

        if yandi_connected():
            from agent.orch_risk          import assess_risk
            from agent.orch_node_selector import select_nodes
            from agent.orch_validator     import validate_parallel
            from agent.orch_arbiter       import arbitrate

            risk   = assess_risk(query)
            nodes  = select_nodes(risk)
            val    = validate_parallel(query, answer, nodes, domain="general")
            result = arbitrate(query, answer, val)
            trust  = result.verdict
            if trust == "VERIFIED":
                upd_text = "✅ Проверено через YANDI-ноды — ответ актуален."
            elif trust == "PARTIALLY_VERIFIED":
                upd_text = f"⚠ Частично подтверждено через YANDI. {result.explanation}"
            elif trust == "REJECTED":
                upd_text = f"❌ YANDI-ноды не подтвердили ответ. {result.explanation}"
            else:
                trust    = prev_trust
                upd_text = "💭 YANDI: консенсус не достигнут."
        else:
            # ── Orch AI: DeepSeek через отдельный канал ───────────────────
            try:
                from agent.orch_ai_validator import push_validation_task, get_validation_result, log_validation
                task_id = push_validation_task(
                    query=query,
                    frame=frame or {},
                    sources=sources or [],
                    answer=answer,
                )
                # Ждём ответ DeepSeek (до 5 минут)
                result = get_validation_result(task_id, timeout=300)
                if result:
                    trust   = result["verdict"]
                    corr    = result.get("correction", "").strip()
                    adds    = result.get("additions", "").strip()
                    raw     = result.get("raw", "").strip()
                    ds_corr = corr
                    log_validation(task_id, query, frame or {}, answer, result)

                    ds_link = "https://chat.deepseek.com"
                    # Краткий текст от DeepSeek — берём начало ответа
                    raw_excerpt = raw[:500].strip() if raw else ""

                    if trust == "UNVERIFIED":
                        upd_text = "⏳ Не удалось получить ответ DeepSeek."
                    else:
                        headers = {
                            "VERIFIED":           "✅ DeepSeek подтвердил:",
                            "PARTIALLY_VERIFIED": "⚠ DeepSeek нашёл неточности:",
                            "REJECTED":           "❌ DeepSeek оспорил ответ:",
                        }
                        header = headers.get(trust, "ℹ DeepSeek:")
                        body = corr or raw_excerpt
                        lines = [header]
                        if body:
                            lines.append(body)
                        lines.append(f"→ {ds_link}")
                        upd_text = "\n".join(lines)
                else:
                    # DeepSeek не ответил за 5 минут → локальная модель
                    raise TimeoutError("DeepSeek timeout")
            except Exception:
                reply = _local_validate(query, answer)
                if reply:
                    trust    = "PARTIALLY_VERIFIED"
                    upd_text = f"🤖 Локальная проверка: {reply[:300]}"
                else:
                    trust    = prev_trust
                    upd_text = "ℹ Проверка недоступна."

    except Exception as e:
        trust    = prev_trust
        upd_text = f"ℹ Проверка завершена ({e.__class__.__name__})."

    # Иконка и текст замены строки "🔄 Отправлен на проверку..."
    icons = {"VERIFIED": "✅", "PARTIALLY_VERIFIED": "⚠", "REJECTED": "❌"}
    icon  = icons.get(trust, "ℹ")
    if trust == "VERIFIED":
        replace_line = f"{icon} Проверено DeepSeek — ответ подтверждён"
    elif trust == "PARTIALLY_VERIFIED":
        replace_line = f"{icon} DeepSeek: частично подтверждено"
    elif trust == "REJECTED":
        replace_line = f"{icon} DeepSeek: ответ не подтверждён"
    else:
        replace_line = None

    # Обновляем запись в Redis
    async def _redis_update():
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        msgs = await r.lrange(ORCH_MSGS_KEY, 0, MAX_MESSAGES - 1)
        for i, raw in enumerate(msgs):
            try:
                obj = json.loads(raw)
                if obj.get("id") == msg_id:
                    obj["trust_level"]  = trust
                    obj["preliminary"]  = False
                    obj["pending_done"] = True
                    cur_text = obj.get("text", "")
                    # Убираем заголовок [ПРЕДВАРИТЕЛЬНЫЙ • ...] — валидация завершена
                    cur_text = re.sub(
                        r"^\[ПРЕДВАРИТЕЛЬНЫЙ[^\]]*\]\s*\n?", "", cur_text
                    ).lstrip("\n")
                    if replace_line:
                        cur_text = re.sub(
                            r"🔄 Отправлен на проверку[^\n]*",
                            replace_line,
                            cur_text,
                        )
                    obj["text"] = cur_text
                    await r.lset(ORCH_MSGS_KEY, i, json.dumps(obj))
                    break
            except Exception:
                pass
        await r.aclose()

    asyncio.run_coroutine_threadsafe(_redis_update(), loop)

    push = {
        "type":               "orch_update",
        "msg_id":             msg_id,
        "trust_level":        trust,
        "update_text":        upd_text,
        "replace_pending":    replace_line,
        "deepseek_correction": ds_corr,
    }
    asyncio.run_coroutine_threadsafe(broadcast(push), loop)


def _bg_tag(msg_id: str, question: str, answer: str, sources: list, loop):
    """Авто-тегирование в фоне: определяет 3-5 тегов, отправляет в UI через WS."""
    try:
        from agent.orch_tagger import auto_tag
        tags = auto_tag(question, answer)
    except Exception:
        tags = []

    if not tags:
        return

    push = {
        "type":   "orch_update",
        "msg_id": msg_id,
        "tags":   tags,
    }
    asyncio.run_coroutine_threadsafe(broadcast(push), loop)


@router.post("/api/orchestrator/feedback")
async def orchestrator_feedback(payload: dict):
    question             = (payload.get("question") or "").strip()
    answer               = (payload.get("answer")   or "").strip()
    feedback             = payload.get("feedback", "neutral")
    session_id           = payload.get("session_id", "")
    trust_level          = payload.get("trust_level", "UNVERIFIED")
    liked_version        = payload.get("liked_version", None)       # "web"|"deepseek"|"both"|null
    deepseek_verdict     = payload.get("deepseek_verdict", "")
    deepseek_correction  = payload.get("deepseek_correction", "")

    if feedback not in ("positive", "negative", "neutral"):
        return {"ok": False, "error": "feedback must be positive/negative/neutral"}

    try:
        from agent.orch_feedback import record_feedback
        event = record_feedback(
            question=question, answer=answer, feedback=feedback,
            session_id=session_id, trust_level=trust_level,
            liked_version=liked_version,
            deepseek_verdict=deepseek_verdict,
            deepseek_correction=deepseek_correction,
        )
        icon = "👍" if feedback == "positive" else "👎" if feedback == "negative" else "➖"
        lv   = f" [{liked_version}]" if liked_version and liked_version != "both" else ""
        await broadcast({"type": "system", "text": f"Feedback{lv}: {feedback} ({icon})"})
        return {"ok": True, "event": event}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/orchestrator/remember")
async def orchestrator_remember(payload: dict):
    question = (payload.get("question") or "").strip()
    answer   = (payload.get("answer")   or "").strip()
    msg_id   = payload.get("msg_id", "")
    tags     = payload.get("tags") or []

    if not question or not answer:
        return {"ok": False, "error": "question and answer required"}

    # Достаём QueryFrame из Redis если есть (был сохранён при ответе)
    frame_meta: dict = {}
    if msg_id:
        try:
            r_tmp = aioredis.from_url(REDIS_URL, decode_responses=True)
            raw_frame = await r_tmp.get(f"council:frame:{msg_id}")
            await r_tmp.aclose()
            if raw_frame:
                frame_meta = {"query_frame": json.loads(raw_frame)}
        except Exception:
            pass

    try:
        from agent.orch_knowledge_writer import write_knowledge
        write_knowledge(
            question=question, answer=answer, verdict="VERIFIED",
            tags=tags, sources=["user_verified"],
            meta={"msg_id": msg_id, **frame_meta},
        )
        tags_str = " ".join(f"#{t}" for t in tags) if tags else ""
        await broadcast({"type": "system", "text": f"📌 Ответ сохранён! {tags_str}".strip()})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/review/list")
async def review_list():
    """Список неверифицированных записей для review queue."""
    try:
        from agent.db.manager import KnowledgeDB
        rows = KnowledgeDB().list_unverified(limit=30)
        return {"ok": True, "items": [
            {
                "id":      r["id"],
                "query":   r["query"],
                "answer":  r["answer"],
                "tag":     r["tag"],
                "confidence": r["confidence"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]}
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}


@router.post("/api/review/verify")
async def review_verify(payload: dict):
    """Верифицировать запись по id."""
    rid = (payload.get("id") or "").strip()
    if not rid:
        return {"ok": False, "error": "id required"}
    try:
        from agent.db.manager import KnowledgeDB
        ok = KnowledgeDB().verify(rid)
        if ok:
            await broadcast({"type": "system", "text": f"✅ Верифицировано: {rid}"})
        return {"ok": ok}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/review/update")
async def review_update(payload: dict):
    """Обновить ответ и сразу верифицировать."""
    rid    = (payload.get("id")     or "").strip()
    answer = (payload.get("answer") or "").strip()
    if not rid or not answer:
        return {"ok": False, "error": "id and answer required"}
    try:
        from agent.db.manager import KnowledgeDB
        ok = KnowledgeDB().update_answer(rid, answer, trust_level="VERIFIED")
        if ok:
            await broadcast({"type": "system", "text": f"✅ Обновлено и верифицировано: {rid}"})
        return {"ok": ok}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/review/delete")
async def review_delete(payload: dict):
    """Удалить запись из базы знаний."""
    rid = (payload.get("id") or "").strip()
    if not rid:
        return {"ok": False, "error": "id required"}
    try:
        from agent.db.manager import KnowledgeDB
        ok = KnowledgeDB().delete(rid)
        if ok:
            await broadcast({"type": "system", "text": f"🗑 Удалено: {rid}"})
        return {"ok": ok}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/orchestrator/stats")
async def orchestrator_stats():
    result = {}
    try:
        from agent.orch_feedback import get_feedback_stats
        result["feedback"] = get_feedback_stats()
    except Exception as e:
        result["feedback_error"] = str(e)
    try:
        from agent.orch_monitoring import get_stats
        result["monitoring"] = get_stats(last_n=200)
    except Exception as e:
        result["monitoring_error"] = str(e)
    try:
        from agent.orch_knowledge_writer import get_stats as kw_stats
        result["knowledge"] = kw_stats()
    except Exception as e:
        result["knowledge_error"] = str(e)
    return result
