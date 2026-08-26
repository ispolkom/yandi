"""
chat_agent.py — Агент: инструменты, планировщик, браузер-подключения.
Endpoints: /api/agent/*, /api/tools/*, /api/browser/*
Логика ТОЛЬКО для вкладки Агент.
"""
import asyncio
import json

import redis.asyncio as aioredis
from fastapi import APIRouter

from pet.shared import (
    REDIS_URL, AGENT_LOG_KEY, AGENT_STATE_KEY, MAX_MESSAGES,
    _model_last_seen, MODELS_URLS,
)

router = APIRouter()


# ── Agent state ───────────────────────────────────────────────────────────────

@router.get("/api/agent/state")
async def agent_get_state():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    log_raw   = await r.lrange(AGENT_LOG_KEY, 0, MAX_MESSAGES - 1)
    state_raw = await r.get(AGENT_STATE_KEY)
    await r.aclose()
    state = json.loads(state_raw) if state_raw else {}
    return {"log": list(reversed(log_raw)),
            "task": state.get("task", ""), "context": state.get("context", "")}


@router.post("/api/agent/log")
async def agent_save_log(payload: dict):
    html = payload.get("html", "")
    if not html:
        return {"ok": False}
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.lpush(AGENT_LOG_KEY, html)
    await r.ltrim(AGENT_LOG_KEY, 0, MAX_MESSAGES - 1)
    await r.aclose()
    return {"ok": True}


@router.post("/api/agent/state")
async def agent_save_state(payload: dict):
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.set(AGENT_STATE_KEY, json.dumps({
        "task": payload.get("task", ""),
        "context": payload.get("context", ""),
    }))
    await r.aclose()
    return {"ok": True}


@router.post("/api/agent/clear")
async def agent_clear():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.delete(AGENT_LOG_KEY, AGENT_STATE_KEY)
    await r.aclose()
    return {"ok": True}


# ── Tools ─────────────────────────────────────────────────────────────────────

@router.post("/api/tools/run")
async def api_tool_run(payload: dict):
    tool_name = (payload.get("tool") or "").strip()
    args      = payload.get("args") or {}
    if not tool_name:
        return {"ok": False, "error": "tool is required"}
    try:
        from agent.tools import tool as _tool
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: _tool(tool_name, **args))
        return {"ok": True, "tool": tool_name, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/tools/list")
async def api_tool_list():
    try:
        from agent.tools import list_tools
        return {"ok": True, "tools": list_tools()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/tools/plan")
async def api_tool_plan(payload: dict):
    task    = (payload.get("task") or "").strip()
    context = payload.get("context", "")
    if not task:
        return {"ok": False, "error": "task is required"}
    try:
        from agent.tools import tool as _tool
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: _tool("ai.build_plan", task=task, context=context)
        )
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/tools/execute")
async def api_tool_execute(payload: dict):
    steps        = payload.get("steps") or []
    stop_on_fail = payload.get("stop_on_fail", True)
    if not steps:
        return {"ok": False, "error": "steps is required"}
    try:
        from agent.tools.executor import execute_plan
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: execute_plan(steps, stop_on_fail))
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/tools/run_task")
async def api_run_task(payload: dict):
    task    = (payload.get("task") or "").strip()
    context = payload.get("context", "")
    if not task:
        return {"ok": False, "error": "task is required"}
    try:
        from agent.tools.executor import run_task
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: run_task(task, context))
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Browser connections ───────────────────────────────────────────────────────

@router.get("/api/browser/status")
async def api_browser_status():
    import time
    now = time.time()
    return {
        m: {
            "connected":    bool(_model_last_seen.get(m) and (now - _model_last_seen[m]) < 90),
            "last_seen_sec": round(now - _model_last_seen[m]) if _model_last_seen.get(m) else None,
            "url":          MODELS_URLS.get(m, ""),
        }
        for m in ("claude", "gpt", "deepseek", "kimi")
    }


@router.post("/api/browser/connect")
async def api_browser_connect(payload: dict):
    import subprocess, time
    targets = payload.get("models") or list(MODELS_URLS)
    now     = time.time()
    result  = {}
    opened  = []
    for model in targets:
        if model not in MODELS_URLS:
            result[model] = {"status": "❌ неизвестная модель", "action": "none"}
            continue
        last = _model_last_seen.get(model, 0.0)
        if last and (now - last) < 90:
            result[model] = {"status": "🟢 подключён", "action": "none"}
        else:
            url = MODELS_URLS[model]
            try:
                subprocess.Popen(["xdg-open", url],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                opened.append(model)
                result[model] = {"status": "🟡 открываем...", "action": "opened", "url": url}
            except Exception as e:
                result[model] = {"status": "❌ ошибка", "action": "failed", "error": str(e)}
    return {"ok": True, "models": result, "opened": opened}


@router.post("/api/browser/open")
async def api_browser_open(payload: dict):
    import subprocess
    model = (payload.get("model") or "").strip()
    if model not in MODELS_URLS:
        return {"ok": False, "error": f"Неизвестная модель: {model}"}
    url = MODELS_URLS[model]
    try:
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "model": model, "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e)}
