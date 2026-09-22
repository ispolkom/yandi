"""
pet/pet_orch_ai_opinion_regression_test.py — the Оркестратор tab's "альтернативное мнение" AI-chat
toggles (Клод/ГПТ/ДипСик/Кими) deliver their raw answer into the ORCH tab, never the Интернет-чат tab,
never touch its TURN_KEY, and never enter its relay-chain / broadcast-synthesis machinery.

    A SIDE-CHANNEL OPINION IS NOT A TURN IN A CONVERSATION.
    /api/orch/ai_opinion (new) starts a plain _relay_ctx entry tagged tab="orch"; /api/ext/result (shared
    with the Интернет-чат tab's own relay/broadcast flow) must branch on that tag: write to ORCH_MSGS_KEY
    with turn_next="human" always and leave council:chat:turn alone — while the EXISTING Интернет-чат path
    (no tab tag, i.e. tab="inet" default) must behave exactly as before (regression check D).

Against a REAL Redis (started for this test, thrown away after) and the real ASGI app — not a copy of the
endpoint logic.

Run: python -m pet.pet_orch_ai_opinion_regression_test
"""
from __future__ import annotations

import json
import os
import re
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

    tmp = Path(tempfile.mkdtemp(prefix="yandi-orch-ai-opinion-"))
    proc = subprocess.Popen(
        [redis_bin, "--port", "6379", "--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--dir", str(tmp)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        import redis
        import redis.asyncio as aioredis
        for _ in range(50):
            try:
                redis.Redis(host="127.0.0.1", port=6379).ping()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        else:
            print("SKIP: redis-server did not come up")
            return 0

        import pet.chat_orch as chat_orch  # noqa: F401 — production import order
        import pet.council_chat_server as server
        from fastapi.testclient import TestClient

        c = TestClient(server.app, base_url="http://127.0.0.1:9010")
        r = aioredis.from_url("redis://127.0.0.1:6379", decode_responses=True)

        # ── A: only active, valid models are accepted ──
        now = time.time()
        server._model_last_seen["deepseek"] = now
        server._model_last_seen["claude"] = now
        server._model_last_seen.pop("gpt", None)
        server._model_last_seen.pop("kimi", None)
        for m in ("claude", "gpt", "deepseek", "kimi"):
            server._bridge_state[f"{m}_blocked"] = False

        resp = c.post("/api/orch/ai_opinion", json={"text": "тестовый вопрос", "models": ["deepseek", "gpt", "not-a-model"]})
        d = resp.json()
        check("A1 request accepted", resp.status_code == 200 and d.get("ok") is True, repr(d))
        check("A2 only the ACTIVE requested model is sent to (gpt not active, not-a-model not real)", d.get("sent_to") == ["deepseek"], repr(d))
        task_id = d.get("task_id", "")
        check("A3 a task_id is minted", bool(task_id))
        check("A4 the relay ctx is tagged tab=orch", server._relay_ctx.get(task_id, {}).get("tab") == "orch")
        check("A5 pending set is exactly the sent-to models", server._relay_ctx.get(task_id, {}).get("pending") == {"deepseek"})

        # ── A5b: the model is actually asked to disclose its sourcing (owner's request 2026-09-22) ──
        queued = server._ext_queues["deepseek"].get_nowait()
        check("A5b the queued task is for our task_id", queued.get("task_id") == task_id, repr(queued))
        check("A5c the original question text is still there, verbatim", queued.get("text", "").startswith("тестовый вопрос"), repr(queued))
        check("A5d …plus the sourcing-disclosure instruction (own opinion vs web, list sources, never invent a link)",
              queued.get("text", "") == "тестовый вопрос" + server._ORCH_AI_OPINION_SOURCING_SUFFIX, repr(queued))
        check("A5e the ctx itself still keeps the CLEAN original text (suffix is only added to what's sent out)",
              server._relay_ctx.get(task_id, {}).get("text") == "тестовый вопрос")

        # ── A6: no valid models at all -> rejected outright ──
        resp2 = c.post("/api/orch/ai_opinion", json={"text": "q", "models": ["not-a-model", "also-fake"]})
        check("A6 no real model requested -> ok:false", resp2.json().get("ok") is False)

        # ── A7: valid model requested but none currently active -> ok:true, nothing sent ──
        resp3 = c.post("/api/orch/ai_opinion", json={"text": "q", "models": ["kimi"]})
        d3 = resp3.json()
        check("A7 valid-but-inactive model -> ok:true, sent_to empty", d3.get("ok") is True and d3.get("sent_to") == [])

        # ── B/C: the extension posting its answer lands in ORCH, not INET, turn_next always human, TURN_KEY untouched ──
        import asyncio

        async def _redis_setup_and_checks():
            # Seeded to something OTHER than what the orch-opinion path would compute (turn_next is always
            # "human" there) — a mutant that force-writes TURN_KEY for the orch path too would still leave it
            # "human", indistinguishable from "left untouched", if this were seeded "human" too.
            await r.set(server.TURN_KEY, "deepseek")
            await r.delete(server.ORCH_MSGS_KEY)
            await r.delete(server.INET_MSGS_KEY)

            resp4 = c.post("/api/ext/result", json={"from": "deepseek", "text": "ДипСик: моё альтернативное мнение", "task_id": task_id})
            check("B1 extension result accepted", resp4.json().get("ok") is True, repr(resp4.json()))

            orch_raw = await r.lrange(server.ORCH_MSGS_KEY, 0, 10)
            inet_raw = await r.lrange(server.INET_MSGS_KEY, 0, 10)
            check("B2 the opinion landed in ORCH_MSGS_KEY", len(orch_raw) == 1, repr(orch_raw))
            check("B3 …and NOT in INET_MSGS_KEY (== MESSAGES_KEY)", len(inet_raw) == 0, repr(inet_raw))
            if orch_raw:
                m = json.loads(orch_raw[0])
                check("B4 message tab is orch", m.get("tab") == "orch", repr(m))
                check("B5 message from is the model that answered", m.get("from") == "deepseek")
                check("B6 turn_next is always human (never a RELAY_CHAIN wait state)", m.get("turn_next") == "human", repr(m))

            turn_after = await r.get(server.TURN_KEY)
            check("C1 council:chat:turn (Интернет-чат's own status) is left exactly as it was", turn_after == "deepseek", repr(turn_after))
            check("C2 the relay ctx is cleaned up once its only pending model answered", task_id not in server._relay_ctx)

            # ── D: regression — the EXISTING Интернет-чат broadcast path is unaffected ──
            server._model_last_seen["gpt"] = time.time()
            resp5 = c.post("/api/council/broadcast", json={"text": "inet question", "models": ["gpt"]})
            d5 = resp5.json()
            check("D1 inet broadcast still works", d5.get("ok") is True and d5.get("sent_to") == ["gpt"], repr(d5))
            inet_task = d5.get("task_id", "")
            check("D2 its ctx has NO orch tag (defaults to inet behaviour)", server._relay_ctx.get(inet_task, {}).get("tab") != "orch")

            resp6 = c.post("/api/ext/result", json={"from": "gpt", "text": "GPT inet answer", "task_id": inet_task})
            check("D3 extension result still accepted", resp6.json().get("ok") is True)
            inet_raw2 = await r.lrange(server.INET_MSGS_KEY, 0, 10)
            orch_raw2 = await r.lrange(server.ORCH_MSGS_KEY, 0, 10)
            # broadcast() itself already wrote the "human" question into INET_MSGS_KEY (unlike ai_opinion, which
            # writes nothing there) -> 2 entries: the human question + this extension answer.
            check("D4 the inet answer lands in INET_MSGS_KEY as before", len(inet_raw2) == 2, repr(inet_raw2))
            check("D5 …and NOT in ORCH_MSGS_KEY (still exactly the one orch message from B2)", len(orch_raw2) == 1, repr(orch_raw2))
            turn_after2 = await r.get(server.TURN_KEY)
            check("D6 TURN_KEY DOES change for a real inet answer (unlike the orch opinion path)", turn_after2 != "human", repr(turn_after2))
            await r.aclose()

        asyncio.run(_redis_setup_and_checks())

        # ── E: source-shape checks (static) — the guard structurally exists, not just behaviourally by luck ──
        src = (ROOT / "pet" / "council_chat_server.py").read_text(encoding="utf-8")
        check("E1 TURN_KEY write is conditioned on not is_orch_opinion",
              re.search(r'if not is_orch_opinion:\s*\n\s*await r\.set\(TURN_KEY, turn_next\)', src) is not None)
        check("E2 the orch branch returns early before the relay-chain/broadcast-pending code",
              src.index("if is_orch_opinion:\n        # Альтернативное мнение") < src.index("# Relay-цепочка: передаём эстафету"))
        check("E3 msg_key is ORCH_MSGS_KEY only in the is_orch_opinion branch", "msg_key   = ORCH_MSGS_KEY" in src)
        _orch_block = re.search(r'if is_orch_opinion:(.*?)# Relay-цепочка', src, re.S)
        check("E4 the orch branch RETURNS (never falls through into the relay-chain/broadcast-pending code below)",
              bool(_orch_block) and 'return {"ok": True}' in _orch_block.group(1), repr(_orch_block.group(1) if _orch_block else None))
        check("E5 the sourcing-disclosure suffix is actually appended to what's queued out (not left unused)",
              "prompt_text = text + _ORCH_AI_OPINION_SOURCING_SUFFIX" in src and '"text": prompt_text' in src)

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
