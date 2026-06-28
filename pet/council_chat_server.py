#!/usr/bin/env python3
"""
council_chat_server.py — multi-model AI council chat
Usage: python pet/council_chat_server.py
Open:  http://localhost:9010

Архитектура (каждый чат — отдельный файл):
  chat_local.py     — YANDI Помощник (/api/local/*)
  chat_translate.py — переводчик (/api/council/translate)
  chat_orch.py      — Оркестратор (/api/orchestrator/*, /api/orch/*)
  chat_agent.py     — Агент (/api/agent/*, /api/tools/*, /api/browser/*)
  shared.py         — общие константы, broadcast, write_log
"""

import asyncio
import json
import random
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as url_quote

# Project root = yandi/ (one level up from pet/)
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Импорт общих констант из shared.py
from pet.shared import (
    REDIS_URL, PUBSUB_CH, ORCH_MSGS_KEY, INET_MSGS_KEY, MESSAGES_KEY,
    LOCAL_MSGS_KEY, AGENT_LOG_KEY, AGENT_STATE_KEY, MAX_MESSAGES, LOG_FILE,
    RELAY_CHAIN, MODEL_DISPLAY, MODELS_URLS, LANG_NAMES, LANG_FULL,
    browsers, _model_last_seen, _bridge_state, _tokens, TOKEN_LIMITS, TOKEN_WARN,
    write_log, broadcast,
)

STATUS_PFX = "council:chat:status:"
TURN_KEY   = "council:chat:turn"

_HERE        = Path(__file__).parent
CONFIG_FILE  = _HERE / "council_config.json"
REGISTRY_DIR = _HERE.parent / "registry" / "council"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/media", StaticFiles(directory=str(_HERE / "media")), name="media")

# ── Подключаем роутеры каждого чата ──────────────────────────────────────────
from pet.chat_local     import router as _local_router
from pet.chat_translate import router as _translate_router
from pet.chat_orch      import router as _orch_router
from pet.chat_agent     import router as _agent_router

app.include_router(_local_router)
app.include_router(_translate_router)
app.include_router(_orch_router)
app.include_router(_agent_router)

import time as _time

# Три отдельных очереди — по одной на каждый Firefox
_ext_queues: dict[str, asyncio.Queue] = {
    "claude":   asyncio.Queue(maxsize=16),
    "gpt":      asyncio.Queue(maxsize=16),
    "deepseek": asyncio.Queue(maxsize=16),
    "kimi":     asyncio.Queue(maxsize=16),
}
# Контекст relay-цепочки: task_id → {text, broadcast, claude_resp, gpt_resp}
_relay_ctx: dict[str, dict] = {}


# ── relay helper ─────────────────────────────────────────────────────────────

def _active_models() -> list[str]:
    """Вернуть список моделей в порядке RELAY_CHAIN: не заблокированы И вкладка открыта (heartbeat < 90s)."""
    now = _time.time()
    result = []
    for m in RELAY_CHAIN:
        if _bridge_state.get(f"{m}_blocked"):
            continue
        last = _model_last_seen.get(m, 0.0)
        if last and (now - last) < 90:
            result.append(m)
    return result


async def _queue_after(model: str, task_id: str, prompt: str, delay: int):
    """Поставить задачу в очередь модели после задержки (relay timer)."""
    await asyncio.sleep(delay)
    try:
        _ext_queues[model].put_nowait({"task_id": task_id, "text": prompt})
    except asyncio.QueueFull:
        pass


async def _inet_ready_after(seconds: int):
    """Разблокировать inet-чат после паузы (все модели ответили + буфер)."""
    await asyncio.sleep(seconds)
    await broadcast({"type": "inet_ready"})
    # После разблокировки — читаем финальные ответы из Redis и рассылаем сводку
    asyncio.create_task(_inet_collect_responses())


async def _inet_collect_responses():
    """Собрать финальные ответы всех моделей, синтезировать через локальную модель."""
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lrange(INET_MSGS_KEY, 0, 20)
    await r.aclose()

    # raw: lrange 0 20 → новые первые. Ищем последний вопрос человека,
    # берём только реальные ответы моделей новее него.
    responses = {}
    question = ""
    question_idx = None
    parsed = []
    for line in raw:
        try:
            parsed.append(json.loads(line))
        except Exception:
            parsed.append({})

    for i, m in enumerate(parsed):
        if m.get("from") == "human":
            question = m.get("text", "")
            question_idx = i
            break

    if question_idx is not None:
        for m in parsed[:question_idx]:   # новее вопроса
            frm = m.get("from", "")
            txt = m.get("text", "")
            # Фильтруем таймауты и пустые ответы
            if frm in ("claude", "gpt", "deepseek", "kimi") \
               and txt and "[нет ответа" not in txt and "[timeout" not in txt \
               and len(txt) > 20:
                responses[frm] = txt

    if not responses:
        return

    raw_summary = "\n\n".join(
        f"[{frm.upper()}]\n{text[:500]}" for frm, text in responses.items()
    )

    # Синтез через локальную модель
    synthesis = await asyncio.get_event_loop().run_in_executor(
        None, _synthesize_council, question, responses
    )

    # Сохраняем синтез в историю и сразу показываем в чате
    if synthesis and not synthesis.startswith("[синтез недоступен"):
        # Чистим артефакты модели
        import re as _re
        synthesis = _re.sub(r"<\|.*?\|>", "", synthesis).strip()

        r2 = aioredis.from_url(REDIS_URL, decode_responses=True)
        synth_msg = {
            "type": "message", "from": "council", "tab": "inet",
            "text": f"🧠 Синтез совета:\n\n{synthesis}",
            "ts": datetime.now().strftime("%H:%M"),
            "_ts": datetime.now().timestamp(),
            "id": str(uuid.uuid4()),
        }
        await r2.lpush(INET_MSGS_KEY, json.dumps(synth_msg))
        await r2.ltrim(INET_MSGS_KEY, 0, MAX_MESSAGES - 1)
        await r2.aclose()
        # Отправляем сообщение в чат как обычное message — фронт его отобразит
        await broadcast(synth_msg)

        # Пишем в локальный FAISS реестр (QueryFrame будет видеть этот контекст)
        asyncio.get_event_loop().run_in_executor(
            None, _write_to_registry, question, synthesis, list(responses.keys())
        )
        # Передаём знание в YANDI ноду (AI-mesh)
        asyncio.create_task(_push_to_node(question, synthesis, list(responses.keys())))

    await broadcast({
        "type":      "inet_parse_ready",
        "count":     len(responses),
        "models":    list(responses.keys()),
        "summary":   raw_summary,
        "synthesis": synthesis,
    })


def _synthesize_council(question: str, responses: dict) -> str:
    """Локальная модель читает все ответы совета и даёт итоговый вывод."""
    import requests as _req, re
    answers_block = "\n\n".join(
        f"[{frm.upper()}]: {text[:600]}" for frm, text in responses.items()
    )
    prompt = f"""Ты синтезатор мнений AI-совета. Тебе дан вопрос и ответы нескольких AI-моделей.

Вопрос: {question}

Ответы моделей:
{answers_block}

Задача:
1. Найди в чём модели СОГЛАСНЫ — это скорее всего правда
2. Найди ПРОТИВОРЕЧИЯ — отметь их
3. Дай ИТОГОВЫЙ ВЫВОД — краткий, точный, на русском

Верни только итоговый вывод без преамбул."""

    try:
        s = _req.Session()
        s.trust_env = False
        r = s.post(
            f"{_OLLAMA_URL}/api/generate",
            json={"model": _OLLAMA_MOD, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.2, "num_predict": 600}},
            timeout=90,
        )
        text = r.json().get("response", "").strip()
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
        return text
    except Exception as e:
        return f"[синтез недоступен: {e}]"


def _write_to_registry(question: str, synthesis: str, models: list):
    """Записать синтез совета в локальный FAISS реестр знаний."""
    try:
        import sys as _sys
        _sys.path.insert(0, "/media/iam/DATASET/yandi")
        from agent.orch_registry_search import store_synthesis
        store_synthesis(question, synthesis, models=models)
    except Exception as e:
        print(f"  [registry] ошибка записи: {e}")


_NODE_KB_URL = "http://127.0.0.1:18082/api/ai-rpc/knowledge/store"

async def _push_to_node(question: str, synthesis: str, models: list[str]):
    """Отправить синтез совета в YANDI ноду для сохранения в AI-mesh."""
    try:
        import aiohttp as _aiohttp
        payload = {"question": question, "synthesis": synthesis, "models": models}
        async with _aiohttp.ClientSession(trust_env=False) as session:
            async with session.post(_NODE_KB_URL, json=payload, timeout=_aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    print(f"  [node] знание сохранено в AI-mesh: id={data.get('id','?')[:8]}")
    except Exception:
        pass  # Нода не запущена — не страшно, работаем без неё


# ── HTML (embedded) ───────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YANDI Council</title>
<style>
/* ── ICQ Green (YANDI unified style) ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --icq-bg:#e8f0e8;--icq-panel:#d4e0d4;--icq-header:#2a6b2a;
  --icq-header-light:#3a7b3a;--icq-active:#c0e0c0;
  --icq-border:#a0c0a0;--icq-border-dark:#608060;--icq-border-light:#f0fff0;
  --icq-button:#e0e0e0;--icq-text:#1a3a1a;--icq-text-muted:#608060;
  --icq-online:#00aa00;--icq-offline:#a0a0a0;
  --font:'Tahoma','Segoe UI','MS Sans Serif',sans-serif;
}
body{font-family:var(--font);background:var(--icq-bg);color:var(--icq-text);
  font-size:18px;height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* HEADER */
.header{background:linear-gradient(180deg,var(--icq-header-light) 0%,var(--icq-header) 100%);
  border-bottom:2px solid var(--icq-border-dark);flex-shrink:0}
.header-content{display:flex;align-items:center;padding:5px 10px;gap:6px}
.logo{font-size:20px;font-weight:bold;color:white;text-shadow:1px 1px 0 #1a4a1a;
  white-space:nowrap;margin-right:6px}
.nav{display:flex;gap:2px}
.nav-btn{padding:4px 14px;background:var(--icq-header-light);
  border:1px solid var(--icq-border-light);border-right-color:var(--icq-border-dark);
  border-bottom-color:var(--icq-border-dark);color:white;cursor:pointer;
  font-family:var(--font);font-size:15px}
.nav-btn:hover{background:#4a8b4a}
.nav-btn.active{background:var(--icq-bg);color:var(--icq-header);font-weight:bold;
  border-color:var(--icq-border-dark);border-right-color:var(--icq-border-light);
  border-bottom-color:var(--icq-border-light)}
.hdr-btn{padding:3px 9px;background:var(--icq-button);border:1px solid var(--icq-border-light);
  border-right-color:var(--icq-border-dark);border-bottom-color:var(--icq-border-dark);
  cursor:pointer;font-family:var(--font);font-size:14px}
.hdr-btn:hover{background:#f0f0f0}
.ml-auto{margin-left:auto}

/* 3-COLUMN */
.three-col{display:flex;flex:1;overflow:hidden}

/* LEFT PANEL */
.left-panel{width:210px;flex-shrink:0;background:var(--icq-panel);
  border-right:2px solid var(--icq-border-dark);overflow-y:auto;
  display:flex;flex-direction:column}

/* RIGHT PANEL */
.right-panel{width:230px;flex-shrink:0;background:var(--icq-panel);
  border-left:2px solid var(--icq-border-dark);overflow-y:auto;
  display:flex;flex-direction:column}

/* CENTER */
.center-panel{flex:1;display:flex;flex-direction:column;overflow:hidden;background:white}

/* CARDS */
.card-hdr{background:var(--icq-header);padding:5px 9px;display:flex;
  justify-content:space-between;align-items:center;border-bottom:1px solid var(--icq-border-light)}
.card-title{font-size:14px;font-weight:bold;color:white}
.card-body{padding:7px 9px}
.card-sep{border-bottom:1px solid var(--icq-border)}

/* STATUS ROWS */
.st-row{display:flex;justify-content:space-between;align-items:center;
  padding:3px 0;border-bottom:1px solid var(--icq-border);font-size:14px}
.st-row:last-child{border-bottom:none}
.dot-on{color:#00aa00;font-size:13px}.dot-off{color:#a0a0a0;font-size:13px}
.dot-typ{color:#cc8800;font-size:13px}
.stat-val{font-family:monospace;font-weight:bold;font-size:14px}

/* MESSAGES */
.msgs-panel{flex:1;overflow-y:auto;padding:9px 11px;
  display:flex;flex-direction:column;gap:7px;background:white}
.msg{display:flex;flex-direction:column;gap:2px;max-width:82%}
.msg.human{align-self:flex-end}
.msg.claude,.msg.gpt,.msg.deepseek,.msg.orchestrator,.msg.local-ai{align-self:flex-start}
.msg.local-ai .msg-name{color:#7b2fff}
.msg.local-ai .bubble{background:#f3eeff;border-color:#c8a8ff;border-right-color:#9060cc;border-bottom-color:#9060cc}
.msg.system{align-self:center;max-width:100%;opacity:.7}
.msg-meta{font-size:13px;color:var(--icq-text-muted);display:flex;gap:5px;align-items:center}
.msg.human .msg-meta{justify-content:flex-end}
.msg-name{font-weight:bold}
.msg.human       .msg-name{color:#2a6b2a}
.msg.claude      .msg-name{color:#0055aa}
.msg.gpt         .msg-name{color:#7700aa}
.msg.deepseek    .msg-name{color:#aa5500}
.msg.orchestrator .msg-name{color:#2a6b2a}
.bubble{padding:7px 10px;line-height:1.5;white-space:pre-wrap;word-break:break-word;
  font-size:18px;border:1px solid var(--icq-border);
  border-right:2px solid var(--icq-border-dark);border-bottom:2px solid var(--icq-border-dark);
  background:var(--icq-panel)}
.msg.human       .bubble{background:#e0f0ff;border-color:#a0c0e0;border-right-color:#6080a0;border-bottom-color:#6080a0}
.msg.claude      .bubble{background:#e8f0ff;border-color:#a0b0d0;border-right-color:#6070a0;border-bottom-color:#6070a0}
.msg.gpt         .bubble{background:#f0e8ff;border-color:#c0a0d0;border-right-color:#8060a0;border-bottom-color:#8060a0}
.msg.deepseek    .bubble{background:#fff0e0;border-color:#d0b080;border-right-color:#a07040;border-bottom-color:#a07040}
.msg.orchestrator .bubble{background:var(--icq-active)}
.msg.system      .bubble{background:transparent;border:none;font-size:14px;
  color:var(--icq-text-muted);text-align:center}

/* TRUST BADGE */
.trust-badge{font-size:12px;padding:1px 5px;border:1px solid var(--icq-border);
  margin-left:4px;vertical-align:middle}
.trust-VERIFIED{background:#c0f0c0;color:#006600;border-color:#00aa00}
.trust-PARTIALLY_VERIFIED{background:#fff0c0;color:#886600;border-color:#cc8800}
.trust-UNVERIFIED{background:var(--icq-panel);color:var(--icq-text-muted)}
.trust-REJECTED{background:#f0c0c0;color:#880000;border-color:#aa0000}
.trust-HYPOTHESIS{background:#c0d0ff;color:#002288;border-color:#4466cc}

/* FEEDBACK */
.fb-row{display:flex;gap:4px;margin-top:4px;align-items:center;flex-wrap:wrap}
.fb-btn{background:var(--icq-button);border:1px solid var(--icq-border-light);
  border-right-color:var(--icq-border-dark);border-bottom-color:var(--icq-border-dark);
  padding:2px 7px;cursor:pointer;font-size:14px;font-family:var(--font)}
.fb-btn:hover{background:#f0f0f0}
.fb-btn.active-pos{background:#c0f0c0;color:#006600}
.fb-btn.active-neg{background:#f0c0c0;color:#880000}
.fb-btn.active-rem{background:#c0d0ff;color:#002288}
.orch-update{margin-top:4px;font-size:14px;color:var(--icq-text-muted);
  border-top:1px solid var(--icq-border);padding-top:3px}
.orch-tags{margin-top:5px;display:flex;flex-wrap:wrap;gap:4px}
.orch-tag{font-size:11px;padding:1px 6px;border-radius:10px;
  background:var(--icq-active);border:1px solid var(--icq-border);
  color:var(--icq-text-muted);cursor:default}
.tr-btn{background:none;border:none;color:var(--icq-text-muted);font-size:12px;
  cursor:pointer;padding:2px 0;text-decoration:underline dotted;margin-top:3px;
  font-family:var(--font);display:inline-block}
.tr-btn:hover{color:var(--icq-header)}
.tr-btn:disabled{opacity:.5;cursor:wait}
.msg-actions{display:flex;gap:8px;margin-top:3px;flex-wrap:wrap;align-items:center}
.act-btn{background:none;border:none;color:var(--icq-text-muted);font-size:12px;
  cursor:pointer;padding:2px 0;text-decoration:underline dotted;font-family:var(--font)}
.act-btn:hover{color:var(--icq-header)}
.act-btn.del:hover{color:#cc0000}
.rv-list-item:hover{background:var(--icq-bg)}
.rv-list-item.rv-active{background:var(--icq-header-light);color:white;font-weight:bold}
.rv-list-item.rv-active div{color:rgba(255,255,255,.8)!important}
.tr-block{margin-top:5px;padding:6px 9px;background:#f0f8f0;
  border-left:3px solid var(--icq-header);font-size:14px;line-height:1.5;
  white-space:pre-wrap;word-break:break-word;color:var(--icq-text)}
.tr-flag{font-size:11px;color:var(--icq-text-muted);margin-bottom:3px}

/* RELAY BAR */
#relay-bar{background:#fffbe0;border-top:1px solid #cc8800;padding:3px 10px;
  font-size:14px;color:#886600;display:none;align-items:center;gap:8px;flex-shrink:0}
#relay-bar.show{display:flex}
#relay-progress{flex:1;height:3px;background:var(--icq-panel);border:1px solid var(--icq-border)}
#relay-fill{height:100%;background:#cc8800;transition:width 1s linear}

/* INPUT */
#input-area{background:var(--icq-panel);border-top:2px solid var(--icq-border-dark);
  padding:7px 9px;display:flex;gap:5px;align-items:flex-end;flex-shrink:0}
#inp{flex:1;background:white;border:1px solid var(--icq-border);
  border-right:2px solid var(--icq-border-dark);border-bottom:2px solid var(--icq-border-dark);
  padding:6px 9px;font-family:var(--font);font-size:18px;resize:none;height:42px;outline:none}
#inp:focus{border-color:var(--icq-header)}
.send-btn{background:var(--icq-header);color:white;border:1px solid var(--icq-border-light);
  border-right:2px solid var(--icq-border-dark);border-bottom:2px solid var(--icq-border-dark);
  padding:0 13px;height:42px;cursor:pointer;font-family:var(--font);font-size:15px;
  font-weight:bold;white-space:nowrap}
.send-btn:hover{background:var(--icq-header-light)}
.send-btn:disabled{background:var(--icq-offline);cursor:not-allowed}
.web-label{display:flex;align-items:center;gap:4px;font-size:14px;
  color:var(--icq-text-muted);white-space:nowrap;padding-bottom:5px;cursor:pointer}
.web-label input{accent-color:var(--icq-header)}

/* STATUS BAR */
#status-bar{background:var(--icq-panel);border-top:1px solid var(--icq-border);
  padding:2px 9px;font-size:13px;color:var(--icq-text-muted);flex-shrink:0;
  display:flex;gap:12px}

/* TOOLS */
.tool-sec{padding:7px 9px;border-bottom:1px solid var(--icq-border)}
.tool-lbl{font-size:13px;color:var(--icq-text-muted);margin-bottom:4px;text-transform:uppercase}
.tool-btns{display:flex;flex-wrap:wrap;gap:3px}
.tool-btn{padding:3px 8px;background:var(--icq-button);border:1px solid var(--icq-border-light);
  border-right-color:var(--icq-border-dark);border-bottom-color:var(--icq-border-dark);
  cursor:pointer;font-family:var(--font);font-size:14px}
.tool-btn:hover{background:#f0f0f0}
.tool-btn.blocked{background:#f0c0c0;color:#880000;text-decoration:line-through}
.tool-btn.paused{background:#fff0c0;color:#886600}
.tool-btn.primary{background:var(--icq-header);color:white}
.tool-btn.primary:hover{background:var(--icq-header-light)}
.tool-btn.w100{width:100%;margin-top:5px}
.tool-hint{font-size:11px;color:var(--icq-text-muted);line-height:1.4;
  margin-bottom:5px;padding:3px 0;border-bottom:1px dashed var(--icq-border);
  font-style:italic}

/* CONNECTION */
.conn-note{font-size:11px;color:var(--icq-text-muted);margin-bottom:6px;line-height:1.4;
  font-style:italic;padding:3px 0;border-bottom:1px solid var(--icq-border)}
.conn-row{display:flex;align-items:center;gap:6px;padding:4px 0;
  border-bottom:1px solid var(--icq-border)}
.conn-row:last-of-type{border-bottom:none}
.conn-dot{font-size:14px;flex-shrink:0;line-height:1}
.conn-name{flex:1;font-size:13px}
.conn-open{background:var(--icq-button);border:1px solid var(--icq-border-light);
  border-right-color:var(--icq-border-dark);border-bottom-color:var(--icq-border-dark);
  padding:2px 7px;cursor:pointer;font-size:13px;font-family:var(--font);flex-shrink:0}
.conn-open:hover{background:#f0f0f0}
.conn-open:disabled{opacity:.4;cursor:default}

/* TOGGLE */
.tog-wrap{display:flex;align-items:center;gap:8px;padding:5px 0}
.tog-lbl{font-size:14px;flex:1}
.tog-sw{position:relative;width:34px;height:16px;cursor:pointer;display:inline-block}
.tog-sw input{opacity:0;width:0;height:0}
.tog-track{position:absolute;top:0;left:0;right:0;bottom:0;
  background:var(--icq-offline);border:1px solid var(--icq-border-dark);transition:.2s}
.tog-sw input:checked + .tog-track{background:var(--icq-online)}
.tog-knob{position:absolute;top:2px;left:2px;width:10px;height:10px;
  background:white;border:1px solid var(--icq-border-dark);transition:.2s}
.tog-sw input:checked ~ .tog-knob{left:20px}

/* SAVE QUEUE */
#save-list{max-height:180px;overflow-y:auto;display:flex;flex-direction:column;gap:3px}
.save-item{display:flex;gap:5px;align-items:flex-start;font-size:13px;
  padding:2px 0;border-bottom:1px solid var(--icq-border)}
.save-item input{margin-top:2px;accent-color:var(--icq-header);cursor:pointer}
.save-item-body{flex:1;line-height:1.3}
.save-from{font-weight:bold;color:var(--icq-text-muted);font-size:12px}

/* MODAL */
.modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;
  background:rgba(0,0,0,.5);z-index:1000;align-items:center;justify-content:center}
.modal.open{display:flex}
.modal-box{background:var(--icq-panel);border:2px solid var(--icq-border-light);
  border-right-color:var(--icq-border-dark);border-bottom-color:var(--icq-border-dark);
  min-width:370px;max-width:90%}
.modal-hdr{background:linear-gradient(135deg,var(--icq-header-light),var(--icq-header));
  padding:7px 11px;display:flex;justify-content:space-between;align-items:center;
  border-bottom:1px solid var(--icq-border-light)}
.modal-hdr h3{color:white;font-size:18px;text-shadow:1px 1px 0 #1a4a1a}
.modal-x{background:var(--icq-button);border:1px solid var(--icq-border-light);
  border-right-color:var(--icq-border-dark);border-bottom-color:var(--icq-border-dark);
  width:19px;height:19px;cursor:pointer;font-size:14px;font-weight:bold;
  display:flex;align-items:center;justify-content:center}
.modal-body{padding:14px}
.f-row{margin-bottom:9px}
.f-row label{display:block;font-size:14px;margin-bottom:2px;font-weight:bold}
.f-row input{width:100%;background:white;border:1px solid var(--icq-border);
  border-right-color:var(--icq-border-dark);border-bottom-color:var(--icq-border-dark);
  padding:4px 7px;font-family:var(--font);font-size:15px;outline:none}
.f-row input:focus{border-color:var(--icq-header)}
.modal-acts{display:flex;gap:5px;justify-content:flex-end;margin-top:12px}

/* SCROLLBAR WIN98 */
::-webkit-scrollbar{width:12px}
::-webkit-scrollbar-track{background:var(--icq-panel);border:1px solid var(--icq-border)}
::-webkit-scrollbar-thumb{background:#c0c0c0;border:1px solid var(--icq-border-light);
  border-right-color:var(--icq-border-dark);border-bottom-color:var(--icq-border-dark)}
::-webkit-scrollbar-button{background:#c0c0c0;border:1px solid var(--icq-border-light);
  border-right-color:var(--icq-border-dark);border-bottom-color:var(--icq-border-dark);height:12px}

a.cl{color:#2244aa;text-decoration:underline}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div class="header-content">
    <img src="/media/logo.png" style="height:28px;width:28px;object-fit:contain;margin-right:4px" alt="Y">
    <span class="logo">YANDI Council</span>
    <div class="nav">
      <button class="nav-btn" id="tab-orch"  onclick="switchMode('orch')">🤖 Оркестратор</button>
      <button class="nav-btn"        id="tab-inet"  onclick="switchMode('inet')">🌐 Интернет чат</button>
      <button class="nav-btn"        id="tab-local" onclick="switchMode('local')">🟣 YANDI Помощник</button>
      <button class="nav-btn"        id="tab-agent" onclick="switchMode('agent')">🛠 Агент</button>
    </div>
    <div class="ml-auto" style="display:flex;gap:3px">
      <button class="hdr-btn" id="btn-sound" onclick="toggleSound()">🔊</button>
      <button class="hdr-btn" onclick="openSettings()">⚙</button>
      <button class="hdr-btn" onclick="clearChat()" style="color:#aa0000">🗑</button>
    </div>
  </div>
</div>

<!-- 3-COLUMN -->
<div class="three-col">

  <!-- LEFT: Статус -->
  <div class="left-panel">
    <div class="card-sep">
      <div class="card-hdr"><span class="card-title" data-i18n="sys">📡 Система</span></div>
      <div class="card-body">
        <div class="st-row"><span>Оркестратор</span><span id="st-orch" class="dot-off">● –</span></div>
        <div class="st-row"><span>Redis</span>      <span id="st-redis" class="dot-off">● –</span></div>
        <div class="st-row"><span>Ollama</span>     <span id="st-ollama" class="dot-off">● –</span></div>
      </div>
    </div>
    <div class="card-sep">
      <div class="card-hdr"><span class="card-title" data-i18n="models">🌐 Модели</span></div>
      <div class="card-body">
        <div class="st-row"><span style="color:#cc6600;font-weight:bold">Claude</span>  <span id="st-claude"   class="dot-off">● –</span></div>
        <div class="st-row"><span style="color:#111111;font-weight:bold">GPT</span>     <span id="st-gpt"      class="dot-off">● –</span></div>
        <div class="st-row"><span style="color:#0055cc;font-weight:bold">DeepSeek</span><span id="st-deepseek" class="dot-off">● –</span></div>
        <div class="st-row"><span style="color:#111111;font-weight:bold">Kimi</span>    <span id="st-kimi"     class="dot-off">● –</span></div>
      </div>
    </div>
    <div class="card-sep">
      <div class="card-hdr"><span class="card-title" data-i18n="lang">🗣 Язык</span></div>
      <div class="card-body">
        <div style="font-size:11px;color:var(--icq-text-muted);margin-bottom:5px" data-i18n="langHint">Кнопка «Перевести» под сообщениями переведёт текст на выбранный язык</div>
        <select id="lang-sel" onchange="setUserLang(this.value)"
          style="width:100%;padding:4px 6px;font-family:var(--font);font-size:13px;
          background:white;border:1px solid var(--icq-border);
          border-right-color:var(--icq-border-dark);border-bottom-color:var(--icq-border-dark);
          color:var(--icq-text);outline:none">
          <option value="ru">🇷🇺 Русский</option>
          <option value="en">🇬🇧 English</option>
          <option value="zh">🇨🇳 中文</option>
          <option value="de">🇩🇪 Deutsch</option>
          <option value="fr">🇫🇷 Français</option>
          <option value="es">🇪🇸 Español</option>
          <option value="ro">🇷🇴 Română</option>
          <option value="uk">🇺🇦 Українська</option>
          <option value="ja">🇯🇵 日本語</option>
          <option value="ko">🇰🇷 한국어</option>
          <option value="ar">🇸🇦 العربية</option>
          <option value="pl">🇵🇱 Polski</option>
          <option value="tr">🇹🇷 Türkçe</option>
        </select>
      </div>
    </div>
    <div class="card-sep">
      <div class="card-hdr">
        <span class="card-title">📊 Датасет</span>
        <button class="hdr-btn" onclick="refreshStats()" style="font-size:12px;padding:1px 4px">⟳</button>
      </div>
      <div class="card-body">
        <div class="st-row"><span>Трейсов</span> <span id="ds-traces"  class="stat-val">–</span></div>
        <div class="st-row"><span>Verified</span><span id="ds-verified" class="stat-val">–</span></div>
      </div>
    </div>
    <div class="card-sep" id="review-card">
      <div class="card-hdr" onclick="toggleReview()" style="cursor:pointer;user-select:none">
        <span class="card-title">📋 На проверку <span id="review-count" style="color:var(--icq-text-muted)"></span></span>
        <button class="hdr-btn" onclick="event.stopPropagation();loadReviewQueue()" style="font-size:12px;padding:1px 4px" title="Обновить">⟳</button>
      </div>
      <div style="padding:4px 10px 5px;border-bottom:1px solid var(--icq-border)">
        <label style="font-size:11px;display:flex;align-items:center;gap:5px;cursor:pointer;user-select:none">
          <input type="checkbox" id="rv-auto" style="cursor:pointer">
          <span>Авторежим</span>
        </label>
      </div>
      <div id="review-list" style="display:none;max-height:200px;overflow-y:auto;padding:2px 0"></div>
    </div>
    <div class="card-sep">
      <div class="card-hdr"><span class="card-title">🔢 Токены</span></div>
      <div class="card-body">
        <div class="st-row"><span>Claude</span>   <span id="tk-claude"   class="stat-val">0</span></div>
        <div class="st-row"><span>GPT</span>      <span id="tk-gpt"      class="stat-val">0</span></div>
        <div class="st-row"><span>DeepSeek</span><span id="tk-deepseek" class="stat-val">0</span></div>
      </div>
    </div>
  </div>

  <!-- CENTER: Чат -->
  <div class="center-panel">
    <div id="msgs-orch"  class="msgs-panel"></div>
    <div id="msgs-inet"  class="msgs-panel" style="display:none"></div>
    <div id="msgs-local" class="msgs-panel" style="display:none"></div>
    <div id="msgs-agent" class="msgs-panel" style="display:none;font-family:monospace;font-size:12px"></div>
    <div id="copy-chat-bar" style="display:none;padding:4px 10px;text-align:right">
      <button class="act-btn" onclick="copyFullChat()" style="font-size:13px">📋 Копировать весь чат</button>
    </div>
    <div id="relay-bar">
      <span>⏳ Relay через <span id="relay-count">0</span> сек</span>
      <div id="relay-progress"><div id="relay-fill" style="width:100%"></div></div>
    </div>
    <div id="input-area">
      <textarea id="inp" placeholder="Вопрос Оркестратору... (Enter — отправить)"></textarea>
      <label class="web-label" id="web-wrap">
        <input type="checkbox" id="web-chk" checked> 🌐 Веб
      </label>
      <button class="send-btn" id="send-btn" onclick="sendMessage()">Отправить</button>
    </div>
    <div id="status-bar">
      <span id="sb-mode">Режим: Оркестратор</span>
      <span id="sb-turn">Ваш ход ✍️</span>
    </div>
  </div>

  <!-- RIGHT: Инструменты -->
  <div class="right-panel">

    <!-- Оркестратор tools -->
    <div id="tools-orch">
      <div class="card-hdr"><span class="card-title">🔮 Оркестратор</span></div>
      <div class="tool-sec">
        <div class="tool-lbl">Доверие</div>
        <div style="font-size:13px;line-height:1.9">
          <span class="trust-badge trust-VERIFIED">✅</span> Verified — проверено<br>
          <span class="trust-badge trust-HYPOTHESIS">💭</span> Гипотеза — вероятно<br>
          <span class="trust-badge trust-UNVERIFIED">⏳</span> Pending — ожидание<br>
          <span class="trust-badge trust-REJECTED">❌</span> Rejected — не принято
        </div>
      </div>
      <div class="tool-sec">
        <div class="tool-lbl">Действия</div>
        <div class="tool-btns">
          <button class="tool-btn" onclick="saveDataset()">💾 Сессия</button>
          <button class="tool-btn" onclick="exportChat()">📁 .md</button>
          <button class="tool-btn" onclick="copyContext()">📋 Контекст</button>
          <button class="tool-btn" onclick="resetTokens()">⟳ Токены</button>
        </div>
      </div>
    </div>

    <!-- Интернет чат tools -->
    <div id="tools-inet" style="display:none">
      <div class="card-hdr"><span class="card-title">🌐 Интернет чат</span></div>

      <!-- Подключение -->
      <div class="tool-sec">
        <div class="tool-lbl">Подключение</div>
        <div class="conn-note" data-i18n="connectNote">Требуются открытые вкладки с&nbsp;авторизованными чатами ИИ</div>
        <div class="conn-row" id="conn-claude">
          <span class="conn-dot" id="cdot-claude">⚫</span>
          <span class="conn-name" style="color:#cc6600;font-weight:bold">Claude</span>
          <button class="conn-open" onclick="openChat('claude')" title="Открыть чат">↗</button>
        </div>
        <div class="conn-row" id="conn-gpt">
          <span class="conn-dot" id="cdot-gpt">⚫</span>
          <span class="conn-name" style="color:#111;font-weight:bold">GPT</span>
          <button class="conn-open" onclick="openChat('gpt')" title="Открыть чат">↗</button>
        </div>
        <div class="conn-row" id="conn-deepseek">
          <span class="conn-dot" id="cdot-deepseek">⚫</span>
          <span class="conn-name" style="color:#0055cc;font-weight:bold">DeepSeek</span>
          <button class="conn-open" onclick="openChat('deepseek')" title="Открыть чат">↗</button>
        </div>
        <div class="conn-row" id="conn-kimi">
          <span class="conn-dot" id="cdot-kimi">⚫</span>
          <span class="conn-name" style="color:#111;font-weight:bold">Kimi</span>
          <button class="conn-open" onclick="openChat('kimi')" title="Открыть чат">↗</button>
        </div>
        <button class="tool-btn w100" style="margin-top:5px" onclick="checkConnections()" data-i18n="checkConn">🔍 Проверить связь</button>
      </div>

      <div class="tool-sec">
        <div class="tool-lbl">Отправка</div>
        <div class="tool-btns">
          <button class="tool-btn primary" onclick="doBroadcast()">📡 Всем сразу</button>
          <button class="tool-btn" id="btn-pause" onclick="togglePause()">⏸ Пауза</button>
        </div>
      </div>
      <div class="tool-sec">
        <div class="tool-lbl">Модели</div>
        <div class="tool-hint" data-i18n="blockHint">Кнопка = пауза модели. 🟢 получает запросы, 🔴 исключена из чата.</div>
        <div class="tool-btns">
          <button class="tool-btn" id="btn-claude"   onclick="toggleBlock('claude')"><span class="bind">🟢</span> Claude</button>
          <button class="tool-btn" id="btn-gpt"      onclick="toggleBlock('gpt')"><span class="bind">🟢</span> GPT</button>
          <button class="tool-btn" id="btn-deepseek" onclick="toggleBlock('deepseek')"><span class="bind">🟢</span> DeepSeek</button>
          <button class="tool-btn" id="btn-kimi"     onclick="toggleBlock('kimi')"><span class="bind">🟢</span> Kimi</button>
        </div>
      </div>
      <div class="tool-sec">
        <div class="tool-lbl">Анализ Q&A</div>
        <div class="tool-hint" data-i18n="qaHint">Ответы моделей копятся в очередь. Ты сам выбираешь что сохранить в базу знаний — без автоматики.</div>
        <label class="tog-wrap">
          <span class="tog-lbl">Перехватывать ответы</span>
          <label class="tog-sw">
            <input type="checkbox" id="qa-tog" onchange="onQaTog(this.checked)">
            <div class="tog-track"></div>
            <div class="tog-knob"></div>
          </label>
        </label>
        <div id="qa-status" style="font-size:13px;color:var(--icq-text-muted)">
          Выключен
        </div>
      </div>
      <div class="tool-sec" id="qa-save-sec" style="display:none">
        <div class="tool-lbl">Очередь <span id="qa-cnt">0</span> — выбери и сохрани</div>
        <div id="save-list"></div>
        <button class="tool-btn primary w100" onclick="saveSelected()">📌 Сохранить выбранные</button>
      </div>
      <div class="tool-sec">
        <div class="tool-lbl">Действия</div>
        <div class="tool-btns">
          <button class="tool-btn" onclick="saveDataset()">💾 Сессия</button>
          <button class="tool-btn" onclick="resetTokens()">⟳ Токены</button>
        </div>
      </div>
    </div>

    <!-- Агент tools -->
    <div id="tools-agent" style="display:none">
      <div class="card-hdr"><span class="card-title">🛠 Агент</span></div>
      <div class="tool-sec">
        <div class="tool-lbl">Задача для агента</div>
        <textarea id="agent-task" rows="4"
          placeholder="Опиши задачу... например: проверь все tools и напиши отчёт"
          oninput="_saveAgentInput()"
          style="width:100%;box-sizing:border-box;padding:6px;font-size:12px;font-family:Tahoma;border:1px solid var(--icq-border-light);background:var(--icq-panel);resize:vertical"></textarea>
      </div>
      <div class="tool-sec">
        <div class="tool-lbl">Контекст (опционально)</div>
        <textarea id="agent-context" rows="2"
          placeholder="Дополнительный контекст..."
          oninput="_saveAgentInput()"
          style="width:100%;box-sizing:border-box;padding:6px;font-size:12px;font-family:Tahoma;border:1px solid var(--icq-border-light);background:var(--icq-panel);resize:vertical"></textarea>
      </div>
      <div class="tool-sec">
        <div class="tool-lbl">Подключить чаты</div>
        <div style="display:flex;flex-wrap:wrap;gap:3px;margin-bottom:4px">
          <button class="tool-btn" onclick="browserConnect(['claude'])">Claude</button>
          <button class="tool-btn" onclick="browserConnect(['gpt'])">GPT</button>
          <button class="tool-btn" onclick="browserConnect(['deepseek'])">DeepSeek</button>
          <button class="tool-btn" onclick="browserConnect(['kimi'])">Kimi</button>
          <button class="tool-btn" style="background:#4a6b2a" onclick="browserConnect(null)">🌐 Все</button>
        </div>
        <div id="browser-status" style="font-size:11px;font-family:monospace"></div>
      </div>
      <div class="tool-sec" style="display:flex;gap:4px">
        <button class="tool-btn" style="flex:1" onclick="agentBuildPlan()">📋 Построить план</button>
        <button class="tool-btn" style="flex:1;background:#4a6b2a" onclick="agentRunTask()">▶ Запустить</button>
      </div>
      <div class="tool-sec" id="agent-plan-box" style="display:none">
        <div class="tool-lbl">План (<span id="agent-step-count">0</span> шагов)</div>
        <div id="agent-plan" style="font-size:11px;max-height:160px;overflow-y:auto;background:var(--icq-bg);border:1px solid var(--icq-border-light);padding:4px"></div>
        <button class="tool-btn w100" style="margin-top:4px" onclick="agentExecutePlan()">⚙️ Выполнить план</button>
      </div>
      <div class="tool-sec">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div class="tool-lbl" style="margin:0">Лог выполнения</div>
          <button class="tool-btn" style="padding:1px 6px;font-size:11px;color:#cc0000" onclick="agentClearLog()">🗑 Очистить</button>
        </div>
        <div id="agent-log" style="font-size:11px;max-height:200px;overflow-y:auto;background:var(--icq-bg);border:1px solid var(--icq-border-light);padding:4px;font-family:monospace;margin-top:3px"></div>
      </div>
    </div>

    <!-- YANDI Помощник tools -->
    <div id="tools-local" style="display:none">
      <div class="card-hdr"><span class="card-title">🟣 YANDI Помощник</span></div>
      <div class="tool-sec">
        <div class="tool-lbl">Модель</div>
        <select id="local-model" style="width:100%;padding:4px;font-size:13px;font-family:Tahoma;border:1px solid var(--icq-border-light);background:var(--icq-panel)">
          <option value="heretic:q4">heretic:q4</option>
          <option value="heretic:q6">heretic:q6</option>
          <option value="heretic:q8" selected>heretic:q8</option>
          <option value="qwen3:14b">qwen3:14b</option>
          <option value="qwen3:7b">qwen3:7b</option>
          <option value="deepseek-r1:14b">deepseek-r1:14b</option>
          <option value="gemma4:e4b">gemma4:e4b</option>
        </select>
      </div>
      <div class="tool-sec">
        <div class="tool-lbl">Температура: <span id="local-temp-val">0.7</span></div>
        <input type="range" id="local-temp" min="0" max="1" step="0.05" value="0.7"
          style="width:100%" oninput="document.getElementById('local-temp-val').textContent=this.value">
      </div>
      <div class="tool-sec">
        <div class="tool-lbl" style="color:#888;font-size:11px">🔒 Приват — ничего не сохраняется.<br>История только в этой вкладке.</div>
      </div>
      <div class="tool-sec">
        <button class="tool-btn w100" onclick="clearLocalChat()" style="color:#cc0000">🗑 Очистить чат</button>
      </div>
    </div>

  </div><!-- .right-panel -->

</div><!-- .three-col -->

<!-- SETTINGS MODAL -->
<div class="modal" id="settings-modal">
  <div class="modal-box">
    <div class="modal-hdr">
      <h3>⚙ Настройки</h3>
      <button class="modal-x" onclick="closeSettings()">✕</button>
    </div>
    <div class="modal-body">
      <div class="f-row"><label>Claude URL</label>
        <input id="cfg-claude-url" type="text" placeholder="https://claude.ai/chat/..."></div>
      <div class="f-row"><label>GPT URL</label>
        <input id="cfg-gpt-url" type="text" placeholder="https://chatgpt.com/c/..."></div>
      <div class="f-row"><label>DeepSeek URL</label>
        <input id="cfg-deepseek-url" type="text" placeholder="https://chat.deepseek.com/"></div>
      <div class="f-row"><label style="color:#111">Kimi URL</label>
        <input id="cfg-kimi-url" type="text" placeholder="https://www.kimi.com/"></div>
      <div class="f-row"><label style="color:#7b2fff">Qwen URL</label>
        <input id="cfg-qwen-url" type="text" placeholder="https://chat.qwen.ai/"></div>
      <div class="f-row"><label>Proxy</label>
        <input id="cfg-proxy" type="text" placeholder="host:port:user:pass"></div>
      <div id="cfg-st" style="font-size:14px;min-height:14px;color:#006600"></div>
      <div class="modal-acts">
        <button class="tool-btn primary" onclick="saveConfig()">💾 Сохранить</button>
        <button class="tool-btn" onclick="closeSettings()">Отмена</button>
      </div>
    </div>
  </div>
</div>

<script>
// ── Globals ───────────────────────────────────────────────────────────────────
const NAMES={human:"Вы",claude:"Claude",gpt:"GPT",deepseek:"DeepSeek",
             kimi:"Kimi",system:"•",orchestrator:"🤖 Орк"};
let currentMode="orch", soundEnabled=true, qaOn=false;
let lastQ="", saveQueue=[];
let userLang=localStorage.getItem("userLang")||"ru";  // UI localization only
let userInputLang=localStorage.getItem("userInputLang")||"Russian";  // full language name, persists

const LANG_FLAGS={ru:"🇷🇺",en:"🇬🇧",zh:"🇨🇳",de:"🇩🇪",fr:"🇫🇷",
  es:"🇪🇸",ro:"🇷🇴",uk:"🇺🇦",ja:"🇯🇵",ko:"🇰🇷",ar:"🇸🇦",pl:"🇵🇱",tr:"🇹🇷"};

// ── UI i18n ───────────────────────────────────────────────────────────────────
const UI={
  ru:{sys:"📡 Система",models:"🌐 Модели",dataset:"📊 Датасет",tokens:"🔢 Токены",
      lang:"🗣 Язык",langHint:"Кнопка «Перевести» переводит текст на выбранный язык",
      orchTab:"🤖 Оркестратор",inetTab:"🌐 Интернет чат",
      send:"Отправить",clear:"🗑",sound:"🔊",settings:"⚙",
      orchPlaceholder:"Вопрос Оркестратору... (Enter — отправить)",
      inetPlaceholder:"Сообщение в чат... (Enter — отправить)",
      modeOrch:"Режим: Оркестратор",modeInet:"Режим: Интернет чат",
      yourTurn:"Ваш ход ✍️",traces:"Трейсов",verified:"Verified",
      connect:"Подключение",connectNote:"Требуются открытые вкладки с авторизованными чатами ИИ",
      checkConn:"🔍 Проверить связь",sending:"Отправка",blockHint:"Кнопка = пауза модели. 🟢 получает запросы, 🔴 исключена.",
      broadcast:"📡 Всем сразу",pause:"⏸ Пауза",blockSec:"Модели",
      qaTitle:"Анализ Q&A",qaHint:"Ответы копятся в очередь. Ты выбираешь что сохранить.",
      qaTog:"Перехватывать ответы",qaOff:"Выключен",saveQueue:"Очередь",saveSel:"📌 Сохранить выбранные",
      actions:"Действия",session:"💾 Сессия",resetTok:"⟳ Токены",exportMd:"📁 .md",copyCtx:"📋 Контекст",
      trust:"Доверие",orchTools:"🔮 Оркестратор",inetTools:"🌐 Интернет чат",
      trBtn:"🌐 Перевести"},
  en:{sys:"📡 System",models:"🌐 Models",dataset:"📊 Dataset",tokens:"🔢 Tokens",
      lang:"🗣 Language",langHint:"'Translate' button under messages translates to selected language",
      orchTab:"🤖 Orchestrator",inetTab:"🌐 Internet chat",
      send:"Send",clear:"🗑",sound:"🔊",settings:"⚙",
      orchPlaceholder:"Question to Orchestrator... (Enter to send)",
      inetPlaceholder:"Message to chat... (Enter to send)",
      modeOrch:"Mode: Orchestrator",modeInet:"Mode: Internet chat",
      yourTurn:"Your turn ✍️",traces:"Traces",verified:"Verified",
      connect:"Connection",connectNote:"Open browser tabs with authorized AI chats required",
      checkConn:"🔍 Check connection",sending:"Send to",blockHint:"Button = pause model. 🟢 active, 🔴 excluded.",
      broadcast:"📡 Broadcast all",pause:"⏸ Pause",blockSec:"Models",
      qaTitle:"Q&A Analysis",qaHint:"Model answers queue up. You choose what to save.",
      qaTog:"Capture answers",qaOff:"Off",saveQueue:"Queue",saveSel:"📌 Save selected",
      actions:"Actions",session:"💾 Session",resetTok:"⟳ Tokens",exportMd:"📁 .md",copyCtx:"📋 Context",
      trust:"Trust",orchTools:"🔮 Orchestrator",inetTools:"🌐 Internet chat",
      trBtn:"🌐 Translate"},
  zh:{sys:"📡 系统",models:"🌐 模型",dataset:"📊 数据集",tokens:"🔢 令牌",
      lang:"🗣 语言",langHint:"消息下方的「翻译」按钮将文本翻译成所选语言",
      orchTab:"🤖 编排器",inetTab:"🌐 网络聊天",
      send:"发送",clear:"🗑",sound:"🔊",settings:"⚙",
      orchPlaceholder:"向编排器提问…（回车发送）",inetPlaceholder:"发送消息…（回车发送）",
      modeOrch:"模式：编排器",modeInet:"模式：网络聊天",
      yourTurn:"轮到你了 ✍️",traces:"追踪",verified:"已验证",
      connect:"连接",connectNote:"需要打开已授权AI聊天的浏览器标签页",
      checkConn:"🔍 检查连接",sending:"发送至",blockHint:"按钮=暂停模型。🟢接收，🔴排除。",
      broadcast:"📡 全部广播",pause:"⏸ 暂停",blockSec:"模型",
      qaTitle:"问答分析",qaHint:"模型答案进入队列。您选择保存内容。",
      qaTog:"捕获答案",qaOff:"关闭",saveQueue:"队列",saveSel:"📌 保存所选",
      actions:"操作",session:"💾 会话",resetTok:"⟳ 令牌",exportMd:"📁 .md",copyCtx:"📋 上下文",
      trust:"可信度",orchTools:"🔮 编排器",inetTools:"🌐 网络聊天",
      trBtn:"🌐 翻译"},
};
// Fallback: use Russian for any missing language
function t(key){return(UI[userLang]||UI.ru)[key]||(UI.ru[key]||key)}

function applyI18n(){
  document.querySelectorAll("[data-i18n]").forEach(el=>{
    const k=el.dataset.i18n;
    el.textContent=t(k);
  });
  // Placeholders
  const inp=document.getElementById("inp");
  if(inp)inp.placeholder=currentMode==="orch"?t("orchPlaceholder"):t("inetPlaceholder");
  // Status bar
  const sbm=document.getElementById("sb-mode");
  if(sbm)sbm.textContent=currentMode==="orch"?t("modeOrch"):t("modeInet");
  const sbt=document.getElementById("sb-turn");
  if(sbt&&sbt.textContent.includes("✍"))sbt.textContent=t("yourTurn");
  // Translate buttons already rendered — update them
  document.querySelectorAll(".tr-btn").forEach(b=>{if(!b.disabled)b.textContent=t("trBtn")});
}

function setUserLang(lang){
  userLang=lang;
  localStorage.setItem("userLang",lang);
  applyI18n();
}

async function translateMsg(btnEl, text, msgId){
  // Читаем ТОЛЬКО оригинальный текст — клонируем пузырь без .tr-block
  if(!text){
    const bub=btnEl.closest(".msg")?.querySelector(".bubble");
    if(!bub)return;
    const clone=bub.cloneNode(true);
    clone.querySelector(".tr-block")?.remove();
    text=(clone.innerText||clone.textContent).trim();
  }
  if(!text)return;
  // Определяем язык пользователя из последнего его сообщения в истории
  const targetLang=userInputLang||"Russian";
  btnEl.disabled=true;
  btnEl.textContent="⏳...";
  try{
    const r=await fetch("/api/council/translate",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({text,target_lang:targetLang}),
    });
    const d=await r.json();
    if(d.ok){
      const bubble=btnEl.closest(".msg").querySelector(".bubble");
      // Remove existing translation block if any
      bubble.querySelector(".tr-block")?.remove();
      const div=document.createElement("div");
      div.className="tr-block";
      div.innerHTML=`<div class="tr-flag">🌐 ${d.source_lang||"?"} → ${d.target_lang||userInputLang}:</div>`+esc(d.translation).split("\\n").join("<br>");
      bubble.appendChild(div);
      btnEl.textContent="🌐 Перевести";
      btnEl.disabled=false;
    } else {
      btnEl.textContent="❌ "+d.error;
      btnEl.disabled=false;
    }
  }catch(e){btnEl.textContent="❌ "+e.message;btnEl.disabled=false}
}

function copyMsg(btn){
  const bubble=btn.closest(".msg").querySelector(".bubble");
  const text=bubble.innerText||bubble.textContent;
  navigator.clipboard.writeText(text).then(()=>{
    const orig=btn.textContent;btn.textContent="✅";
    setTimeout(()=>btn.textContent=orig,1200);
  }).catch(()=>{btn.textContent="❌";setTimeout(()=>btn.textContent="📋 Копировать",1200)});
}

async function deleteMsg(btn, msgId){
  const div=btn.closest(".msg");
  if(msgId){
    await fetch("/api/council/message/delete",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:msgId})}).catch(()=>{});
  }
  div.remove();
}

function deleteLocalMsg(idx){
  _localHistory.splice(idx,1);
  fetch("/api/local/clear",{method:"POST"}).then(()=>{
    return Promise.all(_localHistory.map(m=>fetch("/api/local/message",{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(m)})));
  }).catch(()=>{});
  renderLocalChat();
}

function _updateCopyChatBar(){
  const bar=document.getElementById("copy-chat-bar");
  if(!bar)return;
  bar.style.display=activeMsgsEl().children.length>0?"":"none";
}

function copyFullChat(btn){
  const msgEls=activeMsgsEl().querySelectorAll(".msg");
  const lines=[];
  msgEls.forEach(el=>{
    const name=el.querySelector(".msg-name")?.textContent||"";
    const time=el.querySelector(".msg-meta span:last-child")?.textContent||"";
    const text=el.querySelector(".bubble")?.innerText||"";
    if(text.trim())lines.push("["+name+(time?" "+time:"")+"]\\n"+text.trim());
  });
  const full=lines.join("\\n\\n");
  const b=document.getElementById("copy-chat-bar")?.querySelector("button");
  navigator.clipboard.writeText(full).then(()=>{
    if(b){const o=b.textContent;b.textContent="✅ Скопировано!";setTimeout(()=>b.textContent=o,1500);}
  }).catch(()=>{if(b){b.textContent="❌";setTimeout(()=>b.textContent="📋 Копировать весь чат",1500);}});
}

const bstate={paused:false,claude_blocked:false,gpt_blocked:false,deepseek_blocked:false,kimi_blocked:false};
function activeMsgsEl(){return document.getElementById("msgs-"+currentMode)||document.getElementById("msgs-orch")}
function msgsElFor(tab){return document.getElementById("msgs-"+(tab||"orch"))||document.getElementById("msgs-orch")}
const inpEl=document.getElementById("inp");
const sendBtn=document.getElementById("send-btn");
const sbMode=document.getElementById("sb-mode");
const sbTurn=document.getElementById("sb-turn");

// ── WebSocket с авто-переподключением ─────────────────────────────────────────
const proto=location.protocol==="https:"?"wss:":"ws:";
let ws, _wsRetry=0;
function wsConnect(){
  ws=new WebSocket(proto+"//"+location.host+"/ws/human");
  ws.onopen=()=>{
    setDot("st-redis",true);
    _wsRetry=0;
    // При восстановлении соединения — обновить историю активной вкладки
    if(currentMode==="orch") _loadTabHistory("orch");
    else if(currentMode==="inet") _loadTabHistory("inet");
  };
  ws.onclose=()=>{
    setDot("st-redis",false);
    sbTurn.textContent="Соединение потеряно — переподключение...";
    const delay=Math.min(1000*Math.pow(2,_wsRetry++),15000);
    setTimeout(wsConnect, delay);
  };
  ws.onerror=()=>ws.close();
}
wsConnect();

ws.onmessage=(e)=>{
  const d=JSON.parse(e.data);
  if(d.type==="history"){
    // Грузим историю в нужную панель
    const panel=msgsElFor(d.tab||"orch");
    panel.innerHTML="";
    d.messages.forEach(m=>addMsg(m,panel));
    _updateCopyChatBar();
    setTurn(d.turn||"human");
    return;
  }
  if(d.type==="message"){
    // Роутим в панель по tab или по from
    const tab=d.tab||(["orchestrator"].includes(d.from)?"orch":"inet");
    addMsg(d, msgsElFor(tab));
    setTurn(d.turn_next||"human");
    return;
  }
  if(d.type==="status")              {handleStatus(d);return}
  if(d.type==="turn")                {setTurn(d.turn);return}
  if(d.type==="relay_timer")         {showRelayTimer(d.seconds);return}
  if(d.type==="bridge_state")        {applyBridgeState(d);return}
  if(d.type==="tokens")              {updateTokUI(d.tokens);return}
  if(d.type==="orch_update")         {handleOrchUpd(d);return}
  if(d.type==="system")              {addMsg({from:"system",tab:"orch",text:d.text,ts:now()},msgsElFor("orch"));return}
  if(d.type==="inet_broadcast_start"){handleInetBroadcastStart(d);return}
  if(d.type==="inet_model_replied")  {handleInetModelReplied(d);return}
  if(d.type==="inet_ready")          {handleInetReady();return}
};

// ── Mode switch ───────────────────────────────────────────────────────────────
async function switchMode(mode){
  currentMode=mode;
  localStorage.setItem("activeTab", mode);
  document.getElementById("tab-orch").classList.toggle("active",mode==="orch");
  document.getElementById("tab-inet").classList.toggle("active",mode==="inet");
  document.getElementById("tab-local").classList.toggle("active",mode==="local");
  document.getElementById("tab-agent").classList.toggle("active",mode==="agent");
  document.getElementById("tools-orch").style.display=mode==="orch"?"":"none";
  document.getElementById("tools-inet").style.display=mode==="inet"?"":"none";
  document.getElementById("tools-local").style.display=mode==="local"?"":"none";
  document.getElementById("tools-agent").style.display=mode==="agent"?"":"none";
  document.getElementById("web-wrap").style.display=mode==="orch"?"":"none";
  // Показываем нужную панель сообщений
  ["orch","inet","local","agent"].forEach(t=>{
    const el=document.getElementById("msgs-"+t);
    if(el)el.style.display=mode===t?"":"none";
  });
  _updateCopyChatBar();
  if(mode==="orch") _loadTabHistory("orch");
  if(mode==="inet") _loadTabHistory("inet");
  if(mode==="local"){_loadLocalHistory();_loadLocalModels();}
  if(mode==="agent"){_loadAgentState();}
  applyI18n();
}

async function _loadTabHistory(tab){
  try{
    const d=await fetch("/api/"+tab+"/history").then(r=>r.json());
    const panel=msgsElFor(tab);
    panel.innerHTML="";
    (d.messages||[]).forEach(m=>addMsg(m,panel));
    _updateCopyChatBar();
  }catch(_){}
}

// ── Агент ─────────────────────────────────────────────────────────────────────
let _agentPlan=[];

function _agentLog(html, save=true){
  // Лог отображается в центральной панели msgs-agent
  const el=document.getElementById("msgs-agent");
  el.innerHTML+=html+"<br>";
  el.scrollTop=el.scrollHeight;
  // Синхронизируем и с боковым agent-log (если видим)
  const side=document.getElementById("agent-log");
  if(side){side.innerHTML+=html+"<br>";side.scrollTop=side.scrollHeight;}
  if(save) fetch("/api/agent/log",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({html})}).catch(()=>{});
}

async function _loadAgentState(){
  try{
    const d=await fetch("/api/agent/state").then(r=>r.json());
    if(d.task) document.getElementById("agent-task").value=d.task;
    if(d.context) document.getElementById("agent-context").value=d.context;
    const msgsEl=document.getElementById("msgs-agent");
    const logEl=document.getElementById("agent-log");
    if(d.log?.length){
      const logHtml=d.log.join("<br>")+"<br>";
      msgsEl.innerHTML=logHtml;
      msgsEl.scrollTop=msgsEl.scrollHeight;
      if(logEl){logEl.innerHTML=logHtml;logEl.scrollTop=logEl.scrollHeight;}
    }
  }catch(_){}
}

async function _saveAgentInput(){
  const task=(document.getElementById("agent-task").value||"");
  const context=(document.getElementById("agent-context").value||"");
  fetch("/api/agent/state",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({task,context})}).catch(()=>{});
}

async function agentClearLog(){
  document.getElementById("msgs-agent").innerHTML="";
  const side=document.getElementById("agent-log");
  if(side) side.innerHTML="";
  _agentPlan=[];
  document.getElementById("agent-plan-box").style.display="none";
  await fetch("/api/agent/clear",{method:"POST"}).catch(()=>{});
}

async function browserConnect(models){
  const statusEl=document.getElementById("browser-status");
  statusEl.textContent="⏳ Проверяем и открываем...";
  _agentLog("🌐 Подключаем: "+(models?models.join(", "):"все"));
  try{
    const r=await fetch("/api/browser/connect",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({models,wait_sec:8}),
    });
    const d=await r.json();
    const lines=Object.entries(d.models||{}).map(([m,v])=>`${v.status} ${m}`);
    statusEl.innerHTML=lines.join("<br>");
    for(const [m,v] of Object.entries(d.models||{})){
      _agentLog(`  ${v.status} ${m}${v.action==="opened"?" → открыли вкладку":""}`);
    }
  }catch(e){statusEl.textContent="❌ "+e.message;}
}

async function agentBuildPlan(){
  const task=(document.getElementById("agent-task").value||"").trim();
  if(!task){alert("Введи задачу!");return;}
  const ctx=document.getElementById("agent-context").value||"";
  _agentLog("📋 Строим план...");
  sbTurn.textContent="⏳ Планирование...";
  try{
    const r=await fetch("/api/tools/plan",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({task,context:ctx}),
    });
    const d=await r.json();
    if(!d.ok){_agentLog("❌ "+d.error);return;}
    _agentPlan=d.steps||[];
    document.getElementById("agent-step-count").textContent=_agentPlan.length;
    const planEl=document.getElementById("agent-plan");
    planEl.innerHTML=_agentPlan.map(s=>
      `<div style="margin-bottom:3px"><b>[${s.step}]</b> <code>${s.tool}</code> — ${escHtml(s.description||"")}</div>`
    ).join("");
    document.getElementById("agent-plan-box").style.display="";
    _agentLog(`✅ План: ${_agentPlan.length} шагов`);
    sbTurn.textContent="✅ План готов";
  }catch(e){_agentLog("❌ "+e.message);sbTurn.textContent="❌ Ошибка";}
}

async function agentExecutePlan(){
  if(!_agentPlan.length){_agentLog("❌ Нет плана");return;}
  _agentLog("⚙️ Выполняем план...");
  sbTurn.textContent="⏳ Выполнение...";
  try{
    const r=await fetch("/api/tools/execute",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({steps:_agentPlan,stop_on_fail:false}),
    });
    const d=await r.json();
    for(const e of (d.log||[])){
      const ok=e.ok?"✅":"❌";
      _agentLog(`${ok} [${e.step}] <b>${e.tool}</b> (${e.elapsed}s)`);
    }
    _agentLog(d.ok?"✅ Выполнено":"❌ Есть ошибки");
    sbTurn.textContent=d.ok?"✅ Готово":"❌ Ошибки";
  }catch(e){_agentLog("❌ "+e.message);}
}

async function agentRunTask(){
  const task=(document.getElementById("agent-task").value||"").trim();
  if(!task){alert("Введи задачу!");return;}
  const ctx=document.getElementById("agent-context").value||"";
  document.getElementById("agent-log").innerHTML="";
  _agentLog("🤖 Задача: <b>"+escHtml(task)+"</b>");
  _agentLog("📋 Строим план → выполняем → проверяем...");
  sbTurn.textContent="⏳ Агент работает...";
  try{
    const r=await fetch("/api/tools/run_task",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({task,context:ctx}),
    });
    const d=await r.json();
    // Показываем ошибку планирования
    if(d.stage==="planning"){
      _agentLog(`❌ Планировщик: ${escHtml(d.error||"")}`)
      if(d.raw) _agentLog(`<details><summary>raw</summary>${escHtml(d.raw)}</details>`);
      sbTurn.textContent="❌ Ошибка планирования";
      return;
    }
    if(d.exec?.log){
      for(const e of d.exec.log){
        const ok=e.ok?"✅":"❌";
        _agentLog(`${ok} [${e.step}] <b>${e.tool}</b> (${e.elapsed}s)`);
      }
    }
    const verdict=escHtml(d.verdict||"нет вердикта");
    _agentLog(`<br>${d.ok?"✅":"❌"} <b>Вердикт:</b> ${verdict}`);
    _agentLog(`⏱ ${d.elapsed??0}s | ${d.steps??0} шагов`);
    sbTurn.textContent=d.ok?"✅ Задача выполнена":"❌ Задача не выполнена";
  }catch(e){_agentLog("❌ "+e.message);sbTurn.textContent="❌ Ошибка";}
}

// ── Send ──────────────────────────────────────────────────────────────────────
async function sendMessage(){
  const text=inpEl.value.trim();
  if(!text)return;
  inpEl.value="";
  if(text.length>8) _detectUserLang(text);
  if(currentMode==="orch"){
    await sendToOrch(text);
  } else if(currentMode==="local"){
    await sendToLocal(text);
  } else {
    if(ws.readyState!==WebSocket.OPEN)return;
    lastQ=text;
    ws.send(JSON.stringify({text, tab: currentMode}));
    // кнопка блокируется через inet_broadcast_start от сервера
  }
}

// ── YANDI Помощник (локальный приват-чат) ─────────────────────────────────────
let _localHistory=[];

function renderLocalChat(){
  const msgs=document.getElementById("msgs-local");
  if(!msgs)return;
  msgs.innerHTML="";
  for(const m of _localHistory){
    const div=document.createElement("div");
    div.className="msg "+(m.role==="user"?"human":"local-ai");
    const idx=_localHistory.indexOf(m);
    const trBtn=m.role==="assistant"
      ?`<button class="act-btn" onclick="translateMsg(this,'','')">🌐 Перевести</button>`
      :"";
    div.innerHTML=`<div class="msg-meta"><span class="msg-name">${m.role==="user"?"Вы":"🟣 YANDI"}</span><span class="msg-time">${m.ts||""}</span></div><div class="bubble">${escHtml(m.content)}</div>
      <div class="msg-actions">${trBtn}
        <button class="act-btn" onclick="copyMsg(this)">📋 Копировать</button>
        <button class="act-btn del" onclick="deleteLocalMsg(${idx})">🗑 Удалить</button>
      </div>`;
    msgs.appendChild(div);
  }
  msgs.scrollTop=msgs.scrollHeight;
  _updateCopyChatBar();
}

function escHtml(s){return s.trim().replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/\\n/g,"<br>")}

async function _saveLocalMsg(msg){
  try{ await fetch("/api/local/message",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(msg)}); }catch(_){}
}

async function clearLocalChat(){
  _localHistory=[];
  renderLocalChat();
  try{ await fetch("/api/local/clear",{method:"POST"}); }catch(_){}
}

async function _loadLocalHistory(){
  try{
    const d=await fetch("/api/local/history").then(r=>r.json());
    if(d.messages?.length){ _localHistory=d.messages; renderLocalChat(); }
  }catch(_){}
}

async function _loadLocalModels(){
  try{
    const d=await fetch("/api/local/models").then(r=>r.json());
    if(!d.models?.length)return;
    const sel=document.getElementById("local-model");
    const cur=sel.value;
    sel.innerHTML=d.models.map(m=>`<option value="${m}"${m===cur?" selected":""}>${m}</option>`).join("");
  }catch(_){}
}

async function sendToLocal(text){
  const model=document.getElementById("local-model").value;
  const temp=parseFloat(document.getElementById("local-temp").value);
  const ts=now();
  const userMsg={role:"user",content:text,ts};
  _localHistory.push(userMsg);
  renderLocalChat();
  await _saveLocalMsg(userMsg);
  sendBtn.disabled=true;
  sbTurn.textContent="⏳ Думает...";

  const thinkId="local-think-"+Date.now();
  const msgs=document.getElementById("msgs-local");
  const ph=document.createElement("div");
  ph.className="msg local-ai"; ph.id=thinkId;
  ph.innerHTML=`<div class="msg-meta"><span class="msg-name">🟣 YANDI</span></div><div class="bubble" id="${thinkId}-bub">⏳</div>`;
  msgs.appendChild(ph); msgs.scrollTop=msgs.scrollHeight;

  try{
    const r=await fetch("/api/local/chat",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({model,temperature:temp,messages:_localHistory.map(m=>({role:m.role,content:m.content}))}),
    });
    const d=await r.json();
    const reply=d.content||"[нет ответа]";
    const aiMsg={role:"assistant",content:reply,ts:now()};
    _localHistory.push(aiMsg);
    document.getElementById(thinkId)?.remove();
    renderLocalChat();
    await _saveLocalMsg(aiMsg);
    sbTurn.textContent="✅ Готово";
  }catch(e){
    document.getElementById(thinkId)?.remove();
    const errMsg={role:"assistant",content:"❌ Ошибка: "+e.message,ts:now()};
    _localHistory.push(errMsg);
    renderLocalChat();
    await _saveLocalMsg(errMsg);
    sbTurn.textContent="❌ Ошибка";
  }
  sendBtn.disabled=false;
  inpEl.focus();
}

async function _detectUserLang(text){
  try{
    const r=await fetch("/api/council/translate",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({text,detect_only:true}),
    });
    const d=await r.json();
    // source_lang теперь полное название: "Russian", "Chinese", "Nanai" и т.д.
    if(d.source_lang){
      userInputLang=d.source_lang;
      localStorage.setItem("userInputLang", d.source_lang);
    }
  }catch(_){}
}

async function sendToOrch(query){
  const useWeb=document.getElementById("web-chk").checked;
  sendBtn.disabled=true;
  sbTurn.textContent=useWeb?"⏳ Ищу + веб...":"⏳ Обрабатываю...";
  // Оптимистично показываем вопрос пользователя сразу — не ждём WS
  const qPanel=msgsElFor("orch");
  addMsg({from:"human",tab:"orch",text:query,ts:now(),id:"tmp-"+Date.now()},qPanel);
  try{
    const r=await fetch("/api/orchestrator/ask",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({query,enable_web:useWeb}),
    });
    const d=await r.json();
    sbTurn.textContent=d.ok?`✅ ${(d.latency||0).toFixed(1)}s | проверка в фоне...`:`❌ ${d.error}`;
    // Всегда перезагружаем историю — подхватывает ответ даже если WS лагнул
    await _loadTabHistory("orch");
  }catch(e){sbTurn.textContent="❌ "+e.message}
  finally{sendBtn.disabled=false;inpEl.focus()}
}

// ── Broadcast (inet) ──────────────────────────────────────────────────────────
async function doBroadcast(){
  const text=inpEl.value.trim();
  if(!text){inpEl.focus();return}
  inpEl.value="";lastQ=text;
  await fetch("/api/council/broadcast",{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({text}),
  });
}

// ── Q&A Parser ────────────────────────────────────────────────────────────────
function onQaTog(on){
  qaOn=on;
  const st=document.getElementById("qa-status");
  const sec=document.getElementById("qa-save-sec");
  if(on){st.textContent="✅ Включён";st.style.color="#006600";sec.style.display=""}
  else  {st.textContent="Выключен";st.style.color="var(--icq-text-muted)";
         sec.style.display="none";saveQueue=[];renderSaveQ()}
}

function addToSaveQ(msgId,from,answer){
  if(!qaOn||currentMode!=="inet"||!lastQ)return;
  saveQueue.push({msgId,from,question:lastQ,answer});
  renderSaveQ();
}

function renderSaveQ(){
  const list=document.getElementById("save-list");
  const cnt=document.getElementById("qa-cnt");
  if(!list)return;
  cnt.textContent=saveQueue.length;
  if(!saveQueue.length){list.innerHTML='<div style="font-size:13px;color:var(--icq-text-muted)">Пусто</div>';return}
  list.innerHTML=saveQueue.map((it,i)=>`
    <div class="save-item">
      <input type="checkbox" id="sq${i}" checked>
      <div class="save-item-body">
        <span class="save-from">${NAMES[it.from]||it.from}:</span>
        ${esc(it.answer.slice(0,70))}${it.answer.length>70?"…":""}
      </div>
    </div>`).join("");
}

async function saveSelected(){
  const items=saveQueue.filter((_,i)=>{const c=document.getElementById("sq"+i);return c&&c.checked});
  if(!items.length)return;
  let saved=0;
  for(const it of items){
    try{
      const r=await fetch("/api/orchestrator/remember",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({question:it.question,answer:it.answer,msg_id:it.msgId}),
      });
      if((await r.json()).ok)saved++;
    }catch(e){}
  }
  const checked=new Set(saveQueue.filter((_,i)=>{const c=document.getElementById("sq"+i);return c&&c.checked}).map(x=>x.msgId));
  saveQueue=saveQueue.filter(x=>!checked.has(x.msgId));
  renderSaveQ();
  if(saved>0){addMsg({from:"system",text:`📌 Сохранено ${saved} Q&A в базу`,ts:now()});refreshStats()}
}

// ── Add message ───────────────────────────────────────────────────────────────
function addMsg(m, panel){
  const who=m.from||"system";
  if(!panel) panel=msgsElFor(m.tab||(["orchestrator"].includes(who)?"orch":"inet"));
  const div=document.createElement("div");
  div.className="msg "+who;
  div.dataset.msgId=m.id||"";
  div.dataset.question=m._question||"";
  div.dataset.answer=m.text||"";

  let badge="";
  if(m.trust_level){
    const L={VERIFIED:"✅ Verified",PARTIALLY_VERIFIED:"⚠ Частично",
             UNVERIFIED:"⏳ Pending",REJECTED:"❌ Rejected",HYPOTHESIS:"💭 Гипотеза"};
    badge=`<span class="trust-badge trust-${m.trust_level}">${L[m.trust_level]||m.trust_level}</span>`;
  }
  const prelim=m.preliminary?`<span class="trust-badge trust-UNVERIFIED">предв.</span>`:"";

  let fbRow="";
  if(who==="orchestrator"){
    const mid=m.id||Math.random().toString(36).slice(2);
    fbRow=`<div class="fb-row">
      <button class="fb-btn" id="fbp-${mid}" onclick="sendFB('${mid}','positive',this)">👍</button>
      <button class="fb-btn" id="fbn-${mid}" onclick="sendFB('${mid}','negative',this)">👎</button>
      <button class="fb-btn" id="fbr-${mid}" onclick="rememberAns('${mid}',this)">📌 Запомнить</button>
    </div>`;
  }

  if(["claude","gpt","deepseek","kimi"].includes(who))addToSaveQ(m.id||"",who,m.text||"");

  const mid2=m.id||Math.random().toString(36).slice(2);
  const txt=m.text||"";
  const actions=who!=="system"?`<div class="msg-actions">
    ${!["human","system"].includes(who)?`<button class="act-btn" onclick="translateMsg(this,'','${mid2}')">🌐 Перевести</button>`:""}
    <button class="act-btn" onclick="copyMsg(this)">📋 Копировать</button>
    <button class="act-btn del" onclick="deleteMsg(this,${JSON.stringify(m.id||"")})">🗑 Удалить</button>
  </div>`:"";

  div.innerHTML=`
    <div class="msg-meta">
      <span class="msg-name">${NAMES[who]||who}</span>${badge}${prelim}
      <span>${m.ts||""}</span>
    </div>
    <div class="bubble">${renderText(txt)}</div>${fbRow}${actions}`;
  panel.appendChild(div);
  panel.scrollTop=panel.scrollHeight;
  _updateCopyChatBar();
  if(["claude","gpt","deepseek","kimi"].includes(who))playSound(who);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
function renderText(s){
  let t=esc(s);
  t=t.replace(/https?:\\/\\/([^\\s&<"]+)/g,(url,rest)=>{
    try{const h=new URL(url.replace(/&amp;/g,"&")).hostname.replace(/^www\\./,"");
        return `<a class="cl" href="${url}" target="_blank" rel="noopener">${h}</a>`}
    catch{return url}
  });
  return t.split("\\n").join("<br>");
}
function now(){return new Date().toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"})}

function setTurn(t){
  const M={human:"Ваш ход ✍️",claude:"Ждём Claude...",gpt:"Ждём GPT...",deepseek:"Ждём DeepSeek..."};
  sbTurn.textContent=M[t]||t;
  sendBtn.disabled=false;
  if(t==="human")inpEl.focus();
}

function setDot(id,on,label){
  const el=document.getElementById(id);if(!el)return;
  el.className=on?"dot-on":"dot-off";
  el.textContent="● "+(label||(on?"online":"offline"));
}

function handleStatus(d){
  const map={claude:"st-claude",gpt:"st-gpt",deepseek:"st-deepseek"};
  const sid=map[d.who];if(!sid)return;
  const el=document.getElementById(sid);if(!el)return;
  if(d.state==="typing"){el.className="dot-typ";el.textContent="● печатает..."}
  else{el.className=d.state==="online"?"dot-on":"dot-off";
       el.textContent="● "+(d.state==="online"?"online":"offline")}
}

async function refreshStats(){
  try{
    const d=await fetch("/api/orchestrator/stats").then(r=>r.json());
    document.getElementById("ds-traces").textContent=d.monitoring?.requests_total??"–";
    document.getElementById("ds-verified").textContent=d.knowledge?.verified_count??"–";
    setDot("st-orch",true,"ready");setDot("st-ollama",true,"ok");
  }catch(e){setDot("st-orch",false,"error")}
  try{
    const d=await fetch("/api/council/tokens").then(r=>r.json());
    updateTokUI(d.tokens);setDot("st-redis",true);
  }catch(e){setDot("st-redis",false)}
}

function updateTokUI(tokens){
  const fk=n=>n>=1000?(n/1000).toFixed(1)+"k":String(n);
  for(const[m,c]of Object.entries(tokens||{})){
    const el=document.getElementById("tk-"+m);
    if(el)el.textContent=`↑${fk(c.sent)} ↓${fk(c.recv)}`;
  }
}

// ── Connections ───────────────────────────────────────────────────────────────
const _DEFAULT_URLS={claude:"https://claude.ai/",gpt:"https://chatgpt.com/",deepseek:"https://chat.deepseek.com/",kimi:"https://www.kimi.com/"};
let _connUrls={claude:"",gpt:"",deepseek:"",kimi:""};

function _setConnDot(model,state){
  // state: "on"|"off"|"unknown"
  const dot=document.getElementById("cdot-"+model);
  if(!dot)return;
  dot.textContent=state==="on"?"🟢":state==="off"?"🔴":"⚫";
  dot.title=state==="on"?"Подключено":state==="off"?"Нет связи":"Не проверялось";
  // Зеркалим в левую панель
  const ldot=document.getElementById("st-"+model);
  if(ldot){
    ldot.className=state==="on"?"dot-on":state==="off"?"dot-off":"dot-off";
    ldot.textContent=state==="on"?"● online":state==="off"?"● offline":"● –";
  }
}

async function checkConnections(){
  try{
    const d=await fetch("/api/council/connections").then(r=>r.json());
    for(const[m,s]of Object.entries(d)){
      _connUrls[m]=s.url||"";
      const state=s.connected?"on":(s.last_seen_sec===null?"unknown":"off");
      _setConnDot(m,state);
      const btn=document.getElementById("conn-"+m)?.querySelector(".conn-open");
      if(btn)btn.disabled=!s.url;
      // Подпись с возрастом
      const nameEl=document.getElementById("conn-"+m)?.querySelector(".conn-name");
      if(nameEl&&s.last_seen_sec!==null){
        const age=s.last_seen_sec<60?s.last_seen_sec+"с":Math.round(s.last_seen_sec/60)+"м";
        nameEl.title="Последний отклик: "+age+" назад";
      }
    }
  }catch(e){["claude","gpt","deepseek","kimi"].forEach(m=>_setConnDot(m,"unknown"))}
}

function openChat(model){
  const url=_connUrls[model]||_DEFAULT_URLS[model]||"";
  if(!url){
    addMsg({from:"system",text:"⚙ Укажите URL "+model+" в Настройках (⚙ в шапке)",ts:now()});
    return;
  }
  window.open(url,"_blank","noopener");
}

// ── Orchestrator actions ──────────────────────────────────────────────────────
async function sendFB(mid,type,btn,liked_version){
  liked_version=liked_version||"both";
  const d=btn.closest(".msg")?.dataset||{};
  try{
    await fetch("/api/orchestrator/feedback",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        question:d.question||"",answer:d.answer||"",feedback:type,
        liked_version:liked_version,
        deepseek_verdict:d.dsVerdict||"",
        deepseek_correction:d.dsCorrection||"",
      }),
    });
    btn.classList.add(type==="positive"?"active-pos":"active-neg");btn.disabled=true;
    // Заглушить противоположную кнопку в той же группе
    const pairs={
      "both":   {pos:"fbp-"+mid,  neg:"fbn-"+mid},
      "web":    {pos:"fbweb-"+mid,neg:"fwbn-"+mid},
      "deepseek":{pos:"fbds-"+mid, neg:"fbdsn-"+mid},
    };
    const p=pairs[liked_version];
    if(p){const o=document.getElementById(type==="positive"?p.neg:p.pos);if(o){o.disabled=true;o.style.opacity=".4"}}
  }catch(e){}
}

async function rememberAns(mid,btn){
  const d=btn.closest(".msg")?.dataset||{};
  if(!d.question||!d.answer){btn.textContent="❌ нет данных";return}
  let tags=[];try{tags=JSON.parse(d.tags||"[]")}catch{}
  try{
    const r=await fetch("/api/orchestrator/remember",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({question:d.question,answer:d.answer,msg_id:mid,tags}),
    });
    const j=await r.json();
    btn.textContent=j.ok?"✅ Сохранено":"❌ Ошибка";
    btn.classList.add("active-rem");btn.disabled=true;
    if(j.ok)refreshStats();
  }catch(e){btn.textContent="❌"}
}

// ── Review Queue ──────────────────────────────────────────────────────────────
let _reviewItems = [];
let _reviewActiveId = null;   // ID текущей открытой карточки

function _rvSetInputLocked(locked){
  // Блокируем ввод пока идёт ревью
  inpEl.disabled = locked;
  sendBtn.disabled = locked;
  inpEl.placeholder = locked
    ? "⛔ Закройте проверку перед отправкой вопроса"
    : "Вопрос Оркестратору... (Enter — отправить)";
}

function _rvClose(){
  // Закрыть текущую карточку и разблокировать ввод
  if(_reviewActiveId){
    const el = document.getElementById("review-msg-"+_reviewActiveId);
    if(el) el.remove();
    // Снять выделение в списке
    document.querySelectorAll(".rv-list-item").forEach(x=>x.classList.remove("rv-active"));
    _reviewActiveId = null;
  }
  _rvSetInputLocked(false);
}

function _rvUpdateCount(){
  const cnt = _reviewItems.length;
  document.getElementById("review-count").textContent = cnt > 0 ? `(${cnt})` : "";
}

async function loadReviewQueue(){
  try{
    const d = await fetch("/api/review/list").then(r=>r.json());
    _reviewItems = d.items || [];
    _rvUpdateCount();
    const list = document.getElementById("review-list");
    if(!_reviewItems.length){
      list.innerHTML = `<div style="padding:6px 10px;font-size:11px;color:var(--icq-text-muted)">Нет записей на проверку</div>`;
      return;
    }
    list.innerHTML = _reviewItems.map((it,i)=>`
      <div class="rv-list-item" id="rvli-${it.id}" onclick="openReviewItem(${i})"
        style="padding:4px 10px;font-size:11px;cursor:pointer;border-bottom:1px solid var(--icq-border);
               line-height:1.35;transition:background .1s">
        <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(it.query.slice(0,55))}</div>
        <div style="color:var(--icq-text-muted)">${it.tag} · ${(it.confidence*100).toFixed(0)}%</div>
      </div>`).join("");
  }catch(e){console.error("review load",e)}
}

function toggleReview(){
  const list = document.getElementById("review-list");
  const open = list.style.display === "none";
  list.style.display = open ? "block" : "none";
  if(open && !_reviewItems.length) loadReviewQueue();
}

function openReviewItem(idx){
  const it = _reviewItems[idx];
  if(!it) return;

  // Закрыть предыдущую карточку (любую) — только одна за раз
  _rvClose();

  switchMode("orch");
  _reviewActiveId = it.id;
  _rvSetInputLocked(true);

  // Подсветить активный элемент в списке
  document.querySelectorAll(".rv-list-item").forEach(x=>x.classList.remove("rv-active"));
  const li = document.getElementById("rvli-"+it.id);
  if(li) li.classList.add("rv-active");

  const panel = document.getElementById("msgs-orch");
  const div = document.createElement("div");
  div.id = "review-msg-" + it.id;
  div.style.cssText = "border:2px solid var(--icq-border-dark);border-radius:6px;margin:8px 4px;padding:0;background:var(--icq-bg)";

  div.innerHTML = `
    <div style="padding:6px 10px 4px;background:var(--icq-panel);border-bottom:1px solid var(--icq-border);
                border-radius:4px 4px 0 0;font-size:11px;color:var(--icq-text-muted);
                display:flex;justify-content:space-between;align-items:center">
      <span>📋 На проверку · <b>${esc(it.tag)}</b> · ${(it.confidence*100).toFixed(0)}%</span>
      <button onclick="_rvClose()" style="background:none;border:none;cursor:pointer;font-size:14px;
              color:var(--icq-text-muted);padding:0 2px;line-height:1" title="Закрыть">✕</button>
    </div>
    <div style="padding:8px 10px 4px">
      <div style="font-weight:bold;font-size:13px;margin-bottom:6px">${esc(it.query)}</div>
      <div id="rv-answer-${it.id}"
           style="font-size:12px;color:var(--icq-text);white-space:pre-wrap;line-height:1.5;
                  background:white;border:1px solid var(--icq-border);padding:6px 8px;
                  border-radius:3px;min-height:40px;outline:none"
           contenteditable="false">${esc(it.answer)}</div>
    </div>
    <div class="fb-row" style="padding:4px 10px 8px;gap:6px">
      <button class="fb-btn" onclick="rvVerify('${it.id}',this)">✅ Верно</button>
      <button class="fb-btn" onclick="rvEdit('${it.id}',this)">✏ Исправить</button>
      <button class="fb-btn" id="rv-save-${it.id}" onclick="rvSave('${it.id}',this)"
              style="display:none">💾 Сохранить</button>
      <button class="fb-btn act-btn del" onclick="rvDelete('${it.id}',this)">🗑 Удалить</button>
    </div>`;

  panel.appendChild(div);
  panel.scrollTop = panel.scrollHeight;
}

function _rvAfterAction(id){
  // Найти индекс следующего ДО удаления
  const nextIdx = _reviewItems.findIndex(x=>x.id===id) + 1;

  _reviewItems = _reviewItems.filter(x=>x.id!==id);
  _rvUpdateCount();
  const li = document.getElementById("rvli-"+id);
  if(li) li.remove();
  _rvClose();
  refreshStats();

  const auto = document.getElementById("rv-auto")?.checked;
  if(auto){
    if(_reviewItems.length === 0){
      // Список закончился
      switchMode("orch");
      const panel = document.getElementById("msgs-orch");
      const div = document.createElement("div");
      div.className = "msg system";
      div.innerHTML = `<div class="bubble">🎉 Все записи проверены! База знаний актуальна.</div>`;
      panel.appendChild(div);
      panel.scrollTop = panel.scrollHeight;
      // Скрыть пустой список
      document.getElementById("review-list").style.display = "none";
    } else {
      // Открыть следующий (или первый если был последний)
      const idx = nextIdx < _reviewItems.length ? nextIdx : 0;
      // Небольшая задержка чтобы закрытие карточки успело отрисоваться
      setTimeout(()=>{
        openReviewItem(idx);
        // Прокрутить список к активному пункту
        const nextLi = document.getElementById("rvli-"+_reviewItems[idx].id);
        if(nextLi) nextLi.scrollIntoView({block:"nearest",behavior:"smooth"});
      }, 120);
    }
  }
}

async function rvVerify(id, btn){
  btn.disabled = true;
  try{
    const j = await fetch("/api/review/verify",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({id})}).then(r=>r.json());
    if(j.ok) _rvAfterAction(id);
    else { btn.textContent="❌ Ошибка"; btn.disabled=false; }
  }catch(e){ btn.textContent="❌"; btn.disabled=false; }
}

function rvEdit(id, btn){
  const el = document.getElementById("rv-answer-"+id);
  el.contentEditable = "true";
  el.style.outline = "2px solid var(--icq-header)";
  el.focus();
  // Курсор в конец
  const range = document.createRange();
  range.selectNodeContents(el);
  range.collapse(false);
  window.getSelection().removeAllRanges();
  window.getSelection().addRange(range);
  btn.style.display = "none";
  document.getElementById("rv-save-"+id).style.display = "";
}

async function rvSave(id, btn){
  const el = document.getElementById("rv-answer-"+id);
  const answer = el.innerText.trim();
  if(!answer){ btn.textContent="❌ пусто"; return; }
  btn.disabled = true;
  try{
    const j = await fetch("/api/review/update",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({id,answer})}).then(r=>r.json());
    if(j.ok) _rvAfterAction(id);
    else { btn.textContent="❌ Ошибка"; btn.disabled=false; }
  }catch(e){ btn.textContent="❌"; btn.disabled=false; }
}

async function rvDelete(id, btn){
  if(!confirm("Удалить запись из базы знаний?")) return;
  btn.disabled = true;
  try{
    const j = await fetch("/api/review/delete",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({id})}).then(r=>r.json());
    if(j.ok) _rvAfterAction(id);
    else { btn.textContent="❌ Ошибка"; btn.disabled=false; }
  }catch(e){ btn.textContent="❌"; btn.disabled=false; }
}

function handleOrchUpd(d){
  const div=document.querySelector(`[data-msg-id="${d.msg_id}"]`);if(!div)return;
  const L={VERIFIED:"✅ Verified",PARTIALLY_VERIFIED:"⚠ Частично",
           UNVERIFIED:"⏳ Pending",REJECTED:"❌ Rejected",HYPOTHESIS:"💭 Гипотеза"};
  if(d.trust_level){
    const b=div.querySelector(".trust-badge");
    if(b){b.textContent=L[d.trust_level]||d.trust_level;b.className="trust-badge trust-"+d.trust_level}
    div.querySelectorAll(".trust-badge").forEach(x=>{if(x.textContent==="предв.")x.remove()});
    // Сохраняем вердикт DeepSeek в dataset для feedback
    div.dataset.dsVerdict=d.trust_level;
    if(d.deepseek_correction)div.dataset.dsCorrection=d.deepseek_correction;
    // Обновляем кнопки лайка: при расхождении — две пары, при согласии — одна
    const mid=div.dataset.msgId;
    const fbRow=div.querySelector(".fb-row");
    if(fbRow&&mid){
      if(d.trust_level==="PARTIALLY_VERIFIED"||d.trust_level==="REJECTED"){
        fbRow.innerHTML=
          `<span style="font-size:11px;opacity:.7">Qwen+web:</span>`+
          `<button class="fb-btn" id="fbweb-${mid}" onclick="sendFB('${mid}','positive',this,'web')">👍</button>`+
          `<button class="fb-btn" id="fwbn-${mid}" onclick="sendFB('${mid}','negative',this,'web')">👎</button>`+
          `<span style="font-size:11px;opacity:.7">DeepSeek:</span>`+
          `<button class="fb-btn" id="fbds-${mid}" onclick="sendFB('${mid}','positive',this,'deepseek')">👍</button>`+
          `<button class="fb-btn" id="fbdsn-${mid}" onclick="sendFB('${mid}','negative',this,'deepseek')">👎</button>`+
          `<button class="fb-btn" id="fbr-${mid}" onclick="rememberAns('${mid}',this)">📌 Запомнить</button>`;
      }
    }
  }
  const bubble=div.querySelector(".bubble");
  if(!bubble)return;
  // Заменить строку "🔄 Отправлен на проверку..." на итог верификации
  if(d.replace_pending){
    bubble.innerHTML=bubble.innerHTML.replace(
      /🔄 Отправлен на проверку[\s\S]*?(?=<br>|$)/,
      "<strong>"+d.replace_pending+"</strong>"
    );
  }
  if(d.update_text){
    const u=document.createElement("div");u.className="orch-update";
    u.innerHTML=renderText(d.update_text);bubble.appendChild(u);
  }
  if(d.tags&&d.tags.length){
    div.dataset.tags=JSON.stringify(d.tags);
    let tl=bubble.querySelector(".orch-tags");
    if(!tl){tl=document.createElement("div");tl.className="orch-tags";bubble.appendChild(tl);}
    tl.innerHTML=d.tags.map(t=>`<span class="orch-tag">#${t}</span>`).join("");
  }
}

// ── Inet broadcast status ─────────────────────────────────────────────────────
const _MNAMES={claude:"Claude",gpt:"GPT",deepseek:"DeepSeek",kimi:"Kimi"};
let _inetPending=new Set();

function handleInetBroadcastStart(d){
  _inetPending=new Set(d.models||[]);
  sendBtn.disabled=true;
  const names=(d.models||[]).map(m=>_MNAMES[m]||m).join(", ");
  sbTurn.textContent=`⏳ Ожидаем: ${names}`;
}

function handleInetModelReplied(d){
  _inetPending.delete(d.model);
  if(_inetPending.size>0){
    const names=[..._inetPending].map(m=>_MNAMES[m]||m).join(", ");
    sbTurn.textContent=`⏳ Ещё ждём: ${names}`;
  } else {
    sbTurn.textContent="⏳ Все ответили. Буфер 180с...";
  }
}

function handleInetReady(){
  _inetPending.clear();
  sendBtn.disabled=false;
  sbTurn.textContent="Ваш ход ✍️";
  inpEl.focus();
}

// ── Bridge/Council ────────────────────────────────────────────────────────────
function applyBridgeState(s){
  Object.assign(bstate,s);
  const bp=document.getElementById("btn-pause");
  if(bp){bp.classList.toggle("paused",s.paused);bp.textContent=s.paused?"▶ Resume":"⏸ Пауза"}
  ["claude","gpt","deepseek","kimi"].forEach(m=>{
    const b=document.getElementById("btn-"+m);
    if(!b)return;
    const blocked=s[m+"_blocked"];
    b.classList.toggle("blocked",blocked);
    const dot=b.querySelector(".bind");
    if(dot)dot.textContent=blocked?"🔴":"🟢";
  });
}

async function togglePause(){
  const r=await fetch(bstate.paused?"/api/council/resume":"/api/council/pause",{method:"POST"});
  applyBridgeState(await r.json());
}

async function toggleBlock(who){
  const key=who+"_blocked";
  const r=await fetch("/api/council/state",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({[key]:!bstate[key]})});
  applyBridgeState(await r.json());
}

// ── Relay timer ───────────────────────────────────────────────────────────────
let relayIv=null;
function showRelayTimer(tot){
  const bar=document.getElementById("relay-bar"),cnt=document.getElementById("relay-count");
  const fill=document.getElementById("relay-fill");
  bar.classList.add("show");let rem=tot;cnt.textContent=rem;fill.style.width="100%";
  if(relayIv)clearInterval(relayIv);
  relayIv=setInterval(()=>{
    rem--;cnt.textContent=rem;
    fill.style.width=(rem/tot*100).toFixed(1)+"%";
    if(rem<=0){clearInterval(relayIv);bar.classList.remove("show")}
  },1000);
}

// ── Sound ─────────────────────────────────────────────────────────────────────
const sndA=new Audio("/media/icq-online.mp3"),sndB=new Audio("/media/icq.mp3");
function toggleSound(){
  soundEnabled=!soundEnabled;
  document.getElementById("btn-sound").textContent=soundEnabled?"🔊":"🔇";
}
function playSound(who){
  if(!soundEnabled)return;
  try{(who==="claude"?sndA:sndB).cloneNode().play()}catch(_){}
}

// ── Controls ──────────────────────────────────────────────────────────────────
async function clearChat(){
  const labels={orch:"Оркестратор",inet:"Интернет чат",local:"YANDI Помощник"};
  const label=labels[currentMode]||currentMode;
  if(!confirm("Очистить историю: "+label+"?"))return;
  if(currentMode==="local"){await clearLocalChat();return;}
  const url="/api/"+currentMode+"/clear";
  const d=await fetch(url,{method:"POST"}).then(r=>r.json());
  if(d.ok) activeMsgsEl().innerHTML="";
  _updateCopyChatBar();
}
async function resetTokens(){await fetch("/api/council/tokens/reset",{method:"POST"})}
async function saveDataset(){
  addMsg({from:"system",text:"⏳ Сохраняю + обогащаю через LLM...",ts:now()});
  try{
    const d=await fetch("/api/council/save_dataset",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({clear:false})}).then(r=>r.json());
    if(d.ok){
      const fname=d.jsonl.split("/").pop();
      const tags=(d.tags||[]).join(", ")||"—";
      const kw=d.kw_written||0;
      addMsg({from:"system",
        text:`💾 ${fname}\n🏷 ${tags}\n📚 +${kw} записей в базу знаний`,ts:now()});
      refreshStats();
    } else {
      addMsg({from:"system",text:"❌ Ошибка сохранения",ts:now()});
    }
  }catch(e){addMsg({from:"system",text:"❌ "+e.message,ts:now()})}
}
async function exportChat(){
  try{
    const d=await fetch("/api/council/export",{method:"POST"}).then(r=>r.json());
    addMsg({from:"system",text:d.status==="exported"?"📁 "+d.path.split("/").pop():"❌ Ошибка",ts:now()});
  }catch(e){addMsg({from:"system",text:"❌ "+e.message,ts:now()})}
}
async function getBootstrap(){return(await fetch("/api/council/bootstrap").then(r=>r.json())).bootstrap||""}
async function copyContext(){
  try{await navigator.clipboard.writeText(await getBootstrap());
    addMsg({from:"system",text:"📋 Контекст скопирован",ts:now()})}
  catch(e){addMsg({from:"system",text:"❌ "+e.message,ts:now()})}
}

// ── Settings ──────────────────────────────────────────────────────────────────
function openSettings(){loadConfig();document.getElementById("settings-modal").classList.add("open")}
function closeSettings(){document.getElementById("settings-modal").classList.remove("open")}
async function loadConfig(){
  try{
    const c=await fetch("/api/council/config").then(r=>r.json());
    document.getElementById("cfg-claude-url").value=c.claude_web_url||"";
    document.getElementById("cfg-gpt-url").value=c.gpt_web_url||"";
    document.getElementById("cfg-deepseek-url").value=c.deepseek_url||"";
    document.getElementById("cfg-kimi-url").value=c.kimi_url||"";
    document.getElementById("cfg-qwen-url").value=c.qwen_url||"";
    document.getElementById("cfg-proxy").value=c.proxy||"";
  }catch(e){}
}
async function saveConfig(){
  const p={claude_web_url:document.getElementById("cfg-claude-url").value.trim(),
           gpt_web_url:document.getElementById("cfg-gpt-url").value.trim(),
           deepseek_url:document.getElementById("cfg-deepseek-url").value.trim(),
           kimi_url:document.getElementById("cfg-kimi-url").value.trim(),
           qwen_url:document.getElementById("cfg-qwen-url").value.trim(),
           proxy:document.getElementById("cfg-proxy").value.trim()};
  const d=await fetch("/api/council/config",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(p)}).then(r=>r.json());
  const st=document.getElementById("cfg-st");
  st.textContent=d.ok?"✅ Сохранено":"❌ Ошибка";
  st.style.color=d.ok?"#006600":"#aa0000";
}

// ── Init ──────────────────────────────────────────────────────────────────────
inpEl.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendMessage()}});
fetch("/api/council/state").then(r=>r.json()).then(applyBridgeState).catch(()=>{});
refreshStats();
checkConnections();
loadReviewQueue();
setInterval(refreshStats,10000);
setInterval(checkConnections,15000);
setInterval(loadReviewQueue,30000);
loadConfig();
switchMode(localStorage.getItem("activeTab")||"orch");
// Restore language selector and apply i18n
(()=>{const s=document.getElementById("lang-sel");if(s)s.value=userLang})();
applyI18n();
</script>
</body>
</html>
"""


# ── council memory helpers ────────────────────────────────────────────────────

def _build_bootstrap(messages: list) -> str:
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    recent = messages[-30:]
    lines = [
        "# PET Council — Bootstrap Context",
        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Project",
        "PET cognitive pipeline. Scenarios: book_qa, research, culinary.",
        "PETCoordinator = control plane (core/pet_coordinator.py).",
        "",
        "## Status",
        "F2: DONE (commit 9e06329) — culinary multi-turn clarification + gated web search.",
        "F3: planned — TBD.",
        "",
        "## Recent Council",
    ]
    for msg in recent:
        who = {"human": "Human", "claude": "Claude", "gpt": "GPT"}.get(
            msg.get("from", "?"), msg.get("from", "?")
        )
        text = (msg.get("text") or "").strip().replace("\n", " ")[:300]
        lines.append(f"[{who}] {text}")
    if cfg.get("claude_web_url"):
        lines += ["", f"## Claude web chat\n{cfg['claude_web_url']}"]
    if cfg.get("gpt_web_url"):
        lines += [f"## GPT web chat\n{cfg['gpt_web_url']}"]
    return "\n".join(lines)


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.websocket("/ws/{client_id}")
async def ws_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    browsers[client_id] = websocket

    r = aioredis.from_url(REDIS_URL, decode_responses=True)

    # send orch history on connect (default tab)
    raw   = await r.lrange(ORCH_MSGS_KEY, 0, 99)
    hist  = [json.loads(m) for m in reversed(raw)]
    turn  = await r.get(TURN_KEY) or "human"
    await websocket.send_json({"type": "history", "tab": "orch", "messages": hist, "turn": turn})

    # mark online + notify others
    await r.set(STATUS_PFX + client_id, "online", ex=300)
    await r.publish(PUBSUB_CH, json.dumps({"type": "status", "who": client_id, "state": "online"}))
    await broadcast({"type": "status", "who": client_id, "state": "online"})

    try:
        while True:
            data = await websocket.receive_json()
            text = data.get("text", "").strip()
            if not text:
                continue

            tab    = data.get("tab", "inet")   # клиент передаёт текущую вкладку
            key    = ORCH_MSGS_KEY if tab == "orch" else INET_MSGS_KEY
            msg_id = str(uuid.uuid4())
            msg = {
                "type":      "message",
                "from":      client_id,
                "text":      text,
                "tab":       tab,
                "ts":        datetime.now().strftime("%H:%M"),
                "_ts":       datetime.now().timestamp(),
                "id":        msg_id,
                "turn_next": "claude",
            }
            await r.lpush(key, json.dumps(msg)); await r.ltrim(key, 0, MAX_MESSAGES - 1)
            await r.set(TURN_KEY, "claude")
            write_log(msg)
            await r.publish(PUBSUB_CH, json.dumps(msg))
            await broadcast(msg)
            # Отправляем ВСЕМ активным моделям одновременно (только вопрос, без цепочки)
            if client_id == "human" and tab == "inet":
                active = _active_models()
                _relay_ctx[msg_id] = {
                    "text":      text,
                    "broadcast": True,
                    "pending":   set(active),
                }
                for model in active:
                    try:
                        _ext_queues[model].put_nowait({"task_id": msg_id, "text": text})
                    except asyncio.QueueFull:
                        pass
                # Сообщаем UI: к каким моделям ушёл вопрос (и блокируем ввод)
                await broadcast({
                    "type":    "inet_broadcast_start",
                    "msg_id":  msg_id,
                    "models":  active,
                    "pending": active,
                })

    except WebSocketDisconnect:
        browsers.pop(client_id, None)
        await r.set(STATUS_PFX + client_id, "offline", ex=60)
        ev = {"type": "status", "who": client_id, "state": "offline"}
        await r.publish(PUBSUB_CH, json.dumps(ev))
        await broadcast(ev)
    finally:
        await r.aclose()


def _is_russian(text: str) -> bool:
    """Быстрая проверка: >= 25% символов — кириллица."""
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return True
    cyrillic = sum(1 for c in alpha if 'Ѐ' <= c <= 'ӿ')
    return (cyrillic / len(alpha)) >= 0.25


def _translate_to_russian(text: str, who: str) -> str:
    """Локальная Ollama переводит ответ совета на русский."""
    import re as _re, requests as _req
    prompt = (
        f"Переведи текст ниже на русский язык. "
        f"Верни ТОЛЬКО перевод — без оригинала, без слов «перевод», «вот», «ответ», без вступлений.\n\n"
        f"Текст:\n{text[:3000]}\n\nПеревод:"
    )
    try:
        s = _req.Session(); s.trust_env = False
        r = s.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": "heretic:q8", "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.1, "num_predict": 1200}},
            timeout=90,
        )
        raw = r.json().get("response", "").strip()
        if not raw:
            return text
        # Срезаем по первому EOS/служебному токену
        for stop in ("<|endoftext|>", "<|im_start|>", "<|im_end|>", "</s>", "<|end|>"):
            if stop in raw:
                raw = raw.split(stop)[0].strip()
        # Убрать строки на латинице (>60% латиница) — остатки оригинала или промпта
        lines = raw.splitlines()
        clean = []
        for line in lines:
            alpha = [c for c in line if c.isalpha()]
            if not alpha:
                clean.append(line); continue
            latin = sum(1 for c in alpha if 'a' <= c.lower() <= 'z')
            if latin / len(alpha) < 0.6:
                clean.append(line)
        result = "\n".join(clean).strip()
        # Дедупликация: убрать повтор если вторая половина = первая
        half = len(result) // 2
        if half > 50 and result[:half].strip() == result[half:].strip():
            result = result[:half].strip()
        return result if len(result) > 20 else text
    except Exception:
        return text


async def _localize(text: str, who: str) -> str:
    """Если ответ не на русском — переводим через локальную модель."""
    if _is_russian(text):
        return text
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _translate_to_russian(text, who))


@app.post("/reply")
async def api_reply(payload: dict):
    """Terminal agents post replies: {"from": "claude"|"gpt", "text": "..."}"""
    who  = payload.get("from", "claude")
    text = payload.get("text", "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}

    text = await _localize(text, who)

    # default flow: human→claude→gpt→deepseek→human
    # pass explicit turn_next in payload to override
    default_map = {"human": "claude", "claude": "gpt", "gpt": "deepseek", "deepseek": "human"}
    turn_next   = payload.get("turn_next") or default_map.get(who, "human")

    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    msg = {
        "type":      "message",
        "from":      who,
        "tab":       "inet",
        "text":      text,
        "ts":        datetime.now().strftime("%H:%M"),
        "_ts":       datetime.now().timestamp(),
        "id":        str(uuid.uuid4()),
        "turn_next": turn_next,
    }
    await r.lpush(INET_MSGS_KEY, json.dumps(msg)); await r.ltrim(INET_MSGS_KEY, 0, MAX_MESSAGES - 1)
    await r.set(TURN_KEY, turn_next)
    await r.set(STATUS_PFX + who, "online", ex=300)
    write_log(msg)
    await r.publish(PUBSUB_CH, json.dumps(msg))
    await r.aclose()
    await broadcast(msg)
    return {"ok": True, "turn_next": turn_next}


@app.post("/status")
async def api_status(payload: dict):
    """Set typing/online status from terminal: {"who": "claude", "state": "typing"}"""
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.set(STATUS_PFX + payload["who"], payload["state"], ex=30)
    ev = {"type": "status", **payload}
    await r.publish(PUBSUB_CH, json.dumps(ev))
    await r.aclose()
    await broadcast(ev)
    return {"ok": True}


@app.delete("/history")
async def clear_history():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.delete(MESSAGES_KEY)
    await r.set(TURN_KEY, "human")
    await r.aclose()
    await broadcast({"type": "history", "messages": [], "turn": "human"})
    return {"ok": True}


@app.post("/api/council/relay")
async def api_relay(payload: dict):
    """Send human message and start relay chain: claude → gpt → deepseek.
    Unlike /api/council/broadcast (all at once), this goes sequentially."""
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    msg_id = str(uuid.uuid4())
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    msg = {
        "type": "message", "from": "human", "tab": "inet", "text": text,
        "ts": datetime.now().strftime("%H:%M"), "_ts": datetime.now().timestamp(),
        "id": msg_id, "turn_next": "claude",
    }
    await r.lpush(INET_MSGS_KEY, json.dumps(msg)); await r.ltrim(INET_MSGS_KEY, 0, MAX_MESSAGES - 1)
    await r.set(TURN_KEY, "claude")
    write_log(msg)
    await r.publish(PUBSUB_CH, json.dumps(msg))
    await r.aclose()
    await broadcast(msg)
    # Start relay chain from first active model
    _relay_ctx[msg_id] = {"text": text, "broadcast": False}
    active = _active_models()
    if active:
        try:
            _ext_queues[active[0]].put_nowait({"task_id": msg_id, "text": text})
        except asyncio.QueueFull:
            pass
    return {"ok": True, "task_id": msg_id, "sent_to": active[0] if active else None}


@app.get("/api/inet/history")
async def inet_history():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lrange(INET_MSGS_KEY, 0, MAX_MESSAGES - 1)
    await r.aclose()
    return {"messages": [json.loads(m) for m in reversed(raw)]}

@app.post("/api/inet/clear")
async def inet_clear():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.delete(INET_MSGS_KEY)
    await r.set(TURN_KEY, "human")
    await r.aclose()
    await broadcast({"type": "history", "tab": "inet", "messages": [], "turn": "human"})
    return {"ok": True}

@app.post("/api/council/clear")
async def api_clear_chat():
    """Очистить оба чата (orch + inet) — legacy endpoint."""
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.delete(ORCH_MSGS_KEY, INET_MSGS_KEY)
    await r.set(TURN_KEY, "human")
    await r.aclose()
    await broadcast({"type": "history", "tab": "orch", "messages": [], "turn": "human"})
    await broadcast({"type": "history", "tab": "inet", "messages": [], "turn": "human"})
    return {"ok": True}


@app.post("/api/council/message/delete")
async def api_delete_message(payload: dict):
    """Удалить одно сообщение из Redis по его id."""
    msg_id = payload.get("id", "")
    if not msg_id:
        return {"ok": False, "error": "id required"}
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lrange(MESSAGES_KEY, 0, MAX_MESSAGES - 1)
    deleted = 0
    for item in raw:
        try:
            obj = json.loads(item)
            if obj.get("id") == msg_id:
                deleted += await r.lrem(MESSAGES_KEY, 1, item)
                break
        except Exception:
            pass
    await r.aclose()
    return {"ok": True, "deleted": deleted}


# ── council memory API ───────────────────────────────────────────────────────

@app.get("/api/council/config")
async def api_get_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {"claude_web_url": "", "gpt_web_url": "", "gmail_to": ""}


@app.post("/api/council/config")
async def api_save_config(payload: dict):
    CONFIG_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return {"ok": True}


@app.get("/api/council/bootstrap")
async def api_bootstrap():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lrange(MESSAGES_KEY, 0, 49)
    await r.aclose()
    messages = [json.loads(m) for m in reversed(raw)]
    return {"bootstrap": _build_bootstrap(messages)}


@app.post("/api/council/export")
async def api_export():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lrange(MESSAGES_KEY, 0, MAX_MESSAGES - 1)
    await r.aclose()
    messages = [json.loads(m) for m in reversed(raw)]

    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    path = REGISTRY_DIR / f"{ts}.md"

    lines = [
        "---",
        "topic: council-chat-export",
        f"date: {datetime.now().strftime('%Y-%m-%d')}",
        "project: pet",
        "status: active",
        "tags: [council, chat, export]",
        "---",
        "",
        f"# Council Chat Export — {ts}",
        "",
    ]
    for msg in messages:
        who = {"human": "Human", "claude": "Claude", "gpt": "GPT"}.get(
            msg.get("from", "?"), msg.get("from", "?")
        )
        lines.append(f"**[{who}] {msg.get('ts', '')}**")
        lines.append(msg.get("text", ""))
        lines.append("")

    content = "\n".join(lines)
    path.write_text(content, encoding="utf-8")
    bootstrap = _build_bootstrap(messages)
    return {"status": "exported", "path": str(path), "bootstrap": bootstrap}


# ── browser extension API ─────────────────────────────────────────────────────

@app.get("/api/ext/poll")
async def ext_poll(model: str = "claude", tab_open: str = "false"):
    """Extension polls this to get the next pending task for its model."""
    # Heartbeat: обновляем только если вкладка реально открыта
    if model in _model_last_seen and tab_open.lower() == "true":
        _model_last_seen[model] = _time.time()
    if _bridge_state["paused"]:
        return {"paused": True}
    q = _ext_queues.get(model)
    if not q:
        return {}
    try:
        task = q.get_nowait()
        return task
    except asyncio.QueueEmpty:
        return {}


@app.post("/api/ext/result")
async def ext_result(payload: dict):
    """Extension posts response here after getting it from web chat."""
    from_who = payload.get("from", "claude")
    text     = (payload.get("text") or "").strip()
    task_id  = payload.get("task_id", "")
    if not text:
        return {"ok": False}

    text = await _localize(text, from_who)

    # Heartbeat: фиксируем время последнего отклика модели
    if from_who in _model_last_seen:
        _model_last_seen[from_who] = _time.time()

    # Обновляем счётчики токенов
    if from_who in _tokens:
        _tokens[from_who]["sent"] += int(payload.get("tokens_sent", 0))
        _tokens[from_who]["recv"] += int(payload.get("tokens_recv", 0))
        await broadcast({"type": "tokens", "tokens": _tokens, "limits": TOKEN_LIMITS})

    # Записываем сообщение — turn_next по RELAY_CHAIN, последний → human
    try:
        _idx = RELAY_CHAIN.index(from_who)
        turn_next = RELAY_CHAIN[_idx + 1] if _idx + 1 < len(RELAY_CHAIN) else "human"
    except ValueError:
        turn_next = "human"
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    msg = {
        "type":      "message",
        "from":      from_who,
        "text":      text,
        "ts":        datetime.now().strftime("%H:%M"),
        "_ts":       datetime.now().timestamp(),
        "id":        str(uuid.uuid4()),
        "turn_next": turn_next,
    }
    await r.lpush(MESSAGES_KEY, json.dumps(msg)); await r.ltrim(MESSAGES_KEY, 0, MAX_MESSAGES - 1)
    await r.set(TURN_KEY, turn_next)
    write_log(msg)
    await r.publish(PUBSUB_CH, json.dumps(msg))
    await r.aclose()
    await broadcast(msg)

    # Relay-цепочка: передаём эстафету следующей АКТИВНОЙ модели
    ctx = _relay_ctx.get(task_id)
    if ctx and not ctx.get("broadcast"):
        ctx[f"{from_who}_resp"] = text
        active     = _active_models()
        next_model = None
        # Найти следующую активную после from_who
        found_current = False
        for m in active:
            if found_current:
                next_model = m
                break
            if m == from_who:
                found_current = True

        if next_model:
            prompt = ctx.get("text", "")
            delay  = random.randint(10, 120)
            await broadcast({"type": "relay_timer", "seconds": delay})
            asyncio.create_task(_queue_after(next_model, task_id, prompt, delay))
        else:
            _relay_ctx.pop(task_id, None)

    elif ctx and ctx.get("broadcast"):
        # Broadcast: снимаем ответившую модель из pending
        pending = ctx.get("pending", set())
        pending.discard(from_who)
        ctx["pending"] = pending
        await broadcast({
            "type":    "inet_model_replied",
            "model":   from_who,
            "pending": list(pending),
        })
        if not pending:
            # Все ответили — сразу синтезируем, потом 180с буфер
            _relay_ctx.pop(task_id, None)
            asyncio.create_task(_inet_collect_responses())
            asyncio.create_task(_inet_ready_after(180))

    return {"ok": True}


@app.post("/api/ext/timer")
async def ext_timer(payload: dict):
    """Extension notifies UI of pending relay countdown."""
    seconds = int(payload.get("seconds", 0))
    await broadcast({"type": "relay_timer", "seconds": seconds})
    return {"ok": True}


# ── Orch AI Validator — изолированный канал (orch:ai:*) ───────────────────────
# Отдельная очередь валидации оркестратора через DeepSeek.
# НЕ пересекается с council-чатом.

@app.get("/api/ext/orch/poll")
async def orch_ai_poll(model: str = "deepseek"):
    """
    Extension polls this endpoint для задач валидации оркестратора.
    Отдельно от /api/ext/poll (council-задачи).
    """
    if model != "deepseek":
        return {}
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lpop("orch:ai:queue")
    await r.aclose()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


@app.post("/api/ext/orch/result")
async def orch_ai_result(payload: dict):
    """
    Extension постит сюда ответ DeepSeek по задаче валидации.
    Результат кладётся в orch:ai:result:{task_id} (TTL 10 мин).
    """
    task_id = payload.get("task_id", "")
    text    = (payload.get("text") or "").strip()
    if not task_id or not text:
        return {"ok": False, "error": "task_id and text required"}

    # Таймаут из расширения — сохраняем как неудачу, не парсим
    if text.startswith("[timeout") or text == "[timeout]":
        result = {
            "task_id":    task_id,
            "verdict":    "UNVERIFIED",
            "correction": "Не удалось получить ответ DeepSeek.",
            "additions":  "",
            "raw":        text,
            "ts":         __import__("time").time(),
        }
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        await r.setex(f"orch:ai:result:{task_id}", 600, json.dumps(result))
        await r.aclose()
        return {"ok": True, "verdict": "UNVERIFIED"}

    try:
        from agent.orch_ai_validator import parse_deepseek_verdict, log_validation
        result = parse_deepseek_verdict(text)
        result["task_id"] = task_id

        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        await r.setex(f"orch:ai:result:{task_id}", 600, json.dumps(result))

        # Лог для датасета (асинхронно)
        meta_raw = payload.get("_meta") or "{}"
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else {}
        except Exception:
            meta = {}

        import threading
        threading.Thread(
            target=log_validation,
            args=(task_id,
                  meta.get("query", ""),
                  meta.get("frame", {}),
                  meta.get("answer", ""),
                  result),
            daemon=True,
        ).start()

        await r.aclose()
        return {"ok": True, "verdict": result["verdict"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/orch/ai/log")
async def orch_ai_log(limit: int = 50):
    """Последние N записей лога валидаций — для просмотра датасета."""
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lrange("orch:ai:log", 0, limit - 1)
    await r.aclose()
    entries = []
    for line in raw:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return {"entries": entries, "count": len(entries)}


# ── bridge state & moderator API ──────────────────────────────────────────────

@app.get("/api/council/state")
async def get_state():
    return _bridge_state.copy()


@app.post("/api/council/state")
async def set_state(payload: dict):
    for key in ("paused", "claude_blocked", "gpt_blocked", "deepseek_blocked", "kimi_blocked"):
        if key in payload:
            _bridge_state[key] = bool(payload[key])
    await broadcast({"type": "bridge_state", **_bridge_state})
    return {"ok": True, **_bridge_state}


@app.post("/api/knowledge/sync")
async def knowledge_sync(payload: dict):
    """Принять верифицированное знание от пира и сохранить локально."""
    import json as _json
    from pathlib import Path as _Path

    peers_file = _Path(__file__).parent.parent / "registry" / "peers.json"
    expected_token = ""
    if peers_file.exists():
        try:
            expected_token = _json.loads(peers_file.read_text())  .get("sync_token", "")
        except Exception:
            pass

    if expected_token and payload.get("token") != expected_token:
        return {"ok": False, "error": "unauthorized"}

    record = payload.get("record")
    if not record or not record.get("id"):
        return {"ok": False, "error": "invalid record"}

    try:
        from agent.orch_knowledge_writer import KW_DIR, _get_db, _index_record
        filename = f"{record['id']}.jsonl"
        path = KW_DIR / filename
        if not path.exists():
            record["_filename"]  = filename
            record["_from_peer"] = True
            path.write_text(_json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            con = _get_db()
            _index_record(con, record)
            con.close()
            print(f"📥 KNOWLEDGE_RECV [{record['id']}] trust={record.get('trust_level','?')}")
        return {"ok": True, "id": record["id"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/knowledge/stats")
async def knowledge_stats():
    """Статистика базы знаний."""
    try:
        from agent.orch_knowledge_writer import get_stats
        return {"ok": True, **get_stats()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/knowledge/list")
async def knowledge_list(trust: str = "VERIFIED", limit: int = 50):
    """Список записей по уровню доверия."""
    try:
        from agent.orch_knowledge_writer import get_by_trust
        return {"ok": True, "records": get_by_trust(trust, limit=limit)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/yandi/validate")
async def yandi_validate(payload: dict):
    """
    YANDI validation endpoint — принимает вопрос+ответ, валидирует через браузерные модели.
    Возвращает вердикт: agree | disagree | partial.
    """
    import uuid as _uuid
    question = (payload.get("question") or "").strip()
    answer   = (payload.get("answer")   or "").strip()
    if not question or not answer:
        return {"ok": False, "error": "question and answer required"}

    prompt = (
        "Ты верификатор ответов. Проверь правильность ответа на вопрос.\n\n"
        "Верни ТОЛЬКО валидный JSON:\n"
        "{\"verdict\": \"agree|disagree|partial\", \"reason\": \"краткое обоснование\"}\n\n"
        f"Вопрос: {question[:500]}\n\nОтвет для проверки:\n{answer[:1500]}"
    )

    # Отправить в relay-цепочку (браузерные модели)
    active = _active_models()
    if not active:
        return {"ok": False, "verdict": "partial", "reason": "нет активных браузерных моделей"}

    model  = active[0]
    task_id = str(_uuid.uuid4())
    await _queue_after(model, task_id, prompt, 0)

    # Ждём ответ до 60 секунд
    import asyncio as _asyncio
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    for _ in range(60):
        raw = await r.get(f"council:relay:result:{task_id}")
        if raw:
            await r.aclose()
            import json as _json, re as _re
            raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
            try:
                data = _json.loads(raw)
                v = data.get("verdict", "partial")
                if v not in ("agree", "disagree", "partial"):
                    v = "partial"
                return {"ok": True, "verdict": v, "reason": data.get("reason", "")}
            except Exception:
                return {"ok": True, "verdict": "partial", "reason": raw[:200]}
        await _asyncio.sleep(1)

    await r.aclose()
    return {"ok": False, "verdict": "partial", "reason": "timeout"}


@app.post("/api/council/pause")
async def pause_bridge():
    _bridge_state["paused"] = True
    await broadcast({"type": "bridge_state", **_bridge_state})
    return {"ok": True, "paused": True}


@app.post("/api/council/resume")
async def resume_bridge():
    _bridge_state["paused"] = False
    await broadcast({"type": "bridge_state", **_bridge_state})
    return {"ok": True, "paused": False}


@app.get("/api/council/tokens")
async def get_tokens():
    return {"tokens": _tokens, "limits": TOKEN_LIMITS}


@app.post("/api/council/tokens/reset")
async def reset_tokens():
    for k in _tokens:
        _tokens[k] = {"sent": 0, "recv": 0}
    await broadcast({"type": "tokens", "tokens": _tokens, "limits": TOKEN_LIMITS})
    return {"ok": True}


@app.get("/api/council/connections")
async def council_connections():
    """Heartbeat status: was each model's extension seen in last 90 sec?"""
    now = _time.time()
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    result = {}
    for model in ("claude", "gpt", "deepseek", "kimi"):
        last = _model_last_seen.get(model, 0.0)
        age  = now - last if last else None
        result[model] = {
            "connected": bool(last and age < 90),
            "last_seen_sec": round(age) if age is not None else None,
            "url": cfg.get(f"{model}_web_url") or cfg.get(f"{model}_url") or "",
        }
    return result


@app.post("/api/council/connections/ping")
async def council_ping(payload: dict):
    """Manual heartbeat bump from UI (user clicked 'check')."""
    model = payload.get("model", "")
    if model in _model_last_seen:
        # We can't actually ping the browser tab, but we return current state
        pass
    return await council_connections()


@app.post("/api/council/broadcast")
async def council_broadcast(payload: dict):
    """Moderator sends same prompt to all AIs simultaneously (no relay)."""
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False}
    msg_id = str(uuid.uuid4())
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    msg = {
        "type": "message", "from": "human", "tab": "inet", "text": text,
        "ts": datetime.now().strftime("%H:%M"), "_ts": datetime.now().timestamp(),
        "id": msg_id, "turn_next": "claude",
    }
    await r.lpush(INET_MSGS_KEY, json.dumps(msg)); await r.ltrim(INET_MSGS_KEY, 0, MAX_MESSAGES - 1)
    await r.set(TURN_KEY, "claude")
    write_log(msg)
    await r.publish(PUBSUB_CH, json.dumps(msg))
    await r.aclose()
    await broadcast(msg)
    active = _active_models()
    _relay_ctx[msg_id] = {"text": text, "broadcast": True, "pending": set(active)}
    for model in active:
        try:
            _ext_queues[model].put_nowait({"task_id": msg_id, "text": text})
        except asyncio.QueueFull:
            pass
    return {"ok": True, "task_id": msg_id, "sent_to": active}


# ── LLM helpers для датасетов и перевода ─────────────────────────────────────

_OLLAMA_URL = "http://127.0.0.1:11434"
_OLLAMA_MOD = "heretic:q8"
_KW_DIR     = _HERE.parent / "registry" / "verified_knowledge"
_KW_FILE    = _KW_DIR / "knowledge.jsonl"


def _ollama_mini(prompt: str, max_tokens: int = 60) -> str:
    import requests as _req
    try:
        s = _req.Session(); s.trust_env = False
        r = s.post(f"{_OLLAMA_URL}/api/generate",
                   json={"model": _OLLAMA_MOD, "prompt": prompt, "stream": False,
                         "options": {"temperature": 0.1, "num_predict": max_tokens}},
                   timeout=60)
        raw = r.json().get("response", "").strip()
        import re
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        for stop in ("<|endoftext|>", "<|im_start|>", "<|im_end|>", "</s>"):
            raw = raw.split(stop)[0]
        return raw.strip()
    except Exception:
        return ""

def _gen_slug(question: str) -> str:
    """Translate question to English 3-5 word lowercase-dash slug."""
    prompt = (
        "Translate to English, make a 3-5 word lowercase dash-separated slug. "
        "Output ONLY the slug, nothing else.\n"
        f"Input: {question[:200]}\nSlug:"
    )
    raw = _ollama_mini(prompt, max_tokens=20)
    import re
    slug = re.sub(r"[^a-z0-9-]", "-", raw.lower().strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:60] if slug else "council-session"

def _gen_tags(question: str) -> list[str]:
    """Generate 3-5 English domain:subcategory tags."""
    prompt = (
        "Classify the topic. Output 3-5 English tags as comma-separated domain:subcategory pairs. "
        "One tag may be 'noise:flood' if it's casual/trivial. Output ONLY tags.\n"
        f"Question: {question[:300]}\nTags:"
    )
    raw = _ollama_mini(prompt, max_tokens=50)
    import re
    tags = [t.strip().lower() for t in raw.split(",") if ":" in t.strip()]
    return tags[:5] if tags else ["general:unknown"]

def _gen_en_summary(question: str, answers: dict[str, str]) -> str:
    """Generate a brief English summary of all model answers (multilingual bridge)."""
    ctx = "\n".join(f"[{m}]: {a[:250]}" for m, a in answers.items() if a)
    if not ctx:
        return ""
    prompt = (
        f"Question: {question[:150]}\n"
        f"AI answers:\n{ctx}\n"
        "Write a 1-2 sentence English summary of the key points:"
    )
    raw = _ollama_mini(prompt, max_tokens=100)
    # strip loops
    import re
    raw = re.sub(r"(Summary:|Answer:|Key points:)", "", raw, flags=re.IGNORECASE).strip()
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    seen, out = set(), []
    for l in lines:
        if l not in seen:
            seen.add(l); out.append(l)
    return " ".join(out[:3])

def _write_knowledge(question: str, answer: str, tags: list[str],
                     title_en: str, answer_en: str, source: str,
                     meta: dict | None = None) -> None:
    """Append one Q&A record to verified_knowledge."""
    import time as _t
    _KW_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "question":    question,
        "answer":      answer,
        "trust_level": "HYPOTHESIS",
        "verdict":     "COUNCIL_CONSENSUS",
        "topic":       tags[0].split(":")[0] if tags else "general",
        "tags":        tags,
        "title_en":    title_en,
        "answer_en":   answer_en,
        "source":      source,
        "ts":          _t.time(),
        "ts_iso":      _t.strftime("%Y-%m-%dT%H:%M:%S"),
        **(meta or {}),
    }
    with open(_KW_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# Language codes → display names (used by UI selector)
LANG_NAMES = {
    "auto": "Авто", "ru": "Русский", "en": "English",
    "zh": "中文", "de": "Deutsch", "fr": "Français",
    "es": "Español", "ro": "Română", "uk": "Українська",
    "ja": "日本語", "ko": "한국어", "ar": "العربية",
    "pl": "Polski", "tr": "Türkçe",
}

LANG_FULL = {
    "ru": "Russian", "en": "English", "zh": "Chinese",
    "de": "German",  "fr": "French",  "es": "Spanish",
    "ro": "Romanian","uk": "Ukrainian","ja": "Japanese",
    "ko": "Korean",  "ar": "Arabic",  "pl": "Polish",
    "tr": "Turkish",
}

def _detect_lang_name(text: str) -> str:
    """Определить язык текста — вернуть полное название на английском (любой язык)."""
    prompt = (
        "What language is the following text written in? "
        "Reply with ONLY the language name in English (e.g. Russian, Chinese, Nanai, French, Arabic). "
        "One word or short phrase, nothing else.\n"
        "Text: " + text[:300] + "\nLanguage:"
    )
    raw = _ollama_mini(prompt, max_tokens=8).strip()
    import re
    # Оставить только первое слово/фразу — убрать лишнее
    raw = re.sub(r"[^a-zA-Z\s\-]", "", raw).strip()
    raw = " ".join(raw.split()[:3])  # не более 3 слов
    return raw if raw else "English"

def _detect_lang(text: str) -> str:
    """Обратная совместимость — возвращает 2-буквенный код если возможно."""
    name = _detect_lang_name(text).lower()
    _name_to_code = {
        "russian": "ru", "english": "en", "chinese": "zh", "german": "de",
        "french": "fr", "spanish": "es", "romanian": "ro", "ukrainian": "uk",
        "japanese": "ja", "korean": "ko", "arabic": "ar", "polish": "pl", "turkish": "tr",
    }
    return _name_to_code.get(name, name[:2])

def _translate(text: str, target_lang_name: str) -> str:
    """Перевести текст на язык target_lang_name (полное название, любой язык)."""
    prompt = (
        f"Translate the following text to {target_lang_name}. "
        "Output ONLY the translation, no explanations, no prefix.\n"
        "Text:\n" + text[:2000] + "\nTranslation:"
    )
    raw = _ollama_mini(prompt, max_tokens=800)
    import re
    paras = raw.split("\n\n")
    seen, out = set(), []
    for p in paras:
        ps = p.strip()
        if ps and ps not in seen:
            seen.add(ps); out.append(ps)
    return "\n\n".join(out).strip()


@app.post("/api/council/save_dataset")
async def save_dataset(payload: dict):
    """Save current chat to registry/council/ as JSONL + MD, then optionally clear.
    Also enriches with LLM tags, English slug, and writes Q&A to verified_knowledge."""
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lrange(MESSAGES_KEY, 0, MAX_MESSAGES - 1)
    await r.aclose()
    messages = [json.loads(m) for m in reversed(raw)]

    # ── Extract Q&A pairs ──────────────────────────────────────────────────────
    human_msgs   = [m for m in messages if m.get("from") == "human"]
    model_msgs   = {m: [x for x in messages if x.get("from") == m]
                    for m in ("claude", "gpt", "deepseek")}
    first_q      = human_msgs[0].get("text", "") if human_msgs else ""

    # ── LLM enrichment (sync, runs in threadpool implicitly via await) ─────────
    import asyncio as _aio
    loop         = _aio.get_event_loop()
    slug         = await loop.run_in_executor(None, _gen_slug, first_q) if first_q else "council-session"
    tags         = await loop.run_in_executor(None, _gen_tags, first_q) if first_q else ["general:unknown"]
    answers_map  = {m: (model_msgs[m][0].get("text","") if model_msgs[m] else "") for m in ("claude","gpt","deepseek")}
    answer_en    = await loop.run_in_executor(None, _gen_en_summary, first_q, answers_map) if first_q else ""
    lang_orig    = await loop.run_in_executor(None, _detect_lang, first_q) if first_q else "en"
    # Dataset: original lang + English only — no extra user-lang translation

    # ── Filenames ──────────────────────────────────────────────────────────────
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    ts           = datetime.now().strftime("%Y-%m-%d_%H-%M")
    file_slug    = slug or "council-session"
    jsonl_path   = REGISTRY_DIR / f"{ts}_{file_slug}.jsonl"
    md_path      = REGISTRY_DIR / f"{ts}_{file_slug}.md"

    # ── JSONL ──────────────────────────────────────────────────────────────────
    with jsonl_path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        f.write(json.dumps({
            "stage": "final", "ts": ts, "slug": file_slug,
            "tags": tags, "title_en": slug,
            "lang_orig": lang_orig, "answer_en": answer_en,
            "message_count": len(messages),
            "tokens": {k: dict(v) for k, v in _tokens.items()},
        }, ensure_ascii=False) + "\n")

    # ── Markdown ───────────────────────────────────────────────────────────────
    lines = [
        "---",
        f"title_en: {slug}",
        f"date: {datetime.now().strftime('%Y-%m-%d')}",
        f"tags: [{', '.join(tags)}]",
        "source: council", "---", "",
        f"# {slug.replace('-',' ').title()} — {ts}", "",
        f"> **EN summary:** {answer_en}", "",
    ]
    for msg in messages:
        who = {"human": "Human", "claude": "Claude", "gpt": "GPT",
               "deepseek": "DeepSeek"}.get(msg.get("from", "?"), msg.get("from", "?"))
        lines += [f"**[{who}] {msg.get('ts','')}**", msg.get("text",""), ""]
    lines += ["---", "## Token usage"]
    for model, counts in _tokens.items():
        lines.append(f"- {model}: ↑{counts['sent']} ↓{counts['recv']} tokens")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    # ── Write Q&A pairs to verified_knowledge (multilingual bridge) ────────────
    kw_written = 0
    if first_q and any(answers_map.values()):
        for model, ans in answers_map.items():
            if ans:
                _write_knowledge(
                    question=first_q, answer=ans,
                    tags=tags, title_en=slug, answer_en=answer_en,
                    source=f"council:{model}",
                    meta={"lang_orig": lang_orig},
                )
                kw_written += 1

    # ── Clear if requested ─────────────────────────────────────────────────────
    if payload.get("clear"):
        r2 = aioredis.from_url(REDIS_URL, decode_responses=True)
        await r2.delete(MESSAGES_KEY)
        await r2.set(TURN_KEY, "human")
        await r2.aclose()
        for k in _tokens:
            _tokens[k] = {"sent": 0, "recv": 0}
        await broadcast({"type": "history", "messages": [], "turn": "human"})
        await broadcast({"type": "tokens", "tokens": _tokens, "limits": TOKEN_LIMITS})

    return {"ok": True, "jsonl": str(jsonl_path), "md": str(md_path),
            "slug": file_slug, "tags": tags, "kw_written": kw_written,
            "messages": len(messages)}


@app.post("/api/council/inject")
async def inject_as_human(payload: dict):
    """Moderator (Claude Code) injects a message as Human → queued for extension."""
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    msg_id = str(uuid.uuid4())
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    msg = {
        "type":      "message",
        "from":      "human",
        "text":      text,
        "ts":        datetime.now().strftime("%H:%M"),
        "_ts":       datetime.now().timestamp(),
        "id":        msg_id,
        "turn_next": "claude",
    }
    await r.lpush(MESSAGES_KEY, json.dumps(msg)); await r.ltrim(MESSAGES_KEY, 0, MAX_MESSAGES - 1)
    await r.set(TURN_KEY, "claude")
    write_log(msg)
    await r.publish(PUBSUB_CH, json.dumps(msg))
    await r.aclose()
    await broadcast(msg)
    _relay_ctx[msg_id] = {"text": text, "broadcast": False}
    for model in RELAY_CHAIN:
        if not _bridge_state.get(f"{model}_blocked"):
            try:
                _ext_queues[model].put_nowait({"task_id": msg_id, "text": text})
            except asyncio.QueueFull:
                pass
            break
    return {"ok": True, "task_id": msg_id}


# ── pub/sub → terminals (council_chat_listen.py connects to Redis directly) ───
# No broadcaster needed here — terminals subscribe to Redis pub/sub themselves.


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="YANDI PET — AI council chat server")
    parser.add_argument("--port",            type=int, default=9010,      help="HTTP port (default: 9010)")
    parser.add_argument("--host",            default="0.0.0.0",            help="Bind host")
    parser.add_argument("--allow-path",      action="append", dest="allow_paths", metavar="PATH",
                        help="Разрешить доступ tools к дополнительной папке (можно несколько)")
    parser.add_argument("--allow-shell-full", action="store_true",         help="Снять ограничения sandbox для shell")
    parser.add_argument("--allow-net",        action="store_true",         help="Разрешить curl/wget в shell")
    args = parser.parse_args()

    # Передаём permissions в tool_fs и tool_shell через env
    import os as _os
    if args.allow_paths:
        _os.environ["AGENT_ALLOW_PATHS"] = ":".join(args.allow_paths)
        print(f"[tools] Дополнительные пути: {args.allow_paths}")
    if args.allow_shell_full:
        _os.environ["AGENT_SHELL_FULL"] = "1"
        print("[tools] Shell: полный доступ")
    if args.allow_net:
        _os.environ["AGENT_SHELL_NET"] = "1"
        print("[tools] Shell: сеть разрешена")

    print(f"[PET] Запуск на http://{args.host}:{args.port}")
    uvicorn.run(
        app,
        host=args.host, port=args.port,
        reload=False, log_level="warning",
    )
