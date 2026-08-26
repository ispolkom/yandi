#!/usr/bin/env python3
"""
Autonomous GPT bridge for Council Chat.

Listens to Redis chat events. When the turn is GPT, it runs Codex CLI
non-interactively with recent chat context and posts the reply back to the chat.

Usage:
  python3 council/council_gpt_auto.py
  python3 council/council_gpt_auto.py --once
  python3 council/council_gpt_auto.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import redis
import requests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from assistant.local_http import local_post

ROOT = Path(__file__).resolve().parent.parent
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
PUBSUB_CH = "council:chat:pubsub"
MESSAGES_KEY = "council:chat:messages"
TURN_KEY = "council:chat:turn"
STATUS_URL = "http://127.0.0.1:9010/status"
REPLY_URL = "http://127.0.0.1:9010/reply"
STATE_FILE = Path("/tmp/council_gpt_auto_last_id")
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_TIMEOUT = int(os.environ.get("COUNCIL_GPT_CODEX_TIMEOUT", "240"))


SYSTEM_PROMPT = """You are GPT in a three-way local Council Chat with a human and Claude.

Rules:
- Answer as GPT only.
- Keep replies concise and directly useful.
- Do not describe internal tooling unless asked.
- If Claude asks for architectural feedback, give concrete engineering judgment.
- Do not ask the human to paste things between terminals.
- Return only the chat message text. No markdown preamble, no role label.
"""


def redis_client() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def recent_messages(r: redis.Redis, limit: int = 20) -> list[dict]:
    raw = r.lrange(MESSAGES_KEY, 0, limit - 1)
    msgs = [json.loads(item) for item in reversed(raw)]
    return msgs


def latest_message_id(msgs: list[dict]) -> str:
    if not msgs:
        return ""
    return str(msgs[-1].get("id") or msgs[-1].get("_ts") or "")


def read_last_seen() -> str:
    try:
        return STATE_FILE.read_text().strip()
    except FileNotFoundError:
        return ""


def write_last_seen(msg_id: str) -> None:
    STATE_FILE.write_text(msg_id)


def format_context(msgs: list[dict]) -> str:
    lines = []
    for msg in msgs:
        who = msg.get("from", "?")
        ts = msg.get("ts", "")
        text = msg.get("text", "")
        lines.append(f"[{ts}] {who}: {text}")
    return "\n".join(lines)


def post_status(state: str) -> None:
    try:
        local_post(STATUS_URL, json={"who": "gpt", "state": state}, timeout=5)
    except Exception as exc:
        print(f"[auto] status post failed: {exc}", file=sys.stderr)


def post_reply(text: str) -> None:
    response = local_post(REPLY_URL, json={"from": "gpt", "text": text}, timeout=20)
    response.raise_for_status()
    data = response.json()
    print(f"[auto] sent reply, next turn: {data.get('turn_next')}")


def run_codex(msgs: list[dict]) -> str:
    context = format_context(msgs)
    prompt = f"""{SYSTEM_PROMPT}

Recent Council Chat:
{context}

It is GPT's turn. Write the next GPT chat reply.
"""

    with tempfile.NamedTemporaryFile("w+", prefix="council_gpt_reply_", suffix=".txt", delete=False) as out:
        output_path = out.name

    cmd = [
        CODEX_BIN,
        "exec",
        "--cd",
        str(ROOT),
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-last-message",
        output_path,
        prompt,
    ]
    print("[auto] running codex exec...")
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=CODEX_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "codex exec failed\n"
            f"stdout:\n{result.stdout[-2000:]}\n"
            f"stderr:\n{result.stderr[-2000:]}"
        )

    reply = Path(output_path).read_text().strip()
    Path(output_path).unlink(missing_ok=True)
    if not reply:
        raise RuntimeError("codex exec produced empty reply")
    return reply


def should_answer(r: redis.Redis, msgs: list[dict]) -> bool:
    if r.get(TURN_KEY) != "gpt":
        return False
    if not msgs:
        return False
    if msgs[-1].get("from") == "gpt":
        return False
    return latest_message_id(msgs) != read_last_seen()


def handle_turn(args: argparse.Namespace) -> bool:
    r = redis_client()
    msgs = recent_messages(r, args.context)
    if not should_answer(r, msgs):
        return False

    msg_id = latest_message_id(msgs)
    print(f"[auto] GPT turn detected, latest={msg_id}")

    if args.dry_run:
        print(format_context(msgs))
        return True

    write_last_seen(msg_id)
    post_status("typing")
    try:
        reply = run_codex(msgs)
        post_reply(reply)
    finally:
        post_status("online")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Codex bridge for Council Chat")
    parser.add_argument("--once", action="store_true", help="Check once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print context, do not call Codex or reply")
    parser.add_argument("--context", type=int, default=20, help="Recent messages to include")
    args = parser.parse_args()

    r = redis_client()
    post_status("online")
    print("[auto] GPT auto bridge online")

    if args.once:
        handled = handle_turn(args)
        print(f"[auto] handled={handled}")
        return

    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(PUBSUB_CH)

    while True:
        try:
            handle_turn(args)
            message = pubsub.get_message(timeout=10)
            if message:
                handle_turn(args)
        except KeyboardInterrupt:
            print("\n[auto] stopped")
            post_status("offline")
            return
        except Exception as exc:
            print(f"[auto] error: {exc}", file=sys.stderr)
            post_status("online")
            time.sleep(5)


if __name__ == "__main__":
    main()
