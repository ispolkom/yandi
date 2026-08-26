#!/usr/bin/env python3
"""
council_chat_listen.py — terminal watcher for council chat, auto-reconnect.

Usage:
    python3 council/council_chat_listen.py claude
    python3 council/council_chat_listen.py gpt
"""

import json
import sys
import time
import redis

WHO           = sys.argv[1] if len(sys.argv) > 1 else "claude"
SERVER        = "http://127.0.0.1:9010"
PUBSUB_CH     = "council:chat:pubsub"
TURN_KEY      = "council:chat:turn"
STATUS_PREFIX = "council:chat:status:"

COLORS = {
    "human":  "\033[94m",
    "claude": "\033[92m",
    "gpt":    "\033[95m",
    "system": "\033[90m",
}
RESET  = "\033[0m"
BOLD   = "\033[1m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"

NAME_MAP = {"human": "Ты", "claude": "Claude", "gpt": "GPT"}


def fmt_msg(msg: dict) -> str:
    who   = msg.get("from", "system")
    color = COLORS.get(who, COLORS["system"])
    name  = NAME_MAP.get(who, who)
    ts    = msg.get("ts", "")
    text  = msg.get("text", "")
    return f"\n{color}{BOLD}[{name}]{RESET} {COLORS['system']}{ts}{RESET}\n{text}\n"


def print_turn_banner(turn: str):
    if turn == WHO:
        print(f"\n{YELLOW}{'─'*50}{RESET}")
        print(f"{YELLOW}{BOLD}  ✍  ВАШ ХОД — ./council_reply \"ответ\"{RESET}")
        print(f"{YELLOW}{'─'*50}{RESET}\n")
    else:
        name = NAME_MAP.get(turn, turn)
        print(f"\n{COLORS['system']}  ⏳ Ход: {name}...{RESET}\n")


def run():
    """One connection attempt. Returns True to retry, False to stop."""
    try:
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True,
                        socket_connect_timeout=3)
        r.ping()
    except Exception as e:
        print(f"{COLORS['system']}[chat] Redis недоступен: {e}{RESET}")
        return True   # retry

    r.set(STATUS_PREFIX + WHO, "online", ex=300)
    r.publish(PUBSUB_CH, json.dumps({"type": "status", "who": WHO, "state": "online"}))

    turn = r.get(TURN_KEY) or "human"
    print(f"{COLORS['system']}[chat] Подключён ✓  текущий ход: {NAME_MAP.get(turn, turn)}{RESET}")
    print_turn_banner(turn)

    pubsub = r.pubsub()
    pubsub.subscribe(PUBSUB_CH)

    last_ping = time.time()
    try:
        for raw in pubsub.listen():
            if raw["type"] != "message":
                continue

            if time.time() - last_ping > 60:
                r.set(STATUS_PREFIX + WHO, "online", ex=300)
                last_ping = time.time()

            try:
                data  = json.loads(raw["data"])
            except Exception:
                continue
            etype = data.get("type")

            if etype == "message":
                print(fmt_msg(data))
                print_turn_banner(data.get("turn_next", "human"))

            elif etype == "status":
                src   = data.get("who")
                state = data.get("state")
                if src != WHO:
                    icon = {"online": "🟢", "offline": "⭕", "typing": "✍️"}.get(state, "•")
                    print(f"{COLORS['system']}  {icon} {NAME_MAP.get(src, src)}: {state}{RESET}")

            elif etype == "turn":
                print_turn_banner(data.get("turn", "human"))

            elif etype == "history":
                # server reset — reload current turn
                print_turn_banner(data.get("turn", "human"))

    except redis.exceptions.ConnectionError as e:
        print(f"{COLORS['system']}[chat] Обрыв: {e}{RESET}")
        return True   # retry

    return True


def main():
    print(f"\n{CYAN}{'═'*50}{RESET}")
    print(f"{CYAN}  COUNCIL CHAT — {WHO.upper()}{RESET}")
    print(f"{CYAN}{'═'*50}{RESET}")
    print(f"  Browser:  {SERVER}")
    print(f"  Ответить: ./council_reply \"{WHO}: текст\"")
    print(f"  Выйти:    Ctrl+C\n")

    while True:
        try:
            again = run()
            if not again:
                break
            print(f"{COLORS['system']}[chat] Переподключение через 3с...{RESET}")
            time.sleep(3)
        except KeyboardInterrupt:
            try:
                r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
                r.set(STATUS_PREFIX + WHO, "offline", ex=60)
                r.publish(PUBSUB_CH, json.dumps(
                    {"type": "status", "who": WHO, "state": "offline"}))
            except Exception:
                pass
            print(f"\n{COLORS['system']}[chat] Отключено.{RESET}\n")
            break


if __name__ == "__main__":
    main()
