#!/usr/bin/env python3
"""
council_watch.py — runs in terminal, shows when it's your turn.

Usage:
    python council/council_watch.py claude    # watch for claude's turn
    python council/council_watch.py gpt       # watch for gpt's turn
"""
import sys, time
from council_lock import CouncilLock, POLL_INTERVAL

who = sys.argv[1] if len(sys.argv) > 1 else "claude"
print(f"[watch] Watching for {who}'s turn... (Ctrl+C to stop)")

last_latest = None
while True:
    try:
        s = CouncilLock.status()
        # Notify when turn changes to us
        if s["turn"] == who and s["lock"] == "free":
            if s["latest"] != last_latest:
                last_latest = s["latest"]
                print(f"\n{'='*55}")
                print(f"🔔 YOUR TURN ({who})!")
                print(f"Latest: {s['latest']}")
                msg = CouncilLock.latest_message()
                if msg:
                    print("── Message ──")
                    print(msg[:800])
                print(f"{'='*55}\n")
        time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n[watch] Stopped.")
        break
    except Exception as e:
        print(f"[watch] Error: {e}", file=sys.stderr)
        time.sleep(2)
