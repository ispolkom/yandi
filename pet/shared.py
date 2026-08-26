"""
shared.py — общие константы, состояние и утилиты для всех чат-модулей.
Импортируется из chat_local, chat_translate, chat_orch, chat_agent.
"""
import json
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import WebSocket

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL       = "redis://127.0.0.1:6379"
PUBSUB_CH       = "council:chat:pubsub"
ORCH_MSGS_KEY   = "council:orch:messages"
INET_MSGS_KEY   = "council:inet:messages"
MESSAGES_KEY    = INET_MSGS_KEY          # backward compat
LOCAL_MSGS_KEY  = "council:local:messages"
AGENT_LOG_KEY   = "council:agent:log"
AGENT_STATE_KEY = "council:agent:state"
MAX_MESSAGES    = 300
LOG_FILE        = "/tmp/council_chat.log"

# ── Пути ─────────────────────────────────────────────────────────────────────
_HERE        = Path(__file__).parent
CONFIG_FILE  = _HERE / "council_config.json"
REGISTRY_DIR = _HERE.parent / "registry" / "council"

# ── Ollama (для переводчика и _gen_* утилит) ──────────────────────────────────
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MOD = "heretic:q8"

# ── Интернет-чат модели ───────────────────────────────────────────────────────
RELAY_CHAIN   = ["claude", "gpt", "deepseek", "kimi"]
MODEL_DISPLAY = {"claude": "Claude", "gpt": "GPT", "deepseek": "DeepSeek",
                 "kimi": "Kimi"}
MODELS_URLS   = {"claude": "https://claude.ai", "gpt": "https://chatgpt.com",
                 "deepseek": "https://chat.deepseek.com",
                 "kimi": "https://www.kimi.com"}

# ── Языки ────────────────────────────────────────────────────────────────────
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
    "ro": "Romanian", "uk": "Ukrainian", "ja": "Japanese",
    "ko": "Korean",  "ar": "Arabic",  "pl": "Polish", "tr": "Turkish",
}

# ── Общее изменяемое состояние ────────────────────────────────────────────────
browsers: dict[str, WebSocket] = {}      # client_id → websocket

_model_last_seen: dict[str, float] = {
    "claude": 0.0, "gpt": 0.0, "deepseek": 0.0, "kimi": 0.0,
}

_bridge_state: dict = {
    "paused":           False,
    "claude_blocked":   False,
    "gpt_blocked":      False,
    "deepseek_blocked": False,
    "kimi_blocked":     False,
}

_tokens: dict = {
    m: {"sent": 0, "recv": 0}
    for m in ("claude", "gpt", "deepseek", "kimi")
}
TOKEN_LIMITS = {m: 900000 for m in ("claude", "gpt", "deepseek", "kimi")}
TOKEN_WARN   = 600000


# ── Утилиты ───────────────────────────────────────────────────────────────────

def write_log(msg: dict):
    name = {"human": "Ты", "claude": "Claude", "gpt": "GPT"}.get(
        msg.get("from", "?"), msg.get("from", "?"))
    line = f"[{name}] {msg.get('ts','')}: {msg.get('text','')}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


async def broadcast(data: dict):
    dead = []
    for cid, ws in list(browsers.items()):
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(cid)
    for cid in dead:
        browsers.pop(cid, None)
