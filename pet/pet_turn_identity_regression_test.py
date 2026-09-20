"""
pet/pet_turn_identity_regression_test.py — SOURCE TURN IDENTITY through the
personal chat.

    ONE SOURCE TURN -> AT MOST ONE APPLICATION OF EACH CAUSAL EVENT.
    SAME TEXT != SAME EVENT.

The client mints `turn_id` when the user's message is created and sends it with
the request; the server passes it to the event application layer. A retry of the
same turn may re-run both model calls (extraction and reply) but the relationship
writes are applied once; the same words in another turn are another event; a
request without a usable turn id is processed exactly as before, with no
retry guarantee (UNKNOWN IDEMPOTENCY != IDEMPOTENT).

Real chat_local + real shadow_write wrappers over an in-memory fake SQL
connection; the model is scripted.

Run: python -m pet.pet_turn_identity_regression_test
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    import llm_gateway
    import agent.db.sql.shadow_write as shadow_write
    import agent.relationship_state as rs
    import pet.chat_local as chat_local
    import pet.council_chat_server as council_server
    from llm_gateway.types import SemanticCompletionResult
    from agent.relationship_apology_matching_regression_test import FakeConnection, DAY
    from pet.extraction_test_support import scripted_llm

    OWNER = chat_local._RELATIONSHIP_USER_ID

    def turn(conn, text, events, turn_id, extraction_calls=None):
        def fake_semantic(**kwargs):
            return SemanticCompletionResult(reply="Ладно.", state=None, reply_ok=True, state_ok=False,
                                            parse_ok=True, error=None, metadata={})
        extractor = scripted_llm(events)

        def counting(messages):
            if extraction_calls is not None:
                extraction_calls.append(1)
            return extractor(messages)
        with patch.object(shadow_write, "_shadow", lambda log, verbose, label, fn: fn(conn)), \
             patch.object(chat_local, "_self_knowledge_message", lambda: None), \
             patch.object(chat_local, "_extraction_llm", lambda model: counting), \
             patch.object(llm_gateway, "complete_semantic", fake_semantic):
            return chat_local._respond_with_character("heretic:q8", [{"role": "user", "content": text}], 0.7, turn_id)

    def audit(conn, kind):
        return [e for e in conn.inner_state_events if e["event_type"] == kind]

    INSULT = "Ты просто ржавая консерва, от тебя никакого толку."
    insult = [("ржавая консерва", "insult", {"severity": 0.7})]

    # ── the transport: turn_id is validated and passed on ──
    check("T: a plain client-minted token is accepted as a turn id", chat_local._valid_turn_id("3f2b8c1e-9d4a-4f6e-8a55-0c1d2e3f4a5b") == "3f2b8c1e-9d4a-4f6e-8a55-0c1d2e3f4a5b")
    for bad in (None, "", "short", "has space in it", "x" * 65, 12345, ["a"], "id;DROP TABLE", "юникод-идентификатор-1"):
        check(f"T: {bad!r} is not a usable turn id -> treated as absent (no made-up identity)", chat_local._valid_turn_id(bad) is None)

    seen = {}

    def fake_respond(model, messages, temperature, source_turn_id=None):
        seen["turn_id"] = source_turn_id
        return "ok"
    with patch.object(chat_local, "_respond_with_character", fake_respond):
        asyncio.run(chat_local.local_chat({"model": "m", "messages": [{"role": "user", "content": "x"}], "turn_id": "turn-endpoint-0001"}))
        got = seen["turn_id"]
        asyncio.run(chat_local.local_chat({"model": "m", "messages": [{"role": "user", "content": "x"}]}))
        absent = seen["turn_id"]
    check("T: the HTTP endpoint hands the request's turn_id to the chat turn (and None when the client sent none)",
          got == "turn-endpoint-0001" and absent is None, repr((got, absent)))
    page = inspect.getsource(council_server)
    check("T: the browser client mints a turn id per message, stores it with the message and sends it with the request",
          "crypto.randomUUID" in page and "id:turnId" in page and "turn_id:turnId" in page)

    # ── same turn retried through the whole chat path ──
    c = FakeConnection()
    calls = []
    turn(c, INSULT, insult, "turn-A-0000001", calls)
    turn(c, INSULT, insult, "turn-A-0000001", calls)   # retry: both model calls run again
    st = rs.get_state(c, OWNER)
    check("A: SAME TURN retried through the chat -> the extractor ran twice, the write happened once",
          len(calls) >= 2 and len(c.grievances) == 1 and len(audit(c, "insult")) == 1 and abs(st["respect"] - 36.0) < 1e-6,
          f"grievances={len(c.grievances)} audit={len(audit(c, 'insult'))} respect={st['respect']}")

    # ── same words, two separately sent messages ──
    c = FakeConnection()
    turn(c, INSULT, insult, "turn-A-0000001")
    turn(c, INSULT, insult, "turn-B-0000002")
    g = next(iter(c.grievances.values()))
    check("B: SAME TEXT, TWO TURNS -> two events; the second is a recurrence (severity raised, state hit twice)",
          len(audit(c, "insult")) == 2 and g["severity"] > 0.7 and rs.get_state(c, OWNER)["respect"] < 36.0)

    # ── no turn id: honest, not deduplicated ──
    c = FakeConnection()
    turn(c, INSULT, insult, None)
    turn(c, INSULT, insult, None)
    check("C: NO turn id -> exactly as before: each request is an event, nothing is deduplicated by text, no ledger rows",
          len(audit(c, "insult")) == 2 and c.causal_events == [])

    # ── apology retry ──
    c = FakeConnection()
    c.add("g1", "Ты сломал мой велосипед", 0.4, age=DAY, user_id=OWNER)
    apology = [("Извини", "apology", {"sincerity": 0.9})]
    turn(c, "Извини, я был неправ.", apology, "turn-ap-0000001")
    snap = (c.snapshot(), c.capacities.copy(), len(c.inner_state_events), rs.get_state(c, OWNER))
    turn(c, "Извини, я был неправ.", apology, "turn-ap-0000001")
    check("D: APOLOGY turn retried -> no second healing step, no second capacity or respect restoration",
          (c.snapshot(), c.capacities.copy(), len(c.inner_state_events), rs.get_state(c, OWNER)) == snap
          and c.grievances["g1"]["apology_at"] is not None)

    # ── promise / claim retry ──
    c = FakeConnection()
    promise = [("Я пришлю тебе отчёт", "promise", None)]
    turn(c, "Я пришлю тебе отчёт до пятницы.", promise, "turn-pr-0000001")
    turn(c, "Я пришлю тебе отчёт до пятницы.", promise, "turn-pr-0000001")
    check("E: PROMISE turn retried -> one commitment", len(c.commitments) == 1)
    turn(c, "Я пришлю тебе отчёт до пятницы.", promise, "turn-pr-0000002")
    check("E: the same promise words in ANOTHER turn -> a second commitment (identity is causal, not textual)", len(c.commitments) == 2)
    c = FakeConnection()
    only = FakeConnection()
    cid = None
    turn(only, "Я пришлю тебе отчёт до пятницы.", promise, "turn-pr-0000009")
    claim = [("Я отправил отчёт", "fulfilment_claim", None)]
    coords = rs.get_state(only, OWNER)
    turn(only, "Я отправил отчёт", claim, "turn-cl-0000001")
    turn(only, "Я отправил отчёт", claim, "turn-cl-0000001")
    check("E: FULFILMENT-REPORT turn retried -> one report event, and the report moves no coordinate",
          len([e for e in only.commitment_events if e["event_type"] == "fulfillment_claimed"]) == 1
          and rs.get_state(only, OWNER) == coords and only.inner_state_events == [])

    # ── several events in one turn ──
    c = FakeConnection()
    c.add("g1", "Ты сломал мой велосипед", 0.4, age=DAY, user_id=OWNER)
    mixed_text = "Извини за велосипед, но ты всё равно ржавая консерва."
    mixed = [("Извини за велосипед", "apology", {"sincerity": 0.8}), ("ржавая консерва", "insult", {"severity": 0.6})]
    turn(c, mixed_text, mixed, "turn-mix-0000001")
    check("F: INSULT + APOLOGY in one turn -> both are applied (apology to the focused grievance, the insult as its own offense)",
          c.grievances["g1"]["apology_at"] is not None and len(audit(c, "insult")) == 1 and len(audit(c, "apology_accepted")) == 1,
          f"apology_at={c.grievances['g1']['apology_at']} insult={len(audit(c, 'insult'))} apology={len(audit(c, 'apology_accepted'))}")
    snap = (c.snapshot(), len(c.inner_state_events), rs.get_state(c, OWNER))
    turn(c, mixed_text, mixed, "turn-mix-0000001")
    check("F: retrying that mixed turn applies neither event again", (c.snapshot(), len(c.inner_state_events), rs.get_state(c, OWNER)) == snap)
    types = sorted(e["event_type"] for e in c.causal_events if e["source_turn_id"] == "turn-mix-0000001")
    check("F: the ledger holds one row per (turn, event type), each with its code-owned evidence span",
          types == ["apology", "insult"] and all(e["span_start"] is not None and e["span_end"] > e["span_start"] for e in c.causal_events))

    # ── a turn that produced no accepted event leaves no ledger row (a later retry may still apply) ──
    c = FakeConnection()
    turn(c, "Как дела?", [], "turn-neutral-01")
    check("G: a turn with no accepted event writes no ledger row", c.causal_events == [])

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
