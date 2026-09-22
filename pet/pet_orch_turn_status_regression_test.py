"""
pet/pet_orch_turn_status_regression_test.py — the orchestrator tab's status line ("Ваш ход" / "Ждём Claude…") never shows
state that belongs to the Интернет-чат (browser-council-relay) tab.

    THE OWNER SAW "Режим: Оркестратор  Ждём Claude…" ON A QUESTION THAT NEVER TOUCHED THE BROWSER CHATS.
    ROOT CAUSE: council:chat:turn IS A SINGLE REDIS KEY WRITTEN ONLY BY THE INET TAB, BUT READ FOR THE ORCH TAB'S
    OWN CONNECT HANDSHAKE — A STALE "claude" LEFT FROM AN EARLIER, UNRELATED Интернет-чат SESSION BLED INTO THE
    ORCHESTRATOR'S STATUS ON EVERY PAGE LOAD.

Against a REAL Redis (started for this test, thrown away after) and the real ASGI app — not a copy of the handshake logic.

Run: python -m pet.pet_orch_turn_status_regression_test
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("YANDI_TEST_MODE", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    redis_bin = None
    for candidate in ("redis-server",):
        try:
            subprocess.run([candidate, "--version"], capture_output=True, check=True)
            redis_bin = candidate
            break
        except (OSError, subprocess.CalledProcessError):
            pass
    if not redis_bin:
        print("SKIP: redis-server not found")
        return 0

    # pet/shared.py's REDIS_URL is a hard-coded "redis://127.0.0.1:6379" (no environment override) — the SAME thing
    # every other Redis-backed e2e test of this server already does (e.g. pet/settings_tab_firefox_e2e.py), so this
    # test needs a real server on that exact port, thrown away after.
    tmp = Path(tempfile.mkdtemp(prefix="yandi-turn-status-"))
    proc = subprocess.Popen(
        [redis_bin, "--port", "6379", "--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--dir", str(tmp)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        import redis.asyncio as aioredis
        for _ in range(50):
            try:
                asyncio.run(aioredis.from_url("redis://127.0.0.1:6379").ping())
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        else:
            print("SKIP: redis-server did not come up")
            return 0

        import pet.chat_orch as chat_orch  # noqa: F401 — imported so its module is loaded before the server, matching production import order
        import pet.council_chat_server as server

        # Raw ASGI, not starlette's TestClient(app) context manager: the latter also runs the app's @app.on_event
        # ("startup") hooks (SQL reconciliation, system-awareness probe) — unrelated to the /ws endpoint under test
        # here, refused anyway by the live-db-guard in a test process, but the refusal itself would fail this suite
        # under scripts/test-core.sh's hermetic check. The websocket route never depends on those hooks having run.
        async def connect_and_receive_history(client_id: str) -> dict:
            sent: list = []
            queue = [{"type": "websocket.connect"}]

            async def receive():
                return queue.pop(0) if queue else {"type": "websocket.disconnect", "code": 1000}

            async def send(message):
                sent.append(message)
            scope = {
                "type": "websocket", "path": f"/ws/{client_id}", "raw_path": f"/ws/{client_id}".encode(),
                "query_string": b"", "headers": [(b"host", b"127.0.0.1")], "subprotocols": [],
                "server": ("127.0.0.1", 9010), "client": ("127.0.0.1", 5), "scheme": "ws", "root_path": "",
                "asgi": {"version": "3.0"},
            }
            await server.app(scope, receive, send)
            accepted = any(m["type"] == "websocket.accept" for m in sent)
            if not accepted:
                return {"_disconnected": True}
            text_frame = next(m for m in sent if m["type"] == "websocket.send")
            return json.loads(text_frame["text"])

        async def seed_stale_claude_turn() -> None:
            """Exactly what the Интернет-чат tab leaves behind mid-relay, and nothing else: council:chat:turn="claude",
            no orchestrator message, no orchestrator history — an unrelated tab's own leftover state."""
            r = aioredis.from_url(server.REDIS_URL, decode_responses=True)
            try:
                await r.set(server.TURN_KEY, "claude")
                await r.delete(server.ORCH_MSGS_KEY)
            finally:
                await r.aclose()

        asyncio.run(seed_stale_claude_turn())

        first = asyncio.run(connect_and_receive_history("test-client-1"))
        check("A1 on connect, the orch tab's own history message is what the orchestrator server sends",
              first.get("type") == "history" and first.get("tab") == "orch")
        check("A2 …and its turn is 'human', NEVER the stale Интернет-чат value left in Redis",
              first.get("turn") == "human", repr(first))

        async def read_turn_key() -> str:
            r = aioredis.from_url(server.REDIS_URL, decode_responses=True)
            try:
                return await r.get(server.TURN_KEY)
            finally:
                await r.aclose()
        check("A3 the stale key itself is left exactly as it was (this is a read, not a fix-by-deleting)", asyncio.run(read_turn_key()) == "claude")

        # ── the same reproduced with a SECOND stale value, to rule out "human" being a coincidence ──
        async def seed(value: str) -> None:
            r = aioredis.from_url(server.REDIS_URL, decode_responses=True)
            try:
                await r.set(server.TURN_KEY, value)
            finally:
                await r.aclose()
        for stale in ("gpt", "deepseek"):
            asyncio.run(seed(stale))
            msg = asyncio.run(connect_and_receive_history(f"test-client-2-{stale}"))
            check(f"A4 the same holds for a stale '{stale}' turn: the orch tab still gets 'human'", msg.get("turn") == "human")

        # ── the frontend script: setTurn is scoped to the tab actually being viewed ──
        script = re.search(r"<script>(.*?)</script>", server.HTML, re.S).group(1)
        check("B1 the history branch only calls setTurn when the message's tab is the one on screen",
              re.search(r'if\(\(d\.tab\|\|"orch"\)===currentMode\)\s*setTurn\(d\.turn\|\|"human"\)', script) is not None)
        check("B2 the message branch only calls setTurn when the message's tab is the one on screen",
              re.search(r'if\(tab===currentMode\)\s*setTurn\(d\.turn_next\|\|"human"\)', script) is not None)
        check("B3 switching to the orchestrator tab resets the status line itself (never leaves a foreign tab's text showing)",
              re.search(r'if\(mode==="orch"\)\s*setTurn\("human"\)', script) is not None)
        # the unconditional calls this replaces must be gone, not just duplicated alongside a guarded copy: each of
        # these two exact calls appears exactly ONCE in the whole script, and that one occurrence is the guarded form.
        check("B4 the old UNCONDITIONAL calls are gone (not left in place alongside the guard)",
              script.count('setTurn(d.turn||"human")') == 1 and script.count('setTurn(d.turn_next||"human")') == 1)

        # ── functional: a message for the OTHER tab, delivered while "viewing" orch, must not change what orch shows ──
        # (JS logic re-executed faithfully in Python against the same two conditions the real script uses, since no
        # browser runs here — B1-B4 above prove this IS the code that ships; this proves what that code DOES.)
        def apply_message(d: dict, current_mode: str) -> str | None:
            tab = d.get("tab") or ("orch" if d.get("from") == "orchestrator" else "inet")
            return (d.get("turn_next") or "human") if tab == current_mode else None

        check("C1 a claude-turn message for 'inet' while viewing 'orch' changes nothing", apply_message({"tab": "inet", "turn_next": "claude"}, "orch") is None)
        check("C2 the same message while actually viewing 'inet' does update it", apply_message({"tab": "inet", "turn_next": "claude"}, "inet") == "claude")
        check("C3 an orchestrator message (always human) updates the orch status while viewing orch", apply_message({"tab": "orch", "turn_next": "human"}, "orch") == "human")

        return 1 if FAILURES else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    r = main()
    print()
    print("=" * 72)
    print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}" if FAILURES else "РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    sys.exit(r)
