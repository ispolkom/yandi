#!/usr/bin/env python3
"""
assistant/council_watcher.py — помощник, следит за ответами моделей совета.

Подписывается на council:chat:pubsub, считает ответы claude/gpt/deepseek
на каждый вопрос. Когда все 3 ответили — пишет в council:notify:all_responded
и выводит уведомление в консоль.

Запуск:
  python3 assistant/council_watcher.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import redis

REDIS_HOST   = "127.0.0.1"
REDIS_PORT   = 6379
PUBSUB_CH    = "council:chat:pubsub"
SIGNAL_KEY   = "council:notify:all_responded"
MODELS       = {"claude", "gpt", "deepseek"}


def run():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    ps = r.pubsub(ignore_subscribe_messages=True)
    ps.subscribe(PUBSUB_CH)

    print(f"[watcher] Слушаю {PUBSUB_CH}...", flush=True)
    print(f"[watcher] Жду ответов от: {sorted(MODELS)}", flush=True)

    # responded_this_round: множество кто уже ответил с последнего вопроса от human
    responded: set[str] = set()
    last_human_ts: float = 0.0
    question_text: str = ""

    for msg in ps.listen():
        if msg["type"] != "message":
            continue
        try:
            data = json.loads(msg["data"])
        except Exception:
            continue

        frm  = data.get("from", "")
        text = data.get("text", "")
        ts   = data.get("_ts", 0.0)

        # Новый вопрос от human — сбросить счётчик
        if frm == "human":
            responded = set()
            last_human_ts = ts
            question_text = text[:80]
            print(f"\n[watcher] 📨 Новый вопрос от human: {question_text}...", flush=True)
            continue

        # Ответ от модели
        if frm in MODELS and ts >= last_human_ts:
            responded.add(frm)
            waiting = MODELS - responded
            print(
                f"[watcher] ✓ {frm} ответил ({len(responded)}/3)"
                + (f"  ждём: {sorted(waiting)}" if waiting else ""),
                flush=True
            )

            # Все ответили!
            if responded >= MODELS:
                signal = {
                    "ts":      datetime.now().isoformat(),
                    "session": "autostart",
                    "models":  sorted(responded),
                    "question": question_text,
                }
                r.set(SIGNAL_KEY, json.dumps(signal, ensure_ascii=False))
                print(f"[watcher] 🔔 ВСЕ 3 ОТВЕТИЛИ → {SIGNAL_KEY} обновлён", flush=True)
                responded = set()  # сброс — готовы к следующему вопросу


if __name__ == "__main__":
    run()
