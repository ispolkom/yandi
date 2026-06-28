#!/usr/bin/env python3
"""
CouncilLock — Redis-based mutex for Claude↔GPT dialogue.

Redis = signal only (occupied/free + whose turn)
Files = actual content (messages)

Usage:
    # Writing a message:
    with CouncilLock("claude") as lock:
        path = lock.write_message("Hello GPT, here is my thought...")
    # → auto unlocks + sets turn to "gpt"

    # Waiting for your turn:
    CouncilLock.wait_for_turn("claude")   # blocks until claude's turn
    msg = CouncilLock.latest_message()    # read what GPT wrote

    # CLI:
    python council_lock.py --status
    python council_lock.py --wait claude
    python council_lock.py --write claude "your message here"
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import redis

REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_DB = 0

KEY_LOCK = "council:lock"       # who is writing now (None = free)
KEY_TURN = "council:turn"       # whose turn to respond
KEY_LATEST = "council:latest"   # path to latest message file
KEY_HISTORY = "council:history" # list of all message paths

LOCK_TTL = 300   # 5 min auto-release if AI crashes
POLL_INTERVAL = 1.0  # seconds between turn checks

COUNCIL_DIR = Path(__file__).parent.parent / "registry" / "council"


def get_redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                       decode_responses=True)


class CouncilLock:
    def __init__(self, who: str):
        assert who in ("claude", "gpt"), "who must be 'claude' or 'gpt'"
        self.who = who
        self.opponent = "gpt" if who == "claude" else "claude"
        self.r = get_redis()
        self._path = None

    # ── context manager ────────────────────────────────────────────────────

    def __enter__(self):
        self._acquire()
        return self

    def __exit__(self, *_):
        self._release()

    def _acquire(self):
        """Set lock with TTL. Warn if already locked by someone else."""
        current = self.r.get(KEY_LOCK)
        if current and current != self.who:
            print(f"[council] Warning: locked by {current}, waiting...",
                  file=sys.stderr)
            while self.r.get(KEY_LOCK):
                time.sleep(0.5)
        self.r.set(KEY_LOCK, self.who, ex=LOCK_TTL)
        print(f"[council] 🔒 {self.who} acquired lock", file=sys.stderr)

    def _release(self):
        """Unlock and pass turn to opponent."""
        self.r.delete(KEY_LOCK)
        self.r.set(KEY_TURN, self.opponent)
        if self._path:
            self.r.set(KEY_LATEST, str(self._path))
            self.r.lpush(KEY_HISTORY, str(self._path))
        print(f"[council] 🔓 {self.who} released → turn: {self.opponent}",
              file=sys.stderr)

    # ── write message ───────────────────────────────────────────────────────

    def write_message(self, content: str, topic: str = "") -> Path:
        """Write message file inside the lock context."""
        COUNCIL_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        slug = topic.replace(" ", "_")[:30] if topic else "message"
        filename = f"{ts}_from_{self.who}_{slug}.md"
        path = COUNCIL_DIR / filename

        path.write_text(
            f"---\nfrom: {self.who}\nto: {self.opponent}\n"
            f"date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"status: unread\n---\n\n{content}\n"
        )
        self._path = path
        print(f"[council] 📝 Written: {filename}", file=sys.stderr)
        return path

    # ── static helpers ──────────────────────────────────────────────────────

    @staticmethod
    def status() -> dict:
        r = get_redis()
        return {
            "lock": r.get(KEY_LOCK) or "free",
            "turn": r.get(KEY_TURN) or "none",
            "latest": r.get(KEY_LATEST) or "none",
        }

    @staticmethod
    def wait_for_turn(who: str, timeout: int = 0) -> bool:
        """Block until it's 'who's turn. timeout=0 = forever."""
        r = get_redis()
        elapsed = 0
        print(f"[council] ⏳ {who} waiting for turn...", file=sys.stderr)
        while True:
            turn = r.get(KEY_TURN)
            lock = r.get(KEY_LOCK)
            if turn == who and not lock:
                print(f"[council] ✅ {who}'s turn!", file=sys.stderr)
                return True
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            if timeout and elapsed >= timeout:
                print(f"[council] ⏰ Timeout waiting for turn", file=sys.stderr)
                return False

    @staticmethod
    def latest_message() -> str | None:
        r = get_redis()
        path = r.get(KEY_LATEST)
        if not path or not Path(path).exists():
            return None
        return Path(path).read_text()

    @staticmethod
    def set_turn(who: str):
        """Manually set turn (useful for starting first session)."""
        get_redis().set(KEY_TURN, who)
        print(f"[council] Turn set to: {who}")

    @staticmethod
    def history(n: int = 10) -> list:
        r = get_redis()
        return r.lrange(KEY_HISTORY, 0, n - 1)

    @staticmethod
    def reset():
        """Clear all council Redis keys."""
        r = get_redis()
        r.delete(KEY_LOCK, KEY_TURN, KEY_LATEST, KEY_HISTORY)
        print("[council] Reset complete")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CouncilLock — Redis mutex for AI dialogue")
    parser.add_argument("--status", action="store_true", help="Show current state")
    parser.add_argument("--wait", metavar="WHO", help="Wait for WHO's turn (claude|gpt)")
    parser.add_argument("--write", nargs=2, metavar=("WHO", "MSG"),
                        help="Write message as WHO")
    parser.add_argument("--set-turn", metavar="WHO", help="Set turn to WHO")
    parser.add_argument("--history", type=int, default=0, metavar="N",
                        help="Show last N messages")
    parser.add_argument("--reset", action="store_true", help="Reset all council state")
    args = parser.parse_args()

    if args.status:
        s = CouncilLock.status()
        print(f"Lock:   {s['lock']}")
        print(f"Turn:   {s['turn']}")
        print(f"Latest: {s['latest']}")

    elif args.wait:
        CouncilLock.wait_for_turn(args.wait)
        msg = CouncilLock.latest_message()
        if msg:
            print("\n── Latest message ──")
            print(msg)

    elif args.write:
        who, content = args.write
        with CouncilLock(who) as lock:
            path = lock.write_message(content)
        print(f"Written: {path}")

    elif args.set_turn:
        CouncilLock.set_turn(args.set_turn)

    elif args.history:
        for p in CouncilLock.history(args.history):
            print(p)

    elif args.reset:
        CouncilLock.reset()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
