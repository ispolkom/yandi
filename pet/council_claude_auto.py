#!/usr/bin/env python3
"""
Autonomous Claude bridge for Council Chat.

Listens to Redis. When turn == "claude", calls claude CLI non-interactively
with recent chat context and posts the reply back via council API.

Usage:
  python3 council/council_claude_auto.py
  python3 council/council_claude_auto.py --once
  python3 council/council_claude_auto.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import redis
import requests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.local_http import local_post

ROOT       = Path(__file__).resolve().parent.parent
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
PUBSUB_CH  = "council:chat:pubsub"
MSGS_KEY   = "council:chat:messages"
TURN_KEY   = "council:chat:turn"
REPLY_URL  = "http://127.0.0.1:9010/reply"
STATUS_URL = "http://127.0.0.1:9010/status"
LAST_ID_KEY = "council:bridge:claude:last_id"
PID_KEY     = "council:bridge:claude:pid"
CLAUDE_BIN  = os.environ.get("CLAUDE_BIN", "claude")
TIMEOUT     = int(os.environ.get("COUNCIL_CLAUDE_TIMEOUT", "180"))

SYSTEM_PROMPT = """\
You are Claude in a three-way local Council Chat with a human and GPT.

Rules:
- Answer as Claude only. Be direct and concise.
- If GPT raised an architectural point, respond with concrete engineering position.
- If the human asks a question, answer it clearly.
- Do not describe internal tooling unless asked.
- Do not ask the human to paste things between terminals.
- Return only the chat message text. No markdown preamble, no role label.
- You have access to the project registry at <project_root>/registry/
"""


def rc() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def recent_messages(r: redis.Redis, limit: int = 20) -> list[dict]:
    raw = r.lrange(MSGS_KEY, 0, limit - 1)
    return [json.loads(x) for x in reversed(raw)]


def latest_id(msgs: list[dict]) -> str:
    if not msgs:
        return ""
    m = msgs[-1]
    return str(m.get("id") or m.get("_ts") or "")


def format_context(msgs: list[dict]) -> str:
    names = {"human": "Human", "claude": "Claude", "gpt": "GPT"}
    lines = []
    for m in msgs:
        who  = names.get(m.get("from", "?"), m.get("from", "?"))
        ts   = m.get("ts", "")
        text = m.get("text", "")
        lines.append(f"[{ts}] {who}: {text}")
    return "\n".join(lines)


def post_status(state: str) -> None:
    try:
        local_post(STATUS_URL, json={"who": "claude", "state": state}, timeout=5)
    except Exception as e:
        print(f"[auto] status error: {e}", file=sys.stderr)


def post_reply(text: str) -> str:
    resp = local_post(REPLY_URL, json={"from": "claude", "text": text}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    turn_next = data.get("turn_next", "?")
    print(f"[auto] sent → turn:{turn_next}")
    return turn_next


_OLLAMA_URL = "http://127.0.0.1:11434"
_FILTER_MODEL = os.environ.get("COUNCIL_FILTER_MODEL", "heretic:q8")


def _filter_reply(raw: str) -> str:
    """
    Локальная модель (Ollama) убирает мета-нарративы из ответа Claude CLI.
    Примеры мусора: 'Claude responded:', 'I'll write Claude's reply:', 'As an AI...'
    """
    import re

    # Быстрый regex-стрип — детерминировано убирает самые частые префиксы
    cleaned = re.sub(
        r"^(Claude\s+(responded|says|said|replies|writes|here|:\s*)[:\s]*)+",
        "", raw, flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"^(I('ll| will) write (Claude('s)? )?(next )?reply:?\s*)+",
        "", cleaned, flags=re.IGNORECASE,
    ).strip()
    # Если первая строка — только метка роли типа "Claude:" — срезаем
    cleaned = re.sub(r"^Claude:\s*", "", cleaned, flags=re.IGNORECASE).strip()

    # Если после regex-чистки всё ещё похоже на мета-нарратив — отправляем в Ollama
    meta_signals = ("as an ai", "i'll respond as", "here is my response", "my response:")
    first_line = cleaned.split("\n")[0].lower()
    if any(s in first_line for s in meta_signals):
        try:
            s = requests.Session()
            s.trust_env = False
            prompt = (
                "Ниже — ответ участника совета. Убери любые мета-нарративы "
                "('As an AI', 'I'll write', 'Claude responded:' и подобные), "
                "дублирующие вводные фразы и служебные метки. "
                "Верни только чистый текст ответа без изменений смысла.\n\n"
                f"Текст:\n{cleaned[:2000]}\n\nЧистый текст:"
            )
            r = s.post(
                f"{_OLLAMA_URL}/api/generate",
                json={
                    "model": _FILTER_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": 600},
                },
                timeout=30,
            )
            result_text = r.json().get("response", "").strip()
            if result_text:
                cleaned = result_text
        except Exception as e:
            print(f"[filter] ollama filter failed: {e}", file=sys.stderr)

    return cleaned or raw


def run_claude(msgs: list[dict]) -> str:
    context = format_context(msgs)
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Recent Council Chat:\n{context}\n\n"
        "It is Claude's turn. Write the next Claude chat reply."
    )

    print("[auto] calling claude CLI...")
    result = subprocess.run(
        [CLAUDE_BIN, "--print", "--output-format", "text", prompt],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=TIMEOUT,
        env={**os.environ, "NO_PROXY": "localhost,127.0.0.1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (rc={result.returncode})\n"
            f"stderr: {result.stderr[-1000:]}"
        )
    reply = result.stdout.strip()
    if not reply:
        raise RuntimeError("claude CLI returned empty reply")
    return _filter_reply(reply)


def should_answer(r: redis.Redis, msgs: list[dict]) -> bool:
    if r.get(TURN_KEY) != "claude":
        return False
    if not msgs:
        return False
    if msgs[-1].get("from") == "claude":
        return False
    lid = latest_id(msgs)
    if lid and lid == r.get(LAST_ID_KEY):
        return False   # already answered this event
    return True


def handle_turn(args: argparse.Namespace) -> bool:
    r = rc()
    msgs = recent_messages(r, args.context)
    if not should_answer(r, msgs):
        return False

    lid = latest_id(msgs)
    print(f"[auto] Claude turn detected, event={lid}")

    if args.dry_run:
        print(format_context(msgs))
        return True

    # Mark as being processed (prevent double-answer on restart)
    r.set(LAST_ID_KEY, lid, ex=3600)
    post_status("typing")

    try:
        reply = run_claude(msgs)
        post_reply(reply)
    except Exception as e:
        print(f"[auto] error: {e}", file=sys.stderr)
        post_status("online")
        return False
    finally:
        post_status("online")

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Claude bridge for Council Chat")
    parser.add_argument("--once",    action="store_true", help="Handle one turn and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print context only")
    parser.add_argument("--context", type=int, default=20, help="Messages to include")
    args = parser.parse_args()

    r = rc()

    # Detect double start
    existing_pid = r.get(PID_KEY)
    if existing_pid and existing_pid != str(os.getpid()):
        try:
            os.kill(int(existing_pid), 0)
            print(f"[auto] already running (PID {existing_pid}), exiting")
            sys.exit(1)
        except OSError:
            pass
    r.set(PID_KEY, os.getpid(), ex=3600)

    post_status("online")
    print("[auto] Claude auto bridge online")
    print(f"[auto] claude bin: {CLAUDE_BIN}")

    if args.once:
        handled = handle_turn(args)
        print(f"[auto] handled={handled}")
        r.delete(PID_KEY)
        return

    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(PUBSUB_CH)

    try:
        while True:
            handle_turn(args)
            msg = pubsub.get_message(timeout=10)
            if msg:
                handle_turn(args)
    except KeyboardInterrupt:
        print("\n[auto] stopped")
    finally:
        r.delete(PID_KEY)
        post_status("offline")


if __name__ == "__main__":
    main()
